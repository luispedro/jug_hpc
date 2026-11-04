import pytest
from hypothesis import given
from hypothesis import strategies as st

from jug_hpc.classes import (
    Classifier,
    ClassOverlapError,
    classify,
    selector,
)
from jug_hpc.config import ConfigError, ResourceClass, load_project_settings


def rc(name, match=None, pattern=None):
    return ResourceClass(name=name, cpus=1, memory=2**30, time=3600, workers=1,
                         time_fraction=0.8, match=match, pattern=pattern)


DEFAULT = rc('default')

PROJECT = '''
[default]
memory = "4G"
time   = "1:00:00"

[class.align]
match = "align_*"

[class.merge]
match = "merge"

[class.plots]
pattern = "/^plots\\\\./"
'''

TASKS = [
    'jugfile.align_sample', 'jugfile.align_sample', 'jugfile.align_pairs',
    'jugfile.merge', 'jugfile.merge_other', 'plots.figure1', 'plots.figure2',
    'jugfile.load',
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'jug_hpc.toml').write_text(PROJECT)
    return load_project_settings(tmp_path)


def test_class_of_project(project):
    c = Classifier.from_project(project)
    assert [c.class_of(t) for t in TASKS] == [
        'align', 'align', 'align', 'merge', 'default', 'plots', 'plots', 'default']


@pytest.mark.parametrize(('match', 'task_name', 'expected'), [
    # same rules as jug execute --name
    ('func', 'pkg.mod.func', True),
    ('mod.func', 'pkg.mod.func', True),
    ('pkg.mod.func', 'pkg.mod.func', True),
    ('func', 'pkg.mod.func_other', False),
    ('func', 'pkg.mod.other_func', False),
    ('od.func', 'pkg.mod.func', False),
    ('func*', 'mod.func_other', True),
    ('*func', 'mod.other_func', True),
    ('f?nc', 'mod.func', True),
])
def test_match_rules(match, task_name, expected):
    c = Classifier([rc('a', match=match)])
    assert c.class_of(task_name) == ('a' if expected else 'default')


@pytest.mark.parametrize(('pattern', 'task_name', 'expected'), [
    # same rules as jug execute --pattern
    ('func', 'mod.func_other', True),
    ('func', 'mod.other_func', True),
    ('a.b', 'a.b', True),
    ('a.b', 'axb', False),          # plain strings are not regexes
    ('/a.b/', 'axb', True),
    ('/^mod\\./', 'mod.func', True),
    ('/^mod\\./', 'pkg.mod.func', False),
    ('/', 'a/b', True),             # a lone slash is a plain string
])
def test_pattern_rules(pattern, task_name, expected):
    c = Classifier([rc('a', pattern=pattern)])
    assert c.class_of(task_name) == ('a' if expected else 'default')


@given(st.text(alphabet='abc._*?', min_size=1), st.text(alphabet='abc._', min_size=1))
def test_agrees_with_jug(name, task_name):
    from jug.utils import prepare_matcher_from_options

    c = Classifier([rc('a', match=name)])
    jug_matcher = prepare_matcher_from_options(name=name)
    assert (c.class_of(task_name) == 'a') == bool(jug_matcher(task_name))


def test_default_class_is_skipped():
    c = Classifier([rc('a', match='f'), DEFAULT])
    assert [x.name for x in c.classes] == ['a']
    assert c.class_of('mod.g') == 'default'


def test_class_without_selector():
    with pytest.raises(ValueError):
        Classifier([rc('a')])


def test_overlap_error(project):
    c = Classifier([rc('a', match='align_*'), rc('b', pattern='sample'),
                    rc('c', match='x')], path=project.path)
    assert c.matching('mod.align_sample') == ('a', 'b')
    with pytest.raises(ClassOverlapError) as e:
        c.class_of('mod.align_sample')
    assert isinstance(e.value, ConfigError)
    assert e.value.task_name == 'mod.align_sample'
    assert e.value.classes == ('a', 'b')
    msg = str(e.value)
    assert msg.startswith(f'{project.path}: ')
    assert "'mod.align_sample'" in msg
    assert "[class.a] (match = 'align_*')" in msg
    assert "[class.b] (pattern = 'sample')" in msg
    assert '[class.c]' not in msg


def test_overlap_error_without_path():
    c = Classifier([rc('a', match='f'), rc('b', match='mod.f')])
    with pytest.raises(ClassOverlapError) as e:
        c.class_of('mod.f')
    assert str(e.value).startswith("task 'mod.f' is matched by 2 classes")


def test_selector():
    assert selector(rc('a', match='x*')) == "match = 'x*'"
    assert selector(rc('a', pattern='/^x/')) == "pattern = '/^x/'"
    assert selector(DEFAULT) == ''


def test_classify(project):
    r = classify(Classifier.from_project(project), TASKS)
    assert r.ok
    assert r.class_names == ('align', 'merge', 'plots', 'default')
    assert r.by_name == {
        'jugfile.align_sample': 'align',
        'jugfile.align_pairs': 'align',
        'jugfile.merge': 'merge',
        'jugfile.merge_other': 'default',
        'plots.figure1': 'plots',
        'plots.figure2': 'plots',
        'jugfile.load': 'default',
    }
    assert r.counts['align'] == {'jugfile.align_sample': 2, 'jugfile.align_pairs': 1}
    assert r.total('align') == 3
    assert r.total('merge') == 1
    assert r.total('default') == 2
    assert r.unused() == []
    assert r.errors == []


def test_classify_unused():
    c = Classifier([rc('a', match='f'), rc('b', match='g'), rc('c', match='h')])
    r = classify(c, ['m.f', 'm.x'])
    assert r.unused() == ['b', 'c']
    assert r.counts['b'] == {}
    assert r.total('b') == 0


def test_classify_empty():
    c = Classifier([rc('a', match='f')])
    r = classify(c, [])
    assert r.ok
    assert r.total('default') == 0
    assert r.unused() == ['a']


def test_classify_overlaps():
    c = Classifier([rc('a', match='f*'), rc('b', match='*g'), rc('c', match='x'),
                    rc('d', match='nothing')])
    r = classify(c, ['m.fg', 'm.f', 'm.fg', 'm.fxg', 'm.y'])
    assert not r.ok
    assert r.overlaps == {'m.fg': ('a', 'b'), 'm.fxg': ('a', 'b')}
    assert [e.task_name for e in r.errors] == ['m.fg', 'm.fxg']
    # overlapping tasks are not counted anywhere
    assert r.counts['a'] == {'m.f': 1}
    assert r.counts['b'] == {}
    assert r.by_name == {'m.f': 'a', 'm.y': 'default'}
    # b only matches overlapping tasks: reported as an overlap, not as unused
    assert r.unused() == ['c', 'd']


def test_no_classes():
    r = classify(Classifier([]), ['m.f', 'm.f'])
    assert r.class_names == ('default',)
    assert r.counts == {'default': {'m.f': 2}}
