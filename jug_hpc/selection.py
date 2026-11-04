'''The ``jug execute`` options that select the tasks of one resource class

A class with ``match = 'x'`` runs ``jug execute --name=x``, and one with
``pattern = 'x'`` runs ``jug execute --pattern=x``, so its workers run exactly
the tasks that :mod:`jug_hpc.classes` assigns to it.

The default class must run every task that *no other class* matches. Recent
versions of Jug have ``--exclude-name`` and ``--exclude-pattern`` for this.
With older versions, a single ``--pattern`` regular expression is generated
instead (:func:`exclusion_regex`), which reproduces Jug's matching rules
inside negative lookaheads. That fallback cannot express everything a
``pattern`` may contain (e.g. backreferences or global inline flags), so it
is checked: it must compile and, when task names are given, select the same
tasks as the classifier.
'''
import fnmatch
import inspect
import re

from .config.errors import ConfigError

__all__ = [
    'SelectorError',
    'exclusion_regex',
    'jug_supports_exclude',
    'selector_args',
]


class SelectorError(ConfigError):
    '''The default class's tasks cannot be selected on the command line'''


def jug_supports_exclude():
    '''Whether the installed Jug has ``jug execute --exclude-name``

    This is checked on the Jug that jug_hpc imports, which is assumed to be
    the one the workers run (the usual case: same environment).
    '''
    from jug.utils import prepare_matcher_from_options
    params = inspect.signature(prepare_matcher_from_options).parameters
    return 'exclude_names' in params and 'exclude_patterns' in params


def selector_args(rc, classes, *, exclude=None, task_names=None, path=None):
    '''The ``jug execute`` arguments that select the tasks of class ``rc``

    Values are attached with ``=`` (``--name=x``), so a selector starting with
    ``-`` is not read as an option.

    Parameters
    ----------
    rc : ResourceClass
    classes : iterable of ResourceClass
        Every class of the project (the default class among them is
        skipped); only used for the default class
    exclude : bool, optional
        Use ``--exclude-name``/``--exclude-pattern`` for the default class
        (default: if the installed Jug has them, see
        :func:`jug_supports_exclude`); otherwise a generated ``--pattern``
    task_names : iterable of str, optional
        If given, the generated ``--pattern`` is checked against them
    path : Path, optional
        The ``jug_hpc.toml`` file, named in error messages

    Returns
    -------
    args : tuple of str
        Empty for a default class when there are no other classes (it runs
        every task)

    Raises
    ------
    SelectorError
        If the generated ``--pattern`` does not compile, or selects different
        tasks than the classes do
    '''
    if not rc.is_default:
        if rc.match is not None:
            return (f'--name={rc.match}',)
        if rc.pattern is not None:
            return (f'--pattern={rc.pattern}',)
        raise ValueError(f'class {rc.name!r} has neither match nor pattern')
    others = [c for c in classes if not c.is_default]
    if not others:
        return ()
    if exclude is None:
        exclude = jug_supports_exclude()
    if exclude:
        args = [f'--exclude-name={c.match}' for c in others if c.match is not None]
        args += [f'--exclude-pattern={c.pattern}' for c in others if c.pattern is not None]
        return tuple(args)
    regex = exclusion_regex(others, path=path)
    if task_names is not None:
        _check_exclusion(regex, others, task_names, path)
    return (f'--pattern=/{regex}/',)


def exclusion_regex(classes, *, path=None):
    '''A regular expression found in exactly the task names no class matches

    Used as ``jug execute --pattern=/<regex>/`` (Jug *searches* the name
    for it). Each class becomes one negative lookahead at the start of the
    name:

    - ``match = 'x'``: ``(?s:.*\\.)?(?:<fnmatch.translate(x)>)``, i.e., the
      whole name or a part of it starting after a ``.``, as in
      ``jug.utils.prepare_name_matcher``;
    - ``pattern = '/r/'``: ``(?s:.*?)(?:r)`` (``r`` anywhere);
    - ``pattern = 'x'``: ``(?s:.*?)<re.escape(x)>`` (``x`` anywhere).

    Parameters
    ----------
    classes : iterable of ResourceClass
        Classes other than the default (each with a ``match`` or ``pattern``)
    path : Path, optional
        Named in error messages

    Returns
    -------
    regex : str

    Raises
    ------
    SelectorError
        If the combined expression does not compile
    '''
    parts = []
    for rc in classes:
        if rc.match is not None:
            parts.append(r'(?s:.*\.)?(?:' + fnmatch.translate(rc.match) + ')')
        elif _is_regex(rc.pattern):
            parts.append('(?s:.*?)(?:' + rc.pattern[1:-1] + ')')
        else:
            parts.append('(?s:.*?)' + re.escape(rc.pattern))
    regex = '^' + ''.join(f'(?!{p})' for p in parts)
    try:
        re.compile(regex)
    except re.error as e:
        raise SelectorError(
            f'{_prefix(path)}cannot build a single --pattern selecting the tasks '
            f'of the default class ({e}): a class pattern probably uses global '
            'inline flags or group names that cannot be combined. '
            + _UPGRADE_HINT) from e
    return regex


_UPGRADE_HINT = ('A version of Jug with `jug execute --exclude-name` avoids '
                 'the generated pattern.')


def _is_regex(pattern):
    # the rule of jug.utils.prepare_task_matcher
    return len(pattern) >= 2 and pattern.startswith('/') and pattern.endswith('/')


def _prefix(path):
    return f'{path}: ' if path is not None else ''


def _check_exclusion(regex, classes, task_names, path):
    from .classes import Classifier
    classifier = Classifier(classes)
    compiled = re.compile(regex)
    for name in set(task_names):
        selected = compiled.search(name) is not None
        if selected != (not classifier.matching(name)):
            what = 'selects' if selected else 'does not select'
            raise SelectorError(
                f'{_prefix(path)}the --pattern generated for the default class '
                f'{what} task {name!r}, unlike the class selectors (a class '
                'pattern probably uses backreferences). ' + _UPGRADE_HINT)
