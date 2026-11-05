'''Per-project record of submitted jobs (the ``.jug_hpc/`` directory)

jug_hpc keeps what it submitted next to ``jug_hpc.toml``::

    .jug_hpc/
      .gitignore      '*', so the directory is never committed
      jobs.json       every job submitted for the project
      jobs.lock       held while jobs.json is updated
      submitter.lock  held by the polling submitter (``jug-hpc submit``) while it runs
      scripts/        every script written, <class>-<n>.sh

Scripts are kept (including those written by ``submit --dry-run``, which are
not recorded as jobs) so that a user can read them and resubmit them by hand.
They are numbered per class (``default-1.sh``, ``default-2.sh``, ...); a
number is never reused while the file exists.

``jobs.json`` is a JSON object::

    {"version": 1,
     "jobs": [{"job_id": "1234.pbs01", "class": "default", "scheduler": "pbs",
               "workers": 4, "script": ".jug_hpc/scripts/default-1.sh",
               "submitted": "2026-11-04T10:00:00+00:00"}, ...]}

in submission order. Script paths are relative to the project directory, so
the project can be moved. The file is replaced atomically, so readers (e.g.
``jug-hpc status`` while the submitter runs) never see a partial file, and
writers take ``jobs.lock`` so that concurrent additions are not lost.

Only one polling submitter may run for a project, or two would each submit
the workers the tasks need: it holds ``submitter.lock``
(:meth:`ProjectState.submitter_lock`), which says where it runs.
'''
import contextlib
import fcntl
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .schedulers.base import JobId

__all__ = [
    'FORMAT_VERSION',
    'STATE_DIR',
    'JobRecord',
    'ProjectState',
    'StateError',
]

#: Name of the state directory, in the project directory
STATE_DIR = '.jug_hpc'

#: Version of the ``jobs.json`` format written by this module
FORMAT_VERSION = 1

_JOBS_FILE = 'jobs.json'
_LOCK_FILE = 'jobs.lock'
_SUBMITTER_LOCK_FILE = 'submitter.lock'
_SCRIPTS_DIR = 'scripts'


class StateError(Exception):
    '''The state directory cannot be read or written

    The message names the file, so it can be shown to the user as is.
    '''


def _now():
    return datetime.now(UTC).replace(microsecond=0)


@dataclass(frozen=True)
class JobRecord:
    '''One submitted job (a single worker or a job array)

    Attributes
    ----------
    job_id : JobId
        As returned by the scheduler backend
    class_name : str
    scheduler : str
        Name of the backend that submitted it
    workers : int
        Number of workers (array elements when more than one)
    script : Path
        The submitted script, relative to the project directory (absolute if
        it is outside of it)
    submitted : datetime
        Time of submission, timezone aware
    '''
    job_id: JobId
    class_name: str
    scheduler: str
    workers: int
    script: Path
    submitted: datetime = field(default_factory=_now)

    def to_json(self):
        '''The record as a JSON-compatible dict'''
        return {
            'job_id': self.job_id,
            'class': self.class_name,
            'scheduler': self.scheduler,
            'workers': self.workers,
            'script': str(self.script),
            'submitted': self.submitted.isoformat(),
        }

    @classmethod
    def from_json(cls, data):
        '''The record from a dict written by :meth:`to_json`

        Raises
        ------
        ValueError
            If ``data`` is not a valid record (the message says why)
        '''
        # ValueError, not TypeError, for bad types too: this is bad file content
        if not isinstance(data, dict):
            raise ValueError('not an object')  # noqa: TRY004
        expected = {'job_id': str, 'class': str, 'scheduler': str, 'workers': int,
                    'script': str, 'submitted': str}
        for key, kind in expected.items():
            value = data.get(key)
            if not isinstance(value, kind) or isinstance(value, bool):
                raise ValueError(f'{key!r} is missing or not a {kind.__name__}')  # noqa: TRY004
        if data['workers'] < 1:
            raise ValueError("'workers' must be positive")
        submitted = datetime.fromisoformat(data['submitted'])
        if submitted.tzinfo is None:
            raise ValueError("'submitted' has no timezone")
        return cls(job_id=JobId(data['job_id']), class_name=data['class'],
                   scheduler=data['scheduler'], workers=data['workers'],
                   script=Path(data['script']), submitted=submitted)


class ProjectState:
    '''The ``.jug_hpc/`` directory of a project

    Nothing is created until something is written.

    Parameters
    ----------
    project_dir : path
        The directory holding ``jug_hpc.toml``
    '''

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir).absolute()
        self.path = self.project_dir / STATE_DIR
        self.jobs_file = self.path / _JOBS_FILE
        self.scripts_dir = self.path / _SCRIPTS_DIR

    def _ensure(self):
        '''Create the directory (and its ``.gitignore``) if needed'''
        try:
            self.scripts_dir.mkdir(parents=True, exist_ok=True)
            gitignore = self.path / '.gitignore'
            if not gitignore.exists():
                gitignore.write_text('# Created by jug_hpc\n*\n')
        except OSError as e:
            raise StateError(f'{self.path}: cannot create the state directory: {e}') from e

    # --- scripts ----------------------------------------------------------------

    def write_script(self, class_name, text):
        '''Save a script for ``class_name`` under a new name

        Parameters
        ----------
        class_name : str
        text : str

        Returns
        -------
        path : Path
            Absolute path of the new script (``.jug_hpc/scripts/<class>-<n>.sh``,
            ``n`` one more than the largest in use for the class)

        Raises
        ------
        StateError
        '''
        self._ensure()
        prefix = f'{class_name}-'
        used = [0]
        for p in self.scripts_dir.glob(f'{prefix}*.sh'):
            n = p.name[len(prefix):-len('.sh')]
            if n.isdigit():
                used.append(int(n))
        n = max(used)
        while True:
            n += 1
            path = self.scripts_dir / f'{prefix}{n}.sh'
            try:
                # O_EXCL: another process writing a script at the same time
                # gets a different number
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
            except FileExistsError:
                continue
            except OSError as e:
                raise StateError(f'{path}: cannot write script: {e}') from e
            with os.fdopen(fd, 'w') as f:
                f.write(text)
            return path

    def script_path(self, record):
        '''Absolute path of the script of ``record`` (a JobRecord)'''
        return self.project_dir / record.script

    # --- jobs -------------------------------------------------------------------

    def jobs(self):
        '''Every recorded job, in submission order (empty if none)

        Raises
        ------
        StateError
            If ``jobs.json`` is unreadable or invalid
        '''
        return self._read()

    def add_job(self, job_id, class_name, scheduler, workers, script, submitted=None):
        '''Record a submitted job

        Parameters
        ----------
        job_id : JobId
        class_name : str
        scheduler : str
        workers : int
        script : path
            The submitted script; stored relative to the project directory if
            it is inside it
        submitted : datetime, optional
            Default: now

        Returns
        -------
        record : JobRecord

        Raises
        ------
        StateError
        '''
        script = Path(script)
        if script.is_absolute() and script.is_relative_to(self.project_dir):
            script = script.relative_to(self.project_dir)
        record = JobRecord(job_id=JobId(job_id), class_name=class_name,
                           scheduler=scheduler, workers=workers, script=script,
                           submitted=submitted if submitted is not None else _now())
        self._ensure()
        with self._locked():
            jobs = self._read()
            jobs.append(record)
            self._write(jobs)
        return record

    # --- the submitter's lock ---------------------------------------------------

    @contextlib.contextmanager
    def submitter_lock(self):
        '''Hold ``.jug_hpc/submitter.lock`` while the polling submitter runs

        The lock is an ``flock``, so it is released when the process ends,
        however it ends. While held, the file says which process holds it
        (``pid@host``).

        Raises
        ------
        StateError
            If another process holds the lock (the message says which), or
            the lock file cannot be opened
        '''
        self._ensure()
        lock = self.path / _SUBMITTER_LOCK_FILE
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise StateError(f'{lock}: cannot open lock file: {e}') from e
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = os.pread(fd, 256, 0).decode(errors='replace').strip()
                holder = f' (process {holder})' if holder else ''
                raise StateError(f'{lock}: another jug-hpc submit is running for this '
                                 f'project{holder}') from None
            except OSError as e:
                raise StateError(f'{lock}: cannot lock: {e}') from e
            os.ftruncate(fd, 0)
            os.pwrite(fd, f'{os.getpid()}@{socket.gethostname()}\n'.encode(), 0)
            try:
                yield
            finally:
                os.ftruncate(fd, 0)
        finally:
            os.close(fd)

    def submitter_lock_holder(self):
        '''Who holds the submitter's lock, without waiting for it

        Nothing is created. The lock is taken for a moment if it is free, so a
        submitter started at that very moment may find it held.

        Returns
        -------
        holder : str or None
            ``pid@host`` as written by :meth:`submitter_lock` (empty if the
            file does not say), or None if no process holds the lock

        Raises
        ------
        StateError
            If the lock file exists but cannot be opened or locked
        '''
        lock = self.path / _SUBMITTER_LOCK_FILE
        try:
            fd = os.open(lock, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StateError(f'{lock}: cannot open lock file: {e}') from e
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return os.pread(fd, 256, 0).decode(errors='replace').strip()
            except OSError as e:
                raise StateError(f'{lock}: cannot lock: {e}') from e
            return None
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _locked(self):
        lock = self.path / _LOCK_FILE
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise StateError(f'{lock}: cannot open lock file: {e}') from e
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        path = self.jobs_file
        try:
            text = path.read_text()
        except FileNotFoundError:
            return []
        except OSError as e:
            raise StateError(f'{path}: cannot read: {e}') from e
        try:
            data = json.loads(text)
        except ValueError as e:
            raise StateError(f'{path}: not valid JSON: {e}') from e
        if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
            raise StateError(f'{path}: not a jug_hpc jobs file')
        version = data.get('version')
        if not isinstance(version, int) or isinstance(version, bool):
            raise StateError(f'{path}: not a jug_hpc jobs file (no version)')
        if version > FORMAT_VERSION:
            raise StateError(f'{path}: written by a newer version of jug_hpc '
                             f'(format {version}, this version reads up to '
                             f'{FORMAT_VERSION})')
        records = []
        for i, entry in enumerate(data['jobs']):
            try:
                records.append(JobRecord.from_json(entry))
            except ValueError as e:
                raise StateError(f'{path}: job {i + 1} is invalid: {e}') from e
        return records

    def _write(self, records):
        path = self.jobs_file
        data = {'version': FORMAT_VERSION, 'jobs': [r.to_json() for r in records]}
        tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
        try:
            tmp.write_text(json.dumps(data, indent=2) + '\n')
            os.replace(tmp, path)
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise StateError(f'{path}: cannot write: {e}') from e
