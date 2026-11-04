'''Cluster settings: how to submit to this cluster (``cluster.toml``)

Two files are read and merged key by key (see GOALS.md):

1. the user file, ``$XDG_CONFIG_HOME/jug_hpc/cluster.toml`` (by default
   ``~/.config/jug_hpc/cluster.toml``);
2. the project file, ``cluster.toml`` next to ``jug_hpc.toml``, which
   overrides the user file.

At least one of them must exist, and the merged result must name a
scheduler: jug_hpc never guesses one.
'''
import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .toml import format_key, iter_leaves, merge, read_toml

__all__ = [
    'CLUSTER_FILE',
    'CLUSTER_KEYS',
    'CLUSTER_ONLY_KEYS',
    'ClusterSettings',
    'load_cluster_settings',
    'user_cluster_path',
]

CLUSTER_FILE = 'cluster.toml'

#: Scalar keys allowed at the top level of ``cluster.toml``, with their meaning.
#: Every table at the top level holds raw options for the scheduler of the same
#: name (e.g. ``[pbs]``), passed through to the backend.
CLUSTER_KEYS = {
    'scheduler': 'scheduler backend to submit with (e.g. "pbs"); required',
    'account': 'account (project/grant) to charge jobs to',
    'partition': 'partition (PBS: queue) used when a class sets none',
    'setup': 'shell code run by every worker before the project setup',
}

#: Keys that belong to the cluster and may not appear in ``jug_hpc.toml``
#: (``partition`` and ``setup`` are allowed there too, with a project meaning).
CLUSTER_ONLY_KEYS = frozenset({'scheduler', 'account'})


@dataclass(frozen=True)
class ClusterSettings:
    '''Merged and validated cluster settings

    Attributes
    ----------
    scheduler : str
    account : str or None
    partition : str or None
    setup : str
        Empty if unset
    raw : dict
        Raw scheduler options: scheduler name -> table, unchecked here (the
        backend checks their shape)
    data : dict
        The merged TOML data, as read
    sources : dict
        Key tuple -> Path of the file each leaf value came from
    files : tuple of Path
        The files that were read, user file first
    '''
    scheduler: str
    account: str | None = None
    partition: str | None = None
    setup: str = ''
    raw: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    files: tuple = ()

    def raw_options(self, scheduler=None):
        '''Raw options for ``scheduler`` (default: the configured one)'''
        return self.raw.get(scheduler or self.scheduler, {})

    def source(self, *key):
        '''File the value at ``key`` came from (None if it is not set)'''
        return self.sources.get(key)

    def leaves(self):
        '''Yield ``(key, value, source)`` for every value that was set'''
        for key, value in iter_leaves(self.data):
            yield key, value, self.sources[key]


def user_cluster_path(environ=None):
    '''Path of the user's ``cluster.toml``, following the XDG base directory spec

    ``$XDG_CONFIG_HOME/jug_hpc/cluster.toml`` if ``XDG_CONFIG_HOME`` is set to
    an absolute path; otherwise ``~/.config/jug_hpc/cluster.toml`` (the spec
    says to ignore empty and relative values).
    '''
    if environ is None:
        environ = os.environ
    config_home = environ.get('XDG_CONFIG_HOME', '')
    if config_home and os.path.isabs(config_home):
        base = Path(config_home)
    else:
        home = environ.get('HOME')
        base = (Path(home) if home else Path.home()) / '.config'
    return base / 'jug_hpc' / CLUSTER_FILE


def load_cluster_settings(project_dir, *, environ=None):
    '''Read, merge and validate the cluster settings for a project

    Parameters
    ----------
    project_dir : path
        Directory holding ``jug_hpc.toml``; its ``cluster.toml``, if any,
        overrides the user file.
    environ : mapping, optional
        Environment used to find the user file (default: ``os.environ``)

    Returns
    -------
    settings : ClusterSettings

    Raises
    ------
    ConfigError
    '''
    user_path = user_cluster_path(environ)
    project_path = Path(project_dir) / CLUSTER_FILE

    data, sources, files = {}, {}, []
    paths = [user_path]
    if not _same_file(user_path, project_path):
        paths.append(project_path)
    for path in paths:
        content = read_toml(path)
        if content is None:
            continue
        data, sources = merge(data, sources, content, path)
        files.append(path)

    if not files:
        raise ConfigError(
            'no cluster settings found: create a cluster.toml naming at least '
            'the scheduler (e.g. scheduler = "pbs"). Looked for:\n'
            f'  {user_path} (user settings; set XDG_CONFIG_HOME to change)\n'
            f'  {project_path} (project settings)')
    return _validate(data, sources, tuple(files))


def _same_file(a, b):
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    except OSError:
        return False


def _validate(data, sources, files):
    def where(key):
        return sources.get(key) or ' and '.join(map(str, files))

    raw = {}
    values = {}
    for k, v in data.items():
        key = (k,)
        if isinstance(v, dict):
            if k in CLUSTER_KEYS:
                raise ConfigError(
                    f'{_table_source(sources, key, files)}: {format_key(key)!r} must be '
                    f'a string, not a table')
            raw[k] = v
            continue
        if k not in CLUSTER_KEYS:
            raise ConfigError(f'{where(key)}: unknown key {format_key(key)!r}'
                              f'{_suggestion(k, CLUSTER_KEYS)}')
        if not isinstance(v, str):
            raise ConfigError(
                f'{where(key)}: {k!r} must be a string, not {_toml_type(v)} ({v!r})')
        if k != 'setup' and not v.strip():
            raise ConfigError(f'{where(key)}: {k!r} must not be empty')
        values[k] = v.strip() if k != 'setup' else v

    if 'scheduler' not in values:
        raise ConfigError(
            f'no scheduler set in {" or ".join(map(str, files))}: add, e.g., '
            f'scheduler = "pbs"')
    return ClusterSettings(raw=raw, data=data, sources=sources, files=files, **values)


def _table_source(sources, key, files):
    found = sorted({str(p) for kk, p in sources.items() if kk[:len(key)] == key})
    return ' and '.join(found or map(str, files))


def _suggestion(key, known):
    close = difflib.get_close_matches(key, known, n=1)
    known = ', '.join(sorted(known))
    if close:
        return f' (did you mean {close[0]!r}?); known keys: {known}'
    return f'; known keys: {known} (tables hold raw scheduler options, e.g. [pbs])'


def _toml_type(v):
    return {
        bool: 'a boolean',
        int: 'an integer',
        float: 'a float',
        list: 'an array',
    }.get(type(v), f'a {type(v).__name__}')
