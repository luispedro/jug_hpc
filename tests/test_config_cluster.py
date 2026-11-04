from pathlib import Path

import pytest

from jug_hpc.config import (
    ClusterSettings,
    ConfigError,
    load_cluster_settings,
    user_cluster_path,
)
from jug_hpc.config.toml import format_key, merge, read_toml


@pytest.fixture
def dirs(tmp_path):
    '''A fake home directory (with XDG_CONFIG_HOME unset) and a project dir'''
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    home.mkdir()
    project.mkdir()
    environ = {'HOME': str(home)}
    user_file = home / '.config' / 'jug_hpc' / 'cluster.toml'
    return environ, user_file, project


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- user file lookup --------------------------------------------------------

def test_user_path_default():
    assert user_cluster_path({'HOME': '/home/u'}) == \
        Path('/home/u/.config/jug_hpc/cluster.toml')


def test_user_path_xdg():
    env = {'HOME': '/home/u', 'XDG_CONFIG_HOME': '/etc/xdg-u'}
    assert user_cluster_path(env) == Path('/etc/xdg-u/jug_hpc/cluster.toml')


@pytest.mark.parametrize('value', ['', 'relative/dir'])
def test_user_path_xdg_ignored_if_empty_or_relative(value):
    env = {'HOME': '/home/u', 'XDG_CONFIG_HOME': value}
    assert user_cluster_path(env) == Path('/home/u/.config/jug_hpc/cluster.toml')


def test_user_path_uses_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    assert user_cluster_path() == tmp_path / 'jug_hpc' / 'cluster.toml'


# --- loading and merging -----------------------------------------------------

def test_neither_file_exists(dirs):
    environ, user_file, project = dirs
    with pytest.raises(ConfigError) as e:
        load_cluster_settings(project, environ=environ)
    msg = str(e.value)
    assert str(user_file) in msg
    assert str(project / 'cluster.toml') in msg
    assert 'scheduler' in msg


def test_user_file_only(dirs):
    environ, user_file, project = dirs
    write(user_file, '''
scheduler = "pbs"
account = "grp"
partition = "standard"
setup = """
module load python
"""
[pbs]
directives = ["-m n"]
[pbs.select]
ngpus = 1
''')
    s = load_cluster_settings(project, environ=environ)
    assert isinstance(s, ClusterSettings)
    assert s.scheduler == 'pbs'
    assert s.account == 'grp'
    assert s.partition == 'standard'
    assert s.setup == 'module load python\n'
    assert s.raw == {'pbs': {'directives': ['-m n'], 'select': {'ngpus': 1}}}
    assert s.raw_options() == s.raw['pbs']
    assert s.raw_options('slurm') == {}
    assert s.files == (user_file,)
    assert s.source('account') == user_file
    assert s.source('pbs', 'select', 'ngpus') == user_file
    assert s.source('nonexistent') is None


def test_project_file_only(dirs):
    environ, _, project = dirs
    pfile = write(project / 'cluster.toml', 'scheduler = "slurm"\n')
    s = load_cluster_settings(project, environ=environ)
    assert s.scheduler == 'slurm'
    assert s.account is None
    assert s.partition is None
    assert s.setup == ''
    assert s.raw == {}
    assert s.files == (pfile,)


def test_project_overrides_user_key_by_key(dirs):
    environ, user_file, project = dirs
    write(user_file, '''
scheduler = "slurm"
account = "my_group"
partition = "standard"
[slurm]
qos = "normal"
constraint = "skylake"
[slurm.nested]
a = 1
b = [1, 2]
''')
    pfile = write(project / 'cluster.toml', '''
account = "other_grant"
[slurm]
qos = "high"
[slurm.nested]
b = [3]
''')
    s = load_cluster_settings(project, environ=environ)
    assert s.scheduler == 'slurm'
    assert s.account == 'other_grant'
    assert s.partition == 'standard'
    # tables merged recursively; arrays replaced as a whole
    assert s.raw['slurm'] == {
        'qos': 'high', 'constraint': 'skylake', 'nested': {'a': 1, 'b': [3]}}
    assert s.files == (user_file, pfile)
    assert {k: src for k, _, src in s.leaves()} == {
        ('scheduler',): user_file,
        ('account',): pfile,
        ('partition',): user_file,
        ('slurm', 'qos'): pfile,
        ('slurm', 'constraint'): user_file,
        ('slurm', 'nested', 'a'): user_file,
        ('slurm', 'nested', 'b'): pfile,
    }


def test_xdg_config_home_is_used(tmp_path):
    xdg = tmp_path / 'xdg'
    project = tmp_path / 'project'
    project.mkdir()
    write(xdg / 'jug_hpc' / 'cluster.toml', 'scheduler = "pbs"\n')
    s = load_cluster_settings(project, environ={'HOME': '/nonexistent',
                                                'XDG_CONFIG_HOME': str(xdg)})
    assert s.scheduler == 'pbs'


def test_same_file_read_once(dirs):
    environ, user_file, _ = dirs
    write(user_file, 'scheduler = "pbs"\n')
    s = load_cluster_settings(user_file.parent, environ=environ)
    assert s.files == (user_file,)


def test_table_vs_value_conflict(dirs):
    environ, user_file, project = dirs
    write(user_file, 'scheduler = "pbs"\n[pbs]\nselect = "x"\n')
    pfile = write(project / 'cluster.toml', '[pbs.select]\nngpus = 1\n')
    with pytest.raises(ConfigError) as e:
        load_cluster_settings(project, environ=environ)
    msg = str(e.value)
    assert str(pfile) in msg
    assert str(user_file) in msg
    assert "'pbs.select'" in msg


def test_invalid_toml_names_file(dirs):
    environ, user_file, project = dirs
    write(user_file, 'scheduler = \n')
    with pytest.raises(ConfigError, match='invalid TOML') as e:
        load_cluster_settings(project, environ=environ)
    assert str(user_file) in str(e.value)


def test_unreadable_file(dirs):
    environ, user_file, project = dirs
    user_file.mkdir(parents=True)  # a directory where the file should be
    with pytest.raises(ConfigError, match='cannot read') as e:
        load_cluster_settings(project, environ=environ)
    assert str(user_file) in str(e.value)


# --- validation --------------------------------------------------------------

def test_scheduler_required(dirs):
    environ, user_file, project = dirs
    write(user_file, 'account = "x"\n')
    pfile = write(project / 'cluster.toml', 'partition = "y"\n')
    with pytest.raises(ConfigError, match='no scheduler') as e:
        load_cluster_settings(project, environ=environ)
    assert str(user_file) in str(e.value)
    assert str(pfile) in str(e.value)


@pytest.mark.parametrize('text,message', [
    ('scheduler = 1', "'scheduler' must be a string, not an integer"),
    ('scheduler = "pbs"\naccount = true', "'account' must be a string, not a boolean"),
    ('scheduler = "pbs"\nsetup = ["a"]', "'setup' must be a string, not an array"),
    ('scheduler = "  "', "'scheduler' must not be empty"),
    ('scheduler = "pbs"\n[partition]\nx = 1', "'partition' must be a string, not a table"),
    ('scheduler = "pbs"\nacount = "x"', "unknown key 'acount' (did you mean 'account'?)"),
    ('scheduler = "pbs"\nfoo = "x"', "unknown key 'foo'; known keys"),
])
def test_invalid_values(dirs, text, message):
    environ, _, project = dirs
    pfile = write(project / 'cluster.toml', text + '\n')
    with pytest.raises(ConfigError) as e:
        load_cluster_settings(project, environ=environ)
    assert message in str(e.value)
    assert str(pfile) in str(e.value)


def test_error_names_file_the_bad_value_came_from(dirs):
    environ, user_file, project = dirs
    write(user_file, 'scheduler = "pbs"\naccount = 3\n')
    write(project / 'cluster.toml', 'partition = "q"\n')
    with pytest.raises(ConfigError) as e:
        load_cluster_settings(project, environ=environ)
    assert str(e.value).startswith(f'{user_file}:')


def test_values_are_stripped_except_setup(dirs):
    environ, _, project = dirs
    write(project / 'cluster.toml', 'scheduler = " pbs "\nsetup = "  x\\n"\n')
    s = load_cluster_settings(project, environ=environ)
    assert s.scheduler == 'pbs'
    assert s.setup == '  x\n'


# --- toml helpers ------------------------------------------------------------

def test_read_toml_missing(tmp_path):
    assert read_toml(tmp_path / 'nope.toml') is None


def test_merge_does_not_modify_inputs():
    base = {'a': {'b': 1}}
    override = {'a': {'c': 2}}
    merged, sources = merge(base, {('a', 'b'): Path('x')}, override, Path('y'))
    assert merged == {'a': {'b': 1, 'c': 2}}
    assert sources == {('a', 'b'): Path('x'), ('a', 'c'): Path('y')}
    assert base == {'a': {'b': 1}}
    assert override == {'a': {'c': 2}}
    merged['a']['d'] = 3
    assert override == {'a': {'c': 2}}


def test_merge_empty_table_keeps_base_values():
    merged, sources = merge({'a': {'b': 1}}, {('a', 'b'): Path('x')},
                            {'a': {}}, Path('y'))
    assert merged == {'a': {'b': 1}}
    assert sources == {('a', 'b'): Path('x')}


@pytest.mark.parametrize('key,expected', [
    (('a',), 'a'),
    (('pbs', 'select', 'ngpus'), 'pbs.select.ngpus'),
    (('a b', 'c'), '"a b".c'),
    (('',), '""'),
])
def test_format_key(key, expected):
    assert format_key(key) == expected


def test_merge_value_over_table_is_an_error():
    with pytest.raises(ConfigError, match="'a' is not a table, but it is a table in x"):
        merge({'a': {'b': 1}}, {('a', 'b'): Path('x')}, {'a': 1}, Path('y'))
