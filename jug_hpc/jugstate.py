'''Loading a jugfile through Jug

This is the only module of jug_hpc that imports Jug's internals (``jug.jug``,
``jug.task``). Everything else works with task *names* and the
:class:`JugState` returned by :func:`load_jugfile`.

Loading a jugfile has process-wide side effects in Jug: the global task list
(``jug.task.alltasks``), the store (``jug.task.Task.store``), the jugfile's
entry in ``sys.modules`` and ``sys.path``. :func:`load_jugfile` starts from
an empty task list and restores all of these afterwards, so that it can be
called repeatedly. Modules the jugfile imports stay in ``sys.modules``, as
they would after any import. So, on the next load in the same process, changes
to them are not seen, and tasks they create when imported are not created
again (and :func:`task_counts` fails, as a task then depends on a task that
is not in the jugfile). This is why the submitter, which reloads the jugfile
on every poll, loads it in a subprocess.

:func:`task_counts` counts the tasks of a loaded jugfile in each state
(waiting, ready, running, failed, finished) with the function behind ``jug
status`` (``update_status`` in ``jug.subcommands.status``), so the counts are
always the ones ``jug status`` would show.
'''
import contextlib
import logging
import os
import sys
import traceback
import warnings
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    'STATES',
    'JugState',
    'JugfileError',
    'TaskCounts',
    'default_jugdir',
    'load_jugfile',
    'load_project',
    'task_counts',
]

#: The states of a task, as ``jug status`` names them, in dependency order
STATES = ('waiting', 'ready', 'running', 'failed', 'finished')


class JugfileError(Exception):
    '''The jugfile could not be loaded

    The message names the jugfile and the exception it raised, so it can be
    shown to the user as is. ``details`` holds the formatted traceback (an
    empty string if there is none, e.g., when the jugfile does not exist).
    '''
    def __init__(self, message, details=''):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class JugState:
    '''The tasks of a jugfile, as Jug sees them

    Attributes
    ----------
    jugfile : Path
        The jugfile (absolute)
    jugdir : str
        The jugdir used to load it (a path or a backend URL, as passed to
        ``jug --jugdir``); barriers are checked against it
    store : jug store
        The store for ``jugdir``. ``Task.store`` is restored after loading, so
        pass it explicitly (``t.can_load(state.store)``)
    tasks : tuple of jug.Task
        Every task defined while loading, in definition order
    hit_barrier : bool
        Whether loading stopped at a ``barrier()`` or ``bvalue()`` whose tasks
        are not all done: more tasks may appear once they are
    hashes : tuple of bytes
        The hash of each task (computed while loading, as hashing may need
        the jugfile's module); empty if ``graph_error`` is set
    dependencies : tuple of tuple of int
        For each task, the indices (in ``tasks``) of its direct dependencies;
        empty if ``graph_error`` is set
    graph_error : str or None
        Why the hashes and dependencies could not be computed (naming the
        jugfile and the task), or None. Loading does not fail because of
        this, since the task names are still known, but
        :func:`task_counts` does.
    '''
    jugfile: Path
    jugdir: str
    store: object = field(repr=False)
    tasks: tuple = field(repr=False)
    hit_barrier: bool = False
    hashes: tuple = field(default=(), repr=False)
    dependencies: tuple = field(default=(), repr=False)
    graph_error: str | None = None

    @property
    def task_names(self):
        '''One name per task (so names repeat), in definition order'''
        return tuple(t.name for t in self.tasks)


@dataclass(frozen=True)
class TaskCounts:
    '''Number of tasks in each state (as ``jug status`` counts them)

    Attributes
    ----------
    waiting : int
        Not finished, and some dependency is not finished either
    ready : int
        Every dependency is finished, and no worker holds the task's lock
    running : int
        Locked by a worker (which may have died without releasing it)
    failed : int
        Marked as failed by a worker (``jug execute`` left a failed lock)
    finished : int
        The result is in the store
    '''
    waiting: int = 0
    ready: int = 0
    running: int = 0
    failed: int = 0
    finished: int = 0

    def __add__(self, other):
        if not isinstance(other, TaskCounts):
            return NotImplemented
        return TaskCounts(*(getattr(self, s) + getattr(other, s) for s in STATES))

    @property
    def total(self):
        '''Number of tasks, whatever their state'''
        return sum(getattr(self, s) for s in STATES)

    @property
    def done(self):
        '''True if every task is finished (also when there are none)'''
        return self.finished == self.total


def default_jugdir(jugfile):
    '''Jug's default jugdir for ``jugfile``: ``<jugfile without .py>.jugdata``

    This is what ``jug`` uses when neither ``--jugdir`` nor a ``jugrc`` file
    sets one (the ``%(jugfile)s.jugdata`` default in ``jug.options``).
    '''
    jugfile = str(jugfile)
    return jugfile[:-len('.py')] + '.jugdata'


def load_project(settings):
    '''Load the jugfile of a project (a ``ProjectSettings``)

    The jugfile is run from the project directory, as the workers will.
    '''
    return load_jugfile(settings.jugfile, settings.jugdir, cwd=settings.project_dir)


def load_jugfile(jugfile, jugdir=None, cwd=None):
    '''Load ``jugfile`` with Jug and return its tasks

    Parameters
    ----------
    jugfile : str or Path
        The jugfile (must end in ``.py``, as Jug requires). A relative path
        is taken relative to ``cwd``.
    jugdir : str or Path, optional
        The jugdir (default: :func:`default_jugdir`). It is not created. A
        Path is taken relative to ``cwd``; a str is passed to Jug as is (it
        may be a backend URL such as ``redis:...``), so a relative one only
        works while loading.
    cwd : str or Path, optional
        Directory to run the jugfile from (default: the current directory).
        Jug puts it first in ``sys.path``, and relative paths in the jugfile
        are relative to it. The current directory is restored afterwards.

    Returns
    -------
    JugState

    Raises
    ------
    JugfileError
        If the jugfile does not exist or raises an exception (including
        ``SystemExit``) while loading.
    '''
    cwd = Path.cwd() if cwd is None else Path(cwd).absolute()
    jugfile = cwd / jugfile
    if jugfile.suffix != '.py':
        raise JugfileError(f"{jugfile}: a jugfile's name must end in '.py'")
    if not jugfile.is_file():
        raise JugfileError(f'{jugfile}: jugfile not found')
    if jugdir is None:
        jugdir = default_jugdir(jugfile)
    elif isinstance(jugdir, Path):
        jugdir = str(cwd / jugdir)

    from jug import jug, task
    with _isolated(cwd, jugfile):
        try:
            store, jugspace = jug.init(str(jugfile), jugdir, on_error='propagate')
        except (Exception, SystemExit) as e:
            with warnings.catch_warnings():
                # linecache warns about the jugfile's module, which Jug
                # creates without a __spec__ (the source is still found)
                warnings.simplefilter('ignore', DeprecationWarning)
                details = _jugfile_traceback(e, jugfile)
            raise JugfileError(
                f'{jugfile}: error while loading the jugfile: '
                f'{type(e).__name__}: {e}',
                details) from e
        tasks = tuple(task.alltasks)
        hashes, dependencies, graph_error = _dependency_graph(tasks, jugfile)
    return JugState(
        jugfile=jugfile,
        jugdir=jugdir,
        store=store,
        tasks=tasks,
        hit_barrier=bool(jugspace.get('__jug__hasbarrier__', False)),
        hashes=hashes,
        dependencies=dependencies,
        graph_error=graph_error,
    )


def task_counts(state):
    '''Count the tasks of ``state`` in each state, by task name

    The store is read now, so the counts are those at the time of the call,
    not at the time ``state`` was loaded. Tasks after a barrier that was hit
    while loading are not counted, as they are not known yet.

    Parameters
    ----------
    state : JugState

    Returns
    -------
    counts : dict
        Task name -> :class:`TaskCounts`, in the order the names first appear

    Raises
    ------
    JugfileError
        If the dependency graph could not be built while loading (see
        ``JugState.graph_error``)
    '''
    from jug.subcommands.status import unknown, update_status
    if state.graph_error is not None:
        raise JugfileError(state.graph_error)
    ht = [(i, t.name, h, unknown)
          for i, (t, h) in enumerate(zip(state.tasks, state.hashes, strict=True))]
    deps = dict(enumerate(state.dependencies))
    status, _ = update_status(state.store, ht, deps, {})
    counts = {}
    for name in dict.fromkeys(state.task_names):
        counts[name] = TaskCounts(*(getattr(status, s).get(name, 0) for s in STATES))
    return counts


def _dependency_graph(tasks, jugfile):
    '''The hash of each task and the indices of its dependencies

    This must run while the jugfile is loaded: hashing a task may pickle
    functions defined in the jugfile, which needs its module in
    ``sys.modules``.

    Returns
    -------
    hashes : tuple of bytes
    dependencies : tuple of tuple of int
    error : str or None
        Why the graph could not be built (then ``hashes`` and
        ``dependencies`` are empty)
    '''
    index = {}
    hashes = []
    dependencies = []
    for i, t in enumerate(tasks):
        try:
            h = t.hash()
            deps = tuple(index[d.hash()] for d in t.dependencies())
        except KeyError:
            return (), (), (
                f'{jugfile}: could not build the dependency graph: task '
                f'{t.name!r} depends on a task that is not in the jugfile (this '
                'normally means that a task was built with a mutable argument '
                'that was modified afterwards)')
        except Exception as e:  # noqa: BLE001 (hashing runs arbitrary pickling code)
            return (), (), (
                f'{jugfile}: could not compute the hash of task {t.name!r}: '
                f'{type(e).__name__}: {e}')
        index[h] = i
        hashes.append(h)
        dependencies.append(deps)
    return tuple(hashes), tuple(dependencies), None


def _jugfile_traceback(e, jugfile):
    '''The traceback of ``e`` from the jugfile's first frame on

    The frames above it (jug_hpc and ``jug.init``) are of no use to the
    user. If the jugfile has no frame (e.g. a SyntaxError, raised while
    compiling it), only the exception is formatted.
    '''
    tb = e.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != str(jugfile):
        tb = tb.tb_next
    return ''.join(traceback.format_exception(type(e), e, tb))


class _DropJugImportError(logging.Filter):
    '''Drops the message ``jug.init`` logs before re-raising an import error

    The error is reported through :class:`JugfileError` instead.
    '''
    def filter(self, record):
        return not str(record.msg).startswith('Could not import file')


@contextlib.contextmanager
def _isolated(cwd, jugfile):
    '''Undo the global side effects of ``jug.init`` on exit'''
    from jug import task
    modname = jugfile.stem
    saved = {
        'cwd': os.getcwd(),
        'path': sys.path[:],
        'argv': sys.argv[:],
        'module': sys.modules.get(modname),
        'store': task.Task.store,
        'alltasks': task.alltasks[:],
    }
    log_filter = _DropJugImportError()
    root = logging.getLogger()
    root.addFilter(log_filter)
    del task.alltasks[:]
    # As `jug` does (see jug.options.parse): the jugfile sees itself as argv[0]
    sys.argv[:] = [str(jugfile)]
    os.chdir(cwd)
    try:
        yield
    finally:
        os.chdir(saved['cwd'])
        sys.path[:] = saved['path']
        sys.argv[:] = saved['argv']
        if saved['module'] is None:
            sys.modules.pop(modname, None)
        else:
            sys.modules[modname] = saved['module']
        task.Task.store = saved['store']
        task.alltasks[:] = saved['alltasks']
        root.removeFilter(log_filter)
