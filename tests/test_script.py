import os
import shlex
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from jug_hpc.config import load_cluster_settings, load_project_settings
from jug_hpc.script import (
    NR_WAIT_CYCLES,
    WAIT_CYCLE_TIME,
    ScriptVars,
    jug_execute_command,
    log_path,
    render_body,
    render_script,
)
from jug_hpc.worker import make_worker_spec

CLUSTER = '''
scheduler = "pbs"
setup     = "export FROM_CLUSTER=1"
'''

PROJECT = '''
setup = """
export FROM_PROJECT="it's here"
export JUG_MAX_MINUTES=600
"""

[default]
memory  = "4G"
time    = "100s"
time_fraction = 0.29

[class.align]
match   = "align"
cpus    = 8
time    = "1:00:00"
workers = 3

[class.merge]
pattern = "/^jf\\\\.merge$/"
'''

needs_bash = pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')


def make_project(root, project_toml=PROJECT, cluster_toml=CLUSTER):
    home = root / 'home'
    (home / '.config' / 'jug_hpc').mkdir(parents=True)
    (home / '.config' / 'jug_hpc' / 'cluster.toml').write_text(cluster_toml)
    project = root / "my proj's"   # space and quote in the path
    project.mkdir()
    (project / 'jug_hpc.toml').write_text(project_toml)
    cluster = load_cluster_settings(project, environ={'HOME': str(home)})
    return cluster, load_project_settings(project)


@pytest.fixture
def settings(tmp_path):
    return make_project(tmp_path)


def spec_for(settings, name, **kw):
    cluster, project = settings
    rc = project.get_class(name)
    command = jug_execute_command(project, rc, exclude=kw.pop('exclude', True))
    return make_worker_spec(cluster, project, rc, command, **kw)


VARS = ScriptVars(job_id='${FAKE_JOBID%%.*}', array_index='$FAKE_INDEX')


def test_command_class(settings):
    _, project = settings
    argv = jug_execute_command(project, project.get_class('align'))
    assert argv == (
        'jug', 'execute', '--name=align',
        f'--nr-wait-cycles={NR_WAIT_CYCLES}', f'--wait-cycle-time={WAIT_CYCLE_TIME}',
        f'--jugdir={project.project_dir}/jugfile.jugdata',
        '--', f'{project.project_dir}/jugfile.py')
    assert NR_WAIT_CYCLES * WAIT_CYCLE_TIME <= 5 * 60


def test_command_default(settings):
    _, project = settings
    argv = jug_execute_command(project, project.default, exclude=True, jug='/opt/jug',
                               nr_wait_cycles=2, wait_cycle_time=3)
    assert argv[:2] == ('/opt/jug', 'execute')
    assert argv[2:4] == ('--exclude-name=align', '--exclude-pattern=/^jf\\.merge$/')
    assert '--nr-wait-cycles=2' in argv
    assert '--wait-cycle-time=3' in argv
    argv = jug_execute_command(project, project.default, exclude=False)
    assert argv[2].startswith('--pattern=/^(?!')


@pytest.mark.parametrize('jugdir, expected', [
    ('store', '{p}/store'),
    ('redis://host:6379', 'redis://host:6379'),
    ('file_keepalive:store', 'file_keepalive:{p}/store'),
])
def test_command_jugdir(tmp_path, jugdir, expected):
    _, project = make_project(tmp_path, f'jugdir = "{jugdir}"\n' + PROJECT)
    argv = jug_execute_command(project, project.default, exclude=True)
    assert f'--jugdir={expected.format(p=project.project_dir)}' in argv


def test_body_order_and_values(settings):
    spec = spec_for(settings, 'default')
    assert spec.max_seconds == 29      # floor(0.29 * 100)
    body = render_body(spec, VARS)
    lines = body.splitlines()
    assert lines[0] == 'set -e'
    assert lines[1] == 'cd -- ' + shlex.quote(str(spec.workdir))
    assert 'export JUG_MAX_SECONDS=29' in lines
    unset = lines.index('unset JUG_MAX_HOURS JUG_MAX_MINUTES')
    assert lines[unset + 1] == 'export JUG_MAX_SECONDS=29'
    # setup snippets in order, before the limits; CPU variables before the setup
    cluster_setup = lines.index('export FROM_CLUSTER=1')
    project_setup = lines.index('export JUG_MAX_MINUTES=600')
    cpus = next(i for i, ln in enumerate(lines) if 'OMP_NUM_THREADS=1' in ln)
    assert cpus < cluster_setup < project_setup < unset
    assert lines[-1].startswith('exec jug execute ')
    assert shlex.split(lines[-1])[1:] == list(spec.command)
    assert body.endswith('\n')
    assert '$FAKE_INDEX' not in body          # not an array


def test_body_array(settings):
    spec = spec_for(settings, 'align')
    assert spec.is_array
    body = render_body(spec, VARS)
    assert 'JUG_HPC_ARRAY_INDEX="$FAKE_INDEX"' in body
    assert log_path(spec).endswith('/align-\'"$JUG_HPC_JOB_ID"-"$JUG_HPC_ARRAY_INDEX".log')
    assert 'OMP_NUM_THREADS=8' in body
    assert 'JUG_HPC_CPUS=8' in body
    with pytest.raises(ValueError, match='array_index'):
        render_body(spec, ScriptVars(job_id='$X'))


def test_body_requires_logs_and_command(settings):
    spec = spec_for(settings, 'merge')
    with pytest.raises(ValueError, match='logs'):
        render_body(replace(spec, logs=None), VARS)
    with pytest.raises(ValueError, match='command'):
        render_body(replace(spec, command=()), VARS)


@pytest.mark.parametrize('expr', ['a"b', 'a`b`', 'a\\', 'a\\\\\\'])
def test_script_vars_rejects_unsafe(expr):
    with pytest.raises(ValueError):
        ScriptVars(job_id=expr)
    with pytest.raises(ValueError):
        ScriptVars(job_id='$X', array_index=expr)


@pytest.mark.parametrize('expr', ['$X', '${X%%.*}', 'a\\"b', 'a\\\\', '$(echo 1)'])
def test_script_vars_accepts(expr):
    ScriptVars(job_id=expr)


def test_render_script(settings):
    spec = spec_for(settings, 'merge')
    script = render_script(spec, ['#PBS -N x', '#PBS -l walltime=00:01:40'], VARS)
    assert script.startswith('#!/bin/bash\n#PBS -N x\n#PBS -l walltime=00:01:40\n\nset -e\n')
    assert script.endswith(render_body(spec, VARS))
    assert render_script(spec, [], VARS).startswith('#!/bin/bash\n\nset -e\n')
    with pytest.raises(ValueError, match='newline'):
        render_script(spec, ['#PBS -N x\nrm -rf /'], VARS)


FAKE_JUG = '''#!/usr/bin/env python3
import json, os, sys
keys = ['JUG_MAX_HOURS', 'JUG_MAX_MINUTES', 'JUG_MAX_SECONDS', 'OMP_NUM_THREADS',
        'JUG_HPC_CPUS', 'JUG_HPC_CLASS', 'JUG_HPC_JOB_ID', 'JUG_HPC_ARRAY_INDEX',
        'FROM_CLUSTER', 'FROM_PROJECT']
print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),
                  'env': {k: os.environ.get(k) for k in keys}}))
'''


def run_script(tmp_path, spec, environ=None):
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    (bindir / 'jug').write_text(FAKE_JUG)
    (bindir / 'jug').chmod(0o755)
    script = tmp_path / 'worker.sh'
    script.write_text(render_script(spec, ['#PBS -N x'], VARS))
    env = {'PATH': f'{bindir}:{os.environ["PATH"]}', 'HOME': str(tmp_path),
           'FAKE_JOBID': '1234.server', 'FAKE_INDEX': '2',
           'JUG_MAX_HOURS': '10', 'JUG_MAX_MINUTES': '10', **(environ or {})}
    r = subprocess.run(['bash', str(script)], check=False, env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=30)
    return r


@needs_bash
def test_run_script(tmp_path, settings):
    import json
    spec = spec_for(settings, 'merge')
    r = run_script(tmp_path, spec)
    assert r.returncode == 0, r.stderr
    assert r.stdout == r.stderr == ''           # everything goes to the log
    log = spec.logs / 'merge-1234.log'
    out = json.loads(log.read_text())
    assert out['argv'] == list(spec.command[1:])
    assert out['cwd'] == str(spec.workdir)
    assert out['env'] == {
        'JUG_MAX_HOURS': None, 'JUG_MAX_MINUTES': None, 'JUG_MAX_SECONDS': '29',
        'OMP_NUM_THREADS': '1', 'JUG_HPC_CPUS': '1', 'JUG_HPC_CLASS': 'merge',
        'JUG_HPC_JOB_ID': '1234', 'JUG_HPC_ARRAY_INDEX': None,
        'FROM_CLUSTER': '1', 'FROM_PROJECT': "it's here",
    }
    # a second run of the same job appends to the log
    run_script(tmp_path, spec)
    assert len(log.read_text().splitlines()) == 2


@needs_bash
def test_run_script_array(tmp_path, settings):
    import json
    spec = spec_for(settings, 'align')
    r = run_script(tmp_path, spec)
    assert r.returncode == 0, r.stderr
    out = json.loads((spec.logs / 'align-1234-2.log').read_text())
    assert out['env']['JUG_HPC_ARRAY_INDEX'] == '2'
    assert out['env']['OMP_NUM_THREADS'] == '8'
    assert out['argv'][1] == '--name=align'


@needs_bash
def test_run_script_setup_overrides_cpus_and_fails(tmp_path):
    import json
    project_toml = PROJECT.replace('export JUG_MAX_MINUTES=600',
                                   'export OMP_NUM_THREADS=2')
    settings = make_project(tmp_path, project_toml)
    spec = spec_for(settings, 'merge')
    assert run_script(tmp_path, spec).returncode == 0
    out = json.loads((spec.logs / 'merge-1234.log').read_text())
    assert out['env']['OMP_NUM_THREADS'] == '2'
    assert out['env']['JUG_HPC_CPUS'] == '1'

    # a failing setup stops the worker before jug runs
    failing = replace(spec, setup=('false',))
    r = run_script(tmp_path, failing, {'FAKE_JOBID': '99'})
    assert r.returncode != 0
    assert (spec.logs / 'merge-99.log').read_text() == ''


@needs_bash
def test_run_script_quoting(tmp_path, settings):
    '''Every value reaches jug unchanged, whatever characters it contains'''
    import json
    spec = spec_for(settings, 'merge')
    nasty = ('jug', 'execute', "--name=a'b", '--pattern=/$(touch pwned)`x`;"/',
             '--', 'a b\\c\n*.py')
    spec = replace(spec, command=nasty, class_name="c'l $x")
    r = run_script(tmp_path, spec)
    assert r.returncode == 0, r.stderr
    (log,) = Path(spec.logs).glob('c*-1234.log')
    assert log.name == "c'l $x-1234.log"
    out = json.loads(log.read_text())
    assert out['argv'] == list(nasty[1:])
    assert out['env']['JUG_HPC_CLASS'] == "c'l $x"
    assert not (spec.workdir / 'pwned').exists()
