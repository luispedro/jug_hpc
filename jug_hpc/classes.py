'''Assigning tasks to resource classes

Each class (other than the default) selects tasks by name, with ``match``
(the rules of ``jug execute --name``) or ``pattern`` (the rules of ``jug
execute --pattern``). Matching uses Jug's own matchers, so a class selects
exactly the tasks that its workers will run.

A task name matched by no class belongs to the default class. A task name
matched by two or more classes is an error, which is why the order of the
classes does not matter.

Everything here works on task *names* (``module.function``): it does not load
a jugfile, so it can be tested, and reused, without Jug's task machinery.
'''
from collections import Counter
from dataclasses import dataclass, field

from jug.utils import prepare_name_matcher, prepare_task_matcher

from .config.errors import ConfigError
from .config.project import DEFAULT_CLASS

__all__ = [
    'ClassOverlapError',
    'Classification',
    'Classifier',
    'classify',
    'selector',
]


def selector(rc):
    '''The selector of class ``rc``, as written in ``jug_hpc.toml``

    Returns
    -------
    selector : str
        E.g. ``match = "align_*"``; ``''`` for the default class
    '''
    if rc.match is not None:
        return f'match = {rc.match!r}'
    if rc.pattern is not None:
        return f'pattern = {rc.pattern!r}'
    return ''


class ClassOverlapError(ConfigError):
    '''A task name is matched by more than one class

    Attributes
    ----------
    task_name : str
    classes : tuple of str
        Names of the classes matching it, in the order of the file
    '''
    def __init__(self, task_name, classes, message):
        super().__init__(message)
        self.task_name = task_name
        self.classes = tuple(classes)


class Classifier:
    '''Maps task names to class names

    Parameters
    ----------
    classes : iterable of ResourceClass
        The classes that select tasks (a default class among them is
        skipped: it has no selector)
    path : Path, optional
        The ``jug_hpc.toml`` file, named in error messages
    '''
    def __init__(self, classes, path=None):
        self.classes = [rc for rc in classes if not rc.is_default]
        self.path = path
        self._matchers = []
        for rc in self.classes:
            if rc.match is not None:
                m = prepare_name_matcher(rc.match)
            elif rc.pattern is not None:
                m = prepare_task_matcher(rc.pattern)
            else:
                raise ValueError(f'class {rc.name!r} has neither match nor pattern')
            self._matchers.append((rc.name, m))
        self._cache = {}

    @classmethod
    def from_project(cls, settings):
        '''A classifier for the classes of ``settings`` (ProjectSettings)'''
        return cls(settings.classes.values(), path=settings.path)

    def matching(self, task_name):
        '''Names of every class matching ``task_name``, in the order of the file

        Returns
        -------
        names : tuple of str
            Empty if no class matches (the task belongs to the default class)
        '''
        r = self._cache.get(task_name)
        if r is None:
            r = tuple(name for name, m in self._matchers if m(task_name))
            self._cache[task_name] = r
        return r

    def class_of(self, task_name):
        '''Name of the class that runs ``task_name``

        Returns
        -------
        name : str
            ``'default'`` if no class matches

        Raises
        ------
        ClassOverlapError
            If two or more classes match
        '''
        names = self.matching(task_name)
        if not names:
            return DEFAULT_CLASS
        if len(names) > 1:
            raise self.overlap_error(task_name, names)
        return names[0]

    def overlap_error(self, task_name, names):
        '''The ClassOverlapError for ``task_name`` matched by classes ``names``'''
        by_name = {rc.name: rc for rc in self.classes}
        listed = ', '.join(f'[class.{n}] ({selector(by_name[n])})' for n in names)
        prefix = f'{self.path}: ' if self.path is not None else ''
        message = (
            f'{prefix}task {task_name!r} is matched by {len(names)} classes: '
            f'{listed}; each task must belong to one class at most, so make '
            'the selectors more specific')
        return ClassOverlapError(task_name, names, message)


@dataclass(frozen=True)
class Classification:
    '''Every task name assigned to its class

    Attributes
    ----------
    class_names : tuple of str
        Every class, in the order of the file, ``'default'`` last
    by_name : dict
        Task name -> class name, for names matched by at most one class (in
        the order they were first seen)
    counts : dict
        Class name -> Counter (task name -> number of tasks). Every class has
        an entry, possibly empty. Tasks matched by several classes are not
        counted.
    overlaps : dict
        Task name -> names of the classes matching it, for names matched by
        two or more
    errors : list of ClassOverlapError
        One per entry in ``overlaps``
    '''
    class_names: tuple
    by_name: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    overlaps: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    @property
    def ok(self):
        '''True if no task name is matched by two classes'''
        return not self.overlaps

    def total(self, class_name):
        '''Number of tasks in ``class_name``'''
        return self.counts[class_name].total()

    def unused(self):
        '''Classes (not the default) that match no task, in the order of the file

        A class whose only matches overlap with another class is not unused.
        '''
        overlapping = {n for names in self.overlaps.values() for n in names}
        return [n for n in self.class_names
                if n != DEFAULT_CLASS and not self.counts[n] and n not in overlapping]


def classify(classifier, task_names):
    '''Assign every task name to a class, collecting every overlap

    Unlike :meth:`Classifier.class_of`, this does not stop at the first
    overlap, so that all of them can be reported at once.

    Parameters
    ----------
    classifier : Classifier
    task_names : iterable of str
        One entry per task, so names can (and usually do) repeat

    Returns
    -------
    classification : Classification
    '''
    class_names = (*(rc.name for rc in classifier.classes), DEFAULT_CLASS)
    counts = {n: Counter() for n in class_names}
    by_name = {}
    overlaps = {}
    for task_name in task_names:
        names = classifier.matching(task_name)
        if len(names) > 1:
            overlaps.setdefault(task_name, names)
            continue
        c = names[0] if names else DEFAULT_CLASS
        by_name.setdefault(task_name, c)
        counts[c][task_name] += 1
    errors = [classifier.overlap_error(t, names) for t, names in overlaps.items()]
    return Classification(class_names, by_name, counts, overlaps, errors)
