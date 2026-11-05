import dataclasses
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
from jug_hpc.schedulers import (
    JobId,
    JobOutcome,
    JobState,
    JobStatus,
    Outcome,
    SchedulerError,
)
from jug_hpc.schedulers.local import LocalScheduler
from jug_hpc.schedulers.pbs import PBSScheduler
from jug_hpc.state import ProjectState, StateError
from jug_hpc.submitter import MAX_FAILED_JOBS, Progress

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
        self.outcomes = {}           # how finished jobs ended (default: success)
        self.queries = []
        self.outcome_queries = []
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

    def outcome(self, jobs):
        self.outcome_queries.append(list(jobs))
        return {j: self.outcomes.get(j, JobOutcome(Outcome.SUCCESS, 0)) for j in jobs
                if j in self.statuses and self.statuses[j].state is JobState.FINISHED}

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


# --- jobs that keep failing ------------------------------------------------------

EXIT_127 = JobOutcome(Outcome.FAILED, 127, None, 'exit status 127')


def fail_all(sched):
    '''Every job ends, in error'''
    for j in sched.statuses:
        sched.statuses[j] = JobStatus(JobState.FINISHED)
        sched.outcomes.setdefault(j, EXIT_127)


def test_poll_stops_class_whose_jobs_fail(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    counts.set(ALIGN, ready=3)
    r = submitter.poll()
    assert submitted(r) == [('align', 2), ('default', 1)]
    sched.statuses['1002.pbs'] = JobStatus(JobState.RUNNING, running=1)
    counts.set(LOAD, running=1)
    align_jobs = ['1001.pbs', '1003.pbs', '1004.pbs']
    for n, job_id in enumerate(align_jobs[:-1], 1):
        sched.statuses[job_id] = JobStatus(JobState.FINISHED)
        sched.outcomes[job_id] = EXIT_127
        r = submitter.poll()
        assert submitted(r) == [('align', 2)]
        assert r.stopped == {}
        assert [rec.job_id for rec, _ in submitter.failing['align']] == align_jobs[:n]
    sched.statuses['1004.pbs'] = JobStatus(JobState.FINISHED)
    sched.outcomes['1004.pbs'] = EXIT_127
    r = submitter.poll()
    # the default class still runs a task
    assert r.report.progress is Progress.RUNNING
    assert submitted(r) == []
    assert list(r.stopped) == ['align']
    assert [(rec.job_id, o) for rec, o in r.stopped['align']] == [
        (j, EXIT_127) for j in align_jobs]
    # each ended job is asked about once
    assert sched.outcome_queries == [[j] for j in align_jobs]
    r = submitter.poll()
    assert submitted(r) == []
    assert len(sched.outcome_queries) == len(align_jobs)

    # a task of the class ends (e.g. run by hand): workers are submitted again
    counts.set(ALIGN, ready=2, finished=1)
    r = submitter.poll()
    assert r.stopped == {}
    assert submitted(r) == [('align', 2)]


def test_cli_stopped_messages(setup):
    from jug_hpc.cli import _stopped_messages
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, running=1)
    counts.set(ALIGN, ready=3)
    for _ in range(MAX_FAILED_JOBS):
        assert _stopped_messages(set(), submitter.poll()) == []
        fail_all(sched)
    r = submitter.poll()
    assert _stopped_messages(set(), r) == [(
        'warning', ('[class.align]: 3 jobs in a row ended in error before any task of the '
        'class ended (jobs 1001.pbs, 1002.pbs, 1003.pbs; the last: exit status 127); no more workers '
        'are submitted for it while this lasts'))]
    assert _stopped_messages({'align'}, r) == []
    counts.set(ALIGN, ready=2, finished=1)
    assert _stopped_messages({'align'}, submitter.poll()) == [
        ('note', ('[class.align]: a task of the class ended or a job succeeded: '
                 'submitting its workers again'))]


def test_poll_stalled(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, finished=1)
    counts.set(ALIGN, ready=3)
    counts.set(MERGE, waiting=1)
    for _ in range(MAX_FAILED_JOBS):
        assert submitted(submitter.poll()) == [('align', 2)]
        fail_all(sched)
    r = submitter.poll()
    assert r.report.progress is Progress.STALLED
    assert list(r.report.stalled) == ['align']
    assert submitted(r) == []


def test_poll_stopped_class_with_queued_workers(setup):
    '''A stopped class's queued workers may still run its tasks: keep polling'''
    submitter, counts, sched, state, _ = setup
    counts.set(ALIGN, ready=3)
    sched.statuses['99.pbs'] = JobStatus(JobState.QUEUED, queued=1)
    state.add_job('99.pbs', 'align', 'pbs', 1, '.jug_hpc/scripts/align-1.sh')
    for _ in range(MAX_FAILED_JOBS):
        assert submitted(submitter.poll()) == [('align', 1)]
        fail_all(sched)
        sched.statuses['99.pbs'] = JobStatus(JobState.QUEUED, queued=1)
    r = submitter.poll()
    assert r.report.progress is Progress.RUNNING
    assert list(r.stopped) == ['align']
    assert submitted(r) == []
    # it ends in error too: nothing can run
    fail_all(sched)
    r = submitter.poll()
    assert r.report.progress is Progress.STALLED
    assert [rec.job_id for rec, _ in r.report.stalled['align']][-1] == '99.pbs'


def test_poll_ignores_jobs_ended_before_it_started(setup):
    '''Jobs found ended at the first poll may have failed before a fix'''
    submitter, counts, sched, state, _ = setup
    for i in range(5):
        job_id = f'{90 + i}.pbs'
        sched.statuses[job_id] = JobStatus(JobState.FINISHED)
        sched.outcomes[job_id] = EXIT_127
        state.add_job(job_id, 'align', 'pbs', 2, '.jug_hpc/scripts/align-1.sh')
    counts.set(ALIGN, ready=3)
    r = submitter.poll()
    assert submitted(r) == [('align', 2)]
    assert sched.outcome_queries == []
    assert submitter.failing == {}


def test_poll_succeeded_job_clears_failures(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(ALIGN, ready=3)
    for _ in range(MAX_FAILED_JOBS - 1):
        submitter.poll()
        fail_all(sched)
    submitter.poll()
    sched.finish_all()                   # this one succeeds
    r = submitter.poll()
    assert submitter.failing == {}
    assert submitted(r) == [('align', 2)]


# --- jobs killed by the scheduler -------------------------------------------------

KILLED_MEMORY = JobOutcome(Outcome.KILLED_MEMORY, -27, None,
                           'killed by PBS: memory limit exceeded (Exit_status -27)')


def test_poll_reports_ended_jobs(setup):
    '''Each poll gives the jobs it saw end, with their outcomes, once'''
    submitter, counts, sched, state, _ = setup
    state.add_job('99.pbs', 'align', 'pbs', 2, '.jug_hpc/scripts/align-1.sh')
    sched.statuses['99.pbs'] = JobStatus(JobState.FINISHED)    # ended before: left out
    counts.set(LOAD, ready=1)
    counts.set(ALIGN, ready=3)
    r = submitter.poll()
    assert r.ended == []
    assert submitted(r) == [('align', 2), ('default', 1)]
    sched.statuses['1001.pbs'] = JobStatus(JobState.FINISHED)
    sched.outcomes['1001.pbs'] = KILLED_MEMORY
    sched.statuses['1002.pbs'] = JobStatus(JobState.FINISHED)
    r = submitter.poll()
    assert [(rec.job_id, rec.class_name, o) for rec, o in r.ended] == [
        ('1001.pbs', 'align', KILLED_MEMORY),
        ('1002.pbs', 'default', JobOutcome(Outcome.SUCCESS, 0))]
    assert submitter.poll().ended == []


def test_cli_kill_messages(setup):
    from jug_hpc.cli import _kill_message, _kills_by_class
    from jug_hpc.kills import scheduler_kills
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    counts.set(ALIGN, ready=3)
    submitter.poll()
    killed_time = JobOutcome(Outcome.KILLED_TIME, -29, None,
                             'killed by PBS: wall time limit exceeded (Exit_status -29)')
    for job_id, outcome in [('1001.pbs', KILLED_MEMORY), ('1002.pbs', killed_time)]:
        sched.statuses[job_id] = JobStatus(JobState.FINISHED)
        sched.outcomes[job_id] = outcome
    kills = scheduler_kills(submitter.poll().ended)
    project = submitter.project
    assert [_kill_message(rec, o, project) for rec, o in kills] == [
        ('[class.align]: job 1001.pbs ended: killed by PBS: memory limit exceeded '
         '(Exit_status -27); raise memory (now 1G)'),
        ('[default]: job 1002.pbs ended: killed by PBS: wall time limit exceeded '
         '(Exit_status -29); raise time (now 01:00:00), or lower time_fraction (now 0.8: '
         'a task started after 00:48:00 has 00:12:00 left)'),
    ]
    sched.statuses['1003.pbs'] = JobStatus(JobState.FINISHED)
    sched.outcomes['1003.pbs'] = KILLED_MEMORY
    kills += scheduler_kills(submitter.poll().ended)
    assert _kills_by_class(kills) == '[class.align] 2 for memory; [default] 1 for wall time'
    # a class no longer in jug_hpc.toml: no suggestion
    gone = dataclasses.replace(kills[0][0], class_name='gone')
    assert _kill_message(gone, KILLED_MEMORY, project) == (
        '[class.gone]: job 1001.pbs ended: killed by PBS: memory limit exceeded '
        '(Exit_status -27)')


def test_poll_failed_outcome_query_changes_nothing(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(ALIGN, ready=3)
    submitter.poll()
    fail_all(sched)

    def broken(jobs):
        raise SchedulerError('pbs: qstat timed out')

    sched.outcome = broken
    with pytest.raises(SchedulerError):
        submitter.poll()
    assert submitter.settled == set()
    del sched.outcome
    r = submitter.poll()
    assert [rec.job_id for rec, _ in submitter.failing['align']] == ['1001.pbs']
    assert submitted(r) == [('align', 2)]


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


def test_run_backs_off_while_nothing_changes(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, ready=3, waiting=1)
    polls = []
    sleeps = []

    def on_poll(result):
        polls.append(submitted(result))
        n = len(polls)
        if n == 7:
            counts.set(LOAD, running=3, waiting=1)      # the workers start
        elif n == 9:
            sched.finish_all()                          # ... and end
            counts.set(LOAD, finished=4)

    r = run(submitter, interval=10, max_interval=45, on_poll=on_poll, sleep=sleeps.append)
    assert r.report.progress is Progress.FINISHED
    assert polls[0] == [('default', 3)]
    # 1: submitted; 2: workers now active; 3-7: no change; 8: running;
    # 9: no change; 10: finished
    assert sleeps == [10, 10, 20, 40, 45, 45, 45, 10, 20]


def test_run_backoff_reset_by_failure(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    polls = []
    sleeps = []

    def on_poll(result):
        polls.append(submitted(result))
        if len(polls) == 4:
            sched.fail_query = 1        # the next poll fails
        elif len(polls) == 6:
            counts.set(LOAD, finished=1)

    failures = []
    r = run(submitter, interval=1, on_poll=on_poll, on_failure=failures.append,
            sleep=sleeps.append)
    assert r.report.progress is Progress.FINISHED
    assert len(failures) == 1
    # polls 1-4, a failed poll, and polls 5-6 (the first after a failure sees
    # nothing to compare with); the default longest wait is 5 intervals
    assert sleeps == [1, 1, 2, 4, 1, 1, 2]


def test_run_without_backoff(setup):
    submitter, counts, _sched, _state, _ = setup
    counts.set(LOAD, ready=1)
    sleeps = []

    def on_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 4:
            counts.set(LOAD, finished=1)

    run(submitter, interval=3, max_interval=3, sleep=on_sleep)
    assert sleeps == [3, 3, 3, 3]
    with pytest.raises(ValueError, match='max_interval'):
        run(submitter, interval=3, max_interval=2, sleep=pytest.fail)


def test_run_until_blocked(setup):
    submitter, counts, _sched, _state, _ = setup
    counts.set(LOAD, failed=3)
    counts.set(ALIGN, waiting=3)
    r = run(submitter, sleep=pytest.fail)
    assert r.report.progress is Progress.BLOCKED
    assert r.report.failed == {'default': 3}
    assert r.report.waiting == {'align': 3}


def test_run_until_stalled(setup):
    submitter, counts, sched, _state, _ = setup
    counts.set(ALIGN, ready=3)
    sleeps = []
    r = run(submitter, interval=7, on_poll=lambda _: fail_all(sched), sleep=sleeps.append)
    assert r.report.progress is Progress.STALLED
    assert len(r.report.stalled['align']) == MAX_FAILED_JOBS
    assert sleeps == [7] * MAX_FAILED_JOBS


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
    with pytest.raises(SystemExit):
        main(['submit', '--dry-run', '--max-interval=300'])
    assert '--max-interval: only without --all' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(['submit', '--max-interval=30'])
    assert '--max-interval (30 s) is less than --interval (60 s)' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(['submit', '--as-job', '--interval=10', '--max-interval=5'])
    assert '--max-interval (5 s) is less than --interval (10 s)' in capsys.readouterr().err


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


@needs_bash
def test_cli_submit_stops_failing_workers(tmp_path, monkeypatch, capsys):
    '''Workers whose setup fails are not replaced forever'''
    project_dir, sched = local_project(tmp_path, monkeypatch, CHAIN)
    toml = project_dir / 'jug_hpc.toml'
    toml.write_text(toml.read_text().replace('setup = "', 'setup = "exit 127; '))
    status = main(['-C', str(project_dir), 'submit', '--interval=0.2'])
    out, err = capsys.readouterr()
    jobs = [r.job_id for r in ProjectState(project_dir).jobs()]
    wait_for_jobs(sched, jobs)
    assert status == 1, out
    assert jobs == [str(i) for i in range(1, MAX_FAILED_JOBS + 1)]
    detail = ('3 jobs in a row ended in error before any task of the class ended '
              '(jobs 1, 2, 3; the last: 2 of 2 workers did not succeed '
              '(workers 1, 2: exit status 127))')
    assert err == (
        f'jug-hpc: error: no task can run: the workers keep failing: [default]: {detail}\n'
        "jug-hpc: note: the worker logs (in logs/) and the scheduler's (in "
        "logs/scheduler/) say why the jobs failed; once fixed, run 'jug-hpc submit' "
        'again\n')

    # once fixed, a new submitter runs it to the end
    toml.write_text(toml.read_text().replace('setup = "exit 127; ', 'setup = "'))
    status = main(['-C', str(project_dir), 'submit', '--interval=0.2'])
    out, err = capsys.readouterr()
    wait_for_jobs(sched, [r.job_id for r in ProjectState(project_dir).jobs()])
    assert (status, err) == (0, ''), out
    assert out.splitlines()[-1].endswith('every task is finished')


SLOW_TASK = '''\
import time
from jug import TaskGenerator

@TaskGenerator
def slow():
    time.sleep(60)

slow()
'''


@needs_bash
def test_cli_submit_reports_killed_jobs(tmp_path, monkeypatch, capsys):
    '''Jobs killed for wall time are reported, with what to change'''
    toml = '[default]\nmemory = "1G"\ntime = "00:00:02"\nworkers = 1\n'
    project_dir, sched = local_project(tmp_path, monkeypatch, SLOW_TASK, toml)
    status = main(['-C', str(project_dir), 'submit', '--interval=0.2'])
    out, err = capsys.readouterr()
    jobs = [r.job_id for r in ProjectState(project_dir).jobs()]
    wait_for_jobs(sched, jobs)
    assert status == 1, out
    assert jobs == [str(i) for i in range(1, MAX_FAILED_JOBS + 1)]
    killed = ('ended: killed: wall time limit reached; raise time (now 00:00:02), or lower '
              'time_fraction (now 0.8: a task started after 00:00:01 has 00:00:01 left)')
    assert err.splitlines() == [
        *(f'jug-hpc: warning: [default]: job {j} {killed}' for j in jobs),
        'jug-hpc: warning: jobs killed by the scheduler: [default] 3 for wall time',
        ('jug-hpc: error: no task can run: the workers keep failing: [default]: 3 jobs in a '
         'row ended in error before any task of the class ended (jobs 1, 2, 3; the last: '
         'killed: wall time limit reached)'),
        ("jug-hpc: note: the worker logs (in logs/) and the scheduler's (in logs/scheduler/) "
         "say why the jobs failed; once fixed, run 'jug-hpc submit' again"),
    ]


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


# Each task returns the class and CPU count of the worker that ran it
THREE_CLASSES = '''\
import os
from jug import TaskGenerator

def where():
    return (os.environ['JUG_HPC_CLASS'], os.environ['JUG_HPC_CPUS'])

@TaskGenerator
def prepare(i):
    return [where()]

@TaskGenerator
def align(x):
    return x + [where()]

@TaskGenerator
def merge(xs):
    return [x for x in xs] + [[where()]]

@TaskGenerator
def report(xs):
    return xs + [[where()]]

result = report(merge([align(prepare(i)) for i in range(3)]))
'''

THREE_CLASSES_PROJECT = '''\
[default]
memory = "1G"
time = "1h"
workers = 2

[class.align]
match = "align"
cpus = 2
workers = 3

[class.merge]
match = "merge"
cpus = 3
workers = 1
'''


@needs_bash
def test_cli_submit_runs_three_classes(tmp_path, monkeypatch, capsys):
    '''A default -> align -> merge -> default chain: each task runs in its class's workers'''
    project_dir, sched = local_project(tmp_path, monkeypatch, THREE_CLASSES,
                                       THREE_CLASSES_PROJECT)
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
        ('tasks: 5 waiting, 3 ready, 0 running, 0 failed, 0 finished; '
         'workers queued or running: 2'),
    ]
    assert lines[-1] == 'every task is finished'
    assert lines[-2].startswith('tasks: 0 waiting, 0 ready, 0 running, 0 failed, 8 finished')
    # each class is submitted once its tasks are ready, in the order of the chain
    first_seen = list(dict.fromkeys(r.class_name for r in records))
    assert first_seen == ['default', 'align', 'merge'], out
    by_class = {}
    for r in records:
        by_class.setdefault(r.class_name, []).append(r.workers)
    assert all(w <= 3 for w in by_class['align'])
    assert by_class['merge'] == [1]
    for outcome in sched.outcome([r.job_id for r in records]).values():
        assert outcome.outcome is Outcome.SUCCESS, logs
    state = load_project(load_project_settings(project_dir))
    [i] = [i for i, t in enumerate(state.tasks) if t.name == 'jugfile.report']
    *aligned, [merged], [reported] = state.store.load(state.hashes[i].decode())
    prepare_align = [('default', '1'), ('align', '2')]
    assert [[tuple(w) for w in ws] for ws in aligned] == [prepare_align] * 3
    assert tuple(merged) == ('merge', '3')
    assert tuple(reported) == ('default', '1')


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


# --- submit --as-job --------------------------------------------------------------

DOUBLE = '''\
from jug import TaskGenerator

@TaskGenerator
def double(i):
    return 2 * i

results = [double(i) for i in range(3)]
'''

AS_JOB_PROJECT = LOCAL_PROJECT + '[submitter]\ntime = "1h"\n'


@needs_bash
def test_cli_submit_as_job(tmp_path, monkeypatch, capsys):
    '''The submitter runs as a job, and runs the jugfile to the end'''
    project_dir, sched = local_project(tmp_path, monkeypatch, DOUBLE, AS_JOB_PROJECT)
    status = main(['-C', str(project_dir), 'submit', '--as-job', '--interval=0.2',
                   '--max-interval=0.4'])
    out, err = capsys.readouterr()
    assert status == 0, err
    assert err == ''
    assert out == ('submitter: submitted job 1 (.jug_hpc/scripts/submitter-1.sh); '
                   'it logs to logs/submitter-<job id>.log\n')
    [record] = ProjectState(project_dir).jobs()
    assert (record.job_id, record.class_name, record.workers) == ('1', 'submitter', 1)
    script = (project_dir / '.jug_hpc' / 'scripts' / 'submitter-1.sh').read_text()
    assert 'exec jug-hpc submit --interval=0.2 --max-interval=0.4\n' in script
    assert 'JUG_MAX' not in script

    # a second submitter job is refused while the first is queued or running
    status = main(['-C', str(project_dir), 'submit', '--as-job'])
    out, err = capsys.readouterr()
    assert status == 1
    assert out == ''
    assert err == ('jug-hpc: error: a submitter job is already queued or running for '
                   'this project (job 1); nothing submitted\n')

    wait_for_jobs(sched, ['1'])
    records = ProjectState(project_dir).jobs()
    wait_for_jobs(sched, [r.job_id for r in records])
    log = (project_dir / 'logs' / 'submitter-1.log').read_text()
    assert [r.class_name for r in records] == ['submitter', 'default'], log
    assert 'default: submitted job 2 (2 workers, .jug_hpc/scripts/default-1.sh)' in log
    assert log.rstrip('\n').endswith('] every task is finished'), log
    # the submitter does not count itself as a worker
    assert 'workers queued or running: 2\n' in log
    for outcome in sched.outcome([r.job_id for r in records]).values():
        assert outcome.outcome is Outcome.SUCCESS, log
    assert not (project_dir / '.jug_hpc' / 'submitter.lock').read_text()

    # with every task finished, there is nothing to submit
    status = main(['-C', str(project_dir), 'submit', '--as-job'])
    out, err = capsys.readouterr()
    assert status == 0, err
    assert out.endswith('] every task is finished\n')
    assert err == 'jug-hpc: note: no submitter job submitted\n'
    assert len(ProjectState(project_dir).jobs()) == 2


def test_cli_submit_as_job_needs_time(tmp_path, monkeypatch, capsys):
    project_dir, _ = local_project(tmp_path, monkeypatch, DOUBLE)
    status = main(['-C', str(project_dir), 'submit', '--as-job'])
    out, err = capsys.readouterr()
    assert status == 1
    assert out == ''
    assert err == (f"jug-hpc: error: {project_dir / 'jug_hpc.toml'}: [submitter] must set "
                   "'time' for jug-hpc submit --as-job (the wall time of the submitter job, "
                   "which runs until every task is finished, e.g. time = '7d')\n")
    assert not ProjectState(project_dir).jobs()


def test_cli_submit_as_job_refused_while_submitter_runs(tmp_path, monkeypatch, capsys):
    project_dir, _ = local_project(tmp_path, monkeypatch, DOUBLE, AS_JOB_PROJECT)
    state = ProjectState(project_dir)
    with state.submitter_lock():
        status = main(['-C', str(project_dir), 'submit', '--as-job'])
    out, err = capsys.readouterr()
    assert status == 1
    assert out == ''
    assert 'another jug-hpc submit is running for this project' in err
    assert not state.jobs()
    assert not list((project_dir / '.jug_hpc' / 'scripts').glob('*'))


def test_cli_submit_as_job_jugfile_error(tmp_path, monkeypatch, capsys):
    project_dir, _ = local_project(tmp_path, monkeypatch, 'raise ValueError("oops")\n',
                                   AS_JOB_PROJECT)
    status = main(['-C', str(project_dir), 'submit', '--as-job'])
    out, err = capsys.readouterr()
    assert status == 1
    assert out == ''
    assert 'ValueError: oops' in err
    assert not ProjectState(project_dir).jobs()


@pytest.mark.parametrize('argv', [
    ['--as-job', '--all'],
    ['--as-job', '--dry-run'],
    ['--as-job', 'default'],
])
def test_cli_submit_as_job_args(argv, capsys):
    with pytest.raises(SystemExit) as e:
        main(['submit', *argv])
    assert e.value.code == 2
