'''Jobs killed by the scheduler, and what to change in the settings

A worker killed for using more memory or more wall time than its class asks
for is a problem with the settings rather than with the jugfile, and the fix
is in ``jug_hpc.toml``: raise the class's ``memory``, raise its ``time``, or
lower its ``time_fraction`` (a worker takes no new task after ``time_fraction
× time``, so a task that starts just before then must end in the time left).

On clusters that enforce memory limits with cgroups, a job that runs out of
memory is killed by the kernel with ``SIGKILL`` and the scheduler records
nothing else (see :mod:`jug_hpc.schedulers.pbs`), so a ``SIGKILL`` gets the
memory suggestion too, as a guess. Jobs deleted by the user
(``Outcome.CANCELLED``) are not kills.

An array job is summarized by its worst worker
(:func:`jug_hpc.schedulers.base.combine_outcomes`), so an array with workers
killed for memory and others for wall time counts as killed for memory (its
detail lists both).

These are pure functions; the submitter's log (:func:`jug_hpc.cli._poll`)
prints them.
'''
import signal

from .schedulers.base import OUTCOME_SEVERITY, Outcome
from .units import format_dhms, format_memory

__all__ = [
    'KILLED',
    'kill_counts',
    'kill_summary',
    'scheduler_kills',
    'suggestion',
]

#: The outcomes of jobs killed by the scheduler (or by the kernel, for it)
KILLED = (Outcome.KILLED_MEMORY, Outcome.KILLED_TIME, Outcome.KILLED_SIGNAL)

_WHY = {
    Outcome.KILLED_MEMORY: 'for memory',
    Outcome.KILLED_TIME: 'for wall time',
    Outcome.KILLED_SIGNAL: 'by a signal',
}


def scheduler_kills(ended):
    '''The jobs among ``ended`` that were killed by the scheduler

    Parameters
    ----------
    ended : iterable of (JobRecord, JobOutcome)

    Returns
    -------
    kills : list of (JobRecord, JobOutcome)
        In the order of ``ended``
    '''
    return [(record, outcome) for record, outcome in ended if outcome.outcome in KILLED]


def kill_counts(kills):
    '''How many jobs of each class were killed, and why

    Parameters
    ----------
    kills : iterable of (JobRecord, JobOutcome)
        As returned by :func:`scheduler_kills`

    Returns
    -------
    counts : dict
        Class name -> dict Outcome -> number of jobs; classes in the order
        of their first kill, outcomes from the worst
    '''
    counts = {}
    for record, outcome in kills:
        by_outcome = counts.setdefault(record.class_name, {})
        by_outcome[outcome.outcome] = by_outcome.get(outcome.outcome, 0) + 1
    return {name: dict(sorted(by_outcome.items(), key=lambda i: OUTCOME_SEVERITY.index(i[0])))
            for name, by_outcome in counts.items()}


def kill_summary(by_outcome):
    '''``2 for memory, 1 for wall time``, for one class of :func:`kill_counts`'''
    return ', '.join(f'{n} {_WHY[outcome]}' for outcome, n in by_outcome.items())


def suggestion(outcome, rc):
    '''What to change in a class whose job ended with ``outcome``

    Parameters
    ----------
    outcome : JobOutcome
    rc : ResourceClass
        The class, as it is set now

    Returns
    -------
    text : str
        E.g. ``raise memory (now 4G)``; empty if there is nothing to suggest
        (the job was not killed for memory or wall time)
    '''
    memory = f'raise memory (now {format_memory(rc.memory)})'
    if outcome.outcome is Outcome.KILLED_MEMORY:
        return memory
    if outcome.outcome is Outcome.KILLED_TIME:
        time = f'raise time (now {format_dhms(rc.time)})'
        left = rc.time - rc.max_seconds
        if left <= 0:
            fraction = (f'lower time_fraction (now {rc.time_fraction:g}: workers take new '
                        'tasks until the wall time ends)')
        else:
            fraction = (f'lower time_fraction (now {rc.time_fraction:g}: a task started '
                        f'after {format_dhms(rc.max_seconds)} has {format_dhms(left)} left)')
        return f'{time}, or {fraction}'
    if outcome.outcome is Outcome.KILLED_SIGNAL and outcome.signal == signal.SIGKILL:
        return f'if it used more memory than requested, {memory}'
    return ''
