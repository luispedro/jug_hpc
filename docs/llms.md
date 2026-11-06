# For LLMs and coding assistants

This page is written for language models and coding assistants that help users
with jug_hpc. It puts the facts needed to write configuration files, run the
commands and diagnose problems on one page, without examples meant for
learning. It is written in plain Markdown, and the built documentation also
serves it as `/llms.txt`. Human readers should start with the
[tutorial](tutorial.md).

jug_hpc is in early development (version 0.0.1.dev0, unreleased). This page
describes what is implemented now. Do not suggest the features listed as not
implemented (at the end of [Commands](#commands)).

## What jug_hpc is

- It runs a [Jug](https://jug.readthedocs.io) jugfile on an HPC cluster. Each
  scheduler job (*worker*) is sized for one *resource class* of tasks.
- A resource class is a set of Jug tasks, selected by **task name**,
  together with the resources of its workers (CPUs, memory, wall time, the
  largest number of workers at once, partition, raw scheduler options).
- Workers are plain `jug execute --name=...` (or `--pattern=...`) processes.
  Jug does all the locking, result storage and dependency tracking.
- `jug-hpc submit` is a *polling submitter*. It submits workers for a class
  only when tasks of that class are ready to run, and stops when every task
  is finished or no task can run.
- Schedulers: `pbs` (OpenPBS, PBS Professional; not Torque) and `local`
  (processes on this machine). SLURM is not implemented yet.

## Rules an assistant must follow

1. **Never change the jugfile to make jug_hpc work.** All scheduling
   information goes in `jug_hpc.toml` and `cluster.toml`. The one accepted
   jugfile change is splitting a function into two `TaskGenerator`s when tasks
   of one function need very different resources (see below).
2. **`scheduler` and `account` go only in `cluster.toml`.** They are errors in
   `jug_hpc.toml`.
3. **Unknown keys are errors.** Use only the keys listed on this page. Do not
   invent keys such as `gpus`, `nodes`, `queue`, `mem` or `walltime`. GPUs and
   other scheduler-specific resources go in raw scheduler tables (`[pbs]`).
4. **`memory` needs a unit** (`"16G"`, not `16`). Time strings with two fields
   (`"1:30"`) are errors.
5. **Wall time is per worker, not per task.** A worker runs many tasks one
   after the other.
6. Before submitting, run `jug-hpc check` (which class runs which task) and
   `jug-hpc submit --dry-run` (the exact scripts). Read their output.
7. Never suggest `jug cleanup --locks-only` while workers may be running. It
   removes the locks of running tasks too. For locks left by dead workers,
   suggest `jug-hpc status --release-stale`.

## Files

| File | Where | Holds |
|---|---|---|
| `cluster.toml` | `$XDG_CONFIG_HOME/jug_hpc/cluster.toml` (default `~/.config/jug_hpc/cluster.toml`), and optionally the project directory | *How* to submit on this cluster. The two files are deep-merged key by key, and the project file wins. |
| `jug_hpc.toml` | the project directory (next to the jugfile) | *What* the project needs: jugfile, jugdir, classes, submitter resources. |
| `.jug_hpc/` | the project directory | Written by jug_hpc: `scripts/<class>-<n>.sh`, `jobs.json` (every job submitted), lock files, `.gitignore`. Never edit it. |
| `logs/` | the project directory (the `logs` key) | Worker logs `<class>-<job id>.log` or `<class>-<job id>-<index>.log`, `submitter-<job id>.log`, and the scheduler's own output files in `logs/scheduler/`. |

The project directory is the nearest directory holding `jug_hpc.toml`,
searched from the current directory upwards, or from `DIR` with
`jug-hpc -C DIR`. Relative paths in `jug_hpc.toml` are relative to it. Workers
run in it.

Neither TOML file can run code or import the jugfile.

## `cluster.toml` keys

| Key | Meaning |
|---|---|
| `scheduler` | `"pbs"` or `"local"`. Required: jug_hpc never guesses. |
| `account` | Account to charge (PBS: `-A`). |
| `partition` | Default partition (PBS: queue, `-q`), used when a class sets none. |
| `setup` | Shell code run by every worker and by the submitter job, before the project's `setup`, e.g. `module load ...`. |
| `[pbs]` | Raw PBS options for every job (see below). |

## `jug_hpc.toml` keys

Top level:

| Key | Meaning | Default |
|---|---|---|
| `jugfile` | The jugfile. | `"jugfile.py"` |
| `jugdir` | Directory, or `file_keepalive:<dir>`, or `redis://host:port/`. `dict_store` is rejected by `submit`. | Jug's default (`<jugfile name>.jugdata`) |
| `logs` | Worker log directory. | `"logs"` |
| `setup` | Shell code run by every worker after the cluster's `setup`, e.g. `source .venv/bin/activate`. | none |

`[default]` (the default class: every task that no class matches) and
`[class.<name>]` (a resource class; inherits every key it does not set from
`[default]`):

| Key | Meaning | Default |
|---|---|---|
| `match` | Task selection, rules of `jug execute --name`. Classes only. | |
| `pattern` | Task selection, rules of `jug execute --pattern`. Classes only. | |
| `cpus` | CPUs per worker. | `1` |
| `memory` | Memory per worker. | required in `[default]` |
| `time` | Wall time per worker. | required in `[default]` |
| `workers` | Largest number of workers of the class queued or running at once. | `1` |
| `time_fraction` | A worker takes no new task after `floor(time_fraction × time)` seconds (Jug's `JUG_MAX_SECONDS`). In (0, 1]. | `0.8` |
| `partition` | Partition (PBS: queue). Overrides the cluster's. | the cluster's |
| `[class.<name>.pbs]`, `[default.pbs]` | Raw PBS options. A class's are merged over `[default.pbs]`. | |

- Each class sets exactly one of `match` and `pattern`.
- Class names: letters, digits, `-` and `_`. `default` and `submitter` are
  reserved.
- A task matched by two classes is an error, so the order of classes does not
  matter.
- `[default]` must set `memory` and `time`. There are no built-in values.

`[submitter]` (the job started by `jug-hpc submit --as-job`; inherits nothing
from `[default]`):

| Key | Meaning | Default |
|---|---|---|
| `cpus` | CPUs. | `1` |
| `memory` | Memory. | `"2G"` |
| `time` | Wall time, as long as the whole run (e.g. `"7d"`). | required by `--as-job` |
| `partition` | Partition. | the cluster's |
| `[submitter.pbs]` | Raw PBS options. | |

### Units

- Memory: number + `K`/`M`/`G`/`T`/`P`, optionally followed by `B` or `iB`, in
  any case. Always binary (`1G` = 2³⁰ bytes). A bare number is an error.
- Time: `"HH:MM:SS"` (hours may exceed 23); `"D-HH"`, `"D-HH:MM"`,
  `"D-HH:MM:SS"`; `"1h30m"`, `"2d12h"`, `"30m"` (units `d h m s`, in this
  order); a bare number is minutes (`90` or `"90"`). `"H:MM"` is an error.

### Raw PBS options (`[pbs]` tables)

Only three keys:

- `select`: table of extra chunk resources, appended to `-l select=1:ncpus=..:mem=..`.
  Example: `select = { ngpus = 1 }`.
- `resources`: table of extra job-wide `-l` resources. Example:
  `resources = { place = "scatter" }`.
- `directives`: list of `qsub` options, each written as one `#PBS` line.
  Example: `directives = ["-m n", "-P my_project"]`.

`ncpus`, `mem`, `walltime` and `select` can't be set as raw resources. Use
`cpus`, `memory` and `time`. When the cluster and a class set the same key,
the class wins, and a list such as `directives` is *replaced*, not appended
to. Values are passed to `qsub` unchecked. Tables named after a scheduler that
is not installed are accepted, with a warning from `jug-hpc config`.

## Task names and selection

- A task's name is `<module>.<function>` of the function its `TaskGenerator`
  wraps, e.g. `jugfile.align`. `jug status` and `jug-hpc check` list them.
- `match = "align"` matches the whole name or a trailing part that starts
  after a `.`: it matches `jugfile.align` but **not** `jugfile.align_reads`.
  Shell wildcards work: `"align*"`.
- `pattern = "align"` is a substring search: it matches `jugfile.align_reads`
  and `jugfile.realign`. `pattern = "/regex/"` is a regular expression
  searched anywhere in the name. In TOML, escape backslashes:
  `pattern = "/\\.train_(cnn|rnn)$/"`.
- jug_hpc uses Jug's own matchers, so a class selects exactly what its
  workers run.
- Selection is by name only. To give tasks of one function different
  resources, the user defines two `TaskGenerator`s (`align_small`,
  `align_large`) calling the same inner function, and gives one of them its
  own class.

## Commands

| Command | What it does | Exit status |
|---|---|---|
| `jug-hpc config` | Prints the merged cluster settings (with the file each value came from) and every class fully resolved. Checks the raw scheduler tables. | 0, or 1 on any error |
| `jug-hpc check` | Loads the jugfile and lists the tasks of each class. Never reads `cluster.toml`, so it runs anywhere. Overlaps are errors; a class matching no task is a warning (often a typo). Notes a jugdir that can't detect dead workers (Jug's default directory, a plain directory, `redis:`), suggesting `file_keepalive:`. | 0, or 1 on errors |
| `jug-hpc status` | Prints a table: per class, tasks waiting/ready/running/failed/finished (loads the jugfile in a subprocess) and workers queued/running (asks the scheduler about every job of the backend in use in `.jug_hpc/jobs.json`), then `Submitter: running (process pid@host)`, `job N, queued`/`running` or `not running`. Warnings: per class and kind (memory, wall time, signal), the jobs killed by the scheduler, with a suggestion; per class, tasks locked (running) while no worker of the class is queued or running (stale locks of dead workers, unless a `jug execute` outside jug-hpc runs them; ends with "remove the lock(s) with 'jug-hpc status --release-stale'"); per class with ready tasks, no worker and no submitter running, when its last 3 or more jobs all ended in error (a user's `qdel`/`jug-hpc cancel` or an unknown outcome breaks the run; e.g. after a run that stopped with "the workers keep failing"): "tasks are ready, but no worker of the class is queued or running and its last N jobs ended in error (the last: job J: ...), which stops a submitter from submitting workers for it; ... once fixed, run 'jug-hpc submit'". Notes: every task finished; failed tasks block the run; tasks ready (outside such classes) but no worker and no submitter. Changes nothing; no lock needed. |
| `jug-hpc status --release-stale` | As `status`, then: reads `jobs.json` and asks the scheduler again, and removes the locks (not the failed ones) of the classes warned about that still have no worker queued or running; prints `[class.x]: released the locks of N tasks` before the table, which comes from a second count. Also removes the locks of a `jug execute` started outside jug-hpc. A class that got a worker meanwhile: a note, locks kept. Nothing to release: a note. The tasks become ready; a running submitter submits workers for them. | As `status` | 0, or 1 if settings, jugfile, `jobs.json` or scheduler can't be read |
| `jug-hpc cancel` | Cancels every job in `.jug_hpc/jobs.json` (of the backend in use) that is queued or running, or submitted < 5 min ago and not listed yet; prints `<class>: cancelled job N (queued/running/k workers queued, m running)`. The submitter job (`--as-job`) first: waits up to 2 min until the scheduler no longer lists it and `submitter.lock` is free, then rereads `jobs.json` and cancels the workers. Refuses (cancels nothing) while a submitter runs outside the scheduler (lock held, no submitter job active): stop it with Ctrl-C first. Reads only `cluster.toml` and `jobs.json` (not `jug_hpc.toml`, not the jugfile). PBS: `qdel` (no `-x`). Cancelled jobs are outcome "cancelled", not reported as kills. Workers get SIGTERM, on which Jug releases the running task's lock. Nothing active: a note. | 0; 1 on errors, a refusal, or a submitter job that had not stopped after 2 min (the workers are cancelled anyway: run it again) |
| `jug-hpc submit` | The polling submitter (below). Only one per project, enforced with a lock. | 0 all finished; 1 blocked by failed tasks, stalled by failing workers, or an error; 130 on Ctrl-C; 143 on SIGTERM (`jug-hpc cancel`, the submitter job's wall time), logged as `stopped by SIGTERM; ...` (workers keep running) |
| `jug-hpc submit --as-job` | Submits the polling submitter as a job (`[submitter]` resources; log `logs/submitter-<job id>.log`), and exits. Checks the settings and the jugfile first. Submits nothing if a submitter is running or queued, or no task is left. | 0, or 1 |
| `jug-hpc submit --all [CLASS...]` | Submits every class with tasks at once (at most one worker per task), and exits. For graphs whose classes don't wait for each other. `default` names the default class. A named class is submitted even with no tasks. | 0, or 1 |
| `jug-hpc submit --dry-run [CLASS...]` | Writes and prints the scripts and the exact submit commands that `--all` would run. Submits nothing. | 0, or 1 |

Options of plain `submit`: `--interval SECONDS` (default 60) and
`--max-interval SECONDS` (default 5 × `--interval`). They can't be combined
with `--all` or `--dry-run`, and `CLASS` arguments are only allowed with those
two.

Listings go to stdout. Messages go to stderr as `jug-hpc: error: ...`,
`jug-hpc: warning: ...` or `jug-hpc: note: ...`. Error messages about
configuration name the file and the key.

Not implemented: SLURM, named cluster profiles.

## How the polling submitter works

At each poll it:

1. queries the scheduler for the queued and running workers of each class,
   and for how the jobs that ended since the last poll ended (PBS: `qstat -f
   -F json -x`, and `-t` for array elements);
2. loads the jugfile **in a subprocess** and counts, per class, tasks waiting,
   ready, running, failed and finished (as `jug status` does, without its
   cache, so tasks created after a `barrier()` and edits to the jugfile are
   seen);
3. submits `min(ready tasks, workers) − queued or running workers` new
   workers per class, when positive. Several workers are one job array.

While nothing changes, the wait between polls doubles, up to
`--max-interval`. It stops when:

- every task is finished (exit 0);
- no task is ready or running and some have failed (exit 1). The failed tasks
  are listed per class;
- the only tasks that could run belong to *stalled* classes (exit 1).

A class is stalled after 3 jobs of it in a row end in error (non-zero exit,
killed, or deleted) while no task of it ends. Its workers are submitted again
once one of its tasks ends or one of its jobs succeeds. Only jobs that this
submitter saw queued or running count, so restarting `jug-hpc submit` gives
every class a fresh start.

Errors at the first poll are fatal. Later scheduler or jugfile errors are
retried, up to 5 times in a row. When the run ends because no task can run
(the two exit-1 cases), the submitter cancels the workers still queued or
running (they have nothing to do; each would otherwise hold its allocation for
about 3 minutes) and logs `<class>: cancelled job N (<status>): no task can
run`; if that fails, a warning, and it stops anyway. When every task is
finished, workers are left alone (they exit at once).

Tasks locked (running) in a class with no worker queued or running at two
polls in a row (locks left by a worker killed with SIGKILL): a warning, once
while it lasts ("N tasks are locked, but no worker of the class is queued or
running: ... this submitter will not finish (remove the locks with 'jug-hpc
status --release-stale': workers are then submitted for them)"). It keeps
polling; after `status --release-stale` the tasks are ready and its next poll
submits workers.

The submitter keeps almost no state: task states come from the jugdir, jobs
from `.jug_hpc/jobs.json`. Stopping and starting it again is safe.

## The worker script

The body is the same for every scheduler. The backend adds only the
directive header and the variables holding the job ID and array index. In
order, a worker:

1. runs `set -e` (not `set -u`) and `cd`s to the project directory;
2. redirects its output to its log in `logs/`;
3. exports `JUG_HPC_CLASS`, `JUG_HPC_JOB_ID`, `JUG_HPC_ARRAY_INDEX` (arrays
   only), and `JUG_HPC_CPUS` and `OMP_NUM_THREADS` (both = `cpus`);
4. runs the cluster's `setup`, then the project's;
5. unsets `JUG_MAX_HOURS` and `JUG_MAX_MINUTES`, and exports
   `JUG_MAX_SECONDS = floor(time_fraction × time)`;
6. `exec jug execute --keep-failed <selection> --nr-wait-cycles=15
   --wait-cycle-time=12 --jugdir=<jugdir> -- <jugfile>`.

The default class's selection is `--exclude-name`/`--exclude-pattern` with
Jug versions that have them, or else a generated `--pattern` regex, checked to
select the same tasks. `--keep-failed` keeps failed tasks failed until
`jug cleanup --failed-only`. Tasks that use threads should read
`int(os.environ.get('JUG_HPC_CPUS', '1'))`.

PBS header, in order: `-N jug-<class>-<project dir name>`,
`-l select=1:ncpus=<cpus>:mem=<n>gb[:<select extras>]`, `-l walltime=HH:MM:SS`,
one `-l` line per raw `resources` entry, `-q <partition>`, `-A <account>`,
`-J 1-<n>` (only for more than one worker), `-o`/`-e logs/scheduler/`, then
the raw `directives`.

## Diagnosing problems

| Symptom | Likely cause | What to suggest |
|---|---|---|
| `[class.x]: 3 jobs in a row ended in error ...`, exit status 127 (afterwards, `jug-hpc status` warns `... its last N jobs ended in error ...`) | A command in `setup` fails (e.g. `module` missing, wrong venv path), or `jug` is not on `PATH` in the job. | Read `logs/<class>-<job>-<index>.log` and `logs/scheduler/`. Fix `setup`. Run `jug-hpc submit` again. |
| Warning `job N ended: killed: wall time limit reached; raise time ...` (from `jug-hpc status`: `N jobs killed for wall time ...`) | A task started near the end of the wall time did not finish. | Raise `time`, or lower `time_fraction` so that `(1 − time_fraction) × time` is longer than the longest task. For tasks that take most of the wall time, use a class where each worker runs one task. |
| Warning `job N ended: killed ... memory` or `SIGKILL` | Used more memory than `memory`. On cgroup clusters an out-of-memory kill appears only as SIGKILL (exit 137 or 265), so the memory diagnosis is a guess. | Raise `memory` for that class. |
| The submitter never finishes; it and `jug-hpc status` warn "N tasks are locked, but no worker of the class is queued or running" (status shows tasks running but no worker running) | A worker killed by SIGKILL left its task's lock. | Fix the cause (usually raise `memory`), then `jug-hpc status --release-stale` (the tasks become ready again; a running submitter submits workers for them). For the future: `jugdir = "file_keepalive:<dir>"` (same storage format, so an existing jugdir can be switched): unrefreshed locks count as failed after 30 minutes, so the run ends instead of hanging. |
| `no task can run: failed tasks: ...` | Tasks raised exceptions. | Read the worker logs, fix the code, `jug cleanup --failed-only`, then `jug-hpc submit` again. |
| Workers of a later class start and exit without doing anything | They were submitted with `--all` before their dependencies ran. | Use plain `jug-hpc submit`, which waits until the tasks are ready. |
| Error naming the process (and host) of another submitter | One submitter already runs for this project. | Use that one, or stop it first. |
| `jug-hpc cancel`: `a submitter is running for this project outside the scheduler` | `jug-hpc submit` runs in a terminal; it would replace the cancelled workers. | Stop it with Ctrl-C (its workers keep running), then `jug-hpc cancel`. |
| `check` lists fewer tasks than expected | The jugfile stops at a `barrier()` whose tasks are not done. | Expected. The classes apply to later tasks once they appear. |
| Error about `scheduler` or `account` in `jug_hpc.toml` | Cluster-only key in the project file. | Move it to `cluster.toml` (or a project-dir `cluster.toml`). |
| No `qstat -x` history on the PBS server | Finished jobs vanish, so outcomes are unknown and failing classes are not detected. | Ask the admins about `job_history_enable`. |

`jug-hpc submit --as-job` needs compute nodes that may run `qsub` and
`qstat`, and `jug-hpc` on `PATH` after the `setup` snippets. If the submitter
job reaches its wall time, it stops (its log ends with `stopped by SIGTERM`)
and the workers keep running. Run `--as-job` again to continue.

## Setting up a project: a procedure

1. Get the task names with `jug status`, or with `jug-hpc check` and a
   `jug_hpc.toml` holding only a `[default]` table with `memory` and `time`.
2. Ask the user, or find out, the resources of each kind of task: CPUs (only
   useful if the task uses threads), peak memory, and duration.
3. Write `cluster.toml` once per cluster: `scheduler`, `account`,
   `partition`, and a `setup` that makes `jug` (and `jug-hpc`, for
   `--as-job`) available.
4. Write `jug_hpc.toml`: put the most common, cheap tasks in `[default]`, and
   give a `[class.<name>]` only to tasks that need clearly different
   resources. Set `time` to cover many tasks per worker, and `workers` to the
   parallelism wanted. Prefer `jugdir = "file_keepalive:..."` on clusters.
5. Run `jug-hpc config`, `jug-hpc check`, `jug-hpc submit --dry-run`, and
   fix every error and warning.
6. Run `jug-hpc submit` in `tmux`/`screen` on a login node, or
   `jug-hpc submit --as-job` with a `[submitter]` table that sets `time`.
7. To try it on a laptop first, use a project-dir `cluster.toml` with
   `scheduler = "local"` and `setup = ""`. The local backend enforces only
   wall time and ignores `memory`, `partition` and `account`.

## Example

```toml
# ~/.config/jug_hpc/cluster.toml
scheduler = "pbs"
account   = "my_group"
partition = "standard"
setup     = "module load python/3.12"

[pbs]
directives = ["-m n"]
```

```toml
# jug_hpc.toml, next to jugfile.py
jugfile = "jugfile.py"
jugdir  = "file_keepalive:jugfile.jugdata"
setup   = "source .venv/bin/activate"

[default]                  # every task no class matches
memory  = "4G"
time    = "1:00:00"
workers = 50

[class.align]
match   = "align"          # jugfile.align
cpus    = 8
memory  = "32G"
time    = "4h"
workers = 10

[class.merge]
match         = "merge"
memory        = "128G"
time          = "12:00:00"
partition     = "bigmem"
time_fraction = 0.5        # long tasks

[class.train]
pattern = "/\\.train_(cnn|rnn)$/"
time    = "1-00:00"
workers = 4

[class.train.pbs]
select = { ngpus = 1 }

[submitter]
time = "7d"
```
