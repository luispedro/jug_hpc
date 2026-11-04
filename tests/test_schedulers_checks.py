from types import SimpleNamespace

import pytest

from jug_hpc.config import (
    ConfigError,
    load_cluster_settings,
    load_project_settings,
)
from jug_hpc.schedulers import SchedulerError, UnknownSchedulerError, base
from jug_hpc.schedulers.checks import check_scheduler_settings
from jug_hpc.schedulers.pbs import PBSScheduler

PROJECT = '[default]\nmemory = "4G"\ntime = "1h"\n'


@pytest.fixture
def registry(monkeypatch):
    '''Only the PBS backend is installed (whatever this environment has)'''
    eps = [SimpleNamespace(name='pbs', value='jug_hpc.schedulers.pbs:PBSScheduler',
                           load=lambda: PBSScheduler)]
    monkeypatch.setattr(base, '_entry_points', lambda: eps)
    return eps


@pytest.fixture
def files(tmp_path):
    '''Write the user cluster.toml, the project cluster.toml and jug_hpc.toml,
    and load them'''
    user = tmp_path / 'xdg' / 'jug_hpc' / 'cluster.toml'
    user.parent.mkdir(parents=True)
    project = tmp_path / 'project'
    project.mkdir()
    environ = {'XDG_CONFIG_HOME': str(tmp_path / 'xdg')}

    def load(cluster, project_toml=PROJECT, project_cluster=None):
        user.write_text(cluster)
        if project_cluster is not None:
            (project / 'cluster.toml').write_text(project_cluster)
        (project / 'jug_hpc.toml').write_text(project_toml)
        return (load_cluster_settings(project, environ=environ),
                load_project_settings(project))

    load.user = user
    load.project = project
    return load


def messages(found):
    return [str(e) for e in found.errors], found.warnings


def test_valid(registry, files):
    cluster, project = files(
        'scheduler = "pbs"\n[pbs.select]\nngpus = 1\n',
        PROJECT + '[default.pbs]\ndirectives = ["-m n"]\n'
                  '[class.big]\nmatch = "x"\n[class.big.pbs.resources]\nplace = "scatter"\n')
    found = check_scheduler_settings(cluster, project)
    assert found.ok
    assert messages(found) == ([], [])


def test_nothing_to_check(registry):
    found = check_scheduler_settings()
    assert found.ok
    assert messages(found) == ([], [])


def test_unknown_scheduler(registry, files):
    cluster, _ = files('scheduler = "pbz"\n')
    found = check_scheduler_settings(cluster)
    assert not found.ok
    [e] = found.errors
    assert isinstance(e, UnknownSchedulerError)
    assert str(e) == (f"{files.user}: unknown scheduler 'pbz' "
                      "(did you mean 'pbs'? available: pbs)")


def test_cluster_table_errors(registry, files):
    cluster, _ = files('scheduler = "pbs"\n[pbs.select]\nncpus = 4\n',
                       project_cluster='[pbs]\ndirectives = "-m n"\n')
    [e] = check_scheduler_settings(cluster).errors
    assert isinstance(e, ConfigError)
    # the table's values come from both files
    where = ' and '.join(sorted([str(files.user), str(files.project / 'cluster.toml')]))
    assert str(e).startswith(f'{where}: [pbs]: ')


def test_cluster_table_error_names_the_file(registry, files):
    cluster, _ = files('scheduler = "pbs"\n[pbs]\nselct = {}\n')
    [e] = check_scheduler_settings(cluster).errors
    assert str(e) == (f"{files.user}: [pbs]: 'selct' is not a PBS raw option "
                      "(did you mean 'select'?)")


def test_cluster_tables_ignored(registry, files):
    cluster, _ = files('scheduler = "pbs"\n[pbz]\na = 1\n')
    found = check_scheduler_settings(cluster)
    assert found.ok
    assert found.warnings == [
        (f"{files.user}: [pbz] is ignored: no scheduler backend named 'pbz' is "
         "installed (did you mean 'pbs'? available: pbs)")]


def test_cluster_table_for_other_backend(registry, files):
    registry.append(SimpleNamespace(name='fake', value='x:Fake', load=lambda: Fake))
    cluster, _ = files('scheduler = "pbs"\n[fake]\na = 1\n')
    found = check_scheduler_settings(cluster)
    assert found.ok
    assert found.warnings == [f"{files.user}: [fake] is ignored: the scheduler is 'pbs'"]


def test_project_tables(registry, files):
    cluster, project = files(
        'scheduler = "pbs"\n',
        PROJECT + '[default.pbs]\ndirectives = [""]\n'
                  '[class.big]\nmatch = "x"\n[class.big.pbs.select]\nmem = "1G"\n'
                  '[class.big.slurm]\nqos = "long"\n'
                  '[class.c]\nmatch = "y"\n[class.c.pbs]\nresources = 3\n'
                  '[submitter.pbs]\ndirectives = 1\n[submitter.slurm]\nqos = "x"\n')
    errors, warnings = messages(check_scheduler_settings(cluster, project))
    path = files.project / 'jug_hpc.toml'
    assert errors == [
        f"{path}: [default.pbs]: 'directives' must not contain empty strings",
        (f"{path}: [class.big.pbs]: 'select.mem' cannot be set as a raw option: "
         'use memory in jug_hpc.toml'),
        (f"{path}: [class.c.pbs]: 'resources' must be a table of PBS resources "
         '(e.g. ngpus = 1)'),
        f"{path}: [submitter.pbs]: 'directives' must be a list of strings (qsub options)",
    ]
    assert warnings == [
        (f"{path}: [class.big.slurm] is ignored: no scheduler backend named 'slurm' is "
         'installed (available: pbs)'),
        (f"{path}: [submitter.slurm] is ignored: no scheduler backend named 'slurm' is "
         'installed (available: pbs)')]


def test_project_without_cluster(registry, files):
    # the project's tables are checked even if the cluster settings are broken
    _, project = files('scheduler = "pbs"\n', PROJECT + '[default.pbs]\nx = 1\n')
    [e] = check_scheduler_settings(None, project).errors
    assert "[default.pbs]: 'x' is not a PBS raw option" in str(e)


class Fake(PBSScheduler):
    name = 'fake'


def test_broken_backend_reported_once(registry, files):
    def load():
        raise ImportError('no module named fakepkg')
    registry.append(SimpleNamespace(name='fake', value='fakepkg:Fake', load=load))
    cluster, project = files(
        'scheduler = "fake"\n[fake]\na = 1\n',
        PROJECT + '[default.fake]\na = 1\n[class.x]\nmatch = "x"\n[class.x.fake]\nb = 2\n')
    [e] = check_scheduler_settings(cluster, project).errors
    assert isinstance(e, SchedulerError)
    assert 'could not be loaded: ImportError' in str(e)


def test_no_backends(monkeypatch, files):
    monkeypatch.setattr(base, '_entry_points', list)
    cluster, _ = files('scheduler = "pbs"\n[pbs]\n')
    found = check_scheduler_settings(cluster)
    assert [str(e) for e in found.errors] == [
        f"{files.user}: unknown scheduler 'pbs' (no scheduler backends are installed)"]
    assert found.warnings == [
        (f"{files.user}: [pbs] is ignored: no scheduler backend named 'pbs' is installed "
         '(available: none)')]


def test_conflicting_registrations(registry, files):
    registry.append(SimpleNamespace(name='pbs', value='other:PBS', load=lambda: PBSScheduler))
    cluster, _ = files('scheduler = "pbs"\n')
    [e] = check_scheduler_settings(cluster).errors
    assert isinstance(e, SchedulerError)
    assert 'two scheduler backends' in str(e)
