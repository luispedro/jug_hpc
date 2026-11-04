import os
import shutil
import sys
from pathlib import Path

import jug.task
import pytest

from jug_hpc.config import load_project_settings
from jug_hpc.jugstate import JugfileError, default_jugdir, load_jugfile, load_project

JUGFILES = Path(__file__).parent / 'jugfiles'
JUG_TEST_JUGFILES = Path(jug.__file__).parent / 'tests' / 'jugfiles'


@pytest.fixture
def project(tmp_path):
    '''A copy of tests/jugfiles in a temporary directory'''
    shutil.copytree(JUGFILES, tmp_path, dirs_exist_ok=True)
    (tmp_path / 'input.txt').write_text('a\nb\n')
    return tmp_path


def global_state(jugfile):
    '''What load_jugfile must leave unchanged

    Modules the jugfile imports may stay loaded, but not the jugfile itself.
    '''
    return (os.getcwd(), sys.path[:], sys.argv[:], jug.task.Task.store,
            jug.task.alltasks[:], sys.modules.get(Path(jugfile).stem))


def run_all(state):
    '''Run every task (as `jug execute` would) in the state's store'''
    saved = jug.task.Task.store
    jug.task.Task.store = state.store
    try:
        for t in state.tasks:
            t.run()
    finally:
        jug.task.Task.store = saved


def test_default_jugdir():
    assert default_jugdir('/p/jugfile.py') == '/p/jugfile.jugdata'
    assert default_jugdir(Path('/p/analysis.py')) == '/p/analysis.jugdata'


def test_simple(project):
    state = load_jugfile('simple.py', cwd=project)
    assert state.jugfile == project / 'simple.py'
    assert state.jugdir == str(project / 'simple.jugdata')
    assert not state.hit_barrier
    assert sorted(state.task_names) == sorted(
        ['simple.load'] * 3 + ['simple.align_sample'] * 3 + ['simple.merge'])
    # Loading does not create the jugdir
    assert not (project / 'simple.jugdata').exists()


def test_absolute_jugfile_and_jugdir(project, tmp_path_factory):
    jugdir = tmp_path_factory.mktemp('elsewhere') / 'store'
    state = load_jugfile(project / 'simple.py', jugdir)
    assert state.jugfile == project / 'simple.py'
    assert state.jugdir == str(jugdir)
    assert len(state.tasks) == 7


def test_relative_jugdir_path_is_relative_to_cwd(project):
    state = load_jugfile('simple.py', Path('data'), cwd=project)
    assert state.jugdir == str(project / 'data')


def test_str_jugdir_is_passed_as_is(project):
    state = load_jugfile('simple.py', 'dict_store', cwd=project)
    assert state.jugdir == 'dict_store'
    assert len(state.tasks) == 7


def test_state_is_restored(project):
    jug.task.alltasks.append('sentinel')
    try:
        before = global_state('simple.py')
        load_jugfile('simple.py', cwd=project)
        assert global_state('simple.py') == before
        before = global_state('failing.py')
        with pytest.raises(JugfileError):
            load_jugfile('failing.py', cwd=project)
        assert global_state('failing.py') == before
    finally:
        jug.task.alltasks.remove('sentinel')


def test_repeated_loads(project):
    first = load_jugfile('simple.py', cwd=project)
    second = load_jugfile('simple.py', cwd=project)
    assert first.task_names == second.task_names
    assert not set(map(id, first.tasks)) & set(map(id, second.tasks))


@pytest.mark.parametrize('jugfile', ['barrier.py', 'bvalue.py'])
def test_barrier(project, jugfile):
    name = jugfile[:-len('.py')]
    state = load_jugfile(jugfile, cwd=project)
    assert state.hit_barrier
    assert state.task_names == (f'{name}.split',)

    run_all(state)
    state = load_jugfile(jugfile, cwd=project)
    assert not state.hit_barrier
    n = 3 if name == 'barrier' else 2
    assert state.task_names == (f'{name}.split',) + (f'{name}.process',) * n


def test_runs_in_cwd(project):
    # Reads input.txt (relative path), imports helper.py from the project dir
    state = load_jugfile('reads_input.py', cwd=project)
    assert state.task_names == ('helper.process',) * 3
    assert state.tasks[0].args == (str(project / 'reads_input.py'),)
    assert [t.args for t in state.tasks[1:]] == [('a',), ('b',)]


def test_error(project):
    with pytest.raises(JugfileError) as e:
        load_jugfile('failing.py', cwd=project)
    msg = str(e.value)
    assert msg.startswith(str(project / 'failing.py'))
    assert 'RuntimeError: broken jugfile' in msg
    assert 'failing.py' in e.value.details
    assert "raise RuntimeError('broken jugfile')" in e.value.details
    # Only the jugfile's frames: not jug_hpc's or Jug's
    assert 'jugstate.py' not in e.value.details
    assert 'jug.py' not in e.value.details


def test_syntax_error(project):
    (project / 'bad.py').write_text('x = (\n')
    with pytest.raises(JugfileError) as e:
        load_jugfile('bad.py', cwd=project)
    assert 'SyntaxError' in str(e.value)
    assert 'bad.py' in e.value.details
    assert 'jugstate.py' not in e.value.details


def test_error_is_not_logged(project, caplog):
    with pytest.raises(JugfileError):
        load_jugfile('failing.py', cwd=project)
    assert 'Could not import' not in caplog.text


def test_system_exit(project):
    (project / 'exits.py').write_text('import sys\nsys.exit(3)\n')
    with pytest.raises(JugfileError, match='SystemExit: 3'):
        load_jugfile('exits.py', cwd=project)


def test_missing(project):
    with pytest.raises(JugfileError, match='jugfile not found'):
        load_jugfile('nope.py', cwd=project)
    (project / 'dir.py').mkdir()
    with pytest.raises(JugfileError, match='jugfile not found'):
        load_jugfile('dir.py', cwd=project)


def test_not_py(project):
    (project / 'jugfile.txt').write_text('')
    with pytest.raises(JugfileError, match="must end in '.py'"):
        load_jugfile('jugfile.txt', cwd=project)


def test_load_project(project):
    (project / 'jug_hpc.toml').write_text(
        'jugfile = "simple.py"\njugdir = "store"\n'
        '[default]\nmemory = "1G"\ntime = "1:00:00"\n')
    state = load_project(load_project_settings(project))
    assert state.jugfile == project / 'simple.py'
    assert state.jugdir == str(project / 'store')
    assert len(state.tasks) == 7


@pytest.mark.parametrize(
    'jugfile', sorted(p.name for p in JUG_TEST_JUGFILES.glob('*.py')
                      if p.name != '__init__.py'))
def test_jug_test_jugfiles(tmp_path, jugfile):
    '''Jug's own test jugfiles load, or fail with a JugfileError'''
    shutil.copytree(JUG_TEST_JUGFILES, tmp_path, dirs_exist_ok=True)
    before = global_state(jugfile)
    try:
        state = load_jugfile(jugfile, cwd=tmp_path)
    except JugfileError:
        pass
    else:
        assert all(isinstance(n, str) for n in state.task_names)
    assert global_state(jugfile) == before
