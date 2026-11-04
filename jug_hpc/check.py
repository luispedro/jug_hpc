'''``jug-hpc check``: which class runs each task of the jugfile

:func:`check` takes the project settings and the loaded jugfile and returns a
:class:`CheckReport`: the listing for stdout (every class with its tasks,
counted per task name) and the problems found, which the CLI prints on
stderr. It does no I/O, so it is tested without the CLI.
'''
from collections import Counter
from dataclasses import dataclass, field

from .classes import Classifier, classify, selector
from .config.project import jugdir_problem

__all__ = [
    'CheckReport',
    'check',
]

BARRIER_NOTE = (
    'loading stopped at a barrier() or bvalue() whose tasks are not all '
    'done: only the tasks defined before it are listed. More tasks will '
    'appear as they finish, and the classes are applied to them then.')


@dataclass(frozen=True)
class CheckReport:
    '''The result of checking a project

    Attributes
    ----------
    classification : Classification
    text : str
        The listing (for stdout)
    errors : list of str
        One per task name matched by several classes
    warnings : list of str
        One per class that matches no task, and one if workers cannot share
        the jugdir (``jug-hpc submit`` would refuse it)
    notes : list of str
        Informational (e.g. the task list stops at a barrier)
    '''
    classification: object
    text: str
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self):
        '''True if there are no errors (warnings are allowed)'''
        return not self.errors


def check(settings, state):
    '''Classify the tasks of ``state`` with the classes of ``settings``

    Parameters
    ----------
    settings : ProjectSettings
    state : JugState
        The project's jugfile, loaded

    Returns
    -------
    CheckReport
    '''
    classifier = Classifier.from_project(settings)
    names = state.task_names
    c = classify(classifier, names)

    errors = [str(e) for e in c.errors]
    warnings = []
    problem = jugdir_problem(settings)
    if problem is not None:
        warnings.append(f'{problem} (jug-hpc submit will refuse it)')
    for name in c.unused():
        rc = settings.classes[name]
        msg = f'{settings.path}: [class.{name}] ({selector(rc)}) matches no task'
        if state.hit_barrier:
            msg += ' (so far: loading stopped at a barrier)'
        warnings.append(msg)
    notes = [BARRIER_NOTE] if state.hit_barrier else []
    return CheckReport(
        classification=c,
        text=_format(settings, state, c, Counter(names)),
        errors=errors,
        warnings=warnings,
        notes=notes,
    )


def _format(settings, state, c, all_counts):
    jugfile = _relative_path(state.jugfile, settings.project_dir)
    lines = [f'Jugfile: {jugfile} ({_n_tasks(len(state.tasks))})']
    width = len(str(max(all_counts.values(), default=0)))

    def entry(count, text):
        return f'  {count:>{width}}  {text}'

    for name in c.class_names:
        rc = settings.get_class(name)
        header = '[default]' if rc.is_default else f'[class.{name}]  {selector(rc)}'
        lines.append('')
        lines.append(f'{header}  ({_n_tasks(c.total(name))})')
        for task_name, n in c.counts[name].items():
            lines.append(entry(n, task_name))
    if c.overlaps:
        n = sum(all_counts[t] for t in c.overlaps)
        lines.append('')
        lines.append(f'Matched by several classes (error)  ({_n_tasks(n)})')
        for task_name, classes in c.overlaps.items():
            listed = ', '.join(f'[class.{x}]' for x in classes)
            lines.append(entry(all_counts[task_name], f'{task_name}  <- {listed}'))
    return '\n'.join(lines)


def _n_tasks(n):
    if n == 0:
        return 'no tasks'
    return '1 task' if n == 1 else f'{n} tasks'


def _relative_path(path, base):
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
