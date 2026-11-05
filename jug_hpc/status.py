'''``jug-hpc status``: tasks and workers per class, and jobs killed by the scheduler

:func:`status` takes what the CLI read (the task counts, the recorded jobs, the
scheduler's answers about them, and who holds the submitter's lock) and
returns a :class:`StatusReport`: a table for stdout, with the tasks of each
class in each state and its queued and running workers, a line on the
submitter, and the problems found, which the CLI prints on stderr. It does no
I/O, so it is tested without the CLI or a scheduler.

Workers are counted as the submitter counts them
(:func:`jug_hpc.submitter.active_workers`): a job the scheduler does not know
yet, submitted less than :data:`~jug_hpc.submitter.SUBMIT_GRACE` seconds ago,
counts as queued.

Every job recorded in ``.jug_hpc/jobs.json`` with the backend in use is asked
about, so the report on killed jobs covers the project's whole history, as far
as the scheduler remembers it (without job history, PBS forgets jobs soon after
they end). Each class gets one warning per way its jobs were killed (memory,
wall time, signal), with the suggestion of :func:`jug_hpc.kills.suggestion`
for the class as it is set now.

Tasks that are locked (``running`` in the table) in a class with no queued or
running worker get a warning: their workers probably died while running them
(e.g., killed by the scheduler for memory) and left their locks behind, so no
worker will run them again, and the submitter will not finish (with the
``file_keepalive`` backend, Jug instead lists such a task as failed after 30
minutes). The scheduler is asked about the jobs before the tasks are
counted, so the locks were seen after the workers; a lock taken in between
(by a worker submitted meanwhile, or by a ``jug execute`` started outside
jug-hpc) is reported as well, which the warning says. Nothing is reported
while a class no longer in ``jug_hpc.toml`` has workers queued or running,
as their tasks are counted in the classes that match them now.

``jug-hpc status --release-stale`` removes those locks: the CLI reads
``jobs.json`` and asks the scheduler again, and releases (with
:func:`jug_hpc.jugstate.release_locks`) the locks listed by the count in the
classes of :attr:`StatusReport.stale` that still have no worker queued or
running (:func:`classes_to_release`, :func:`locks_to_release`). A worker that
locked a task before the count was recorded before it, so it is seen then.
'''
from dataclasses import dataclass, field

from .config.project import SUBMITTER
from .jugstate import STATES, TaskCounts
from .kills import kill_counts, kill_reason, scheduler_kills, suggestion
from .schedulers.base import JobState
from .submitter import (
    SUBMIT_GRACE,
    Progress,
    active_submitter_jobs,
    locked_without_workers,
    progress,
)

__all__ = [
    'StatusReport',
    'WorkerCounts',
    'classes_to_release',
    'lock_warning',
    'locks_to_release',
    'release_messages',
    'status',
    'worker_counts',
]


@dataclass(frozen=True)
class WorkerCounts:
    '''Workers of a class waiting to start and running'''
    queued: int = 0
    running: int = 0

    def __add__(self, other):
        if not isinstance(other, WorkerCounts):
            return NotImplemented
        return WorkerCounts(self.queued + other.queued, self.running + other.running)

    @property
    def active(self):
        '''Workers queued or running'''
        return self.queued + self.running


def worker_counts(records, statuses, now, *, grace=SUBMIT_GRACE):
    '''The queued and running workers of each class

    Parameters
    ----------
    records : sequence of JobRecord
    statuses : dict
        JobId -> JobStatus, as returned by :meth:`Scheduler.query`; a record
        without a status counts for no workers
    now : datetime
        Timezone aware
    grace : float, optional
        An unknown job submitted less than ``grace`` seconds before ``now``
        counts for all of its workers, as queued

    Returns
    -------
    counts : dict
        Class name -> :class:`WorkerCounts`, for every class in ``records``,
        in the order they first appear
    '''
    counts = {}
    for r in records:
        s = statuses.get(r.job_id)
        if s is None:
            c = WorkerCounts()
        elif s.state is JobState.UNKNOWN and (now - r.submitted).total_seconds() < grace:
            c = WorkerCounts(queued=r.workers)
        else:
            c = WorkerCounts(s.queued, s.running)
        counts[r.class_name] = counts.get(r.class_name, WorkerCounts()) + c
    return counts


@dataclass(frozen=True)
class StatusReport:
    '''The result of :func:`status`

    Attributes
    ----------
    text : str
        The table and the submitter's line (for stdout)
    warnings : list of str
        Jobs killed by the scheduler, one per class and way of being killed;
        then the classes with locked tasks but no workers
    notes : list of str
        Informational: the run is over, tasks are ready with nobody to run
        them, jobs of another backend were not asked about, ...
    stale : tuple of str
        The classes warned about for locked tasks without workers (see
        :func:`jug_hpc.submitter.locked_without_workers`)
    '''
    text: str
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    stale: tuple = ()


def status(project, by_class, hit_barrier, records, statuses, outcomes, now, *,
           scheduler_name, lock_holder=None, grace=SUBMIT_GRACE):
    '''Describe where a project is

    Parameters
    ----------
    project : ProjectSettings
    by_class : dict
        Class name -> TaskCounts, every class (as returned by
        :func:`jug_hpc.classes.counts_by_class`)
    hit_barrier : bool
        Loading the jugfile stopped at a ``barrier()``
    records : sequence of JobRecord
        Every recorded job (also those of other backends, which are only
        counted in a note)
    statuses : dict
        JobId -> JobStatus, for the jobs of backend ``scheduler_name``
    outcomes : dict
        JobId -> JobOutcome, for the finished jobs whose outcome is known
    now : datetime
        Timezone aware
    scheduler_name : str
        The backend in use
    lock_holder : str, optional
        Who holds the submitter's lock (``pid@host``; empty if it does not
        say), or None if nobody does
    grace : float, optional
        See :func:`worker_counts`

    Returns
    -------
    StatusReport
    '''
    ours = [r for r in records if r.scheduler == scheduler_name]
    workers_jobs = [r for r in ours if r.class_name != SUBMITTER]
    workers = worker_counts(workers_jobs, statuses, now, grace=grace)
    # classes no longer in jug_hpc.toml only while they have workers
    unknown = [n for n, w in workers.items() if n not in by_class and w.active]
    names = list(by_class) + unknown

    header = ['class', *STATES, 'queued', 'running']
    rows = []
    total_tasks = TaskCounts()
    total_workers = WorkerCounts()
    for name in names:
        tc = by_class.get(name, TaskCounts())
        wc = workers.get(name, WorkerCounts())
        total_tasks += tc
        total_workers += wc
        rows.append([_label(name), *(getattr(tc, s) for s in STATES), wc.queued, wc.running])
    rows.append(['total', *(getattr(total_tasks, s) for s in STATES),
                 total_workers.queued, total_workers.running])
    lines = [_group_header(header, rows), *_table(header, rows)]
    if hit_barrier:
        lines.append('(more tasks will appear after a barrier)')

    submitters = active_submitter_jobs(
        [r for r in ours if r.class_name == SUBMITTER], statuses, now, grace=grace)
    lines.append('')
    lines.append(_submitter_line(submitters, statuses, lock_holder))

    notes = []
    report = progress(by_class, hit_barrier)
    if report.progress is Progress.FINISHED:
        notes.append('every task is finished')
    elif report.progress is Progress.BLOCKED:
        failed = ', '.join(f'{_label(n)} {c}' for n, c in report.failed.items())
        notes.append(f'no task can run: failed tasks: {failed}')
    elif (total_tasks.ready and not total_workers.active and not submitters
          and lock_holder is None):
        notes.append("tasks are ready, but no worker is queued or running and no submitter "
                     "is running: run 'jug-hpc submit'")
    if unknown:
        listed = ', '.join(_label(n) for n in unknown)
        notes.append(f'not in {project.path.name} any more, but with workers queued or '
                     f'running: {listed} (its tasks are counted in the classes that match '
                     'them now)')
    others = {}
    for r in records:
        if r.scheduler != scheduler_name:
            others[r.scheduler] = others.get(r.scheduler, 0) + 1
    for other, n in others.items():
        notes.append(f'{n} job{"s" if n != 1 else ""} submitted with the {other!r} backend '
                     f'not asked about (the backend in use is {scheduler_name!r})')

    ended = [(r, outcomes[r.job_id]) for r in workers_jobs if r.job_id in outcomes]
    warnings = _kill_warnings(project, scheduler_kills(ended))
    stale = locked_without_workers(by_class, {n: w.active for n, w in workers.items()})
    warnings.extend(lock_warning(name, by_class[name].running) for name in stale)
    return StatusReport('\n'.join(lines), warnings, notes, stale)


def classes_to_release(stale, records, statuses, now, *, scheduler_name,
                       grace=SUBMIT_GRACE):
    '''The classes of ``stale`` that still have no worker queued or running

    Parameters
    ----------
    stale : sequence of str
        ``StatusReport.stale`` of a report made before
    records, statuses, now, scheduler_name, grace
        As for :func:`status`, read again after that report

    Returns
    -------
    names : tuple of str
        In the order of ``stale``
    '''
    workers = worker_counts(
        [r for r in records if r.scheduler == scheduler_name and r.class_name != SUBMITTER],
        statuses, now, grace=grace)
    return tuple(n for n in stale if not workers.get(n, WorkerCounts()).active)


def release_messages(stale, released):
    '''What ``--release-stale`` did, for each class of ``stale``

    Parameters
    ----------
    stale : sequence of str
        ``StatusReport.stale`` of the report before the locks were released
    released : dict
        Class name -> the hashes whose lock was removed, for the classes whose
        locks were released (others were left alone, as a worker of the class
        was queued or running when the scheduler was asked again)

    Returns
    -------
    lines : list of str
        For stdout: the released locks
    notes : list of str
        The classes whose locks were left alone
    '''
    lines = []
    notes = []
    for name in stale:
        if name in released:
            n = len(released[name])
            tasks = '1 task' if n == 1 else f'{n} tasks'
            lines.append(f'{_label(name)}: released the locks of {tasks}')
        else:
            notes.append(f'{_label(name)}: locks not released, as a worker of the class is '
                         'queued or running now')
    return lines, notes


def locks_to_release(classifier, locked, classes):
    '''The locked tasks of ``classes``, by class

    Parameters
    ----------
    classifier : Classifier
    locked : dict
        Task name -> hashes of its locked tasks (``SubprocessCounts.locked``)
    classes : iterable of str
        The classes whose locks to release

    Returns
    -------
    hashes : dict
        Class name -> tuple of hashes, for the classes of ``classes`` that
        have locked tasks, in the order of ``classes``
    '''
    by_class = {name: [] for name in classes}
    for task_name, hashes in locked.items():
        c = classifier.class_of(task_name)
        if c in by_class:
            by_class[c].extend(hashes)
    return {name: tuple(hashes) for name, hashes in by_class.items() if hashes}


def _kill_warnings(project, kills):
    '''One warning per class and way of being killed, with the last such job'''
    last = {}
    for record, outcome in kills:
        last[record.class_name, outcome.outcome] = (record, outcome)
    warnings = []
    for name, by_outcome in kill_counts(kills).items():
        try:
            rc = project.get_class(name)
        except KeyError:    # the class is no longer in jug_hpc.toml
            rc = None
        for kind, n in by_outcome.items():
            record, outcome = last[name, kind]
            jobs = 'job' if n == 1 else 'jobs'
            what = outcome.detail or outcome.outcome.value
            message = (f'{_label(name)}: {n} {jobs} killed {kill_reason(kind)} (the last: job '
                       f'{record.job_id}: {what})')
            advice = suggestion(outcome, rc) if rc is not None else ''
            warnings.append(f'{message}; {advice}' if advice else message)
    return warnings


def lock_warning(name, n, *, submitter=False):
    '''The warning about the locked tasks of a class without workers

    Parameters
    ----------
    name : str
        The class
    n : int
        Its locked tasks
    submitter : bool, optional
        For the submitter's log (which says what it then does), rather than
        ``jug-hpc status``

    Returns
    -------
    message : str
    '''
    if n == 1:
        what, it, workers_that, its_locks = '1 task is', 'it', 'worker that', 'its lock'
    else:
        what, it, workers_that, its_locks = (f'{n} tasks are', 'them', 'workers that',
                                             'their locks')
    locks = 'lock' if n == 1 else 'locks'
    if submitter:
        end = (f"this submitter will not finish (remove the {locks} with 'jug-hpc status "
               f"--release-stale': workers are then submitted for {it})")
    else:
        end = (f"the submitter will not finish (remove the {locks} with 'jug-hpc status "
               "--release-stale')")
    return (f'{_label(name)}: {what} locked, but no worker of the class is queued or '
            f"running: unless a 'jug execute' started outside jug-hpc is running {it}, "
            f'the {workers_that} locked {it} died (e.g., killed by the scheduler) and '
            f'left {its_locks} behind, so no worker will run {it} and {end}')


def _submitter_line(active_jobs, statuses, lock_holder):
    parts = []
    if lock_holder is not None:
        parts.append(f'running (process {lock_holder})' if lock_holder else 'running')
    for r in active_jobs:
        s = statuses[r.job_id]
        where = 'running' if s.state is JobState.RUNNING else 'queued'
        parts.append(f'job {r.job_id}, {where}')
    return f'Submitter: {"; ".join(parts) if parts else "not running"}'


def _table(header, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=True)]

    def line(cells):
        first = f'{cells[0]:<{widths[0]}}'
        rest = '  '.join(f'{c!s:>{w}}' for c, w in zip(cells[1:], widths[1:], strict=True))
        return f'{first}  {rest}'.rstrip()

    return [line(header), *(line(r) for r in rows)]


def _group_header(header, rows):
    '''``tasks`` over the task columns, ``workers`` over the worker columns'''
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=True)]
    tasks = sum(widths[1:-2]) + 2 * (len(widths) - 4)
    workers = sum(widths[-2:]) + 2
    return (f'{"":<{widths[0]}}  {"tasks":^{tasks}}  {"workers":^{workers}}').rstrip()


def _label(name):
    return '[default]' if name == 'default' else f'[class.{name}]'
