'''Loading a jugfile through Jug

This is the only module of jug_hpc that imports Jug's internals (``jug.jug``,
``jug.task``). Everything else works with task *names* and the
:class:`JugState` returned by :func:`load_jugfile`.

Loading a jugfile has process-wide side effects in Jug: the global task list
(``jug.task.alltasks``), the store (``jug.task.Task.store``), the jugfile's
entry in ``sys.modules`` and ``sys.path``. :func:`load_jugfile` starts from
an empty task list and restores all of these afterwards, so that it can be
called repeatedly (by tests, and by the submitter on every poll). Modules the
jugfile imports stay in ``sys.modules``, as they would after any import.
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
    'JugState',
    'JugfileError',
    'default_jugdir',
    'load_jugfile',
    'load_project',
]


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
    '''
    jugfile: Path
    jugdir: str
    store: object = field(repr=False)
    tasks: tuple = field(repr=False)
    hit_barrier: bool = False

    @property
    def task_names(self):
        '''One name per task (so names repeat), in definition order'''
        return tuple(t.name for t in self.tasks)


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
    return JugState(
        jugfile=jugfile,
        jugdir=jugdir,
        store=store,
        tasks=tasks,
        hit_barrier=bool(jugspace.get('__jug__hasbarrier__', False)),
    )


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
