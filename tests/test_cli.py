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
