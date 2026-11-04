'''Running external commands (``qsub``, ``qstat``, ...)

Scheduler backends never call :mod:`subprocess` themselves: they get a
:class:`Runner` and call :meth:`Runner.run`. jug_hpc uses
:class:`SubprocessRunner`; tests use :class:`FakeRunner`, which records every
command and replies with canned output, so backends can be tested without a
scheduler.
'''
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    'Call',
    'CommandError',
    'Completed',
    'FakeRunner',
    'Runner',
    'SubprocessRunner',
]


@dataclass(frozen=True)
class Completed:
    '''Result of a command that ran

    Attributes
    ----------
    argv : tuple of str
    returncode : int
    stdout : str
    stderr : str
    '''
    argv: tuple
    returncode: int
    stdout: str = ''
    stderr: str = ''


class CommandError(Exception):
    '''A command could not be started, timed out, or exited with an error

    Attributes
    ----------
    argv : tuple of str
    returncode : int or None
        None if the command did not run to completion (not found, not
        executable, timed out)
    stdout : str
    stderr : str
    reason : str
        Short description of what went wrong
    '''
    def __init__(self, argv, returncode, stdout='', stderr='', reason=None):
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        if reason is None:
            reason = f'exit status {returncode}'
        self.reason = reason
        message = f'command failed ({reason}): {shlex.join(self.argv)}'
        if stderr.strip():
            message += '\n' + stderr.rstrip()
        super().__init__(message)


class Runner(ABC):
    '''Runs external commands'''

    @abstractmethod
    def run(self, argv, *, input=None, cwd=None, check=True, timeout=None):
        '''Run ``argv`` and wait for it to finish

        Parameters
        ----------
        argv : sequence of str
        input : str, optional
            Sent to the command's standard input
        cwd : path, optional
            Working directory (default: the current one)
        check : bool, optional
            Raise :class:`CommandError` if the exit status is not 0 (default)
        timeout : float, optional
            Seconds; :class:`CommandError` if the command takes longer

        Returns
        -------
        result : Completed

        Raises
        ------
        CommandError
            If the command cannot be started, times out, or (with ``check``)
            exits with a non-zero status
        '''


class SubprocessRunner(Runner):
    '''Runs commands with :func:`subprocess.run`'''

    def run(self, argv, *, input=None, cwd=None, check=True, timeout=None):
        argv = tuple(str(a) for a in argv)
        try:
            p = subprocess.run(argv, input=input, cwd=cwd, capture_output=True,
                               text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            raise CommandError(argv, None, reason=f'{argv[0]}: command not found') from None
        except PermissionError:
            raise CommandError(argv, None, reason=f'{argv[0]}: permission denied') from None
        except subprocess.TimeoutExpired as e:
            raise CommandError(argv, None, _text(e.stdout), _text(e.stderr),
                               reason=f'timed out after {timeout} seconds') from None
        result = Completed(argv, p.returncode, p.stdout, p.stderr)
        if check and p.returncode != 0:
            raise CommandError(argv, p.returncode, p.stdout, p.stderr)
        return result


def _text(output):
    if output is None:
        return ''
    if isinstance(output, bytes):
        return output.decode(errors='replace')
    return output


@dataclass(frozen=True)
class Call:
    '''A command run through a :class:`FakeRunner`'''
    argv: tuple
    input: str | None = None
    cwd: Path | None = None


@dataclass
class _Rule:
    prefix: tuple
    replies: list = field(default_factory=list)
    handler: object = None
    repeat: bool = True


class FakeRunner(Runner):
    '''Runner for tests: records every command and replies with canned output

    Replies are registered with :meth:`on` for commands starting with a given
    prefix (the most recently registered matching rule wins). A command that
    matches no rule behaves like a missing program: exit status 127.

    Attributes
    ----------
    calls : list of Call
        Every command run, in order
    '''

    def __init__(self):
        self.calls = []
        self._rules = []

    def on(self, *prefix, stdout='', stderr='', returncode=0, handler=None, once=False):
        '''Reply to commands whose argv starts with ``prefix``

        Parameters
        ----------
        *prefix : str
            First elements of the argv (none: every command)
        stdout, stderr : str
        returncode : int
        handler : callable, optional
            Called with the :class:`Call`; returns a :class:`Completed`, a
            ``(returncode, stdout, stderr)`` tuple, or a str (the stdout, exit
            status 0). Replaces ``stdout``/``stderr``/``returncode``.
        once : bool, optional
            Reply only to the first matching command; later ones fall through
            to earlier rules. Several ``once`` rules for the same prefix reply
            in the order they were registered.

        Returns
        -------
        self
        '''
        reply = handler if handler is not None else (returncode, stdout, stderr)
        prefix = tuple(str(p) for p in prefix)
        if once:
            for rule in self._rules:
                if rule.prefix == prefix and not rule.repeat and rule.replies:
                    rule.replies.append(reply)
                    return self
        self._rules.append(_Rule(prefix, [reply], repeat=not once))
        return self

    def run(self, argv, *, input=None, cwd=None, check=True, timeout=None):
        argv = tuple(str(a) for a in argv)
        call = Call(argv, input, Path(cwd) if cwd is not None else None)
        self.calls.append(call)
        result = Completed(argv, 127, '', f'{argv[0]}: command not found (FakeRunner)')
        for rule in reversed(self._rules):
            if argv[:len(rule.prefix)] == rule.prefix and rule.replies:
                reply = rule.replies[0] if rule.repeat else rule.replies.pop(0)
                result = self._reply(call, reply)
                break
        if check and result.returncode != 0:
            raise CommandError(argv, result.returncode, result.stdout, result.stderr)
        return result

    @property
    def argvs(self):
        '''The argv of every command run, in order'''
        return [c.argv for c in self.calls]

    @staticmethod
    def _reply(call, reply):
        if callable(reply):
            reply = reply(call)
        if isinstance(reply, Completed):
            return Completed(call.argv, reply.returncode, reply.stdout, reply.stderr)
        if isinstance(reply, str):
            return Completed(call.argv, 0, reply, '')
        returncode, stdout, stderr = reply
        return Completed(call.argv, returncode, stdout, stderr)
