'''The key tables of the documentation match the code

The tables in ``docs/configuration.md`` and ``docs/llms.md`` are written by
hand (their wording differs, and ``llms.md`` is plain Markdown), so these
tests check that they list exactly the keys the code accepts, in the same
order, with the code's defaults.
'''
import re
from pathlib import Path

import pytest

from jug_hpc.config import (
    CLASS_KEYS,
    CLUSTER_KEYS,
    PROJECT_KEYS,
    SUBMITTER_KEYS,
    SubmitterSettings,
    load_project_settings,
)
from jug_hpc.config.project import _BUILTIN_DEFAULTS
from jug_hpc.schedulers import pbs
from jug_hpc.schedulers.base import scheduler_names
from jug_hpc.units import parse_memory

DOCS = Path(__file__).parent.parent / 'docs'


def _section(path, heading):
    '''Text of the section under ``heading`` (a whole line), up to the next
    heading of the same or a higher level'''
    lines = (DOCS / path).read_text().splitlines()
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip('#'))
    body = []
    for line in lines[start + 1:]:
        m = re.match(r'(#+) ', line)
        if m and len(m.group(1)) <= level:
            break
        body.append(line)
    return body


def _tables(path, heading):
    '''The Markdown tables in a section, as lists of rows (lists of cells),
    without the header and separator rows'''
    tables = []
    current = None
    for line in _section(path, heading):
        if line.startswith('|'):
            if current is None:
                current = []
                tables.append(current)
            current.append([c.strip() for c in line.strip().strip('|').split('|')])
        else:
            current = None
    return [t[2:] for t in tables]


def _keys(rows):
    '''Key -> row, for rows naming a key (not those naming a raw table)'''
    found = {}
    for row in rows:
        m = re.fullmatch(r'`([a-z_]+)`', row[0])
        if m:
            found[m.group(1)] = row
        else:
            assert row[0].startswith('`['), row
    return found


def _plain(cell):
    return cell.replace('`', '').replace('"', '')


CONFIGURATION = 'configuration.md'
LLMS = 'llms.md'

CLUSTER_TABLES = [
    (CONFIGURATION, '### Keys', 0),
    (LLMS, '## `cluster.toml` keys', 0),
]
PROJECT_TABLES = [
    (CONFIGURATION, '### Top-level keys', 0),
    (LLMS, '## `jug_hpc.toml` keys', 0),
]
CLASS_TABLES = [
    (CONFIGURATION, '### `[default]` and `[class.<name>]`', 0),
    (LLMS, '## `jug_hpc.toml` keys', 1),
]
SUBMITTER_TABLES = [
    (CONFIGURATION, '### `[submitter]`', 0),
    (LLMS, '## `jug_hpc.toml` keys', 2),
]


def _table(path, heading, index):
    return _keys(_tables(path, heading)[index])


@pytest.mark.parametrize(('tables', 'known'), [
    (CLUSTER_TABLES, CLUSTER_KEYS),
    (PROJECT_TABLES, PROJECT_KEYS),
    (CLASS_TABLES, CLASS_KEYS),
    (SUBMITTER_TABLES, SUBMITTER_KEYS),
], ids=['cluster', 'project', 'class', 'submitter'])
def test_key_tables_list_the_known_keys(tables, known):
    for path, heading, index in tables:
        assert list(_table(path, heading, index)) == list(known), (path, heading)


def _no_default(cell):
    return cell == "the cluster's" or cell.startswith('required')


@pytest.mark.parametrize(('path', 'heading', 'index'), CLASS_TABLES)
def test_class_defaults(path, heading, index):
    for k, row in _table(path, heading, index).items():
        default = row[2]
        if k in ('match', 'pattern'):
            assert default == ''
        elif k not in _BUILTIN_DEFAULTS:
            assert default == 'required in `[default]`', k
        elif _BUILTIN_DEFAULTS[k] is None:
            assert _no_default(default), k
        else:
            assert _plain(default) == str(_BUILTIN_DEFAULTS[k]), k


@pytest.mark.parametrize(('path', 'heading', 'index'), SUBMITTER_TABLES)
def test_submitter_defaults(path, heading, index):
    defaults = SubmitterSettings()
    for k, row in _table(path, heading, index).items():
        default, value = row[2], getattr(defaults, k)
        if value is None:
            assert _no_default(default), k
        elif k == 'memory':
            assert parse_memory(_plain(default)) == value
        else:
            assert _plain(default) == str(value), k


@pytest.mark.parametrize(('path', 'heading', 'index'), PROJECT_TABLES)
def test_project_defaults(tmp_path, path, heading, index):
    (tmp_path / 'jug_hpc.toml').write_text('[default]\nmemory = "1G"\ntime = "1h"\n')
    settings = load_project_settings(tmp_path)
    keys = _table(path, heading, index)
    assert _plain(keys['jugfile'][2]) == settings.jugfile.name
    assert _plain(keys['logs'][2]) == settings.logs.name


def test_scheduler_names():
    table = _tables('schedulers.md', '# Schedulers')[0]
    assert sorted(_plain(row[0]) for row in table) == scheduler_names()


def test_pbs_raw_keys():
    terms = [line.strip('`') for line in _section('schedulers.md', '### Raw options')
             if re.fullmatch(r'`[a-z_]+`', line)]
    assert terms == list(pbs.RAW_KEYS)
    listed = [m.group(1) for line in _section(LLMS, '### Raw PBS options (`[pbs]` tables)')
              if (m := re.match(r'- `([a-z_]+)`:', line))]
    assert listed == list(pbs.RAW_KEYS)
