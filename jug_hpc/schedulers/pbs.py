'''PBS backend (OpenPBS / PBS Professional)

A worker is submitted with ``qsub <script>``, run from the project directory.
Its script starts with a ``#PBS`` header::

    #PBS -N jug-<class>-<project>
    #PBS -l select=1:ncpus=<cpus>:mem=<memory>[:<raw select>]
    #PBS -l walltime=HH:MM:SS
    #PBS -l <raw resource>=<value>        # one line per raw resource
    #PBS -q <partition>                   # if set
    #PBS -A <account>                     # if set
    #PBS -J 1-<workers>                   # only if workers > 1
    #PBS -o <logs>/scheduler/
    #PBS -e <logs>/scheduler/
    #PBS <raw directive>                  # one line per raw directive

followed by the shared body (:mod:`jug_hpc.script`). One worker is a plain job
and several are one array job (some PBS installations reject single-element
arrays).

Raw options (``[pbs]`` in ``cluster.toml``, ``[default.pbs]`` and
``[class.<name>.pbs]`` in ``jug_hpc.toml``) have three keys, because PBS
options don't map to one flat namespace:

- ``select``: a table of extra chunk resources, appended to the ``select``
  statement (e.g. ``ngpus = 1``);
- ``resources``: a table of extra job-wide ``-l`` resources (e.g.
  ``place = "scatter"``);
- ``directives``: a list of ``qsub`` options, each written as a ``#PBS`` line
  as is (e.g. ``"-m n"``; a leading ``#PBS`` is optional).

Values are passed through unchecked. Resources that jug_hpc sets itself
(``ncpus``, ``mem``, ``walltime``, ``select``) cannot be given as raw
resources: they come from ``cpus``, ``memory`` and ``time``, which the worker
script also relies on (``JUG_HPC_CPUS``, ``JUG_MAX_SECONDS``).

The scheduler's own output files go to ``<logs>/scheduler/``, given relative
to the project directory when the logs are inside it (``qsub`` resolves
relative paths against the directory it runs in). They catch what happens
before the worker redirects its output (e.g. a failing ``setup``); the
directory must exist before ``qsub`` runs.
'''
import difflib
import os
import re

from ..config.errors import ConfigError
from ..script import ScriptVars, render_script
from ..units import format_hms, memory_in_unit
from .base import JobId, Scheduler, SchedulerError

__all__ = [
    'JOB_NAME_MAX',
    'RAW_KEYS',
    'SCRIPT_VARS',
    'PBSScheduler',
    'format_memory',
    'header_lines',
    'job_name',
    'parse_job_id',
    'scheduler_log_dir',
    'validate_raw_options',
]

#: Keys of a ``[pbs]`` raw-options table, with their meaning
RAW_KEYS = {
    'select': 'extra chunk resources, appended to -l select=1:...',
    'resources': 'extra job-wide -l resources',
    'directives': 'list of qsub options, each written as a #PBS line',
}

#: Resources set from the portable settings, which raw options may not set
_RESERVED = {
    'select': {'ncpus': 'cpus', 'mem': 'memory'},
    'resources': {'select': 'cpus and memory', 'walltime': 'time',
                  'ncpus': 'cpus', 'mem': 'memory'},
}

#: Longest job name PBS Professional accepts
JOB_NAME_MAX = 236

#: The job's ID without the server name or array index (``1234`` from
#: ``1234.server`` and ``1234[2].server``), and the array index
SCRIPT_VARS = ScriptVars(job_id='${PBS_JOBID%%[.[]*}', array_index='$PBS_ARRAY_INDEX')

_RESOURCE_NAME_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')

# qsub prints the job ID (``1234.server``, ``1234[].server`` for an array) on
# its own line
_JOB_ID_RE = re.compile(r'\d+(?:\[\])?(?:\.[^\s]+)?\Z')

# characters that cannot appear in a path in a #PBS -o/-e directive (whitespace
# separates options; ':' separates a host name from the path)
_UNSAFE_PATH_RE = re.compile(r'[\s:"\'\\#]')


def format_memory(nbytes):
    '''Memory in PBS units, exact: ``16gb``, ``1536mb``, ``1000b``'''
    number, unit = memory_in_unit(nbytes, units='BKMGT')
    return f'{number}b' if unit == 'B' else f'{number}{unit.lower()}b'


def job_name(spec):
    '''The job name of ``spec``: ``jug-<class>-<project>``, made acceptable to PBS

    Characters other than letters, digits and ``_.+-`` become ``_``, and the
    name is cut to :data:`JOB_NAME_MAX` characters.
    '''
    return re.sub(r'[^A-Za-z0-9_.+-]', '_', spec.job_name)[:JOB_NAME_MAX]


def scheduler_log_dir(spec):
    '''Directory for PBS's own output files, as written in the script

    ``<logs>/scheduler``, relative to ``spec.workdir`` if it is inside it.

    Raises
    ------
    SchedulerError
        If the path cannot be written in a ``#PBS`` directive
    '''
    if spec.logs is None:
        raise ValueError('WorkerSpec.logs is not set')
    path = os.path.join(spec.logs, 'scheduler')
    rel = os.path.relpath(path, spec.workdir)
    if not rel.startswith(os.pardir):
        path = rel
    if _UNSAFE_PATH_RE.search(path):
        raise SchedulerError(
            f'pbs: the log directory {path!r} cannot be used in a #PBS directive '
            '(it contains whitespace, a quote, a backslash, \'#\' or \':\'); '
            'set logs in jug_hpc.toml to a simpler path')
    return path


def _format_value(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def header_lines(spec):
    '''The ``#PBS`` directive lines for ``spec``

    Parameters
    ----------
    spec : WorkerSpec
        ``spec.raw`` holds this backend's raw options

    Returns
    -------
    lines : list of str

    Raises
    ------
    ConfigError
        If the raw options have the wrong shape
    SchedulerError
        If the log directory cannot be written in a directive
    '''
    raw = spec.raw
    validate_raw_options(raw, f'raw options for class {spec.class_name!r}')
    chunk = [f'select=1:ncpus={spec.cpus}:mem={format_memory(spec.memory)}']
    chunk += [f'{k}={_format_value(v)}' for k, v in raw.get('select', {}).items()]
    lines = [
        f'#PBS -N {job_name(spec)}',
        '#PBS -l ' + ':'.join(chunk),
        f'#PBS -l walltime={format_hms(spec.time)}',
    ]
    lines += [f'#PBS -l {k}={_format_value(v)}' for k, v in raw.get('resources', {}).items()]
    for option, what, value in (('-q', 'partition', spec.partition),
                                ('-A', 'account', spec.account)):
        if value:
            if re.search(r'\s', value):
                raise SchedulerError(f'pbs: {what} {value!r} must not contain whitespace')
            lines.append(f'#PBS {option} {value}')
    if spec.is_array:
        lines.append(f'#PBS -J 1-{spec.workers}')
    logdir = scheduler_log_dir(spec)
    lines += [f'#PBS -o {logdir}/', f'#PBS -e {logdir}/']
    for d in raw.get('directives', []):
        d = d.strip()
        lines.append(d if d.startswith('#PBS') else f'#PBS {d}')
    return lines


def parse_job_id(stdout):
    '''The job ID in ``qsub``'s output (its last line that looks like one)

    Raises
    ------
    SchedulerError
    '''
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if _JOB_ID_RE.match(line):
            return JobId(line)
    raise SchedulerError(f'pbs: no job ID in the output of qsub: {stdout.strip()!r}')


def validate_raw_options(raw, where):
    '''Check the shape of one ``[pbs]`` raw-options table

    Parameters
    ----------
    raw : dict
    where : str
        File and table, for error messages

    Raises
    ------
    ConfigError
    '''
    def error(key, message):
        return ConfigError(f'{where}: {key!r} {message}')

    if not isinstance(raw, dict):
        raise ConfigError(f'{where}: must be a table')
    for k, v in raw.items():
        if k not in RAW_KEYS:
            close = difflib.get_close_matches(k, RAW_KEYS, n=1)
            hint = f'did you mean {close[0]!r}?' if close else \
                f'known keys: {", ".join(RAW_KEYS)}'
            raise error(k, f'is not a PBS raw option ({hint})')
        if k == 'directives':
            if not isinstance(v, list) or not all(isinstance(d, str) for d in v):
                raise error(k, 'must be a list of strings (qsub options)')
            for d in v:
                if not d.strip():
                    raise error(k, 'must not contain empty strings')
                if '\n' in d or '\r' in d:
                    raise error(k, f'must not contain newlines (in {d!r})')
            continue
        if not isinstance(v, dict):
            raise error(k, 'must be a table of PBS resources (e.g. ngpus = 1)')
        for name, value in v.items():
            key = f'{k}.{name}'
            if not _RESOURCE_NAME_RE.match(name):
                raise error(key, 'is not a valid PBS resource name')
            if name in _RESERVED[k]:
                raise error(key, f'cannot be set as a raw option: use '
                                 f'{_RESERVED[k][name]} in jug_hpc.toml')
            if not isinstance(value, (str, int, float)):
                raise error(key, 'must be a string, a number or a boolean')
            text = _format_value(value)
            if not text.strip() or re.search(r'\s', text):
                raise error(key, f'must not be empty or contain whitespace (it is {text!r})')
            if k == 'select' and ':' in text:
                raise error(key, f"must not contain ':' (it is {text!r})")


class PBSScheduler(Scheduler):
    '''OpenPBS / PBS Professional'''

    name = 'pbs'

    def render_script(self, spec):
        return render_script(spec, header_lines(spec), SCRIPT_VARS)

    def submit_command(self, script, spec):
        return ['qsub', str(script)]

    def parse_submit_output(self, stdout):
        return parse_job_id(stdout)

    @classmethod
    def validate_raw_options(cls, raw, where):
        validate_raw_options(raw, where)

    def query(self, jobs):
        raise NotImplementedError('pbs: querying jobs is not implemented yet')

    def outcome(self, jobs):
        raise NotImplementedError('pbs: job outcomes are not implemented yet')

    def cancel(self, jobs):
        raise NotImplementedError('pbs: cancelling jobs is not implemented yet')
