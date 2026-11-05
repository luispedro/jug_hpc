import shutil
from pathlib import Path

import pytest

from jug_hpc.check import BARRIER_NOTE, check, dead_worker_note
from jug_hpc.config import load_project_settings
from jug_hpc.jugstate import load_project

JUGFILES = Path(__file__).parent / 'jugfiles'

DEFAULT = '[default]\nmemory = "4G"\ntime = "1h"\n'


@pytest.fixture
def project(tmp_path):
    '''A copy of tests/jugfiles; call it with the jugfile and the classes'''
    shutil.copytree(JUGFILES, tmp_path, dirs_exist_ok=True)

    def make(jugfile, classes=''):
        (tmp_path / 'jug_hpc.toml').write_text(
            f'jugfile = "{jugfile}"\n{DEFAULT}{classes}')
        settings = load_project_settings(tmp_path)
        return check(settings, load_project(settings))
    return make


def test_simple(project):
    r = project('simple.py', '[class.align]\nmatch = "simple.align_sample"\n')
    assert r.ok
    assert r.errors == r.warnings == []
    assert len(r.notes) == 1
    assert r.notes[0].startswith("Jug's default jugdir (simple.jugdata) can't tell")
    c = r.classification
    assert c.total('align') == 3
    assert c.total('default') == 4
    assert r.text.splitlines() == [
        'Jugfile: simple.py (7 tasks)',
        '',
        "[class.align]  match = 'simple.align_sample'  (3 tasks)",
        '  3  simple.align_sample',
        '',
        '[default]  (4 tasks)',
        '  3  simple.load',
        '  1  simple.merge',
    ]


def test_no_classes(project):
    r = project('simple.py')
    assert r.ok
    assert r.classification.total('default') == 7
    assert r.text.splitlines()[2] == '[default]  (7 tasks)'


def test_overlap_is_an_error(project):
    r = project('simple.py',
                '[class.align]\nmatch = "align_sample"\n'
                '[class.all]\npattern = "/simple\\\\.(align|merge)/"\n')
    assert not r.ok
    assert len(r.errors) == 1
    assert "task 'simple.align_sample' is matched by 2 classes" in r.errors[0]
    assert '[class.align]' in r.errors[0]
    assert '[class.all]' in r.errors[0]
    # Neither class is unused: [class.all] runs merge, [class.align] overlaps
    assert r.warnings == []
    lines = r.text.splitlines()
    assert "[class.align]  match = 'align_sample'  (no tasks)" in lines
    assert lines[-2:] == [
        'Matched by several classes (error)  (3 tasks)',
        '  3  simple.align_sample  <- [class.align], [class.all]',
    ]


def test_unused_class_is_a_warning(project):
    r = project('simple.py', '[class.big]\nmatch = "simple.nothing"\n')
    assert r.ok
    assert len(r.warnings) == 1
    assert r.warnings[0].endswith(
        "[class.big] (match = 'simple.nothing') matches no task")
    assert "[class.big]  match = 'simple.nothing'  (no tasks)" in r.text


@pytest.mark.parametrize('jugfile', ['barrier.py', 'bvalue.py'])
def test_barrier(project, jugfile):
    r = project(jugfile, '[class.proc]\nmatch = "process"\n')
    assert r.ok
    assert r.notes[0] == BARRIER_NOTE
    assert len(r.notes) == 2
    # process() tasks only appear after the barrier
    assert r.classification.total('default') == 1
    assert len(r.warnings) == 1
    assert 'so far' in r.warnings[0]


def test_jugfile_outside_project(project, tmp_path_factory):
    other = tmp_path_factory.mktemp('other')
    shutil.copy(JUGFILES / 'simple.py', other / 'simple.py')
    r = project(other / 'simple.py')
    assert r.text.splitlines()[0] == f'Jugfile: {other / "simple.py"} (7 tasks)'


def test_count_column_is_aligned(project, tmp_path):
    (tmp_path / 'many.py').write_text(
        'from jug import TaskGenerator\n'
        '@TaskGenerator\ndef f(i):\n    return i\n'
        '@TaskGenerator\ndef g(xs):\n    return xs\n'
        'g([f(i) for i in range(12)])\n')
    r = project('many.py')
    assert r.text.splitlines()[-2:] == ['  12  many.f', '   1  many.g']


@pytest.mark.parametrize('jugdir', ['dict_store', 'file_keepalive:store'])
def test_jugdir_backend(tmp_path, jugdir):
    shutil.copytree(JUGFILES, tmp_path, dirs_exist_ok=True)
    (tmp_path / 'jug_hpc.toml').write_text(
        f'jugfile = "simple.py"\njugdir = "{jugdir}"\n{DEFAULT}')
    settings = load_project_settings(tmp_path)
    state = load_project(settings)
    assert state.jugdir == settings.jugdir
    r = check(settings, state)
    assert r.ok
    assert r.classification.total('default') == 7


@pytest.mark.parametrize('jugdir', ['dict_store', 'dict_store:results.pkl'])
def test_dict_store_is_a_warning(tmp_path, jugdir):
    shutil.copytree(JUGFILES, tmp_path, dirs_exist_ok=True)
    (tmp_path / 'jug_hpc.toml').write_text(
        f'jugfile = "simple.py"\njugdir = "{jugdir}"\n{DEFAULT}')
    settings = load_project_settings(tmp_path)
    r = check(settings, load_project(settings))
    assert r.ok
    assert len(r.warnings) == 1
    assert "'jugdir' is" in r.warnings[0]
    assert 'cannot share' in r.warnings[0]
    assert r.warnings[0].startswith(str(tmp_path / 'jug_hpc.toml'))
    assert r.warnings[0].endswith('(jug-hpc submit will refuse it)')


@pytest.mark.parametrize('jugdir', ['data.jugdata', 'file_keepalive:data'])
def test_shareable_jugdir_no_warning(tmp_path, jugdir):
    shutil.copytree(JUGFILES, tmp_path, dirs_exist_ok=True)
    (tmp_path / 'jug_hpc.toml').write_text(
        f'jugfile = "simple.py"\njugdir = "{jugdir}"\n{DEFAULT}')
    settings = load_project_settings(tmp_path)
    r = check(settings, load_project(settings))
    assert r.warnings == []


def _settings(tmp_path, top=''):
    (tmp_path / 'jug_hpc.toml').write_text(f'jugfile = "sub/pipeline.py"\n{top}{DEFAULT}')
    return load_project_settings(tmp_path)


def test_dead_worker_note_default_jugdir(tmp_path):
    note = dead_worker_note(_settings(tmp_path))
    assert note == (
        "Jug's default jugdir (sub/pipeline.jugdata) can't tell a dead worker's lock "
        'from a running task: a task whose worker the scheduler kills stays locked '
        "until 'jug-hpc status --release-stale' removes the lock (the submitter and "
        "'jug-hpc status' warn about it). With jugdir = "
        '"file_keepalive:sub/pipeline.jugdata" (same storage format) in jug_hpc.toml, '
        'such a lock counts as failed after 30 minutes')


def test_dead_worker_note_directory(tmp_path):
    note = dead_worker_note(_settings(tmp_path, 'jugdir = "data/x.jugdata"\n'))
    assert note.startswith("the jugdir (data/x.jugdata) can't tell")
    assert 'jugdir = "file_keepalive:data/x.jugdata" (same storage format)' in note


def test_dead_worker_note_redis(tmp_path):
    url = 'redis://:secret@host:6379/'
    note = dead_worker_note(_settings(tmp_path, f'jugdir = "{url}"\n'))
    assert note.startswith("the 'redis:' jugdir can't tell")
    assert 'secret' not in note
    assert 'jugdir = "file_keepalive:<directory>" in jug_hpc.toml' in note


@pytest.mark.parametrize('jugdir', ['file_keepalive:data', 'dict_store', 'dict_store:x.pkl'])
def test_no_dead_worker_note(tmp_path, jugdir):
    assert dead_worker_note(_settings(tmp_path, f'jugdir = "{jugdir}"\n')) is None
