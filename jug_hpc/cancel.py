'''``jug-hpc cancel``: cancel every queued or running job of the project

:func:`cancel` asks the scheduler about every job recorded in
``.jug_hpc/jobs.json`` with the backend in use, and cancels those with workers
queued or running (:func:`jug_hpc.submitter.active_jobs`; so a job submitted
less than :data:`~jug_hpc.submitter.SUBMIT_GRACE` seconds ago that the
scheduler does not list yet is cancelled too). Jobs that have ended are left
alone, so cancelling twice is harmless.

The submitter goes first, as it would replace the cancelled workers whose
tasks are still ready:

- A submitter job (``submit --as-job``) is cancelled before the workers.
  Then :func:`cancel` waits, up to :data:`CANCEL_WAIT` seconds, until the
  scheduler no longer lists it as queued or running *and* nothing holds the
  submitter's lock (:meth:`ProjectState.submitter_lock_holder`), so that the
  job has stopped even if the scheduler is slow to say so. Only then are the
  jobs read again from ``jobs.json`` (the submitter may have added some) and
  the workers cancelled. If the submitter has not stopped by then, the
  workers are cancelled anyway, and the result says so
  (:attr:`CancelResult.submitter_left`).
- A submitter run outside the scheduler (``jug-hpc submit`` in a terminal:
  the lock is held, and no submitter job is queued or running) cannot be
  stopped through the scheduler, so nothing is cancelled
  (:class:`CancelError`): it has to be stopped first (with Ctrl-C, which
  leaves its workers running).

Jobs recorded with another backend cannot be asked about with this one; they
are counted in the result, not cancelled.

Cancelled jobs end as :attr:`~jug_hpc.schedulers.Outcome.CANCELLED`, which is
not reported as a kill by ``jug-hpc status``. Workers stopped this way get a
``SIGTERM`` first, on which ``jug execute`` releases the lock of the task it
was running.
'''
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .schedulers.base import JobState
from .submitter import active_jobs, jobs_to_query, submitter_jobs

__all__ = [
    'CANCEL_CHECK',
    'CANCEL_WAIT',
    'CancelError',
    'CancelResult',
    'cancel',
    'describe_status',
]

#: Seconds to wait for a cancelled submitter job to stop before cancelling the
#: workers anyway (PBS gives a job ``kill_delay``, 10 s by default, between
#: ``SIGTERM`` and ``SIGKILL``)
CANCEL_WAIT = 120

#: Seconds between checks while waiting for the submitter job to stop
CANCEL_CHECK = 2


class CancelError(Exception):
    '''Nothing was cancelled, for a reason the message gives the user'''


@dataclass(frozen=True)
class CancelResult:
    '''What :func:`cancel` did

    Attributes
    ----------
    cancelled : list of (JobRecord, JobStatus)
        The jobs cancelled, with their status just before: submitter jobs
        first, then workers, each in submission order
    others : dict
        Backend name -> number of jobs recorded with that backend (not the one
        in use), which were not asked about
    submitter_left : list of JobRecord
        Cancelled submitter jobs that had not stopped after waiting for them
        (when this is not empty, they may have submitted workers after the
        workers were cancelled); empty if they all stopped
    '''
    cancelled: list
    others: dict = field(default_factory=dict)
    submitter_left: list = field(default_factory=list)


def _now():
    return datetime.now(UTC)


def describe_status(record, status):
    '''How a job about to be cancelled stands, in a few words

    ``queued``, ``running``, ``2 workers queued, 1 running`` (an array) or
    ``not listed by the scheduler yet`` (just submitted).
    '''
    if status.state is JobState.UNKNOWN:
        return 'not listed by the scheduler yet'
    if record.workers == 1:
        return 'running' if status.running else 'queued'
    queued, running = status.queued, status.running
    def workers(n):
        return f'{n} worker{"s" if n != 1 else ""}'
    if queued and running:
        return f'{workers(queued)} queued, {running} running'
    return f'{workers(queued)} queued' if queued else f'{workers(running)} running'


def _active(scheduler, records, now):
    '''The records with workers queued or running, and every record's status'''
    statuses = scheduler.query([r.job_id for r in records]) if records else {}
    return active_jobs(records, statuses, now), statuses


def cancel(scheduler, project_state, *, wait=CANCEL_WAIT, check_every=CANCEL_CHECK,
           clock=_now, sleep=time.sleep):
    '''Cancel every queued or running job recorded for the project

    Parameters
    ----------
    scheduler : Scheduler
        The backend in use
    project_state : ProjectState
    wait : float, optional
        Seconds to wait for cancelled submitter jobs to stop
    check_every : float, optional
        Seconds between checks while waiting
    clock : callable, optional
        Returns the current time, timezone aware (for tests)
    sleep : callable, optional
        Waits for a number of seconds (for tests)

    Returns
    -------
    result : CancelResult

    Raises
    ------
    CancelError
        If a submitter runs outside the scheduler (nothing is cancelled)
    SchedulerError, StateError
        If the scheduler or the state directory cannot be read, or a job
        cannot be cancelled (jobs cancelled before stay cancelled)
    '''
    records = project_state.jobs()
    holder = project_state.submitter_lock_holder()
    submitters, statuses = _active(scheduler, submitter_jobs(records, scheduler.name),
                                   clock())
    if holder is not None and not submitters:
        where = f' (process {holder})' if holder else ''
        raise CancelError(
            f'a submitter is running for this project outside the scheduler{where}, '
            'and it would submit new workers for the tasks of the cancelled ones; '
            "stop it first (Ctrl-C), then run 'jug-hpc cancel' again; nothing cancelled")
    cancelled = []
    left = []
    if submitters:
        scheduler.cancel([r.job_id for r in submitters])
        cancelled += [(r, statuses[r.job_id]) for r in submitters]
        left = _wait_stopped(scheduler, project_state, submitters, wait=wait,
                             check_every=check_every, clock=clock, sleep=sleep)
        records = project_state.jobs()
    workers, statuses = _active(scheduler, jobs_to_query(records, scheduler.name), clock())
    if workers:
        scheduler.cancel([r.job_id for r in workers])
        cancelled += [(r, statuses[r.job_id]) for r in workers]
    others = Counter(r.scheduler for r in records if r.scheduler != scheduler.name)
    return CancelResult(cancelled, dict(others), left)


def _wait_stopped(scheduler, project_state, jobs, *, wait, check_every, clock, sleep):
    '''Wait until none of ``jobs`` is queued or running and the submitter's
    lock is free; returns the jobs not stopped by then (all of them if only the
    lock is still held)
    '''
    deadline = clock() + timedelta(seconds=wait)
    ids = [r.job_id for r in jobs]
    while True:
        statuses = scheduler.query(ids)
        left = [r for r in jobs
                if statuses[r.job_id].state in (JobState.QUEUED, JobState.RUNNING)]
        held = project_state.submitter_lock_holder() is not None
        if not left and not held:
            return []
        if clock() >= deadline:
            return left or list(jobs)
        sleep(check_every)
