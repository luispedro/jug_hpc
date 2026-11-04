from pathlib import Path

import pytest

from jug_hpc.config import (
    ConfigError,
    ProjectSettings,
    ResourceClass,
    SubmitterSettings,
    load_project_settings,
    split_jugdir,
)

GOALS_EXAMPLE = '''
jugfile = "my_jugfile.py"
jugdir  = "my_jugfile.jugdata"
logs    = "logs/"
setup   = """
source .venv/bin/activate
"""

[default]
cpus          = 1
memory        = "4G"
time          = "1:00:00"
workers       = 50
time_fraction = 0.8

[class.align]
match   = "align_*"
cpus    = 8
memory  = "32G"
time    = "4:00:00"
workers = 10

[class.merge]
match         = "merge"
memory        = "128G"
time          = "12:00:00"
workers       = 1
partition     = "bigmem"
time_fraction = 0.5

[class.merge.slurm]
gres       = "gpu:1"
constraint = "skylake"
'''

MINIMAL = '''
[default]
memory = "4G"
time = "1:00:00"
'''

G = 1024**3


def load(tmp_path, text):
    (tmp_path / 'jug_hpc.toml').write_text(text)
    return load_project_settings(tmp_path)


def error(tmp_path, text):
    with pytest.raises(ConfigError) as e:
        load(tmp_path, text)
    msg = str(e.value)
    assert msg.startswith(str(tmp_path / 'jug_hpc.toml') + ': ')
    return msg


# --- the example from GOALS.md -----------------------------------------------

def test_goals_example(tmp_path):
    s = load(tmp_path, GOALS_EXAMPLE)
    assert isinstance(s, ProjectSettings)
    assert s.path == tmp_path / 'jug_hpc.toml'
    assert s.project_dir == tmp_path
    assert s.jugfile == tmp_path / 'my_jugfile.py'
    assert s.jugdir == tmp_path / 'my_jugfile.jugdata'
    assert s.logs == tmp_path / 'logs'
    assert s.setup == 'source .venv/bin/activate\n'
    assert list(s.classes) == ['align', 'merge']
    assert [c.name for c in s.all_classes()] == ['align', 'merge', 'default']

    d = s.default
    assert d.is_default
    assert (d.cpus, d.memory, d.time, d.workers, d.time_fraction) == (1, 4 * G, 3600, 50, 0.8)
    assert d.match is None and d.pattern is None and d.partition is None
    assert d.max_seconds == 2880

    align = s.get_class('align')
    assert not align.is_default
    assert align.match == 'align_*' and align.pattern is None
    assert (align.cpus, align.memory, align.time, align.workers) == (8, 32 * G, 4 * 3600, 10)
    assert align.time_fraction == 0.8
    assert align.origins['time_fraction'] == ('default', 'time_fraction')
    assert align.origins['cpus'] == ('class', 'align', 'cpus')
    assert align.raw == {}

    merge = s.classes['merge']
    assert merge.cpus == 1
    assert merge.origins['cpus'] == ('default', 'cpus')
    assert merge.partition == 'bigmem'
    assert merge.max_seconds == 6 * 3600
    assert merge.raw == {'slurm': {'gres': 'gpu:1', 'constraint': 'skylake'}}
    assert merge.raw_options('slurm') == {'gres': 'gpu:1', 'constraint': 'skylake'}
    assert merge.raw_options('pbs') == {}

    assert s.get_class('default') is d
    with pytest.raises(KeyError):
        s.get_class('nope')


def test_minimal_defaults(tmp_path):
    s = load(tmp_path, MINIMAL)
    assert s.jugfile == tmp_path / 'jugfile.py'
    assert s.jugdir is None
    assert s.logs == tmp_path / 'logs'
    assert s.setup == ''
    assert s.classes == {}
    d = s.default
    assert (d.cpus, d.workers, d.time_fraction, d.partition) == (1, 1, 0.8, None)
    assert d.origins['cpus'] is None
    assert d.origins['memory'] == ('default', 'memory')


def test_project_dir_is_made_absolute(tmp_path, monkeypatch):
    (tmp_path / 'jug_hpc.toml').write_text(MINIMAL)
    monkeypatch.chdir(tmp_path)
    s = load_project_settings('.')
    assert s.project_dir.is_absolute()
    assert s.jugfile == tmp_path / 'jugfile.py'


def test_absolute_paths_kept(tmp_path):
    s = load(tmp_path, 'jugfile = "/data/j.py"\nlogs = "/scratch/logs"\n' + MINIMAL)
    assert str(s.jugfile) == '/data/j.py'
    assert str(s.logs) == '/scratch/logs'


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_project_settings(tmp_path)
    assert str(tmp_path / 'jug_hpc.toml') in str(e.value)


def test_invalid_toml(tmp_path):
    msg = error(tmp_path, '[default\n')
    assert 'invalid TOML' in msg


# --- inheritance -------------------------------------------------------------

def test_class_inherits_everything(tmp_path):
    s = load(tmp_path, '''
[default]
cpus = 2
memory = "8G"
time = "2h"
workers = 5
time_fraction = 0.5
partition = "short"

[class.a]
pattern = "/^mod\\\\.a/"
''')
    a = s.classes['a']
    d = s.default
    assert a.pattern == '/^mod\\.a/' and a.match is None
    for k in ('cpus', 'memory', 'time', 'workers', 'time_fraction', 'partition'):
        assert getattr(a, k) == getattr(d, k)
        assert a.origins[k] == ('default', k)


def test_class_inherits_builtin_defaults(tmp_path):
    s = load(tmp_path, MINIMAL + '[class.a]\nmatch = "a"\n')
    a = s.classes['a']
    assert a.cpus == 1 and a.workers == 1 and a.time_fraction == 0.8
    assert a.origins['cpus'] is None


def test_raw_options_merged_with_default(tmp_path):
    s = load(tmp_path, MINIMAL + '''
[default.pbs]
directives = ["-m n"]
[default.pbs.select]
ngpus = 0
host = "x"

[class.gpu]
match = "train"
[class.gpu.pbs.select]
ngpus = 1

[class.cpu]
match = "prep"
''')
    assert s.default.raw == {'pbs': {'directives': ['-m n'], 'select': {'ngpus': 0, 'host': 'x'}}}
    assert s.classes['gpu'].raw == {
        'pbs': {'directives': ['-m n'], 'select': {'ngpus': 1, 'host': 'x'}}}
    assert s.classes['cpu'].raw == s.default.raw


def test_raw_options_conflicting_shape(tmp_path):
    msg = error(tmp_path, MINIMAL + '''
[default.pbs]
select = "ngpus=1"
[class.gpu]
match = "train"
[class.gpu.pbs.select]
ngpus = 1
''')
    assert 'class.gpu.pbs.select' in msg
    assert '[default]' in msg


# --- selectors ---------------------------------------------------------------

def test_class_needs_a_selector(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\ncpus = 2\n')
    assert "'class.a'" in msg and 'neither' in msg


def test_class_cannot_have_both_selectors(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\nmatch = "a"\npattern = "a"\n')
    assert 'both' in msg


@pytest.mark.parametrize('key', ['match', 'pattern'])
def test_selector_not_allowed_in_default(tmp_path, key):
    msg = error(tmp_path, MINIMAL + f'{key} = "x"\n')
    assert f"'default.{key}'" in msg


@pytest.mark.parametrize('pattern', ['//', ''])
def test_empty_pattern(tmp_path, pattern):
    msg = error(tmp_path, MINIMAL + f'[class.a]\npattern = "{pattern}"\n')
    assert 'class.a.pattern' in msg


def test_bad_regex(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\npattern = "/(unclosed/"\n')
    assert 'regular expression' in msg


def test_plain_pattern_is_not_a_regex(tmp_path):
    s = load(tmp_path, MINIMAL + '[class.a]\npattern = "(unclosed"\n')
    assert s.classes['a'].pattern == '(unclosed'


def test_empty_match(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\nmatch = " "\n')
    assert 'class.a.match' in msg and 'empty' in msg


def test_match_must_be_a_string(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\nmatch = ["a", "b"]\n')
    assert 'must be a string' in msg and 'an array' in msg


# --- resource values ---------------------------------------------------------

def test_memory_and_time_required(tmp_path):
    msg = error(tmp_path, '[default]\nmemory = "4G"\n')
    assert "'time'" in msg
    msg = error(tmp_path, '[default]\ntime = "4h"\n')
    assert "'memory'" in msg
    msg = error(tmp_path, '[class.a]\nmatch = "a"\nmemory = "1G"\ntime = "1h"\n')
    assert "[default] must set 'memory'" in msg


@pytest.mark.parametrize('key,value,expected', [
    ('memory', '"16"', 'unit is required'),
    ('memory', '16', 'unit is required'),
    ('memory', '"lots"', 'invalid memory size'),
    ('time', '"1:30"', 'ambiguous'),
    ('time', '0', 'larger than zero'),
    ('time', 'true', 'invalid time'),
])
def test_unit_errors_name_file_and_key(tmp_path, key, value, expected):
    msg = error(tmp_path, f'[default]\nmemory = "1G"\ntime = "1h"\n[class.a]\nmatch = "a"\n'
                          f'{key} = {value}\n')
    assert f"'class.a.{key}'" in msg
    assert expected in msg


@pytest.mark.parametrize('key', ['cpus', 'workers'])
@pytest.mark.parametrize('value,expected', [
    ('0', 'at least 1'),
    ('-2', 'at least 1'),
    ('1.5', 'a float'),
    ('"4"', 'a string'),
    ('true', 'a boolean'),
])
def test_positive_integers(tmp_path, key, value, expected):
    msg = error(tmp_path, MINIMAL + f'{key} = {value}\n')
    assert f"'default.{key}'" in msg
    assert expected in msg


@pytest.mark.parametrize('value', ['0', '0.0', '-0.5', '1.01', '2'])
def test_time_fraction_out_of_range(tmp_path, value):
    msg = error(tmp_path, MINIMAL + f'time_fraction = {value}\n')
    assert "'default.time_fraction'" in msg
    assert 'at most 1' in msg


@pytest.mark.parametrize('value', ['true', '"0.5"'])
def test_time_fraction_type(tmp_path, value):
    msg = error(tmp_path, MINIMAL + f'time_fraction = {value}\n')
    assert 'must be a number' in msg


def test_time_fraction_one_is_allowed(tmp_path):
    s = load(tmp_path, MINIMAL + 'time_fraction = 1\n')
    assert s.default.time_fraction == 1.0
    assert isinstance(s.default.time_fraction, float)
    assert s.default.max_seconds == 3600


@pytest.mark.parametrize('fraction,time,expected', [
    (0.8, 3600, 2880),
    (0.29, 100, 29),       # 0.29 * 100 == 28.999999999999996 in floating point
    (0.7, 100, 70),
    (0.5, 3, 1),           # floor
    (1.0, 7, 7),
    (0.333, 1000, 333),
])
def test_max_seconds(fraction, time, expected):
    rc = ResourceClass(name='x', cpus=1, memory=1, time=time, workers=1,
                       time_fraction=fraction)
    assert rc.max_seconds == expected


def test_max_seconds_must_be_positive(tmp_path):
    msg = error(tmp_path, '[default]\nmemory = "1G"\ntime = "1s"\ntime_fraction = 0.5\n')
    assert "'default.time_fraction'" in msg
    assert 'at least one second' in msg


def test_max_seconds_must_be_positive_with_inherited_values(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\nmatch = "a"\ntime = "1s"\n')
    assert "'class.a.time'" in msg


def test_partition(tmp_path):
    s = load(tmp_path, MINIMAL + 'partition = " short "\n')
    assert s.default.partition == 'short'
    msg = error(tmp_path, MINIMAL + 'partition = ""\n')
    assert 'empty' in msg


# --- rejected keys -----------------------------------------------------------

@pytest.mark.parametrize('key', ['scheduler', 'account'])
def test_cluster_keys_rejected_at_top_level(tmp_path, key):
    msg = error(tmp_path, f'{key} = "x"\n' + MINIMAL)
    assert f"'{key}' is a cluster setting" in msg
    assert 'cluster.toml' in msg


@pytest.mark.parametrize('key', ['scheduler', 'account'])
def test_cluster_keys_rejected_in_class(tmp_path, key):
    msg = error(tmp_path, MINIMAL + f'[class.a]\nmatch = "a"\n{key} = "x"\n')
    assert f"'class.a.{key}' is a cluster setting" in msg


def test_top_level_partition_rejected(tmp_path):
    msg = error(tmp_path, 'partition = "x"\n' + MINIMAL)
    assert '[default]' in msg


def test_top_level_scheduler_table_rejected(tmp_path):
    msg = error(tmp_path, MINIMAL + '[pbs]\nselect = {ngpus = 1}\n')
    assert "'pbs'" in msg
    assert 'cluster.toml' in msg and '[class.<name>.pbs]' in msg


def test_unknown_top_level_key(tmp_path):
    msg = error(tmp_path, 'jugfle = "x.py"\n' + MINIMAL)
    assert "did you mean 'jugfile'" in msg


def test_unknown_class_key(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\nmatch = "a"\nmemroy = "1G"\n')
    assert "'class.a.memroy'" in msg
    assert "did you mean 'memory'" in msg


def test_unknown_default_key(tmp_path):
    msg = error(tmp_path, MINIMAL + 'setup = "module load x"\n')
    assert "'default.setup'" in msg


def test_class_key_as_table_rejected(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.a]\nmatch = "a"\n[class.a.memory]\nx = 1\n')
    assert "'class.a.memory' must be a value" in msg


@pytest.mark.parametrize('text,expected', [
    ('default = 3\n', "'default' must be a table"),
    ('class = "a"\n', "'class' must be a table"),
])
def test_tables_must_be_tables(tmp_path, text, expected):
    assert expected in error(tmp_path, text)


def test_class_entry_must_be_a_table(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class]\na = "align_*"\n')
    assert "'class.a' must be a table" in msg


def test_class_named_default_rejected(tmp_path):
    msg = error(tmp_path, MINIMAL + '[class.default]\nmatch = "a"\n')
    assert "'class.default'" in msg


@pytest.mark.parametrize('name', ['a.b', 'a b', 'ä', ''])
def test_invalid_class_names(tmp_path, name):
    msg = error(tmp_path, MINIMAL + f'[class."{name}"]\nmatch = "a"\n')
    assert 'invalid class name' in msg


# --- jugdir backends ----------------------------------------------------------

@pytest.mark.parametrize('jugdir, expected', [
    ('store', ('', 'store')),
    ('/abs/store', ('', '/abs/store')),
    ('redis://localhost:6379/', ('redis:', None)),
    ('redis:', ('redis:', None)),
    ('dict_store', ('dict_store', None)),
    ('dict_store:state.pkl', ('dict_store:', 'state.pkl')),
    ('file_keepalive:store', ('file_keepalive:', 'store')),
    ('file_keepalive:/abs', ('file_keepalive:', '/abs')),
    # Not a backend prefix: Jug treats these as directories
    ('dict_store_dir', ('', 'dict_store_dir')),
    ('file_keepalive', ('', 'file_keepalive')),
])
def test_split_jugdir(jugdir, expected):
    assert split_jugdir(jugdir) == expected


def test_jugdir_plain_directory_is_a_path(tmp_path):
    s = load(tmp_path, 'jugdir = "data/store"\n' + MINIMAL)
    assert s.jugdir == tmp_path / 'data' / 'store'
    s = load(tmp_path, 'jugdir = "/abs/store"\n' + MINIMAL)
    assert s.jugdir == Path('/abs/store')


@pytest.mark.parametrize('jugdir', ['redis://localhost:6379/', 'redis:', 'dict_store'])
def test_jugdir_url_backends_are_kept_as_is(tmp_path, jugdir):
    s = load(tmp_path, f'jugdir = "{jugdir}"\n' + MINIMAL)
    assert s.jugdir == jugdir
    assert isinstance(s.jugdir, str)


@pytest.mark.parametrize('prefix', ['file_keepalive:', 'dict_store:'])
def test_jugdir_path_backends_are_resolved(tmp_path, prefix):
    s = load(tmp_path, f'jugdir = "{prefix}store"\n' + MINIMAL)
    assert s.jugdir == f'{prefix}{tmp_path / "store"}'
    s = load(tmp_path, f'jugdir = "{prefix} store "\n' + MINIMAL)
    assert s.jugdir == f'{prefix}{tmp_path / "store"}'
    s = load(tmp_path, f'jugdir = "{prefix}/abs/store"\n' + MINIMAL)
    assert s.jugdir == f'{prefix}/abs/store'


@pytest.mark.parametrize('prefix', ['file_keepalive:', 'dict_store:'])
def test_jugdir_path_backend_needs_a_path(tmp_path, prefix):
    msg = error(tmp_path, f'jugdir = "{prefix}  "\n' + MINIMAL)
    assert f"'jugdir' needs a path after {prefix!r}" in msg


@pytest.mark.parametrize('key', ['jugfile', 'jugdir', 'logs'])
def test_paths_must_be_non_empty_strings(tmp_path, key):
    assert 'empty' in error(tmp_path, f'{key} = ""\n' + MINIMAL)
    assert 'must be a string' in error(tmp_path, f'{key} = 1\n' + MINIMAL)


def test_setup_may_be_empty_but_must_be_a_string(tmp_path):
    assert load(tmp_path, 'setup = ""\n' + MINIMAL).setup == ''
    assert 'must be a string' in error(tmp_path, 'setup = ["a"]\n' + MINIMAL)


# --- [submitter] ------------------------------------------------------------------

def test_submitter_defaults(tmp_path):
    settings = load(tmp_path, MINIMAL)
    assert settings.submitter == SubmitterSettings()
    assert (settings.submitter.cpus, settings.submitter.memory) == (1, 2 * G)
    assert settings.submitter.time is None
    assert settings.submitter.name == 'submitter'


def test_submitter_table(tmp_path):
    settings = load(tmp_path, MINIMAL + '''
[submitter]
memory = "500M"
time = "7d"
partition = "long"

[submitter.pbs.resources]
place = "free"
''')
    s = settings.submitter
    assert (s.cpus, s.memory, s.time, s.partition) == (1, 500 * 1024**2, 7 * 86400, 'long')
    assert s.raw_options('pbs') == {'resources': {'place': 'free'}}
    assert s.raw_options('slurm') == {}
    assert s.origins == {'memory': ('submitter', 'memory'), 'time': ('submitter', 'time'),
                         'partition': ('submitter', 'partition')}
    # nothing is inherited from [default], nor given to it
    assert settings.default.partition is None
    assert settings.default.memory == 4 * G


@pytest.mark.parametrize('text, expected', [
    ('workers = 2', "'submitter.workers' is not a known key"),
    ('mem = "1G"', "did you mean 'memory'?"),
    ('cpus = 0', "'submitter.cpus' must be at least 1"),
    ('memory = "lots"', "'submitter.memory'"),
    ('time = true', "'submitter.time'"),
    ('partition = ""', "'submitter.partition' must not be empty"),
    ('account = "x"', "'submitter.account' is a cluster setting"),
    ('time = {}', "'submitter.time' must be a value, not a table"),
])
def test_submitter_errors(tmp_path, text, expected):
    with pytest.raises(ConfigError, match=expected.replace('?', r'\?')) as e:
        load(tmp_path, MINIMAL + '[submitter]\n' + text + '\n')
    assert str(e.value).startswith(str(tmp_path / 'jug_hpc.toml'))


def test_submitter_must_be_a_table(tmp_path):
    with pytest.raises(ConfigError, match="'submitter' must be a table"):
        load(tmp_path, 'submitter = 1\n' + MINIMAL)


def test_class_named_submitter_rejected(tmp_path):
    with pytest.raises(ConfigError, match="'class.submitter' is not allowed: 'submitter' is"):
        load(tmp_path, MINIMAL + '[class.submitter]\nmatch = "x"\n')
