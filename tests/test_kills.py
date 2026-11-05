from pathlib import Path

import pytest

from jug_hpc.config.project import ResourceClass
from jug_hpc.kills import kill_counts, kill_summary, scheduler_kills, suggestion
from jug_hpc.schedulers import JobOutcome, Outcome
from jug_hpc.state import JobRecord

BIG = ResourceClass(name='big', cpus=1, memory=4 * 1024 ** 3, time=4 * 3600, workers=1,
                    time_fraction=0.8)

SUCCESS = JobOutcome(Outcome.SUCCESS, 0)
FAILED = JobOutcome(Outcome.FAILED, 3, None, 'exit status 3')
MEMORY = JobOutcome(Outcome.KILLED_MEMORY, -27, None, 'memory limit exceeded')
TIME = JobOutcome(Outcome.KILLED_TIME, -29, None, 'wall time limit exceeded')
SIGKILL = JobOutcome(Outcome.KILLED_SIGNAL, 137, 9, 'killed by SIGKILL')
SIGTERM = JobOutcome(Outcome.KILLED_SIGNAL, 143, 15, 'killed by SIGTERM')
CANCELLED = JobOutcome(Outcome.CANCELLED, 143, 15, 'deleted with qdel')
UNKNOWN = JobOutcome(Outcome.UNKNOWN)


def record(job_id, class_name='big'):
    return JobRecord(job_id, class_name, 'pbs', 1, Path('x.sh'))


def test_scheduler_kills():
    ended = [(record(str(i)), o) for i, o in enumerate(
        [SUCCESS, FAILED, MEMORY, TIME, SIGKILL, SIGTERM, CANCELLED, UNKNOWN])]
    assert [o for _, o in scheduler_kills(ended)] == [MEMORY, TIME, SIGKILL, SIGTERM]
    assert scheduler_kills([]) == []


def test_kill_counts():
    kills = [(record('1', 'small'), TIME), (record('2'), SIGKILL), (record('3'), MEMORY),
             (record('4', 'small'), TIME), (record('5'), MEMORY)]
    counts = kill_counts(kills)
    assert counts == {'small': {Outcome.KILLED_TIME: 2},
                      'big': {Outcome.KILLED_MEMORY: 2, Outcome.KILLED_SIGNAL: 1}}
    # classes in the order of their first kill, outcomes from the worst
    assert list(counts) == ['small', 'big']
    assert list(counts['big']) == [Outcome.KILLED_MEMORY, Outcome.KILLED_SIGNAL]
    assert kill_summary(counts['big']) == '2 for memory, 1 by a signal'
    assert kill_summary(counts['small']) == '2 for wall time'


@pytest.mark.parametrize('outcome, expected', [
    (MEMORY, 'raise memory (now 4G)'),
    (TIME, ('raise time (now 04:00:00), or lower time_fraction (now 0.8: a task started '
            'after 03:12:00 has 00:48:00 left)')),
    (SIGKILL, 'if it used more memory than requested, raise memory (now 4G)'),
    (SIGTERM, ''),
    (FAILED, ''),
    (CANCELLED, ''),
    (SUCCESS, ''),
])
def test_suggestion(outcome, expected):
    assert suggestion(outcome, BIG) == expected


def test_suggestion_time_fraction_one():
    rc = ResourceClass(name='x', cpus=1, memory=1024 ** 3, time=2 * 86400, workers=1,
                       time_fraction=1.0)
    assert suggestion(TIME, rc) == (
        'raise time (now 2-00:00:00), or lower time_fraction (now 1: workers take new tasks '
        'until the wall time ends)')
