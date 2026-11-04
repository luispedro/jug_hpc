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
- :func:`progress` says whether to keep polling (:class:`Progress`).

A job the scheduler does not know (``JobState.UNKNOWN``) has normally ended
long ago, and its record was purged. But some schedulers take a moment to list
a job they have just accepted, so a job submitted less than
:data:`SUBMIT_GRACE` seconds ago and reported unknown is counted as if all its
workers were queued; otherwise the next poll would submit its workers again.
'''
import enum
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from .schedulers.base import JobState

__all__ = [
    'SUBMIT_GRACE',
    'Progress',
    'ProgressReport',
    'active_workers',
    'class_limits',
    'is_settled',
    'jobs_to_query',
    'progress',
    'workers_to_submit',
]

#: How long (seconds) after submission an unknown job is still counted as queued
SUBMIT_GRACE = 300


class Progress(enum.Enum):
    '''Whether the submitter keeps polling'''
    RUNNING = 'running'    # tasks are ready or running: keep polling
    FINISHED = 'finished'  # every task is finished
    BLOCKED = 'blocked'    # nothing is ready or running, and some tasks failed


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
    '''
    progress: Progress
    failed: Mapping
    waiting: Mapping


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
        In submission order
    '''
    settled = set(settled)
    return [r for r in records
            if r.scheduler == scheduler_name and r.job_id not in settled]


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


def workers_to_submit(by_class, active, limits):
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

    Returns
    -------
    to_submit : dict
        Class name -> number of workers (positive), in the order of
        ``by_class``; classes that need no new workers are left out
    '''
    to_submit = {}
    for name, counts in by_class.items():
        wanted = min(counts.ready, limits[name])
        n = wanted - active.get(name, 0)
        if n > 0:
            to_submit[name] = n
    return to_submit


def progress(by_class, hit_barrier=False):
    '''Whether the submitter keeps polling

    Parameters
    ----------
    by_class : dict
        Class name -> TaskCounts
    hit_barrier : bool, optional
        True if loading the jugfile stopped at a ``barrier()``: tasks after it
        are not known yet, so the run is not finished even if every known task
        is

    Returns
    -------
    report : ProgressReport
        ``FINISHED`` when every task is finished; ``BLOCKED`` when no task is
        ready or running and some have failed (the waiting tasks depend on
        them, so no worker could run them); ``RUNNING`` otherwise. Queued and
        running workers are not considered: a worker with nothing left to do
        ends by itself.
    '''
    failed = {n: c.failed for n, c in by_class.items() if c.failed}
    waiting = {n: c.waiting for n, c in by_class.items() if c.waiting}
    ready = sum(c.ready for c in by_class.values())
    running = sum(c.running for c in by_class.values())
    if ready or running:
        state = Progress.RUNNING
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
