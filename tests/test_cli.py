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


def test_config_checks_scheduler_settings(config_env, capsys):
    tmp_path, project = config_env
    user = tmp_path / 'xdg' / 'jug_hpc' / 'cluster.toml'
    user.write_text('scheduler = "pbs"\n[pbs.select]\nncpus = 2\n[pbz]\na = 1\n')
    with (project / 'jug_hpc.toml').open('a') as f:
        f.write('[class.big.pbs]\ndirectives = "-m n"\n')
    assert main(['-C', str(project), 'config']) == 1
    out, err = capsys.readouterr()
    assert '[class.big]' in out     # everything is still shown
    assert err.splitlines() == [
        (f"jug-hpc: warning: {user}: [pbz] is ignored: no scheduler backend named 'pbz' "
         "is installed (did you mean 'pbs'? available: local, pbs)"),
        (f"jug-hpc: error: {user}: [pbs]: 'select.ncpus' cannot be set as a raw option: "
         'use cpus in jug_hpc.toml'),
        (f"jug-hpc: error: {project / 'jug_hpc.toml'}: [class.big.pbs]: 'directives' "
         'must be a list of strings (qsub options)'),
    ]


def test_config_unknown_scheduler(config_env, capsys):
    tmp_path, project = config_env
    user = tmp_path / 'xdg' / 'jug_hpc' / 'cluster.toml'
    user.write_text('scheduler = "slurmm"\n')
    assert main(['-C', str(project), 'config']) == 1
    out, err = capsys.readouterr()
    assert 'scheduler = "slurmm"' in out
    assert err.startswith(f"jug-hpc: error: {user}: unknown scheduler 'slurmm' (")


def test_project_dir_must_exist(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(['-C', str(tmp_path / 'missing'), 'config'])
    assert e.value.code == 2
    assert 'not a directory' in capsys.readouterr().err


# --- jug-hpc check -----------------------------------------------------------

@pytest.fixture
def check_project(tmp_path):
    (tmp_path / 'jugfile.py').write_text(
        'from jug import TaskGenerator\n'
        '@TaskGenerator\ndef small(i):\n    return i\n'
        '@TaskGenerator\ndef big(xs):\n    return xs\n'
        'big([small(i) for i in range(2)])\n')
    (tmp_path / 'sub').mkdir()
    return tmp_path


def write_toml(project, classes=''):
    (project / 'jug_hpc.toml').write_text(
        f'[default]\nmemory = "4G"\ntime = "1h"\n{classes}')


def test_check(check_project, capsys, monkeypatch):
    write_toml(check_project, '[class.big]\nmatch = "big"\nmemory = "64G"\n')
    monkeypatch.chdir(check_project / 'sub')
    assert main(['check']) == 0
    out, err = capsys.readouterr()
    assert err == ''
    assert out.splitlines() == [
        'Jugfile: jugfile.py (3 tasks)',
        '',
        "[class.big]  match = 'big'  (1 task)",
        '  1  jugfile.big',
        '',
        '[default]  (2 tasks)',
        '  2  jugfile.small',
    ]


def test_check_does_not_need_cluster_settings(check_project, capsys, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(check_project / 'no-such-dir'))
    write_toml(check_project)
    assert main(['-C', str(check_project), 'check']) == 0


def test_check_overlap(check_project, capsys):
    write_toml(check_project,
               '[class.a]\nmatch = "big"\n[class.b]\npattern = "/^jugfile\\\\.b/"\n'
               '[class.c]\nmatch = "nothing"\n')
    assert main(['-C', str(check_project), 'check']) == 1
    out, err = capsys.readouterr()
    assert 'Matched by several classes (error)  (1 task)' in out
    errors = err.splitlines()
    assert len(errors) == 2
    assert errors[0].startswith('jug-hpc: warning: ')
    assert '[class.c]' in errors[0]
    assert errors[1].startswith('jug-hpc: error: ')
    assert "task 'jugfile.big' is matched by 2 classes" in errors[1]


def test_check_barrier_note(check_project, capsys):
    (check_project / 'jugfile.py').write_text(
        'from jug import TaskGenerator, barrier\n'
        '@TaskGenerator\ndef f(i):\n    return i\n'
        'f(1)\nbarrier()\nf(2)\n')
    write_toml(check_project)
    assert main(['-C', str(check_project), 'check']) == 0
    out, err = capsys.readouterr()
    assert '[default]  (1 task)' in out
    assert err.startswith('jug-hpc: note: loading stopped at a barrier()')


def test_check_no_project(tmp_path, capsys):
    assert main(['-C', str(tmp_path), 'check']) == 1
    err = capsys.readouterr().err
    assert f'no jug_hpc.toml found in {tmp_path}' in err


def test_check_config_error(check_project, capsys):
    (check_project / 'jug_hpc.toml').write_text('[default]\nmemory = "4G"\n')
    assert main(['-C', str(check_project), 'check']) == 1
    assert "[default] must set 'time'" in capsys.readouterr().err


def test_check_jugfile_error(check_project, capsys):
    (check_project / 'jugfile.py').write_text('raise ValueError("oops")\n')
    write_toml(check_project)
    assert main(['-C', str(check_project), 'check']) == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert err.startswith('Traceback')
    assert 'jugstate.py' not in err
    assert err.splitlines()[-1].startswith('jug-hpc: error: ')
    assert 'ValueError: oops' in err.splitlines()[-1]
