import sys
from pathlib import Path

import pytest

from jug_hpc.runner import Call, CommandError, Completed, FakeRunner, SubprocessRunner

PY = sys.executable


# --- SubprocessRunner ------------------------------------------------------------

def test_subprocess_ok():
    r = SubprocessRunner().run([PY, '-c', 'print("hi")'])
    assert r.returncode == 0
    assert r.stdout == 'hi\n'
    assert r.argv == (PY, '-c', 'print("hi")')


def test_subprocess_input_and_cwd(tmp_path):
    code = 'import os, sys; print(os.getcwd()); print(sys.stdin.read())'
    r = SubprocessRunner().run([PY, '-c', code], input='data', cwd=tmp_path)
    cwd, data = r.stdout.splitlines()
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert data == 'data'


def test_subprocess_paths_become_str(tmp_path):
    script = tmp_path / 'x.py'
    script.write_text('print(1)')
    r = SubprocessRunner().run([PY, script])
    assert r.argv == (PY, str(script))
    assert r.stdout == '1\n'


def test_subprocess_failure():
    code = 'import sys; print("out"); print("bad thing", file=sys.stderr); sys.exit(3)'
    with pytest.raises(CommandError) as e:
        SubprocessRunner().run([PY, '-c', code])
    assert e.value.returncode == 3
    assert e.value.stdout == 'out\n'
    assert e.value.stderr == 'bad thing\n'
    assert 'exit status 3' in str(e.value)
    assert 'bad thing' in str(e.value)


def test_subprocess_failure_no_check():
    r = SubprocessRunner().run([PY, '-c', 'import sys; sys.exit(2)'], check=False)
    assert r.returncode == 2


def test_subprocess_not_found():
    with pytest.raises(CommandError) as e:
        SubprocessRunner().run(['jug-hpc-no-such-command-xyz', 'a b'])
    assert e.value.returncode is None
    assert 'command not found' in str(e.value)
    assert "'a b'" in str(e.value)  # the command line is quoted


def test_subprocess_timeout():
    with pytest.raises(CommandError) as e:
        SubprocessRunner().run([PY, '-c', 'import time; time.sleep(10)'], timeout=0.2)
    assert e.value.returncode is None
    assert 'timed out' in str(e.value)


# --- FakeRunner ------------------------------------------------------------------

def test_fake_records_calls(tmp_path):
    f = FakeRunner().on('qsub', stdout='1.server\n')
    r = f.run(['qsub', tmp_path / 'x.sh'], input='in', cwd=tmp_path)
    assert r == Completed(('qsub', str(tmp_path / 'x.sh')), 0, '1.server\n', '')
    assert f.calls == [Call(('qsub', str(tmp_path / 'x.sh')), 'in', tmp_path)]
    assert f.argvs == [('qsub', str(tmp_path / 'x.sh'))]


def test_fake_unknown_command():
    f = FakeRunner()
    with pytest.raises(CommandError) as e:
        f.run(['qstat'])
    assert e.value.returncode == 127
    assert f.run(['qstat'], check=False).returncode == 127
    assert len(f.calls) == 2


def test_fake_prefix_and_precedence():
    f = FakeRunner()
    f.on(stdout='anything')
    f.on('qstat', stdout='all')
    f.on('qstat', '-x', stdout='finished')
    assert f.run(['qstat', '-x', '1']).stdout == 'finished'
    assert f.run(['qstat', '-f']).stdout == 'all'
    assert f.run(['qdel', '1']).stdout == 'anything'
    # the most recent matching rule wins, even if it is less specific
    f.on('qstat', stdout='newer')
    assert f.run(['qstat', '-x', '1']).stdout == 'newer'


def test_fake_failure():
    f = FakeRunner().on('qsub', returncode=1, stderr='qsub: Unknown queue')
    with pytest.raises(CommandError) as e:
        f.run(['qsub', 'x.sh'])
    assert e.value.returncode == 1
    assert 'Unknown queue' in str(e.value)


def test_fake_once_in_order():
    f = FakeRunner()
    f.on('qsub', stdout='default')
    f.on('qsub', stdout='1', once=True)
    f.on('qsub', stdout='2', once=True)
    assert [f.run(['qsub']).stdout for _ in range(4)] == ['1', '2', 'default', 'default']


def test_fake_handler():
    def handler(call):
        if call.argv[1] == 'fail':
            return (2, '', 'nope')
        if call.argv[1] == 'completed':
            return Completed((), 0, 'c', 'e')
        return call.input.upper()

    f = FakeRunner().on('cmd', handler=handler)
    assert f.run(['cmd', 'x'], input='abc').stdout == 'ABC'
    assert f.run(['cmd', 'completed']) == Completed(('cmd', 'completed'), 0, 'c', 'e')
    assert f.run(['cmd', 'fail'], check=False) == Completed(('cmd', 'fail'), 2, '', 'nope')
