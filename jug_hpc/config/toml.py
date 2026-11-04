'''Reading TOML files and merging them key by key, keeping track of provenance

A *source map* records, for every leaf value of a (merged) table, the file it
came from. Leaves are all values except tables; arrays are leaves, so an array
in an overriding file replaces the whole array.
'''
import tomllib
from pathlib import Path

from .errors import ConfigError

__all__ = [
    'format_key',
    'iter_leaves',
    'merge',
    'read_toml',
    'toml_type',
]


def format_key(key):
    '''Format a key path (a tuple of strings) as a dotted TOML key'''
    return '.'.join(k if _is_bare_key(k) else _quote(k) for k in key)


def _is_bare_key(k):
    return bool(k) and all(c.isascii() and (c.isalnum() or c in '-_') for c in k)


def _quote(k):
    return '"' + k.replace('\\', '\\\\').replace('"', '\\"') + '"'


def read_toml(path):
    '''Read a TOML file

    Parameters
    ----------
    path : Path

    Returns
    -------
    data : dict or None
        None if the file does not exist

    Raises
    ------
    ConfigError
        If the file exists but cannot be read or is not valid TOML
    '''
    path = Path(path)
    try:
        with path.open('rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{path}: invalid TOML: {e}') from e
    except OSError as e:
        raise ConfigError(f'{path}: cannot read file: {e.strerror or e}') from e


def iter_leaves(table, prefix=()):
    '''Yield ``(key, value)`` for every non-table value, depth first, in order

    ``key`` is a tuple of strings.
    '''
    for k, v in table.items():
        key = prefix + (k,)
        if isinstance(v, dict):
            yield from iter_leaves(v, key)
        else:
            yield key, v


def merge(base, base_sources, override, override_path, *, base_description=None):
    '''Merge ``override`` into ``base``, key by key

    Tables are merged recursively; any other value in ``override`` replaces
    the one in ``base``. Neither input is modified.

    Parameters
    ----------
    base : dict
    base_sources : dict
        Source map of ``base``: key tuple -> Path
    override : dict
    override_path : Path
        File ``override`` was read from
    base_description : str, optional
        How to refer to ``base`` in error messages (default: the files in
        ``base_sources``)

    Returns
    -------
    merged : dict
    sources : dict
        Source map of ``merged``

    Raises
    ------
    ConfigError
        If a key is a table on one side and not on the other
    '''
    merged = _copy_tables(base)
    sources = dict(base_sources)
    _merge_into(merged, sources, override, override_path, (), base_description)
    return merged, sources


def _copy_tables(table):
    return {k: _copy_tables(v) if isinstance(v, dict) else v for k, v in table.items()}


def _merge_into(merged, sources, override, override_path, prefix, base_description):
    for k, v in override.items():
        key = prefix + (k,)
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            _merge_into(merged[k], sources, v, override_path, key, base_description)
            continue
        if k in merged and isinstance(merged[k], dict) != isinstance(v, dict):
            if isinstance(merged[k], dict):
                old_files = {p for kk, p in sources.items() if kk[:len(key)] == key}
            else:
                old_files = {sources[key]} if key in sources else set()
            where = base_description or ', '.join(sorted(map(str, old_files))) or 'the base'
            if isinstance(v, dict):
                this, other = 'a table', 'not a table'
            else:
                this, other = 'not a table', 'a table'
            raise ConfigError(
                f'{override_path}: {format_key(key)!r} is {this}, but it is {other} '
                f'in {where}; the two cannot be merged')
        for old in [kk for kk in sources if kk[:len(key)] == key]:
            del sources[old]
        if isinstance(v, dict):
            merged[k] = _copy_tables(v)
            for leaf, _ in iter_leaves(v, key):
                sources[leaf] = override_path
        else:
            merged[k] = v
            sources[key] = override_path


def toml_type(v):
    '''Describe the TOML type of a value, for error messages ("an integer")'''
    return {
        bool: 'a boolean',
        int: 'an integer',
        float: 'a float',
        str: 'a string',
        list: 'an array',
        dict: 'a table',
    }.get(type(v), f'a {type(v).__name__}')
