from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jug_hpc.classes import Classifier
from jug_hpc.config import load_project_settings
from jug_hpc.jugstate import TaskCounts
from jug_hpc.schedulers import JobId, JobOutcome, JobState, JobStatus, Outcome
from jug_hpc.state import JobRecord, JobResources
from jug_hpc.status import (
    WorkerCounts,
    classes_to_release,
    failing_jobs,
    locks_to_release,
    release_messages,
    status,
    worker_counts,
)
from jug_hpc.submitter import SUBMIT_GRACE

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=1)

MEMORY = JobOutcome(Outcome.KILLED_MEMORY, -27, None, 'memory limit exceeded')
TIME = JobOutcome(Outcome.KILLED_TIME, -29, None, 'wall time limit exceeded')
FAILED = JobOutcome(Outcome.FAILED, 3, None, 'exit status 3')
CANCELLED = JobOutcome(Outcome.CANCELLED, 143, 15, 'deleted with qdel')
OK = JobOutcome(Outcome.SUCCESS, 0)

FINISHED = JobStatus(JobState.FINISHED)
UNKNOWN = JobStatus(JobState.UNKNOWN)


def record(job_id, class_name='default', workers=1, scheduler='pbs', submitted=LONG_AGO,
           resources=None):
    return JobRecord(job_id=JobId(job_id), class_name=class_name, scheduler=scheduler,
                     workers=workers, script=Path(f'.jug_hpc/scripts/{class_name}-1.sh'),
                     submitted=submitted, resources=resources)


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'jugfile.py').write_text('')
    (tmp_path / 'jug_hpc.toml').write_text('''\
[default]
memory = "2G"
time = "1h"

[class.big]
match = "big"
memory = "8G"
''')
    return load_project_settings(tmp_path)


def test_worker_counts():
    records = [record('1', 'big', 4), record('2'), record('3', 'big', 2),
               record('4', workers=3, submitted=NOW - timedelta(seconds=10)),
               record('5', workers=5), record('6')]
    statuses = {'1': JobStatus(JobState.RUNNING, queued=1, running=2),
                '2': FINISHED,
                '3': JobStatus(JobState.QUEUED, queued=2),
                '4': UNKNOWN,       # just submitted: all queued
                '5': UNKNOWN}       # forgotten long ago: none
    counts = worker_counts(records, statuses, NOW)
    assert counts == {'big': WorkerCounts(3, 2), 'default': WorkerCounts(3, 0)}
    assert list(counts) == ['big', 'default']
    assert counts['big'].active == 5
    later = NOW + timedelta(seconds=SUBMIT_GRACE)
    assert worker_counts(records, statuses, later)['default'] == WorkerCounts()
    assert worker_counts([], {}, NOW) == {}


def test_status_table(project):
    by_class = {'big': TaskCounts(waiting=1, ready=12, running=3, finished=100),
                'default': TaskCounts(ready=2, failed=1, finished=4)}
    records = [record('11', 'big', 4), record('12'), record('13', 'submitter')]
    statuses = {'11': JobStatus(JobState.RUNNING, queued=1, running=3), '12': FINISHED,
                '13': JobStatus(JobState.RUNNING, running=1)}
    report = status(project, by_class, False, records, statuses, {'12': OK}, NOW,
                    scheduler_name='pbs')
    assert report.text.splitlines() == [
        '                               tasks                        workers',
        'class        waiting  ready  running  failed  finished  queued  running',
        '[class.big]        1     12        3       0       100       1        3',
        '[default]          0      2        0       1         4       0        0',
        'total              1     14        3       1       104       1        3',
        '',
        'Submitter: job 13, running',
    ]
    assert report.warnings == []
    assert report.notes == []


def test_status_submitter(project):
    by_class = {'big': TaskCounts(ready=1), 'default': TaskCounts()}
    records = [record('1', 'submitter'), record('2', 'submitter')]
    statuses = {'1': FINISHED, '2': JobStatus(JobState.QUEUED, queued=1)}

    def line(**kwargs):
        report = status(project, by_class, False, records, statuses, {}, NOW,
                        scheduler_name='pbs', **kwargs)
        return report.text.splitlines()[-1], report.notes

    assert line() == ('Submitter: job 2, queued', [])
    assert line(lock_holder='123@login1') == (
        'Submitter: running (process 123@login1); job 2, queued', [])
    statuses['2'] = FINISHED
    assert line(lock_holder='') == ('Submitter: running', [])
    # the submitter's job runs no task, so it is not in the table
    assert line() == ('Submitter: not running', [
        ("tasks are ready, but no worker is queued or running and no submitter is "
        "running: run 'jug-hpc submit'")])


def test_status_kills(project):
    by_class = {'big': TaskCounts(ready=1), 'default': TaskCounts(finished=3)}
    records = [record('1', 'big'), record('2', 'big'), record('3'), record('4', 'big'),
               record('5', 'old'), record('6', 'big'), record('7')]
    statuses = {r.job_id: FINISHED for r in records}
    outcomes = {'1': MEMORY, '2': TIME, '3': FAILED, '4': MEMORY, '5': TIME,
                '6': CANCELLED, '7': OK}
    report = status(project, by_class, False, records, statuses, outcomes, NOW,
                    scheduler_name='pbs', lock_holder='9@x')
    assert report.warnings == [
        ('[class.big]: 2 jobs killed for memory (the last: job 4: memory limit exceeded); '
         'raise memory (now 8G)'),
        ('[class.big]: 1 job killed for wall time (the last: job 2: wall time limit '
         'exceeded); raise time (now 01:00:00), or lower time_fraction (now 0.8: a task '
         'started after 00:48:00 has 00:12:00 left)'),
        # not [class.old], no longer in jug_hpc.toml
    ]
    # a class no longer in jug_hpc.toml without workers is not in the table
    assert report.notes == []
    assert [line.split()[0] for line in report.text.splitlines()[2:5]] == [
        '[class.big]', '[default]', 'total']

    # ... but it is while it has workers
    records.append(record('8', 'old', 3))
    statuses['8'] = JobStatus(JobState.RUNNING, queued=1, running=2)
    report = status(project, by_class, False, records, statuses, outcomes, NOW,
                    scheduler_name='pbs', lock_holder='9@x')
    assert report.text.splitlines()[4].split() == ['[class.old]', '0', '0', '0', '0', '0',
                                                   '1', '2']
    assert report.notes == [
        ('not in jug_hpc.toml any more, but with workers queued or running: [class.old] '
         '(its tasks are counted in the classes that match them now)')]


GB = 2**30


def test_status_outdated_kills(project):
    '''Kills followed by a success, or of jobs with other resources than now, are
    not reported'''
    by_class = {'big': TaskCounts(ready=1), 'default': TaskCounts(ready=1)}
    now_big = JobResources(cpus=1, memory=8 * GB, time=3600, max_seconds=2880)
    records = [
        record('1', 'big', resources=JobResources(1, 4 * GB, 3600, 2880)),  # 4G then
        record('2', 'big', resources=now_big),
        record('3'),            # recorded without resources
        record('4'),
        record('5'),
        record('6', 'big', resources=now_big),
    ]
    statuses = {r.job_id: FINISHED for r in records}
    outcomes = {'1': MEMORY, '2': TIME, '3': TIME, '4': OK, '5': MEMORY, '6': FAILED}
    report = status(project, by_class, False, records, statuses, outcomes, NOW,
                    scheduler_name='pbs', lock_holder='9@x')
    assert report.warnings == [
        ('[class.big]: 1 job killed for wall time (the last: job 2: wall time limit '
         'exceeded); raise time (now 01:00:00), or lower time_fraction (now 0.8: a task '
         'started after 00:48:00 has 00:12:00 left)'),
        ('[default]: 1 job killed for memory (the last: job 5: memory limit exceeded); '
        'raise memory (now 2G)'),
    ]
    # a job of the class that succeeds makes them all outdated
    records.append(record('7', 'big', resources=now_big))
    outcomes['7'] = OK
    statuses['7'] = FINISHED
    report = status(project, by_class, False, records, statuses, outcomes, NOW,
                    scheduler_name='pbs', lock_holder='9@x')
    assert report.warnings == [
        ('[default]: 1 job killed for memory (the last: job 5: memory limit exceeded); '
        'raise memory (now 2G)')]


def test_status_notes(project):
    finished = {'big': TaskCounts(finished=2), 'default': TaskCounts(finished=1)}
    records = [record('1', scheduler='local'), record('2', scheduler='local'), record('3')]
    report = status(project, finished, False, records, {'3': FINISHED}, {}, NOW,
                    scheduler_name='pbs')
    assert report.notes == [
        'every task is finished',
        ("2 jobs submitted with the 'local' backend not asked about (the backend in use is "
        "'pbs')")]
    # the local jobs are not counted
    assert report.text.splitlines()[3].split() == ['[default]', '0', '0', '0', '0', '1',
                                                   '0', '0']

    blocked = {'big': TaskCounts(failed=2, waiting=1), 'default': TaskCounts(finished=1)}
    report = status(project, blocked, False, [], {}, {}, NOW, scheduler_name='pbs')
    assert report.notes == ['no task can run: failed tasks: [class.big] 2']
    # why they failed (jug_hpc.failures) comes after it
    report = status(project, blocked, False, [], {}, {}, NOW, scheduler_name='pbs',
                    task_failures=['why', 'what next'])
    assert report.notes == ['no task can run: failed tasks: [class.big] 2', 'why', 'what next']

    barrier = {'big': TaskCounts(), 'default': TaskCounts(finished=1)}
    report = status(project, barrier, True, [], {}, {}, NOW, scheduler_name='pbs')
    assert report.notes == []
    assert '(more tasks will appear after a barrier)' in report.text.splitlines()


def test_failing_jobs():
    records = [record('1', 'big'), record('2'), record('3', 'big'), record('4', 'big'),
               record('5'), record('6'), record('7', 'big'), record('8')]
    outcomes = {'1': FAILED, '2': FAILED, '3': MEMORY, '4': TIME, '5': CANCELLED,
                '6': FAILED, '7': FAILED, '8': FAILED}
    big = [records[i] for i in (0, 2, 3, 6)]
    assert failing_jobs(records, outcomes) == {
        'big': tuple((r, outcomes[r.job_id]) for r in big)}
    assert failing_jobs(records, outcomes, limit=2) == {
        'big': tuple((r, outcomes[r.job_id]) for r in big),
        'default': ((records[5], FAILED), (records[7], FAILED))}
    # a job that succeeded, or whose outcome is not known, ends the run
    assert failing_jobs(records, {**outcomes, '3': OK}, limit=2) == {
        'big': ((records[3], TIME), (records[6], FAILED)),
        'default': ((records[5], FAILED), (records[7], FAILED))}
    assert failing_jobs(records, {k: v for k, v in outcomes.items() if k != '4'}) == {}
    assert failing_jobs([], {}) == {}


def test_failing_jobs_outdated():
    '''Jobs submitted with other resources than the class has now end the run'''
    old = JobResources(1, 4 * GB, 3600, 2880)
    new = JobResources(1, 8 * GB, 3600, 2880)
    records = [record('1', 'big', resources=old), record('2', 'big', resources=old),
               record('3', 'big', resources=old), record('4', 'big')]
    outcomes = {r.job_id: FAILED for r in records}
    assert failing_jobs(records, outcomes, resources={'big': new}) == {}
    assert failing_jobs(records, outcomes, limit=1, resources={'big': new}) == {
        'big': ((records[3], FAILED),)}    # recorded without resources: compared to nothing
    assert len(failing_jobs(records, outcomes, resources={'big': old})['big']) == 4
    assert len(failing_jobs(records, outcomes)['big']) == 4


READY_NOTE = ("tasks are ready, but no worker is queued or running and no submitter is "
              "running: run 'jug-hpc submit'")


def test_status_failing_jobs(project):
    by_class = {'big': TaskCounts(ready=2, finished=1), 'default': TaskCounts(finished=3)}
    records = [record('1'), record('2', 'big'), record('3', 'big'), record('4', 'big')]
    statuses = {r.job_id: FINISHED for r in records}
    outcomes = {'1': OK, '2': FAILED, '3': FAILED, '4': MEMORY}

    def report(by_class=by_class, outcomes=outcomes, **kwargs):
        return status(project, by_class, False, records, statuses, outcomes, NOW,
                      scheduler_name='pbs', **kwargs)

    failing = ("[class.big]: tasks are ready, but no worker of the class is queued or "
               'running and its last 3 jobs ended in error (the last: job 4: memory limit '
               'exceeded), which stops a submitter from submitting workers for it; the '
               "worker logs (in logs/) and the scheduler's (in logs/scheduler/) say why the "
               "jobs failed; once fixed, run 'jug-hpc submit'")
    r = report()
    assert r.warnings[-1] == failing
    assert len(r.warnings) == 2     # the memory kill
    # instead of the note
    assert r.notes == []

    # the note is still there for the ready tasks of other classes
    r = report({**by_class, 'default': TaskCounts(ready=1)})
    assert r.warnings[-1] == failing
    assert r.notes == [READY_NOTE]

    # fewer than MAX_FAILED_JOBS in a row: only the note
    r = report(outcomes={**outcomes, '2': OK})
    assert len(r.warnings) == 1
    assert r.notes == [READY_NOTE]
    # jobs cancelled by a user say nothing about whether the workers work
    r = report(outcomes={**outcomes, '4': CANCELLED})
    assert len(r.warnings) == 0
    assert len(r.notes) == 1

    # a running submitter says so in its log (and 'jug-hpc submit' would fail)
    r = report(lock_holder='9@x')
    assert len(r.warnings) == 1
    assert r.notes == []

    # nothing ready in the class
    r = report({'big': TaskCounts(waiting=2, running=1), 'default': TaskCounts(finished=3)})
    assert all('ended in error' not in w for w in r.warnings)

    # jobs submitted with other resources than the class has now (8G): outdated,
    # so only the note
    old = JobResources(1, 4 * GB, 3600, 2880)
    records[1:] = [replace(r, resources=old) for r in records[1:]]
    r = report()
    assert r.warnings == []
    assert r.notes == [READY_NOTE]


def test_status_stale_locks(project):
    by_class = {'big': TaskCounts(running=2, ready=1), 'default': TaskCounts(running=1)}
    records = [record('1', 'big'), record('2')]
    statuses = {'1': FINISHED, '2': FINISHED}
    outcomes = {'1': MEMORY, '2': OK}

    def warnings(records=records, statuses=statuses):
        return status(project, by_class, False, records, statuses, outcomes, NOW,
                      scheduler_name='pbs').warnings

    assert warnings() == [
        ('[class.big]: 1 job killed for memory (the last: job 1: memory limit exceeded); '
         'raise memory (now 8G)'),
        ("[class.big]: 2 tasks are locked, but no worker of the class is queued or running: "
         "unless a 'jug execute' started outside jug-hpc is running them, the workers that "
         'locked them died (e.g., killed by the scheduler) and left their locks behind, so '
         'no worker will run them and the submitter will not finish (remove the locks '
         "with 'jug-hpc status --release-stale')"),
        ("[default]: 1 task is locked, but no worker of the class is queued or running: "
         "unless a 'jug execute' started outside jug-hpc is running it, the worker that "
         'locked it died (e.g., killed by the scheduler) and left its lock behind, so no '
         'worker will run it and the submitter will not finish (remove the lock '
         "with 'jug-hpc status --release-stale')"),
    ]

    # a queued worker (or one just submitted) may have started since it was queried
    more = [*records, record('3', 'big'), record('4', submitted=NOW - timedelta(seconds=5))]
    queued = {**statuses, '3': JobStatus(JobState.QUEUED, queued=1), '4': UNKNOWN}
    assert len(warnings(more, queued)) == 1

    # a worker of a class no longer in jug_hpc.toml may hold the locks
    more = [*records, record('5', 'old')]
    running = {**statuses, '5': JobStatus(JobState.RUNNING, running=1)}
    assert len(warnings(more, running)) == 1


def test_stale_locks_to_release(project):
    by_class = {'big': TaskCounts(running=2), 'default': TaskCounts(running=1)}
    records = [record('1', 'big'), record('2')]
    report = status(project, by_class, False, records, {'1': FINISHED, '2': FINISHED}, {},
                    NOW, scheduler_name='pbs')
    assert report.stale == ('big', 'default')

    # asked again: a worker of the default class was submitted meanwhile
    records.append(record('3', submitted=NOW))
    statuses = {'1': FINISHED, '2': FINISHED, '3': UNKNOWN}
    assert classes_to_release(report.stale, records, statuses, NOW,
                              scheduler_name='pbs') == ('big',)
    # ... but not with this backend; the submitter job does not count
    records[-1] = record('3', scheduler='local')
    records.append(record('4', 'submitter'))
    statuses = {'1': FINISHED, '2': FINISHED, '4': JobStatus(JobState.RUNNING, running=1)}
    assert classes_to_release(report.stale, records, statuses, NOW,
                              scheduler_name='pbs') == ('big', 'default')

    classifier = Classifier.from_project(project)
    locked = {'a.big': ('a1', 'a2'), 'small': ('b1',), 'b.big': ('c1',)}
    assert locks_to_release(classifier, locked, ['big']) == {'big': ('a1', 'a2', 'c1')}
    assert locks_to_release(classifier, locked, ['default', 'big']) == {
        'default': ('b1',), 'big': ('a1', 'a2', 'c1')}
    assert locks_to_release(classifier, {}, ['big']) == {}

    lines, notes = release_messages(('big', 'default'), {'big': ('a1', 'a2')})
    assert lines == ['[class.big]: released the locks of 2 tasks']
    assert notes == [('[default]: locks not released, as a worker of the class is queued '
                      'or running now')]
    lines, _ = release_messages(('default',), {'default': ('a1',)})
    assert lines == ['[default]: released the locks of 1 task']

    # no stale classes while a class no longer in jug_hpc.toml has workers
    statuses['5'] = JobStatus(JobState.RUNNING, running=1)
    records.append(record('5', 'old'))
    assert status(project, by_class, False, records, statuses, {}, NOW,
                  scheduler_name='pbs').stale == ()
