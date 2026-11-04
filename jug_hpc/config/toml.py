'''Reading TOML files and merging them key by key, keeping track of provenance

A *source map* records, for every leaf value of a (merged) table, the file it
came from. Leaves are all values except tables; arrays are leaves, so an array
in an overriding file replaces the whole array.
'''
import datetime
import math
import tomllib
from pathlib import Path

from .errors import ConfigError

__all__ = [
    'format_key',
    'format_value',
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


_ESCAPES = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\t': '\\t', '\n': '\\n',
            '\f': '\\f', '\r': '\\r'}


def _quote(k):
    '''Write a string as a TOML basic string (escaping what must be)'''
    def escape(c):
        if c in _ESCAPES:
            return _ESCAPES[c]
        if _is_control(c):
            return f'\\u{ord(c):04x}'
        return c
    return '"' + ''.join(map(escape, k)) + '"'


def format_value(v, *, multiline=False):
    '''Format a value (as returned by :mod:`tomllib`) as TOML

    Strings with backslashes (e.g., regular expressions) are written as
    literal strings when they can be, so that they read as written. With
    ``multiline``, a string with newlines is written as a multi-line literal
    string (three single quotes) when it can be, so that shell code stays
    readable; otherwise every value fits on one line.
    '''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return 'nan'
        if math.isinf(v):
            return 'inf' if v > 0 else '-inf'
        return repr(v)
    if isinstance(v, str):
        if multiline and '\n' in v and _literal_ok(v):
            return "'''\n" + v + "'''"
        if '\\' in v and "'" not in v and not any(map(_is_control, v)):
            return "'" + v + "'"
        return _quote(v)
    if isinstance(v, list):
        return '[' + ', '.join(format_value(x) for x in v) + ']'
    if isinstance(v, dict):
        if not v:
            return '{}'
        return '{ ' + ', '.join(f'{format_key((k,))} = {format_value(x)}'
                                for k, x in v.items()) + ' }'
    if isinstance(v, datetime.date | datetime.time):
        return v.isoformat()
    raise TypeError(f'not a TOML value: {v!r}')


def _literal_ok(s):
    # A multi-line literal string cannot hold three single quotes or control characters
    # other than tab and newline, and cannot end in a quote (it would merge
    # with the closing delimiter)
    return ("'''" not in s and not s.endswith("'")
            and all(c in '\t\n' or not _is_control(c) for c in s))


def _is_control(c):
    return c < ' ' or c == '\x7f'


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
