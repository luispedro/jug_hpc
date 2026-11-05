'''The PBS backend against a fake ``qsub``, ``qstat`` and ``qdel`` on ``PATH``

The fakes (in ``tests/fake_pbs/``) record how they were called, and the fake
``qsub`` runs each job before returning, the way PBS would: from ``$HOME``,
with a clean environment and PBS's variables, and one run per array index. So
these tests check what the unit tests in ``test_schedulers_pbs.py`` cannot:
that the commands jug_hpc runs (or prints with ``--dry-run``) submit scripts
that run a jugfile to completion, and that jobs are queried and cancelled with
commands that PBS understands.
'''
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jug_hpc.cli import main
from jug_hpc.config import ClusterSettings, load_project_settings
from jug_hpc.jugstate import load_project
from jug_hpc.schedulers import JobId, JobOutcome, JobState, JobStatus, Outcome
from jug_hpc.schedulers.pbs import PBSScheduler
from jug_hpc.state import ProjectState

FAKE_PBS = Path(__file__).parent / 'fake_pbs'

pytestmark = pytest.mark.skipif(
    any(shutil.which(p) is None for p in ('bash', 'awk', 'timeout', 'seq')),
    reason='the fake qsub needs bash, awk, timeout and seq')

# PBS jobs do not inherit the submission environment, so setup must find jug
PROJECT = f'''
setup = "export PATH={Path(sys.executable).parent}:$PATH"

[default]
memory  = "1G"
time    = "1h"
workers = 2

[class.big]
match   = "big"
cpus    = 2
workers = 5

[class.one]
match   = "one"
'''

JUGFILE = '''
from jug import TaskGenerator

@TaskGenerator
def small(i):
    return i

@TaskGenerator
def big(i):
    return i * 10

@TaskGenerator
def one():
    return 1

[small(i) for i in range(3)]
[big(i) for i in range(2)]
one()
'''


@pytest.fixture
def fake_pbs(tmp_path, monkeypatch):
    '''A project and a fake PBS: returns (project_dir, fake_pbs_dir)'''
    xdg = tmp_path / 'xdg'
    (xdg / 'jug_hpc').mkdir(parents=True)
    (xdg / 'jug_hpc' / 'cluster.toml').write_text('scheduler = "pbs"\n')
    project_dir = tmp_path / 'proj'
    project_dir.mkdir()
    (project_dir / 'jugfile.py').write_text(JUGFILE)
    (project_dir / 'jug_hpc.toml').write_text(PROJECT)
    pbs_dir = tmp_path / 'pbs'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    monkeypatch.setenv('FAKE_PBS_DIR', str(pbs_dir))
    monkeypatch.setenv('PATH', f'{FAKE_PBS}{os.pathsep}{os.environ["PATH"]}')
    return project_dir, pbs_dir


def qsub_calls(pbs_dir, command='qsub'):
    '''(cwd, arguments) of each qsub (or qstat, or qdel) call'''
    lines = (pbs_dir / f'{command}.log').read_text().splitlines()
    return [(Path(cwd), args.split('\t')) for cwd, args in
            (line.split('\t', 1) for line in lines)]


def exit_statuses(pbs_dir):
    '''Exit status of every job and array element: dict name -> int'''
    return {p.stem: int(p.read_text()) for p in (pbs_dir / 'jobs').glob('*.exit')}


def assert_all_done(project_dir):
    state = load_project(load_project_settings(project_dir))
    assert len(state.tasks) == 6
    assert all(t.can_load(state.store) for t in state.tasks)


def worker_logs(project_dir):
    return '\n'.join(f'==> {p.name} <==\n{p.read_text()}'
                     for p in sorted((project_dir / 'logs').rglob('*.*')))


def test_submit_all(fake_pbs, capsys):
    project_dir, pbs_dir = fake_pbs
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        'big: submitted job 1001[].fakepbs (2 workers, .jug_hpc/scripts/big-1.sh)',
        'one: submitted job 1002.fakepbs (1 worker, .jug_hpc/scripts/one-1.sh)',
        'default: submitted job 1003[].fakepbs (2 workers, .jug_hpc/scripts/default-1.sh)',
    ]
    assert 'note: [class.big]: 2 tasks, so 2 workers instead of 5' in err

    # qsub runs from the project directory, with the script relative to it
    assert qsub_calls(pbs_dir) == [
        (project_dir, ['.jug_hpc/scripts/big-1.sh']),
        (project_dir, ['.jug_hpc/scripts/one-1.sh']),
        (project_dir, ['.jug_hpc/scripts/default-1.sh']),
    ]
    assert exit_statuses(pbs_dir) == {
        '1001-1': 0, '1001-2': 0, '1002': 0, '1003-1': 0, '1003-2': 0}, \
        worker_logs(project_dir)
    assert_all_done(project_dir)

    # the worker logs are named after the job ID without the server name, and
    # PBS's own output files went to logs/scheduler/
    logs = project_dir / 'logs'
    assert sorted(p.name for p in logs.glob('*.log')) == [
        'big-1001-1.log', 'big-1001-2.log', 'default-1003-1.log', 'default-1003-2.log',
        'one-1002.log']
    assert sorted(p.name for p in (logs / 'scheduler').iterdir()) == sorted(
        f'{n}.{ext}' for n in ('1001-1', '1001-2', '1002', '1003-1', '1003-2')
        for ext in ('OU', 'ER'))

    records = ProjectState(project_dir).jobs()
    assert [(r.job_id, r.class_name, r.scheduler, r.workers) for r in records] == [
        ('1001[].fakepbs', 'big', 'pbs', 2),
        ('1002.fakepbs', 'one', 'pbs', 1),
        ('1003[].fakepbs', 'default', 'pbs', 2),
    ]


def test_dry_run_commands_submit_by_hand(fake_pbs, capsys):
    '''The commands printed by ``--dry-run``, run in a shell, run the jugfile'''
    project_dir, pbs_dir = fake_pbs
    assert main(['-C', str(project_dir), 'submit', '--dry-run']) == 0
    out = capsys.readouterr().out
    _, commands = out.split('To submit these scripts, run from ')
    where, commands = commands.split(':\n', 1)
    assert Path(where) == project_dir
    commands = [c.strip() for c in commands.splitlines() if not c.strip().startswith('#')]
    assert [shlex.split(c)[0] for c in commands] == ['qsub'] * 3
    assert not (pbs_dir / 'qsub.log').exists()

    for c in commands:
        subprocess.run(c, shell=True, check=True, cwd=where, timeout=300)
    assert sorted(exit_statuses(pbs_dir).values()) == [0] * 5, worker_logs(project_dir)
    assert_all_done(project_dir)
    assert ProjectState(project_dir).jobs() == []       # nothing was recorded


def test_qsub_fails(fake_pbs, capsys):
    project_dir, pbs_dir = fake_pbs
    pbs_dir.mkdir()
    (pbs_dir / 'fail').write_text('qsub: Unknown queue\n')
    assert main(['-C', str(project_dir), 'submit', '--all']) == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert 'pbs: could not submit .jug_hpc/scripts/big-1.sh' in err
    assert 'qsub: Unknown queue' in err
    assert err.splitlines()[-1] == 'jug-hpc: note: not submitted: one, default'
    assert len(qsub_calls(pbs_dir)) == 1
    assert ProjectState(project_dir).jobs() == []


def query(project_dir):
    '''The status of every job recorded for the project, from the fake qstat'''
    jobs = [r.job_id for r in ProjectState(project_dir).jobs()]
    return PBSScheduler(ClusterSettings(scheduler='pbs')).query(jobs)


def test_query_finished(fake_pbs):
    project_dir, pbs_dir = fake_pbs
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    finished = JobStatus(JobState.FINISHED)
    assert query(project_dir) == {
        '1001[].fakepbs': finished, '1002.fakepbs': finished, '1003[].fakepbs': finished}
    assert qsub_calls(pbs_dir, 'qstat')[-1][1] == [
        '-f', '-F', 'json', '-x', '1001[].fakepbs', '1002.fakepbs', '1003[].fakepbs']


def test_query_held(fake_pbs):
    project_dir, pbs_dir = fake_pbs
    pbs_dir.mkdir()
    (pbs_dir / 'hold').touch()
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    assert query(project_dir) == {
        '1001[].fakepbs': JobStatus(JobState.QUEUED, queued=2),
        '1002.fakepbs': JobStatus(JobState.QUEUED, queued=1),
        '1003[].fakepbs': JobStatus(JobState.QUEUED, queued=2),
    }
    assert exit_statuses(pbs_dir) == {}     # nothing ran


@pytest.mark.usefixtures('fake_pbs')
def test_query_unknown():
    pbs = PBSScheduler(ClusterSettings(scheduler='pbs'))
    assert pbs.query([JobId('1001.fakepbs'), JobId('77[].fakepbs')]) == {
        '1001.fakepbs': JobStatus(JobState.UNKNOWN), '77[].fakepbs': JobStatus(JobState.UNKNOWN)}


def outcomes(project_dir):
    '''How every job recorded for the project ended, from the fake qstat'''
    jobs = [r.job_id for r in ProjectState(project_dir).jobs()]
    return PBSScheduler(ClusterSettings(scheduler='pbs')).outcome(jobs)


def test_outcome_success(fake_pbs):
    project_dir, pbs_dir = fake_pbs
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    success = JobOutcome(Outcome.SUCCESS, 0)
    assert outcomes(project_dir) == {
        '1001[].fakepbs': success, '1002.fakepbs': success, '1003[].fakepbs': success}
    assert qsub_calls(pbs_dir, 'qstat')[-1][1] == [
        '-f', '-F', 'json', '-x', '-t', '1001[].fakepbs', '1002.fakepbs', '1003[].fakepbs']


def test_outcome_failed_setup(fake_pbs):
    '''Workers whose setup fails end with its exit status'''
    project_dir, _ = fake_pbs
    toml = project_dir / 'jug_hpc.toml'
    toml.write_text(toml.read_text().replace('setup = "', 'setup = "exit 3; '))
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    failed = JobOutcome(Outcome.FAILED, 3, None, 'exit status 3')
    both_failed = JobOutcome(Outcome.FAILED, 3, None, (
        '2 of 2 workers did not succeed (workers 1, 2: exit status 3)'))
    assert outcomes(project_dir) == {
        '1001[].fakepbs': both_failed, '1002.fakepbs': failed, '1003[].fakepbs': both_failed}


def test_outcome_unknown_and_held(fake_pbs):
    '''Jobs that have not finished, or are unknown, are left out'''
    project_dir, pbs_dir = fake_pbs
    pbs_dir.mkdir()
    (pbs_dir / 'hold').touch()
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    jobs = [r.job_id for r in ProjectState(project_dir).jobs()]
    pbs = PBSScheduler(ClusterSettings(scheduler='pbs'))
    assert pbs.outcome([*jobs, JobId('77.fakepbs')]) == {}


def test_fake_qstat_without_history(fake_pbs):
    '''Like PBS, the fake qstat lists finished jobs only with -x'''
    project_dir, _ = fake_pbs
    assert main(['-C', str(project_dir), 'submit', '--all', 'one']) == 0
    p = subprocess.run(['qstat', '-f', '-F', 'json', '1001.fakepbs', '5.fakepbs'],
                       capture_output=True, text=True, check=False)
    assert (p.returncode, p.stdout) == (153, '')
    assert p.stderr.splitlines() == [
        ('qstat: 1001.fakepbs Job has finished, use -x or -H to obtain historical job '
         'information'),
        'qstat: Unknown Job Id 5.fakepbs']


def test_cancel_held(fake_pbs, capsys):
    '''jug-hpc cancel deletes held jobs with qdel, which PBS then reports as deleted'''
    project_dir, pbs_dir = fake_pbs
    pbs_dir.mkdir()
    (pbs_dir / 'hold').touch()
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    capsys.readouterr()
    assert main(['-C', str(project_dir), 'cancel']) == 0
    out, err = capsys.readouterr()
    assert err == ''
    assert out.splitlines() == [
        'big: cancelled job 1001[].fakepbs (2 workers queued)',
        'one: cancelled job 1002.fakepbs (queued)',
        'default: cancelled job 1003[].fakepbs (2 workers queued)',
    ]
    assert [args for _, args in qsub_calls(pbs_dir, 'qdel')] == [
        ['1001[].fakepbs', '1002.fakepbs', '1003[].fakepbs']]
    finished = JobStatus(JobState.FINISHED)
    assert set(query(project_dir).values()) == {finished}
    deleted = JobOutcome(Outcome.CANCELLED, detail='deleted with qdel before it started')
    assert outcomes(project_dir) == {
        '1001[].fakepbs': deleted, '1002.fakepbs': deleted, '1003[].fakepbs': deleted}
    assert exit_statuses(pbs_dir) == {}     # nothing ran

    # nothing left to cancel: qdel is not run again
    assert main(['-C', str(project_dir), 'cancel']) == 0
    out, err = capsys.readouterr()
    assert out == ''
    assert err == 'jug-hpc: note: no job of this project is queued or running\n'
    assert len(qsub_calls(pbs_dir, 'qdel')) == 1


def test_fake_qdel_finished_and_unknown(fake_pbs):
    '''Like PBS, the fake qdel refuses finished and unknown jobs, which the
    backend ignores'''
    project_dir, _ = fake_pbs
    assert main(['-C', str(project_dir), 'submit', '--all', 'one']) == 0
    p = subprocess.run(['qdel', '1001.fakepbs', '5.fakepbs'],
                       capture_output=True, text=True, check=False)
    assert (p.returncode, p.stdout) == (153, '')
    assert p.stderr.splitlines() == [
        'qdel: Job has finished 1001.fakepbs', 'qdel: Unknown Job Id 5.fakepbs']
    PBSScheduler(ClusterSettings(scheduler='pbs')).cancel(
        [JobId('1001.fakepbs'), JobId('5.fakepbs')])
