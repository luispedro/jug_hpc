# Concepts

## Resource classes

A *resource class* is a set of tasks, chosen by name, together with the
resources of the workers that run them: CPUs, memory, wall time, how many
workers may run at once, and optionally a partition and scheduler-specific
extras such as GPUs.

Classes are defined in `jug_hpc.toml`, one `[class.<name>]` table each. The
name is short (`align`, `merge`) because it appears in job names
(`jug-align-<project>`), log file names and on the command line. It may
contain only letters, digits, `-` and `_`.

### Selecting tasks

A Jug task's name is the module and function of its `TaskGenerator` (or of the
function given to `Task`), e.g. `jugfile.align` or `mypipeline.steps.align`.
`jug status` lists them. Each class selects tasks with exactly one of:

`match`
: The rules of `jug execute --name`. The value must match the whole task name,
  or a trailing part of it that starts after a `.`. So `align` and
  `jugfile.align` both match `jugfile.align`, but `align` does not match
  `jugfile.align_reads`. Shell-style wildcards are allowed: `align*` matches
  both `jugfile.align` and `jugfile.align_reads`.

`pattern`
: The rules of `jug execute --pattern`. `/regex/` is a regular expression
  searched anywhere in the task name. Anything else is a plain string, also
  searched anywhere, so `pattern = "align"` matches `jugfile.align_reads` and
  `jugfile.realign`.

jug_hpc uses Jug's own matching functions for this, and each worker runs
`jug execute --name=<match>` (or `--pattern=<pattern>`). A class therefore
selects exactly the tasks that its workers will run.

Tasks are selected only by name. If tasks of one function need different
resources (say, depending on the size of their input), see the
{ref}`FAQ <faq-same-function>`.

### The default class

Every task that no class matches belongs to the *default class*, configured in
the `[default]` table. Its workers run every task except those of the other
classes. With a recent Jug, this uses `jug execute --exclude-name` and
`--exclude-pattern`. With Jug 2.7, jug_hpc generates an equivalent `--pattern`
regular expression and checks that it selects the same tasks.

`[default]` also provides the values that other classes don't set: a class
inherits every field it leaves out from `[default]`. `memory` and `time` have
no built-in default, so `[default]` must set them.

### Overlaps

A task matched by two classes is an error, reported by `jug-hpc check` and
`jug-hpc submit`. This is why the order of the classes in the file does not
matter.

## Workers

A *worker* is one scheduler job, or one element of a job array, running
`jug execute` with its class's selection. A worker runs many tasks one after
the other, so **wall time applies to the worker, not to a single task**.

`workers` is the largest number of workers of a class that may be queued or
running at the same time. The polling submitter starts at most as many workers
as there are tasks of that class ready to run (see {doc}`submitting`).

When a worker finds no task ready to run, it waits for a few minutes
(15 wait cycles of 12 seconds, instead of Jug's default of 30 minutes) and then
exits. The submitter only starts workers for tasks that are ready, so a worker
with nothing to do should give its allocation back quickly.

### Early exit: `time_fraction`

Every worker is started with Jug's `JUG_MAX_SECONDS` set to

    floor(time_fraction × time)

where `time` is the class's wall time in seconds. After each task, Jug checks
how long the worker has been running. Once it is past this limit, it stops
taking new tasks and exits successfully, before the scheduler kills it. If
tasks of its class are still waiting, the submitter starts new workers for
them.

`time_fraction` defaults to 0.8 and must be larger than 0 and at most 1. Keep
in mind:

- Jug only checks the limit *after a task finishes*. A task that starts just
  before the limit runs to completion, or until the scheduler kills the worker.
  The default of 0.8 assumes each task takes much less than 20% of the wall
  time. For classes with long tasks, use a lower fraction or a longer wall
  time. A task that takes almost the whole wall time needs a class whose
  workers each run one task.
- Jug adds `JUG_MAX_HOURS`, `JUG_MAX_MINUTES` and `JUG_MAX_SECONDS` together.
  The worker script unsets the first two, so a value set in your environment
  or in a `setup` snippet can't extend the limit.

### CPUs

Jug runs one task at a time in each worker, so `cpus > 1` helps only if the
task itself uses several threads or processes. The worker script exports the
number of CPUs as `JUG_HPC_CPUS` and `OMP_NUM_THREADS`, before running the
`setup` snippets (which may change them). Read `JUG_HPC_CPUS` in your task
to decide how many threads to use:

```python
import os

@TaskGenerator
def align(sample):
    threads = int(os.environ.get('JUG_HPC_CPUS', '1'))
    ...
```

With the default of 1, the task still runs normally under plain
`jug execute`.

## Cluster settings and project settings

Configuration is split into two TOML files:

`cluster.toml`
: *How* to submit on this cluster: the scheduler, the account, the default
  partition, module loads, and scheduler options for every job. It is usually
  the same for all your projects on a cluster, so it lives in
  `~/.config/jug_hpc/`.

`jug_hpc.toml`
: *What* this project needs: the jugfile, the resource classes, the project's
  environment and where logs go. It lives in the project directory.

Cluster-only settings (`scheduler`, `account`) are not allowed in
`jug_hpc.toml`. This keeps the project file portable between clusters. A
project that needs a different account can have its own `cluster.toml` next
to `jug_hpc.toml`, which overrides the user file key by key. Neither file can
run code or import the jugfile. See {doc}`configuration`.

The *project directory* is the directory that holds `jug_hpc.toml`. Every
`jug-hpc` command looks for it in the current directory and then in each
parent directory, or starts the search in `DIR` with `jug-hpc -C DIR`. Paths in
`jug_hpc.toml` are relative to it.
