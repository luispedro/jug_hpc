from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from jug_hpc.config import load_project_settings
from jug_hpc.jugstate import STATES, TaskCounts
from jug_hpc.schedulers import JobId, JobOutcome, JobState, JobStatus, Outcome
from jug_hpc.state import JobRecord
from jug_hpc.submitter import (
    MAX_FAILED_JOBS,
    SUBMIT_GRACE,
    Progress,
    active_submitter_jobs,
    active_workers,
    class_limits,
    is_settled,
    jobs_to_query,
    locked_without_workers,
    progress,
    stopped_classes,
    submitter_jobs,
    update_failing,
    workers_to_submit,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=1)


def record(job_id, class_name='default', workers=1, scheduler='pbs', submitted=LONG_AGO):
    return JobRecord(job_id=JobId(job_id), class_name=class_name, scheduler=scheduler,
                     workers=workers, script=Path(f'.jug_hpc/scripts/{class_name}-1.sh'),
                     submitted=submitted)


# --- is_settled / jobs_to_query -------------------------------------------------

def test_is_settled():
    r = record('1')
    assert is_settled(r, JobStatus(JobState.FINISHED), NOW)
    assert is_settled(r, JobStatus(JobState.UNKNOWN), NOW)
    assert not is_settled(r, JobStatus(JobState.QUEUED, queued=1), NOW)
    assert not is_settled(r, JobStatus(JobState.RUNNING, running=1), NOW)


def test_is_settled_unknown_recent_job():
    r = record('1', submitted=NOW - timedelta(seconds=10))
    assert not is_settled(r, JobStatus(JobState.UNKNOWN), NOW)
    assert is_settled(r, JobStatus(JobState.UNKNOWN), NOW, grace=5)
    # finished is final, however recent
    assert is_settled(r, JobStatus(JobState.FINISHED), NOW)
    later = NOW + timedelta(seconds=SUBMIT_GRACE)
    assert is_settled(r, JobStatus(JobState.UNKNOWN), later)


def test_jobs_to_query():
    records = [record('1'), record('2', scheduler='local'), record('3'), record('4')]
    assert [r.job_id for r in jobs_to_query(records, 'pbs')] == ['1', '3', '4']
    assert [r.job_id for r in jobs_to_query(records, 'pbs', {'3'})] == ['1', '4']
    assert [r.job_id for r in jobs_to_query(records, 'local')] == ['2']
    assert jobs_to_query([], 'pbs') == []


def test_submitter_jobs_are_not_queried():
    records = [record('1', 'submitter'), record('2'), record('3', 'submitter', scheduler='local')]
    assert [r.job_id for r in jobs_to_query(records, 'pbs')] == ['2']
    assert [r.job_id for r in submitter_jobs(records, 'pbs')] == ['1']
    assert [r.job_id for r in submitter_jobs(records, 'local')] == ['3']


def test_active_submitter_jobs():
    recent = NOW - timedelta(seconds=30)
    records = [record(str(i), 'submitter') for i in range(1, 5)] + \
        [record('5', 'submitter', submitted=recent), record('6', 'submitter')]
    statuses = {
        '1': JobStatus(JobState.FINISHED),
        '2': JobStatus(JobState.QUEUED, queued=1),
        '3': JobStatus(JobState.RUNNING, running=1),
        '4': JobStatus(JobState.UNKNOWN),
        '5': JobStatus(JobState.UNKNOWN),
    }
    assert [r.job_id for r in active_submitter_jobs(records, statuses, NOW)] == ['2', '3', '5']
    assert active_submitter_jobs([], {}, NOW) == []


# --- active_workers -------------------------------------------------------------

def test_active_workers():
    records = [record('1', 'align', workers=4), record('2', 'align', workers=2),
               record('3', 'default', workers=10), record('4', 'merge')]
    statuses = {
        '1': JobStatus(JobState.RUNNING, queued=1, running=2),
        '2': JobStatus(JobState.QUEUED, queued=2),
        '3': JobStatus(JobState.FINISHED),
        '4': JobStatus(JobState.UNKNOWN),
    }
    assert active_workers(records, statuses, NOW) == {'align': 5, 'default': 0, 'merge': 0}


def test_active_workers_missing_status():
    assert active_workers([record('1', 'align', workers=3)], {}, NOW) == {'align': 0}


def test_active_workers_unknown_recent_job():
    recent = NOW - timedelta(seconds=30)
    records = [record('1', 'align', workers=3, submitted=recent),
               record('2', 'align', workers=2, submitted=recent)]
    statuses = {'1': JobStatus(JobState.UNKNOWN), '2': JobStatus(JobState.FINISHED)}
    assert active_workers(records, statuses, NOW) == {'align': 3}
    assert active_workers(records, statuses, NOW, grace=10) == {'align': 0}


# --- workers_to_submit ----------------------------------------------------------

def test_workers_to_submit():
    by_class = {
        'align': TaskCounts(ready=10, running=4),
        'merge': TaskCounts(waiting=3),
        'small': TaskCounts(ready=2),
        'default': TaskCounts(ready=7, finished=3),
    }
    limits = {'align': 4, 'merge': 1, 'small': 8, 'default': 5}
    active = {'align': 4, 'default': 2}
    # align: capped at 4, all active; merge: nothing ready; small: 2 ready;
    # default: 5 wanted, 2 active
    assert workers_to_submit(by_class, active, limits) == {'small': 2, 'default': 3}


def test_workers_to_submit_more_active_than_wanted():
    by_class = {'default': TaskCounts(ready=1)}
    assert workers_to_submit(by_class, {'default': 3}, {'default': 5}) == {}


def test_workers_to_submit_keeps_class_order():
    by_class = {'b': TaskCounts(ready=1), 'a': TaskCounts(ready=1),
                'default': TaskCounts(ready=1)}
    limits = dict.fromkeys(by_class, 1)
    assert list(workers_to_submit(by_class, {}, limits)) == ['b', 'a', 'default']


def test_workers_to_submit_skips_stopped_classes():
    by_class = {'a': TaskCounts(ready=3), 'default': TaskCounts(ready=2)}
    limits = {'a': 5, 'default': 5}
    assert workers_to_submit(by_class, {}, limits, stopped={'a'}) == {'default': 2}
    assert workers_to_submit(by_class, {}, limits, stopped={'a': ()}) == {'default': 2}


def test_workers_to_submit_ignores_unconfigured_classes():
    # jobs of a class since removed from jug_hpc.toml
    by_class = {'default': TaskCounts(ready=2)}
    assert workers_to_submit(by_class, {'old': 3}, {'default': 2}) == {'default': 2}


counts = st.builds(TaskCounts, *(st.integers(0, 50) for _ in STATES))


@given(c=counts, limit=st.integers(1, 20), active=st.integers(0, 30))
def test_workers_to_submit_property(c, limit, active):
    n = workers_to_submit({'x': c}, {'x': active}, {'x': limit}).get('x', 0)
    assert n >= 0
    # never more workers than ready tasks or the limit, counting the active ones
    if n:
        assert active + n == min(c.ready, limit)
    else:
        assert active >= min(c.ready, limit)


def test_locked_without_workers():
    by_class = {'big': TaskCounts(running=2), 'small': TaskCounts(running=1),
                'default': TaskCounts(ready=3)}
    assert locked_without_workers(by_class, {}) == ('big', 'small')
    assert locked_without_workers(by_class, {'big': 1, 'small': 0}) == ('small',)
    # workers of a class no longer in jug_hpc.toml may run any of them
    assert locked_without_workers(by_class, {'old': 1}) == ()
    assert locked_without_workers(by_class, {'old': 0}) == ('big', 'small')


def test_class_limits(tmp_path):
    (tmp_path / 'jug_hpc.toml').write_text('''
[default]
memory = "1G"
time = "1h"
workers = 10

[class.align]
match = "align"
workers = 2

[class.merge]
match = "merge"
''')
    project = load_project_settings(tmp_path)
    assert list(class_limits(project).items()) == [('align', 2), ('merge', 10),
                                                  ('default', 10)]


# --- update_failing / stopped_classes ---------------------------------------------

FAILED = JobOutcome(Outcome.FAILED, 127, None, 'exit status 127')
OK = JobOutcome(Outcome.SUCCESS, 0)


def test_update_failing_collects_failed_jobs():
    by_class = {'a': TaskCounts(ready=3), 'default': TaskCounts(ready=1)}
    f = update_failing({}, by_class, None, [])
    assert f == {}
    jobs = [(record('1', 'a'), FAILED), (record('2'), FAILED), (record('3', 'a'), FAILED)]
    f = update_failing(f, by_class, by_class, jobs)
    assert f == {'a': (jobs[0], jobs[2]), 'default': (jobs[1],)}
    more = [(record('4', 'a'), JobOutcome(Outcome.KILLED_MEMORY, 271, 9))]
    f = update_failing(f, by_class, by_class, more)
    assert f == {'a': (jobs[0], jobs[2], more[0]), 'default': (jobs[1],)}
    assert stopped_classes(f) == {'a': f['a']}
    assert stopped_classes(f, limit=1) == f
    assert stopped_classes(f, limit=4) == {}


def test_update_failing_reset_by_ended_tasks():
    before = {'a': TaskCounts(ready=3), 'default': TaskCounts(ready=2)}
    failing = {'a': ((record('1', 'a'), FAILED),), 'default': ((record('2'), FAILED),)}
    # a task of a finished, one of default failed: both lists are dropped, and
    # this poll's failures are not counted
    after = {'a': TaskCounts(ready=2, finished=1), 'default': TaskCounts(ready=1, failed=1)}
    new = [(record('3', 'a'), FAILED), (record('4'), FAILED)]
    assert update_failing(failing, after, before, new) == {}
    # fewer ended tasks (e.g. after 'jug invalidate') is not progress
    after = {'a': TaskCounts(ready=3), 'default': TaskCounts(ready=2)}
    before_more = {'a': TaskCounts(finished=3), 'default': TaskCounts(ready=2)}
    assert update_failing(failing, after, before_more, []) == failing


def test_update_failing_reset_by_success():
    by_class = {'a': TaskCounts(ready=3)}
    failing = {'a': ((record('1', 'a'), FAILED), (record('2', 'a'), FAILED))}
    later = (record('4', 'a'), FAILED)
    assert update_failing(failing, by_class, by_class,
                          [(record('3', 'a'), OK), later]) == {'a': (later,)}
    assert update_failing(failing, by_class, by_class,
                          [later, (record('3', 'a'), OK)]) == {}


def test_update_failing_first_poll_and_new_classes():
    # without earlier counts, nothing counts as progress
    by_class = {'a': TaskCounts(finished=3, ready=1)}
    job = (record('1', 'a'), FAILED)
    assert update_failing({}, by_class, None, [job]) == {'a': (job,)}
    assert update_failing({}, by_class, {}, [job]) == {'a': (job,)}


# --- progress -------------------------------------------------------------------

def test_progress_finished():
    r = progress({'a': TaskCounts(finished=3), 'default': TaskCounts()})
    assert r.progress is Progress.FINISHED
    assert r.failed == {}
    assert r.waiting == {}


def test_progress_no_tasks():
    assert progress({'default': TaskCounts()}).progress is Progress.FINISHED


def test_progress_running():
    for c in (TaskCounts(ready=1), TaskCounts(running=1),
              TaskCounts(ready=1, failed=2, waiting=3)):
        r = progress({'a': TaskCounts(finished=5), 'default': c})
        assert r.progress is Progress.RUNNING


def test_progress_blocked():
    r = progress({'a': TaskCounts(failed=2, finished=1), 'b': TaskCounts(waiting=4),
                  'default': TaskCounts(finished=3)})
    assert r.progress is Progress.BLOCKED
    assert r.failed == {'a': 2}
    assert r.waiting == {'b': 4}


def test_progress_barrier():
    by_class = {'default': TaskCounts(finished=3)}
    assert progress(by_class, hit_barrier=True).progress is Progress.RUNNING
    # failed tasks before the barrier block it like any other
    by_class = {'default': TaskCounts(finished=3, failed=1)}
    assert progress(by_class, hit_barrier=True).progress is Progress.BLOCKED


def test_progress_stalled():
    jobs = tuple((record(str(i), 'a'), FAILED) for i in range(MAX_FAILED_JOBS))
    by_class = {'a': TaskCounts(ready=3), 'default': TaskCounts(finished=2, failed=1)}
    r = progress(by_class, stalled={'a': jobs})
    assert r.progress is Progress.STALLED
    assert r.stalled == {'a': jobs}
    assert r.failed == {'default': 1}
    # other ready or running tasks keep it going
    for c in (TaskCounts(ready=1), TaskCounts(running=1)):
        r = progress({**by_class, 'default': c}, stalled={'a': jobs})
        assert r.progress is Progress.RUNNING
        assert r.stalled == {}
    # a stopped class with no ready task does not matter
    by_class = {'a': TaskCounts(finished=3), 'default': TaskCounts(finished=1)}
    assert progress(by_class, stalled={'a': jobs}).progress is Progress.FINISHED
    by_class = {'a': TaskCounts(failed=1), 'default': TaskCounts(waiting=1)}
    r = progress(by_class, stalled={'a': jobs, 'old': jobs})
    assert r.progress is Progress.BLOCKED
    assert r.stalled == {}


def test_progress_waiting_without_cause():
    # not possible with a consistent count; poll again rather than stop
    assert progress({'default': TaskCounts(waiting=2)}).progress is Progress.RUNNING


@given(st.dictionaries(st.sampled_from('abcd'), counts, min_size=1))
def test_progress_property(by_class):
    r = progress(by_class)
    total = sum((c for c in by_class.values()), TaskCounts())
    if r.progress is Progress.FINISHED:
        assert total.done
    elif r.progress is Progress.BLOCKED:
        assert total.ready == total.running == 0
        assert total.failed > 0
    else:
        assert total.ready or total.running or (total.waiting and not total.failed)
