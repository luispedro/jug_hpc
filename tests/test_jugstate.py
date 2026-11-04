import os
import shutil
import sys
from pathlib import Path

import jug.task
import pytest

from jug_hpc.config import load_project_settings
from jug_hpc.jugstate import (
    JugfileError,
    TaskCounts,
    default_jugdir,
    load_jugfile,
    load_project,
    task_counts,
)

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
    '''Jug's own test jugfiles load (and can be counted), or fail with a JugfileError'''
    shutil.copytree(JUG_TEST_JUGFILES, tmp_path, dirs_exist_ok=True)
    before = global_state(jugfile)
    try:
        state = load_jugfile(jugfile, cwd=tmp_path)
    except JugfileError:
        pass
    else:
        assert all(isinstance(n, str) for n in state.task_names)
        counts = task_counts(state)
        assert sum(c.total for c in counts.values()) == len(state.tasks)
    assert global_state(jugfile) == before


def run(state, t):
    '''Run task ``t`` of ``state`` (as `jug execute` would)'''
    saved = jug.task.Task.store
    jug.task.Task.store = state.store
    try:
        t.run()
    finally:
        jug.task.Task.store = saved


def test_task_counts_total():
    c = TaskCounts(waiting=1, ready=2, running=3, failed=4, finished=5)
    assert c.total == 15
    assert not c.done
    assert c + TaskCounts(ready=1) == TaskCounts(1, 3, 3, 4, 5)
    assert TaskCounts().done
    assert TaskCounts(finished=2).done


def test_task_counts(project):
    state = load_jugfile('simple.py', cwd=project)
    assert task_counts(state) == {
        'simple.load': TaskCounts(ready=3),
        'simple.align_sample': TaskCounts(waiting=3),
        'simple.merge': TaskCounts(waiting=1),
    }
    loads = [t for t in state.tasks if t.name == 'simple.load']
    aligns = [t for t in state.tasks if t.name == 'simple.align_sample']
    run(state, loads[0])
    run(state, loads[1])
    state.store.getlock(loads[2].hash()).get()
    # The store is read at each call: no need to reload the jugfile
    assert task_counts(state) == {
        'simple.load': TaskCounts(running=1, finished=2),
        'simple.align_sample': TaskCounts(waiting=1, ready=2),
        'simple.merge': TaskCounts(waiting=1),
    }
    state.store.getlock(loads[2].hash()).fail()
    run(state, aligns[0])
    assert task_counts(state) == {
        'simple.load': TaskCounts(failed=1, finished=2),
        'simple.align_sample': TaskCounts(waiting=1, ready=1, finished=1),
        'simple.merge': TaskCounts(waiting=1),
    }
    state.store.getlock(loads[2].hash()).release()
    run_all(state)
    counts = task_counts(load_jugfile('simple.py', cwd=project))
    assert counts == {
        'simple.load': TaskCounts(finished=3),
        'simple.align_sample': TaskCounts(finished=3),
        'simple.merge': TaskCounts(finished=1),
    }
    assert all(c.done for c in counts.values())


def test_task_counts_barrier(project):
    state = load_jugfile('barrier.py', cwd=project)
    assert task_counts(state) == {'barrier.split': TaskCounts(ready=1)}
    run_all(state)
    # Until the jugfile is reloaded, the tasks after the barrier are not known
    assert task_counts(state) == {'barrier.split': TaskCounts(finished=1)}
    state = load_jugfile('barrier.py', cwd=project)
    assert task_counts(state) == {
        'barrier.split': TaskCounts(finished=1),
        'barrier.process': TaskCounts(ready=3),
    }


def test_task_counts_tasklets(project):
    (project / 'tasklets.py').write_text(
        'from jug import TaskGenerator\n'
        '@TaskGenerator\n'
        'def pair():\n'
        '    return (1, 2)\n'
        '@TaskGenerator\n'
        'def double(x):\n'
        '    return 2 * x\n'
        'p = pair()\n'
        'double(p[0])\n'
        'double(p[1])\n')
    state = load_jugfile('tasklets.py', cwd=project)
    assert task_counts(state) == {
        'tasklets.pair': TaskCounts(ready=1),
        'tasklets.double': TaskCounts(waiting=2),
    }
    run(state, state.tasks[0])
    assert task_counts(state) == {
        'tasklets.pair': TaskCounts(finished=1),
        'tasklets.double': TaskCounts(ready=2),
    }


def test_dependency_graph(project):
    state = load_jugfile('simple.py', cwd=project)
    assert state.graph_error is None
    assert state.hashes == tuple(t.hash() for t in state.tasks)
    index = {t.hash(): i for i, t in enumerate(state.tasks)}
    assert state.dependencies == tuple(
        tuple(index[d.hash()] for d in t.dependencies()) for t in state.tasks)
    merge = state.task_names.index('simple.merge')
    assert sorted(state.task_names[i] for i in state.dependencies[merge]) == \
        ['simple.align_sample'] * 3


def test_dependency_outside_jugfile(project):
    # A dependency that is not in Jug's task list (as happens, e.g., when a
    # task's hash changes after another task was built from it)
    (project / 'outside.py').write_text(
        'import jug.task\n'
        'from jug import TaskGenerator\n'
        '@TaskGenerator\n'
        'def f(x):\n'
        '    return x\n'
        'a = f(1)\n'
        'jug.task.alltasks.remove(a)\n'
        'f(a)\n')
    state = load_jugfile('outside.py', cwd=project)
    # The task names are still known (`check` works) ...
    assert state.task_names == ('outside.f',)
    assert 'could not build the dependency graph' in state.graph_error
    assert state.hashes == ()
    # ... but the tasks cannot be counted
    with pytest.raises(JugfileError, match='could not build the dependency graph'):
        task_counts(state)


def test_unhashable_argument(project):
    (project / 'unhashable.py').write_text(
        'import threading\n'
        'from jug import TaskGenerator\n'
        '@TaskGenerator\n'
        'def f(x):\n'
        '    return 1\n'
        'f(threading.Lock())\n')
    state = load_jugfile('unhashable.py', cwd=project)
    assert state.graph_error.startswith(str(project / 'unhashable.py'))
    assert "could not compute the hash of task 'unhashable.f'" in state.graph_error
    with pytest.raises(JugfileError, match='could not compute the hash'):
        task_counts(state)



def test_tasks_from_imported_module(project):
    (project / 'tasksmod.py').write_text(
        'from jug import TaskGenerator\n'
        '@TaskGenerator\n'
        'def base():\n'
        '    return 1\n'
        'b = base()\n')
    (project / 'usesmod.py').write_text(
        'from jug import TaskGenerator\n'
        'from tasksmod import b\n'
        '@TaskGenerator\n'
        'def use(x):\n'
        '    return x\n'
        'use(b)\n')
    try:
        state = load_jugfile('usesmod.py', cwd=project)
        assert task_counts(state) == {
            'tasksmod.base': TaskCounts(ready=1),
            'usesmod.use': TaskCounts(waiting=1),
        }
        # Known limitation of loading in-process: `tasksmod` stays imported,
        # so its tasks are not created again on the next load (this is why
        # the submitter loads the jugfile in a subprocess)
        state = load_jugfile('usesmod.py', cwd=project)
        assert state.task_names == ('usesmod.use',)
        assert 'could not build the dependency graph' in state.graph_error
    finally:
        sys.modules.pop('tasksmod', None)
