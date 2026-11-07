'''Why tasks failed, from the worker logs

A task that raises an exception is marked failed by Jug (workers run ``jug
execute --keep-failed``) and the worker writes, to its log::

    CRITICAL:root:Exception while running jugfile.first: no
    Traceback (most recent call last):
      ...
    ValueError: no

and exits (workers do not use ``--keep-going``), so its job ends in error.
Jug's failed lock records no job ID (only a PID and a host name), so the log
that says why a task failed is found by its contents: :func:`find_failures`
reads the worker logs (``<logs>/*.log``, not the submitter's) for the names
of the failed tasks and keeps, per name, the newest log (by modification
time) that records a failure of it. Jug's log names the task (``module.function``)
but not its hash, so with several failed tasks of one name, the newest log is
shown (and how many record such failures); a log of an earlier failure that
was since cleaned up and ran again is found too, if no newer log records one.

The error shown is the last line of the traceback (``ValueError: no``; with
chained exceptions, the one raised last), or Jug's message if there is no
traceback. :func:`advice` adds what to change for a few common causes: a
``MemoryError`` (raise the class's ``memory``; jobs killed for memory by the
scheduler are reported from the scheduler's records instead, see
:mod:`jug_hpc.kills`), and a full disk or a disk quota.

A task can be failed with no log saying why: with the ``file_keepalive``
backend, Jug counts the lock of a task whose worker died (e.g., killed by the
scheduler) as failed after 30 minutes; or a ``jug execute --keep-failed``
outside jug-hpc ran it. The message then says so.

:func:`parse_log` and :func:`failure_messages` are pure; :func:`find_failures`
reads the files.
'''
import re
from dataclasses import dataclass
from pathlib import Path

from .config.project import SUBMITTER
from .units import format_memory

__all__ = [
    'TaskFailure',
    'advice',
    'failure_messages',
    'find_failures',
    'parse_log',
]

#: Jug's message when a task raises (``jug.jug.execution_loop``)
_MARKER = 'Exception while running '
_FAILED = re.compile(re.escape(_MARKER) + r'(\S+):\s?(.*)$')
_TRACEBACK = 'Traceback (most recent call last):'


@dataclass(frozen=True)
class TaskFailure:
    '''Why tasks of one name failed, as the newest worker log that says it

    Attributes
    ----------
    name : str
        The task name
    error : str
        E.g. ``ValueError: no``
    log : Path
        The newest worker log that records a failure of a task of this name
    logs : int
        How many worker logs record such failures
    '''
    name: str
    error: str
    log: Path
    logs: int = 1


def parse_log(lines):
    '''The task failures a worker log records

    Parameters
    ----------
    lines : iterable of str

    Returns
    -------
    failures : list of (str, str)
        ``(task name, error)``, in the order of the log; the error is the last
        line of the traceback that follows Jug's message (the exception raised
        last, with chained exceptions), or Jug's message (``str()`` of the
        exception) if no traceback follows
    '''
    failures = []
    in_traceback = False
    for line in lines:
        line = line.rstrip('\r\n')
        if _MARKER in line and (m := _FAILED.search(line)):
            failures.append([m[1], m[2].strip()])
            in_traceback = False
        elif not failures:
            continue
        elif line.startswith(_TRACEBACK):
            in_traceback = True
        elif in_traceback and line and not line[0].isspace():
            failures[-1][1] = line.strip()
            in_traceback = False
    return [(name, error or 'no message') for name, error in failures]


def find_failures(logs_dir, names):
    '''Find the worker logs that say why tasks of ``names`` failed

    Parameters
    ----------
    logs_dir : Path
        The worker logs directory (only its ``*.log`` files are read, not
        those of the submitter job)
    names : collection of str
        The names of the failed tasks

    Returns
    -------
    found : dict
        Task name -> :class:`TaskFailure`, for the names some log records
        a failure of
    '''
    names = set(names)
    if not names:
        return {}
    logs = []
    try:
        for path in Path(logs_dir).glob('*.log'):
            if path.name.startswith(f'{SUBMITTER}-'):
                continue
            try:
                logs.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return {}
    found = {}
    counts = {}
    for _, path in sorted(logs, reverse=True):
        try:
            with open(path, errors='replace') as f:
                failures = parse_log(f)
        except OSError:
            continue
        errors = {}
        for name, error in failures:
            if name in names:
                errors[name] = error
        for name, error in errors.items():
            counts[name] = counts.get(name, 0) + 1
            if name not in found:
                found[name] = TaskFailure(name, error, path)
    return {name: TaskFailure(f.name, f.error, f.log, counts[name])
            for name, f in found.items()}


def advice(error, rc):
    '''What to do about a task that failed with ``error``, for a few common causes

    Parameters
    ----------
    error : str
        As in :class:`TaskFailure`
    rc : ResourceClass or None
        The task's class, as it is set now

    Returns
    -------
    text : str
        Empty if there is nothing to suggest
    '''
    kind = error.split(':', 1)[0]
    if kind.endswith('MemoryError'):     # also numpy's _ArrayMemoryError
        if rc is None or rc.memory is None:
            return 'it ran out of memory: raise memory'
        return f'it ran out of memory: raise memory (now {format_memory(rc.memory)})'
    if '[Errno 28]' in error or 'No space left on device' in error:
        return 'a disk is full'
    if '[Errno 122]' in error or 'Disk quota exceeded' in error:
        return 'a disk quota is exceeded'
    return ''


def failure_messages(failed, found, class_of, get_class, logs):
    '''One message per name of failed tasks, and what to do afterwards

    Parameters
    ----------
    failed : dict
        Task name -> number of failed tasks, for the names that have any
    found : dict
        As returned by :func:`find_failures`
    class_of : callable
        Task name -> class name
    get_class : callable
        Class name -> ResourceClass (raises KeyError for an unknown class)
    logs : str
        The worker logs directory, as shown to the user

    Returns
    -------
    messages : list of str
        Empty if ``failed`` is
    '''
    messages = []
    for name, n in failed.items():
        class_name = class_of(name)
        label = '[default]' if class_name == 'default' else f'[class.{class_name}]'
        tasks = f'{n} task{"s" if n != 1 else ""} failed'
        failure = found.get(name)
        if failure is None:
            messages.append(
                f'{label}: {name}: {tasks}, and no worker log in {logs}/ says why (a task '
                'whose worker was killed while running it is counted as failed after 30 '
                "minutes with the 'file_keepalive' jugdir, and 'jug-hpc status' lists the "
                "jobs killed by the scheduler; or a 'jug execute' outside jug-hpc ran it)")
            continue
        log = f'{logs}/{failure.log.name}'
        if failure.logs > 1:
            log += f' (the newest of {failure.logs} logs with failures of {name})'
        message = f'{label}: {name}: {tasks}; {log} says: {failure.error}'
        try:
            rc = get_class(class_name)
        except KeyError:
            rc = None
        if text := advice(failure.error, rc):
            message += f'; {text}'
        messages.append(message)
    if messages:
        messages.append("once fixed, run 'jug-hpc submit --cleanup-failed' to run the "
                        "failed tasks again (or 'jug cleanup --failed-only' first)")
    return messages
