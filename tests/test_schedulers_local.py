import json
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from jug_hpc.config import (
    ClusterSettings,
    ConfigError,
    load_cluster_settings,
    load_project_settings,
)
from jug_hpc.jugstate import load_project
from jug_hpc.runner import FakeRunner, SubprocessRunner
from jug_hpc.schedulers import (
    JobId,
    JobOutcome,
    JobState,
    Outcome,
    SchedulerError,
    create_scheduler,
    get_scheduler,
)
from jug_hpc.schedulers.local import (
    LocalScheduler,
    combine_outcomes,
    default_jobs_dir,
    element_outcome,
    header_lines,
    parse_job_id,
    parse_status_output,
    validate_raw_options,
)
from jug_hpc.script import jug_execute_command
from jug_hpc.worker import WorkerSpec, make_worker_spec

GOLDEN = Path(__file__).parent / 'golden' / 'local'
JUGFILES = Path(__file__).parent / 'jugfiles'

# Set JUG_HPC_UPDATE_GOLDEN=1 to rewrite the golden files (then review the diff)
UPDATE = bool(os.environ.get('JUG_HPC_UPDATE_GOLDEN'))

needs_bash = pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')

COMMAND = ('jug', 'execute', '--name=align', '--nr-wait-cycles=15', '--wait-cycle-time=12',
           '--jugdir=/home/u/proj/jugfile.jugdata', '--', '/home/u/proj/jugfile.py')

SPEC = WorkerSpec(
    class_name='align', project_name='proj', workdir=Path('/home/u/proj'),
    cpus=8, memory=16 * 2**30, time=3600, max_seconds=3240,
    logs=Path('/home/u/proj/logs'), command=COMMAND)

CLUSTER = ClusterSettings(scheduler='local')


def golden(name, text):
    path = GOLDEN / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert path.exists(), f'{path} is missing: run with JUG_HPC_UPDATE_GOLDEN=1'
    assert text == path.read_text()


def local(runner=None, jobs_dir='/jobs'):
    return LocalScheduler(CLUSTER, runner if runner is not None else FakeRunner(), jobs_dir)


# --- pure -----------------------------------------------------------------------

def test_registered(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    assert get_scheduler('local') is LocalScheduler
    sched = create_scheduler(CLUSTER)
    assert isinstance(sched, LocalScheduler)
    assert sched.jobs_dir == tmp_path / 'jug_hpc' / 'local'


def test_default_jobs_dir():
    assert default_jobs_dir({'XDG_STATE_HOME': '/s'}) == Path('/s/jug_hpc/local')
    assert default_jobs_dir({'HOME': '/h'}) == Path('/h/.local/state/jug_hpc/local')
    assert default_jobs_dir({'HOME': '/h', 'XDG_STATE_HOME': ''}) == \
        Path('/h/.local/state/jug_hpc/local')


def test_golden_single():
    golden('single.sh', local().render_script(SPEC))


def test_golden_array():
    spec = replace(SPEC, workers=4, setup=('source venv/bin/activate',))
    golden('array.sh', local().render_script(spec))


def test_header_lines():
    assert header_lines(replace(SPEC, memory=1536 * 2**20, time=90)) == [
        '# jug_hpc local worker: jug-align-proj',
        '# cpus=8 memory=1536M time=00:01:30 workers=1',
        '# (the local backend enforces only the wall time)',
    ]


def test_validate_raw_options():
    validate_raw_options({}, 'x')
    with pytest.raises(ConfigError, match=r"^x: 'nice' is not a raw option of the local"):
        validate_raw_options({'nice': 10}, 'x')
    with pytest.raises(ConfigError, match='must be a table'):
        validate_raw_options([], 'x')
    with pytest.raises(ConfigError, match="class 'align'"):
        local().render_script(replace(SPEC, raw={'nice': 10}))


def test_submit_command():
    argv = local().submit_command(Path('/p/s.sh'), replace(SPEC, workers=3))
    assert argv == [sys.executable, '-m', 'jug_hpc.schedulers.local', 'submit',
                    '--jobs-dir=/jobs', '--workers=3', '--time=3600',
                    '--output=/home/u/proj/logs/scheduler', '--name=align', '--', '/p/s.sh']


@pytest.mark.parametrize('stdout, expected', [('1\n', '1'), ('note\n 42 \n\n', '42')])
def test_parse_job_id(stdout, expected):
    assert parse_job_id(stdout) == expected


@pytest.mark.parametrize('stdout', ['', 'job 1\n', '0\n', '-3\n', '1\nnot an id\n'])
def test_parse_job_id_fails(stdout):
    with pytest.raises(SchedulerError, match='no job ID'):
        parse_job_id(stdout)


@pytest.mark.parametrize('record, expected', [
    ({'returncode': 0, 'killed': None}, JobOutcome(Outcome.SUCCESS, 0)),
    ({'returncode': 3, 'killed': None}, JobOutcome(Outcome.FAILED, 3, None, 'exit status 3')),
    ({'returncode': -9, 'killed': None},
     JobOutcome(Outcome.KILLED_SIGNAL, None, 9, 'killed by SIGKILL')),
    ({'returncode': -15, 'killed': 'time'},
     JobOutcome(Outcome.KILLED_TIME, None, 15, 'killed: wall time limit reached')),
    ({'returncode': 0, 'killed': 'time'},     # exited on its own after SIGTERM
     JobOutcome(Outcome.KILLED_TIME, 0, None, 'killed: wall time limit reached')),
    ({'returncode': -15, 'killed': 'cancelled'},
     JobOutcome(Outcome.KILLED_SIGNAL, None, 15, 'cancelled')),
    ({}, JobOutcome(Outcome.UNKNOWN, detail='no exit status recorded')),
])
def test_element_outcome(record, expected):
    assert element_outcome(record) == expected


def test_combine_outcomes():
    ok = JobOutcome(Outcome.SUCCESS, 0)
    failed = JobOutcome(Outcome.FAILED, 1, None, 'exit status 1')
    timed_out = JobOutcome(Outcome.KILLED_TIME, None, 15, 'killed: wall time limit reached')
    assert combine_outcomes({}).outcome is Outcome.UNKNOWN
    assert combine_outcomes({1: failed}) == failed
    assert combine_outcomes({1: ok, 2: ok}) == ok
    assert combine_outcomes({1: ok, 2: failed, 3: timed_out, 4: failed}) == JobOutcome(
        Outcome.KILLED_TIME, None, 15,
        '3 of 4 workers did not succeed (worker 2: exit status 1; '
        'worker 3: killed: wall time limit reached; worker 4: exit status 1)')


def test_parse_status_output():
    stdout = json.dumps({
        '1': {'state': 'running'},
        '2': {'state': 'finished', 'elements': {'1': {'returncode': 0, 'killed': None},
                                                '2': {'returncode': 0, 'killed': None}}},
        '3': {'state': 'finished', 'elements': None},
        '4': {'state': 'unknown'},
    })
    assert parse_status_output(stdout) == {
        '1': (JobState.RUNNING, None),
        '2': (JobState.FINISHED, JobOutcome(Outcome.SUCCESS, 0)),
        '3': (JobState.FINISHED,
              JobOutcome(Outcome.UNKNOWN, detail='the local supervisor process died')),
        '4': (JobState.UNKNOWN, None),
    }


@pytest.mark.parametrize('stdout', ['', '[1]', '{"1": {"state": "odd"}}', '{"1": {}}',
                                    '{"1": {"state": "finished", "elements": {"x": {}}}}'])
def test_parse_status_output_fails(stdout):
    with pytest.raises(SchedulerError, match='could not parse the output of status'):
        parse_status_output(stdout)


def test_query_outcome_cancel_commands():
    runner = FakeRunner()
    runner.on(sys.executable, '-m', 'jug_hpc.schedulers.local', 'status', stdout=json.dumps({
        '5': {'state': 'running'},
        '6': {'state': 'finished', 'elements': {'1': {'returncode': 2, 'killed': None}}},
    }))
    runner.on(sys.executable, '-m', 'jug_hpc.schedulers.local', 'cancel')
    sched = local(runner)
    jobs = [JobId('5'), JobId('6'), JobId('7')]
    assert sched.query(jobs) == {'5': JobState.RUNNING, '6': JobState.FINISHED,
                                 '7': JobState.UNKNOWN}
    assert sched.outcome(jobs) == {'6': JobOutcome(Outcome.FAILED, 2, None, 'exit status 2')}
    sched.cancel(['5'])
    assert sched.query([]) == {}
    assert sched.outcome([]) == {}
    sched.cancel([])
    base = (sys.executable, '-m', 'jug_hpc.schedulers.local')
    assert runner.argvs == [
        (*base, 'status', '--jobs-dir=/jobs', '--', '5', '6', '7'),
        (*base, 'status', '--jobs-dir=/jobs', '--', '5', '6', '7'),
        (*base, 'cancel', '--jobs-dir=/jobs', '--', '5'),
    ]


def test_command_failures():
    sched = local(FakeRunner())     # every command fails (exit status 127)
    with pytest.raises(SchedulerError, match='local: could not query jobs'):
        sched.query(['1'])
    with pytest.raises(SchedulerError, match='local: could not cancel jobs'):
        sched.cancel(['1'])
    with pytest.raises(SchedulerError, match='local: could not submit'):
        sched.submit(Path('/p/s.sh'), SPEC)


# --- end to end: real processes -------------------------------------------------

def wait_finished(sched, job, timeout=30):
    deadline = time.monotonic() + timeout
    while sched.query([job])[job] is not JobState.FINISHED:
        assert time.monotonic() < deadline, f'job {job} did not finish'
        time.sleep(0.1)
    return sched.outcome([job])[job]


@pytest.fixture
def run(tmp_path):
    '''Submit a worker that runs ``command`` (instead of jug); returns the
    scheduler, the job ID and the spec'''
    workdir = tmp_path / 'proj'
    workdir.mkdir()
    sched = LocalScheduler(CLUSTER, SubprocessRunner(), tmp_path / 'jobs')

    def submit(command, **changes):
        spec = replace(SPEC, workdir=workdir, logs=workdir / 'logs', command=command,
                       **changes)
        script = tmp_path / f'{spec.class_name}.sh'
        script.write_text(sched.render_script(spec))
        return sched, sched.submit(script, spec), spec
    return submit


@needs_bash
def test_run_array(run):
    echo = 'echo "$JUG_HPC_CLASS $JUG_HPC_JOB_ID $JUG_HPC_ARRAY_INDEX $JUG_HPC_CPUS ' \
        '$JUG_MAX_SECONDS"'
    sched, job, spec = run(('sh', '-c', echo), workers=3)
    assert job == '1'
    assert wait_finished(sched, job) == JobOutcome(Outcome.SUCCESS, 0)
    for i in (1, 2, 3):
        assert (spec.logs / f'align-1-{i}.log').read_text() == f'align 1 {i} 8 3240\n'
        assert (spec.logs / 'scheduler' / f'align-1-{i}.out').exists()
    _, job2, _ = run(('true',))
    assert job2 == '2'
    info = json.loads((sched.jobs_dir / '2' / 'info.json').read_text())
    assert info['workers'] == 1
    assert info['time'] == 3600
    assert wait_finished(sched, job2).outcome is Outcome.SUCCESS
    assert (spec.logs / 'align-2.log').exists()


@needs_bash
def test_run_failure(run):
    sched, job, _ = run(('sh', '-c', 'exit "$JUG_HPC_ARRAY_INDEX"'), workers=2)
    outcome = wait_finished(sched, job)
    assert outcome.outcome is Outcome.FAILED
    assert outcome.exit_code == 1     # the first of the worst
    assert outcome.detail.startswith('2 of 2 workers did not succeed')


@needs_bash
def test_run_setup_failure(run):
    '''A failing setup snippet ends the worker before its output is redirected'''
    sched, job, spec = run(('true',), setup=('echo setting up; false',))
    assert wait_finished(sched, job).outcome is Outcome.FAILED
    assert (spec.logs / 'align-1.log').read_text() == 'setting up\n'


@needs_bash
def test_run_time_limit(run):
    sched, job, _ = run(('sleep', '60'), time=1)
    t0 = time.monotonic()
    outcome = wait_finished(sched, job)
    assert outcome.outcome is Outcome.KILLED_TIME
    assert time.monotonic() - t0 < 10


@needs_bash
def test_cancel(run):
    sched, job, _ = run(('sleep', '60'), workers=2)
    assert sched.query([job]) == {job: JobState.RUNNING}
    sched.cancel([job, JobId('99')])
    outcome = wait_finished(sched, job)
    assert outcome.outcome is Outcome.KILLED_SIGNAL
    assert 'cancelled' in outcome.detail
    sched.cancel([job])     # finished: ignored


def test_unknown_and_lost_jobs(tmp_path):
    jobs = tmp_path / 'jobs'
    sched = LocalScheduler(CLUSTER, SubprocessRunner(), jobs)
    assert sched.query(['1', '../x']) == {'1': JobState.UNKNOWN, '../x': JobState.UNKNOWN}
    (jobs / '1').mkdir(parents=True)     # a supervisor that died without a result
    (jobs / '1' / 'info.json').write_text('{}')
    (jobs / '1' / 'lock').touch()
    assert sched.query(['1']) == {'1': JobState.FINISHED}
    assert sched.outcome(['1'])['1'].outcome is Outcome.UNKNOWN


PROJECT_TOML = '''
[default]
memory = "1G"
time   = "1h"

[class.align]
match   = "align_sample"
workers = 2
'''


@needs_bash
def test_runs_jugfile(tmp_path):
    '''Workers of every class run a jugfile to completion'''
    xdg = tmp_path / 'xdg'
    (xdg / 'jug_hpc').mkdir(parents=True)
    (xdg / 'jug_hpc' / 'cluster.toml').write_text('scheduler = "local"\n')
    project_dir = tmp_path / 'proj'
    project_dir.mkdir()
    shutil.copy(JUGFILES / 'simple.py', project_dir / 'jugfile.py')
    (project_dir / 'jug_hpc.toml').write_text(PROJECT_TOML)
    cluster = load_cluster_settings(project_dir, environ={'XDG_CONFIG_HOME': str(xdg)})
    project = load_project_settings(project_dir)
    sched = LocalScheduler(cluster, SubprocessRunner(), tmp_path / 'jobs')
    jug = str(Path(sys.executable).parent / 'jug')
    jobs = []
    for name in ('align', 'default'):
        rc = project.get_class(name)
        command = jug_execute_command(project, rc, jug=jug, nr_wait_cycles=100,
                                      wait_cycle_time=1,
                                      task_names=load_project(project).task_names)
        spec = make_worker_spec(cluster, project, rc, command)
        script = project_dir / f'{name}.sh'
        script.write_text(sched.render_script(spec))
        jobs.append(sched.submit(script, spec))
    for job in jobs:
        outcome = wait_finished(sched, job, timeout=120)
        logs = '\n'.join(p.read_text() for p in (project_dir / 'logs').glob('*.log'))
        assert outcome.outcome is Outcome.SUCCESS, logs
    state = load_project(project)
    assert len(state.tasks) == 7
    assert all(t.can_load(state.store) for t in state.tasks)
