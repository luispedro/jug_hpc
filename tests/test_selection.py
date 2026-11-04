import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jug.utils import prepare_matcher_from_options, prepare_task_matcher

from jug_hpc.classes import Classifier
from jug_hpc.config import ConfigError
from jug_hpc.config.project import ResourceClass
from jug_hpc.selection import (
    SelectorError,
    exclusion_regex,
    jug_supports_exclude,
    selector_args,
)


def rc(name, match=None, pattern=None):
    return ResourceClass(name=name, cpus=1, memory=1 << 30, time=3600, workers=1,
                         time_fraction=0.8, match=match, pattern=pattern,
                         partition=None, raw={}, origins={})


DEFAULT = rc('default')


def selected_by_fallback(classes, task_name):
    '''What `jug execute --pattern=/<regex>/` selects, with Jug's own matcher'''
    (arg,) = selector_args(DEFAULT, classes, exclude=False)
    assert arg.startswith('--pattern=')
    return bool(prepare_task_matcher(arg[len('--pattern='):])(task_name))


def test_class_selectors():
    assert selector_args(rc('a', match='align*'), []) == ('--name=align*',)
    assert selector_args(rc('a', pattern='/^m\\./'), []) == ('--pattern=/^m\\./',)
    # attached with '=' so a leading '-' is not an option
    assert selector_args(rc('a', match='-x'), []) == ('--name=-x',)


def test_default_alone_runs_everything():
    assert selector_args(DEFAULT, []) == ()
    assert selector_args(DEFAULT, [DEFAULT]) == ()


def test_default_with_exclude():
    classes = [rc('a', match='align'), rc('b', pattern='/mer/'), DEFAULT, rc('c', match='x')]
    assert selector_args(DEFAULT, classes, exclude=True) == (
        '--exclude-name=align', '--exclude-name=x', '--exclude-pattern=/mer/')


def test_jug_supports_exclude_detection(monkeypatch):
    import jug.utils

    def new(name=None, pattern=None, exclude_names=None, exclude_patterns=None):
        pass

    def old(name=None, pattern=None):
        pass

    monkeypatch.setattr(jug.utils, 'prepare_matcher_from_options', new)
    assert jug_supports_exclude()
    assert selector_args(DEFAULT, [rc('a', match='a')]) == ('--exclude-name=a',)
    monkeypatch.setattr(jug.utils, 'prepare_matcher_from_options', old)
    assert not jug_supports_exclude()
    assert selector_args(DEFAULT, [rc('a', match='a')])[0].startswith('--pattern=/')


@pytest.mark.parametrize('task_name, selected', [
    ('m.align', False),
    ('pkg.m.align', False),
    ('m.align_other', True),      # --name is strict
    ('m.realign', True),
    ('m.merge', False),           # pattern: anywhere
    ('m.premerge2', False),
    ('m.run_x', False),           # literal pattern
    ('m.run', True),
    ('m.split', False),           # anchored regex
    ('mx.split', True),
    ('other.thing', True),
])
def test_fallback_examples(task_name, selected):
    classes = [rc('a', match='align'), rc('b', pattern='merge'),
               rc('c', pattern='run_x'), rc('d', pattern='/^m\\.sp/')]
    assert selected_by_fallback(classes, task_name) == selected


def test_fallback_is_anchored_and_compiles():
    regex = exclusion_regex([rc('a', match='a*'), rc('b', pattern='.+')])
    assert regex.startswith('^(?!')
    re.compile(regex)


def test_fallback_global_flags_error():
    with pytest.raises(SelectorError, match='exclude-name') as e:
        exclusion_regex([rc('a', pattern='/(?i)align/')], path='p/jug_hpc.toml')
    assert str(e.value).startswith('p/jug_hpc.toml: ')
    assert isinstance(e.value, ConfigError)


def test_fallback_backreference_detected():
    classes = [rc('a', pattern='/(x)y/'), rc('b', pattern='/(a)\\1/')]
    # \1 refers to the first class's group once combined: 'm.aa' is wrongly selected
    with pytest.raises(SelectorError, match="task 'm.aa'"):
        selector_args(DEFAULT, classes, exclude=False, task_names=['m.b', 'm.aa'])
    # without task names, nothing is checked
    selector_args(DEFAULT, classes, exclude=False)


def test_fallback_checked_names_ok():
    classes = [rc('a', match='align'), rc('b', pattern='merge')]
    assert selector_args(DEFAULT, classes, exclude=False,
                         task_names=['m.align', 'm.merge', 'm.x', 'm.x'])


# Random selectors and task names built from a small alphabet, so that
# matches are frequent
_parts = st.text(alphabet='ab_', min_size=1, max_size=3)
_names = st.lists(_parts, min_size=1, max_size=4).map('.'.join)
_globs = st.text(alphabet='ab_.*?', min_size=1, max_size=5)
_literals = st.text(alphabet='ab_.^$()[]+', min_size=1, max_size=4)
_regexes = st.sampled_from(['a', '^a', 'b$', 'a.b', 'a|b_', '^[ab]+\\.b', '(ab)+', 'a_?b'])
_classes = st.lists(
    st.one_of(
        _globs.map(lambda g: ('match', g)),
        _literals.filter(lambda s: not (s.startswith('/') and s.endswith('/')))
                 .map(lambda s: ('pattern', s)),
        _regexes.map(lambda r: ('pattern', f'/{r}/')),
    ),
    min_size=1, max_size=4)


@settings(max_examples=300, deadline=None)
@given(_classes, st.lists(_names, min_size=1, max_size=10))
def test_fallback_agrees_with_classifier(selectors, task_names):
    classes = [rc(f'c{i}', **{kind: value}) for i, (kind, value) in enumerate(selectors)]
    classifier = Classifier(classes)
    for name in task_names:
        assert selected_by_fallback(classes, name) == (not classifier.matching(name))
    # and the check with task names passes
    selector_args(DEFAULT, classes, exclude=False, task_names=task_names)


@settings(max_examples=100, deadline=None)
@given(_classes, st.lists(_names, min_size=1, max_size=10))
def test_exclude_args_agree_with_classifier(selectors, task_names):
    '''The --exclude-* arguments select the same tasks (with a Jug that has them)'''
    import inspect
    if 'exclude_names' not in inspect.signature(prepare_matcher_from_options).parameters:
        pytest.skip('installed Jug has no --exclude-name')
    classes = [rc(f'c{i}', **{kind: value}) for i, (kind, value) in enumerate(selectors)]
    args = selector_args(DEFAULT, classes, exclude=True)
    names = [a.split('=', 1)[1] for a in args if a.startswith('--exclude-name=')]
    patterns = [a.split('=', 1)[1] for a in args if a.startswith('--exclude-pattern=')]
    matcher = prepare_matcher_from_options(exclude_names=names, exclude_patterns=patterns)
    classifier = Classifier(classes)
    for name in task_names:
        assert bool(matcher(name)) == (not classifier.matching(name))
