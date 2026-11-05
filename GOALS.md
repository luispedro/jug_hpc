# jug_hpc: goals

## Problem

Jug already works on HPC clusters: submit a job array of identical
`jug execute` workers and they coordinate through the jugdir (see the Jug
FAQ). This breaks down when tasks have very different resource needs. If one
step needs 64 GB and 12 hours and another needs 1 GB and 5 minutes, every
worker must be sized for the largest task, which wastes allocation and hurts
queue priority.

`jug_hpc` is a helper for [Jug](../jug) that lets users describe the resources
each *type* of task needs, and then submits appropriately sized workers to the
HPC scheduler.

## Principles

1. **The jugfile stays unchanged.** A jugfile used with `jug_hpc` must still
   run with plain `jug execute` on a laptop. All scheduling information lives
   in a separate configuration, so users can change their analysis without
   touching scheduling details, and vice versa.
2. **Build on Jug and don't reimplement it.** Workers are ordinary
   `jug execute` processes. Tasks are selected with `jug execute --name`
   (and `--pattern`), using exactly the same matching rules. Locking, results,
   and status all come from Jug.
3. **Be transparent.** Users should always be able to see the exact scheduler
   scripts that will be submitted (dry run), and to submit them by hand if
   they want.

## Core concepts

- **Jugfile**: the user's analysis, handled entirely by Jug.
- **Resource class**: a task selector (a Jug task name, e.g. `align_sample`
  or `mymodule.align_*`) together with the resources for a *worker* that runs
  those tasks: CPUs, memory, wall time, number of workers, and optional
  scheduler-specific extras (partition, GPUs, ...).
- **Worker**: one scheduler job (or array element) running
  `jug execute --name <selector> jugfile.py`. A worker runs many tasks one
  after another, so **wall time applies to the worker, not to a single task**.
  Every worker is started with `JUG_MAX_SECONDS` set to a fraction of its wall
  time (see "Early exit" below), so it stops taking new tasks before the
  scheduler kills it.
- **Cluster settings**: how to submit to a particular cluster: scheduler,
  account, default partition, cluster-wide environment setup (module loads),
  and scheduler options shared by every job. They are usually the same for all
  of a user's projects on that cluster, so they live in a separate file (see
  below).
- **Default class**: used for any task that no explicit class matches.

## Configuration files (sketch)

Configuration is declarative TOML, split into two files:

- **`cluster.toml`**: *how* to submit to this cluster. It's normally set once
  per user, in `~/.config/jug_hpc/cluster.toml`.
- **`jug_hpc.toml`**: *what* this project needs: the jugfile, resource
  classes, project environment, logs. It lives in the project directory.

Neither file can run code or import the jugfile, so principle 1 holds by
construction. Both can be checked against a schema, and Python 3.11+ reads
them with the standard library (`tomllib`), which is why jug_hpc requires
Python 3.11 or later.

### Cluster file

```toml
# ~/.config/jug_hpc/cluster.toml
scheduler = "slurm"
account   = "my_group"
partition = "standard"       # used when a class sets no partition
setup     = """
module load python/3.12
"""

[slurm]                      # raw options added to every job, passed through unchecked
qos = "normal"
```

Lookup and overrides:

1. Read the user file, `$XDG_CONFIG_HOME/jug_hpc/cluster.toml`. If
   `XDG_CONFIG_HOME` is unset, this is `~/.config/jug_hpc/cluster.toml`.
2. If the project directory (the directory holding `jug_hpc.toml`) also has a
   `cluster.toml`, it overrides the user file. The two are **merged key by
   key**: tables are merged recursively, and any other value in the project
   file replaces the one in the user file. A project that only needs a
   different account can contain just `account = "other_grant"`.
3. If neither file exists, `jug-hpc` fails with an error saying where it
   looked. It does not guess a scheduler.

`jug-hpc config` prints the merged result and shows which file each value
came from.

### Project file

```toml
# jug_hpc.toml
jugfile = "my_jugfile.py"
jugdir  = "my_jugfile.jugdata"   # optional; Jug's default otherwise
logs    = "logs/"
setup   = """
source .venv/bin/activate
"""                              # runs after the cluster's setup

[default]
cpus          = 1
memory        = "4G"
time          = "1:00:00"
workers       = 50
time_fraction = 0.8          # JUG_MAX_SECONDS = time_fraction * time (this is the default)

[class.align]
match   = "align_*"          # same rules as `jug execute --name`
cpus    = 8
memory  = "32G"
time    = "4:00:00"
workers = 10

[class.merge]
match         = "merge"
memory        = "128G"
time          = "12:00:00"
workers       = 1
partition     = "bigmem"
time_fraction = 0.5          # long tasks: stop taking new ones earlier

[class.merge.slurm]          # raw scheduler options, passed through unchecked
gres       = "gpu:1"
constraint = "skylake"
```

Rules:

- **Classes have short names** (`align`, `merge`). The names are used in job
  names (`jug-align-1234`), log files, and CLI commands
  (`jug-hpc status align`). `match` selects tasks with the same rules as
  `jug execute --name`. A `pattern = "/regex/"` key, mirroring `--pattern`,
  may be used instead.
- **Classes inherit every unset field from `[default]`**. In the example,
  `merge` gets `cpus = 1` from there.
- **The order of classes doesn't matter**, because a task matched by two
  classes is an error.
- **Scheduler-specific options go in a subtable**: `[class.<name>.slurm]` for
  one class, or `[slurm]` in `cluster.toml` for every job. They are passed
  through unchecked, while the portable fields (`cpus`, `memory`, `time`,
  `workers`, `time_fraction`) are validated. When both set the same option,
  the class value wins.
- **Partition**: a class's `partition` overrides the cluster's default
  `partition`.
- **Setup**: each worker script runs the cluster's `setup`, then the
  project's `setup`.
- **Scope**: cluster settings (`scheduler`, `account`, ...) are not allowed
  in `jug_hpc.toml`. To change them for one project, use a project
  `cluster.toml`. This keeps the project file portable between clusters.

Command line (sketch):

```
jug-hpc config                 # show the merged cluster settings and which file each value came from
jug-hpc check                  # list every task with its class; flag tasks matched by 2 classes and classes matching nothing
jug-hpc submit   [--dry-run]   # submit workers
jug-hpc status                 # jug status, grouped by resource class, plus queue state
jug-hpc cancel                 # cancel all jobs submitted for this project
```

## Key design problem: when to submit workers

`jug execute --name X` runs only the tasks matching `X`. It does **not** run
their dependencies. It waits for other processes to finish them, and exits
when its wait cycles run out. Submitting every class at once therefore leaves
workers for later stages idle on their allocation, and then they exit without
doing anything.

Candidate strategies:

1. **Scheduler dependencies**: chain the classes with
   `sbatch --dependency=afterok:...`. This is simple and needs no long-running
   process, but it requires the classes to form a DAG, and it breaks when
   tasks of different classes alternate.
2. **Polling submitter**: a lightweight process (on a login node, or a small
   long-running job) uses Jug's status machinery to find classes that have
   *runnable* tasks, and submits workers only for those, scaled to the number
   of ready tasks. This handles any graph, including dynamic graphs from
   `barrier()`/`bvalue`.
3. **Hybrid**: use (1) when the class graph is a simple chain and fall back
   to (2) otherwise.

This choice shapes the rest of the design and must be made first.

## Requirements

- **Scheduler support**: PBS first, behind a backend interface so that
  other schedulers (SLURM next, then SGE/LSF) are easy to add or swap in.
- **Name matching**: identical to `jug execute --name`/`--pattern`. Tasks
  matched by no class go to the default class. A task matched by more than one
  class is a configuration error.
- **CPU propagation**: Jug runs tasks one at a time in one process, so
  `cpus > 1` only helps if the task uses threads itself. Workers should export
  the allocated CPU count (e.g. `OMP_NUM_THREADS`, and document
  `SLURM_CPUS_PER_TASK`) so tasks can use it.
- **Early exit**: every worker runs with
  `JUG_MAX_SECONDS = floor(time_fraction × wall time in seconds)`.
  `time_fraction` defaults to 0.8 and can be set in `[default]` or per class.
  Values outside (0, 1] are an error. Points to keep in mind:
  - Jug only checks the limit *after each task finishes*. A task that starts
    just before the limit still runs to completion, or until the scheduler
    kills it. The 0.8 default therefore assumes that each task takes much
    less than 20% of the wall time. Classes with long tasks need a lower
    fraction, or a longer wall time.
  - Jug *adds up* `JUG_MAX_HOURS`, `JUG_MAX_MINUTES` and `JUG_MAX_SECONDS`.
    The worker script must unset the first two, so that a value inherited
    from the user's environment doesn't extend the limit.
  - Jug requires an integer, hence the `floor`.
  - A worker that exits early this way ends successfully. If tasks of its
    class are still pending, the submission strategy must start new workers
    for them.
- **Units**: parse memory and time into a canonical form and render them for
  each scheduler (e.g. `16GB` → SLURM's `16G`), with clear errors for bad
  values.
- **Failure handling**: when the scheduler kills a job for exceeding its
  memory or time limit, it uses SIGKILL and Jug's locks are left behind. At a
  minimum: recommend the `file_keepalive` backend, detect stale locks, and
  report jobs killed by the scheduler, broken down by class. Later:
  automatic retry with more resources (see "Smart limits" below).
- **Dry run**: `--dry-run` prints every script and submission command without
  submitting anything.
- **Logs**: one log file per worker, in a predictable location, named after
  the class and job ID.

## Eventual goal: smart limits

Resources in `jug_hpc.toml` are guesses. When a class has many tasks, the
first ones show what the rest need, so `jug_hpc` could use them to size the
later workers, for example:

- submit a small batch of workers first, and more once the time per task is
  known;
- lower `memory` to what the tasks actually use, or raise it after a
  memory kill and run the killed task again;
- size `time` (and `JUG_MAX_SECONDS`) from task durations instead of a fixed
  `time_fraction`;
- warn about classes that use far fewer CPUs than they request.

The principles apply to it as to the rest:

- **Opt-in and bounded.** The values in `jug_hpc.toml` stay the starting
  point; adaptation is configured per class (in TOML, as everything else),
  with lower and upper bounds, and can be turned off.
- **Explained.** Every change is logged with its reason, shown by
  `jug-hpc status`, and visible in the scripts kept for each job. A
  *suggest* mode (report what would change, as a diff of `jug_hpc.toml`)
  comes before any automatic one.
- **No change to the jugfile.** Measurements come from the scheduler (memory,
  CPU time and wall time of each job) and from `jug execute` itself (time and
  memory of each task), never from code added to the jugfile.

PLAN.md ("Smart limits") has the ideas, an API sketch and the order of the
work.

## Open questions

- Which submission strategy (above)?
- How should resource classes work for tasks that only appear after a
  `barrier()`? (Probably fine: classes are name patterns, not lists of tasks.)
- Is a selector by task name enough, or will users need different resources
  for tasks of the same function (e.g. based on input size)? If so, there must
  be some hook into the jugfile, which conflicts with principle 1.
- Users whose home directory is shared between several clusters need more
  than one user-level `cluster.toml`. Options: named profiles
  (`~/.config/jug_hpc/<name>.toml`, chosen with `--cluster <name>` or
  `JUG_HPC_CLUSTER`), or choosing a file automatically from the hostname.
- `time_fraction` is a fixed safety margin. Should `jug_hpc` also learn task
  durations, e.g. by recording how long each class's tasks take, and warn when
  a class's typical task is longer than the margin `(1 - time_fraction) × time`?
  (Yes, as the first step towards smart limits, above.)
- Smart limits need the time and memory of each task, which only the worker
  sees. Should that be an option of `jug execute` (a change in Jug) or a
  wrapper of jug_hpc's around it? And rerunning only the task that was killed
  in a bigger worker needs `jug execute` to select tasks by hash, not only by
  name.

## Non-goals

- Not a general workflow engine: the task graph, caching, and invalidation
  all stay in Jug.
- No cloud or Kubernetes backends (for now).
- No container or environment management beyond running a user-provided
  setup snippet.

## Prior art

- [gridjug](https://gridjug.readthedocs.io/): Jug on SGE via gridmap.
- Nextflow process `label`s: the closest model for attaching resources to
  types of task.
- Snakemake cluster profiles, dask-jobqueue, submitit.
- Retry with more resources: Nextflow's `errorStrategy 'retry'` with
  resources computed from `task.attempt`, and Snakemake's resources as
  functions of `attempt`. Both need code in the workflow; `jug_hpc` would do
  it from configuration.

## Success criteria

- A jugfile with three resource classes runs to completion on PBS with a
  single `jug-hpc submit`, and the jugfile itself is unchanged.
- No worker sits idle for more than a few minutes waiting for dependencies.
- `--dry-run` output is enough for a user to reproduce the submission by
  hand.
