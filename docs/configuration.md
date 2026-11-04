# Configuration reference

jug_hpc reads two TOML files: `cluster.toml` (*how* to submit on this
cluster) and `jug_hpc.toml` (*what* this project needs). {doc}`concepts`
explains why there are two. Neither file can run code.

Unknown keys are errors, with a suggestion when a key looks like a known one.
Error messages always name the file and the key. `jug-hpc config` shows the
settings as jug_hpc reads them, and where each value came from.

## `cluster.toml`

### Where it is read from

1. The user file, `$XDG_CONFIG_HOME/jug_hpc/cluster.toml`. If
   `XDG_CONFIG_HOME` is unset (or not an absolute path), this is
   `~/.config/jug_hpc/cluster.toml`.
2. A `cluster.toml` in the project directory (next to `jug_hpc.toml`), if there
   is one. It overrides the user file.

The two are **merged key by key**. Tables are merged recursively, and any
other value in the project file replaces the one in the user file. A project
that only needs a different account can have a `cluster.toml` with just
`account = "other_grant"`.

If neither file exists, or the merged settings name no `scheduler`, jug-hpc
stops with an error. It never guesses the scheduler.

```{note}
The submitter job (`jug-hpc submit --as-job`) reads the cluster settings
inside a job. If you set `XDG_CONFIG_HOME` to something other than the default,
make sure jobs see the same value.
```

### Keys

| Key | Meaning |
|---|---|
| `scheduler` | The scheduler backend: `"pbs"` or `"local"` (see {doc}`schedulers`). Required. |
| `account` | The account (project, grant) to charge jobs to. |
| `partition` | The partition (PBS: queue) used when a class sets none. |
| `setup` | Shell code run by every worker before the project's `setup`, e.g. `module load` lines. |

Every table at the top level, such as `[pbs]`, holds raw options for the
scheduler of the same name, added to every job (see
[raw scheduler options](#raw-scheduler-options)).

```toml
scheduler = "pbs"
account   = "my_group"
partition = "standard"
setup     = """
module load python/3.12
"""

[pbs]
directives = ["-m n"]       # no email from PBS
```

## `jug_hpc.toml`

The directory holding `jug_hpc.toml` is the project directory. Every relative
path in it is relative to that directory, and workers run in it.

### Top-level keys

| Key | Meaning | Default |
|---|---|---|
| `jugfile` | The jugfile. | `jugfile.py` |
| `jugdir` | The jugdir: a directory, or a Jug backend such as `file_keepalive:<directory>` or `redis://host:port/`. | Jug's: the jugfile's name with `.jugdata` instead of `.py` |
| `logs` | The directory for worker logs. | `logs` |
| `setup` | Shell code run by every worker after the cluster's `setup`, e.g. activating a virtual environment. | (none) |

Workers always get the jugdir as `--jugdir`, so Jug's own configuration
files (`jugrc`) do not change it. `dict_store` jugdirs are rejected by
`jug-hpc submit`, since each worker would keep its own results in memory.

### `[default]` and `[class.<name>]`

`[default]` configures the default class, which runs every task that no other
class matches. Each `[class.<name>]` table is a resource class, and inherits
every key it does not set from `[default]`.

| Key | Meaning | Default |
|---|---|---|
| `match` | The tasks of the class, with the rules of `jug execute --name`. Classes only. | |
| `pattern` | The tasks of the class, with the rules of `jug execute --pattern`. Classes only. | |
| `cpus` | CPUs per worker. | 1 |
| `memory` | Memory per worker, e.g. `"16G"` (see [units](#units)). | required in `[default]` |
| `time` | Wall time per worker, e.g. `"4:00:00"` or `"4h"` (see [units](#units)). | required in `[default]` |
| `workers` | The largest number of workers of the class queued or running at the same time. | 1 |
| `time_fraction` | Workers stop taking new tasks after `time_fraction × time` (`JUG_MAX_SECONDS`). Larger than 0, at most 1. | 0.8 |
| `partition` | The partition (PBS: queue). Overrides the cluster's `partition`. | the cluster's |

Each class must set exactly one of `match` and `pattern` (see
[selecting tasks](concepts.md#selecting-tasks)). The class names `default`
and `submitter` are reserved.

Subtables named after a scheduler, such as `[default.pbs]` or
`[class.merge.pbs]`, hold raw options for that scheduler. A class's raw
options are merged over those of `[default]`, key by key.

### `[submitter]`

The resources of the submitter job, submitted by `jug-hpc submit --as-job`
(see {doc}`submitting`). Nothing is inherited from `[default]`: the submitter
needs one CPU and little memory, but must run for as long as the whole run.

| Key | Meaning | Default |
|---|---|---|
| `cpus` | CPUs of the submitter job. | 1 |
| `memory` | Memory of the submitter job. | `"2G"` |
| `time` | Wall time of the submitter job: as long as the whole run, e.g. `"7d"`. | required by `--as-job` |
| `partition` | The partition (PBS: queue) of the submitter job. | the cluster's |

As in a class, a subtable such as `[submitter.pbs]` holds raw scheduler
options.

### Example

```toml
jugfile = "pipeline.py"
jugdir  = "file_keepalive:pipeline.jugdata"
logs    = "logs/"
setup   = """
source .venv/bin/activate
"""

[default]
cpus          = 1
memory        = "4G"
time          = "1:00:00"
workers       = 50
time_fraction = 0.8

[class.align]
match   = "align_*"
cpus    = 8
memory  = "32G"
time    = "4:00:00"
workers = 10

[class.merge]
match         = "merge"
memory        = "128G"
time          = "12:00:00"
partition     = "bigmem"
time_fraction = 0.5

[class.train]
pattern = "/\\.train_(cnn|rnn)$/"
time    = "1-00:00"
workers = 4

[class.train.pbs]
select = { ngpus = 1 }

[submitter]
time = "7d"
```

## Raw scheduler options

The portable keys (`cpus`, `memory`, `time`, `partition`, ...) cover what every
scheduler has. Anything else goes in a table named after the scheduler:

- `[pbs]` in `cluster.toml`: added to every job on this cluster;
- `[default.pbs]`, `[class.<name>.pbs]` and `[submitter.pbs]` in
  `jug_hpc.toml`: added to that class's jobs.

When the cluster and a class set the same option, the class wins. Values are
passed to the scheduler without checking them. Only their shape is checked,
by the backend: what keys a table may have depends on the scheduler (see
{doc}`schedulers`). Tables for schedulers that are not installed are
accepted, with a warning from `jug-hpc config`, so the same project file can
carry options for several clusters.

## Units

Memory
: A number and a unit: `K`, `M`, `G`, `T` or `P`, optionally followed by `B` or
  `iB`, in any case: `"512M"`, `"16G"`, `"16GB"`, `"16gib"`, `"1.5T"`. Units
  are binary (`1K` = 1024 bytes) in every form, as in both PBS and SLURM. A
  number without a unit is an error, because schedulers disagree on what it
  means.

Time
: - `"HH:MM:SS"`, where hours may be larger than 23: `"4:00:00"`,
    `"36:00:00"`;
  - `"D-HH"`, `"D-HH:MM"` or `"D-HH:MM:SS"`: `"2-12"` is two and a half days;
  - units `d`, `h`, `m` and `s`, in this order: `"30m"`, `"12h"`, `"1h30m"`,
    `"2d12h"`;
  - a plain number of minutes (as in SLURM): `"90"` or `90`.

  Two fields, as in `"1:30"`, are an error, because it is not clear whether
  they mean hours and minutes or minutes and seconds.
