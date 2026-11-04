# Submitting and running

`jug-hpc submit` has four modes:

| Command | What it does |
|---|---|
| `jug-hpc submit` | The polling submitter: submits workers as tasks become ready, until the run is over. |
| `jug-hpc submit --as-job` | Submits the polling submitter itself as a job, and exits. |
| `jug-hpc submit --all [CLASS...]` | Submits workers for every class (or the classes named) at once, and exits. |
| `jug-hpc submit --dry-run [CLASS...]` | Writes and prints the scripts and the commands that would submit them, without submitting. |

## Why workers are not all submitted at once

`jug execute --name X` runs only the tasks matching `X`. It does *not* run the
tasks they depend on: it waits for other workers to finish them, and exits when
its wait cycles run out. If every class were submitted at the start, the
workers of later stages would sit on their allocation waiting for the earlier
stages, and then exit without having done anything.

So jug_hpc submits workers for a class only once its tasks are ready to run.
This works for any task graph: classes may depend on each other in any order,
tasks of different classes may alternate, and new tasks may appear after a
`barrier()` or `bvalue()`.

## The polling submitter

`jug-hpc submit` polls every 60 seconds (change this with `--interval
SECONDS`), and less often while nothing changes (see [How often it
polls](#how-often-it-polls)). At each poll it:

1. loads the jugfile in a separate process and counts, per class, the tasks
   that are waiting, ready, running, failed and finished (the numbers of
   `jug status`);
2. asks the scheduler how many workers of each class are queued or running;
3. submits, for each class,

       min(ready tasks, workers) − queued or running workers

   new workers, when this is positive. Several workers are submitted as one
   job array.

Tasks that a worker is running hold a lock, so Jug counts them as running, not
ready. A class with `workers = 4` whose four workers are each running a task
gets no new worker, however many of its tasks are ready.

The submitter prints a line for each job it submits, and a summary whenever
the counts change:

```
[2026-11-05 07:25:48] align: submitted job 1 (10 workers, .jug_hpc/scripts/align-1.sh)
[2026-11-05 07:25:48] tasks: 21 waiting, 20 ready, 0 running, 0 failed, 0 finished; workers queued or running: 10
```

It stops:

- when every task is finished (exit status 0);
- when no task is ready or running and some have failed, so that the tasks
  still waiting can never run (exit status 1). It lists the failed tasks per
  class. The worker logs say why they failed. Once you have fixed the problem,
  `jug cleanup --failed-only` lets the failed tasks run again, and you can
  start `jug-hpc submit` again.

Workers that are still queued or running when the submitter stops are left
alone: a worker with nothing to do exits by itself after a few minutes.

### How often it polls

Each poll loads the jugfile and queries the scheduler, so the submitter backs
off while nothing changes. After a poll that finds the same task counts and
the same workers queued or running as the one before, and submits nothing, it
waits twice as long as the last time, up to `--max-interval SECONDS` (default:
5 times `--interval`, so 5 minutes). Any change, or a poll that fails, brings
the wait back to `--interval`.

During long stretches where the workers are busy with long tasks, it therefore
polls every 5 minutes. `--max-interval` is also the longest time it can take to
notice that tasks have become ready and submit workers for them. Set it to the
same value as `--interval` to poll at a fixed interval.

Loading a jugfile with 100,000 tasks takes about 2 seconds and 150 MB of memory
(most of it is computing the hashes of the tasks, as `jug status` does). Jug's
own status cache (`jug status --cache`) is not used, because it does not load
the jugfile again, so it would miss tasks created after a `barrier()` and your
changes to the jugfile.

### Stopping and restarting

The submitter keeps almost no state of its own. At each poll it reads the task
states from the jugdir and the submitted jobs from `.jug_hpc/jobs.json`. You can
stop it at any time (`Ctrl-C`) and start it again later. Workers that are
already submitted keep running, and the new submitter counts them.

Only one submitter may run for a project, since two would each submit the
workers the tasks need. A second `jug-hpc submit` fails with an error naming
the process (and host) of the first one.

If the scheduler or the jugfile can't be read at a poll (a `qstat` timeout, a
jugfile you are editing), the submitter prints a warning and tries again at the
next poll, up to 5 times in a row. At the first poll, any error stops it at
once, since it is most likely a mistake in the settings or the jugfile.

### Where to run the submitter

The submitter is a small process that sleeps most of the time, but it must run
for as long as the whole run. Two options:

- on a login node, in `tmux` or `screen`, if your site allows long-running
  processes there;
- as a job, with `jug-hpc submit --as-job`.

`--as-job` submits a job that runs `jug-hpc submit` in the project directory.
Its resources come from the `[submitter]` table of `jug_hpc.toml`, which must
set `time` (see {doc}`configuration`):

```toml
[submitter]
time = "7d"
```

Its script runs the cluster's and the project's `setup`, like a worker's, so
`jug-hpc` must be on the `PATH` after them. Its log is
`logs/submitter-<job id>.log`. Before submitting, `--as-job` checks the
settings and loads the jugfile, so mistakes are reported right away and not in
the job's log. It submits nothing if a submitter is already running, or a
submitter job is already queued or running, or no task is left to run.

The compute nodes must be allowed to submit jobs (`qsub`) and query them
(`qstat`). Most clusters allow this, but not all. If the submitter job reaches
its wall time, it is killed, but the workers keep running. Run
`jug-hpc submit --as-job` again to continue.

## Static submission: `--all` and `--dry-run`

`jug-hpc submit --all` submits workers for every class with tasks, all at
once, and exits. Each class gets `workers` workers, but no more than it has
tasks. This suits task graphs where the classes don't wait for each other,
e.g. independent tasks of different sizes. Name classes to submit only those
(`default` for the default class). A class named this way is submitted even
if it has no tasks yet.

`jug-hpc submit --dry-run` does the same, but prints the scripts and the
commands that would submit them, instead of submitting:

```
To submit these scripts, run from /home/me/rnaseq:
  # [class.merge]: 1 task, 1 worker
  qsub .jug_hpc/scripts/merge-1.sh
```

These are exactly the commands that `--all` runs.

If loading the jugfile stops at a `barrier()` whose tasks are not done, the
tasks after it are not known yet. Classes are then not capped at their task
count, and jug-hpc says so.

## The worker script

Every worker runs the same script body, whatever the scheduler. The scheduler
backend adds only the directive header (`#PBS ...`) and the variables that
hold the job ID and array index. Here is a worker of an array job, with
comments added:

```bash
#!/bin/bash
#PBS ...                                  # resources, from the class (see the PBS backend)

set -e                                    # stop if setup fails
cd -- /home/me/rnaseq                     # the project directory
JUG_HPC_JOB_ID="${PBS_JOBID%%[.[]*}"
JUG_HPC_ARRAY_INDEX="$PBS_ARRAY_INDEX"
mkdir -p -- /home/me/rnaseq/logs
exec >>/home/me/rnaseq/logs/align-"$JUG_HPC_JOB_ID"-"$JUG_HPC_ARRAY_INDEX".log 2>&1
export JUG_HPC_CLASS=align JUG_HPC_JOB_ID JUG_HPC_ARRAY_INDEX JUG_HPC_CPUS=8 OMP_NUM_THREADS=8

module load python/3.12                   # the cluster's setup

source .venv/bin/activate                 # the project's setup

unset JUG_MAX_HOURS JUG_MAX_MINUTES
export JUG_MAX_SECONDS=11520              # floor(time_fraction × time)
exec jug execute --keep-failed --name=align --nr-wait-cycles=15 --wait-cycle-time=12 \
     --jugdir=/home/me/rnaseq/jugfile.jugdata -- /home/me/rnaseq/jugfile.py
```

Some details:

- The script uses `set -e` but not `set -u`, because setup snippets often
  source scripts (conda activation, environment modules) that use unset
  variables.
- The worker redirects its own output to its log. Log names are then the same
  on every scheduler, and you can read the logs while the job runs. (PBS
  otherwise keeps a job's output until the job ends.)
- The scheduler's own output files go to `logs/scheduler/`. They catch what
  happens before the redirect, e.g. a failing `setup`.
- The CPU variables are exported *before* the setup snippets, so a snippet can
  change them. `JUG_MAX_SECONDS` is set *after* them, so nothing can change
  the time limit (see [early exit](concepts.md#early-exit-time_fraction)).
- `--keep-failed`: a task that raises an exception stays failed (until
  `jug cleanup --failed-only`). Without it, Jug would make the task ready
  again, every new worker would run it again, and the submitter would never
  stop.
- `--nr-wait-cycles=15 --wait-cycle-time=12`: a worker waits at most about
  3 minutes for tasks it can run, instead of Jug's default of 30 minutes.
- Every value is quoted for the shell.

The scripts are kept in `.jug_hpc/scripts/`, numbered per class
(`align-1.sh`, `align-2.sh`, ...). You can read them, and submit them by hand.

## Files jug_hpc writes

In the project directory:

```
logs/                     # the worker logs (the `logs` setting)
  <class>-<job id>.log            # one per worker
  <class>-<job id>-<index>.log    # one per element of a job array
  submitter-<job id>.log          # the submitter job's log (--as-job)
  scheduler/                      # the scheduler's own output files
.jug_hpc/
  .gitignore              # '*': the directory is never committed
  jobs.json               # every job submitted for the project
  scripts/                # every script written, <class>-<n>.sh
  jobs.lock, submitter.lock
```

`jobs.json` lists the jobs in the order they were submitted, with their
scheduler job IDs. Until `jug-hpc cancel` exists, use it to find the jobs to
cancel by hand.

## Current limitations

jug_hpc is in early development. In particular:

- `jug-hpc status` (task counts grouped by class) and `jug-hpc cancel` are not
  written yet. Use `jug status`, your scheduler's own commands and
  `.jug_hpc/jobs.json`.
- jug_hpc does not yet look at how jobs ended. A worker killed by the
  scheduler (for exceeding its memory or wall time) leaves its task's lock
  behind. With a plain jugdir, Jug then counts the task as running forever, so
  the submitter never finishes (`jug cleanup --locks-only` removes such locks,
  but only do this when no worker is running). Use the `file_keepalive:` jugdir
  backend instead: there, a lock that is not refreshed for 30 minutes counts
  as failed (see the {doc}`faq`).
- If workers die before they start Jug, for example because a `setup` command
  fails, the submitter submits new workers at every poll. Look at
  `logs/scheduler/` and the worker logs when a class's workers keep being
  submitted.
- The submitter loads the jugfile at every poll. For jugfiles with millions of
  tasks, use a longer `--interval` and `--max-interval`.
