'''``jug-hpc cancel``: unit tests with a fake scheduler, and end-to-end tests
with the local backend'''
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_poll import local_project, needs_bash, wait_for_jobs

from jug_hpc.cancel import CancelError, cancel, describe_status
from jug_hpc.cli import main
from jug_hpc.schedulers import JobId, JobState, JobStatus, Outcome, SchedulerError
from jug_hpc.state import JobRecord, ProjectState
from jug_hpc.submitter import SUBMIT_GRACE

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=1)

QUEUED = JobStatus(JobState.QUEUED, queued=1)
RUNNING = JobStatus(JobState.RUNNING, running=1)
FINISHED = JobStatus(JobState.FINISHED)
UNKNOWN = JobStatus(JobState.UNKNOWN)


class FakeScheduler:
    '''Jobs in a dict; a cancelled job finishes after ``stop_after`` queries'''

    name = 'pbs'

    def __init__(self, statuses=None, stop_after=0):
        self.statuses = dict(statuses or {})
        self.stop_after = stop_after
        self.stopping = {}          # job -> queries left before it finishes
        self.calls = []
        self.on_query = None

    def query(self, jobs):
        self.calls.append(('query', list(jobs)))
        if self.on_query is not None:
            self.on_query()
        for j in list(self.stopping):
            if self.stopping[j] == 0:
                self.statuses[j] = FINISHED
                del self.stopping[j]
            else:
                self.stopping[j] -= 1
        return {j: self.statuses.get(j, UNKNOWN) for j in jobs}

    def cancel(self, jobs):
        self.calls.append(('cancel', list(jobs)))
        for j in jobs:
            if j in self.statuses and self.statuses[j].state is not JobState.FINISHED:
                self.stopping[j] = self.stop_after

    def cancelled(self):
        return [c[1] for c in self.calls if c[0] == 'cancel']


@pytest.fixture
def state(tmp_path):
    (tmp_path / 'jug_hpc.toml').write_text('')
    return ProjectState(tmp_path)


def add(state, job_id, class_name='default', workers=1, scheduler='pbs', submitted=LONG_AGO):
    return state.add_job(JobId(job_id), class_name, scheduler, workers,
                         Path(f'.jug_hpc/scripts/{class_name}-1.sh'), submitted)


class Clock:
    '''A clock that the fake sleep advances'''

    def __init__(self):
        self.now = NOW
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def run_cancel(sched, state, clock=None, **kwargs):
    clock = clock if clock is not None else Clock()
    return cancel(sched, state, clock=clock, sleep=clock.sleep, **kwargs)


def summary(result):
    return [(r.job_id, r.class_name, s) for r, s in result.cancelled]


def test_cancel_active_jobs(state):
    add(state, '1', 'big', 4)
    add(state, '2')
    add(state, '3')
    add(state, '4', 'big', 2)
    add(state, '5', submitted=NOW - timedelta(seconds=10))     # just submitted
    add(state, '6')                                             # forgotten long ago
    add(state, '7', scheduler='local')
    add(state, '8', scheduler='local')
    array = JobStatus(JobState.RUNNING, queued=1, running=2)
    sched = FakeScheduler({'1': array, '2': FINISHED, '3': QUEUED,
                           '4': JobStatus(JobState.QUEUED, queued=2)})
    result = run_cancel(sched, state)
    assert summary(result) == [('1', 'big', array), ('3', 'default', QUEUED),
                               ('4', 'big', sched.statuses['4']), ('5', 'default', UNKNOWN)]
    assert sched.cancelled() == [['1', '3', '4', '5']]
    assert result.others == {'local': 2}
    assert result.submitter_left == []

    # again: everything has ended, except job 5, which is still not listed (and
    # cancelled again: harmless)
    sched.query([])
    result = run_cancel(sched, state)
    assert summary(result) == [('5', 'default', UNKNOWN)]
    assert sched.cancelled()[1:] == [['5']]


def test_cancel_nothing_recorded(state):
    sched = FakeScheduler()
    result = run_cancel(sched, state)
    assert (result.cancelled, result.others, result.submitter_left) == ([], {}, [])
    assert sched.calls == []        # nothing to ask about


def test_cancel_unknown_after_grace(state):
    add(state, '1', submitted=NOW - timedelta(seconds=SUBMIT_GRACE + 1))
    assert run_cancel(FakeScheduler(), state).cancelled == []


def test_cancel_submitter_first(state):
    '''The submitter job is cancelled first; workers it submits meanwhile are
    cancelled too'''
    add(state, '1', 'submitter')
    add(state, '2')
    sched = FakeScheduler({'1': RUNNING, '2': RUNNING}, stop_after=2)

    def submit_more():
        # the submitter, still running, submits a worker
        if not any(r.job_id == '3' for r in state.jobs()):
            add(state, '3', workers=2, submitted=NOW)
            sched.statuses['3'] = JobStatus(JobState.QUEUED, queued=2)
    sched.on_query = submit_more
    clock = Clock()
    result = run_cancel(sched, state, clock)
    assert summary(result) == [('1', 'submitter', RUNNING), ('2', 'default', RUNNING),
                               ('3', 'default', JobStatus(JobState.QUEUED, queued=2))]
    assert sched.cancelled() == [['1'], ['2', '3']]
    assert result.submitter_left == []
    assert clock.slept == [2, 2]
    # waiting queries the submitter job only
    assert sched.calls[1:5] == [('cancel', ['1']), ('query', ['1']), ('query', ['1']),
                                ('query', ['1'])]


def test_cancel_submitter_does_not_stop(state):
    '''Workers are cancelled anyway, and the result says so'''
    add(state, '1', 'submitter')
    add(state, '2')
    sched = FakeScheduler({'1': RUNNING, '2': QUEUED}, stop_after=1000)
    clock = Clock()
    result = run_cancel(sched, state, clock, wait=10, check_every=3)
    assert [r.job_id for r in result.submitter_left] == ['1']
    assert sched.cancelled() == [['1'], ['2']]
    assert clock.slept == [3, 3, 3, 3]
    assert clock.now - NOW >= timedelta(seconds=10)


def test_cancel_waits_for_lock(state):
    '''A submitter job the scheduler says has ended, but whose lock is still held'''
    add(state, '1', 'submitter')
    sched = FakeScheduler({'1': RUNNING})
    with state.submitter_lock():
        result = run_cancel(sched, state, wait=4, check_every=2)
    assert [r.job_id for r in result.submitter_left] == ['1']


def test_cancel_refused_while_submitter_runs_outside(state):
    add(state, '1')
    add(state, '2', 'submitter')
    sched = FakeScheduler({'1': RUNNING, '2': FINISHED})
    with state.submitter_lock(), pytest.raises(CancelError) as e:
        run_cancel(sched, state)
    assert str(e.value).startswith(
        'a submitter is running for this project outside the scheduler (process ')
    assert str(e.value).endswith(
        "stop it first (Ctrl-C), then run 'jug-hpc cancel' again; nothing cancelled")
    assert sched.cancelled() == []


def test_cancel_scheduler_error(state):
    add(state, '1')

    class Failing(FakeScheduler):
        def cancel(self, jobs):
            raise SchedulerError('pbs: could not cancel jobs: no')
    with pytest.raises(SchedulerError, match='could not cancel'):
        run_cancel(Failing({'1': RUNNING}), state)


def record(workers):
    return JobRecord(job_id=JobId('1'), class_name='default', scheduler='pbs',
                     workers=workers, script=Path('x.sh'), submitted=NOW)


@pytest.mark.parametrize('workers, status, text', [
    (1, QUEUED, 'queued'),
    (1, RUNNING, 'running'),
    (1, UNKNOWN, 'not listed by the scheduler yet'),
    (4, UNKNOWN, 'not listed by the scheduler yet'),
    (4, JobStatus(JobState.QUEUED, queued=4), '4 workers queued'),
    (4, JobStatus(JobState.RUNNING, running=1), '1 worker running'),
    (4, JobStatus(JobState.RUNNING, queued=1, running=2), '1 worker queued, 2 running'),
    (4, JobStatus(JobState.RUNNING, queued=3, running=1), '3 workers queued, 1 running'),
])
def test_describe_status(workers, status, text):
    assert describe_status(record(workers), status) == text


# --- end to end, with the local backend --------------------------------------------

SLOW = '''
import time
from jug import TaskGenerator

@TaskGenerator
def slow(i):
    time.sleep(60)

@TaskGenerator
def other(i):
    time.sleep(60)

[slow(i) for i in range(2)]
other(0)
'''

SLOW_PROJECT = ('[default]\nmemory = "1G"\ntime = "1h"\nworkers = 2\n'
                '[class.other]\nmatch = "other"\n')


def outcomes(sched, project_dir):
    jobs = [r.job_id for r in ProjectState(project_dir).jobs()]
    wait_for_jobs(sched, jobs)
    return {j: o.outcome for j, o in sched.outcome(jobs).items()}


@needs_bash
def test_cli_cancel(tmp_path, monkeypatch, capsys):
    project_dir, sched = local_project(tmp_path, monkeypatch, SLOW, SLOW_PROJECT)
    assert main(['-C', str(project_dir), 'submit', '--all']) == 0
    capsys.readouterr()
    status = main(['-C', str(project_dir), 'cancel'])
    out, err = capsys.readouterr()
    assert status == 0, err
    assert err == ''
    assert out.splitlines() == [
        'other: cancelled job 1 (queued)',
        'default: cancelled job 2 (2 workers queued)',
    ] or out.splitlines() == [   # if the workers started already
        'other: cancelled job 1 (running)',
        'default: cancelled job 2 (2 workers running)',
    ]
    assert outcomes(sched, project_dir) == {'1': Outcome.CANCELLED, '2': Outcome.CANCELLED}

    # nothing left to cancel; no job reported as killed
    status = main(['-C', str(project_dir), 'cancel'])
    out, err = capsys.readouterr()
    assert (status, out) == (0, '')
    assert err == 'jug-hpc: note: no job of this project is queued or running\n'
    main(['-C', str(project_dir), 'status'])
    out, err = capsys.readouterr()
    assert 'killed' not in err
    # the workers released the locks of their tasks: nothing is running
    assert out.splitlines()[-3].split() == ['total', '0', '3', '0', '0', '0', '0', '0']


@needs_bash
def test_cli_cancel_submitter_job(tmp_path, monkeypatch, capsys):
    '''The submitter job is cancelled first, then the workers it submitted'''
    toml = SLOW_PROJECT + '[submitter]\ntime = "1h"\n'
    project_dir, sched = local_project(tmp_path, monkeypatch, SLOW, toml)
    assert main(['-C', str(project_dir), 'submit', '--as-job', '--interval=0.2']) == 0
    capsys.readouterr()
    state = ProjectState(project_dir)
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    while len(state.jobs()) < 3:
        assert datetime.now(UTC) < deadline, 'the submitter submitted no workers'
        time.sleep(0.1)
    status = main(['-C', str(project_dir), 'cancel'])
    out, err = capsys.readouterr()
    assert status == 0, err
    assert err == ''
    lines = out.splitlines()
    assert lines[0] == 'submitter: cancelled job 1 (running)'
    assert sorted(line.split(' (')[0] for line in lines[1:]) == [
        'default: cancelled job 3', 'other: cancelled job 2'] or sorted(
        line.split(' (')[0] for line in lines[1:]) == [
        'default: cancelled job 2', 'other: cancelled job 3']
    assert state.submitter_lock_holder() is None
    assert set(outcomes(sched, project_dir).values()) == {Outcome.CANCELLED}
    assert len(state.jobs()) == 3


def test_cli_cancel_refused_while_submitter_runs(tmp_path, monkeypatch, capsys):
    project_dir, _ = local_project(tmp_path, monkeypatch, SLOW, SLOW_PROJECT)
    state = ProjectState(project_dir)
    with state.submitter_lock():
        status = main(['-C', str(project_dir), 'cancel'])
    out, err = capsys.readouterr()
    assert (status, out) == (1, '')
    assert err.startswith('jug-hpc: error: a submitter is running for this project outside '
                          'the scheduler (process ')


def test_cli_cancel_errors(tmp_path, monkeypatch, capsys):
    status = main(['-C', str(tmp_path), 'cancel'])
    _, err = capsys.readouterr()
    assert status == 1
    assert err.startswith('jug-hpc: error: no jug_hpc.toml found in ')

    # jug_hpc.toml is not read, nor is the jugfile loaded
    project_dir, _ = local_project(tmp_path, monkeypatch, 'raise ValueError("oops")\n',
                                   'no such key = 1\n')
    ProjectState(project_dir).add_job('1.pbs01', 'default', 'pbs', 2, 'x.sh')
    status = main(['-C', str(project_dir), 'cancel'])
    _, err = capsys.readouterr()
    assert status == 0, err
    assert err.splitlines() == [
        ("jug-hpc: note: 1 job submitted with the 'pbs' backend not cancelled (the backend "
         "in use is 'local')"),
        'jug-hpc: note: no job of this project is queued or running',
    ]
