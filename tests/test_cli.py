import subprocess
import sys

import pytest

import jug_hpc
from jug_hpc.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(['--version'])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f'jug-hpc {jug_hpc.__version__}'


def test_help(capsys):
    with pytest.raises(SystemExit) as e:
        main(['--help'])
    assert e.value.code == 0
    assert 'usage: jug-hpc' in capsys.readouterr().out


def test_no_command_prints_usage(capsys):
    assert main([]) == 2
    assert 'usage: jug-hpc' in capsys.readouterr().err


def test_python_dash_m():
    out = subprocess.run(
        [sys.executable, '-m', 'jug_hpc', '--version'],
        capture_output=True, text=True, check=True)
    assert jug_hpc.__version__ in out.stdout


# --- jug-hpc config ----------------------------------------------------------

@pytest.fixture
def config_env(tmp_path, monkeypatch):
    '''A user cluster.toml (through XDG_CONFIG_HOME) and a project directory'''
    xdg = tmp_path / 'xdg'
    (xdg / 'jug_hpc').mkdir(parents=True)
    (xdg / 'jug_hpc' / 'cluster.toml').write_text('scheduler = "pbs"\n')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    project = tmp_path / 'project'
    (project / 'sub').mkdir(parents=True)
    (project / 'jug_hpc.toml').write_text(
        '[default]\nmemory = "4G"\ntime = "1h"\n'
        '[class.big]\nmatch = "jugfile.big"\nmemory = "64G"\n')
    return tmp_path, project


def test_config(config_env, capsys, monkeypatch):
    _, project = config_env
    monkeypatch.chdir(project / 'sub')
    assert main(['config']) == 0
    out, err = capsys.readouterr()
    assert err == ''
    assert 'scheduler = "pbs"  # user file' in out
    assert f'# Project settings: {project / "jug_hpc.toml"}' in out
    assert '[class.big]' in out


def test_config_project_dir_option(config_env, capsys):
    _, project = config_env
    assert main(['-C', str(project / 'sub'), 'config']) == 0
    out = capsys.readouterr().out
    assert f'# Project settings: {project / "jug_hpc.toml"}' in out
    assert f'#   project file: {project / "cluster.toml"} (not found)' in out


def test_config_no_project(config_env, capsys):
    tmp_path, _ = config_env
    assert main(['-C', str(tmp_path), 'config']) == 0
    out = capsys.readouterr().out
    assert 'scheduler = "pbs"' in out
    assert f'no jug_hpc.toml found in {tmp_path} or any parent directory' in out


def test_config_reports_every_error(config_env, capsys):
    tmp_path, project = config_env
    (tmp_path / 'xdg' / 'jug_hpc' / 'cluster.toml').write_text('schedular = "pbs"\n')
    (project / 'jug_hpc.toml').write_text('[default]\nmemory = "4G"\n')
    assert main(['-C', str(project), 'config']) == 1
    out, err = capsys.readouterr()
    assert out == ''
    errors = err.splitlines()
    assert len(errors) == 2
    assert errors[0].startswith('jug-hpc: error: ')
    assert "did you mean 'scheduler'?" in errors[0]
    assert "[default] must set 'time'" in errors[1]


def test_config_shows_what_is_valid(config_env, capsys):
    _, project = config_env
    (project / 'jug_hpc.toml').write_text('[default]\nmemory = "4G"\n')
    assert main(['-C', str(project), 'config']) == 1
    out, err = capsys.readouterr()
    assert 'scheduler = "pbs"' in out
    assert 'jug_hpc.toml' in err


def test_project_dir_must_exist(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(['-C', str(tmp_path / 'missing'), 'config'])
    assert e.value.code == 2
    assert 'not a directory' in capsys.readouterr().err
