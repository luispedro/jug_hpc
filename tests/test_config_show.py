import dataclasses
import datetime
import math
import tomllib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jug_hpc.config import (
    PROJECT_FILE,
    find_project_dir,
    load_cluster_settings,
    load_project_settings,
)
from jug_hpc.config.show import format_cluster_settings, format_project_settings
from jug_hpc.config.toml import format_key, format_value

PROJECT = r'''
jugfile = "analysis.py"
setup = """
source .venv/bin/activate
"""

[default]
memory = "4G"
time = "2h"

[default.pbs]
place = "free"

[class.big]
match = "analysis.assemble"
memory = "64G"
cpus = 16
partition = "bigmem"
time_fraction = 0.9

[class.big.pbs]
place = "excl"
hint = "x"

[class.fast]
pattern = '/^analysis\.(count|qc)/'
workers = 20
time = "1-12:00:00"
'''

USER_CLUSTER = '''
scheduler = "pbs"
account = "grp"
partition = "normal"
setup = """
module load python/3.12
"""

[pbs]
l = ["ncpus=1", "x"]
"weird key" = 3
'''

PROJECT_CLUSTER = '''
account = "other_grant"
[pbs]
extra = true
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def setup(tmp_path):
    '''(environ, user cluster file, project dir) with every file written'''
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    user_file = write(home / '.config' / 'jug_hpc' / 'cluster.toml', USER_CLUSTER)
    write(project / 'cluster.toml', PROJECT_CLUSTER)
    write(project / PROJECT_FILE, PROJECT)
    return {'HOME': str(home)}, user_file, project


def comment_of(text, line_start):
    '''The comment on the line of ``text`` that starts with ``line_start``'''
    [line] = [ln for ln in text.splitlines() if ln.startswith(line_start)]
    return line.partition('#')[2].strip() if '#' in line else None


def section(text, header):
    '''Lines of the TOML table ``header`` (up to the next blank line)'''
    lines = text.split('\n\n')
    [block] = [b for b in lines if b.startswith(header + '\n')]
    return block


# --- format_value ------------------------------------------------------------

toml_scalars = (
    st.booleans()
    | st.integers(min_value=-2**63, max_value=2**63 - 1)
    | st.floats(allow_nan=False)
    | st.text()
    | st.dates()
    | st.times()
    | st.datetimes())
toml_values = st.recursive(
    toml_scalars,
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(), children, max_size=4),
    max_leaves=10)


@given(st.text(min_size=1), toml_values, st.booleans())
def test_format_value_round_trips(key, value, multiline):
    text = f'{format_key((key,))} = {format_value(value, multiline=multiline)}'
    assert tomllib.loads(text) == {key: value}


def test_format_value_special_floats():
    assert format_value(math.inf) == 'inf'
    assert format_value(-math.inf) == '-inf'
    assert math.isnan(tomllib.loads(f'x = {format_value(math.nan)}')['x'])


def test_format_value_strings():
    assert format_value('a"b') == '"a\\"b"'
    assert format_value('tab\there') == '"tab\\there"'
    assert format_value('\x7f') == '"\\u007f"'
    # Backslashes: literal string if possible
    assert format_value(r'/a\.b/') == r"'/a\.b/'"
    assert format_value("it's \\") == '"it\'s \\\\"'
    # Newlines: multi-line literal only when asked
    assert format_value('a\nb\n') == '"a\\nb\\n"'
    assert format_value('a\nb\n', multiline=True) == "'''\na\nb\n'''"
    for s in ["ends in quote\n'", "has ''' in it\n", 'cr\r\n']:
        assert tomllib.loads(f'x = {format_value(s, multiline=True)}')['x'] == s
        assert not format_value(s, multiline=True).startswith("'''")


def test_format_value_others():
    assert format_value(True) == 'true'
    assert format_value([1, 'a']) == '[1, "a"]'
    assert format_value({}) == '{}'
    assert format_value({'a b': 1}) == '{ "a b" = 1 }'
    assert format_value(datetime.date(2026, 1, 2)) == '2026-01-02'
    with pytest.raises(TypeError):
        format_value(object())


# --- cluster settings --------------------------------------------------------

def test_cluster_shows_files_and_provenance(setup):
    environ, user_file, project = setup
    text = format_cluster_settings(load_cluster_settings(project, environ=environ))
    lines = text.splitlines()
    assert lines[0] == '# Cluster settings, merged from:'
    assert lines[1] == f'#   user file:    {user_file}'
    assert lines[2] == f'#   project file: {project / "cluster.toml"}'
    assert comment_of(text, 'scheduler =') == 'user file'
    assert comment_of(text, 'account =') == 'project file'
    assert comment_of(text, 'pbs.extra =') == 'project file'
    assert comment_of(text, 'pbs."weird key" =') == 'user file'
    assert "setup = '''\nmodule load python/3.12\n'''  " in text


def test_cluster_output_is_the_merged_toml(setup):
    environ, _, project = setup
    cluster = load_cluster_settings(project, environ=environ)
    assert tomllib.loads(format_cluster_settings(cluster)) == cluster.data


def test_cluster_missing_file_is_marked(setup):
    environ, _, project = setup
    (project / 'cluster.toml').unlink()
    text = format_cluster_settings(load_cluster_settings(project, environ=environ))
    assert f'#   project file: {project / "cluster.toml"} (not found)' in text
    assert 'project file' not in text.split('\n', 3)[3]


def test_cluster_without_project(setup):
    environ, user_file, _ = setup
    cluster = load_cluster_settings(None, environ=environ)
    assert cluster.searched == (user_file,)
    assert cluster.account == 'grp'
    text = format_cluster_settings(cluster)
    assert text.splitlines()[1] == f'#   user file: {user_file}'


# --- project settings --------------------------------------------------------

@pytest.fixture
def loaded(setup):
    environ, _, project = setup
    return (load_project_settings(project),
            load_cluster_settings(project, environ=environ))


def test_project_top_level(loaded):
    project, cluster = loaded
    text = format_project_settings(project, cluster)
    assert text.startswith(f'# Project settings: {project.path}\n')
    assert comment_of(text, 'jugfile =') is None
    assert 'jugfile = "analysis.py"' in text
    assert comment_of(text, 'logs =') == 'built-in default'
    assert "# jugdir: not set (Jug's default" in text
    assert "setup = '''\nsource .venv/bin/activate\n'''" in text


def test_project_class_provenance(loaded):
    project, cluster = loaded
    text = format_project_settings(project, cluster)

    default = section(text, '[default]')
    assert comment_of(default, 'cpus =') == 'built-in default'
    assert comment_of(default, 'memory =') is None
    assert comment_of(default, 'time =') is None
    assert comment_of(default, 'time_fraction =') == \
        'built-in default; JUG_MAX_SECONDS = 5760'
    assert '# partition: not set (uses the cluster\'s, "normal")' in default

    big = section(text, '[class.big]')
    assert comment_of(big, 'match =') is None
    assert comment_of(big, 'memory = "64G"') is None
    assert comment_of(big, 'time = "02:00:00"') == 'from [default]'
    assert comment_of(big, 'workers = 1') == 'built-in default'
    assert comment_of(big, 'time_fraction = 0.9') == 'JUG_MAX_SECONDS = 6480'
    assert comment_of(big, 'pbs.place = "excl"') is None
    assert comment_of(big, 'pbs.hint =') is None

    fast = section(text, '[class.fast]')
    assert r"pattern = '/^analysis\.(count|qc)/'" in fast
    assert 'time = "1-12:00:00"' in fast
    assert comment_of(fast, 'pbs.place = "free"') == 'from [default]'


def test_project_partition_without_cluster(loaded):
    project, _ = loaded
    text = format_project_settings(project)
    assert '# partition: not set (uses the scheduler default)' in text


def test_project_output_reloads_to_the_same_classes(loaded, tmp_path):
    project, cluster = loaded
    copy = tmp_path / 'copy'
    write(copy / PROJECT_FILE, format_project_settings(project, cluster))
    reloaded = load_project_settings(copy)

    def strip(rc):
        return dataclasses.replace(rc, origins={})

    assert [strip(rc) for rc in reloaded.all_classes()] == \
        [strip(rc) for rc in project.all_classes()]
    assert reloaded.jugfile == copy / 'analysis.py'
    assert reloaded.setup == project.setup


def test_project_path_outside_project_dir(tmp_path):
    write(tmp_path / 'p' / PROJECT_FILE,
          f'jugdir = "{tmp_path / "elsewhere"}"\n[default]\nmemory = "1G"\ntime = "1h"\n')
    text = format_project_settings(load_project_settings(tmp_path / 'p'))
    assert f'jugdir = "{tmp_path / "elsewhere"}"' in text


@pytest.mark.parametrize('jugdir, shown', [
    ('store', 'store'),
    ('redis://localhost:6379/', 'redis://localhost:6379/'),
    ('dict_store', 'dict_store'),
    ('dict_store:state.pkl', 'dict_store:state.pkl'),
    ('file_keepalive:data/store', 'file_keepalive:data/store'),
    ('file_keepalive:/elsewhere/store', 'file_keepalive:/elsewhere/store'),
])
def test_jugdir_backends(tmp_path, jugdir, shown):
    write(tmp_path / PROJECT_FILE,
          f'jugdir = "{jugdir}"\n[default]\nmemory = "1G"\ntime = "1h"\n')
    project = load_project_settings(tmp_path)
    text = format_project_settings(project)
    assert f'jugdir = "{shown}"' in text.splitlines()
    # The output reads back to the same settings (relative to the new place)
    write(tmp_path / 'copy' / PROJECT_FILE, text)
    reloaded = format_project_settings(load_project_settings(tmp_path / 'copy'))
    assert f'jugdir = "{shown}"' in reloaded.splitlines()


# --- finding the project -----------------------------------------------------

def test_find_project_dir(tmp_path):
    write(tmp_path / 'proj' / PROJECT_FILE, '')
    sub = tmp_path / 'proj' / 'a' / 'b'
    sub.mkdir(parents=True)
    assert find_project_dir(tmp_path / 'proj') == tmp_path / 'proj'
    assert find_project_dir(sub) == tmp_path / 'proj'
    assert find_project_dir(tmp_path) is None or find_project_dir(tmp_path) != tmp_path


def test_find_project_dir_nearest(tmp_path):
    write(tmp_path / PROJECT_FILE, '')
    write(tmp_path / 'inner' / PROJECT_FILE, '')
    assert find_project_dir(tmp_path / 'inner') == tmp_path / 'inner'


def test_find_project_dir_ignores_directories(tmp_path):
    (tmp_path / 'p' / PROJECT_FILE).mkdir(parents=True)
    assert find_project_dir(tmp_path / 'p') != tmp_path / 'p'


def test_find_project_dir_relative(tmp_path, monkeypatch):
    write(tmp_path / PROJECT_FILE, '')
    (tmp_path / 'sub').mkdir()
    monkeypatch.chdir(tmp_path / 'sub')
    assert find_project_dir('.') == tmp_path
