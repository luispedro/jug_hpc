import pytest

from jug_hpc.config import ConfigError, load_cluster_settings, load_project_settings
from jug_hpc.worker import WorkerSpec, make_submitter_spec, make_worker_spec

CLUSTER = '''
scheduler = "pbs"
account   = "grant1"
partition = "workq"
setup     = "module load python\\n"

[pbs]
directives = ["-m n"]

[pbs.select]
ngpus = 0
host  = "any"
'''

PROJECT = '''
setup = "source .venv/bin/activate"

[default]
memory  = "4G"
time    = "1:00:00"
workers = 5

[class.align]
match   = "align"
cpus    = 8
memory  = "32G"
time    = "10:00:00"
time_fraction = 0.5
partition = "long"

[class.align.pbs.select]
ngpus = 1

[class.merge]
match   = "merge"
workers = 1

[class.merge.pbs]
directives = ["-m abe"]
'''


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / 'home'
    project = tmp_path / 'myproj'
    (home / '.config' / 'jug_hpc').mkdir(parents=True)
    project.mkdir()
    (home / '.config' / 'jug_hpc' / 'cluster.toml').write_text(CLUSTER)
    (project / 'jug_hpc.toml').write_text(PROJECT)
    cluster = load_cluster_settings(project, environ={'HOME': str(home)})
    return cluster, load_project_settings(project)


def test_class_spec(settings):
    cluster, project = settings
    spec = make_worker_spec(cluster, project, project.get_class('align'),
                            ['jug', 'execute', 'jugfile.py'])
    assert spec == WorkerSpec(
        class_name='align',
        project_name='myproj',
        workdir=project.project_dir,
        cpus=8,
        memory=32 * 2**30,
        time=10 * 3600,
        max_seconds=5 * 3600,
        workers=5,
        partition='long',
        account='grant1',
        raw={'directives': ['-m n'], 'select': {'ngpus': 1, 'host': 'any'}},
        setup=('module load python\n', 'source .venv/bin/activate'),
        logs=project.project_dir / 'logs',
        command=('jug', 'execute', 'jugfile.py'),
    )
    assert spec.job_name == 'jug-align-myproj'
    assert spec.is_array


def test_default_spec(settings):
    cluster, project = settings
    spec = make_worker_spec(cluster, project, project.default)
    assert spec.class_name == 'default'
    assert spec.partition == 'workq'  # the cluster's
    assert spec.raw == {'directives': ['-m n'], 'select': {'ngpus': 0, 'host': 'any'}}
    assert spec.max_seconds == 2880
    assert spec.command == ()


def test_class_raw_replaces_arrays(settings):
    cluster, project = settings
    spec = make_worker_spec(cluster, project, project.get_class('merge'))
    assert spec.raw['directives'] == ['-m abe']
    assert not spec.is_array


def test_raw_not_shared(settings):
    cluster, project = settings
    spec = make_worker_spec(cluster, project, project.get_class('align'))
    spec.raw['select']['ngpus'] = 7
    assert cluster.raw_options()['select']['ngpus'] == 0
    assert project.get_class('align').raw_options('pbs')['select']['ngpus'] == 1


def test_workers_override(settings):
    cluster, project = settings
    spec = make_worker_spec(cluster, project, project.default, workers=2)
    assert spec.workers == 2
    with pytest.raises(ValueError):
        make_worker_spec(cluster, project, project.default, workers=0)


def test_empty_setup_dropped(tmp_path):
    (tmp_path / 'cluster.toml').write_text('scheduler = "pbs"\nsetup = "  \\n"\n')
    (tmp_path / 'jug_hpc.toml').write_text('[default]\nmemory = "1G"\ntime = 10\n')
    cluster = load_cluster_settings(tmp_path, environ={'HOME': str(tmp_path / 'h')})
    project = load_project_settings(tmp_path)
    spec = make_worker_spec(cluster, project, project.default)
    assert spec.setup == ()
    assert spec.partition is None
    assert spec.account is None
    assert spec.raw == {}


def test_raw_shape_conflict(tmp_path):
    (tmp_path / 'cluster.toml').write_text('scheduler = "pbs"\n[pbs]\nselect = "x"\n')
    (tmp_path / 'jug_hpc.toml').write_text(
        '[default]\nmemory = "1G"\ntime = 10\n'
        '[class.a]\nmatch = "a"\n[class.a.pbs.select]\nngpus = 1\n')
    cluster = load_cluster_settings(tmp_path, environ={'HOME': str(tmp_path / 'h')})
    project = load_project_settings(tmp_path)
    with pytest.raises(ConfigError) as e:
        make_worker_spec(cluster, project, project.get_class('a'))
    message = str(e.value)
    assert message.startswith(str(tmp_path / 'jug_hpc.toml'))
    assert 'class.a.pbs.select' in message
    assert str(tmp_path / 'cluster.toml') in message


def test_submitter_spec(settings):
    cluster, project = settings
    with pytest.raises(ConfigError, match=r"\[submitter\] must set 'time'"):
        make_submitter_spec(cluster, project, ['jug-hpc', 'submit'])
    (project.project_dir / 'jug_hpc.toml').write_text(
        PROJECT + '[submitter]\ntime = "2d"\n[submitter.pbs.select]\nhost = "login"\n')
    project = load_project_settings(project.project_dir)
    spec = make_submitter_spec(cluster, project, ['jug-hpc', 'submit'])
    assert spec == WorkerSpec(
        class_name='submitter',
        project_name='myproj',
        workdir=project.project_dir,
        cpus=1,
        memory=2 * 2**30,
        time=2 * 86400,
        max_seconds=None,
        partition='workq',   # the cluster's: nothing comes from [default]
        account='grant1',
        raw={'directives': ['-m n'], 'select': {'ngpus': 0, 'host': 'login'}},
        setup=('module load python\n', 'source .venv/bin/activate'),
        logs=project.project_dir / 'logs',
        command=('jug-hpc', 'submit'),
    )
    assert spec.job_name == 'jug-submitter-myproj'
