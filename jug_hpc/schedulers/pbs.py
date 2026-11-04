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

Jobs are queried with ``qstat -f -F json -x JOB...`` (``-x`` includes finished
jobs while the server keeps their history), :data:`QSTAT_BATCH` jobs per call.
A plain job's ``job_state`` letter gives its state; an array job's
``array_state_count`` (``Queued:1 Running:2 Exiting:0 Expired:3``) gives how
many of its workers are queued or running. Jobs that ``qstat`` does not list
(``Unknown Job Id``, e.g. finished jobs whose history was purged) are
:attr:`~.JobState.UNKNOWN`. Output is matched to the queried IDs by job number
(``1234`` or ``1234[]``), as the server part of an ID may be written
differently by ``qsub`` and ``qstat``.

How finished jobs ended (:meth:`PBSScheduler.outcome`) is read from the same
command with ``-t``, which also lists an array's elements (``1234[1].server``,
...), each with its own ``Exit_status``. Its meaning (PBS Professional
reference guide, "Job exit status"):

- ``0``: success; ``1``--``255``: the exit status of the job script (``128 +
  N`` if the shell saw a command killed by signal ``N``);
- ``256`` or more: the job script was killed by signal ``Exit_status % 256``;
- negative: PBS's own codes (:data:`PBS_EXIT_CODES`), e.g. ``-27`` and ``-29``
  when PBS killed the job for exceeding its memory or wall time.

As a wall time kill often shows as a plain ``SIGTERM``/``SIGKILL`` (``271``,
``265``), a job that did not succeed and used at least its requested
``walltime`` (``resources_used`` against ``Resource_List``) counts as killed
for wall time. An array job without listed elements falls back to the array's
own ``Exit_status`` (0: every element succeeded; 1: some element failed; 2:
some element was deleted or PBS failed). An array job's outcome is that of its
worst element (:func:`~.base.combine_outcomes`). These codes are taken from
the documentation and OpenPBS's sources, not yet checked on a real cluster.
'''
import difflib
import json
import os
import re
from collections import defaultdict

from ..config.errors import ConfigError
from ..runner import CommandError
from ..script import ScriptVars, render_script
from ..units import format_hms, memory_in_unit
from .base import (
    JobId,
    JobOutcome,
    JobState,
    JobStatus,
    Outcome,
    Scheduler,
    SchedulerError,
    combine_outcomes,
    signal_name,
)

__all__ = [
    'JOB_NAME_MAX',
    'JOB_STATES',
    'PBS_EXIT_CODES',
    'QSTAT_BATCH',
    'QSTAT_TIMEOUT',
    'RAW_KEYS',
    'SCRIPT_VARS',
    'PBSScheduler',
    'array_exit_outcome',
    'element_outcome',
    'format_memory',
    'header_lines',
    'job_name',
    'job_status',
    'match_jobs',
    'parse_array_state_count',
    'parse_job_id',
    'parse_qstat_outcomes',
    'parse_qstat_output',
    'qstat_command',
    'scheduler_log_dir',
    'unexpected_qstat_errors',
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

#: What each ``job_state`` letter of ``qstat`` means. Suspended jobs (S, U)
#: still hold their allocation, so they count as running; a job moved to
#: another server (M) is no longer known to this one.
JOB_STATES = {
    'Q': JobState.QUEUED,      # queued
    'H': JobState.QUEUED,      # held
    'W': JobState.QUEUED,      # waiting for its start time
    'T': JobState.QUEUED,      # being moved
    'R': JobState.RUNNING,     # running
    'E': JobState.RUNNING,     # exiting
    'B': JobState.RUNNING,     # array job: at least one element has started
    'S': JobState.RUNNING,     # suspended
    'U': JobState.RUNNING,     # suspended (workstation busy)
    'F': JobState.FINISHED,    # finished
    'X': JobState.FINISHED,    # array element: finished
    'M': JobState.UNKNOWN,     # moved to another server
}

#: PBS's own (negative) exit statuses, as in OpenPBS's ``job.h``: code ->
#: (Outcome, explanation). Codes not listed are other PBS errors that kept the
#: job from running (:attr:`Outcome.FAILED`).
PBS_EXIT_CODES = {
    -1: (Outcome.FAILED, 'PBS could not run the job'),
    -2: (Outcome.FAILED, 'PBS could not run the job'),
    -3: (Outcome.FAILED, 'PBS could not run the job'),
    -4: (Outcome.FAILED, 'the job was aborted when its execution host restarted'),
    -10: (Outcome.FAILED, 'PBS could not run the job: invalid user or group'),
    -17: (Outcome.FAILED, 'a PBS hook rejected the job'),
    -19: (Outcome.FAILED, 'a PBS hook deleted the job'),
    -20: (Outcome.FAILED, "the connection to the job's execution host was lost"),
    -22: (Outcome.FAILED, 'a PBS hook failed or timed out'),
    -24: (Outcome.KILLED_SIGNAL, 'killed by PBS: used more CPUs than requested'),
    -25: (Outcome.KILLED_SIGNAL, 'killed by PBS: used more CPUs than requested'),
    -26: (Outcome.KILLED_MEMORY, 'killed by PBS: virtual memory limit exceeded'),
    -27: (Outcome.KILLED_MEMORY, 'killed by PBS: memory limit exceeded'),
    -28: (Outcome.KILLED_SIGNAL, 'killed by PBS: CPU time limit exceeded'),
    -29: (Outcome.KILLED_TIME, 'killed by PBS: wall time limit exceeded'),
}

#: Jobs per ``qstat`` call
QSTAT_BATCH = 100

#: Seconds before a ``qstat`` call is given up
QSTAT_TIMEOUT = 300

# qstat's errors about jobs it does not list: unknown jobs (e.g. history
# purged), and finished jobs when asked without -x
_QSTAT_MISSING_RE = re.compile(r'Unknown Job Id|Job has finished')

_ARRAY_COUNT_RE = re.compile(r'([A-Za-z]+):(\d+)\Z')

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
    path = str(spec.scheduler_logs)
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


def qstat_command(jobs, elements=False):
    '''The ``qstat`` command line that queries ``jobs``

    With ``elements``, it also lists the elements of array jobs (``-t``).
    '''
    return ['qstat', '-f', '-F', 'json', '-x', *(['-t'] if elements else []), *jobs]


def _short_id(job_id):
    '''The job number of a job ID: ``1234``, ``1234[]`` or ``1234[5]`` (no server)'''
    return job_id.split('.', 1)[0]


def _is_array(job_id, attrs):
    return attrs.get('array') in ('True', True) or _short_id(job_id).endswith('[]')


def parse_array_state_count(text):
    '''An array job's ``array_state_count``, as a dict

    Parameters
    ----------
    text : str
        E.g. ``'Queued:1 Running:2 Exiting:0 Expired:3 '``

    Returns
    -------
    counts : dict
        Name (``'Queued'``, ...) -> int; ``Queued`` and ``Running`` are always
        present

    Raises
    ------
    ValueError
    '''
    counts = {}
    for item in str(text).split():
        m = _ARRAY_COUNT_RE.match(item)
        if m is None:
            raise ValueError(f'bad array_state_count: {text!r}')
        counts[m[1]] = int(m[2])
    if 'Queued' not in counts or 'Running' not in counts:
        raise ValueError(f'bad array_state_count: {text!r}')
    return counts


def job_status(job_id, attrs):
    '''The JobStatus of one job, from its attributes in ``qstat -f -F json``

    Parameters
    ----------
    job_id : str
        As listed by ``qstat`` (``1234[].server`` for an array job)
    attrs : dict
        Its attributes (``job_state``, and ``array_state_count`` for an array)

    Raises
    ------
    ValueError
        If the attributes are not understood
    '''
    letter = attrs.get('job_state')
    state = JOB_STATES.get(letter)
    if state is None:
        raise ValueError(f'unknown job_state {letter!r}')
    is_array = _is_array(job_id, attrs)
    if state is JobState.QUEUED and not is_array:
        return JobStatus(state, queued=1)
    if state is JobState.RUNNING and not is_array:
        return JobStatus(state, running=1)
    if state not in (JobState.QUEUED, JobState.RUNNING):
        return JobStatus(state)
    counts = parse_array_state_count(attrs.get('array_state_count'))
    return JobStatus(state, queued=counts['Queued'],
                     running=counts['Running'] + counts.get('Exiting', 0))


def _qstat_jobs(stdout):
    '''The ``Jobs`` object of ``qstat -f -F json``'s output: JobId -> dict

    Raises
    ------
    SchedulerError
    '''
    if not stdout.strip():
        return {}
    def error(what):
        return SchedulerError(f'pbs: could not parse the output of qstat ({what})')

    try:
        # strict=False: some PBS versions leave control characters unescaped
        data = json.loads(stdout, strict=False)
    except ValueError as e:
        raise error(e) from e
    if not isinstance(data, dict):
        raise error('not a JSON object')
    listed = data.get('Jobs', {})
    if not isinstance(listed, dict):
        raise error("'Jobs' is not a JSON object")
    for job_id, attrs in listed.items():
        if not isinstance(attrs, dict):
            raise error(f'job {job_id}: its attributes are not a JSON object')
    return {JobId(j): attrs for j, attrs in listed.items()}


def parse_qstat_output(stdout):
    '''The status of each job listed by ``qstat -f -F json``

    Parameters
    ----------
    stdout : str
        May be empty (``qstat`` lists nothing when it knows none of the jobs)

    Returns
    -------
    jobs : dict
        JobId (as listed) -> JobStatus

    Raises
    ------
    SchedulerError
        If the output is not understood
    '''
    jobs = {}
    for job_id, attrs in _qstat_jobs(stdout).items():
        try:
            jobs[job_id] = job_status(job_id, attrs)
        except ValueError as e:
            raise SchedulerError(
                f'pbs: could not parse the output of qstat (job {job_id}: {e})') from e
    return jobs


_HMS_RE = re.compile(r'(\d+):(\d{2}):(\d{2})\Z')


def _seconds(hms):
    '''``HH:MM:SS`` as seconds, or None'''
    m = _HMS_RE.match(hms) if isinstance(hms, str) else None
    return None if m is None else (int(m[1]) * 60 + int(m[2])) * 60 + int(m[3])


def _resource(attrs, table, name):
    value = attrs.get(table)
    return value.get(name) if isinstance(value, dict) else None


def _exit_status(attrs):
    '''``Exit_status`` as an int, or None'''
    status = attrs.get('Exit_status')
    if isinstance(status, str) and re.fullmatch(r'\s*-?\d+\s*', status):
        return int(status)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def element_outcome(attrs, limits=None):
    '''How a finished plain job or array element ended, from its ``qstat`` attributes

    Parameters
    ----------
    attrs : dict
        Its attributes in ``qstat -f -F json -x``: ``Exit_status``, and
        ``resources_used`` and ``Resource_List`` (for wall time kills)
    limits : dict, optional
        The array job's ``Resource_List``, for an element that lists none

    Returns
    -------
    outcome : JobOutcome
    '''
    status = _exit_status(attrs)
    if status is None:
        return JobOutcome(Outcome.UNKNOWN,
                          detail='no exit status (deleted before it started?)')
    if status == 0:
        return JobOutcome(Outcome.SUCCESS, 0)
    if status < 0:
        outcome, what = PBS_EXIT_CODES.get(status, (Outcome.FAILED, 'PBS error'))
        result = JobOutcome(outcome, status, None, f'{what} (Exit_status {status})')
    elif status >= 256:
        signum = status % 256
        detail = f'killed by {signal_name(signum)} (Exit_status {status})'
        used_mem = _resource(attrs, 'resources_used', 'mem')
        mem = _resource(attrs, 'Resource_List', 'mem') or (limits or {}).get('mem')
        if used_mem and mem:
            detail += f'; memory used: {used_mem} of {mem}'
        result = JobOutcome(Outcome.KILLED_SIGNAL, status, signum, detail)
    else:
        detail = f'exit status {status}'
        if 128 < status <= 128 + 64:
            detail += f' (128 + {signal_name(status - 128)})'
        result = JobOutcome(Outcome.FAILED, status, None, detail)
    if result.outcome in (Outcome.KILLED_MEMORY, Outcome.KILLED_TIME):
        return result
    used = _resource(attrs, 'resources_used', 'walltime')
    limit = _resource(attrs, 'Resource_List', 'walltime') or (limits or {}).get('walltime')
    used_s, limit_s = _seconds(used), _seconds(limit)
    if used_s is not None and limit_s is not None and used_s >= limit_s:
        return JobOutcome(Outcome.KILLED_TIME, status, result.signal,
                          f'wall time limit reached (used {used} of {limit}); '
                          f'{result.detail}')
    return result


def array_exit_outcome(attrs):
    '''How a finished array job ended, from its own ``Exit_status`` only

    For when ``qstat`` does not list the array's elements.
    '''
    status = _exit_status(attrs)
    if status == 0:
        return JobOutcome(Outcome.SUCCESS, 0)
    if status == 1:
        return JobOutcome(Outcome.FAILED, 1, None, 'some workers failed (Exit_status 1)')
    detail = 'no exit status' if status is None else (
        f'some workers were deleted or PBS failed (Exit_status {status})')
    return JobOutcome(Outcome.UNKNOWN, status, None, detail)


_ELEMENT_RE = re.compile(r'(\d+)\[(\d+)\]\Z')


def parse_qstat_outcomes(stdout):
    '''How each finished job listed by ``qstat -f -F json -x -t`` ended

    Parameters
    ----------
    stdout : str
        May be empty

    Returns
    -------
    outcomes : dict
        JobId (as listed) -> JobOutcome, for plain jobs and array jobs that
        have finished (``job_state`` F); array elements are combined into
        their array's outcome

    Raises
    ------
    SchedulerError
        If the output is not understood
    '''
    jobs = {}
    elements = defaultdict(dict)        # array job number -> index -> attrs
    for job_id, attrs in _qstat_jobs(stdout).items():
        m = _ELEMENT_RE.match(_short_id(job_id))
        if m:
            elements[m[1]][int(m[2])] = attrs
        else:
            jobs[job_id] = attrs
    outcomes = {}
    for job_id, attrs in jobs.items():
        if attrs.get('job_state') != 'F':
            continue
        if not _is_array(job_id, attrs):
            outcomes[job_id] = element_outcome(attrs)
            continue
        listed = elements.get(_short_id(job_id).removesuffix('[]'))
        if listed:
            limits = attrs.get('Resource_List')
            limits = limits if isinstance(limits, dict) else None
            outcomes[job_id] = combine_outcomes(
                {i: element_outcome(a, limits) for i, a in listed.items()})
        else:
            outcomes[job_id] = array_exit_outcome(attrs)
    return outcomes


def unexpected_qstat_errors(stderr):
    '''Lines of ``qstat``'s stderr other than those about jobs it does not list'''
    return [line for line in stderr.splitlines()
            if line.strip() and not _QSTAT_MISSING_RE.search(line)]


def match_jobs(jobs, found):
    '''The status of each of ``jobs``, from what ``qstat`` listed

    Parameters
    ----------
    jobs : list of JobId
        As printed by ``qsub``
    found : dict
        JobId (as listed by ``qstat``) -> JobStatus

    Returns
    -------
    statuses : dict
        Every job in ``jobs`` -> JobStatus (:attr:`JobState.UNKNOWN` if not
        listed). A job is matched by its full ID, or else by its job number.
    '''
    matched = _matched(jobs, found)
    unknown = JobStatus(JobState.UNKNOWN)
    return {j: matched.get(j, unknown) for j in jobs}


def _matched(jobs, found):
    '''``found`` (keyed as listed by ``qstat``) keyed by those of ``jobs`` it has'''
    by_number = {_short_id(j): v for j, v in found.items()}
    result = {}
    for j in jobs:
        if j in found:
            result[j] = found[j]
        elif _short_id(j) in by_number:
            result[j] = by_number[_short_id(j)]
    return result


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

    def _qstat(self, jobs, elements=False):
        '''Run ``qstat`` on ``jobs`` in batches; yields each call's stdout'''
        jobs = list(jobs)
        for start in range(0, len(jobs), QSTAT_BATCH):
            argv = qstat_command(jobs[start:start + QSTAT_BATCH], elements)
            try:
                result = self.runner.run(argv, check=False, timeout=QSTAT_TIMEOUT)
            except CommandError as e:
                raise SchedulerError(f'pbs: could not query jobs: {e}') from e
            # qstat exits with an error when it does not know some of the jobs
            if result.returncode != 0 and (unexpected_qstat_errors(result.stderr)
                                           or not result.stderr.strip()):
                error = CommandError(argv, result.returncode, result.stdout, result.stderr)
                raise SchedulerError(f'pbs: could not query jobs: {error}')
            yield result.stdout

    def query(self, jobs):
        jobs = list(jobs)
        found = {}
        for stdout in self._qstat(jobs):
            found.update(parse_qstat_output(stdout))
        return match_jobs(jobs, found)

    def outcome(self, jobs):
        jobs = list(jobs)
        found = {}
        for stdout in self._qstat(jobs, elements=True):
            found.update(parse_qstat_outcomes(stdout))
        return _matched(jobs, found)

    def cancel(self, jobs):
        raise NotImplementedError('pbs: cancelling jobs is not implemented yet')
