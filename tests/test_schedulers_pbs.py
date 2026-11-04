import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from jug_hpc.config import (
    ClusterSettings,
    ConfigError,
    load_cluster_settings,
    load_project_settings,
)
from jug_hpc.runner import CommandError, FakeRunner
from jug_hpc.schedulers import (
    JobId,
    JobState,
    JobStatus,
    SchedulerError,
    create_scheduler,
    get_scheduler,
)
from jug_hpc.schedulers.pbs import (
    JOB_NAME_MAX,
    QSTAT_BATCH,
    PBSScheduler,
    format_memory,
    header_lines,
    job_name,
    job_status,
    match_jobs,
    parse_array_state_count,
    parse_job_id,
    parse_qstat_output,
    scheduler_log_dir,
    unexpected_qstat_errors,
    validate_raw_options,
)
from jug_hpc.script import jug_execute_command
from jug_hpc.worker import WorkerSpec, make_worker_spec

GOLDEN = Path(__file__).parent / 'golden' / 'pbs'

# Output of qstat -f -F json, in the format of OpenPBS 23 (not yet recorded on
# a real cluster; replace with real output when the backend is tested on one)
QSTAT_JSON = (Path(__file__).parent / 'pbs_output' / 'qstat-f-json.json').read_text()

# Set JUG_HPC_UPDATE_GOLDEN=1 to rewrite the golden files (then review the diff)
UPDATE = bool(os.environ.get('JUG_HPC_UPDATE_GOLDEN'))

needs_bash = pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')

COMMAND = ('jug', 'execute', '--name=align', '--nr-wait-cycles=15', '--wait-cycle-time=12',
           '--jugdir=/home/u/proj/jugfile.jugdata', '--', '/home/u/proj/jugfile.py')

SPEC = WorkerSpec(
    class_name='align', project_name='proj', workdir=Path('/home/u/proj'),
    cpus=8, memory=16 * 2**30, time=3600, max_seconds=3240,
    logs=Path('/home/u/proj/logs'), command=COMMAND)

CLUSTER = ClusterSettings(scheduler='pbs')


def golden(name, text):
    path = GOLDEN / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert path.exists(), f'{path} is missing: run with JUG_HPC_UPDATE_GOLDEN=1'
    assert text == path.read_text()


def render(spec):
    return PBSScheduler(CLUSTER, FakeRunner()).render_script(spec)


def test_registered():
    assert get_scheduler('pbs') is PBSScheduler
    assert isinstance(create_scheduler(CLUSTER), PBSScheduler)


def test_golden_single():
    golden('single.sh', render(SPEC))


def test_golden_array_with_everything():
    spec = replace(
        SPEC, workers=4, partition='long', account='grant42',
        setup=('module load python/3.12', 'source venv/bin/activate'),
        raw={'select': {'ngpus': 1, 'host_shared': True},
             'resources': {'place': 'scatter:excl'},
             'directives': ['-m n', '#PBS -W group_list=lab']})
    golden('array.sh', render(spec))


def test_header_order():
    lines = header_lines(replace(SPEC, partition='q', account='a', workers=2))
    assert [ln.split()[1] for ln in lines] == ['-N', '-l', '-l', '-q', '-A', '-J', '-o', '-e']
    assert '#PBS -J 1-2' in lines
    assert not any(ln.startswith('#PBS -J') for ln in header_lines(SPEC))


@pytest.mark.parametrize('nbytes, expected', [
    (16 * 2**30, '16gb'),
    (1536 * 2**20, '1536mb'),
    (2**40, '1tb'),
    (1000, '1000b'),
    (3 * 2**10, '3kb'),
])
def test_format_memory(nbytes, expected):
    assert format_memory(nbytes) == expected


def test_job_name():
    assert job_name(SPEC) == 'jug-align-proj'
    assert job_name(replace(SPEC, project_name="my proj's")) == 'jug-align-my_proj_s'
    assert len(job_name(replace(SPEC, project_name='x' * 300))) == JOB_NAME_MAX


def test_scheduler_log_dir():
    assert scheduler_log_dir(SPEC) == 'logs/scheduler'
    # relative to the project directory, so spaces in it don't matter
    spec = replace(SPEC, workdir=Path('/home/u/my proj'), logs=Path('/home/u/my proj/out'))
    assert scheduler_log_dir(spec) == 'out/scheduler'
    assert scheduler_log_dir(replace(SPEC, logs=Path('/scratch/u/logs'))) == \
        '/scratch/u/logs/scheduler'
    with pytest.raises(SchedulerError, match='cannot be used in a #PBS directive'):
        scheduler_log_dir(replace(SPEC, logs=Path('/scratch/my logs')))


def test_partition_with_whitespace():
    with pytest.raises(SchedulerError, match='partition'):
        header_lines(replace(SPEC, partition='a b'))


@pytest.mark.parametrize('stdout, expected', [
    ('1234.pbs01\n', '1234.pbs01'),
    ('1234[].pbs01.example.org\n', '1234[].pbs01.example.org'),
    ('5678\n', '5678'),
    ('qsub: warning: something\n  1234.srv  \n\n', '1234.srv'),
])
def test_parse_job_id(stdout, expected):
    assert parse_job_id(stdout) == expected


@pytest.mark.parametrize('stdout', ['', 'qsub: Unknown queue\n', 'abc.server\n'])
def test_parse_job_id_fails(stdout):
    with pytest.raises(SchedulerError, match='no job ID'):
        parse_job_id(stdout)


def test_submit():
    runner = FakeRunner().on('qsub', stdout='42.server\n')
    pbs = PBSScheduler(CLUSTER, runner)
    assert pbs.submit(Path('/home/u/proj/.jug_hpc/scripts/a.sh'), SPEC) == '42.server'
    assert runner.argvs == [('qsub', '/home/u/proj/.jug_hpc/scripts/a.sh')]
    assert runner.calls[0].cwd == SPEC.workdir


def test_submit_fails():
    runner = FakeRunner().on('qsub', returncode=1, stderr='qsub: Unknown queue')
    with pytest.raises(SchedulerError, match='Unknown queue'):
        PBSScheduler(CLUSTER, runner).submit(Path('a.sh'), SPEC)


# --- query ----------------------------------------------------------------------

QUEUED, RUNNING = JobState.QUEUED, JobState.RUNNING
FINISHED, UNKNOWN = JobState.FINISHED, JobState.UNKNOWN

QSTAT_STATUSES = {
    '1001.pbs01.example.org': JobStatus(FINISHED),
    '1002.pbs01.example.org': JobStatus(RUNNING, running=1),
    '1003.pbs01.example.org': JobStatus(QUEUED, queued=1),
    '1004[].pbs01.example.org': JobStatus(RUNNING, queued=2, running=2),
    '1005[].pbs01.example.org': JobStatus(QUEUED, queued=3),
    '1006[].pbs01.example.org': JobStatus(FINISHED),
}


def test_parse_qstat_output():
    assert parse_qstat_output(QSTAT_JSON) == QSTAT_STATUSES
    assert parse_qstat_output('') == {}
    assert parse_qstat_output('{"timestamp": 1, "pbs_server": "s"}') == {}


@pytest.mark.parametrize('stdout, message', [
    ('{"Jobs": {', 'Expecting'),
    ('[]', 'not a JSON object'),
    ('{"Jobs": []}', "'Jobs' is not a JSON object"),
    ('{"Jobs": {"1.s": 3}}', 'job 1.s: its attributes are not'),
    ('{"Jobs": {"1.s": {}}}', "job 1.s: unknown job_state None"),
    ('{"Jobs": {"1.s": {"job_state": "Z"}}}', "unknown job_state 'Z'"),
    ('{"Jobs": {"1[].s": {"job_state": "B"}}}', 'job 1\\[\\].s: bad array_state_count: None'),
])
def test_parse_qstat_output_fails(stdout, message):
    with pytest.raises(SchedulerError, match=f'could not parse the output of qstat.*{message}'):
        parse_qstat_output(stdout)


@pytest.mark.parametrize('letter, state', [
    ('Q', QUEUED), ('H', QUEUED), ('W', QUEUED), ('T', QUEUED),
    ('R', RUNNING), ('E', RUNNING), ('S', RUNNING), ('U', RUNNING),
    ('F', FINISHED), ('X', FINISHED), ('M', UNKNOWN),
])
def test_job_status_letters(letter, state):
    status = job_status('7.s', {'job_state': letter})
    assert status.state is state
    assert status.active == (1 if state in (QUEUED, RUNNING) else 0)
    assert status.queued == (state is QUEUED)


def test_job_status_array():
    counts = 'Queued:1 Running:2 Exiting:0 Expired:3'
    # an array is recognized by its attribute or by its ID
    assert job_status('7.s', {'job_state': 'B', 'array': 'True', 'array_state_count': counts}) \
        == JobStatus(RUNNING, queued=1, running=2)
    assert job_status('7[].s', {'job_state': 'H', 'array_state_count': 'Queued:4 Running:0'}) \
        == JobStatus(QUEUED, queued=4)
    # a finished array needs no counts
    assert job_status('7[].s', {'job_state': 'F'}) == JobStatus(FINISHED)


@pytest.mark.parametrize('text, expected', [
    ('Queued:0 Running:2 Exiting:1 Expired:1 ',
     {'Queued': 0, 'Running': 2, 'Exiting': 1, 'Expired': 1}),
    ('Running:3 Queued:1', {'Queued': 1, 'Running': 3}),
])
def test_parse_array_state_count(text, expected):
    assert parse_array_state_count(text) == expected


@pytest.mark.parametrize('text', [None, '', 'Queued:1', 'Queued:1 Running:x', 'Queued 1 Running 2',
                                  'Queued:-1 Running:0'])
def test_parse_array_state_count_fails(text):
    with pytest.raises(ValueError, match='bad array_state_count'):
        parse_array_state_count(text)


def test_unexpected_qstat_errors():
    stderr = ('qstat: Unknown Job Id 99.pbs01\n'
              'qstat: 98.pbs01 Job has finished, use -x or -H to obtain historical job '
              'information\n\n')
    assert unexpected_qstat_errors(stderr) == []
    assert unexpected_qstat_errors('qstat: cannot connect to server pbs01 (errno=111)\n' +
                                   stderr) == ['qstat: cannot connect to server pbs01 (errno=111)']


def test_match_jobs():
    jobs = [JobId('1002.pbs01'), JobId('1004[].pbs01'), JobId('1004.pbs01'),
            JobId('1001.pbs01.example.org'), JobId('1007')]
    assert match_jobs(jobs, QSTAT_STATUSES) == {
        '1002.pbs01': JobStatus(RUNNING, running=1),
        '1004[].pbs01': JobStatus(RUNNING, queued=2, running=2),
        '1004.pbs01': JobStatus(UNKNOWN),     # not the array 1004[]
        '1001.pbs01.example.org': JobStatus(FINISHED),
        '1007': JobStatus(UNKNOWN),
    }


def test_query():
    runner = FakeRunner().on('qstat', returncode=153, stdout=QSTAT_JSON,
                             stderr='qstat: Unknown Job Id 1007.pbs01\n')
    pbs = PBSScheduler(CLUSTER, runner)
    jobs = [JobId('1004[].pbs01'), JobId('1003.pbs01'), JobId('1007.pbs01')]
    assert pbs.query(jobs) == {
        '1004[].pbs01': JobStatus(RUNNING, queued=2, running=2),
        '1003.pbs01': JobStatus(QUEUED, queued=1),
        '1007.pbs01': JobStatus(UNKNOWN),
    }
    assert runner.argvs == [('qstat', '-f', '-F', 'json', '-x', *jobs)]
    assert pbs.query([]) == {}
    assert len(runner.calls) == 1


def test_query_unknown_only():
    '''qstat may print nothing at all when it knows none of the jobs'''
    runner = FakeRunner().on('qstat', returncode=153, stderr='qstat: Unknown Job Id 1.s\n')
    assert PBSScheduler(CLUSTER, runner).query(['1.s']) == {'1.s': JobStatus(UNKNOWN)}


def test_query_batches():
    jobs = [JobId(f'{i}.s') for i in range(QSTAT_BATCH * 2 + 1)]
    runner = FakeRunner().on('qstat', handler=lambda call: json.dumps({'Jobs': {
        j: {'job_state': 'R'} for j in call.argv[5:]}}))
    statuses = PBSScheduler(CLUSTER, runner).query(jobs)
    assert statuses == {j: JobStatus(RUNNING, running=1) for j in jobs}
    assert [len(c.argv) - 5 for c in runner.calls] == [QSTAT_BATCH, QSTAT_BATCH, 1]


@pytest.mark.parametrize('returncode, stderr, message', [
    (2, 'qstat: cannot connect to server pbs01 (errno=111)\n', 'cannot connect'),
    (1, '', 'exit status 1'),
    (153, 'qstat: Unknown Job Id 1.s\nqstat: something else\n', 'something else'),
    (127, 'qstat: command not found', 'command not found'),
])
def test_query_fails(returncode, stderr, message):
    runner = FakeRunner().on('qstat', returncode=returncode, stderr=stderr)
    with pytest.raises(SchedulerError, match=f'pbs: could not query jobs: (?s:.*){message}'):
        PBSScheduler(CLUSTER, runner).query(['1.s'])


def test_query_timeout():
    def hang(call):
        raise CommandError(call.argv, None, reason='timed out after 300 seconds')
    runner = FakeRunner().on('qstat', handler=hang)
    with pytest.raises(SchedulerError, match='pbs: could not query jobs.*timed out'):
        PBSScheduler(CLUSTER, runner).query(['1.s'])


@pytest.mark.parametrize('raw', [
    {},
    {'select': {'ngpus': 2, 'mpiprocs': 4}, 'resources': {'place': 'pack'},
     'directives': ['-m abe', '-M me@example.org']},
])
def test_validate_ok(raw):
    validate_raw_options(raw, 'f: [pbs]')


@pytest.mark.parametrize('raw, message', [
    ({'selct': {}}, "'selct' is not a PBS raw option (did you mean 'select'?)"),
    ({'foo': 1}, "'foo' is not a PBS raw option (known keys: select, resources, directives)"),
    ({'select': 'ngpus=1'}, "'select' must be a table"),
    ({'directives': '-m n'}, "'directives' must be a list of strings"),
    ({'directives': [1]}, "'directives' must be a list of strings"),
    ({'directives': [' ']}, 'must not contain empty strings'),
    ({'directives': ['-m n\n-q x']}, 'must not contain newlines'),
    ({'select': {'ncpus': 2}}, "'select.ncpus' cannot be set as a raw option: use cpus"),
    ({'select': {'mem': '2gb'}}, "'select.mem' cannot be set as a raw option: use memory"),
    ({'resources': {'walltime': '1:00:00'}}, 'use time in jug_hpc.toml'),
    ({'resources': {'select': '2:ncpus=1'}}, "'resources.select' cannot be set"),
    ({'select': {'n gpus': 1}}, 'is not a valid PBS resource name'),
    ({'select': {'ngpus': [1]}}, 'must be a string, a number or a boolean'),
    ({'select': {'host': 'a b'}}, 'must not be empty or contain whitespace'),
    ({'select': {'host': ''}}, 'must not be empty or contain whitespace'),
    ({'select': {'host': 'a:b'}}, "must not contain ':'"),
])
def test_validate_errors(raw, message):
    with pytest.raises(ConfigError) as e:
        validate_raw_options(raw, 'cluster.toml: [pbs]')
    assert str(e.value).startswith('cluster.toml: [pbs]: ')
    assert message in str(e.value)


def test_render_validates_merged_raw():
    with pytest.raises(ConfigError, match="class 'align'"):
        render(replace(SPEC, raw={'select': {'ncpus': 2}}))


CLUSTER_TOML = '''
scheduler = "pbs"
account   = "grant42"
partition = "short"

[pbs]
directives = ["-m n"]

[pbs.select]
ngpus = 0
'''

PROJECT_TOML = '''
[default]
memory = "4G"
time   = "2h"

[class.align]
match     = "align"
cpus      = 8
workers   = 3
partition = "gpu"

[class.align.pbs.select]
ngpus = 1
'''


@pytest.fixture
def project_settings(tmp_path):
    xdg = tmp_path / 'xdg'
    (xdg / 'jug_hpc').mkdir(parents=True)
    (xdg / 'jug_hpc' / 'cluster.toml').write_text(CLUSTER_TOML)
    project_dir = tmp_path / 'proj'
    project_dir.mkdir()
    (project_dir / 'jug_hpc.toml').write_text(PROJECT_TOML)
    cluster = load_cluster_settings(project_dir, environ={'XDG_CONFIG_HOME': str(xdg)})
    return cluster, load_project_settings(project_dir)


def spec_for(settings, name):
    cluster, project = settings
    rc = project.get_class(name)
    return make_worker_spec(cluster, project, rc, jug_execute_command(project, rc, exclude=True))


def test_from_settings(project_settings):
    cluster, _ = project_settings
    pbs = create_scheduler(cluster)
    lines = header_lines(spec_for(project_settings, 'align'))
    assert '#PBS -l select=1:ncpus=8:mem=4gb:ngpus=1' in lines     # class wins
    assert '#PBS -l walltime=02:00:00' in lines
    assert '#PBS -q gpu' in lines
    assert '#PBS -A grant42' in lines
    assert '#PBS -J 1-3' in lines
    assert lines[-1] == '#PBS -m n'
    default = pbs.render_script(spec_for(project_settings, 'default'))
    assert '#PBS -l select=1:ncpus=1:mem=4gb:ngpus=0\n' in default
    assert '#PBS -q short\n' in default
    assert '-J' not in default


FAKE_JUG = '''#!/bin/sh
echo "jug $*"
echo "job=$JUG_HPC_JOB_ID index=$JUG_HPC_ARRAY_INDEX"
'''


@needs_bash
@pytest.mark.parametrize('workers, jobid, index, log', [
    (1, '1234.server', None, 'align-1234.log'),
    (3, '1234[2].server', '2', 'align-1234-2.log'),
])
def test_script_runs(tmp_path, workers, jobid, index, log):
    '''The rendered script runs under bash with PBS's environment variables'''
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'jug').write_text(FAKE_JUG)
    (bindir / 'jug').chmod(0o755)
    workdir = tmp_path / 'proj'
    workdir.mkdir()
    spec = replace(SPEC, workdir=workdir, logs=workdir / 'logs', workers=workers)
    script = tmp_path / 'worker.sh'
    script.write_text(render(spec))
    env = {'PATH': f'{bindir}:{os.environ["PATH"]}', 'HOME': str(tmp_path),
           'PBS_JOBID': jobid}
    if index is not None:
        env['PBS_ARRAY_INDEX'] = index
    subprocess.run(['bash', str(script)], check=True, env=env, cwd=tmp_path, timeout=30)
    output = (workdir / 'logs' / log).read_text()
    assert output.startswith('jug execute --name=align ')
    assert f'job=1234 index={index or ""}' in output
