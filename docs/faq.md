# Frequently asked questions

## Do I need to change my jugfile?

No. All the scheduling information is in `jug_hpc.toml` and `cluster.toml`,
and workers are plain `jug execute` processes. The same jugfile still runs with
`jug execute` on your laptop.

## Which jugdir backend should I use?

On a cluster, use `file_keepalive:`:

```toml
jugdir = "file_keepalive:jugfile.jugdata"
```

It stores results exactly like the default directory backend, so you can
switch an existing project to it. The difference is how it handles dead
workers. When the scheduler kills a worker (for exceeding its memory or wall
time), the task it was running keeps its lock. With the default backend, Jug
counts that task as running until you remove the lock (the submitter and
`jug-hpc status` warn about such locks, and `jug-hpc status --release-stale`
removes them). With
`file_keepalive`,
a lock that hasn't been refreshed for 30 minutes counts as failed.
`jug-hpc submit --cleanup-failed` (or `jug cleanup --failed-only`) then
removes it, so the task can run again. See Jug's
[backends documentation](https://jug.readthedocs.io/en/latest/backends.html).

`jug-hpc check` adds a note when the jugdir can't detect dead workers: Jug's
default, any plain directory, and `redis:`.

## How do I choose `time_fraction`?

Workers stop taking new tasks once they have run for `time_fraction × time`.
But a task that has already started runs to its end, so the margin
`(1 − time_fraction) × time` must be longer than your longest task:

- many short tasks (minutes, in a worker with several hours): the default of
  0.8 is fine;
- tasks that take a sizeable part of the wall time: lower `time_fraction`, or
  raise `time`. With `time = "12:00:00"` and tasks of up to 5 hours,
  `time_fraction = 0.5` leaves a 6-hour margin;
- one task that takes most of the wall time: give it its own class. Each worker
  then runs it and exits.

See [early exit](concepts.md#early-exit-time_fraction).

## My tasks use several threads. How do they know how many CPUs they have?

Jug runs one task at a time in each worker, so the extra CPUs of a class are
only used if the task starts threads or processes itself. Each worker exports
`JUG_HPC_CPUS` and `OMP_NUM_THREADS`, set to the class's `cpus`. Read
`JUG_HPC_CPUS` in your task, with a default, so it still works with plain
`jug execute`:

```python
threads = int(os.environ.get('JUG_HPC_CPUS', '1'))
```

(faq-same-function)=
## Some tasks of the same function need much more memory than others. What can I do?

Classes select tasks by name, so all tasks of one function are in the same
class. This is deliberate: anything finer would need to run code from your
jugfile to decide, and the jugfile would no longer be independent of the
scheduling settings.

The usual way around it is two `TaskGenerator`s for the two sizes, calling
the same function. A task takes its name from the function that the
`TaskGenerator` wraps, so each needs its own thin wrapper:

```python
def _align(sample):
    ...

@TaskGenerator
def align_small(sample):
    return _align(sample)

@TaskGenerator
def align_large(sample):
    return _align(sample)

small = [align_small(s) for s in samples if size(s) < LIMIT]
large = [align_large(s) for s in samples if size(s) >= LIMIT]
```

Then give `align_large` its own class.

## Where should the submitter run?

On a login node if your site allows long-running processes there (in `tmux` or
`screen`, so it survives logging out). Otherwise, submit it as a job with
`jug-hpc submit --as-job`. See
[where to run the submitter](submitting.md#where-to-run-the-submitter).

## Can I submit the scripts myself?

Yes. `jug-hpc submit --dry-run` writes the scripts to `.jug_hpc/scripts/` and
prints the `qsub` commands, run from the project directory, that submit them.
`jug-hpc submit --all` runs exactly those commands.

## How do I see which class runs which task?

`jug-hpc check`. It loads the jugfile and lists the tasks of every class. If
the jugfile has a `barrier()` whose tasks are not done yet, only the tasks
defined before it are listed. The classes apply to the later tasks too, once
they appear.

## The same home directory is shared by several clusters. Can I have a `cluster.toml` per cluster?

Not yet (named cluster profiles are planned). For now, put a `cluster.toml`
in each project directory, or set `XDG_CONFIG_HOME` per cluster.
