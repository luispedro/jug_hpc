import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jug_hpc.cli import main
from jug_hpc.config import ConfigError, load_cluster_settings, load_project_settings
from jug_hpc.jugstate import STATES, JugfileError, load_project
from jug_hpc.poll import Submitter, run
from jug_hpc.runner import FakeRunner
from jug_hpc.schedulers import JobId, JobState, JobStatus, Outcome, SchedulerError
from jug_hpc.schedulers.local import LocalScheduler
from jug_hpc.schedulers.pbs import PBSScheduler
from jug_hpc.state import ProjectState, StateError
from jug_hpc.submitter import Progress

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
'''

# The task names of tests/jugfiles/simple.py
LOAD, ALIGN, MERGE = 'jugfile.load', 'jugfile.align_sample', 'jugfile.merge'


class FakePBS(PBSScheduler):
    '''PBS scripts and commands, but jobs are kept in memory'''

    def __init__(self, cluster):
        super().__init__(cluster, FakeRunner())
        self.statuses = {}
        self.queries = []
        self.fail_submit = set()     # classes whose submission fails
        self.fail_query = 0          # number of queries to fail
        self._next = 1000

    def submit(self, script, spec):
        if spec.class_name in self.fail_submit:
            raise SchedulerError(f'pbs: could not submit {script}: qsub: no')
        self._next += 1
        job_id = JobId(f'{self._next}.pbs')
        self.statuses[job_id] = JobStatus(JobState.QUEUED, queued=spec.workers)
        return job_id

    def query(self, jobs):
        self.queries.append(list(jobs))
        if self.fail_query:
            self.fail_query -= 1
            raise SchedulerError('pbs: qstat timed out')
        return {j: self.statuses.get(j, JobStatus(JobState.UNKNOWN)) for j in jobs}

    def finish_all(self):
        for j in self.statuses:
            self.statuses[j] = JobStatus(JobState.FINISHED)


class Counts:
    '''Replies to the task-counting subprocess with ``self.counts``'''

    def __init__(self):
        self.counts = {}
        self.hit_barrier = False
        self.runner = FakeRunner()
        self.runner.on(sys.executable, handler=self.reply)

    def set(self, name, **counts):
        self.counts[name] = {s: counts.get(s, 0) for s in STATES}

    def reply(self, call):
        return json.dumps({'format': 1, 'hit_barrier': self.hit_barrier,
                           'counts': [[n, c] for n, c in self.counts.items()]})


@pytest.fixture
def project(tmp_path):
    '''Returns a function loading (cluster, project, FakePBS, ProjectState)'''
    def load(toml=PROJECT, cluster='scheduler = "pbs"\n'):
        xdg = tmp_path / 'xdg' / 'jug_hpc'
        xdg.mkdir(parents=True, exist_ok=True)
        (xdg / 'cluster.toml').write_text(cluster)
        project_dir = tmp_path / 'proj'
        project_dir.mkdir(exist_ok=True)
        shutil.copy(JUGFILES / 'simple.py', project_dir / 'jugfile.py')
        (project_dir / 'jug_hpc.toml').write_text(toml)
        c = load_cluster_settings(project_dir, environ={'XDG_CONFIG_HOME': str(tmp_path / 'xdg')})
        p = load_project_settings(project_dir)
        return c, p, FakePBS(c), ProjectState(project_dir)
    return load


@pytest.fixture
def setup(project):
    '''A Submitter on simple.py with faked counts and scheduler'''
    cluster, settings, sched, state = project()
    counts = Counts()
    # records get the real time of submission
    clock = [datetime.now(UTC)]
    submitter = Submitter(cluster, settings, sched, state, counts_runner=counts.runner,
                          clock=lambda: clock[0])
    return submitter, counts, sched, state, clock


def submitted(result):
    return [(r.class_name, r.workers) for r in result.submitted]


# --- Submitter.poll -------------------------------------------------------------

def test_poll_follows_ready_tasks(setup):
    submitter, counts, sched, state, _ = setup
    counts.set(LOAD, ready=3)
    counts.set(ALIGN, waiting=3)
    counts.set(MERGE, waiting=1)
    r = submitter.poll()
    assert r.report.progress is Progress.RUNNING
    assert r.active == {}
    assert submitted(r) == [('default', 3)]
    assert sched.queries == []           # nothing recorded yet: no query
    [record] = state.jobs()
    assert record.job_id == '1001.pbs'
    assert record.script == Path('.jug_hpc/scripts/default-1.sh')
    script = (state.project_dir / record.script).read_text()
    assert '#PBS -J 1-3' in script
    assert (state.project_dir / 'logs' / 'scheduler').is_dir()

    # the workers are queued: nothing more to submit
    r = submitter.poll()
    assert r.active == {'default': 3}
    assert submitted(r) == []
    assert sched.queries == [['1001.pbs']]

    # the loads are done; the default workers end, aligns are ready
    sched.statuses['1001.pbs'] = JobStatus(JobState.RUNNING, running=1)
    counts.set(LOAD, finished=3)
    counts.set(ALIGN, ready=3)
    r = submitter.poll()
    assert submitted(r) == [('align', 2)]
    assert r.by_class['align'].ready == 3
    script = (state.project_dir / '.jug_hpc' / 'scripts' / 'align-1.sh').read_text()
    assert 'ncpus=4' in script
    assert '#PBS -J 1-2' in script

    # merge is ready, and a default worker still runs: it will take it
    sched.finish_all()
    sched.statuses['1001.pbs'] = JobStatus(JobState.RUNNING, running=1)
    counts.set(ALIGN, finished=3)
    counts.set(MERGE, ready=1)
    r = submitter.poll()
    assert submitted(r) == []
    assert submitter.settled == {'1002.pbs'}

    # finished jobs are not queried again
    counts.set(MERGE, finished=1)
    r = submitter.poll()
    assert r.report.progress is Progress.FINISHED
    assert sched.queries[-1] == ['1001.pbs']


def test_poll_tops_up_workers(setup):
    submitter, counts, sched, state, _ = setup
    counts.set(ALIGN, ready=10)
    assert submitted(submitter.poll()) == [('align', 2)]
    sched.statuses['1001.pbs'] = JobStatus(JobState.RUNNING, running=1)
    assert submitted(submitter.poll()) == [('align', 1)]
    assert [r.workers for r in state.jobs()] == [2, 1]
    # a single worker is a plain job, not an array
    script = (state.project_dir / '.jug_hpc' / 'scripts' / 'align-2.sh').read_text()
    assert '#PBS -J' not in script


def test_poll_counts_recent_unknown_jobs(setup):
    submitter, counts, sched, _state, clock = setup
    counts.set(LOAD, ready=3)
    assert submitted(submitter.poll()) == [('default', 3)]
    del sched.statuses['1001.pbs']        # not listed by qstat yet
    start = clock[0]
    clock[0] = start + timedelta(seconds=10)
    r = submitter.poll()
    assert r.active == {'default': 3}
    assert submitted(r) == []
    assert submitter.settled == set()
    # long after: the job is gone for good
    clock[0] = start + timedelta(hours=1)
    r = submitter.poll()
    assert submitted(r) == [('default', 3)]
    assert submitter.settled == {'1001.pbs'}


def test_poll_ignores_other_schedulers(setup):
    submitter, counts, sched, state, _ = setup
    state.add_job('7', 'default', 'local', 3, '.jug_hpc/scripts/default-1.sh')
    counts.set(LOAD, ready=3)
    r = submitter.poll()
    assert sched.queries == []
    assert submitted(r) == [('default', 3)]


def test_poll_counts_jobs_of_earlier_runs(setup):
    submitter, counts, sched, state, _ = setup
    sched.statuses['99.pbs'] = JobStatus(JobState.RUNNING, running=2)
    state.add_job('99.pbs', 'default', 'pbs', 4, '.jug_hpc/scripts/default-1.sh')
    counts.set(LOAD, ready=3)
    r = submitter.poll()
    assert r.active == {'default': 2}
    assert submitted(r) == [('default', 1)]


@pytest.mark.parametrize('kind', ['finished', 'blocked'])
def test_poll_submits_nothing_when_over(setup, kind):
    submitter, counts, _sched, state, _ = setup
    if kind == 'finished':
        counts.set(LOAD, finished=3)
    else:
        counts.set(LOAD, failed=1, finished=2)
        counts.set(ALIGN, waiting=3)
    r = submitter.poll()
    assert r.report.progress is (Progress.FINISHED if kind == 'finished' else Progress.BLOCKED)
    assert submitted(r) == []
    assert state.jobs() == []


def test_poll_failed_submission(setup):
    submitter, counts, sched, state, _ = setup
    sched.fail_submit = {'align'}
    counts.set(LOAD, ready=1)
    counts.set(ALIGN, ready=1)
    r = submitter.poll()
    assert submitted(r) == [('default', 1)]
    assert [str(e) for e in r.errors] == [
        'pbs: could not submit .jug_hpc/scripts/align-1.sh: qsub: no']
    assert [j.class_name for j in state.jobs()] == ['default']


def test_poll_default_class_selection(setup):
    '''The default class's selection is built from the task names counted'''
    submitter, counts, _sched, state, _ = setup
    counts.set(LOAD, ready=1)
    counts.set(ALIGN, waiting=1)
    submitter.poll()
    script = (state.project_dir / '.jug_hpc' / 'scripts' / 'default-1.sh').read_text()
    assert 'jug execute' in script
    # --exclude-name with newer Jug, otherwise a --pattern built from the names
    assert ('--exclude-name' in script) or ('--pattern' in script)


def test_poll_overlap_is_an_error(project):
    toml = PROJECT + '[class.other]\nmatch = "align*"\n'
    cluster, settings, sched, state = project(toml)
    counts = Counts()
    counts.set(ALIGN, ready=1)
    submitter = Submitter(cluster, settings, sched, state, counts_runner=counts.runner)
    with pytest.raises(ConfigError, match='align_sample'):
        submitter.poll()


def test_submitter_checks_settings(project):
    cluster, settings, sched, state = project(PROJECT + '[class.align.pbs]\nresources = 3\n')
    with pytest.raises(ConfigError, match=r'\[class\.align\.pbs\]'):
        Submitter(cluster, settings, sched, state)
    cluster, settings, sched, state = project('jugdir = "dict_store"\n' + PROJECT)
    with pytest.raises(ConfigError, match='dict_store'):
        Submitter(cluster, settings, sched, state)


def test_poll_counts_in_a_subprocess(project):
    '''Without a fake counts runner, the jugfile itself is counted'''
    submitter = Submitter(*project())
    r = submitter.poll()
    assert r.by_class['default'].ready == 3
    assert r.by_class['align'].waiting == 3
    assert submitted(r) == [('default', 3)]


# --- run ------------------------------------------------------------------------

def test_run_until_finished(setup):
    submitter, counts, _sched, _state, _ = setup
    counts.set(LOAD, ready=3)
    polls = []
    sleeps = []

    def on_poll(result):
        polls.append(submitted(result))
        if len(polls) == 2:
            counts.set(LOAD, finished=3)

    r = run(submitter, interval=7, on_poll=on_poll, sleep=sleeps.append)
    assert r.report.progress is Progress.FINISHED
    assert polls == [[('default', 3)], [], []]
    assert sleeps == [7, 7]


def test_run_until_blocked(setup):
    submitter, counts, _sched, _state, _ = setup
    counts.set(LOAD, failed=3)
    counts.set(ALIGN, waiting=3)
    r = run(submitter, sleep=pytest.fail)
    assert r.report.progress is Progress.BLOCKED
    assert r.report.failed == {'default': 3}
    assert r.report.waiting == {'align': 3}


def test_run_first_poll_failure_is_fatal(setup):
    submitter, counts, sched, state, _ = setup
    state.add_job('1.pbs', 'default', 'pbs', 1, '.jug_hpc/scripts/default-1.sh')
    sched.fail_query = 1
    counts.set(LOAD, ready=1)
    with pytest.raises(SchedulerError, match='timed out'):
        run(submitter, sleep=pytest.fail)


def test_run_retries_failed_polls(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    failures = []
    polls = []

    def on_poll(result):
        polls.append(submitted(result))
        if len(polls) == 1:
            sched.fail_query = 2      # the next two polls fail
        elif len(polls) == 2:
            counts.set(LOAD, finished=1)

    r = run(submitter, on_poll=on_poll, on_failure=failures.append, sleep=lambda s: None)
    assert r.report.progress is Progress.FINISHED
    assert polls == [[('default', 1)], [], []]
    assert [str(e) for e in failures] == ['pbs: qstat timed out'] * 2


def test_run_gives_up_after_failed_polls(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    failures = []

    def on_poll(result):
        sched.fail_query = 100

    with pytest.raises(SchedulerError):
        run(submitter, on_poll=on_poll, on_failure=failures.append, max_failed=3,
            sleep=lambda s: None)
    assert len(failures) == 3


def test_run_first_failed_submission_is_fatal(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    counts.set(ALIGN, ready=1)
    sched.fail_submit = {'default'}
    polls = []
    with pytest.raises(SchedulerError, match='qsub: no'):
        run(submitter, on_poll=polls.append, sleep=pytest.fail)
    # the other class was submitted, and reported
    assert [submitted(r) for r in polls] == [[('align', 1)]]


def test_run_retries_failed_submissions(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(ALIGN, ready=1)
    polls = []

    def on_poll(result):
        polls.append((submitted(result), len(result.errors)))
        if len(polls) == 1:
            sched.fail_submit = {'default'}
            counts.set(LOAD, ready=1)
        elif len(polls) == 2:
            sched.fail_submit = set()
        elif len(polls) == 3:
            counts.set(LOAD, finished=1)
            counts.set(ALIGN, finished=1)

    failures = []
    r = run(submitter, on_poll=on_poll, on_failure=failures.append, sleep=lambda s: None)
    assert r.report.progress is Progress.FINISHED
    assert polls == [([('align', 1)], 0), ([], 1), ([('default', 1)], 0), ([], 0)]
    assert len(failures) == 1


def test_run_jugfile_error_later_is_retried(setup):
    submitter, counts, _sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    replies = []

    def broken(call):
        replies.append(1)
        if len(replies) == 2:
            return (1, json.dumps({'format': 1, 'error': 'jugfile.py: SyntaxError',
                                   'details': ''}), '')
        if len(replies) >= 3:
            counts.set(LOAD, finished=1)
        return counts.reply(call)

    counts.runner.on(sys.executable, handler=broken)
    failures = []
    r = run(submitter, on_failure=failures.append, sleep=lambda s: None)
    assert r.report.progress is Progress.FINISHED
    assert len(failures) == 1
    assert isinstance(failures[0], JugfileError)


# --- the submitter's lock -------------------------------------------------------

def test_submitter_lock(tmp_path):
    state = ProjectState(tmp_path)
    other = ProjectState(tmp_path)
    with state.submitter_lock():
        lock = tmp_path / '.jug_hpc' / 'submitter.lock'
        assert lock.read_text().startswith(f'{os.getpid()}@')
        match = rf'another jug-hpc submit is running for this project \(process {os.getpid()}@'
        with pytest.raises(StateError, match=match), other.submitter_lock():
            pass
    assert lock.read_text() == ''
    with other.submitter_lock():
        pass


# --- the command line -----------------------------------------------------------

def test_cli_submit_arguments(capsys):
    with pytest.raises(SystemExit):
        main(['submit', 'align'])
    assert 'CLASS: only with --all or --dry-run' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(['submit', '--all', '--interval=3'])
    assert '--interval: only without --all' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(['submit', '--interval=0'])
    assert 'must be positive' in capsys.readouterr().err


def test_cli_submit_locked(tmp_path, monkeypatch, capsys, project):
    _, settings, _, state = project()
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    with state.submitter_lock():
        assert main(['-C', str(settings.project_dir), 'submit']) == 1
    err = capsys.readouterr().err
    assert 'another jug-hpc submit is running for this project' in err
    assert state.jobs() == []


CHAIN = '''\
from jug import TaskGenerator

@TaskGenerator
def first(i):
    return i

@TaskGenerator
def middle(x):
    return x * 2

@TaskGenerator
def last(xs):
    return sum(xs)

total = last([middle(first(i)) for i in range(3)])
'''


def wait_for_jobs(sched, jobs):
    deadline = time.monotonic() + 120
    while any(s.state is not JobState.FINISHED for s in sched.query(jobs).values()):
        assert time.monotonic() < deadline, 'jobs did not finish'
        time.sleep(0.1)


LOCAL_PROJECT = ('[default]\nmemory = "1G"\ntime = "1h"\nworkers = 2\n'
                 '[class.middle]\nmatch = "middle"\nworkers = 5\n')


def local_project(tmp_path, monkeypatch, jugfile, toml=LOCAL_PROJECT):
    toml = 'setup = "export PATH=' + str(Path(sys.executable).parent) + ':$PATH"\n' + toml
    xdg = tmp_path / 'xdg' / 'jug_hpc'
    xdg.mkdir(parents=True)
    (xdg / 'cluster.toml').write_text('scheduler = "local"\n')
    project_dir = tmp_path / 'proj'
    project_dir.mkdir()
    (project_dir / 'jugfile.py').write_text(jugfile)
    (project_dir / 'jug_hpc.toml').write_text(toml)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    cluster = load_cluster_settings(project_dir)
    sched = LocalScheduler(cluster, jobs_dir=tmp_path / 'state' / 'jug_hpc' / 'local')
    return project_dir, sched


@needs_bash
def test_cli_submit_runs_chain(tmp_path, monkeypatch, capsys):
    '''A default -> middle -> default chain runs to completion with the local backend'''
    project_dir, sched = local_project(tmp_path, monkeypatch, CHAIN)
    status = main(['-C', str(project_dir), 'submit', '--interval=0.2'])
    out, err = capsys.readouterr()
    records = ProjectState(project_dir).jobs()
    wait_for_jobs(sched, [r.job_id for r in records])
    logs = '\n'.join(p.read_text() for p in (project_dir / 'logs').glob('*.log'))
    assert status == 0, err + out + logs
    assert err == ''
    lines = [line.split('] ', 1)[1] for line in out.splitlines()]
    assert lines[:2] == [
        'default: submitted job 1 (2 workers, .jug_hpc/scripts/default-1.sh)',
        ('tasks: 4 waiting, 3 ready, 0 running, 0 failed, 0 finished; '
         'workers queued or running: 2'),
    ]
    assert lines[-1] == 'every task is finished'
    assert lines[-2].startswith('tasks: 0 waiting, 0 ready, 0 running, 0 failed, 7 finished')
    # the middle tasks were submitted only once they were ready
    classes = [r.class_name for r in records]
    assert classes[0] == 'default'
    assert 'middle' in classes
    for outcome in sched.outcome([r.job_id for r in records]).values():
        assert outcome.outcome is Outcome.SUCCESS, logs
    state = load_project(load_project_settings(project_dir))
    assert all(t.can_load(state.store) for t in state.tasks)
    assert not (project_dir / '.jug_hpc' / 'submitter.lock').read_text()


FAILING_TASK = '''\
from jug import TaskGenerator

@TaskGenerator
def first(i):
    if i == 1:
        raise ValueError('no')
    return i

@TaskGenerator
def middle(x):
    return x * 2

[middle(first(i)) for i in range(2)]
'''


@needs_bash
def test_cli_submit_blocked(tmp_path, monkeypatch, capsys):
    project_dir, sched = local_project(tmp_path, monkeypatch, FAILING_TASK)
    status = main(['-C', str(project_dir), 'submit', '--interval=0.2'])
    out, err = capsys.readouterr()
    # the middle worker waits for the task whose dependency failed (Jug does
    # not know it never will run): stop it
    jobs = [r.job_id for r in ProjectState(project_dir).jobs()]
    sched.cancel(jobs)
    wait_for_jobs(sched, jobs)
    assert status == 1, out
    assert err == ('jug-hpc: error: no task can run: failed tasks: [default] 1; '
                   'tasks waiting for them: [class.middle] 1\n'
                   "jug-hpc: note: the worker logs say why the tasks failed; once fixed, "
                   "'jug cleanup --failed-only' lets them run again\n")


BARRIER = '''\
from jug import TaskGenerator, barrier

@TaskGenerator
def first(n):
    return list(range(n))

@TaskGenerator
def middle(x):
    return x * 2

@TaskGenerator
def last(xs):
    return sum(xs)

blocks = first(3)
barrier()
total = last([middle(b) for b in blocks.value()])
'''


@needs_bash
def test_cli_submit_runs_barrier(tmp_path, monkeypatch, capsys):
    '''Tasks created after a barrier() are seen once the tasks before it finish'''
    project_dir, sched = local_project(tmp_path, monkeypatch, BARRIER)
    status = main(['-C', str(project_dir), 'submit', '--interval=0.2'])
    out, err = capsys.readouterr()
    records = ProjectState(project_dir).jobs()
    wait_for_jobs(sched, [r.job_id for r in records])
    logs = '\n'.join(p.read_text() for p in (project_dir / 'logs').glob('*.log'))
    assert status == 0, err + out + logs
    assert err == ''
    lines = [line.split('] ', 1)[1] for line in out.splitlines()]
    # only the task before the barrier is known at first
    assert lines[:2] == [
        'default: submitted job 1 (1 worker, .jug_hpc/scripts/default-1.sh)',
        ('tasks: 0 waiting, 1 ready, 0 running, 0 failed, 0 finished (more after a barrier); '
         'workers queued or running: 1'),
    ]
    assert lines[-1] == 'every task is finished'
    assert lines[-2].startswith('tasks: 0 waiting, 0 ready, 0 running, 0 failed, 5 finished')
    assert [r.class_name for r in records][:2] == ['default', 'middle']
    for outcome in sched.outcome([r.job_id for r in records]).values():
        assert outcome.outcome is Outcome.SUCCESS, logs
    state = load_project(load_project_settings(project_dir))
    assert not state.hit_barrier
    [i] = [i for i, t in enumerate(state.tasks) if t.name == 'jugfile.last']
    assert state.store.load(state.hashes[i].decode()) == 6


SLOW_TASKS = '''\
import time
from jug import TaskGenerator

@TaskGenerator
def slow(i):
    time.sleep(0.5)
    return i

results = [slow(i) for i in range(6)]
'''


@needs_bash
def test_cli_submit_replaces_stopped_workers(tmp_path, monkeypatch, capsys):
    '''Workers that stop because of JUG_MAX_SECONDS are replaced until the tasks are done'''
    toml = '[default]\nmemory = "1G"\ntime = "00:00:10"\ntime_fraction = 0.1\nworkers = 1\n'
    project_dir, sched = local_project(tmp_path, monkeypatch, SLOW_TASKS, toml)
    status = main(['-C', str(project_dir), 'submit', '--interval=0.2'])
    out, err = capsys.readouterr()
    records = ProjectState(project_dir).jobs()
    wait_for_jobs(sched, [r.job_id for r in records])
    logs = '\n'.join(p.read_text() for p in (project_dir / 'logs').glob('*.log'))
    assert status == 0, err + out + logs
    assert err == ''
    assert out.splitlines()[-1].endswith('every task is finished')
    # each worker runs about two tasks (JUG_MAX_SECONDS=1) and ends by itself
    assert len(records) >= 2, out
    assert all(r.workers == 1 for r in records)
    for outcome in sched.outcome([r.job_id for r in records]).values():
        assert outcome.outcome is Outcome.SUCCESS, logs
    state = load_project(load_project_settings(project_dir))
    assert all(t.can_load(state.store) for t in state.tasks)
