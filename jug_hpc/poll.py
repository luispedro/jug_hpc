'''The polling submitter's loop: ``jug-hpc submit``

:class:`Submitter` does one poll at a time (:meth:`Submitter.poll`): it asks
the scheduler about the jobs not yet known to have ended (and how those that
just ended did), counts the tasks of each class in a subprocess, and submits
the workers that the rule of :mod:`jug_hpc.submitter` asks for. :func:`run`
polls until the run is over.

The scheduler is asked before the tasks are counted, so that the tasks a job
ran before it ended are in the counts of the poll that sees it end (the
failed jobs of a class are forgotten when its tasks end; see
:func:`jug_hpc.submitter.update_failing`).

The decisions are the pure functions of :mod:`jug_hpc.submitter`; this module
does the I/O around them. The state kept between polls is small: the jobs
known to have ended (so they are not queried again), the jobs seen queued or
running (only the end of those is looked at: a job that had ended when first
queried may have ended before a fix), the previous poll's task counts, and the
jobs of each class that ended in error. Everything else is read again at each
poll, from the jugfile's store and ``.jug_hpc/jobs.json``, so a submitter can
be stopped and started again at any time (a new submitter gives stopped
classes another chance).

:func:`run` backs off while nothing changes: after a poll that finds the same
task counts and active workers as the one before and submits nothing, the wait
doubles, up to ``max_interval`` (default: :data:`MAX_INTERVAL_FACTOR` times
the interval). Any change, and any failed poll, brings it back to the
interval. This keeps the submitter cheap (each poll loads the jugfile and runs
``qstat``) during the long stretches when workers are busy, while
``max_interval`` bounds how late it notices tasks that became ready.

Jug's status cache (``jug status --cache``) is not used: it is fast because
it does not load the jugfile again, so it would miss the tasks created after a
``barrier()`` and the changes to the jugfile. Measured on a jugfile with 100k
tasks, a poll takes about 2 s and 150 MB, almost all of it hashing the tasks to
build the dependency graph, which only loading the jugfile can do.
(Without numpy installed, Jug 2.7.0's hashing tries to import numpy for
every task, which made the same poll take 11-14 s; fixed for the next Jug
release.)

Errors at the first poll are raised at once: they are most likely a problem
with the settings or the jugfile. Later, a poll that fails because the
scheduler or the jugfile could not be read (a ``SchedulerError`` or
``JugfileError``, e.g. ``qstat`` timing out, or the jugfile being edited) is
retried at the next poll, up to :data:`MAX_FAILED_POLLS` times in a row.
'''
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .classes import Classifier, counts_by_class
from .jugstate import JugfileError, project_task_counts
from .schedulers.base import JobState, SchedulerError
from .schedulers.checks import check_scheduler_settings
from .submit import check_jugdir, prepare_class, submit_worker, write_script
from .submitter import (
    Progress,
    active_workers,
    class_limits,
    is_settled,
    jobs_to_query,
    progress,
    stopped_classes,
    update_failing,
    workers_to_submit,
)

__all__ = [
    'MAX_FAILED_POLLS',
    'MAX_INTERVAL_FACTOR',
    'POLL_INTERVAL',
    'PollResult',
    'Submitter',
    'run',
]

#: Default seconds between polls
POLL_INTERVAL = 60

#: Default longest wait between polls, as a multiple of the interval
MAX_INTERVAL_FACTOR = 5

#: Polls in a row that may fail (after the first) before :func:`run` gives up
MAX_FAILED_POLLS = 5


def _now():
    return datetime.now(UTC)


@dataclass
class PollResult:
    '''What one poll found and did

    Attributes
    ----------
    by_class : dict
        Class name -> TaskCounts, every class, the default class last
    hit_barrier : bool
        Loading the jugfile stopped at a ``barrier()``
    active : dict
        Class name -> queued or running workers, before this poll's submissions
    report : ProgressReport
        Whether to keep polling
    stopped : dict
        Class name -> the jobs that ended in error, as ``(JobRecord,
        JobOutcome)`` pairs, for the classes that get no new workers (see
        :func:`jug_hpc.submitter.stopped_classes`)
    submitted : list of JobRecord
        The jobs submitted by this poll
    errors : list of SchedulerError
        Submissions that failed (the other classes were still submitted)
    output : str
        What the jugfile wrote while it was loaded
    '''
    by_class: dict
    hit_barrier: bool
    active: dict
    report: object
    stopped: dict = field(default_factory=dict)
    submitted: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    output: str = ''


class Submitter:
    '''Polls a project and submits its workers

    Parameters
    ----------
    cluster : ClusterSettings
    project : ProjectSettings
    scheduler : Scheduler
    project_state : ProjectState
    jug : str, optional
        The ``jug`` executable in the worker scripts
    counts_runner : Runner, optional
        Runs the subprocess that counts the tasks (default: a
        ``SubprocessRunner``)
    counts_timeout : float, optional
        Seconds to wait for the tasks to be counted (default: no limit)
    clock : callable, optional
        Returns the current time, timezone aware (for tests)

    Raises
    ------
    ConfigError
        If the settings are invalid for this scheduler or the jugdir cannot
        be shared (as for ``submit --all``)
    '''

    def __init__(self, cluster, project, scheduler, project_state, *, jug='jug',
                 counts_runner=None, counts_timeout=None, clock=_now):
        found = check_scheduler_settings(cluster, project)
        if found.errors:
            raise found.errors[0]
        check_jugdir(project)
        self.warnings = list(found.warnings)
        self.cluster = cluster
        self.project = project
        self.scheduler = scheduler
        self.project_state = project_state
        self.jug = jug
        self.counts_runner = counts_runner
        self.counts_timeout = counts_timeout
        self.clock = clock
        self.classifier = Classifier.from_project(project)
        self.limits = class_limits(project)
        #: JobIds of the jobs known to have ended
        self.settled = set()
        #: JobIds of the jobs submitted, or seen queued or running, by this
        #: submitter and not yet ended: the outcome of those that end is used
        self.watched = set()
        #: Class name -> TaskCounts, at the previous poll
        self.previous = None
        #: Class name -> the jobs that ended in error (see ``update_failing``)
        self.failing = {}

    def poll(self):
        '''Query the jobs, count the tasks, and submit the workers needed

        Nothing is submitted unless the run is still going on
        (``Progress.RUNNING``), and nothing for the stopped classes.

        Returns
        -------
        result : PollResult

        Raises
        ------
        JugfileError
            If the jugfile could not be loaded or its tasks counted
        SchedulerError
            If the scheduler could not be queried
        ConfigError
            If a task is matched by two classes, or the default class's tasks
            cannot be selected
        StateError
            If ``.jug_hpc/`` cannot be read or written, or a job was submitted
            but could not be recorded
        '''
        records = jobs_to_query(self.project_state.jobs(), self.scheduler.name, self.settled)
        statuses = self.scheduler.query([r.job_id for r in records]) if records else {}
        now = self.clock()
        settled = [r for r in records
                   if (s := statuses.get(r.job_id)) is not None and is_settled(r, s, now)]
        seen_ending = [r for r in settled if r.job_id in self.watched
                       and statuses[r.job_id].state is JobState.FINISHED]
        outcomes = (self.scheduler.outcome([r.job_id for r in seen_ending])
                    if seen_ending else {})
        counted = project_task_counts(self.project, runner=self.counts_runner,
                                      timeout=self.counts_timeout)
        by_class = counts_by_class(self.classifier, counted.counts)

        # nothing above changed the submitter's state, so a poll that failed
        # can be done again
        self.settled.update(r.job_id for r in settled)
        self.watched = {r.job_id for r in records} - self.settled
        self.failing = update_failing(
            self.failing, by_class, self.previous,
            [(r, outcomes[r.job_id]) for r in seen_ending if r.job_id in outcomes])
        self.previous = by_class

        active = active_workers(records, statuses, now)
        stopped = stopped_classes(self.failing)
        stalled = {n: jobs for n, jobs in stopped.items() if not active.get(n)}
        report = progress(by_class, counted.hit_barrier, stalled)
        result = PollResult(by_class, counted.hit_barrier, active, report, stopped,
                            output=counted.output)
        if report.progress is not Progress.RUNNING:
            return result
        for name, n in workers_to_submit(by_class, active, self.limits, stopped).items():
            prepared = prepare_class(self.cluster, self.project, self.project.get_class(name),
                                     self.scheduler, workers=n, tasks=by_class[name].total,
                                     task_names=counted.counts.keys(), jug=self.jug)
            written = write_script(prepared, self.scheduler, self.project_state)
            try:
                record = submit_worker(written, self.scheduler, self.project_state)
            except SchedulerError as e:
                result.errors.append(e)
            else:
                result.submitted.append(record)
                self.watched.add(record.job_id)
        return result


def run(submitter, *, interval=POLL_INTERVAL, max_interval=None, on_poll=None,
        on_failure=None, max_failed=MAX_FAILED_POLLS, sleep=time.sleep):
    '''Poll until every task is finished or no task can run (because tasks
    failed, or the workers of the classes with ready tasks keep failing)

    Parameters
    ----------
    submitter : Submitter
    interval : float, optional
        Seconds between polls while something changes
    max_interval : float, optional
        Longest wait between polls while nothing changes (default:
        :data:`MAX_INTERVAL_FACTOR` × ``interval``); the wait doubles after
        each poll that changes nothing. Equal to ``interval``, there is no
        backoff.
    on_poll : callable, optional
        Called with the :class:`PollResult` of every poll that counted the
        tasks (also those where a submission failed)
    on_failure : callable, optional
        Called with the exception of a failed poll that will be retried (the
        next poll is ``interval`` seconds later)
    max_failed : int, optional
        Polls in a row that may fail before giving up (the first poll may not)
    sleep : callable, optional
        Waits for a number of seconds (for tests)

    Returns
    -------
    result : PollResult
        The last poll's, whose ``report.progress`` is ``FINISHED``,
        ``BLOCKED`` or ``STALLED``

    Raises
    ------
    JugfileError, SchedulerError
        If the first poll fails, or ``max_failed`` polls in a row fail (a poll
        fails if it raises, or if a submission fails)
    ConfigError, StateError
        As soon as a poll raises one
    '''
    if max_interval is None:
        max_interval = MAX_INTERVAL_FACTOR * interval
    if max_interval < interval:
        raise ValueError(f'max_interval ({max_interval:g}) is less than interval '
                         f'({interval:g})')
    failed = 0
    first = True
    wait = interval
    seen = None
    while True:
        try:
            result = submitter.poll()
        except (JugfileError, SchedulerError) as e:
            error = e
        else:
            if on_poll is not None:
                on_poll(result)
            error = result.errors[0] if result.errors else None
            if error is None:
                if result.report.progress is not Progress.RUNNING:
                    return result
                failed = 0
                now_seen = _seen(result)
                wait = (min(2 * wait, max_interval)
                        if now_seen == seen and not result.submitted else interval)
                seen = now_seen
        if error is not None:
            failed += 1
            if first or failed > max_failed:
                raise error
            if on_failure is not None:
                on_failure(error)
            wait = interval
            seen = None
        first = False
        sleep(wait)


def _seen(result):
    '''What a poll found that, when it changes, resets the backoff'''
    return (result.by_class, result.hit_barrier, result.active, tuple(result.stopped))
