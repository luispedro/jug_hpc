'''The polling submitter's loop: ``jug-hpc submit``

:class:`Submitter` does one poll at a time (:meth:`Submitter.poll`): it counts
the tasks of each class in a subprocess, asks the scheduler about the jobs not
yet known to have ended, and submits the workers that the rule of
:mod:`jug_hpc.submitter` asks for. :func:`run` polls until the run is over.

The decisions are the pure functions of :mod:`jug_hpc.submitter`; this module
does the I/O around them. The only state kept between polls is the set of jobs
known to have ended (so they are not queried again); everything else is read
again at each poll, from the jugfile's store and ``.jug_hpc/jobs.json``, so a
submitter can be stopped and started again at any time.

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
from .schedulers.base import SchedulerError
from .schedulers.checks import check_scheduler_settings
from .submit import check_jugdir, prepare_class, submit_worker, write_script
from .submitter import (
    Progress,
    active_workers,
    class_limits,
    is_settled,
    jobs_to_query,
    progress,
    workers_to_submit,
)

__all__ = [
    'MAX_FAILED_POLLS',
    'POLL_INTERVAL',
    'PollResult',
    'Submitter',
    'run',
]

#: Default seconds between polls
POLL_INTERVAL = 60

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

    def poll(self):
        '''Count the tasks, query the jobs, and submit the workers needed

        Nothing is submitted unless the run is still going on
        (``Progress.RUNNING``).

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
        counted = project_task_counts(self.project, runner=self.counts_runner,
                                      timeout=self.counts_timeout)
        by_class = counts_by_class(self.classifier, counted.counts)
        records = jobs_to_query(self.project_state.jobs(), self.scheduler.name, self.settled)
        statuses = self.scheduler.query([r.job_id for r in records]) if records else {}
        now = self.clock()
        for r in records:
            status = statuses.get(r.job_id)
            if status is not None and is_settled(r, status, now):
                self.settled.add(r.job_id)
        active = active_workers(records, statuses, now)
        report = progress(by_class, counted.hit_barrier)
        result = PollResult(by_class, counted.hit_barrier, active, report,
                            output=counted.output)
        if report.progress is not Progress.RUNNING:
            return result
        for name, n in workers_to_submit(by_class, active, self.limits).items():
            prepared = prepare_class(self.cluster, self.project, self.project.get_class(name),
                                     self.scheduler, workers=n, tasks=by_class[name].total,
                                     task_names=counted.counts.keys(), jug=self.jug)
            written = write_script(prepared, self.scheduler, self.project_state)
            try:
                result.submitted.append(
                    submit_worker(written, self.scheduler, self.project_state))
            except SchedulerError as e:
                result.errors.append(e)
        return result


def run(submitter, *, interval=POLL_INTERVAL, on_poll=None, on_failure=None,
        max_failed=MAX_FAILED_POLLS, sleep=time.sleep):
    '''Poll until every task is finished or no task can run

    Parameters
    ----------
    submitter : Submitter
    interval : float, optional
        Seconds between polls
    on_poll : callable, optional
        Called with the :class:`PollResult` of every poll that counted the
        tasks (also those where a submission failed)
    on_failure : callable, optional
        Called with the exception of a failed poll that will be retried
    max_failed : int, optional
        Polls in a row that may fail before giving up (the first poll may not)
    sleep : callable, optional
        Waits for a number of seconds (for tests)

    Returns
    -------
    result : PollResult
        The last poll's, whose ``report.progress`` is ``FINISHED`` or
        ``BLOCKED``

    Raises
    ------
    JugfileError, SchedulerError
        If the first poll fails, or ``max_failed`` polls in a row fail (a poll
        fails if it raises, or if a submission fails)
    ConfigError, StateError
        As soon as a poll raises one
    '''
    failed = 0
    first = True
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
        if error is not None:
            failed += 1
            if first or failed > max_failed:
                raise error
            if on_failure is not None:
                on_failure(error)
        first = False
        sleep(interval)
