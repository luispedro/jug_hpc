'''Showing the settings and where each value came from (``jug-hpc config``)

Each section is written as TOML, with the provenance of each value in a
comment, so that a line can be copied into a configuration file as it is.
Values that are not set are shown as comments.
'''
from pathlib import Path

from ..units import format_dhms, format_memory
from .project import CLASS_KEYS, DEFAULT_CLASS, SUBMITTER, SUBMITTER_KEYS, split_jugdir
from .toml import format_key, format_value, iter_leaves

__all__ = [
    'format_cluster_settings',
    'format_project_settings',
]

BUILTIN = 'built-in default'


def format_cluster_settings(cluster):
    '''The merged cluster settings, with the file each value came from

    Parameters
    ----------
    cluster : ClusterSettings

    Returns
    -------
    text : str
    '''
    labels = {}
    header = ['# Cluster settings, merged from:']
    names = ['user file', 'project file']
    width = max(len(n) for n in names[:len(cluster.searched)]) + 1
    for name, path in zip(names, cluster.searched, strict=False):
        labels[path] = name
        missing = '' if path in cluster.files else ' (not found)'
        header.append(f'#   {name + ":":<{width}} {path}{missing}')
    entries = [(f'{format_key(key)} = {format_value(value, multiline=True)}',
                labels.get(source, str(source)))
               for key, value, source in cluster.leaves()]
    return _join(header, entries)


def format_project_settings(project, cluster=None):
    '''The project settings, with every class fully resolved

    Values inherited from ``[default]`` or built in are marked as such.

    Parameters
    ----------
    project : ProjectSettings
    cluster : ClusterSettings, optional
        Used to show the partition a class without one would use

    Returns
    -------
    text : str
    '''
    data = project.data
    header = [f'# Project settings: {project.path}',
              '# (paths are relative to the directory holding it)']
    entries = []
    for k, value, builtin in [
            ('jugfile', project.jugfile, 'jugfile.py'),
            ('jugdir', project.jugdir, None),
            ('logs', project.logs, 'logs'),
            ('setup', project.setup, ''),
    ]:
        if isinstance(value, Path):
            value = _relative_path(value, project.project_dir)
        elif k == 'jugdir' and value is not None:
            prefix, path = split_jugdir(value)
            if path is not None:
                value = prefix + _relative_path(Path(path), project.project_dir)
        if k in data:
            entries.append((f'{k} = {format_value(value, multiline=True)}', None))
        elif k == 'jugdir':
            entries.append(("# jugdir: not set (Jug's default, next to the jugfile)", None))
        elif builtin:
            entries.append((f'{k} = {format_value(value)}', BUILTIN))
    text = _join(header, entries)
    for rc in [project.default, *project.classes.values()]:
        text += '\n\n' + _format_class(rc, project, cluster)
    text += '\n\n' + _format_submitter(project, cluster)
    return text


def _format_submitter(project, cluster):
    s = project.submitter
    entries = []
    for k in SUBMITTER_KEYS:
        value = getattr(s, k)
        if value is None:
            if k == 'time':
                note = 'needed by jug-hpc submit --as-job'
            else:
                note = _partition_note(cluster)
            entries.append((f'# {k}: not set ({note})', None))
            continue
        if k == 'memory':
            value = format_memory(value)
        elif k == 'time':
            value = format_dhms(value)
        entries.append((f'{k} = {format_value(value)}', None if k in s.origins else BUILTIN))
    for scheduler, table in s.raw.items():
        for key, value in iter_leaves(table, (scheduler,)):
            entries.append((f'{format_key(key)} = {format_value(value)}', None))
    return _join([f'[{SUBMITTER}]'], entries)


def _partition_note(cluster):
    if cluster is not None and cluster.partition is not None:
        return f"uses the cluster's, {format_value(cluster.partition)}"
    return 'uses the scheduler default'


def _format_class(rc, project, cluster):
    if rc.is_default:
        header = ['[default]']
        own = project.data.get('default', {})
    else:
        header = [f'[class.{format_key((rc.name,))}]']
        own = project.data['class'][rc.name]

    def origin(k):
        o = rc.origins.get(k)
        if o is None:
            return BUILTIN
        if not rc.is_default and o[0] == DEFAULT_CLASS:
            return 'from [default]'
        return None

    entries = []
    for k in CLASS_KEYS:
        value = getattr(rc, k)
        if k in ('match', 'pattern'):
            if value is not None:
                entries.append((f'{k} = {format_value(value)}', None))
            continue
        if value is None:   # partition
            entries.append((f'# {k}: not set ({_partition_note(cluster)})', None))
            continue
        if k == 'memory':
            value = format_memory(value)
        elif k == 'time':
            value = format_dhms(value)
        comment = origin(k)
        if k == 'time_fraction':
            note = f'JUG_MAX_SECONDS = {rc.max_seconds}'
            comment = f'{comment}; {note}' if comment else note
        entries.append((f'{k} = {format_value(value)}', comment))

    for scheduler, table in rc.raw.items():
        for key, value in iter_leaves(table, (scheduler,)):
            comment = None if _has_key(own, key) else 'from [default]'
            entries.append((f'{format_key(key)} = {format_value(value)}', comment))
    return _join(header, entries)


def _relative_path(path, base):
    '''``path`` relative to ``base`` if it is inside it, as a string'''
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _has_key(table, key):
    for k in key:
        if not isinstance(table, dict) or k not in table:
            return False
        table = table[k]
    return True


def _join(header, entries, *, max_width=40):
    '''Lines of ``header``, then each ``(text, comment)`` entry, with the
    comments aligned (multi-line values get their comment after the last line)
    '''
    widths = [len(text) for text, comment in entries
              if comment and '\n' not in text and len(text) <= max_width]
    column = max(widths, default=0) + 2
    lines = list(header)
    for text, comment in entries:
        if comment:
            last = text.rsplit('\n', 1)[-1]
            pad = max(column - len(last), 2)
            text = f'{text}{" " * pad}# {comment}'
        lines.append(text)
    return '\n'.join(lines)
