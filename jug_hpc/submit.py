'''Static submission: ``jug-hpc submit --all`` and ``--dry-run``

Every class is submitted at once, sized from the configuration. This is the
"naive" mode: it suits flat task graphs, where the workers of one class do not
wait for the tasks of another, and is useful before the polling submitter
exists. A worker that waits for the tasks of another class gives up after a
few minutes (see :data:`jug_hpc.script.NR_WAIT_CYCLES`).

The work is split in three steps, so that ``--dry-run`` and ``--all`` share
everything but the last:

1. :func:`prepare` builds a :class:`PreparedWorker` (the WorkerSpec and the
   rendered script) for every class that has tasks. It does no I/O.
2. :func:`write_scripts` saves each script in ``.jug_hpc/scripts/`` and
   creates the log directories, which the scheduler needs before submission
   (PBS does not create the directory of its ``-o``/``-e`` files). This is
   all that ``--dry-run`` does, so that the commands it prints can be run by
   hand as they are.
3. :func:`submit_worker` submits one script and records the job in
   ``.jug_hpc/jobs.json``.

Which classes are submitted, and how many workers each gets:

- a class with no tasks is skipped, and a class gets at most one worker per
  task (``min(workers, tasks)``);
- unless loading the jugfile stopped at a ``barrier()``: the tasks after it
  are not known yet, so every class gets its configured number of workers.

Tasks are counted whether or not they have already run: deciding which tasks
are ready is the job of the polling submitter (milestone 3).
'''
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .classes import Classifier, classify
from .config.errors import ConfigError
from .config.project import jugdir_problem
from .schedulers.checks import check_scheduler_settings
from .script import jug_execute_command
from .state import StateError
from .worker import make_worker_spec

__all__ = [
    'Preparation',
    'PreparedWorker',
    'WrittenWorker',
    'check_jugdir',
    'format_dry_run',
    'prepare',
    'submit_worker',
    'write_scripts',
]


@dataclass(frozen=True)
class PreparedWorker:
    '''The workers of one class, ready to be written and submitted

    Attributes
    ----------
    class_name : str
    tasks : int
        Number of tasks of the class (when the jugfile was loaded)
    spec : WorkerSpec
    script : str
        The rendered script
    '''
    class_name: str
    tasks: int
    spec: object
    script: str


@dataclass(frozen=True)
class WrittenWorker:
    '''A prepared worker whose script has been saved

    Attributes
    ----------
    prepared : PreparedWorker
    path : Path
        Absolute path of the script
    script : Path
        The script as named in ``command``: relative to the project
        directory, which the command is run from, if it is inside it
    command : tuple of str
        The command that submits it
    '''
    prepared: PreparedWorker
    path: Path
    script: Path
    command: tuple


@dataclass
class Preparation:
    '''The result of :func:`prepare`

    Attributes
    ----------
    workers : list of PreparedWorker
        In the order of the file, the default class last
    notes : list of str
        Informational (classes skipped, numbers of workers reduced, ...)
    warnings : list of str
        From the scheduler settings check
    '''
    workers: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def check_jugdir(project):
    '''Reject jugdirs that concurrent workers cannot share

    Raises
    ------
    ConfigError
        For ``dict_store`` and ``dict_store:<file>``: every worker would keep
        its own copy of the store in memory
    '''
    problem = jugdir_problem(project)
    if problem is not None:
        raise ConfigError(problem)


def prepare(cluster, project, state, scheduler, *, classes=None, jug='jug'):
    '''Build the workers of every class to submit

    Parameters
    ----------
    cluster : ClusterSettings
    project : ProjectSettings
    state : JugState
        The project's jugfile, loaded
    scheduler : Scheduler
    classes : sequence of str, optional
        Only these classes (default: every class with tasks). A class named
        here is submitted even if it has no tasks.
    jug : str, optional
        The ``jug`` executable in the worker scripts

    Returns
    -------
    preparation : Preparation

    Raises
    ------
    ConfigError
        If the settings are invalid for this scheduler, the jugdir cannot be
        shared, a class named in ``classes`` does not exist, a task is matched
        by two classes, or the default class's tasks cannot be selected
    SchedulerError
        If a backend cannot be loaded or a script cannot be rendered
    '''
    found = check_scheduler_settings(cluster, project)
    if found.errors:
        raise found.errors[0]
    check_jugdir(project)
    result = Preparation(warnings=list(found.warnings))

    c = classify(Classifier.from_project(project), state.task_names)
    if c.errors:
        raise c.errors[0]
    if classes is None:
        selected = list(c.class_names)
    else:
        for name in classes:
            if name not in c.class_names:
                known = ', '.join(c.class_names)
                raise ConfigError(f'{project.path}: no class {name!r} (classes: {known})')
        selected = [n for n in c.class_names if n in set(classes)]

    for name in selected:
        rc = project.get_class(name)
        tasks = c.total(name)
        workers = rc.workers
        if not state.hit_barrier:
            if tasks == 0:
                if classes is None:
                    result.notes.append(f'{_label(name)}: no tasks, not submitted')
                    continue
            elif tasks < workers:
                result.notes.append(
                    f'{_label(name)}: {_n(tasks, "task")}, so {_n(tasks, "worker")} '
                    f'instead of {workers}')
                workers = tasks
        command = jug_execute_command(project, rc, jug=jug, task_names=state.task_names)
        spec = make_worker_spec(cluster, project, rc, command, workers=max(workers, 1))
        result.workers.append(PreparedWorker(name, tasks, spec, scheduler.render_script(spec)))
    if state.hit_barrier:
        result.notes.append(
            'loading stopped at a barrier(): tasks defined after it are not known '
            'yet, so every class gets its configured number of workers')
    return result


def write_scripts(preparation, scheduler, project_state):
    '''Save every script and create the log directories

    Parameters
    ----------
    preparation : Preparation
    scheduler : Scheduler
    project_state : ProjectState

    Returns
    -------
    written : list of WrittenWorker

    Raises
    ------
    StateError
        If a script or a log directory cannot be written
    '''
    written = []
    for w in preparation.workers:
        path = project_state.write_script(w.class_name, w.script)
        logs = w.spec.scheduler_logs
        try:
            logs.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StateError(f'{logs}: cannot create the log directory: {e}') from e
        script = path.relative_to(project_state.project_dir) \
            if path.is_relative_to(project_state.project_dir) else path
        command = tuple(scheduler.submit_command(script, w.spec))
        written.append(WrittenWorker(w, path, script, command))
    return written


def submit_worker(written, scheduler, project_state):
    '''Submit one written worker and record its job

    Parameters
    ----------
    written : WrittenWorker
    scheduler : Scheduler
    project_state : ProjectState

    Returns
    -------
    record : JobRecord

    Raises
    ------
    SchedulerError
        If submission fails (nothing was submitted)
    StateError
        If the job was submitted but could not be recorded (the message
        gives its ID)
    '''
    p = written.prepared
    job_id = scheduler.submit(written.script, p.spec)
    try:
        return project_state.add_job(job_id, p.class_name, scheduler.name,
                                     p.spec.workers, written.path)
    except StateError as e:
        raise StateError(f'job {job_id} ({_label(p.class_name)}) was submitted but '
                         f'could not be recorded: {e}') from e


def format_dry_run(written, project_dir):
    '''The ``--dry-run`` listing: every script, then the commands to submit them

    Parameters
    ----------
    written : list of WrittenWorker
    project_dir : Path

    Returns
    -------
    text : str
    '''
    if not written:
        return 'Nothing to submit.'
    parts = [f'==> {w.script} <==\n{w.prepared.script}' for w in written]
    commands = []
    for w in written:
        p = w.prepared
        commands.append(f'  # {_label(p.class_name)}: {_n(p.tasks, "task")}, '
                        f'{_n(p.spec.workers, "worker")}')
        commands.append(f'  {shlex.join(w.command)}')
    parts.append(f'To submit these scripts, run from {project_dir}:\n' + '\n'.join(commands))
    return '\n'.join(parts)


def _table(name):
    return 'default' if name == 'default' else f'class.{name}'


def _label(name):
    return f'[{_table(name)}]'


def _n(n, what):
    return f'{n} {what}' if n == 1 else f'{n} {what}s'
