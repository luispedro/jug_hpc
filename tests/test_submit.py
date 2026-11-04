import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from jug_hpc.cli import main
from jug_hpc.config import ConfigError, load_cluster_settings, load_project_settings
from jug_hpc.jugstate import load_project
from jug_hpc.runner import FakeRunner
from jug_hpc.schedulers import JobState, Outcome, SchedulerError, create_scheduler
from jug_hpc.schedulers.local import LocalScheduler
from jug_hpc.state import ProjectState, StateError
from jug_hpc.submit import (
    check_jugdir,
    format_dry_run,
    prepare,
    submit_worker,
    write_scripts,
)

JUGFILES = Path(__file__).parent / 'jugfiles'

needs_bash = pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')

PROJECT = '''
[default]
memory  = "1G"
time    = "1h"
workers = 10

[class.align]
match   = "align_sample"
cpus    = 4
workers = 2

[class.unused]
match = "nothing"
'''


def make_project(tmp_path, toml=PROJECT, jugfile='simple.py', cluster='scheduler = "pbs"\n'):
    xdg = tmp_path / 'xdg'
    (xdg / 'jug_hpc').mkdir(parents=True, exist_ok=True)
    (xdg / 'jug_hpc' / 'cluster.toml').write_text(cluster)
    project_dir = tmp_path / 'proj'
    project_dir.mkdir(exist_ok=True)
    shutil.copy(JUGFILES / jugfile, project_dir / 'jugfile.py')
    (project_dir / 'jug_hpc.toml').write_text(toml)
    return xdg, project_dir


@pytest.fixture
def loaded(tmp_path):
    '''Load a project: returns a function giving (cluster, project, state, scheduler)'''
    def load(toml=PROJECT, jugfile='simple.py', cluster='scheduler = "pbs"\n', runner=None):
        xdg, project_dir = make_project(tmp_path, toml, jugfile, cluster)
        c = load_cluster_settings(project_dir, environ={'XDG_CONFIG_HOME': str(xdg)})
        p = load_project_settings(project_dir)
        return c, p, load_project(p), create_scheduler(c, runner or FakeRunner())
    return load


# --- prepare --------------------------------------------------------------------

def test_prepare(loaded):
    prep = prepare(*loaded())
    assert [(w.class_name, w.tasks, w.spec.workers) for w in prep.workers] == [
        ('align', 3, 2), ('default', 4, 4)]
    assert prep.notes == [
        '[class.unused]: no tasks, not submitted',
        '[default]: 4 tasks, so 4 workers instead of 10',
    ]
    assert prep.warnings == []
    align, default = prep.workers
    assert '#PBS -J 1-2' in align.script
    assert 'ncpus=4' in align.script
    assert 'exec jug execute --keep-failed --name=align_sample ' in align.script
    assert '#PBS -J 1-4' in default.script
    assert 'align_sample' in default.script      # excluded from the default class


def test_prepare_selected_classes(loaded):
    prep = prepare(*loaded(), classes=['unused', 'default'])
    # in the order of the file; a class named explicitly is submitted without
    # tasks, with its configured workers (inherited from [default])
    assert [(w.class_name, w.tasks, w.spec.workers) for w in prep.workers] == [
        ('unused', 0, 10), ('default', 4, 4)]


def test_prepare_unknown_class(loaded):
    with pytest.raises(ConfigError, match=r"no class 'algin' \(classes: align, unused, "
                                          r'default\)'):
        prepare(*loaded(), classes=['algin'])


def test_prepare_barrier_keeps_configured_workers(loaded):
    toml = '[default]\nmemory = "1G"\ntime = "1h"\nworkers = 3\n' \
        '[class.later]\nmatch = "nothing"\n'
    prep = prepare(*loaded(toml, jugfile='barrier.py'))
    assert [(w.class_name, w.spec.workers) for w in prep.workers] == [
        ('later', 3), ('default', 3)]
    assert len(prep.notes) == 1
    assert prep.notes[0].startswith('loading stopped at a barrier()')


def test_prepare_overlap(loaded):
    toml = PROJECT + '[class.again]\npattern = "align"\n'
    with pytest.raises(ConfigError, match="'jugfile.align_sample' is matched by 2 classes"):
        prepare(*loaded(toml))


def test_prepare_scheduler_settings(loaded):
    toml = PROJECT + '[class.align.pbs]\nselct = {ngpus = 1}\n[class.align.slurmm]\nx = 1\n'
    with pytest.raises(ConfigError, match="'selct' is not a PBS raw option"):
        prepare(*loaded(toml))
    toml = PROJECT + '[class.align.slurmm]\nx = 1\n'
    prep = prepare(*loaded(toml))
    assert len(prep.warnings) == 1
    assert '[class.align.slurmm] is ignored' in prep.warnings[0]


@pytest.mark.parametrize('jugdir', ['dict_store', 'dict_store:results.pkl'])
def test_prepare_rejects_dict_store(loaded, jugdir):
    with pytest.raises(ConfigError, match="'jugdir' is .*dict_store.* cannot share"):
        prepare(*loaded(f'jugdir = "{jugdir}"\n' + PROJECT))


@pytest.mark.parametrize('jugdir', ['data.jugdata', 'file_keepalive:data', 'redis://host'])
def test_check_jugdir_accepts(tmp_path, jugdir):
    _, project_dir = make_project(tmp_path, f'jugdir = "{jugdir}"\n' + PROJECT)
    check_jugdir(load_project_settings(project_dir))


# --- write, submit --------------------------------------------------------------

def test_write_scripts(loaded):
    cluster, project, state, sched = loaded()
    prep = prepare(cluster, project, state, sched)
    ps = ProjectState(project.project_dir)
    written = write_scripts(prep, sched, ps)
    assert [w.script for w in written] == [
        Path('.jug_hpc/scripts/align-1.sh'), Path('.jug_hpc/scripts/default-1.sh')]
    assert [w.command for w in written] == [
        ('qsub', '.jug_hpc/scripts/align-1.sh'), ('qsub', '.jug_hpc/scripts/default-1.sh')]
    for w in written:
        assert w.path == project.project_dir / w.script
        assert w.path.read_text() == w.prepared.script
    assert (project.project_dir / 'logs' / 'scheduler').is_dir()
    assert ps.jobs() == []
    # writing again gives new scripts
    assert write_scripts(prep, sched, ps)[0].script.name == 'align-2.sh'


def test_write_scripts_log_dir_fails(loaded):
    cluster, project, state, sched = loaded()
    (project.project_dir / 'logs').write_text('not a directory')
    with pytest.raises(StateError, match='cannot create the log directory'):
        write_scripts(prepare(cluster, project, state, sched), sched,
                      ProjectState(project.project_dir))


def test_submit_worker(loaded):
    runner = FakeRunner()
    runner.on('qsub', stdout='1234[].pbs01\n')
    cluster, project, state, sched = loaded(runner=runner)
    ps = ProjectState(project.project_dir)
    w = write_scripts(prepare(cluster, project, state, sched), sched, ps)[0]
    record = submit_worker(w, sched, ps)
    assert runner.calls[-1].argv == ('qsub', '.jug_hpc/scripts/align-1.sh')
    assert runner.calls[-1].cwd == project.project_dir
    assert (record.job_id, record.class_name, record.scheduler, record.workers,
            record.script) == ('1234[].pbs01', 'align', 'pbs', 2,
                               Path('.jug_hpc/scripts/align-1.sh'))
    assert ps.jobs() == [record]


def test_submit_worker_fails(loaded):
    runner = FakeRunner()
    runner.on('qsub', returncode=1, stderr='qsub: Unknown queue')
    cluster, project, state, sched = loaded(runner=runner)
    ps = ProjectState(project.project_dir)
    w = write_scripts(prepare(cluster, project, state, sched), sched, ps)[0]
    with pytest.raises(SchedulerError, match='qsub: Unknown queue'):
        submit_worker(w, sched, ps)
    assert ps.jobs() == []


def test_submit_worker_not_recorded(loaded):
    runner = FakeRunner()
    runner.on('qsub', stdout='77.pbs01\n')
    cluster, project, state, sched = loaded(runner=runner)
    ps = ProjectState(project.project_dir)
    w = write_scripts(prepare(cluster, project, state, sched), sched, ps)[0]
    ps.jobs_file.write_text('{"version": 99, "jobs": []}')
    with pytest.raises(StateError, match=r'job 77.pbs01 \(\[class.align\]\) was submitted '
                                         'but could not be recorded: .*newer version'):
        submit_worker(w, sched, ps)


def test_format_dry_run(loaded):
    cluster, project, state, sched = loaded()
    prep = prepare(cluster, project, state, sched)
    written = write_scripts(prep, sched, ProjectState(project.project_dir))
    text = format_dry_run(written, project.project_dir)
    lines = text.splitlines()
    assert lines[0] == '==> .jug_hpc/scripts/align-1.sh <=='
    assert lines[1] == '#!/bin/bash'
    assert text.endswith(
        f'To submit these scripts, run from {project.project_dir}:\n'
        '  # [class.align]: 3 tasks, 2 workers\n'
        '  qsub .jug_hpc/scripts/align-1.sh\n'
        '  # [default]: 4 tasks, 4 workers\n'
        '  qsub .jug_hpc/scripts/default-1.sh')
    assert written[1].prepared.script in text
    assert format_dry_run([], project.project_dir) == 'Nothing to submit.'


# --- CLI ------------------------------------------------------------------------

def test_cli_dry_run(tmp_path, monkeypatch, capsys):
    xdg, project_dir = make_project(tmp_path)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    assert main(['-C', str(project_dir), 'submit', '--dry-run']) == 0
    out, err = capsys.readouterr()
    assert out.startswith('==> .jug_hpc/scripts/align-1.sh <==\n#!/bin/bash\n#PBS -N ')
    assert out.endswith('  qsub .jug_hpc/scripts/default-1.sh\n')
    assert err.splitlines() == [
        'jug-hpc: note: [class.unused]: no tasks, not submitted',
        'jug-hpc: note: [default]: 4 tasks, so 4 workers instead of 10',
    ]
    assert not (project_dir / '.jug_hpc' / 'jobs.json').exists()


def test_cli_errors(tmp_path, monkeypatch, capsys):
    assert main(['-C', str(tmp_path), 'submit', '--all']) == 1
    assert 'no jug_hpc.toml found' in capsys.readouterr().err

    xdg, project_dir = make_project(tmp_path, cluster='scheduler = "pbss"\n')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    assert main(['-C', str(project_dir), 'submit', '--all']) == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert "unknown scheduler 'pbss' (did you mean 'pbs'?" in err

    (xdg / 'jug_hpc' / 'cluster.toml').write_text('scheduler = "pbs"\n')
    (project_dir / 'jugfile.py').write_text('raise ValueError("oops")\n')
    assert main(['-C', str(project_dir), 'submit', '--dry-run']) == 1
    err = capsys.readouterr().err
    assert err.startswith('Traceback')
    assert err.splitlines()[-1].endswith('ValueError: oops')


def test_cli_submission_fails(tmp_path, monkeypatch, capsys):
    '''No qsub on PATH: the first submission fails, the rest is not attempted'''
    xdg, project_dir = make_project(tmp_path)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    assert main(['-C', str(project_dir), 'submit', '--all']) == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert 'qsub: command not found' in err
    assert err.splitlines()[-1] == 'jug-hpc: note: not submitted: default'
    assert ProjectState(project_dir).jobs() == []


INDEPENDENT = '''
from jug import TaskGenerator

@TaskGenerator
def small(i):
    return i

@TaskGenerator
def big(i):
    return i * 10

[small(i) for i in range(3)]
[big(i) for i in range(2)]
'''


@needs_bash
def test_cli_all_runs_jugfile(tmp_path, monkeypatch, capsys):
    '''``submit --all`` with the local backend runs a jugfile to completion'''
    toml = ('setup = "export PATH=' + str(Path(sys.executable).parent) + ':$PATH"\n'
            '[default]\nmemory = "1G"\ntime = "1h"\nworkers = 2\n'
            '[class.big]\nmatch = "big"\nworkers = 5\n')
    xdg, project_dir = make_project(tmp_path, toml, cluster='scheduler = "local"\n')
    (project_dir / 'jugfile.py').write_text(INDEPENDENT)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        'big: submitted job 1 (2 workers, .jug_hpc/scripts/big-1.sh)',
        'default: submitted job 2 (2 workers, .jug_hpc/scripts/default-1.sh)',
    ]
    assert err == 'jug-hpc: note: [class.big]: 2 tasks, so 2 workers instead of 5\n'

    records = ProjectState(project_dir).jobs()
    assert [(r.job_id, r.class_name, r.scheduler) for r in records] == [
        ('1', 'big', 'local'), ('2', 'default', 'local')]
    cluster = load_cluster_settings(project_dir, environ={'XDG_CONFIG_HOME': str(xdg)})
    sched = LocalScheduler(cluster, jobs_dir=tmp_path / 'state' / 'jug_hpc' / 'local')
    jobs = [r.job_id for r in records]
    deadline = time.monotonic() + 120
    while any(s.state is not JobState.FINISHED for s in sched.query(jobs).values()):
        assert time.monotonic() < deadline, 'jobs did not finish'
        time.sleep(0.1)
    logs = '\n'.join(p.read_text() for p in (project_dir / 'logs').glob('*.log'))
    for outcome in sched.outcome(jobs).values():
        assert outcome.outcome is Outcome.SUCCESS, logs
    state = load_project(load_project_settings(project_dir))
    assert len(state.tasks) == 5
    assert all(t.can_load(state.store) for t in state.tasks)
    info = json.loads((tmp_path / 'state' / 'jug_hpc' / 'local' / '1' / 'info.json')
                      .read_text())
    assert info['cwd'] == os.fspath(project_dir)
