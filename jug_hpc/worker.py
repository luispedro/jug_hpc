'''WorkerSpec: everything one worker (job or job array) needs

A :class:`WorkerSpec` is scheduler independent: resources are in canonical
units (bytes, seconds) and backends translate them into their own directives.
It is built from the cluster settings, the project settings and one resource
class by :func:`make_worker_spec`. The submitter job (``jug-hpc submit
--as-job``) is described by a WorkerSpec too (:func:`make_submitter_spec`),
whose ``class_name`` is :data:`~jug_hpc.config.project.SUBMITTER`.
'''
from dataclasses import dataclass, field
from pathlib import Path

from .config.errors import ConfigError
from .config.project import SUBMITTER
from .config.toml import merge

#: Subdirectory of the logs directory for the scheduler's own output files
SCHEDULER_LOGS = 'scheduler'

__all__ = [
    'SCHEDULER_LOGS',
    'WorkerSpec',
    'make_submitter_spec',
    'make_worker_spec',
]


@dataclass(frozen=True)
class WorkerSpec:
    '''One worker, or an array of identical workers, for one resource class

    Attributes
    ----------
    class_name : str
    project_name : str
        Short name of the project (its directory name), used in job names
    workdir : Path
        Directory the worker runs in (the project directory)
    cpus : int
    memory : int
        Bytes
    time : int
        Wall time, in seconds
    max_seconds : int or None
        ``JUG_MAX_SECONDS`` for the worker (None for the submitter job, which
        does not run ``jug execute``)
    workers : int
        Number of workers (array elements when more than one)
    partition : str or None
        The class's partition, else the cluster's (None if neither is set)
    account : str or None
    raw : dict
        Raw options for the scheduler: the cluster's table merged with the
        class's (class values win)
    setup : tuple of str
        Shell snippets to run before ``jug execute``, in order (the cluster's
        setup, then the project's); empty ones are left out
    logs : Path
        Directory for worker logs
    command : tuple of str
        The ``jug execute`` command line (``jug-hpc submit`` for the
        submitter job)
    '''
    class_name: str
    project_name: str
    workdir: Path
    cpus: int
    memory: int
    time: int
    max_seconds: int | None
    workers: int = 1
    partition: str | None = None
    account: str | None = None
    raw: dict = field(default_factory=dict)
    setup: tuple = ()
    logs: Path | None = None
    command: tuple = ()

    @property
    def job_name(self):
        '''``jug-<class>-<project>``; backends may shorten or sanitize it'''
        return f'jug-{self.class_name}-{self.project_name}'

    @property
    def is_array(self):
        return self.workers > 1

    @property
    def scheduler_logs(self):
        '''``<logs>/scheduler``: the directory for the scheduler's own output files

        They catch what a worker prints before its script redirects its output
        to its own log (e.g. a failing setup snippet). It must exist before the
        job is submitted (PBS does not create it).
        '''
        if self.logs is None:
            raise ValueError('WorkerSpec.logs is not set')
        return Path(self.logs) / SCHEDULER_LOGS


def make_worker_spec(cluster, project, rc, command=(), *, workers=None):
    '''Build the WorkerSpec for resource class ``rc``

    Parameters
    ----------
    cluster : ClusterSettings
    project : ProjectSettings
    rc : ResourceClass
    command : sequence of str, optional
        The ``jug execute`` command line
    workers : int, optional
        Number of workers (default: the class's ``workers``)

    Returns
    -------
    spec : WorkerSpec

    Raises
    ------
    ConfigError
        If a raw option is a table in one file and a value in the other
    '''
    if workers is None:
        workers = rc.workers
    if workers < 1:
        raise ValueError(f'workers must be at least 1, not {workers}')
    key = ('default',) if rc.is_default else ('class', rc.name)
    raw = _merge_raw(cluster, project, key, rc)
    return WorkerSpec(
        class_name=rc.name,
        project_name=project.project_dir.name,
        workdir=project.project_dir,
        cpus=rc.cpus,
        memory=rc.memory,
        time=rc.time,
        max_seconds=rc.max_seconds,
        workers=workers,
        partition=rc.partition or cluster.partition,
        account=cluster.account,
        raw=raw,
        setup=tuple(s for s in (cluster.setup, project.setup) if s.strip()),
        logs=project.logs,
        command=tuple(command),
    )


def make_submitter_spec(cluster, project, command):
    '''Build the WorkerSpec of the submitter job (``jug-hpc submit --as-job``)

    Its resources come from ``[submitter]`` in ``jug_hpc.toml`` (nothing is
    inherited from ``[default]``), its partition from there or the cluster.

    Parameters
    ----------
    cluster : ClusterSettings
    project : ProjectSettings
    command : sequence of str
        The ``jug-hpc submit`` command line

    Returns
    -------
    spec : WorkerSpec
        With ``class_name`` :data:`SUBMITTER` and no ``max_seconds``

    Raises
    ------
    ConfigError
        If ``[submitter]`` sets no ``time``, or a raw option is a table in one
        file and a value in the other
    '''
    s = project.submitter
    if s.time is None:
        raise ConfigError(
            f"{project.path}: [{SUBMITTER}] must set 'time' for jug-hpc submit --as-job "
            '(the wall time of the submitter job, which runs until every task is '
            "finished, e.g. time = '7d')")
    return WorkerSpec(
        class_name=SUBMITTER,
        project_name=project.project_dir.name,
        workdir=project.project_dir,
        cpus=s.cpus,
        memory=s.memory,
        time=s.time,
        max_seconds=None,
        partition=s.partition or cluster.partition,
        account=cluster.account,
        raw=_merge_raw(cluster, project, (SUBMITTER,), s),
        setup=tuple(x for x in (cluster.setup, project.setup) if x.strip()),
        logs=project.logs,
        command=tuple(command),
    )


def _merge_raw(cluster, project, key, rc):
    '''The cluster's raw options for its scheduler, with those of ``rc`` merged over them

    Both tables are placed at the key of ``rc``'s table (e.g.
    ``class.merge.pbs``) before merging, so that an error names the full key
    in ``jug_hpc.toml`` and the cluster file the conflicting value came from.
    '''
    sched = cluster.scheduler
    key = key + (sched,)
    base_sources = {key + k[1:]: p for k, p in cluster.sources.items()
                    if len(k) > 1 and k[0] == sched}
    merged, _ = merge(_nest(key, cluster.raw_options(sched)), base_sources,
                      _nest(key, rc.raw_options(sched)), project.path)
    for k in key:
        merged = merged[k]
    return merged


def _nest(key, table):
    for k in reversed(key):
        table = {k: table}
    return table
