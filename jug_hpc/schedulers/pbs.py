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

- ``0``: success; ``1``--``255``: the exit status of the job script;
- ``256`` or more: the job script was killed by signal ``Exit_status % 256``;
- negative: PBS's own codes (:data:`PBS_EXIT_CODES`), e.g. ``-27`` and ``-29``
  when PBS killed the job for exceeding its memory or wall time.

A status of ``128 + N`` (up to ``128 + 64``) is taken as a kill by signal
``N``: the worker script ``exec``\\ s ``jug``, so the job's status is Jug's, and
this is how the shell (and PBS, on the clusters checked) reports a process
killed by a signal. A ``qdel`` leaves the job in ``substate`` 91
(:data:`SUBSTATE_TERMINATED`; 92 is finished, 93 failed), whatever its status.

As a wall time kill often shows as a plain ``SIGTERM``/``SIGKILL`` (``271``,
``265``), a job that did not succeed and used at least its requested
``walltime`` (``resources_used`` against ``Resource_List``) counts as killed
for wall time. An array job without listed elements falls back to the array's
own ``Exit_status`` (0: every element succeeded; 1: some element failed; 2:
some element was deleted or PBS failed). An array job's outcome is that of its
worst element (:func:`~.base.combine_outcomes`).

Checked on PBS Professional 2026.1 with cgroups (``tests/pbs_output/pbspro-2026.1/``):
a wall time kill is ``-29``; a ``qdel`` of a running job is ``143`` with
substate 91 (a running Jug worker exits with ``1``: Jug turns ``SIGTERM`` into
``sys.exit(1)``); a ``qdel`` of a whole array still queued leaves the array in
substate 91, but its elements in substate 92 with no ``Exit_status``, so such
elements count as deleted when their array is; a job over its memory is killed by the cgroup's OOM killer, which
PBS shows only as ``137`` (SIGKILL), so it is reported as a SIGKILL that is
probably a memory kill. ``Exit_status`` may be an integer or a string
(``"-29"``). The other codes are from the documentation and OpenPBS's sources.

Jobs are cancelled with ``qdel JOB...`` (:data:`QSTAT_BATCH` jobs per call;
an array job's ID deletes all its elements), without ``-x``, so the scheduler
keeps the deleted jobs' history. ``qdel`` exits with an error when some job
has already finished or is unknown; its messages about those (``Unknown Job
Id``, ``Job has finished``, as in OpenPBS's sources; not yet recorded on a
real cluster) are ignored, as in ``qstat``'s output.
'''
import difflib
import json
import os
import re
import signal
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
    'SUBSTATE_TERMINATED',
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
    'qdel_command',
    'qstat_command',
    'scheduler_log_dir',
    'unexpected_qdel_errors',
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

#: ``substate`` of a finished job that was deleted with ``qdel`` (OpenPBS's
#: ``JOB_SUBSTATE_TERMINATED``)
SUBSTATE_TERMINATED = 91

#: Jobs per ``qstat`` (or ``qdel``) call
QSTAT_BATCH = 100

#: Seconds before a ``qstat`` (or ``qdel``) call is given up
QSTAT_TIMEOUT = 300

# qstat's errors about jobs it does not list: unknown jobs (e.g. history
# purged), and finished jobs when asked without -x; qdel's about jobs it cannot
# delete for the same reasons
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


def qdel_command(jobs):
    '''The ``qdel`` command line that cancels ``jobs`` (keeping their history)'''
    return ['qdel', *jobs]


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


def _int_attr(attrs, name):
    '''Attribute ``name`` as an int (qstat writes some as strings), or None'''
    value = attrs.get(name)
    if isinstance(value, str) and re.fullmatch(r'\s*-?\d+\s*', value):
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _exit_status(attrs):
    '''``Exit_status`` as an int, or None'''
    return _int_attr(attrs, 'Exit_status')


def _killed_by(status):
    '''The signal that killed a job with (positive) ``Exit_status`` ``status``, or None'''
    if status >= 256:
        return status % 256
    if 128 < status <= 128 + 64:
        return status - 128
    return None


def element_outcome(attrs, limits=None, array_deleted=False):
    '''How a finished plain job or array element ended, from its ``qstat`` attributes

    Parameters
    ----------
    attrs : dict
        Its attributes in ``qstat -f -F json -x``: ``Exit_status``,
        ``substate`` (for deleted jobs), and ``resources_used`` and
        ``Resource_List`` (for wall time kills, and memory use)
    limits : dict, optional
        The array job's ``Resource_List``, for an element that lists none
    array_deleted : bool, optional
        The element's array job was deleted with ``qdel`` (its elements that
        had not started show no sign of it)

    Returns
    -------
    outcome : JobOutcome
    '''
    status = _exit_status(attrs)
    deleted = _int_attr(attrs, 'substate') == SUBSTATE_TERMINATED
    if status is None:
        if deleted or array_deleted:
            return JobOutcome(Outcome.CANCELLED, detail='deleted with qdel before it started')
        return JobOutcome(Outcome.UNKNOWN,
                          detail='no exit status (deleted before it started?)')
    if status == 0:
        return JobOutcome(Outcome.SUCCESS, 0)
    signum = _killed_by(status) if status > 0 else None
    if deleted:
        return JobOutcome(Outcome.CANCELLED, status, signum,
                          f'deleted with qdel (Exit_status {status})')
    if status < 0:
        outcome, what = PBS_EXIT_CODES.get(status, (Outcome.FAILED, 'PBS error'))
        cause = f'{what} (Exit_status {status})'
        if outcome in (Outcome.KILLED_MEMORY, Outcome.KILLED_TIME):
            return JobOutcome(outcome, status, None, cause)
    elif signum is not None:
        outcome, cause = (Outcome.KILLED_SIGNAL,
                          f'killed by {signal_name(signum)} (Exit_status {status})')
    else:
        outcome, cause = Outcome.FAILED, f'exit status {status}'
    used = _resource(attrs, 'resources_used', 'walltime')
    limit = _resource(attrs, 'Resource_List', 'walltime') or (limits or {}).get('walltime')
    used_s, limit_s = _seconds(used), _seconds(limit)
    if used_s is not None and limit_s is not None and used_s >= limit_s:
        return JobOutcome(Outcome.KILLED_TIME, status, signum,
                          f'wall time limit reached (used {used} of {limit}); {cause}')
    if outcome is Outcome.KILLED_SIGNAL:
        used_mem = _resource(attrs, 'resources_used', 'mem')
        mem = _resource(attrs, 'Resource_List', 'mem') or (limits or {}).get('mem')
        if signum == signal.SIGKILL:
            # cgroup OOM kills show only as SIGKILL; memory use is sampled, so
            # the last measurement may be well below the limit
            cause += ', probably for using more memory than requested'
            if used_mem and mem:
                cause += f' (last measured use: {used_mem} of {mem})'
        elif used_mem and mem:
            cause += f'; memory used: {used_mem} of {mem}'
    return JobOutcome(outcome, status, signum, cause)


def array_exit_outcome(attrs):
    '''How a finished array job ended, from its own ``Exit_status`` only

    For when ``qstat`` does not list the array's elements.
    '''
    status = _exit_status(attrs)
    if status == 0:
        return JobOutcome(Outcome.SUCCESS, 0)
    if status == 1:
        return JobOutcome(Outcome.FAILED, 1, None, 'some workers failed (Exit_status 1)')
    if status is None and _int_attr(attrs, 'substate') == SUBSTATE_TERMINATED:
        return JobOutcome(Outcome.CANCELLED, detail='deleted with qdel before it started')
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
            deleted = _int_attr(attrs, 'substate') == SUBSTATE_TERMINATED
            outcomes[job_id] = combine_outcomes(
                {i: element_outcome(a, limits, deleted) for i, a in listed.items()})
        else:
            outcomes[job_id] = array_exit_outcome(attrs)
    return outcomes


def unexpected_qstat_errors(stderr):
    '''Lines of ``qstat``'s stderr other than those about jobs it does not list'''
    return [line for line in stderr.splitlines()
            if line.strip() and not _QSTAT_MISSING_RE.search(line)]


def unexpected_qdel_errors(stderr):
    '''Lines of ``qdel``'s stderr other than those about jobs that have finished
    or are unknown'''
    return unexpected_qstat_errors(stderr)


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

    def _batches(self, jobs, command, unexpected, what):
        '''Run ``command(batch)`` on ``jobs`` in batches; yields each call's stdout

        A call that fails is an error unless its stderr has only messages that
        ``unexpected`` (a function of the stderr) does not return.
        '''
        jobs = list(jobs)
        for start in range(0, len(jobs), QSTAT_BATCH):
            argv = command(jobs[start:start + QSTAT_BATCH])
            try:
                result = self.runner.run(argv, check=False, timeout=QSTAT_TIMEOUT)
            except CommandError as e:
                raise SchedulerError(f'pbs: could not {what}: {e}') from e
            # qstat and qdel exit with an error when some jobs are unknown
            if result.returncode != 0 and (unexpected(result.stderr)
                                           or not result.stderr.strip()):
                error = CommandError(argv, result.returncode, result.stdout, result.stderr)
                raise SchedulerError(f'pbs: could not {what}: {error}')
            yield result.stdout

    def _qstat(self, jobs, elements=False):
        '''Run ``qstat`` on ``jobs`` in batches; yields each call's stdout'''
        return self._batches(jobs, lambda batch: qstat_command(batch, elements),
                             unexpected_qstat_errors, 'query jobs')

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
        for _ in self._batches(jobs, qdel_command, unexpected_qdel_errors, 'cancel jobs'):
            pass
