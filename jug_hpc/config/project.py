'''Project settings: what this project needs (``jug_hpc.toml``)

The file lives in the project directory and has:

- top-level keys: ``jugfile``, ``jugdir``, ``logs`` and ``setup``;
- a ``[default]`` table with the resources of the default class, which runs
  every task that no other class matches;
- ``[class.<name>]`` tables, one per resource class. Each selects tasks with
  exactly one of ``match`` (as ``jug execute --name``) or ``pattern`` (as
  ``jug execute --pattern``) and inherits every unset field from
  ``[default]``;
- an optional ``[submitter]`` table with the resources of the submitter job
  (``jug-hpc submit --as-job``), which inherits nothing from ``[default]``:
  the submitter needs little memory and one CPU, but must run as long as the
  whole run.

Subtables of ``[default]``, of a class (e.g. ``[class.merge.pbs]``) or of
``[submitter]`` hold raw options for the scheduler of the same name. They are passed through to the
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
    'SUBMITTER',
    'SUBMITTER_KEYS',
    'ProjectSettings',
    'ResourceClass',
    'SubmitterSettings',
    'find_project_dir',
    'jugdir_problem',
    'load_project_settings',
    'split_jugdir',
]

PROJECT_FILE = 'jug_hpc.toml'

#: Name of the class that runs every task matched by no other class
DEFAULT_CLASS = 'default'

#: Name of the submitter job (``jug-hpc submit --as-job``), used like a class
#: name in job names, log names, scripts and ``.jug_hpc/jobs.json``, so no
#: class may be called this
SUBMITTER = 'submitter'

#: Scalar keys allowed at the top level of ``jug_hpc.toml``
PROJECT_KEYS = {
    'jugfile': 'the jugfile, relative to the project directory (default: jugfile.py)',
    'jugdir': ("the jugdir, relative to the project directory, or a Jug backend "
               "('redis://host:port/', 'file_keepalive:<dir>'; default: Jug's)"),
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

#: Keys allowed in ``[submitter]``. Tables hold raw scheduler options.
SUBMITTER_KEYS = {
    'cpus': 'CPUs of the submitter job (default: 1)',
    'memory': "memory of the submitter job, e.g. '2G' (default: 2G)",
    'time': ("wall time of the submitter job, e.g. '7d': as long as the whole run; "
             'required by `jug-hpc submit --as-job`'),
    'partition': "partition (PBS: queue) of the submitter job; overrides the cluster's",
}

_SELECTOR_KEYS = ('match', 'pattern')

_BUILTIN_DEFAULTS = {
    'cpus': 1,
    'workers': 1,
    'time_fraction': 0.8,
    'partition': None,
}

_CLASS_NAME_RE = re.compile(r'[A-Za-z0-9_-]+')

#: Jug backends whose jugdir is ``<prefix><path>``: the path is resolved
#: against the project directory, like a plain jugdir
JUGDIR_PATH_BACKENDS = ('file_keepalive:', 'dict_store:')


def split_jugdir(jugdir):
    '''Split a jugdir into the Jug backend prefix and its path

    Mirrors ``jug.backends.select.select``.

    Parameters
    ----------
    jugdir : str or Path

    Returns
    -------
    prefix : str
        ``''`` for a plain directory (Jug's file store), else the backend
        prefix (``'file_keepalive:'``, ``'redis:'``, ...)
    path : str or None
        The path part (the whole of a plain jugdir); None for a backend that
        does not take a path (``redis:`` URLs, ``dict_store``)
    '''
    jugdir = str(jugdir) if isinstance(jugdir, Path) else jugdir
    for prefix in JUGDIR_PATH_BACKENDS:
        if jugdir.startswith(prefix):
            return prefix, jugdir[len(prefix):]
    if jugdir.startswith('redis:'):
        return 'redis:', None
    if jugdir == 'dict_store':
        return 'dict_store', None
    return '', jugdir


def jugdir_problem(settings):
    '''Why concurrent workers cannot share the project's jugdir, if they cannot

    ``dict_store`` and ``dict_store:<file>`` keep the results in the memory of
    each process, so every worker would have its own copy. ``jug-hpc submit``
    refuses such a jugdir and ``jug-hpc check`` warns about it.

    Parameters
    ----------
    settings : ProjectSettings

    Returns
    -------
    str or None
        The problem, naming the file and key; None if the jugdir can be shared
        (also for the default jugdir)
    '''
    if settings.jugdir is None:
        return None
    prefix, _ = split_jugdir(settings.jugdir)
    if prefix.startswith('dict_store'):
        return (
            f"{settings.path}: 'jugdir' is {str(settings.jugdir)!r}, which workers "
            'cannot share (each would keep its own copy of the results in memory); '
            'use a directory, file_keepalive:<directory> or redis:')
    return None


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
class SubmitterSettings:
    '''Resources of the submitter job (``[submitter]``; ``jug-hpc submit --as-job``)

    Attributes
    ----------
    cpus : int
    memory : int
        Bytes
    time : int or None
        Wall time, in seconds; None if not set (``--as-job`` then refuses to
        run)
    partition : str or None
        None to use the cluster's default
    raw : dict
        Raw scheduler options: scheduler name -> table
    origins : dict
        Field name -> key tuple in ``jug_hpc.toml`` where the value was set
        (fields not set are missing)
    '''
    cpus: int = 1
    memory: int = 2 * 1024 ** 3
    time: int | None = None
    partition: str | None = None
    raw: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)

    @property
    def name(self):
        return SUBMITTER

    def raw_options(self, scheduler):
        '''Raw options of the submitter job for ``scheduler``'''
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
    jugdir : Path or str or None
        None for Jug's default; an absolute Path for a plain directory (Jug's
        file store); a str for any other Jug backend, as passed to ``jug
        --jugdir``, with the path of ``file_keepalive:`` and ``dict_store:``
        made absolute (see :func:`split_jugdir`)
    logs : Path
        Absolute
    setup : str
        Empty if unset
    default : ResourceClass
        The default class
    classes : dict
        Class name -> ResourceClass, in the order of the file (without the
        default class)
    submitter : SubmitterSettings
        The submitter job's resources (built-in defaults if there is no
        ``[submitter]`` table)
    data : dict
        The TOML data, as read
    '''
    path: Path
    project_dir: Path
    jugfile: Path
    default: ResourceClass
    jugdir: Path | str | None = None
    logs: Path | None = None
    setup: str = ''
    classes: dict = field(default_factory=dict)
    submitter: SubmitterSettings = field(default_factory=SubmitterSettings)
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


def find_project_dir(start):
    '''The nearest directory holding ``jug_hpc.toml``: ``start`` or a parent

    Parameters
    ----------
    start : path
        Directory to start from (made absolute, but symbolic links are not
        resolved, so that ``..`` follows the path as given)

    Returns
    -------
    project_dir : Path or None
        None if neither ``start`` nor any of its parents has the file
    '''
    start = Path(start).absolute()
    for d in (start, *start.parents):
        if (d / PROJECT_FILE).is_file():
            return d
    return None


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
            if k in ('default', 'class', SUBMITTER):
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
            if name == SUBMITTER:
                raise self.error(
                    key, f'is not allowed: {SUBMITTER!r} is the name of the submitter job '
                         '(its resources are set in [submitter])')
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
            jugdir=self.jugdir(values.get('jugdir'), project_dir),
            logs=resolve('logs', 'logs'),
            setup=values.get('setup', ''),
            default=default,
            classes=classes,
            submitter=self.submitter(data.get(SUBMITTER, {})),
            data=data,
        )

    def jugdir(self, value, project_dir):
        if value is None:
            return None
        prefix, path = split_jugdir(value)
        if not prefix:
            return project_dir / path
        if path is None:
            return value
        path = path.strip()
        if not path:
            raise self.error(('jugdir',), f'needs a path after {prefix!r}')
        return prefix + str(project_dir / path)

    # --- the submitter job ------------------------------------------------

    def submitter(self, table):
        values = {}
        origins = {}
        raw = {}
        for k, v in table.items():
            key = (SUBMITTER, k)
            if isinstance(v, dict):
                if k in SUBMITTER_KEYS:
                    raise self.error(key, 'must be a value, not a table')
                raw[k] = v
                continue
            if k in CLUSTER_ONLY_KEYS:
                raise self.error(key, _cluster_only_message(k))
            if k not in SUBMITTER_KEYS:
                raise self.error(key, f'is not a known key{_suggestion(k, SUBMITTER_KEYS)}')
            values[k] = self.value(key, k, v)
            origins[k] = key
        return SubmitterSettings(raw=raw, origins=origins, **values)

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

