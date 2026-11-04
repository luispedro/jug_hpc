'''Project settings: what this project needs (``jug_hpc.toml``)

The file lives in the project directory and has:

- top-level keys: ``jugfile``, ``jugdir``, ``logs`` and ``setup``;
- a ``[default]`` table with the resources of the default class, which runs
  every task that no other class matches;
- ``[class.<name>]`` tables, one per resource class. Each selects tasks with
  exactly one of ``match`` (as ``jug execute --name``) or ``pattern`` (as
  ``jug execute --pattern``) and inherits every unset field from
  ``[default]``.

Subtables of ``[default]`` or of a class (e.g. ``[class.merge.pbs]``) hold raw
options for the scheduler of the same name. They are passed through to the
backend unchecked; a class's raw options are merged over those of
``[default]``, key by key.

Cluster settings (``scheduler``, ``account``) are rejected here, so that the
project file stays portable between clusters.
'''
import difflib
import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ..units import UnitError, parse_memory, parse_time
from .cluster import CLUSTER_FILE, CLUSTER_ONLY_KEYS
from .errors import ConfigError
from .toml import format_key, merge, read_toml, toml_type

__all__ = [
    'CLASS_KEYS',
    'DEFAULT_CLASS',
    'PROJECT_FILE',
    'PROJECT_KEYS',
    'ProjectSettings',
    'ResourceClass',
    'load_project_settings',
]

PROJECT_FILE = 'jug_hpc.toml'

#: Name of the class that runs every task matched by no other class
DEFAULT_CLASS = 'default'

#: Scalar keys allowed at the top level of ``jug_hpc.toml``
PROJECT_KEYS = {
    'jugfile': 'the jugfile, relative to the project directory (default: jugfile.py)',
    'jugdir': "the jugdir, relative to the project directory (default: Jug's)",
    'logs': 'directory for worker logs, relative to the project directory (default: logs)',
    'setup': "shell code run by every worker after the cluster's setup",
}

#: Keys allowed in ``[default]`` and ``[class.<name>]`` (``match`` and
#: ``pattern`` only in classes). Tables hold raw scheduler options.
CLASS_KEYS = {
    'match': 'tasks to run, with the rules of `jug execute --name` (classes only)',
    'pattern': 'tasks to run, with the rules of `jug execute --pattern` (classes only)',
    'cpus': 'CPUs per worker (default: 1)',
    'memory': "memory per worker, e.g. '16G'; required",
    'time': "wall time per worker, e.g. '4:00:00' or '4h'; required",
    'workers': 'maximum number of workers at the same time (default: 1)',
    'time_fraction': 'JUG_MAX_SECONDS = time_fraction * time, in (0, 1] (default: 0.8)',
    'partition': "partition (PBS: queue); overrides the cluster's default",
}

_SELECTOR_KEYS = ('match', 'pattern')

_BUILTIN_DEFAULTS = {
    'cpus': 1,
    'workers': 1,
    'time_fraction': 0.8,
    'partition': None,
}

_CLASS_NAME_RE = re.compile(r'[A-Za-z0-9_-]+')


@dataclass(frozen=True)
class ResourceClass:
    '''One resource class, with every field resolved (inherited from
    ``[default]`` or built in, if the class does not set it)

    Attributes
    ----------
    name : str
    match : str or None
        Selector with ``jug execute --name`` rules (None for the default
        class, and when ``pattern`` is used)
    pattern : str or None
        Selector with ``jug execute --pattern`` rules
    cpus : int
    memory : int
        Bytes
    time : int
        Wall time, in seconds
    workers : int
    time_fraction : float
    partition : str or None
        None to use the cluster's default
    raw : dict
        Raw scheduler options: scheduler name -> table (those of
        ``[default]`` merged with the class's own)
    origins : dict
        Field name -> key tuple in ``jug_hpc.toml`` where the value was set,
        or None if it is a built-in default
    '''
    name: str
    cpus: int
    memory: int
    time: int
    workers: int
    time_fraction: float
    match: str | None = None
    pattern: str | None = None
    partition: str | None = None
    raw: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)

    @property
    def is_default(self):
        return self.name == DEFAULT_CLASS

    @property
    def max_seconds(self):
        '''``JUG_MAX_SECONDS`` for a worker: ``floor(time_fraction * time)``

        Computed with the decimal value of ``time_fraction`` as written, so
        that, e.g., ``0.29 * 100`` is 29 and not 28.
        '''
        return math.floor(Fraction(repr(self.time_fraction)) * self.time)

    def raw_options(self, scheduler):
        '''Raw options of this class for ``scheduler``'''
        return self.raw.get(scheduler, {})


@dataclass(frozen=True)
class ProjectSettings:
    '''Validated project settings

    Attributes
    ----------
    path : Path
        The ``jug_hpc.toml`` file
    project_dir : Path
        Absolute path of the directory holding it
    jugfile : Path
        Absolute
    jugdir : Path or None
        Absolute; None for Jug's default
    logs : Path
        Absolute
    setup : str
        Empty if unset
    default : ResourceClass
        The default class
    classes : dict
        Class name -> ResourceClass, in the order of the file (without the
        default class)
    data : dict
        The TOML data, as read
    '''
    path: Path
    project_dir: Path
    jugfile: Path
    default: ResourceClass
    jugdir: Path | None = None
    logs: Path | None = None
    setup: str = ''
    classes: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def all_classes(self):
        '''Every class, the default class last'''
        return [*self.classes.values(), self.default]

    def get_class(self, name):
        '''The class called ``name`` (``'default'`` for the default class)

        Raises
        ------
        KeyError
        '''
        if name == DEFAULT_CLASS:
            return self.default
        return self.classes[name]


def load_project_settings(project_dir):
    '''Read and validate ``jug_hpc.toml`` in ``project_dir``

    Parameters
    ----------
    project_dir : path

    Returns
    -------
    settings : ProjectSettings

    Raises
    ------
    ConfigError
    '''
    project_dir = Path(project_dir).absolute()
    path = project_dir / PROJECT_FILE
    data = read_toml(path)
    if data is None:
        raise ConfigError(
            f'{path}: no project settings found; create a {PROJECT_FILE} with at '
            f'least a [default] table setting memory and time')
    return _Validator(path).project(data, project_dir)


class _Validator:
    def __init__(self, path):
        self.path = path

    def error(self, key, message):
        if key:
            return ConfigError(f'{self.path}: {format_key(key)!r} {message}')
        return ConfigError(f'{self.path}: {message}')

    # --- top level --------------------------------------------------------

    def project(self, data, project_dir):
        values = {}
        for k, v in data.items():
            key = (k,)
            if k in ('default', 'class'):
                if not isinstance(v, dict):
                    raise self.error(key, f'must be a table, not {toml_type(v)}')
                continue
            if k in CLUSTER_ONLY_KEYS:
                raise self.error(key, _cluster_only_message(k))
            if isinstance(v, dict):
                raise self.error(
                    key,
                    f'is not allowed here: raw scheduler options go in {CLUSTER_FILE} '
                    f'(for every job) or in a class, as [class.<name>.{k}]')
            if k == 'partition':
                raise self.error(key, 'is not allowed at the top level: set it in '
                                      '[default] or in a class')
            if k not in PROJECT_KEYS:
                raise self.error(key, f'is not a known key{_suggestion(k, PROJECT_KEYS)}')
            values[k] = self.string(key, v, allow_empty=(k == 'setup'))

        default_table = data.get('default', {})
        classes_table = data.get('class', {})
        default = self.resource_class(DEFAULT_CLASS, ('default',), default_table, None)
        classes = {}
        for name, table in classes_table.items():
            key = ('class', name)
            if not isinstance(table, dict):
                raise self.error(
                    key, f'must be a table ([class.{format_key((name,))}]), '
                         f'not {toml_type(table)}')
            if name == DEFAULT_CLASS:
                raise self.error(key, 'is not allowed: the default class is set in [default]')
            if not _CLASS_NAME_RE.fullmatch(name):
                raise self.error(
                    key, 'has an invalid class name: use only letters, digits, '
                         "'-' and '_' (it is used in job and log file names)")
            classes[name] = self.resource_class(name, key, table, (default_table, default))

        def resolve(k, default):
            v = values.get(k, default)
            return None if v is None else project_dir / v

        return ProjectSettings(
            path=self.path,
            project_dir=project_dir,
            jugfile=resolve('jugfile', 'jugfile.py'),
            jugdir=resolve('jugdir', None),
            logs=resolve('logs', 'logs'),
            setup=values.get('setup', ''),
            default=default,
            classes=classes,
            data=data,
        )

    # --- classes ----------------------------------------------------------

    def resource_class(self, name, prefix, table, parent):
        '''Validate one class table

        ``parent`` is ``(default_table, default_class)`` for a class, None
        for ``[default]`` itself.
        '''
        is_default = parent is None
        values = {}
        origins = {}
        raw = {}
        for k, v in table.items():
            key = prefix + (k,)
            if isinstance(v, dict):
                if k in CLASS_KEYS:
                    raise self.error(key, 'must be a value, not a table')
                raw[k] = v
                continue
            if k in CLUSTER_ONLY_KEYS:
                raise self.error(key, _cluster_only_message(k))
            if k not in CLASS_KEYS:
                raise self.error(key, f'is not a known key{_suggestion(k, CLASS_KEYS)}')
            if is_default and k in _SELECTOR_KEYS:
                raise self.error(
                    key, 'is not allowed in [default]: the default class runs every task '
                         'that no other class matches')
            values[k] = self.value(key, k, v)
            origins[k] = key

        if not is_default:
            selectors = [k for k in _SELECTOR_KEYS if k in values]
            if len(selectors) != 1:
                what = 'both' if selectors else 'neither'
                raise self.error(
                    prefix, f"must set exactly one of 'match' or 'pattern' (it sets {what})")
            default_table, default = parent
            for k in CLASS_KEYS:
                if k not in values and k not in _SELECTOR_KEYS:
                    values[k] = getattr(default, k)
                    origins[k] = default.origins[k]
            raw = self.merge_raw(name, default_table, table)
        else:
            for k, description in CLASS_KEYS.items():
                if k in values or k in _SELECTOR_KEYS:
                    continue
                if k not in _BUILTIN_DEFAULTS:
                    raise self.error(
                        (), f"[default] must set {k!r} ({description}); every class "
                            'inherits it from there')
                values[k] = _BUILTIN_DEFAULTS[k]
                origins[k] = None

        rc = ResourceClass(name=name, raw=raw, origins=origins, **values)
        if rc.max_seconds < 1:
            raise self.error(
                origins['time_fraction'] or origins['time'],
                f'is too small: time_fraction * time must be at least one second '
                f'(it is {rc.time_fraction} * {rc.time}s)')
        return rc

    def merge_raw(self, name, default_table, class_table):
        # Merge with full key paths, so that errors name the class's keys
        def raw_only(t):
            return {k: v for k, v in t.items() if isinstance(v, dict)}

        base = {'class': {name: raw_only(default_table)}}
        override = {'class': {name: raw_only(class_table)}}
        merged, _ = merge(base, {}, override, self.path, base_description='[default]')
        return merged['class'][name]

    def value(self, key, k, v):
        if k in ('match', 'pattern', 'partition'):
            v = self.string(key, v)
            if k == 'pattern':
                self.check_pattern(key, v)
            return v
        if k in ('cpus', 'workers'):
            if isinstance(v, bool) or not isinstance(v, int):
                raise self.error(key, f'must be an integer, not {toml_type(v)} ({v!r})')
            if v < 1:
                raise self.error(key, f'must be at least 1 (it is {v})')
            return v
        if k == 'time_fraction':
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise self.error(key, f'must be a number, not {toml_type(v)} ({v!r})')
            if not 0 < v <= 1:
                raise self.error(key, f'must be larger than 0 and at most 1 (it is {v})')
            return float(v)
        parse = {'memory': parse_memory, 'time': parse_time}[k]
        try:
            return parse(v)
        except UnitError as e:
            raise ConfigError(f'{self.path}: {format_key(key)!r}: {e}') from e

    def string(self, key, v, *, allow_empty=False):
        if not isinstance(v, str):
            raise self.error(key, f'must be a string, not {toml_type(v)} ({v!r})')
        if allow_empty:
            return v
        if not v.strip():
            raise self.error(key, 'must not be empty')
        return v.strip()

    def check_pattern(self, key, pattern):
        # Same rules as jug.utils.prepare_task_matcher; checked here so that
        # errors are reported with the file and key
        if pattern == '//':
            raise self.error(key, 'must not be empty (it would match every task)')
        if len(pattern) >= 2 and pattern.startswith('/') and pattern.endswith('/'):
            try:
                re.compile(pattern[1:-1])
            except re.error as e:
                raise self.error(key, f'is not a valid regular expression: {e}') from e


def _cluster_only_message(k):
    return (f'is a cluster setting and is not allowed in {PROJECT_FILE}: to change it '
            f'for this project, put it in a {CLUSTER_FILE} next to {PROJECT_FILE}')


def _suggestion(key, known):
    close = difflib.get_close_matches(key, known, n=1)
    known = ', '.join(sorted(known))
    if close:
        return f' (did you mean {close[0]!r}?); known keys: {known}'
    return f'; known keys: {known}'

