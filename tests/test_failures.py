import os
from pathlib import Path

from jug_hpc.config.project import ResourceClass
from jug_hpc.failures import TaskFailure, advice, failure_messages, find_failures, parse_log

BIG = ResourceClass(name='big', cpus=1, memory=4 * 1024 ** 3, time=4 * 3600, workers=1,
                    time_fraction=0.8)

# as written by Jug 2.7.0 (paths shortened)
LOG = '''\
some output of an earlier task
CRITICAL:root:Exception while running jugfile.first: no
Traceback (most recent call last):
  File "/venv/bin/jug", line 10, in <module>
    sys.exit(main())
  File "jugfile.py", line 5, in first
    raise ValueError('no')
ValueError: no
'''

CHAINED = '''\
CRITICAL:root:Exception while running jugfile.load: 'x'
CRITICAL:root:Other tasks are dependent on this one! Parallel processors will be held waiting!
Traceback (most recent call last):
  File "jugfile.py", line 5, in load
    d['x']
KeyError: 'x'

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "jugfile.py", line 7, in load
    raise RuntimeError('bad input') from e
RuntimeError: bad input
'''


def test_parse_log():
    assert parse_log(LOG.splitlines(keepends=True)) == [('jugfile.first', 'ValueError: no')]
    # the exception raised last
    assert parse_log(CHAINED.splitlines()) == [('jugfile.load', 'RuntimeError: bad input')]
    assert parse_log([]) == []
    assert parse_log(['Traceback (most recent call last):', 'ValueError: no']) == []


def test_parse_log_without_traceback():
    # e.g., with --keep-going, or a log cut short
    assert parse_log(['CRITICAL:root:Exception while running jugfile.f: oops',
                      'CRITICAL:root:Exception while running jugfile.g: ']) == [
        ('jugfile.f', 'oops'), ('jugfile.g', 'no message')]
    # MemoryError() has no message, but the traceback names it
    assert parse_log(['CRITICAL:root:Exception while running jugfile.f: ',
                      'Traceback (most recent call last):', '  File "x"', 'MemoryError']) == [
        ('jugfile.f', 'MemoryError')]


def write_log(path, text, mtime):
    path.write_text(text)
    os.utime(path, (mtime, mtime))
    return path


def test_find_failures(tmp_path):
    write_log(tmp_path / 'default-1.log', LOG, 1000)
    newest = write_log(tmp_path / 'default-2.log', LOG.replace('ValueError: no', 'OSError: x'),
                       2000)
    write_log(tmp_path / 'load-3.log', CHAINED, 1500)
    write_log(tmp_path / 'default-4.log', 'nothing failed\n', 3000)
    # the submitter's log is not a worker's
    write_log(tmp_path / 'submitter-5.log', LOG, 4000)
    (tmp_path / 'scheduler').mkdir()
    write_log(tmp_path / 'scheduler' / '1.OU', LOG, 5000)

    found = find_failures(tmp_path, ['jugfile.first', 'jugfile.load', 'jugfile.other'])
    assert found == {
        'jugfile.first': TaskFailure('jugfile.first', 'OSError: x', newest, 2),
        'jugfile.load': TaskFailure('jugfile.load', 'RuntimeError: bad input',
                                    tmp_path / 'load-3.log'),
    }
    assert find_failures(tmp_path, []) == {}
    assert find_failures(tmp_path / 'missing', ['jugfile.first']) == {}


def test_advice():
    assert advice('MemoryError', BIG) == 'it ran out of memory: raise memory (now 4G)'
    assert advice('numpy._core._exceptions._ArrayMemoryError: Unable to allocate 8 GiB',
                  BIG) == 'it ran out of memory: raise memory (now 4G)'
    assert advice('MemoryError', None) == 'it ran out of memory: raise memory'
    assert advice("OSError: [Errno 28] No space left on device: 'out'", BIG) == \
        'a disk is full'
    assert advice("OSError: [Errno 122] Disk quota exceeded: 'out'", BIG) == \
        'a disk quota is exceeded'
    assert advice('ValueError: no', BIG) == ''


def test_failure_messages():
    classes = {'big': BIG}
    found = {
        'jugfile.first': TaskFailure('jugfile.first', 'ValueError: no',
                                     Path('/p/logs/default-1.log')),
        'jugfile.align': TaskFailure('jugfile.align', 'MemoryError',
                                     Path('/p/logs/big-2-3.log'), 2),
    }
    failed = {'jugfile.first': 1, 'jugfile.align': 3, 'jugfile.lost': 1}

    def class_of(name):
        return 'big' if name == 'jugfile.align' else 'default'

    assert failure_messages(failed, found, class_of, classes.__getitem__, 'logs') == [
        '[default]: jugfile.first: 1 task failed; logs/default-1.log says: ValueError: no',
        ('[class.big]: jugfile.align: 3 tasks failed; logs/big-2-3.log (the newest of 2 logs '
        'with failures of jugfile.align) says: MemoryError; it ran out of memory: raise '
        'memory (now 4G)'),
        ("[default]: jugfile.lost: 1 task failed, and no worker log in logs/ says why (a task "
        "whose worker was killed while running it is counted as failed after 30 minutes "
        "with the 'file_keepalive' jugdir, and 'jug-hpc status' lists the jobs killed by "
        "the scheduler; or a 'jug execute' outside jug-hpc ran it)"),
        ("once fixed, run 'jug-hpc submit --cleanup-failed' to run the failed tasks again "
         "(or 'jug cleanup --failed-only' first)"),
    ]
    assert failure_messages({}, found, class_of, classes.__getitem__, 'logs') == []
