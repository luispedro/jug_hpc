'''Checking the scheduler-specific settings against the installed backends

The configuration modules check raw-options tables (``[pbs]`` in
``cluster.toml``, ``[default.pbs]``, ``[class.<name>.pbs]`` and
``[submitter.pbs]`` in ``jug_hpc.toml``) only for being tables, as they know no scheduler. This
module checks them with the backends:

- the cluster's ``scheduler`` must name an installed backend (an error);
- every table named after an installed backend is checked by that backend's
  :meth:`~.base.Scheduler.validate_raw_options`, whether or not it is the
  configured scheduler, so that a project's tables for another cluster are
  checked too (errors);
- a table named after no installed backend is ignored (a warning: it is most
  likely a typo), and so is a table in ``cluster.toml`` for a backend other than
  the configured one (a warning: a cluster has one scheduler).

Every problem is collected, so that one run of ``jug-hpc config`` shows all
that needs fixing.
'''
import difflib
from dataclasses import dataclass, field

from ..config.errors import ConfigError
from ..config.toml import format_key
from .base import SchedulerError, UnknownSchedulerError, get_scheduler, scheduler_names

__all__ = [
    'SchedulerSettingsCheck',
    'check_scheduler_settings',
]


@dataclass
class SchedulerSettingsCheck:
    '''The problems found by :func:`check_scheduler_settings`

    Attributes
    ----------
    errors : list of ConfigError or SchedulerError
        Exceptions, so that a caller that stops at the first one can raise it
    warnings : list of str
    '''
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


def check_scheduler_settings(cluster=None, project=None):
    '''Check the scheduler name and every raw-options table

    Parameters
    ----------
    cluster : ClusterSettings, optional
    project : ProjectSettings, optional
        Either may be None (e.g. if it could not be loaded); what is given is
        checked

    Returns
    -------
    check : SchedulerSettingsCheck
    '''
    result = SchedulerSettingsCheck()
    try:
        installed = scheduler_names()
    except SchedulerError as e:
        result.errors.append(e)
        return result
    backends = {}

    def backend(name):
        '''The backend class for ``name`` (None if it cannot be loaded, which
        is reported once)'''
        if name not in backends:
            try:
                backends[name] = get_scheduler(name)
            except SchedulerError as e:
                result.errors.append(e)
                backends[name] = None
        return backends[name]

    def check_table(name, table, where):
        cls = backend(name)
        if cls is None:
            return
        try:
            cls.validate_raw_options(table, where)
        except ConfigError as e:
            result.errors.append(e)

    def not_installed(name):
        close = difflib.get_close_matches(name, installed, n=1)
        hint = f'did you mean {close[0]!r}? ' if close else ''
        available = ', '.join(installed) if installed else 'none'
        return (f'no scheduler backend named {name!r} is installed '
                f'({hint}available: {available})')

    if cluster is not None:
        if cluster.scheduler in installed:
            backend(cluster.scheduler)   # reports a backend that cannot be loaded
        else:
            result.errors.append(UnknownSchedulerError(
                cluster.scheduler, installed, cluster.source('scheduler')))
        for name, table in cluster.raw.items():
            where = f'{_table_files(cluster, name)}: [{format_key((name,))}]'
            if name not in installed:
                result.warnings.append(f'{where} is ignored: {not_installed(name)}')
            elif name != cluster.scheduler:
                result.warnings.append(
                    f'{where} is ignored: the scheduler is {cluster.scheduler!r}')
            else:
                check_table(name, table, where)

    if project is not None:
        tables = [(('default',), project.data.get('default', {}))]
        tables += [(('class', name), table)
                   for name, table in project.data.get('class', {}).items()]
        tables.append((('submitter',), project.data.get('submitter', {})))
        for prefix, table in tables:
            for name, raw in table.items():
                if not isinstance(raw, dict):
                    continue
                where = f'{project.path}: [{format_key(prefix + (name,))}]'
                if name in installed:
                    check_table(name, raw, where)
                else:
                    result.warnings.append(f'{where} is ignored: {not_installed(name)}')
    return result


def _table_files(cluster, name):
    '''The file(s) that set values in the cluster's table ``name``'''
    found = sorted({str(path) for key, path in cluster.sources.items() if key[0] == name})
    return ' and '.join(found or map(str, cluster.files))
