'''The worker script: the part shared by every scheduler

A worker script is a backend's directive header (``#PBS ...``) followed by the
body rendered here::

    set -e
    cd -- <workdir>
    JUG_HPC_JOB_ID="<backend expression>"
    mkdir -p -- <logs>
    exec >><logs>/<class>-"$JUG_HPC_JOB_ID".log 2>&1
    export JUG_HPC_CLASS=<class> JUG_HPC_JOB_ID JUG_HPC_CPUS=<cpus> OMP_NUM_THREADS=<cpus>
    <cluster setup>
    <project setup>
    unset JUG_MAX_HOURS JUG_MAX_MINUTES
    export JUG_MAX_SECONDS=<max_seconds>
    exec jug execute ...

The submitter job (``jug-hpc submit --as-job``) has the same script, with
``class`` ``submitter``, no ``JUG_MAX_*`` lines, and ``exec jug-hpc submit
--interval=<seconds> [--max-interval=<seconds>]`` as its command
(:func:`submitter_command`).

Choices made here:

- The script changes to the project directory itself (PBS starts jobs in the
  home directory).
- The worker redirects its own output, so log names are the same on every
  scheduler and logs can be read while the job runs.
- Only ``set -e``, not ``set -u``: setup snippets often source scripts (conda
  activation, environment modules) that use unset variables.
- The CPU variables are exported *before* the setup snippets, so a snippet
  can override them; ``JUG_MAX_*`` are set *after* them, so that nothing
  (neither the user's environment nor a snippet) changes the time limit.
  Jug adds up ``JUG_MAX_HOURS``, ``JUG_MAX_MINUTES`` and ``JUG_MAX_SECONDS``.
- Workers run ``jug execute --keep-failed``: a task that raises stays locked
  as failed (until ``jug cleanup --failed-only``). Without it, Jug releases
  the lock and the task is ready again, so every new worker would run it again
  and the polling submitter would keep submitting workers for it; with it, the
  submitter sees failed tasks and stops when nothing else can run.

Every value is quoted with :func:`shlex.quote`, except the shell expressions
supplied by the backend (job ID, array index), which are placed inside double
quotes.
'''
import shlex
from dataclasses import dataclass

from .jugstate import default_jugdir
from .selection import selector_args

__all__ = [
    'NR_WAIT_CYCLES',
    'WAIT_CYCLE_TIME',
    'ScriptVars',
    'jug_execute_command',
    'log_path',
    'render_body',
    'render_script',
    'submitter_command',
]

#: ``jug execute --nr-wait-cycles``: workers wait at most a few minutes for
#: dependencies (Jug's default is 30 minutes), since the submitter only starts
#: workers when tasks are ready
NR_WAIT_CYCLES = 15

#: ``jug execute --wait-cycle-time`` (seconds; Jug's default)
WAIT_CYCLE_TIME = 12


@dataclass(frozen=True)
class ScriptVars:
    '''Shell expressions, supplied by a backend, evaluated inside the job

    Each is placed inside double quotes, so it may use ``$VAR`` and
    ``${VAR%%.*}`` but must not contain an unescaped ``"``.

    Attributes
    ----------
    job_id : str
        The job's ID, as used in log names (e.g. ``${PBS_JOBID%%.*}``)
    array_index : str or None
        The index of this worker in an array job (e.g. ``$PBS_ARRAY_INDEX``);
        only used when the spec is an array
    '''
    job_id: str
    array_index: str | None = None

    def __post_init__(self):
        for name in ('job_id', 'array_index'):
            value = getattr(self, name)
            if value is not None and not _safe_in_double_quotes(value):
                raise ValueError(f'{name} {value!r} cannot be placed in double quotes')


def _safe_in_double_quotes(expr):
    i = 0
    while i < len(expr):
        c = expr[i]
        if c == '\\':
            if i == len(expr) - 1:
                return False
            i += 2
            continue
        if c in '"`':
            return False
        i += 1
    return True


def jug_execute_command(project, rc, *, jug='jug', exclude=None, task_names=None,
                        nr_wait_cycles=NR_WAIT_CYCLES, wait_cycle_time=WAIT_CYCLE_TIME):
    '''The ``jug execute`` command line of a worker of class ``rc``

    Parameters
    ----------
    project : ProjectSettings
    rc : ResourceClass
    jug : str, optional
        The ``jug`` executable (found on ``PATH`` after the setup snippets)
    exclude, task_names
        See :func:`jug_hpc.selection.selector_args`
    nr_wait_cycles, wait_cycle_time : int, optional

    Returns
    -------
    argv : tuple of str

    Raises
    ------
    SelectorError
        See :func:`jug_hpc.selection.selector_args`
    '''
    jugdir = project.jugdir if project.jugdir is not None else default_jugdir(project.jugfile)
    return (
        jug, 'execute',
        '--keep-failed',
        *selector_args(rc, project.all_classes(), exclude=exclude,
                       task_names=task_names, path=project.path),
        f'--nr-wait-cycles={nr_wait_cycles}',
        f'--wait-cycle-time={wait_cycle_time}',
        f'--jugdir={jugdir}',
        '--',
        str(project.jugfile),
    )


def submitter_command(interval, max_interval=None, *, jug_hpc='jug-hpc'):
    '''The command line of the submitter job: ``jug-hpc submit --interval=<s>``

    The job starts in the project directory, so no ``-C`` is needed.

    Parameters
    ----------
    interval : float
        Seconds between polls
    max_interval : float, optional
        Longest wait between polls (default: none given, so the job uses the
        default for ``interval``)
    jug_hpc : str, optional
        The ``jug-hpc`` executable (found on ``PATH`` after the setup snippets)

    Returns
    -------
    argv : tuple of str
    '''
    argv = (jug_hpc, 'submit', f'--interval={interval:g}')
    if max_interval is not None:
        argv += (f'--max-interval={max_interval:g}',)
    return argv


def log_path(spec):
    '''The worker's log file, as a shell word (quoted, with the backend's expressions)

    ``<logs>/<class>-<job id>.log``, or ``<logs>/<class>-<job id>-<index>.log``
    for an array.
    '''
    name = '"$JUG_HPC_JOB_ID"'
    if spec.is_array:
        name += '-"$JUG_HPC_ARRAY_INDEX"'
    return shlex.quote(f'{spec.logs}/{spec.class_name}-') + name + '.log'


def render_body(spec, variables):
    '''The scheduler-independent part of the worker script for ``spec``

    Parameters
    ----------
    spec : WorkerSpec
        ``spec.logs`` and ``spec.command`` must be set; without
        ``spec.max_seconds`` (the submitter job), ``JUG_MAX_*`` are not set
    variables : ScriptVars

    Returns
    -------
    body : str
        Ends with a newline
    '''
    if spec.logs is None:
        raise ValueError('WorkerSpec.logs is not set')
    if not spec.command:
        raise ValueError('WorkerSpec.command is not set')
    if spec.is_array and variables.array_index is None:
        raise ValueError('an array worker needs ScriptVars.array_index')
    q = shlex.quote
    lines = [
        'set -e',
        f'cd -- {q(str(spec.workdir))}',
        f'JUG_HPC_JOB_ID="{variables.job_id}"',
    ]
    exports = [f'JUG_HPC_CLASS={q(spec.class_name)}', 'JUG_HPC_JOB_ID']
    if spec.is_array:
        lines.append(f'JUG_HPC_ARRAY_INDEX="{variables.array_index}"')
        exports.append('JUG_HPC_ARRAY_INDEX')
    exports += [f'JUG_HPC_CPUS={spec.cpus}', f'OMP_NUM_THREADS={spec.cpus}']
    lines += [
        f'mkdir -p -- {q(str(spec.logs))}',
        f'exec >>{log_path(spec)} 2>&1',
        'export ' + ' '.join(exports),
    ]
    for snippet in spec.setup:
        lines += ['', snippet.strip('\n')]
    if spec.setup:
        lines.append('')
    if spec.max_seconds is not None:
        lines += [
            'unset JUG_MAX_HOURS JUG_MAX_MINUTES',
            f'export JUG_MAX_SECONDS={int(spec.max_seconds)}',
        ]
    lines.append('exec ' + shlex.join(spec.command))
    return '\n'.join(lines) + '\n'


def render_script(spec, header, variables):
    '''A complete worker script: ``#!/bin/bash``, the backend's header, the body

    Parameters
    ----------
    spec : WorkerSpec
    header : iterable of str
        Directive lines (e.g. ``#PBS -l walltime=01:00:00``), without newlines
    variables : ScriptVars

    Returns
    -------
    script : str
    '''
    header = list(header)
    for line in header:
        if '\n' in line or '\r' in line:
            raise ValueError(f'header line {line!r} contains a newline')
    return '\n'.join(['#!/bin/bash', *header, '', '']) + render_body(spec, variables)
