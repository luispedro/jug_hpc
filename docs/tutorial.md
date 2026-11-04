# Tutorial

This tutorial runs the jugfile from the {doc}`front page <index>` on a PBS
cluster. It assumes you already know Jug. If you don't, start with
[Jug's tutorial](https://jug.readthedocs.io/en/latest/tutorial.html).

## Installing

jug_hpc requires Python 3.11 or later and Jug 2.7 or later. It is not on PyPI
yet, so install it from a source checkout, into the same environment as Jug:

```bash
git clone https://github.com/luispedro/jug_hpc
pip install ./jug_hpc
```

This installs the `jug-hpc` command. Workers run `jug`, and the submitter job
(`jug-hpc submit --as-job`) runs `jug-hpc`. Both must be on the `PATH` inside
a job, which is what the `setup` snippets below are for.

## Step 1: describe the cluster

jug_hpc reads *how* to submit from `~/.config/jug_hpc/cluster.toml`. You
normally write this file once per cluster and use it for all your projects:

```toml
# ~/.config/jug_hpc/cluster.toml
scheduler = "pbs"
account   = "my_group"      # passed to qsub -A
partition = "standard"      # PBS queue, used when a class sets none
setup     = """
module load python/3.12
"""
```

`setup` is shell code that every worker runs before it starts Jug. See
{doc}`configuration` for every key.

## Step 2: describe the project

In the project directory, next to the jugfile, `jug_hpc.toml` says *what* the
project needs:

```toml
# jug_hpc.toml
jugfile = "jugfile.py"
setup = """
source .venv/bin/activate
"""                          # runs after the cluster's setup

[default]
memory  = "4G"
time    = "1:00:00"
workers = 50

[class.align]
match   = "align"
cpus    = 8
memory  = "32G"
time    = "4h"
workers = 10

[class.merge]
match         = "merge"
memory        = "128G"
time          = "12:00:00"
partition     = "bigmem"
time_fraction = 0.5          # long tasks: stop taking new ones earlier
```

Each `[class.<name>]` table is a *resource class*. `match` selects its tasks,
using the same rules as `jug execute --name`. Every task that no class matches
goes to the *default class*, configured in `[default]`. Classes inherit every
field they don't set from `[default]`, so `merge` gets `cpus = 1` (the built-in
default) and `workers = 50`. {doc}`concepts` explains what each field means.

To see the settings as jug_hpc reads them, with every inherited value filled
in and the file each one came from, run `jug-hpc config`:

```
$ jug-hpc config
# Cluster settings, merged from:
#   user file:    /home/me/.config/jug_hpc/cluster.toml
#   project file: /home/me/rnaseq/cluster.toml (not found)
scheduler = "pbs"       # user file
account = "my_group"    # user file
partition = "standard"  # user file
...

[class.merge]
match = "merge"
cpus = 1             # built-in default
memory = "128G"
time = "12:00:00"
workers = 50         # from [default]
time_fraction = 0.5  # JUG_MAX_SECONDS = 21600
partition = "bigmem"
...
```

## Step 3: check which class runs each task

`jug-hpc check` loads the jugfile and lists, for each class, the tasks it
selects:

```
$ jug-hpc check
Jugfile: jugfile.py (41 tasks)

[class.align]  match = 'align'  (20 tasks)
  20  jugfile.align

[class.merge]  match = 'merge'  (1 task)
   1  jugfile.merge

[default]  (20 tasks)
  20  jugfile.count
```

A task matched by two classes is an error, and a class that matches no task is
a warning (often a typo in `match`). `check` never reads the cluster settings,
so you can run it anywhere, including on a laptop.

## Step 4: look at the scripts

`jug-hpc submit --dry-run` writes the worker scripts and prints them, together
with the commands that would submit them. It does not submit anything. Here it
is for the `merge` class only:

```
$ jug-hpc submit --dry-run merge
jug-hpc: note: [class.merge]: 1 task, so 1 worker instead of 50
==> .jug_hpc/scripts/merge-1.sh <==
#!/bin/bash
#PBS -N jug-merge-rnaseq
#PBS -l select=1:ncpus=1:mem=128gb
#PBS -l walltime=12:00:00
#PBS -q bigmem
#PBS -A my_group
#PBS -o logs/scheduler/
#PBS -e logs/scheduler/

set -e
cd -- /home/me/rnaseq
JUG_HPC_JOB_ID="${PBS_JOBID%%[.[]*}"
mkdir -p -- /home/me/rnaseq/logs
exec >>/home/me/rnaseq/logs/merge-"$JUG_HPC_JOB_ID".log 2>&1
export JUG_HPC_CLASS=merge JUG_HPC_JOB_ID JUG_HPC_CPUS=1 OMP_NUM_THREADS=1

module load python/3.12

source .venv/bin/activate

unset JUG_MAX_HOURS JUG_MAX_MINUTES
export JUG_MAX_SECONDS=21600
exec jug execute --keep-failed --name=merge --nr-wait-cycles=15 --wait-cycle-time=12 --jugdir=/home/me/rnaseq/jugfile.jugdata -- /home/me/rnaseq/jugfile.py

To submit these scripts, run from /home/me/rnaseq:
  # [class.merge]: 1 task, 1 worker
  qsub .jug_hpc/scripts/merge-1.sh
```

The script is plain bash with a `#PBS` header. You could submit it yourself
with exactly the `qsub` command shown. {doc}`submitting` explains each line.

## Step 5: run it

```
$ jug-hpc submit
[2026-11-05 07:25:48] align: submitted job 1 (10 workers, .jug_hpc/scripts/align-1.sh)
[2026-11-05 07:25:48] tasks: 21 waiting, 20 ready, 0 running, 0 failed, 0 finished; workers queued or running: 10
[2026-11-05 07:25:51] default: submitted job 2 (20 workers, .jug_hpc/scripts/default-1.sh)
[2026-11-05 07:25:51] tasks: 1 waiting, 20 ready, 0 running, 0 failed, 20 finished; workers queued or running: 20
[2026-11-05 07:25:54] merge: submitted job 3 (1 worker, .jug_hpc/scripts/merge-1.sh)
[2026-11-05 07:25:54] tasks: 0 waiting, 1 ready, 0 running, 0 failed, 40 finished; workers queued or running: 1
[2026-11-05 07:25:58] tasks: 0 waiting, 0 ready, 0 running, 0 failed, 41 finished; workers queued or running: 0
[2026-11-05 07:25:58] every task is finished
```

(This run used the {ref}`local backend <local-backend>` with tasks that take
no time, so it is much faster than a real run.)

`jug-hpc submit` is the *polling submitter*. Once a minute, it counts the tasks
of each class that are ready to run and submits workers for them, up to each
class's `workers`. It keeps going until every task is finished, or until no
task can run because some have failed. It is a small process that sleeps most
of the time. You can run it on a login node (in `tmux` or `screen`), or submit
it as a job with `jug-hpc submit --as-job`. See {doc}`submitting`.

Each worker writes its log to `logs/<class>-<job id>.log`, or
`logs/<class>-<job id>-<index>.log` for an element of a job array. Use plain
`jug status` to follow progress, as usual.
