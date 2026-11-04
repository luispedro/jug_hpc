import pytest
from hypothesis import given
from hypothesis import strategies as st

from jug_hpc.units import (
    UnitError,
    format_dhms,
    format_hms,
    format_memory,
    memory_in_unit,
    parse_memory,
    parse_time,
)

K, M, G, T = 1024, 1024**2, 1024**3, 1024**4


@pytest.mark.parametrize('value,expected', [
    ('512M', 512 * M),
    ('16G', 16 * G),
    ('16GB', 16 * G),
    ('16gb', 16 * G),
    ('16GiB', 16 * G),
    ('16 G', 16 * G),
    (' 4g ', 4 * G),
    ('1.5T', 3 * T // 2),
    ('.5G', 512 * M),
    ('2048K', 2 * M),
    ('100B', 100),
    ('100b', 100),
    ('1P', 1024**5),
    ('0.1K', 103),  # rounded up to a whole byte
])
def test_parse_memory(value, expected):
    assert parse_memory(value) == expected


@pytest.mark.parametrize('value,message', [
    ('16', 'unit is required'),
    ('1.5', 'unit is required'),
    (16, 'unit is required'),
    (1.5, 'unit is required'),
    (True, 'expected a string'),
    (None, 'expected a string'),
    ('', 'e.g.'),
    ('G', 'e.g.'),
    ('16X', 'e.g.'),
    ('16GG', 'e.g.'),
    ('16iB', 'e.g.'),
    ('-1G', 'e.g.'),
    ('1e3M', 'e.g.'),
    ('0G', 'larger than zero'),
    ('0.0M', 'larger than zero'),
])
def test_parse_memory_errors(value, message):
    with pytest.raises(UnitError, match=message):
        parse_memory(value)


def test_unit_error_is_value_error():
    with pytest.raises(ValueError):
        parse_memory('nonsense')


@pytest.mark.parametrize('nbytes,units,expected', [
    (16 * G, 'KMGT', (16, 'G')),
    (1536 * M, 'KMGT', (1536, 'M')),
    (2 * T, 'KMGT', (2, 'T')),
    (2 * T, 'KMG', (2048, 'G')),
    (100, 'KMGT', (1, 'K')),         # rounded up to the smallest unit
    (1025, 'KMGT', (2, 'K')),
    (100, 'BKMGT', (100, 'B')),
    (3 * M + 1, 'MG', (4, 'M')),
])
def test_memory_in_unit(nbytes, units, expected):
    assert memory_in_unit(nbytes, units) == expected


def test_memory_in_unit_rejects_nonpositive():
    with pytest.raises(ValueError):
        memory_in_unit(0)


@pytest.mark.parametrize('nbytes,expected', [
    (16 * G, '16G'),
    (1536 * M, '1536M'),
    (100, '100B'),
    (1024**5, '1P'),
])
def test_format_memory(nbytes, expected):
    assert format_memory(nbytes) == expected


@given(st.integers(min_value=1, max_value=1024**6))
def test_memory_roundtrip(nbytes):
    assert parse_memory(format_memory(nbytes)) == nbytes


@given(st.integers(min_value=1, max_value=1024**6), st.sampled_from(['K', 'M', 'G', 'T']))
def test_memory_in_unit_never_rounds_down(nbytes, smallest):
    units = 'KMGT'[('KMGT').index(smallest):]
    number, unit = memory_in_unit(nbytes, units)
    assert unit in units
    rendered = parse_memory(f'{number}{unit}')
    assert rendered >= nbytes
    assert rendered - nbytes < parse_memory(f'1{units[0]}')


@pytest.mark.parametrize('value,expected', [
    ('90', 90 * 60),
    (90, 90 * 60),
    ('1:00:00', 3600),
    ('01:30:00', 5400),
    ('48:00:00', 48 * 3600),
    ('0:00:30', 30),
    ('1-00:00:00', 24 * 3600),
    ('2-12', 60 * 3600),
    ('1-1:30', 25 * 3600 + 30 * 60),
    ('1-01:02:03', 24 * 3600 + 3600 + 2 * 60 + 3),
    ('30m', 30 * 60),
    ('30min', 30 * 60),
    ('12h', 12 * 3600),
    ('2d', 48 * 3600),
    ('45s', 45),
    ('1h30m', 5400),
    ('1H30M', 5400),
    ('1d 12h', 36 * 3600),
    ('1h0m30s', 3630),
    ('90m', 5400),  # no range check on units
])
def test_parse_time(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize('value,message', [
    ('1:30', 'ambiguous'),
    ('0', 'larger than zero'),
    (0, 'larger than zero'),
    (-5, 'larger than zero'),
    ('0:00:00', 'larger than zero'),
    ('0m', 'larger than zero'),
    ('1:60:00', 'below 60'),
    ('1:00:60', 'below 60'),
    ('1-24:00:00', 'below 24'),
    ('', 'HH:MM:SS'),
    ('h', 'HH:MM:SS'),
    ('1.5h', 'HH:MM:SS'),
    ('30m1h', 'HH:MM:SS'),
    ('1:2:3:4', 'HH:MM:SS'),
    (1.5, 'HH:MM:SS'),
    (True, 'expected a string'),
])
def test_parse_time_errors(value, message):
    with pytest.raises(UnitError, match=message):
        parse_time(value)


@pytest.mark.parametrize('seconds,hms,dhms', [
    (1, '00:00:01', '00:00:01'),
    (3600, '01:00:00', '01:00:00'),
    (86399, '23:59:59', '23:59:59'),
    (86400, '24:00:00', '1-00:00:00'),
    (90061, '25:01:01', '1-01:01:01'),
    (100 * 3600, '100:00:00', '4-04:00:00'),
])
def test_format_time(seconds, hms, dhms):
    assert format_hms(seconds) == hms
    assert format_dhms(seconds) == dhms


@given(st.integers(min_value=1, max_value=1000 * 86400))
def test_time_roundtrip(seconds):
    assert parse_time(format_hms(seconds)) == seconds
    assert parse_time(format_dhms(seconds)) == seconds


def test_format_time_rejects_nonpositive():
    with pytest.raises(ValueError):
        format_hms(0)
    with pytest.raises(ValueError):
        format_dhms(-1)
