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

1. asks the scheduler how many workers of each class are queued or running,
   and how the jobs that ended since the last poll did;
2. loads the jugfile in a separate process and counts, per class, the tasks
   that are waiting, ready, running, failed and finished (the numbers of
   `jug status`);
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
  start `jug-hpc submit` again;
- when the only tasks that could run belong to classes whose workers keep
  failing (exit status 1; see [Workers that keep
  failing](#workers-that-keep-failing)).

When it stops because no task can run, the submitter cancels the workers
that are still queued or running, with a line such as

```
[2026-11-05 17:20:45] default: cancelled job 2 (4 workers running): no task can run
```

They have nothing to do (no task is ready or running), but each would keep its
allocation for about 3 minutes waiting for tasks before giving up, and a queued
one would still start. If they can't be cancelled (the scheduler can't be
reached), the submitter prints a warning and stops all the same: run
`jug-hpc cancel` later, or let them exit by themselves. When every task is
finished, the workers left exit at once, so they are not cancelled.

### Workers that keep failing

A worker that dies before it starts Jug, for example because a command in
`setup` fails, leaves its tasks ready, so the submitter would replace it at
every poll, forever. To avoid this, the submitter looks at how its jobs ended.
When 3 jobs of a class in a row end in error (a non-zero exit status, or
killed by the scheduler) and no task of the class has ended (finished or
failed) in the meantime, it stops submitting workers for that class and prints
a warning. Workers of the class that are already queued keep their chance: if
one of them runs a task, or a job of the class succeeds, the submitter
submits workers for the class again.

If nothing else can run, the submitter stops. Here, `setup` runs `module load
samtools` on a machine without `module`:

```
[2026-11-05 09:03:50] align: submitted job 1 (10 workers, .jug_hpc/scripts/align-1.sh)
[2026-11-05 09:03:50] tasks: 1 waiting, 20 ready, 0 running, 0 failed, 0 finished; workers queued or running: 10
[2026-11-05 09:03:55] align: submitted job 2 (10 workers, .jug_hpc/scripts/align-2.sh)
[2026-11-05 09:04:00] align: submitted job 3 (10 workers, .jug_hpc/scripts/align-3.sh)
[2026-11-05 09:04:06] tasks: 1 waiting, 20 ready, 0 running, 0 failed, 0 finished; workers queued or running: 0
jug-hpc: error: no task can run: the workers keep failing: [class.align]: 3 jobs in a row ended in error before any task of the class ended (jobs 1, 2, 3; the last: 10 of 10 workers did not succeed (workers 1-10: exit status 127))
jug-hpc: note: the worker logs (in logs/) and the scheduler's (in logs/scheduler/) say why the jobs failed; once fixed, run 'jug-hpc submit' again
```

and `logs/align-3-1.log` says:

```
/home/me/rnaseq/.jug_hpc/scripts/align-3.sh: line 14: module: command not found
```

Only the jobs that the submitter saw queued or running count: jobs that had
already ended when it started may have failed before you fixed the problem,
so a new `jug-hpc submit` gives every class a fresh start. On PBS, this needs
the server to keep job history (`qstat -x`); without it, finished jobs are
forgotten at once and their outcome is unknown.

### Workers killed by the scheduler

When a worker is killed by the scheduler, for using more memory or more wall
time than its class asks for, the submitter prints a warning naming the class,
the job and what to change in `jug_hpc.toml`: raise `memory`, or raise `time`
or lower `time_fraction` (a worker takes no new task after `time_fraction ×
time`, so a task that starts just before then must end in the time left; see
[early exit](concepts.md#early-exit-time_fraction)). When the run is over, it
sums up the jobs killed in each class.

Here, one `align` task takes 30 seconds, in a test with a 5-second wall time
for the class (the output is cut):

```
[2026-11-05 14:19:23] tasks: 1 waiting, 0 ready, 1 running, 0 failed, 5 finished; workers queued or running: 1
jug-hpc: warning: [class.align]: job 1 ended: 1 of 2 workers did not succeed (worker 2: killed: wall time limit reached); raise time (now 00:00:05), or lower time_fraction (now 0.8: a task started after 00:00:04 has 00:00:01 left)
[2026-11-05 14:19:25] align: submitted job 2 (1 worker, .jug_hpc/scripts/align-2.sh)
...
jug-hpc: warning: [class.align]: job 3 ended: killed: wall time limit reached; raise time (now 00:00:05), or lower time_fraction (now 0.8: a task started after 00:00:04 has 00:00:01 left)
[2026-11-05 14:19:42] tasks: 1 waiting, 1 ready, 0 running, 0 failed, 5 finished; workers queued or running: 0
jug-hpc: warning: jobs killed by the scheduler: [class.align] 3 for wall time
jug-hpc: error: no task can run: the workers keep failing: [class.align]: 3 jobs in a row ended in error before any task of the class ended (jobs 1, 2, 3; the last: killed: wall time limit reached)
```

The task was killed three times in a row, so the class was stopped as
described above.

On clusters that enforce memory limits with cgroups, a worker that runs out of
memory is killed with `SIGKILL` and the scheduler records nothing else (see
{doc}`schedulers`), so a job killed by `SIGKILL` gets the memory suggestion
too, as a guess. Jobs you delete yourself (`qdel`) are not reported. An array
job is reported once, by its worst worker (killed for memory before killed for
wall time); the warning lists how each of its workers that did not succeed
ended. The values in the suggestion are the class's current ones. The local
backend enforces only the wall time. `jug-hpc status` reports the killed jobs
too (see [below](#following-a-run-jug-hpc-status)).

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
stop it at any time (`Ctrl-C`, exit status 130) and start it again later.
Workers that are already submitted keep running, and the new submitter counts
them.

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

## Following a run: `jug-hpc status`

`jug-hpc status` shows, for each class, its tasks in each state (counted as
the submitter counts them, loading the jugfile) and its workers queued and
running (asking the scheduler about the jobs in `.jug_hpc/jobs.json`), and
whether a submitter is running. It changes nothing, and you can run it at any
time, also while the submitter runs. Here, during the run of the example
above:

```
$ jug-hpc status
                                 tasks                        workers
class          waiting  ready  running  failed  finished  queued  running
[class.align]        0      1       10       0         9       0       10
[class.merge]        1      0        0       0         0       0        0
[default]           11      1        4       0         4       0        4
total               12      2       14       0        13       0       14

Submitter: running (process 723229@login1)
```

The submitter is the process that holds the project's lock (on a login node,
or in a job), or a submitter job (`--as-job`) that is queued or running.

The jobs that the scheduler killed are warnings, one per class and way of
being killed, with the same suggestion as in the submitter's log. They cover
every job in `jobs.json` that the scheduler still knows (on PBS, this needs
job history), also those from earlier runs. Here, after the run stopped:

```
$ jug-hpc status
                                 tasks                        workers
class          waiting  ready  running  failed  finished  queued  running
[class.align]        0      1        0       0        19       0        0
[class.merge]        1      0        0       0         0       0        0
[default]            1      0        0       0        19       0        0
total                2      1        0       0        38       0        0

Submitter: not running
jug-hpc: warning: [class.align]: 4 jobs killed for wall time (the last: job 5: killed: wall time limit reached); raise time (now 00:00:05), or lower time_fraction (now 0.8: a task started after 00:00:04 has 00:00:01 left)
jug-hpc: note: tasks are ready, but no worker is queued or running and no submitter is running: run 'jug-hpc submit'
```

The submitter cancelled the four `default` workers when it stopped: they were
waiting for the `count` task that needs the `align` task that keeps being
killed. The note does not know why the submitter stopped: here, raise `time`
first.

Tasks that are locked (counted as running) in a class with no worker queued
or running are a warning too. Their workers died while running them (a worker
killed with `SIGKILL`, for example for exceeding its memory, can't release its
task's lock), or someone runs them with `jug execute` outside jug-hpc. Here,
after a worker was killed in the middle of a task:

```
jug-hpc: warning: [default]: 1 job killed by a signal (the last: job 1: killed by SIGKILL); if it used more memory than requested, raise memory (now 1G)
jug-hpc: warning: [default]: 1 task is locked, but no worker of the class is queued or running: unless a 'jug execute' started outside jug-hpc is running it, the worker that locked it died (e.g., killed by the scheduler) and left its lock behind, so no worker will run it and the submitter will not finish (remove the lock with 'jug-hpc status --release-stale')
```

`jug-hpc status --release-stale` removes these locks, so that the tasks are
ready again. It asks the scheduler about the jobs again just before, and
leaves alone the locks of a class that has a worker queued or running by
then. It also removes the locks of a `jug execute` started outside jug-hpc,
so don't use it while one runs. The table is that of a second count, after
the locks were removed:

```
$ jug-hpc status --release-stale
[default]: released the locks of 1 task
                                  tasks                        workers
class           waiting  ready  running  failed  finished  queued  running
[class.middle]        0      0        0       0         0       0        0
[default]             0      1        0       0         0       0        0
total                 0      1        0       0         0       0        0

Submitter: not running
jug-hpc: warning: [default]: 1 job killed by a signal (the last: job 1: killed by SIGKILL); if it used more memory than requested, raise memory (now 1G)
jug-hpc: note: tasks are ready, but no worker is queued or running and no submitter is running: run 'jug-hpc submit'
```

A submitter that is running submits workers for these tasks at its next
poll. Fix the cause first (here, raise `memory`), or the next worker will be
killed too.

Notes say when every task is finished, when no task can run because some
failed, and when tasks are ready but no worker is queued or running and no
submitter is running (run `jug-hpc submit`). The exit status is 0 unless the
settings, the jugfile, `jobs.json` or the scheduler can't be read.

## Cancelling a run: `jug-hpc cancel`

`jug-hpc cancel` cancels every job in `.jug_hpc/jobs.json` that is queued or
running: the submitter job, if there is one, and the workers. Here, with
`submit --as-job` and its workers running:

```
$ jug-hpc cancel
submitter: cancelled job 1 (running)
align: cancelled job 2 (10 workers running)
default: cancelled job 3 (4 workers running)
```

A submitter would replace the cancelled workers, as their tasks are still
ready, so it goes first. `cancel` cancels the submitter job, waits (up to 2
minutes) until it has stopped, and only then cancels the workers, including
any the submitter submitted in the meantime. If the submitter job has not
stopped by then, the workers are cancelled anyway, with an error asking you to
run `jug-hpc cancel` again once it has stopped (exit status 1).

A submitter running outside the scheduler (`jug-hpc submit` on a login node)
can't be cancelled this way, so `cancel` then cancels nothing and fails, naming
its process. Stop it first with Ctrl-C (its workers keep running), then run
`jug-hpc cancel`.

Cancelled workers get `SIGTERM` (and later `SIGKILL` from the scheduler, if
they are still running), on which `jug execute` releases the lock of the task
it was running, so those tasks are ready again. To carry on later, run
`jug-hpc submit` again. Cancelled jobs are not reported as killed by
`jug-hpc status`. Jobs that have already ended are left alone, so running
`cancel` twice does no harm:

```
$ jug-hpc cancel
jug-hpc: note: no job of this project is queued or running
```

`cancel` reads only the cluster settings and `jobs.json`: it neither loads the
jugfile nor reads `jug_hpc.toml`, so it works even while they have errors.
Jobs recorded with another scheduler backend (e.g. the local one, while trying
the project out) are not cancelled; a note says how many.

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
scheduler job IDs. `jug-hpc status` and `jug-hpc cancel` use it to find the
project's jobs.

## Current limitations

jug_hpc is in early development. In particular:

- Locks left behind are not removed by the submitter. A worker killed with
  `SIGKILL` (for example for exceeding its memory) leaves its task's lock
  behind (one killed with `SIGTERM`, as for wall time, usually releases it).
  With a plain jugdir, Jug then counts the task as running forever, so the
  submitter never finishes and its log does not say why: `jug-hpc status`
  reports such locks, and `jug-hpc status --release-stale` removes them. With
  the `file_keepalive:` jugdir backend, a lock that is not refreshed for 30
  minutes counts as failed instead, so the run ends (see the {doc}`faq`).
- The submitter loads the jugfile at every poll. For jugfiles with millions of
  tasks, use a longer `--interval` and `--max-interval`.
