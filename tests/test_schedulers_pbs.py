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
    JobOutcome,
    JobState,
    JobStatus,
    Outcome,
    SchedulerError,
    create_scheduler,
    get_scheduler,
)
from jug_hpc.schedulers.pbs import (
    JOB_NAME_MAX,
    QSTAT_BATCH,
    PBSScheduler,
    array_exit_outcome,
    element_outcome,
    format_memory,
    header_lines,
    job_name,
    job_status,
    match_jobs,
    parse_array_state_count,
    parse_job_id,
    parse_qstat_outcomes,
    parse_qstat_output,
    scheduler_log_dir,
    unexpected_qstat_errors,
    validate_raw_options,
)
from jug_hpc.script import jug_execute_command
from jug_hpc.worker import WorkerSpec, make_worker_spec

GOLDEN = Path(__file__).parent / 'golden' / 'pbs'

# Output of qstat -f -F json, modelled on OpenPBS 23 (not recorded): it covers
# what the recordings in pbs_output/pbspro-2026.1/ (see RECORDED) do not, such
# as PBS's memory kills (-27), signal kills as 256 + N, and unescaped control
# characters (older PBS versions)
QSTAT_JSON = (Path(__file__).parent / 'pbs_output' / 'qstat-f-json.json').read_text()
# ... and of qstat -f -F json -x -t, with array elements and exit statuses
QSTAT_XT_JSON = (Path(__file__).parent / 'pbs_output' / 'qstat-f-json-xt.json').read_text()

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


# --- outcome --------------------------------------------------------------------

SUCCESS = JobOutcome(Outcome.SUCCESS, 0)

QSTAT_OUTCOMES = {
    '2001.pbs01.example.org': SUCCESS,
    '2002.pbs01.example.org': JobOutcome(Outcome.FAILED, 3, None, 'exit status 3'),
    '2003.pbs01.example.org': JobOutcome(
        Outcome.KILLED_MEMORY, -27, None,
        'killed by PBS: memory limit exceeded (Exit_status -27)'),
    '2004.pbs01.example.org': JobOutcome(
        Outcome.KILLED_TIME, 271, 15,
        'wall time limit reached (used 01:00:02 of 01:00:00); '
        'killed by SIGTERM (Exit_status 271)'),
    '2005.pbs01.example.org': JobOutcome(
        Outcome.KILLED_SIGNAL, 265, 9,
        'killed by SIGKILL (Exit_status 265), probably for using more memory than requested '
        '(last measured use: 16252928kb of 16gb)'),
    '2007[].pbs01.example.org': JobOutcome(
        Outcome.KILLED_TIME, -29, None,
        '2 of 3 workers did not succeed ('
        'worker 2: killed by PBS: wall time limit exceeded (Exit_status -29); '
        'worker 3: wall time limit reached (used 02:00:01 of 02:00:00); '
        'killed by SIGKILL (Exit_status 265))'),
    '2008[].pbs01.example.org': SUCCESS,
    '2009.pbs01.example.org': JobOutcome(
        Outcome.UNKNOWN, detail='no exit status (deleted before it started?)'),
}


def test_parse_qstat_outcomes():
    assert parse_qstat_outcomes(QSTAT_XT_JSON) == QSTAT_OUTCOMES
    assert parse_qstat_outcomes('') == {}
    # without -t, an array job falls back to its own exit status
    assert parse_qstat_outcomes(QSTAT_JSON) == {
        '1001.pbs01.example.org': SUCCESS,
        '1006[].pbs01.example.org': JobOutcome(Outcome.UNKNOWN, None, None, 'no exit status'),
    }


def test_parse_qstat_outcomes_fails():
    with pytest.raises(SchedulerError, match='could not parse the output of qstat'):
        parse_qstat_outcomes('{"Jobs": {"1.s": 3}}')


@pytest.mark.parametrize('attrs, expected', [
    ({'Exit_status': 0}, SUCCESS),
    ({'Exit_status': '0'}, SUCCESS),
    ({'Exit_status': 1}, JobOutcome(Outcome.FAILED, 1, None, 'exit status 1')),
    # 128 + N: killed by signal N (the worker script execs jug)
    ({'Exit_status': 137}, JobOutcome(Outcome.KILLED_SIGNAL, 137, 9,
                                      'killed by SIGKILL (Exit_status 137), probably for '
                                      'using more memory than requested')),
    ({'Exit_status': 143, 'resources_used': {'mem': '1kb'}, 'Resource_List': {'mem': '1gb'}},
     JobOutcome(Outcome.KILLED_SIGNAL, 143, 15,
                'killed by SIGTERM (Exit_status 143); memory used: 1kb of 1gb')),
    ({'Exit_status': 128}, JobOutcome(Outcome.FAILED, 128, None, 'exit status 128')),
    ({'Exit_status': 193}, JobOutcome(Outcome.FAILED, 193, None, 'exit status 193')),
    # deleted with qdel (substate 91), whatever the exit status
    ({'Exit_status': 143, 'substate': 91},
     JobOutcome(Outcome.CANCELLED, 143, 15, 'deleted with qdel (Exit_status 143)')),
    ({'Exit_status': 271, 'substate': '91'},
     JobOutcome(Outcome.CANCELLED, 271, 15, 'deleted with qdel (Exit_status 271)')),
    ({'Exit_status': 3, 'substate': 93}, JobOutcome(Outcome.FAILED, 3, None, 'exit status 3')),
    # a SIGKILL at the wall time limit is a wall time kill, not a memory one
    ({'Exit_status': 137, 'resources_used': {'walltime': '01:00:00'},
      'Resource_List': {'walltime': '01:00:00'}},
     JobOutcome(Outcome.KILLED_TIME, 137, 9, 'wall time limit reached (used 01:00:00 of '
                '01:00:00); killed by SIGKILL (Exit_status 137)')),
    ({'Exit_status': 271}, JobOutcome(Outcome.KILLED_SIGNAL, 271, 15,
                                      'killed by SIGTERM (Exit_status 271)')),
    ({'Exit_status': -29}, JobOutcome(Outcome.KILLED_TIME, -29, None,
                                      'killed by PBS: wall time limit exceeded '
                                      '(Exit_status -29)')),
    ({'Exit_status': -26}, JobOutcome(Outcome.KILLED_MEMORY, -26, None,
                                      'killed by PBS: virtual memory limit exceeded '
                                      '(Exit_status -26)')),
    ({'Exit_status': -17}, JobOutcome(Outcome.FAILED, -17, None,
                                      'a PBS hook rejected the job (Exit_status -17)')),
    ({'Exit_status': -99}, JobOutcome(Outcome.FAILED, -99, None, 'PBS error (Exit_status -99)')),
    # a memory kill stays one, even at the wall time limit
    ({'Exit_status': -27, 'resources_used': {'walltime': '01:00:00'},
      'Resource_List': {'walltime': '01:00:00'}},
     JobOutcome(Outcome.KILLED_MEMORY, -27, None,
                'killed by PBS: memory limit exceeded (Exit_status -27)')),
    # malformed resources are ignored
    ({'Exit_status': 1, 'resources_used': 'x', 'Resource_List': {'walltime': 'long'}},
     JobOutcome(Outcome.FAILED, 1, None, 'exit status 1')),
    ({}, JobOutcome(Outcome.UNKNOWN, detail='no exit status (deleted before it started?)')),
    ({'Exit_status': True},
     JobOutcome(Outcome.UNKNOWN, detail='no exit status (deleted before it started?)')),
])
def test_element_outcome(attrs, expected):
    assert element_outcome(attrs) == expected


def test_element_outcome_array_limits():
    '''An element without its own Resource_List uses the array's'''
    attrs = {'Exit_status': 271, 'resources_used': {'walltime': '100:00:00'}}
    assert element_outcome(attrs, {'walltime': '99:00:00'}).outcome is Outcome.KILLED_TIME
    assert element_outcome(attrs, {'walltime': '101:00:00'}).outcome is Outcome.KILLED_SIGNAL
    assert element_outcome(attrs).outcome is Outcome.KILLED_SIGNAL


@pytest.mark.parametrize('status, outcome', [
    (0, Outcome.SUCCESS), (1, Outcome.FAILED), (2, Outcome.UNKNOWN), (None, Outcome.UNKNOWN)])
def test_array_exit_outcome(status, outcome):
    attrs = {} if status is None else {'Exit_status': status}
    assert array_exit_outcome(attrs).outcome is outcome


def test_outcome():
    runner = FakeRunner().on('qstat', returncode=153, stdout=QSTAT_XT_JSON,
                             stderr='qstat: Unknown Job Id 2011.pbs01\n')
    pbs = PBSScheduler(CLUSTER, runner)
    jobs = [JobId('2007[].pbs01'), JobId('2002.pbs01'), JobId('2006.pbs01'),
            JobId('2010[].pbs01'), JobId('2011.pbs01')]
    # running (2006, 2010[]) and unknown (2011) jobs are left out
    assert pbs.outcome(jobs) == {
        '2007[].pbs01': QSTAT_OUTCOMES['2007[].pbs01.example.org'],
        '2002.pbs01': QSTAT_OUTCOMES['2002.pbs01.example.org'],
    }
    assert runner.argvs == [('qstat', '-f', '-F', 'json', '-x', '-t', *jobs)]
    assert pbs.outcome([]) == {}
    assert len(runner.calls) == 1


def test_outcome_fails():
    runner = FakeRunner().on('qstat', returncode=2, stderr='qstat: cannot connect\n')
    with pytest.raises(SchedulerError, match='pbs: could not query jobs: (?s:.*)cannot connect'):
        PBSScheduler(CLUSTER, runner).outcome(['1.s'])


# --- recorded on a real cluster -------------------------------------------------------
# PBS Professional 2026.1 with cgroups, recorded by tests/manual/pbs/run-everything.sh
# (see the README there; user, host and project names replaced). For an unknown job,
# qstat printed 'qstat: Unknown Job Id 99999999.aqua' and exited with 153.

RECORDED = Path(__file__).parent / 'pbs_output' / 'pbspro-2026.1'
RECORDED_UNKNOWN = 'qstat: Unknown Job Id 99999999.aqua\n'


def recorded(name):
    return (RECORDED / name).read_text()


@pytest.mark.parametrize('name, expected', [
    # 1-static: a plain job and an array of 3, queued, running, then finished
    ('qstat-x-queued.json', {'27666324.aqua': JobStatus(QUEUED, queued=1),
                             '27666325[].aqua': JobStatus(QUEUED, queued=3)}),
    ('qstat-x-running.json', {'27666324.aqua': JobStatus(RUNNING, running=1),
                              '27666325[].aqua': JobStatus(RUNNING, running=3)}),
    ('qstat-x-finished.json', {'27666324.aqua': JobStatus(FINISHED),
                               '27666325[].aqua': JobStatus(FINISHED)}),
    ('qstat-x-mixed.json', {'27666324.aqua': JobStatus(FINISHED)}),
    ('qstat-x-unknown.json', {}),
])
def test_recorded_qstat_output(name, expected):
    assert parse_qstat_output(recorded(name)) == expected


def test_recorded_query_unknown():
    '''A known and an unknown job in one call: qstat lists the known one and fails'''
    runner = FakeRunner().on('qstat', returncode=153, stdout=recorded('qstat-x-mixed.json'),
                             stderr=RECORDED_UNKNOWN)
    pbs = PBSScheduler(CLUSTER, runner)
    assert pbs.query([JobId('27666324.aqua'), JobId('99999999.aqua')]) == {
        '27666324.aqua': JobStatus(FINISHED), '99999999.aqua': JobStatus(UNKNOWN)}
    runner = FakeRunner().on('qstat', returncode=153, stdout=recorded('qstat-x-unknown.json'),
                             stderr=RECORDED_UNKNOWN)
    pbs = PBSScheduler(CLUSTER, runner)
    assert pbs.query([JobId('99999999.aqua')]) == {'99999999.aqua': JobStatus(UNKNOWN)}
    assert pbs.outcome([JobId('99999999.aqua')]) == {}


EXIT_3 = JobOutcome(Outcome.FAILED, 3, None, 'exit status 3')


@pytest.mark.parametrize('name, expected', [
    ('qstat-xt-running.json', {}),
    ('qstat-xt-finished.json', {'27666324.aqua': SUCCESS, '27666325[].aqua': SUCCESS}),
    # without -t, the finished array has its own Exit_status 0
    ('qstat-x-finished.json', {'27666324.aqua': SUCCESS, '27666325[].aqua': SUCCESS}),
    # 5-exit-status
    ('qstat-xt-ok.json', {'27677406.aqua': SUCCESS}),
    ('qstat-xt-exit3.json', {'27677408.aqua': EXIT_3}),
    ('qstat-xt-mem.json', {'27677409.aqua': JobOutcome(
        Outcome.KILLED_SIGNAL, 137, 9,
        'killed by SIGKILL (Exit_status 137), probably for using more memory than requested '
        '(last measured use: 446448kb of 1gb)')}),
    # Exit_status is the string "-29" here
    ('qstat-xt-walltime.json', {'27677410.aqua': JobOutcome(
        Outcome.KILLED_TIME, -29, None,
        'killed by PBS: wall time limit exceeded (Exit_status -29)')}),
    ('qstat-xt-qdel.json', {'27677413.aqua': JobOutcome(
        Outcome.CANCELLED, 143, 15, 'deleted with qdel (Exit_status 143)')}),
    ('qstat-xt-array.json', {'27677414[].aqua': JobOutcome(
        Outcome.FAILED, 3, None,
        '2 of 3 workers did not succeed (worker 2: exit status 3; '
        'worker 3: deleted with qdel (Exit_status 143))')}),
    ('qstat-xt-array-ok.json', {'27677412[].aqua': SUCCESS}),
    # 3-failing-setup: setup = "exit 3; ..."
    ('qstat-xt-failing-setup.json', {'27668263.aqua': EXIT_3, '27668695.aqua': EXIT_3,
                                     '27669621.aqua': EXIT_3}),
])
def test_recorded_qstat_outcomes(name, expected):
    assert parse_qstat_outcomes(recorded(name)) == expected


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


def test_golden_submitter():
    spec = replace(SPEC, class_name='submitter', cpus=1, memory=2 * 2**30, time=7 * 86400,
                   max_seconds=None, command=('jug-hpc', 'submit', '--interval=60'))
    golden('submitter.sh', render(spec))
