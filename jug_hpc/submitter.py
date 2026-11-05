'''The polling submitter: what to submit at each poll, and when to stop

At each poll, the submitter loads the jugfile in a subprocess to count the
tasks of each class (:func:`jug_hpc.jugstate.project_task_counts` and
:func:`jug_hpc.classes.counts_by_class`), asks the scheduler where the jobs it
submitted are (:meth:`Scheduler.query`), and then, per class::

    wanted = min(ready tasks, workers)
    active = queued + running workers of the class's jobs
    submit wanted - active new workers, if positive

Tasks being run hold a lock, so Jug counts them as running, not ready: a
class whose four workers each run a task, with ten more tasks ready and
``workers = 4``, gets no new worker.

This module holds the decisions only (pure functions, no I/O), so that they
can be tested without a scheduler or a jugfile:

- :func:`jobs_to_query` picks the recorded jobs worth asking about;
- :func:`is_settled` says when a job never needs to be asked about again;
- :func:`active_workers` counts the queued and running workers per class;
- :func:`workers_to_submit` applies the rule above;
- :func:`update_failing` keeps track of the jobs of each class that ended in
  error, and :func:`stopped_classes` says which classes get no more workers
  because of them;
- :func:`progress` says whether to keep polling (:class:`Progress`);
- :func:`active_submitter_jobs` finds a submitter job (``jug-hpc submit
  --as-job``) still queued or running, so that a second one is not
  submitted (:func:`active_jobs` does the same for any jobs, e.g. those
  ``jug-hpc cancel`` cancels).

The submitter job is recorded in ``.jug_hpc/jobs.json`` like a worker, with
the class name :data:`~jug_hpc.config.project.SUBMITTER`; the polling
submitter does not count it (and does not query it).

A job the scheduler does not know (``JobState.UNKNOWN``) has normally ended
long ago, and its record was purged. But some schedulers take a moment to list
a job they have just accepted, so a job submitted less than
:data:`SUBMIT_GRACE` seconds ago and reported unknown is counted as if all its
workers were queued; otherwise the next poll would submit its workers again.

Workers that die before Jug starts (e.g. because a ``setup`` command fails)
leave their tasks ready, so the rule above would replace them at every poll,
forever. So the submitter looks at how its jobs ended (:meth:`Scheduler.outcome`)
and, for each class, keeps the jobs that ended in error (anything but
``Outcome.SUCCESS``) since a task of the class last ended (finished or failed).
After :data:`MAX_FAILED_JOBS` such jobs in a row, the class is *stopped*: it
gets no new workers until one of its tasks ends (e.g. run by a worker that was
already queued) or the submitter is started again. A job that succeeds, or a
task of the class that ends, clears the class's list. The rule is lenient on
purpose: the failures seen at a poll where a task of the class ended are not
counted, as that job may have run tasks before it was killed. When nothing
else can run, the run ends with ``Progress.STALLED``.
'''
import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from .config.project import SUBMITTER
from .schedulers.base import JobState, Outcome

__all__ = [
    'MAX_FAILED_JOBS',
    'SUBMIT_GRACE',
    'Progress',
    'ProgressReport',
    'active_jobs',
    'active_submitter_jobs',
    'active_workers',
    'class_limits',
    'is_settled',
    'jobs_to_query',
    'progress',
    'stopped_classes',
    'submitter_jobs',
    'update_failing',
    'workers_to_submit',
]

#: How long (seconds) after submission an unknown job is still counted as queued
SUBMIT_GRACE = 300

#: Jobs of a class in a row that may end in error, with no task of the class
#: ending, before the class gets no more workers
MAX_FAILED_JOBS = 3


class Progress(enum.Enum):
    '''Whether the submitter keeps polling'''
    RUNNING = 'running'    # tasks are ready or running: keep polling
    FINISHED = 'finished'  # every task is finished
    BLOCKED = 'blocked'    # nothing is ready or running, and some tasks failed
    STALLED = 'stalled'    # only tasks of stopped classes are ready (their jobs fail)


@dataclass(frozen=True)
class ProgressReport:
    '''The result of :func:`progress`

    Attributes
    ----------
    progress : Progress
    failed : dict
        Class name -> number of failed tasks, for the classes that have any
    waiting : dict
        Class name -> number of waiting tasks, for the classes that have any
        (when blocked: the tasks that depend on failed tasks)
    stalled : dict
        Class name -> the jobs that ended in error, as ``(JobRecord,
        JobOutcome)`` pairs, for the stopped classes that have ready tasks and
        no queued or running workers (when stalled: why nothing can run)
    '''
    progress: Progress
    failed: Mapping
    waiting: Mapping
    stalled: Mapping = field(default_factory=dict)


def _recent(record, now, grace):
    return now - record.submitted < timedelta(seconds=grace)


def is_settled(record, status, now, *, grace=SUBMIT_GRACE):
    '''Whether a job has ended for good, so it need not be queried again

    Parameters
    ----------
    record : JobRecord
    status : JobStatus
        The job's status at the last query
    now : datetime
        Timezone aware
    grace : float, optional
        Seconds after submission during which an unknown job may still be
        on its way into the queue

    Returns
    -------
    settled : bool
        True if the job is finished, or unknown and submitted more than
        ``grace`` seconds before ``now``
    '''
    if status.state is JobState.FINISHED:
        return True
    if status.state is JobState.UNKNOWN:
        return not _recent(record, now, grace)
    return False


def jobs_to_query(records, scheduler_name, settled=()):
    '''The recorded jobs to ask the scheduler about

    Parameters
    ----------
    records : sequence of JobRecord
        As returned by :meth:`ProjectState.jobs`
    scheduler_name : str
        Name of the backend in use: jobs submitted with another backend (e.g.
        the local one, while trying a project out) cannot be queried with it
    settled : collection of JobId, optional
        Jobs already known to have ended (see :func:`is_settled`)

    Returns
    -------
    records : list of JobRecord
        In submission order; without submitter jobs (``--as-job``), which
        run no task
    '''
    settled = set(settled)
    return [r for r in records
            if r.scheduler == scheduler_name and r.job_id not in settled
            and r.class_name != SUBMITTER]


def submitter_jobs(records, scheduler_name):
    '''The recorded submitter jobs (``--as-job``) of backend ``scheduler_name``'''
    return [r for r in records
            if r.scheduler == scheduler_name and r.class_name == SUBMITTER]


def active_submitter_jobs(records, statuses, now, *, grace=SUBMIT_GRACE):
    '''The submitter jobs that are queued or running

    Parameters
    ----------
    records : sequence of JobRecord
        The submitter jobs (see :func:`submitter_jobs`)
    statuses, now, grace
        As for :func:`active_jobs`

    Returns
    -------
    records : list of JobRecord
    '''
    return active_jobs(records, statuses, now, grace=grace)


def active_jobs(records, statuses, now, *, grace=SUBMIT_GRACE):
    '''The jobs that have workers queued or running

    Parameters
    ----------
    records : sequence of JobRecord
    statuses : dict
        JobId -> JobStatus, as returned by :meth:`Scheduler.query`; a record
        without a status is not active
    now : datetime
        Timezone aware
    grace : float, optional
        See :func:`is_settled`: an unknown job submitted less than ``grace``
        seconds before ``now`` is active

    Returns
    -------
    records : list of JobRecord
    '''
    return [r for r in records
            if active_workers([r], statuses, now, grace=grace).get(r.class_name, 0) > 0]


def active_workers(records, statuses, now, *, grace=SUBMIT_GRACE):
    '''The number of queued or running workers of each class

    Parameters
    ----------
    records : sequence of JobRecord
        The queried jobs (those of :func:`jobs_to_query`)
    statuses : dict
        JobId -> JobStatus, as returned by :meth:`Scheduler.query`. A record
        without a status counts for no workers.
    now : datetime
        Timezone aware
    grace : float, optional
        See :func:`is_settled`: an unknown job submitted less than ``grace``
        seconds before ``now`` counts for all of its workers

    Returns
    -------
    active : dict
        Class name -> number of workers, for every class in ``records`` (also
        classes no longer in the configuration)
    '''
    active = {}
    for r in records:
        status = statuses.get(r.job_id)
        if status is None:
            n = 0
        elif status.state is JobState.UNKNOWN and _recent(r, now, grace):
            n = r.workers
        else:
            n = status.active
        active[r.class_name] = active.get(r.class_name, 0) + n
    return active


def class_limits(project):
    '''The ``workers`` setting of every class of ``project`` (ProjectSettings)

    Returns
    -------
    limits : dict
        Class name -> maximum number of workers, the default class last
    '''
    return {rc.name: rc.workers for rc in project.all_classes()}


def _ended(counts):
    return counts.finished + counts.failed


def update_failing(failing, by_class, previous, ended_jobs):
    '''The jobs of each class that ended in error since a task of the class ended

    Parameters
    ----------
    failing : dict
        Class name -> tuple of ``(JobRecord, JobOutcome)``, as returned at the
        previous poll (empty at the first)
    by_class : dict
        Class name -> TaskCounts, at this poll
    previous : dict or None
        Class name -> TaskCounts, at the previous poll (None at the first)
    ended_jobs : sequence of (JobRecord, JobOutcome)
        The jobs seen to end since the previous poll, in submission order
        (jobs that had already ended when first queried are left out: when they
        ended is not known)

    Returns
    -------
    failing : dict
        Class name -> tuple of ``(JobRecord, JobOutcome)``, oldest first, for
        the classes that have any. A class's jobs are dropped when a task of
        the class ended since the previous poll (its finished and failed
        tasks went up; this poll's jobs are then not counted either) or when
        one of its jobs succeeded.
    '''
    previous = previous or {}
    progressed = {name for name, counts in by_class.items()
                  if name in previous and _ended(counts) > _ended(previous[name])}
    updated = {name: list(jobs) for name, jobs in failing.items() if name not in progressed}
    for record, outcome in ended_jobs:
        name = record.class_name
        if name in progressed:
            continue
        if outcome.outcome is Outcome.SUCCESS:
            updated.pop(name, None)
        else:
            updated.setdefault(name, []).append((record, outcome))
    return {name: tuple(jobs) for name, jobs in updated.items() if jobs}


def stopped_classes(failing, limit=MAX_FAILED_JOBS):
    '''The classes that get no new workers: dict class name -> failed jobs

    Parameters
    ----------
    failing : dict
        As returned by :func:`update_failing`
    limit : int, optional
        Jobs that may end in error in a row

    Returns
    -------
    stopped : dict
        Class name -> tuple of ``(JobRecord, JobOutcome)``, for the classes
        with at least ``limit`` failed jobs
    '''
    return {name: jobs for name, jobs in failing.items() if len(jobs) >= limit}


def workers_to_submit(by_class, active, limits, stopped=()):
    '''How many new workers to submit for each class

    Parameters
    ----------
    by_class : dict
        Class name -> TaskCounts (as returned by
        :func:`jug_hpc.classes.counts_by_class`)
    active : dict
        Class name -> queued and running workers (as returned by
        :func:`active_workers`); missing classes have none
    limits : dict
        Class name -> the class's ``workers`` setting, for every class in
        ``by_class`` (see :func:`class_limits`)
    stopped : collection of str, optional
        Classes that get no new workers (see :func:`stopped_classes`)

    Returns
    -------
    to_submit : dict
        Class name -> number of workers (positive), in the order of
        ``by_class``; classes that need no new workers are left out
    '''
    to_submit = {}
    for name, counts in by_class.items():
        if name in stopped:
            continue
        wanted = min(counts.ready, limits[name])
        n = wanted - active.get(name, 0)
        if n > 0:
            to_submit[name] = n
    return to_submit


def progress(by_class, hit_barrier=False, stalled=None):
    '''Whether the submitter keeps polling

    Parameters
    ----------
    by_class : dict
        Class name -> TaskCounts
    hit_barrier : bool, optional
        True if loading the jugfile stopped at a ``barrier()``: tasks after it
        are not known yet, so the run is not finished even if every known task
        is
    stalled : dict, optional
        Class name -> failed jobs, for the stopped classes (see
        :func:`stopped_classes`) without queued or running workers: their
        ready tasks will not be run

    Returns
    -------
    report : ProgressReport
        ``FINISHED`` when every task is finished; ``STALLED`` when no task is
        running and only the tasks of ``stalled`` classes are ready;
        ``BLOCKED`` when no task is ready or running and some have failed (the
        waiting tasks depend on them, so no worker could run them); ``RUNNING``
        otherwise. Queued and running workers are not considered (except
        through ``stalled``): a worker with nothing left to do ends by itself
        (and :mod:`jug_hpc.poll` cancels those left when the run ends
        ``BLOCKED`` or ``STALLED``).
    '''
    stalled = {n: jobs for n, jobs in (stalled or {}).items()
               if n in by_class and by_class[n].ready}
    failed = {n: c.failed for n, c in by_class.items() if c.failed}
    waiting = {n: c.waiting for n, c in by_class.items() if c.waiting}
    ready = sum(c.ready for n, c in by_class.items() if n not in stalled)
    running = sum(c.running for c in by_class.values())
    if ready or running:
        state = Progress.RUNNING
    elif stalled:
        return ProgressReport(Progress.STALLED, failed, waiting, stalled)
    elif failed:
        state = Progress.BLOCKED
    elif waiting or hit_barrier:
        # Every waiting task has an unfinished dependency, and following those
        # ends at a ready, running or failed task, so this does not happen
        # unless the store changed between counts (a worker finished a task
        # while it was being counted): count again at the next poll
        state = Progress.RUNNING
    else:
        state = Progress.FINISHED
    return ProgressReport(state, failed, waiting)
