'''Parsing and formatting of memory sizes and wall times

The canonical forms are an integer number of **bytes** for memory and an
integer number of **seconds** for time. Configuration values are parsed into
these forms as early as possible, and scheduler backends render them in their
own syntax (helpers for the common formats are provided here).

Memory units are binary (``1K = 1024 bytes``), as in both SLURM and PBS.
'''
import re
from fractions import Fraction

__all__ = [
    'UnitError',
    'format_dhms',
    'format_hms',
    'format_memory',
    'memory_in_unit',
    'parse_memory',
    'parse_time',
]


class UnitError(ValueError):
    '''A memory or time value could not be parsed'''


_MEMORY_UNITS = {
    '': 1,
    'K': 1024,
    'M': 1024**2,
    'G': 1024**3,
    'T': 1024**4,
    'P': 1024**5,
}

_MEMORY_RE = re.compile(
    r'''^\s*
        (?P<number>\d+(?:\.\d*)?|\.\d+)   # 16, 1.5, .5
        \s*
        (?:(?P<unit>[KMGTP])(?:I?B)?      # 16G, 16GB, 16GiB
          |B)                             # plain bytes
        \s*$''',
    re.VERBOSE | re.IGNORECASE)

_MEMORY_HELP = "use a number with a unit, e.g. '512M', '16G', '16GB' or '1.5T'"


def _check_not_bool(value, what):
    # TOML booleans are Python bools, which are also ints
    if isinstance(value, bool):
        raise UnitError(f'invalid {what} {value!r}: expected a string')


def parse_memory(value):
    '''Parse a memory size into a number of bytes

    Accepted: a number followed by a unit, ``K``, ``M``, ``G``, ``T`` or ``P``
    (case insensitive), optionally followed by ``B`` or ``iB``: ``512M``,
    ``16G``, ``16GB``, ``16gib``, ``1.5T``. Units are binary (``1K = 1024``).
    ``B`` alone means bytes. A number without a unit is rejected, since it is
    ambiguous between schedulers.

    Fractional sizes are rounded up to a whole byte.

    Parameters
    ----------
    value : str

    Returns
    -------
    nbytes : int
        Always positive

    Raises
    ------
    UnitError
    '''
    _check_not_bool(value, 'memory size')
    if isinstance(value, int | float):
        raise UnitError(f'invalid memory size {value!r}: a unit is required; {_MEMORY_HELP}')
    if not isinstance(value, str):
        raise UnitError(f'invalid memory size {value!r}: expected a string; {_MEMORY_HELP}')
    m = _MEMORY_RE.match(value)
    if m is None:
        if re.fullmatch(r'\s*\d+(\.\d*)?\s*', value):
            raise UnitError(f'invalid memory size {value!r}: a unit is required; {_MEMORY_HELP}')
        raise UnitError(f'invalid memory size {value!r}: {_MEMORY_HELP}')
    number = Fraction(m['number'])
    unit = (m['unit'] or '').upper()
    nbytes = number * _MEMORY_UNITS[unit]
    nbytes = -(-nbytes.numerator // nbytes.denominator)  # ceiling
    if nbytes <= 0:
        raise UnitError(f'invalid memory size {value!r}: must be larger than zero')
    return nbytes


def memory_in_unit(nbytes, units='KMGT'):
    '''Express ``nbytes`` exactly, in the largest possible unit

    Parameters
    ----------
    nbytes : int
    units : str, optional
        The units that may be used, in increasing order of size. The smallest
        one is used, rounding *up*, when ``nbytes`` is not a multiple of it.
        Pass ``'BKMGT'`` to allow plain bytes.

    Returns
    -------
    number : int
    unit : str
        One of ``units`` (``'B'`` for bytes)
    '''
    if nbytes <= 0:
        raise ValueError(f'memory size must be positive (got {nbytes})')
    sizes = [(u, _MEMORY_UNITS['' if u == 'B' else u]) for u in units]
    for unit, size in reversed(sizes):
        if nbytes % size == 0:
            return nbytes // size, unit
    unit, size = sizes[0]
    return -(-nbytes // size), unit


def format_memory(nbytes):
    '''Format a number of bytes in a human readable, exact form (``16G``)'''
    number, unit = memory_in_unit(nbytes, units='BKMGTP')
    return f'{number}{unit}'


_TIME_SUFFIX_RE = re.compile(
    r'''^\s*
        (?:(?P<d>\d+)\s*d\s*)?
        (?:(?P<h>\d+)\s*h\s*)?
        (?:(?P<m>\d+)\s*m(?:in)?\s*)?
        (?:(?P<s>\d+)\s*s\s*)?
        $''',
    re.VERBOSE | re.IGNORECASE)

_TIME_DAYS_RE = re.compile(
    r'^\s*(?P<d>\d+)-(?P<h>\d+)(?::(?P<m>\d+)(?::(?P<s>\d+))?)?\s*$')

_TIME_HMS_RE = re.compile(r'^\s*(?P<h>\d+):(?P<m>\d+):(?P<s>\d+)\s*$')

_TIME_HELP = (
    "use minutes ('90'), 'HH:MM:SS', 'D-HH', 'D-HH:MM', 'D-HH:MM:SS', "
    "or units such as '30m', '12h', '2d' or '1h30m'")


def parse_time(value):
    '''Parse a wall time into a number of seconds

    Accepted formats:

    - ``MM``: a plain number is minutes (as in SLURM). A TOML integer is also
      accepted.
    - ``HH:MM:SS``: hours may be larger than 23.
    - ``D-HH``, ``D-HH:MM``, ``D-HH:MM:SS``: days and hours.
    - Units: any of ``<n>d``, ``<n>h``, ``<n>m``, ``<n>s``, in this order,
      e.g. ``30m``, ``12h``, ``1h30m``, ``2d12h``.

    Two fields, as in ``1:30``, are rejected: SLURM reads them as
    ``MM:SS``, but they are easily meant as ``HH:MM``.

    Parameters
    ----------
    value : str or int

    Returns
    -------
    seconds : int
        Always positive

    Raises
    ------
    UnitError
    '''
    _check_not_bool(value, 'time')
    if isinstance(value, int):
        if value <= 0:
            raise UnitError(f'invalid time {value!r}: must be larger than zero')
        return value * 60
    if not isinstance(value, str):
        raise UnitError(f'invalid time {value!r}: {_TIME_HELP}')

    if re.fullmatch(r'\s*\d+\s*', value):
        seconds = int(value) * 60
    elif m := _TIME_HMS_RE.match(value):
        seconds = _from_fields(value, 0, m['h'], m['m'], m['s'], hours_limit=None)
    elif m := _TIME_DAYS_RE.match(value):
        seconds = _from_fields(value, m['d'], m['h'], m['m'], m['s'], hours_limit=24)
    elif re.fullmatch(r'\s*\d+:\d+\s*', value):
        raise UnitError(
            f'invalid time {value!r}: two fields are ambiguous (MM:SS or HH:MM?); '
            f"write it as 'HH:MM:SS'")
    elif (m := _TIME_SUFFIX_RE.match(value)) and any(m.groups()):
        seconds = _from_fields(value, m['d'], m['h'], m['m'], m['s'], hours_limit=None,
                               check_ranges=False)
    else:
        raise UnitError(f'invalid time {value!r}: {_TIME_HELP}')

    if seconds <= 0:
        raise UnitError(f'invalid time {value!r}: must be larger than zero')
    return seconds


def _from_fields(value, d, h, m, s, *, hours_limit, check_ranges=True):
    d, h, m, s = (int(x) if x else 0 for x in (d, h, m, s))
    if check_ranges:
        if m >= 60 or s >= 60:
            raise UnitError(f'invalid time {value!r}: minutes and seconds must be below 60')
        if hours_limit is not None and h >= hours_limit:
            raise UnitError(
                f'invalid time {value!r}: hours must be below 24 when days are given')
    return ((d * 24 + h) * 60 + m) * 60 + s


def format_hms(seconds):
    '''Format seconds as ``HH:MM:SS`` (hours may be larger than 23)'''
    if seconds <= 0:
        raise ValueError(f'time must be positive (got {seconds})')
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f'{h:02}:{m:02}:{s:02}'


def format_dhms(seconds):
    '''Format seconds as ``D-HH:MM:SS``, or ``HH:MM:SS`` if under a day'''
    if seconds <= 0:
        raise ValueError(f'time must be positive (got {seconds})')
    d, rest = divmod(seconds, 24 * 3600)
    hms = format_hms(rest) if rest else '00:00:00'
    return f'{d}-{hms}' if d else hms
