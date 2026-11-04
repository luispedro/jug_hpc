'''WorkerSpec: everything one worker (job or job array) needs

A :class:`WorkerSpec` is scheduler independent: resources are in canonical
units (bytes, seconds) and backends translate them into their own directives.
It is built from the cluster settings, the project settings and one resource
class by :func:`make_worker_spec`.
'''
from dataclasses import dataclass, field
from pathlib import Path

from .config.toml import merge

__all__ = [
    'WorkerSpec',
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
    max_seconds : int
        ``JUG_MAX_SECONDS`` for the worker
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
        The ``jug execute`` command line
    '''
    class_name: str
    project_name: str
    workdir: Path
    cpus: int
    memory: int
    time: int
    max_seconds: int
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
    raw = _merge_raw(cluster, project, rc)
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


def _merge_raw(cluster, project, rc):
    '''The cluster's raw options for its scheduler, with the class's merged over them

    Both tables are placed at the class's key (e.g. ``class.merge.pbs``)
    before merging, so that an error names the full key in ``jug_hpc.toml``
    and the cluster file the conflicting value came from.
    '''
    sched = cluster.scheduler
    key = (('default',) if rc.is_default else ('class', rc.name)) + (sched,)
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
