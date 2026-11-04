'''Local backend: runs workers as processes on this machine

This backend is for laptops, workstations and end-to-end tests: it needs no
cluster, but it runs the same worker script as the other backends. Select it
with ``scheduler = "local"`` in ``cluster.toml``.

The backend has two halves:

- :class:`LocalScheduler`, the backend proper. Like any other backend, it
  renders the script and runs commands through its runner, and its parsing
  functions are pure.
- A tiny scheduler, ``python -m jug_hpc.schedulers.local``, playing the part of
  ``qsub``/``qstat``/``qdel``. Its commands are::

      python -m jug_hpc.schedulers.local submit [--workers N] [--time SECONDS]
             [--output DIR] [--name NAME] [--jobs-dir DIR] SCRIPT
      python -m jug_hpc.schedulers.local status [--jobs-dir DIR] [JOB_ID ...]
      python -m jug_hpc.schedulers.local cancel [--jobs-dir DIR] JOB_ID ...

  ``submit`` prints a job ID (``1``, ``2``, ...) and returns at once. A
  detached supervisor process then runs ``bash SCRIPT`` once per worker, all
  at the same time, waits for them, and records how each one ended.

Each job has a directory ``<jobs dir>/<job id>/`` holding:

- ``info.json``: what was submitted;
- ``lock``: locked by the supervisor while it runs, so a supervisor that died
  (e.g. killed with ``SIGKILL``) is detected even if its PID was reused;
- ``cancel``: created by ``cancel``; the supervisor checks for it;
- ``progress.json``: the workers that have ended, while others still run;
- ``result.json``: written when every worker has ended;
- ``supervisor.log``: the supervisor's own errors.

The jobs directory defaults to ``$XDG_STATE_HOME/jug_hpc/local`` (``XDG_STATE_HOME``
defaults to ``~/.local/state``), like a scheduler's per-user queue.

Each worker's output goes to ``<output>/<name>-<job id>[-<index>].out`` until
the worker script redirects it to its own log (the equivalent of PBS's
``-o``/``-e`` files). The worker gets the submitter's environment plus
``JUG_HPC_LOCAL_JOB_ID`` and, in an array, ``JUG_HPC_LOCAL_ARRAY_INDEX``; the
script falls back to ``$$`` and ``1``, so it can also be run by hand with
``bash SCRIPT``.

Only the wall time is enforced: a worker still running after ``--time``
seconds gets ``SIGTERM`` (to its process group), and ``SIGKILL``
:data:`KILL_GRACE` seconds later. ``cpus`` and ``memory`` only set the
environment variables of the worker script, and ``partition`` and ``account``
are ignored. The backend has no raw options.
'''
import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from ..config.errors import ConfigError
from ..runner import CommandError
from ..script import ScriptVars, render_script
from ..units import format_hms, format_memory
from .base import JobId, JobOutcome, JobState, JobStatus, Outcome, Scheduler, SchedulerError

__all__ = [
    'KILL_GRACE',
    'POLL_INTERVAL',
    'SCRIPT_VARS',
    'LocalScheduler',
    'combine_outcomes',
    'default_jobs_dir',
    'element_outcome',
    'header_lines',
    'main',
    'parse_job_id',
    'parse_status_output',
    'validate_raw_options',
]

#: Seconds between ``SIGTERM`` and ``SIGKILL`` when a worker is stopped
KILL_GRACE = 5

#: Seconds between the supervisor's checks of its workers
POLL_INTERVAL = 0.2

#: The job ID and array index set by the supervisor, with fallbacks for
#: running the script by hand
SCRIPT_VARS = ScriptVars(job_id='${JUG_HPC_LOCAL_JOB_ID:-$$}',
                         array_index='${JUG_HPC_LOCAL_ARRAY_INDEX:-1}')

_MODULE = 'jug_hpc.schedulers.local'

_JOB_ID_RE = re.compile(r'[1-9][0-9]*\Z')

#: How bad each outcome is, for summarizing an array (worst first)
_SEVERITY = [Outcome.KILLED_MEMORY, Outcome.KILLED_TIME, Outcome.KILLED_SIGNAL,
             Outcome.FAILED, Outcome.UNKNOWN, Outcome.SUCCESS]


def default_jobs_dir(environ=None):
    '''``$XDG_STATE_HOME/jug_hpc/local`` (``~/.local/state/jug_hpc/local``)'''
    environ = os.environ if environ is None else environ
    state = environ.get('XDG_STATE_HOME') or os.path.join(
        environ.get('HOME') or os.path.expanduser('~'), '.local', 'state')
    return Path(state) / 'jug_hpc' / 'local'


# --- pure functions -------------------------------------------------------------

def header_lines(spec):
    '''Comment lines describing ``spec`` (the local backend has no directives)'''
    validate_raw_options(spec.raw, f'raw options for class {spec.class_name!r}')
    return [
        f'# jug_hpc local worker: {spec.job_name}',
        (f'# cpus={spec.cpus} memory={format_memory(spec.memory)} '
         f'time={format_hms(spec.time)} workers={spec.workers}'),
        '# (the local backend enforces only the wall time)',
    ]


def validate_raw_options(raw, where):
    '''Check a ``[local]`` raw-options table: the local backend has none

    Raises
    ------
    ConfigError
    '''
    if not isinstance(raw, dict):
        raise ConfigError(f'{where}: must be a table')
    if raw:
        key = next(iter(raw))
        raise ConfigError(f'{where}: {key!r} is not a raw option of the local backend '
                          '(it has none)')


def parse_job_id(stdout):
    '''The job ID printed by ``submit`` (its last non-empty line)

    Raises
    ------
    SchedulerError
    '''
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if lines and _JOB_ID_RE.match(lines[-1]):
        return JobId(lines[-1])
    raise SchedulerError(f'local: no job ID in the output of submit: {stdout.strip()!r}')


def element_outcome(record):
    '''The outcome of one worker, from its record in ``result.json``

    Parameters
    ----------
    record : dict
        ``returncode`` (int, negative for a signal) and ``killed`` (None,
        ``'time'`` or ``'cancelled'``)

    Returns
    -------
    outcome : JobOutcome
    '''
    rc = record.get('returncode')
    killed = record.get('killed')
    signum = -rc if isinstance(rc, int) and rc < 0 else None
    exit_code = rc if isinstance(rc, int) and rc >= 0 else None
    if killed == 'time':
        return JobOutcome(Outcome.KILLED_TIME, exit_code, signum,
                          'killed: wall time limit reached')
    if killed == 'cancelled':
        return JobOutcome(Outcome.KILLED_SIGNAL, exit_code, signum, 'cancelled')
    if not isinstance(rc, int):
        return JobOutcome(Outcome.UNKNOWN, detail='no exit status recorded')
    if rc == 0:
        return JobOutcome(Outcome.SUCCESS, 0)
    if rc < 0:
        return JobOutcome(Outcome.KILLED_SIGNAL, None, signum, f'killed by {_signame(signum)}')
    return JobOutcome(Outcome.FAILED, rc, None, f'exit status {rc}')


def _signame(signum):
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f'signal {signum}'


def combine_outcomes(outcomes):
    '''The outcome of a job from those of its workers: the worst one

    Parameters
    ----------
    outcomes : dict
        Array index (int) -> JobOutcome

    Returns
    -------
    outcome : JobOutcome
        For one worker, its own outcome. For several, the worst one, with a
        detail that lists every worker that did not succeed.
    '''
    if not outcomes:
        return JobOutcome(Outcome.UNKNOWN, detail='no workers recorded')
    if len(outcomes) == 1:
        return next(iter(outcomes.values()))
    order = sorted(outcomes, key=lambda i: (_SEVERITY.index(outcomes[i].outcome), i))
    worst = outcomes[order[0]]
    if worst.outcome is Outcome.SUCCESS:
        return JobOutcome(Outcome.SUCCESS, 0)
    failed = [i for i in sorted(outcomes) if outcomes[i].outcome is not Outcome.SUCCESS]
    detail = '; '.join(f'worker {i}: {outcomes[i].detail or outcomes[i].outcome.value}'
                       for i in failed)
    return JobOutcome(worst.outcome, worst.exit_code, worst.signal,
                      f'{len(failed)} of {len(outcomes)} workers did not succeed ({detail})')


def parse_status_output(stdout):
    '''The state of each job, and the outcome of finished jobs, from ``status``

    Parameters
    ----------
    stdout : str
        JSON: job ID -> ``{"state": ...}``, plus ``"workers"`` and
        ``"finished"`` (counts) for a running job and ``"elements"`` (the
        content of ``result.json``, or null) for a finished one

    Returns
    -------
    jobs : dict
        JobId -> (JobStatus, JobOutcome or None)

    Raises
    ------
    SchedulerError
        If the output is not understood
    '''
    try:
        data = json.loads(stdout)
        jobs = {}
        for job_id, entry in data.items():
            state = JobState(entry['state'])
            outcome = None
            status = JobStatus(state)
            if state is JobState.RUNNING:
                workers, finished = entry['workers'], entry['finished']
                if not (isinstance(workers, int) and isinstance(finished, int)
                        and 0 <= finished < workers):
                    raise ValueError(f'bad worker counts for job {job_id}')
                status = JobStatus(state, running=workers - finished)
            elif state is JobState.FINISHED:
                elements = entry.get('elements')
                if elements is None:
                    outcome = JobOutcome(Outcome.UNKNOWN,
                                         detail='the local supervisor process died')
                else:
                    outcome = combine_outcomes(
                        {int(i): element_outcome(r) for i, r in elements.items()})
            jobs[JobId(job_id)] = (status, outcome)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise SchedulerError(
            f'local: could not parse the output of status ({e}): {stdout.strip()!r}') from e
    return jobs


# --- the backend ----------------------------------------------------------------

class LocalScheduler(Scheduler):
    '''Runs workers as processes on this machine

    Parameters
    ----------
    cluster : ClusterSettings
    runner : Runner, optional
    jobs_dir : path, optional
        Where jobs are recorded (default: :func:`default_jobs_dir`)
    '''

    name = 'local'

    def __init__(self, cluster, runner=None, jobs_dir=None):
        super().__init__(cluster, runner)
        self.jobs_dir = Path(jobs_dir) if jobs_dir is not None else default_jobs_dir()

    def _command(self, *args):
        return [sys.executable, '-m', _MODULE, args[0], f'--jobs-dir={self.jobs_dir}',
                *args[1:]]

    def render_script(self, spec):
        return render_script(spec, header_lines(spec), SCRIPT_VARS)

    def submit_command(self, script, spec):
        return self._command(
            'submit', f'--workers={spec.workers}', f'--time={spec.time}',
            f'--output={spec.scheduler_logs}', f'--name={spec.class_name}',
            '--', str(script))

    def parse_submit_output(self, stdout):
        return parse_job_id(stdout)

    @classmethod
    def validate_raw_options(cls, raw, where):
        validate_raw_options(raw, where)

    def _status(self, jobs):
        jobs = list(jobs)
        if not jobs:
            return {}
        try:
            result = self.runner.run(self._command('status', '--', *jobs))
        except CommandError as e:
            raise SchedulerError(f'local: could not query jobs: {e}') from e
        return parse_status_output(result.stdout)

    def query(self, jobs):
        found = self._status(jobs)
        return {j: found.get(j, (JobStatus(JobState.UNKNOWN), None))[0] for j in jobs}

    def outcome(self, jobs):
        '''How each finished job ended (jobs that have not finished are left out)'''
        return {j: outcome for j, (_, outcome) in self._status(jobs).items()
                if outcome is not None}

    def cancel(self, jobs):
        jobs = list(jobs)
        if not jobs:
            return
        try:
            self.runner.run(self._command('cancel', '--', *jobs))
        except CommandError as e:
            raise SchedulerError(f'local: could not cancel jobs: {e}') from e


# --- the scheduler itself (python -m jug_hpc.schedulers.local) --------------------

def _write_json(path, data):
    '''Write ``data`` to ``path`` atomically'''
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(tmp, path)


def _new_job_dir(jobs_dir):
    '''Create the directory of a new job; returns (job ID, directory)'''
    jobs_dir.mkdir(parents=True, exist_ok=True)
    n = max((int(p.name) for p in jobs_dir.iterdir() if _JOB_ID_RE.match(p.name)), default=0)
    while True:
        n += 1
        path = jobs_dir / str(n)
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return str(n), path


def _lock_is_held(job_dir):
    '''Whether a supervisor holds the job's lock'''
    try:
        fd = os.open(job_dir / 'lock', os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _job_status(jobs_dir, job_id):
    job_dir = jobs_dir / job_id
    result = job_dir / 'result.json'
    if not _JOB_ID_RE.match(job_id) or not (job_dir / 'info.json').exists():
        return {'state': JobState.UNKNOWN.value}
    if not result.exists():
        if _lock_is_held(job_dir):
            workers = json.loads((job_dir / 'info.json').read_text())['workers']
            try:
                finished = json.loads((job_dir / 'progress.json').read_text())['finished']
            except FileNotFoundError:
                finished = []
            # all of them may have finished since the lock was checked
            return {'state': JobState.RUNNING.value, 'workers': workers,
                    'finished': min(len(finished), workers - 1)}
        if not result.exists():   # the supervisor may have finished meanwhile
            return {'state': JobState.FINISHED.value, 'elements': None}
    return {'state': JobState.FINISHED.value,
            'elements': json.loads(result.read_text())['elements']}


def _supervise(job_dir, job_id, script, workers, time_limit, output, name):
    '''Run the workers of a job, wait for them, and write ``result.json``'''
    stop = []
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda *_: stop.append('cancelled'))
    output.mkdir(parents=True, exist_ok=True)
    procs = {}
    for i in range(1, workers + 1):
        env = dict(os.environ, JUG_HPC_LOCAL_JOB_ID=job_id)
        suffix = ''
        if workers > 1:
            env['JUG_HPC_LOCAL_ARRAY_INDEX'] = str(i)
            suffix = f'-{i}'
        with open(output / f'{name}-{job_id}{suffix}.out', 'ab') as out:
            procs[i] = subprocess.Popen(
                ['bash', script], stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    deadline = time.monotonic() + time_limit if time_limit else None
    killed = {}
    kill_at = None
    returncodes = {}
    while True:
        ended = len(returncodes)
        for i, p in procs.items():
            if i not in returncodes and p.poll() is not None:
                returncodes[i] = p.returncode
        running = [i for i in procs if i not in returncodes]
        if not running:
            break
        if len(returncodes) != ended:
            _write_json(job_dir / 'progress.json', {'finished': sorted(returncodes)})
        now = time.monotonic()
        if kill_at is None:
            reason = None
            if stop or (job_dir / 'cancel').exists():
                reason = 'cancelled'
            elif deadline is not None and now >= deadline:
                reason = 'time'
            if reason is not None:
                for i in running:
                    killed[i] = reason
                    _killpg(procs[i].pid, signal.SIGTERM)
                kill_at = now + KILL_GRACE
        elif now >= kill_at:
            for i in running:
                _killpg(procs[i].pid, signal.SIGKILL)
            kill_at = now + KILL_GRACE
        time.sleep(POLL_INTERVAL)
    _write_json(job_dir / 'result.json', {'elements': {
        str(i): {'returncode': returncodes[i], 'killed': killed.get(i)} for i in procs}})


def _killpg(pgid, signum):
    # The group is that of a worker that has not been reaped, so its ID cannot
    # have been reused
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signum)


def _submit(args):
    script = Path(args.script).resolve()
    if not script.is_file():
        raise SystemExit(f'{script}: no such file')
    if args.workers < 1:
        raise SystemExit('--workers must be at least 1')
    output = Path(args.output).resolve() if args.output else None
    name = args.name or script.stem
    job_id, job_dir = _new_job_dir(args.jobs_dir)
    output = output or job_dir
    _write_json(job_dir / 'info.json', {
        'script': str(script), 'workers': args.workers, 'time': args.time,
        'output': str(output), 'name': name, 'cwd': os.getcwd(),
        'submitted': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    (job_dir / 'lock').touch()
    sys.stdout.flush()
    sys.stderr.flush()
    ready_r, ready_w = os.pipe()
    pid = os.fork()
    if pid == 0:    # the supervisor
        code = 1
        try:
            os.close(ready_r)
            os.setsid()
            log = os.open(job_dir / 'supervisor.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            lock = os.open(job_dir / 'lock', os.O_RDWR)
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.write(ready_w, b'1')
            os.close(ready_w)
            _supervise(job_dir, job_id, str(script), args.workers, args.time, output, name)
            code = 0
        except BaseException:   # noqa: BLE001 (the forked child must never return)
            traceback.print_exc()
        finally:
            with contextlib.suppress(Exception):
                sys.stdout.flush()
                sys.stderr.flush()
            os._exit(code)
    os.close(ready_w)
    ready = os.read(ready_r, 1)
    os.close(ready_r)
    if not ready:
        raise SystemExit(f'the supervisor of job {job_id} did not start '
                         f'(see {job_dir / "supervisor.log"})')
    print(job_id)
    return 0


def _status_command(args):
    ids = args.job_ids
    if not ids and args.jobs_dir.is_dir():
        ids = sorted((p.name for p in args.jobs_dir.iterdir() if _JOB_ID_RE.match(p.name)),
                     key=int)
    print(json.dumps({j: _job_status(args.jobs_dir, j) for j in ids}, indent=2))
    return 0


def _cancel(args):
    for job_id in args.job_ids:
        job_dir = args.jobs_dir / job_id
        if _JOB_ID_RE.match(job_id) and (job_dir / 'info.json').exists():
            if not (job_dir / 'result.json').exists():
                (job_dir / 'cancel').touch()
        else:
            print(f'{job_id}: no such job', file=sys.stderr)
    return 0


def main(argv=None):
    '''``python -m jug_hpc.schedulers.local``: submit, status, cancel'''
    parser = argparse.ArgumentParser(
        prog=f'python -m {_MODULE}', description='The scheduler of the local backend')
    commands = parser.add_subparsers(dest='command', required=True)
    sub = commands.add_parser('submit', help='run SCRIPT in the background; print a job ID')
    sub.add_argument('--workers', type=int, default=1, help='copies to run (default: 1)')
    sub.add_argument('--time', type=int, default=0,
                     help='wall time in seconds (default: 0, no limit)')
    sub.add_argument('--output', help="directory for the workers' output "
                                      '(default: the job directory)')
    sub.add_argument('--name', help='prefix of output file names (default: the script name)')
    sub.add_argument('script')
    sub.set_defaults(run=_submit)
    sub = commands.add_parser('status', help='print the state of jobs as JSON')
    sub.add_argument('job_ids', nargs='*', help='(default: every job)')
    sub.set_defaults(run=_status_command)
    sub = commands.add_parser('cancel', help='stop jobs')
    sub.add_argument('job_ids', nargs='+')
    sub.set_defaults(run=_cancel)
    for sub in commands.choices.values():
        sub.add_argument('--jobs-dir', type=Path, default=default_jobs_dir(),
                         help='where jobs are recorded (default: %(default)s)')
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == '__main__':
    sys.exit(main())
