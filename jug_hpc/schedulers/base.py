'''The scheduler interface and the registry of scheduler backends

A backend is a subclass of :class:`Scheduler` registered under the
``jug_hpc.schedulers`` entry point group, with its :attr:`Scheduler.name` as
the entry point name, e.g. in ``pyproject.toml``::

    [project.entry-points."jug_hpc.schedulers"]
    pbs = "jug_hpc.schedulers.pbs:PBSScheduler"

so a new scheduler can be added by a separate package. The name is what
``scheduler = "..."`` in ``cluster.toml`` selects, and the name of the tables
holding raw options for that scheduler (``[pbs]``, ``[class.x.pbs]``).

Backends keep pure functions (rendering a script, building a command line,
parsing output) apart from side effects, which go through
:attr:`Scheduler.runner` only (see :mod:`jug_hpc.runner`).
'''
import difflib
import enum
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import ClassVar, NewType

from ..config.errors import ConfigError
from ..runner import CommandError, SubprocessRunner

__all__ = [
    'ENTRY_POINT_GROUP',
    'OUTCOME_SEVERITY',
    'JobId',
    'JobOutcome',
    'JobState',
    'JobStatus',
    'Outcome',
    'Scheduler',
    'SchedulerError',
    'UnknownSchedulerError',
    'combine_outcomes',
    'create_scheduler',
    'get_scheduler',
    'scheduler_names',
    'signal_name',
]

ENTRY_POINT_GROUP = 'jug_hpc.schedulers'

#: A job identifier, as printed by the scheduler (e.g. ``'1234.pbs01'`` or,
#: for a PBS array, ``'1234[].pbs01'``)
JobId = NewType('JobId', str)


class JobState(enum.Enum):
    '''Where a job is in the scheduler's queue'''
    QUEUED = 'queued'        # waiting to start (including held)
    RUNNING = 'running'
    FINISHED = 'finished'    # ended, whatever the outcome
    UNKNOWN = 'unknown'      # the scheduler no longer knows the job


@dataclass(frozen=True)
class JobStatus:
    '''Where a job and its workers are in the scheduler's queue

    A job runs one worker, or several as an array whose elements start and end
    independently. The submitter counts *active* workers (queued or running),
    so a long array of which only a few elements are left counts for those few.

    Attributes
    ----------
    state : JobState
        The job as a whole: queued until its first worker starts, running until
        its last worker ends, then finished
    queued : int
        Workers waiting to start
    running : int
        Workers running (including those being stopped)
    '''
    state: JobState
    queued: int = 0
    running: int = 0

    def __post_init__(self):
        if self.queued < 0 or self.running < 0:
            raise ValueError(f'negative worker counts in {self!r}')

    @property
    def active(self):
        '''Workers queued or running'''
        return self.queued + self.running


class Outcome(enum.Enum):
    '''How a finished job ended'''
    SUCCESS = 'success'
    FAILED = 'failed'                  # non-zero exit status
    KILLED_MEMORY = 'killed-memory'    # killed by the scheduler: memory limit
    KILLED_TIME = 'killed-time'        # killed by the scheduler: wall time limit
    KILLED_SIGNAL = 'killed-signal'    # killed by another signal
    CANCELLED = 'cancelled'            # deleted by the user (qdel, jug-hpc cancel)
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class JobOutcome:
    '''How a finished job ended

    Attributes
    ----------
    outcome : Outcome
    exit_code : int or None
        Exit status as reported by the scheduler, if any
    signal : int or None
        Signal that killed the job, if known
    detail : str
        Free text for the user (e.g. the scheduler's own explanation)
    '''
    outcome: Outcome
    exit_code: int | None = None
    signal: int | None = None
    detail: str = ''


def signal_name(signum):
    '''The name of signal ``signum`` (``'SIGKILL'``), or ``'signal <n>'``'''
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f'signal {signum}'


#: How bad each outcome is, for summarizing an array (worst first)
OUTCOME_SEVERITY = (Outcome.KILLED_MEMORY, Outcome.KILLED_TIME, Outcome.KILLED_SIGNAL,
                    Outcome.FAILED, Outcome.CANCELLED, Outcome.UNKNOWN, Outcome.SUCCESS)


def combine_outcomes(outcomes):
    '''The outcome of a job from those of its workers: the worst one

    Parameters
    ----------
    outcomes : dict
        Array index (int) -> JobOutcome

    Returns
    -------
    outcome : JobOutcome
        For one worker, its own outcome. For several, the worst one (by
        :data:`OUTCOME_SEVERITY`), with a detail that lists the workers that
        did not succeed, those with the same detail together (``workers 1-10:
        exit status 127``).
    '''
    if not outcomes:
        return JobOutcome(Outcome.UNKNOWN, detail='no workers recorded')
    if len(outcomes) == 1:
        return next(iter(outcomes.values()))
    order = sorted(outcomes, key=lambda i: (OUTCOME_SEVERITY.index(outcomes[i].outcome), i))
    worst = outcomes[order[0]]
    if worst.outcome is Outcome.SUCCESS:
        return JobOutcome(Outcome.SUCCESS, 0)
    failed = [i for i in sorted(outcomes) if outcomes[i].outcome is not Outcome.SUCCESS]
    by_detail = {}
    for i in failed:
        by_detail.setdefault(outcomes[i].detail or outcomes[i].outcome.value, []).append(i)
    detail = '; '.join(f'{"workers" if len(ixs) > 1 else "worker"} {_index_list(ixs)}: {d}'
                       for d, ixs in by_detail.items())
    return JobOutcome(worst.outcome, worst.exit_code, worst.signal,
                      f'{len(failed)} of {len(outcomes)} workers did not succeed ({detail})')


def _index_list(indices):
    '''``1-4, 7, 9`` for sorted integers ``indices`` (runs of three or more as ranges)'''
    runs = []
    for i in indices:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return ', '.join(f'{r[0]}-{r[-1]}' if len(r) >= 3 else ', '.join(map(str, r))
                     for r in runs)


class SchedulerError(Exception):
    '''A scheduler command failed or printed something unexpected, or a backend
    is broken'''


class UnknownSchedulerError(ConfigError):
    '''``scheduler`` names no installed backend

    Attributes
    ----------
    name : str
    known : tuple of str
        Installed backends
    '''
    def __init__(self, name, known, where=None):
        self.name = name
        self.known = tuple(known)
        prefix = f'{where}: ' if where else ''
        if not self.known:
            hint = 'no scheduler backends are installed'
        else:
            close = difflib.get_close_matches(name, self.known, n=1)
            hint = f'did you mean {close[0]!r}? ' if close else ''
            hint += f'available: {", ".join(self.known)}'
        super().__init__(f'{prefix}unknown scheduler {name!r} ({hint})')


class Scheduler(ABC):
    '''A scheduler backend

    Parameters
    ----------
    cluster : ClusterSettings
    runner : Runner, optional
        Runs the scheduler's commands (default: :class:`SubprocessRunner`)
    '''

    #: Name of the backend: the value of ``scheduler`` in ``cluster.toml`` and
    #: the name of its raw-options tables
    name: ClassVar[str]

    def __init__(self, cluster, runner=None):
        self.cluster = cluster
        self.runner = runner if runner is not None else SubprocessRunner()

    # --- pure ---------------------------------------------------------------

    @abstractmethod
    def render_script(self, spec):
        '''The job script for ``spec`` (a WorkerSpec), as a string'''

    @abstractmethod
    def submit_command(self, script, spec):
        '''The command line (list of str) that submits ``script``'''

    @abstractmethod
    def parse_submit_output(self, stdout):
        '''The JobId in the output of the submit command

        Raises
        ------
        SchedulerError
            If the output is not understood
        '''

    @classmethod
    def validate_raw_options(cls, raw, where):
        '''Check the shape of this scheduler's raw options

        Values are passed through unchecked; only the structure the backend
        relies on is checked. The default accepts any table. A classmethod, so
        that tables can be checked without cluster settings (e.g. those of a
        project, for any installed backend).

        Parameters
        ----------
        raw : dict
            One raw-options table (e.g. ``[pbs]`` or ``[class.x.pbs]``)
        where : str
            File and table, for error messages (e.g. ``'jug_hpc.toml:
            [class.x.pbs]'``)

        Raises
        ------
        ConfigError
        '''

    # --- side effects (through self.runner) ---------------------------------

    def submit(self, script, spec):
        '''Submit ``script`` for ``spec``; returns its JobId

        Raises
        ------
        SchedulerError
        '''
        argv = self.submit_command(script, spec)
        try:
            result = self.runner.run(argv, cwd=spec.workdir)
        except CommandError as e:
            raise SchedulerError(f'{self.name}: could not submit {script}: {e}') from e
        return self.parse_submit_output(result.stdout)

    @abstractmethod
    def query(self, jobs):
        '''Current state of each job: dict JobId -> JobStatus

        Every job in ``jobs`` is in the result (with :attr:`JobState.UNKNOWN`
        if the scheduler does not know it, e.g. because it forgot a finished
        job).

        Raises
        ------
        SchedulerError
        '''

    @abstractmethod
    def outcome(self, jobs):
        '''How each finished job ended: dict JobId -> JobOutcome

        Jobs that have not finished, and jobs the scheduler no longer knows,
        are left out. An array job's outcome is that of its worst worker
        (:func:`combine_outcomes`).

        Raises
        ------
        SchedulerError
        '''

    @abstractmethod
    def cancel(self, jobs):
        '''Cancel ``jobs``; jobs that already finished are ignored'''


# --- registry -------------------------------------------------------------------

def _entry_points():
    '''The registered backends' entry points (replaced in tests)'''
    return entry_points(group=ENTRY_POINT_GROUP)


def _backends():
    '''name -> entry point; an error if two packages claim the same name'''
    found = {}
    for ep in _entry_points():
        other = found.get(ep.name)
        if other is not None and other.value != ep.value:
            raise SchedulerError(
                f'two scheduler backends are registered as {ep.name!r}: '
                f'{other.value} and {ep.value}')
        found[ep.name] = ep
    return found


def scheduler_names():
    '''Names of the installed scheduler backends, sorted'''
    return sorted(_backends())


def get_scheduler(name, where=None):
    '''The Scheduler subclass registered as ``name``

    Parameters
    ----------
    name : str
    where : str or Path, optional
        File that set ``name``, for the error message

    Raises
    ------
    UnknownSchedulerError
        If no backend is registered as ``name``
    SchedulerError
        If the backend cannot be loaded or is not a valid Scheduler
    '''
    backends = _backends()
    ep = backends.get(name)
    if ep is None:
        raise UnknownSchedulerError(name, sorted(backends), where)
    try:
        cls = ep.load()
    except Exception as e:
        raise SchedulerError(
            f'scheduler backend {name!r} ({ep.value}) could not be loaded: '
            f'{type(e).__name__}: {e}') from e
    if not (isinstance(cls, type) and issubclass(cls, Scheduler)):
        raise SchedulerError(
            f'scheduler backend {name!r} ({ep.value}) is not a Scheduler subclass')
    if getattr(cls, 'name', None) != name:
        raise SchedulerError(
            f'scheduler backend {name!r} ({ep.value}) calls itself '
            f'{getattr(cls, "name", None)!r}')
    return cls


def create_scheduler(cluster, runner=None):
    '''The backend for ``cluster.scheduler``, ready to use

    Raises
    ------
    UnknownSchedulerError
    SchedulerError
    '''
    cls = get_scheduler(cluster.scheduler, cluster.source('scheduler'))
    return cls(cluster, runner)
