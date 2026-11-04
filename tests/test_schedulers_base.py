from pathlib import Path
from types import SimpleNamespace

import pytest

from jug_hpc.config import ClusterSettings, ConfigError
from jug_hpc.runner import FakeRunner, SubprocessRunner
from jug_hpc.schedulers import (
    JobId,
    JobOutcome,
    JobState,
    JobStatus,
    Outcome,
    Scheduler,
    SchedulerError,
    UnknownSchedulerError,
    base,
    combine_outcomes,
    create_scheduler,
    get_scheduler,
    scheduler_names,
)
from jug_hpc.worker import WorkerSpec


class FakeScheduler(Scheduler):
    name = 'fake'

    def render_script(self, spec):
        return f'#!/bin/sh\n# {spec.job_name}\n'

    def submit_command(self, script, spec):
        return ['fsub', '-N', spec.job_name, str(script)]

    def parse_submit_output(self, stdout):
        if not stdout.strip():
            raise SchedulerError('no job id')
        return JobId(stdout.strip())

    def query(self, jobs):
        return {j: JobStatus(JobState.UNKNOWN) for j in jobs}

    def outcome(self, jobs):
        return {}

    def cancel(self, jobs):
        pass


class Misnamed(FakeScheduler):
    name = 'other'


def ep(name, obj, value=None):
    def load():
        if isinstance(obj, Exception):
            raise obj
        return obj
    return SimpleNamespace(name=name, value=value or f'pkg:{name}', load=load)


@pytest.fixture
def registry(monkeypatch):
    eps = []
    monkeypatch.setattr(base, '_entry_points', lambda: eps)
    return eps


SPEC = WorkerSpec(class_name='a', project_name='p', workdir=Path('/proj'),
                  cpus=1, memory=2**30, time=60, max_seconds=48)


def test_get_scheduler(registry):
    registry += [ep('fake', FakeScheduler), ep('fake', FakeScheduler)]  # duplicates ok
    assert scheduler_names() == ['fake']
    assert get_scheduler('fake') is FakeScheduler


def test_unknown_scheduler(registry):
    registry += [ep('fake', FakeScheduler), ep('pbs', FakeScheduler)]
    with pytest.raises(UnknownSchedulerError) as e:
        get_scheduler('pbss', where='/h/cluster.toml')
    assert isinstance(e.value, ConfigError)
    assert str(e.value) == ("/h/cluster.toml: unknown scheduler 'pbss' "
                            "(did you mean 'pbs'? available: fake, pbs)")
    assert e.value.known == ('fake', 'pbs')
    with pytest.raises(UnknownSchedulerError, match=r"'lsf' \(available: fake, pbs\)"):
        get_scheduler('lsf')


def test_no_schedulers(registry):
    with pytest.raises(UnknownSchedulerError, match='no scheduler backends are installed'):
        get_scheduler('pbs')


def test_conflicting_registrations(registry):
    registry += [ep('fake', FakeScheduler, 'a:X'), ep('fake', FakeScheduler, 'b:Y')]
    with pytest.raises(SchedulerError, match='a:X and b:Y'):
        scheduler_names()


@pytest.mark.parametrize(('obj', 'message'), [
    (ImportError('no module named pkg'), 'could not be loaded: ImportError'),
    (object, 'not a Scheduler subclass'),
    (FakeScheduler(None), 'not a Scheduler subclass'),
    (Misnamed, "calls itself 'other'"),
])
def test_broken_backend(registry, obj, message):
    registry.append(ep('fake', obj))
    with pytest.raises(SchedulerError, match=message):
        get_scheduler('fake')


def test_create_scheduler(registry):
    registry.append(ep('fake', FakeScheduler))
    cluster = ClusterSettings(scheduler='fake', sources={('scheduler',): Path('/c.toml')})
    runner = FakeRunner()
    s = create_scheduler(cluster, runner)
    assert isinstance(s, FakeScheduler)
    assert s.cluster is cluster
    assert s.runner is runner
    assert isinstance(create_scheduler(cluster).runner, SubprocessRunner)

    with pytest.raises(UnknownSchedulerError, match=r'^/c\.toml: unknown scheduler'):
        create_scheduler(ClusterSettings(scheduler='x',
                                         sources={('scheduler',): Path('/c.toml')}))


def test_installed_backends_are_valid():
    # Whatever is registered in this environment must load
    for name in scheduler_names():
        assert get_scheduler(name).name == name


def test_submit():
    runner = FakeRunner().on('fsub', stdout='42.server\n')
    s = FakeScheduler(ClusterSettings(scheduler='fake'), runner)
    assert s.submit(Path('/proj/x.sh'), SPEC) == '42.server'
    assert runner.calls[0].argv == ('fsub', '-N', 'jug-a-p', '/proj/x.sh')
    assert runner.calls[0].cwd == Path('/proj')


def test_submit_failure():
    runner = FakeRunner().on('fsub', returncode=1, stderr='fsub: queue is closed')
    s = FakeScheduler(ClusterSettings(scheduler='fake'), runner)
    with pytest.raises(SchedulerError, match='could not submit /x.sh(.|\n)*queue is closed'):
        s.submit(Path('/x.sh'), SPEC)


def test_submit_bad_output():
    s = FakeScheduler(ClusterSettings(scheduler='fake'), FakeRunner().on('fsub'))
    with pytest.raises(SchedulerError, match='no job id'):
        s.submit(Path('/x.sh'), SPEC)


def test_validate_raw_options_default():
    s = FakeScheduler(ClusterSettings(scheduler='fake'))
    assert s.validate_raw_options({'anything': {'goes': 1}}, 'x') is None


def test_abstract():
    with pytest.raises(TypeError):
        Scheduler(ClusterSettings(scheduler='x'))


def test_job_status():
    assert JobStatus(JobState.FINISHED).active == 0
    assert JobStatus(JobState.RUNNING, queued=2, running=3).active == 5
    with pytest.raises(ValueError, match='negative'):
        JobStatus(JobState.RUNNING, running=-1)


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
