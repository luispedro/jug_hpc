import json
import os
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jug_hpc.state import FORMAT_VERSION, STATE_DIR, JobRecord, ProjectState, StateError

T0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)


# --- scripts ----------------------------------------------------------------------

def test_nothing_created_until_written(tmp_path):
    state = ProjectState(tmp_path)
    assert state.jobs() == []
    assert not (tmp_path / STATE_DIR).exists()


def test_write_script(tmp_path):
    state = ProjectState(tmp_path)
    p = state.write_script('big', '#!/bin/bash\necho hi\n')
    assert p == tmp_path / '.jug_hpc' / 'scripts' / 'big-1.sh'
    assert p.read_text() == '#!/bin/bash\necho hi\n'
    assert os.access(p, os.X_OK)
    assert (tmp_path / '.jug_hpc' / '.gitignore').read_text().splitlines()[-1] == '*'


def test_script_numbers_per_class(tmp_path):
    state = ProjectState(tmp_path)
    names = [state.write_script(c, '').name for c in ['a', 'a', 'b', 'a', 'a-b', 'b']]
    assert names == ['a-1.sh', 'a-2.sh', 'b-1.sh', 'a-3.sh', 'a-b-1.sh', 'b-2.sh']


def test_script_numbers_not_reused(tmp_path):
    state = ProjectState(tmp_path)
    for _ in range(3):
        state.write_script('a', '')
    (state.scripts_dir / 'a-2.sh').unlink()
    (state.scripts_dir / 'a-notes.sh').write_text('')
    assert state.write_script('a', '').name == 'a-4.sh'


def test_gitignore_kept(tmp_path):
    state = ProjectState(tmp_path)
    (tmp_path / '.jug_hpc').mkdir()
    (tmp_path / '.jug_hpc' / '.gitignore').write_text('mine\n')
    state.write_script('a', '')
    assert (tmp_path / '.jug_hpc' / '.gitignore').read_text() == 'mine\n'


def test_concurrent_scripts_get_distinct_names(tmp_path):
    state = ProjectState(tmp_path)
    paths = []

    def write():
        for _ in range(10):
            paths.append(state.write_script('x', ''))

    threads = [threading.Thread(target=write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(paths)) == 40


def test_unwritable_state_dir(tmp_path):
    (tmp_path / '.jug_hpc').write_text('a file, not a directory')
    with pytest.raises(StateError, match=r'\.jug_hpc'):
        ProjectState(tmp_path).write_script('a', '')


# --- jobs -------------------------------------------------------------------------

def test_add_and_read_jobs(tmp_path):
    state = ProjectState(tmp_path)
    script = state.write_script('default', 'x')
    r1 = state.add_job('1234.pbs01', 'default', 'pbs', 4, script, submitted=T0)
    r2 = state.add_job('1235.pbs01', 'big', 'pbs', 1, 'elsewhere/x.sh')
    assert r1.script == Path('.jug_hpc/scripts/default-1.sh')
    assert state.script_path(r1) == script
    assert r2.script == Path('elsewhere/x.sh')
    assert r2.submitted.tzinfo is not None
    assert state.jobs() == [r1, r2]
    # a fresh object reads the same thing
    assert ProjectState(tmp_path).jobs() == [r1, r2]


def test_script_outside_project_kept_absolute(tmp_path):
    state = ProjectState(tmp_path / 'proj')
    r = state.add_job('1', 'a', 'local', 1, tmp_path / 'other.sh', submitted=T0)
    assert r.script == tmp_path / 'other.sh'
    assert state.script_path(r) == tmp_path / 'other.sh'


def test_jobs_file_format(tmp_path):
    state = ProjectState(tmp_path)
    state.add_job('7', 'default', 'local', 2, tmp_path / '.jug_hpc/scripts/default-1.sh',
                  submitted=T0)
    data = json.loads(state.jobs_file.read_text())
    assert data == {'version': FORMAT_VERSION, 'jobs': [{
        'job_id': '7', 'class': 'default', 'scheduler': 'local', 'workers': 2,
        'script': '.jug_hpc/scripts/default-1.sh',
        'submitted': '2026-11-04T10:00:00+00:00'}]}
    assert not list(state.path.glob('*.tmp'))


def test_concurrent_adds_all_kept(tmp_path):
    state = ProjectState(tmp_path)

    def add(k):
        for i in range(10):
            ProjectState(tmp_path).add_job(f'{k}-{i}', 'a', 'local', 1, 'x.sh')

    threads = [threading.Thread(target=add, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.job_id for r in state.jobs()) == sorted(
        f'{k}-{i}' for k in range(4) for i in range(10))


@pytest.mark.parametrize('content, message', [
    ('{not json', 'not valid JSON'),
    ('[]', 'not a jug_hpc jobs file'),
    ('{"jobs": []}', 'no version'),
    ('{"version": true, "jobs": []}', 'no version'),
    ('{"version": 99, "jobs": []}', 'newer version'),
    ('{"version": 1, "jobs": [3]}', 'job 1 is invalid: not an object'),
    ('{"version": 1, "jobs": [{"job_id": "1"}]}', "job 1 is invalid: 'class'"),
])
def test_invalid_jobs_file(tmp_path, content, message):
    state = ProjectState(tmp_path)
    state.path.mkdir()
    state.jobs_file.write_text(content)
    with pytest.raises(StateError, match=message) as e:
        state.jobs()
    assert str(state.jobs_file) in str(e.value)
    # nothing is overwritten
    with pytest.raises(StateError):
        state.add_job('1', 'a', 'local', 1, 'x.sh')
    assert state.jobs_file.read_text() == content


def _entry(**changes):
    entry = {'job_id': '1', 'class': 'a', 'scheduler': 'pbs', 'workers': 1,
             'script': 'x.sh', 'submitted': '2026-11-04T10:00:00+00:00'}
    entry.update(changes)
    return entry


@pytest.mark.parametrize('changes, message', [
    ({'workers': 0}, 'positive'),
    ({'workers': True}, "'workers'"),
    ({'workers': '2'}, "'workers'"),
    ({'submitted': '2026-11-04T10:00:00'}, 'timezone'),
    ({'submitted': 'yesterday'}, 'Invalid isoformat'),
])
def test_invalid_record(changes, message):
    with pytest.raises(ValueError, match=message):
        JobRecord.from_json(_entry(**changes))


@given(job_id=st.text(min_size=1), class_name=st.from_regex(r'[A-Za-z0-9_-]+', fullmatch=True),
       workers=st.integers(1, 10_000),
       offset=st.integers(-14 * 60, 14 * 60),
       seconds=st.integers(0, 10**9))
def test_record_round_trip(job_id, class_name, workers, offset, seconds):
    tz = timezone(timedelta(minutes=offset))
    submitted = datetime.fromtimestamp(seconds, tz)
    r = JobRecord(job_id=job_id, class_name=class_name, scheduler='pbs', workers=workers,
                  script=Path('.jug_hpc/scripts') / f'{class_name}-1.sh', submitted=submitted)
    assert JobRecord.from_json(json.loads(json.dumps(r.to_json()))) == r
