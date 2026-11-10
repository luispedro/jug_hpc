# Manual test on a real PBS cluster

These are the checks that CI cannot do, rerun by hand for each release. Each
step says what to look for. `collect.sh` saves
qstat output to `recorded/`, which goes back into `tests/pbs_output/`.

Takes two to three hours, plus time waiting in the queue. Every command below
uses `$RPID`, the project to charge jobs to (`qsub -P`): `export RPID=<project>`
first.

## All at once: `run-everything.sh`

Runs steps 0 to 9 one after the other, unattended, keeping every result in
this directory. On a login node, from this directory inside an up-to-date
jug_hpc checkout, in `tmux`/`screen` (or with `nohup`):

```bash
RPID=<project> PYTHON=python3.12 ./run-everything.sh
```

- A full run starts from scratch: the results of an earlier run, if any, are
  first moved into `old-results-<date>.tgz`, and `venv/` is recreated (with
  `install.sh`) from the checkout this directory is in, so the code tested is
  the checkout's (its commit goes to `recorded/env.txt`). Run from elsewhere,
  give the checkout: `./run-everything.sh /path/to/jug_hpc [/path/to/jug]`.
- It creates `~/.config/jug_hpc/cluster.toml` from `cluster.toml.example`
  (with `-P $RPID`) if there is none. If the site needs more (`account`,
  `partition`, `module load` in `setup`), create that file yourself first (as
  in step 0) and edit it.
- Its output goes to `run.log`; `recorded/summary.txt` says which steps
  passed, and `run.log` has an `ok:`/`NOT AS EXPECTED:` line for each thing
  checked in steps 3 to 9. For steps 4 and 8, "OK" means the submitter
  stopped as it should.
- Each wait for jobs (queueing included) is capped at `WAIT` seconds (default
  4 hours).
- To rerun only some steps, adding to the results already here:
  `STEPS="2" ./run-everything.sh`. `./run-everything.sh --clean` removes the
  results.
- It records the queue limits (`qstat -Qf`) in `recorded/queues.txt`, for
  jobs that `qsub` rejects.

At the end, everything worth sending back is in `pbs-results.tgz` (no
jugdirs, no `venv/`).

The steps below are what it does, for running them by hand.

## 0. Setup (login node)

```bash
# copy this directory and a jug_hpc checkout to the cluster, then:
cd pbs                                  # this directory
PYTHON=python3.12 ./install.sh /path/to/jug_hpc   # Python >= 3.11; `module load` first if needed
mkdir -p ~/.config/jug_hpc
sed "s/myproject/$RPID/" cluster.toml.example > ~/.config/jug_hpc/cluster.toml   # set account/partition/setup if needed
source venv/bin/activate
jug-hpc -C 1-static config              # should show scheduler = "pbs" from ~/.config

mkdir -p recorded
qstat --version > recorded/env.txt 2>&1
qmgr -c 'print server' 2>&1 | grep -i history >> recorded/env.txt   # job history settings, if allowed
```

The projects' `setup` runs `source ../venv/bin/activate` in the workers. If
the venv's Python needs a module on compute nodes, put the `module load` in
`setup` in `cluster.toml`.

## 1. Static submission, by hand (milestone 2)

`1-static` has 6 `work` tasks (default class, 3 workers → an array job) and 2
`single` tasks (1 worker → a plain job). Each task sleeps 60 s.

```bash
cd 1-static
jug-hpc check
jug-hpc submit --dry-run | tee dry-run.txt
# run the two printed qsub lines by hand, from this directory; note the IDs
qsub .jug_hpc/scripts/single-1.sh       # -> SINGLE
qsub .jug_hpc/scripts/default-1.sh      # -> ARRAY (e.g. 1234[].server)
../collect.sh queued  SINGLE ARRAY      # while still queued, if you can catch it
../collect.sh running SINGLE ARRAY      # while running
# wait until both are done (tasks ~2-3 min, then workers wait ~3 min for more tasks)
../collect.sh finished SINGLE ARRAY
../collect.sh unknown 99999999
../collect.sh mixed SINGLE 99999999
jug status                              # expect 8 finished
ls logs/ logs/scheduler/
cd ..
```

Look for:
- Did `qsub` accept the `-o logs/scheduler/` and `-e logs/scheduler/` directories? The
  PBS output files should be in `logs/scheduler/`, and the worker logs
  (`default-<id>-<n>.log`, `single-<id>.log`) in `logs/`.
- Was the plain job (`single`, no `-J`) accepted? Did the array run 3 elements?
- What exactly did `qsub` print (with or without a server part, `[]` for arrays)?

## 2. Exit statuses (job outcomes)

Plain PBS scripts, no jug_hpc. They record how PBS reports each kind of
ending. They don't read `cluster.toml`, so the project is given to `qsub`.

```bash
cd 5-exit-status
qsub -P "$RPID" ok.sh; qsub -P "$RPID" exit3.sh; qsub -P "$RPID" mem.sh
qsub -P "$RPID" walltime.sh; qsub -P "$RPID" array-ok.sh
qsub -P "$RPID" qdel.sh               # once it is running: qdel <id>
qsub -P "$RPID" array.sh              # once running: qdel '<n>[3].<server>'  (element 2 exits 3 by itself)
# when all have ended (walltime takes a few minutes):
../collect.sh exit-ok OK_ID
../collect.sh exit-3 EXIT3_ID
../collect.sh exit-mem MEM_ID
../collect.sh exit-walltime WALLTIME_ID
../collect.sh exit-qdel QDEL_ID
../collect.sh exit-array ARRAY_ID
../collect.sh exit-array-ok ARRAY_OK_ID
cd ..
```

Look for: the `Exit_status` of each, and whether `-t` lists finished array
elements with their own `Exit_status`. For `mem.sh`, also check how it was
killed (the `jhpc-mem.o<id>` file, or nothing if it ran to the end: then the
site doesn't enforce memory).

## 3. Three classes, polling submitter (milestone 3)

`2-three-classes` is a chain: 4 `prepare` (default class) → 4 `align` (2 CPUs) →
`merge` (3 CPUs) → `report` (default). Run the submitter on the login node,
in `tmux` or `screen`:

```bash
cd 2-three-classes
jug-hpc submit --interval=30 2>&1 | tee submit.log
python ../check.py . | tee check.txt
../collect.sh three-classes $(python -c "import json; print(*(j['job_id'] for j in json.load(open('.jug_hpc/jobs.json'))['jobs']))")
cd ..
```

`run-everything.sh` also runs `jug-hpc status` every 2 minutes during the
run (`status.log`), and once after it (`status.txt`).

Look for: `submit` ends with `every task is finished` and exit status 0;
`check.py` says `OK`; `align` and `merge` jobs are submitted only once their
tasks are ready; no worker waits more than a few minutes between starting and
running its first task (`check.py` prints these waits from `qstat -x -t`).

## 4. Failing setup

Every worker in `3-failing-setup` fails in `setup` (`exit 3`), before Jug
starts.

```bash
cd 3-failing-setup
jug-hpc submit --interval=20 2>&1 | tee submit.log; echo "exit status: $?" >> submit.log
ls logs/scheduler/
cd ..
```

Then `jug-hpc status > status.txt 2>&1`.

Look for: it stops after 3 jobs, with an error that the workers keep failing
and exit status 1; `status` warns that the last jobs of `[default]` ended in
error. It needs job history: if `submit` keeps submitting, or stops
without having seen the jobs end, note the site's job history settings
(step 0).

## 5. The submitter as a job

The same chain as step 3, with the submitter running as a PBS job. This checks
that compute nodes may run `qsub`/`qstat`, and that the job finds
`~/.config/jug_hpc/cluster.toml`.

```bash
cd 4-as-job
jug-hpc submit --as-job
# wait for it to finish; follow logs/submitter-<id>.log
python ../check.py . | tee check.txt
cd ..
```

Look for: as in step 3, in `logs/submitter-<id>.log`. If the job fails at once,
see `logs/scheduler/`. `run-everything.sh` runs `jug-hpc status` every 2
minutes while the submitter job runs (`status.log`): it should show the
submitter job.

## 6. `jug-hpc cancel`

The submitter as a job again, with tasks too long to finish (20 minutes each).
Once its workers run, `jug-hpc cancel` cancels the submitter job first, waits
until it has stopped, and then cancels the workers.

```bash
cd 6-cancel
jug-hpc submit --as-job --interval=30
# once a worker is running (jug-hpc status, or qstat):
jug-hpc cancel 2>&1 | tee cancel.log
# once every job has ended:
jug-hpc cancel                          # nothing left: a note, exit status 0
jug-hpc status > status.txt 2>&1        # no job reported as killed
../collect.sh cancel $(python -c "import json; print(*(j['job_id'] for j in json.load(open('.jug_hpc/jobs.json'))['jobs']))")
qdel SUBMITTER_ID 99999999 > ../recorded/qdel-finished-unknown.txt 2>&1; echo "exit status: $?" >> ../recorded/qdel-finished-unknown.txt
cd ..
```

Look for: `cancel` lists the submitter job first, then the workers, and exits
with status 0; the submitter's log ends with `stopped by SIGTERM`, and no
worker is submitted after it (`logs/submitter-<id>.log`); the jobs end deleted
(`substate` 91 in `recorded/cancel-t.json`). The messages of the last `qdel`
(a finished job, an unknown one) are what the backend expects
to ignore: if they differ, `jug-hpc cancel` fails on jobs that have ended.

## 7. Jobs killed by the scheduler, and stale locks

`7-kills` has a `hog` task that touches 3 GB, a `slow` task that sleeps 4
minutes, and a `final` task (default class) that needs both. With
`too-small.toml`, `hog` gets 1 GB and `slow` 2 minutes. On PBS, the memory
kill is the cgroup's OOM killer (SIGKILL: the Jug lock stays), the wall time
kill a SIGTERM (Jug releases the lock, so the submitter tries again until it
stops the class after 3 jobs).

```bash
cd 7-kills
cp too-small.toml jug_hpc.toml
jug-hpc submit --interval=30 --max-interval=60 > submit.log 2>&1 &
# wait until submit.log has '[class.hog]: 1 task is locked' and
# '[class.slow]: 3 jobs in a row ended in error'
jug-hpc status > status-during.txt 2>&1
kill -TERM %1; wait %1; echo "exit status: $?" >> submit.log    # 143
jug-hpc status > status-stopped.txt 2>&1
cp fixed.toml jug_hpc.toml
jug-hpc status --release-stale > release.txt 2>&1
jug-hpc submit --interval=30 > submit2.log 2>&1; echo "exit status: $?" >> submit2.log
jug-hpc status > status.txt 2>&1
../collect.sh kills $(python -c "import json; print(*(j['job_id'] for j in json.load(open('.jug_hpc/jobs.json'))['jobs']))")
cd ..
```

Look for, in `submit.log`: a warning for the `hog` job (SIGKILL, with the
suggestion to raise `memory`) and one per `slow` job (wall time, raise `time`
or lower `time_fraction`); the stale-lock warning for `hog`; `[class.slow]`
stopped after 3 jobs; `stopped by SIGTERM` at the end. In
`status-during.txt`: the kills per class. In `status-stopped.txt`: also that
`[class.slow]`'s jobs keep failing, and `hog`'s stale lock. `release.txt`:
`[class.hog]: released the locks of 1 task`. `submit2.log`: `every task is
finished`, exit status 0. If `hog` is not killed, the site doesn't enforce
memory (see step 2).

## 8. Failed tasks: the workers left are cancelled

`8-blocked` has a task that raises, two that sleep 2.5 minutes, and one that
needs all three (3 workers). Once the sleeping ones are done, no task can run;
the workers still waiting (a few minutes) for the last task are cancelled.

```bash
cd 8-blocked
jug-hpc submit --interval=20 --max-interval=20 2>&1 | tee submit.log; echo "exit status: $?" >> submit.log
jug-hpc status > status.txt 2>&1
../collect.sh blocked $(python -c "import json; print(*(j['job_id'] for j in json.load(open('.jug_hpc/jobs.json'))['jobs']))")
cd ..
```

Look for: `submit` ends with `no task can run: failed tasks`, exit status 1,
after `default: cancelled job ... no task can run`; the jobs end deleted
(`recorded/blocked-t.json`).

## 9. A process a task ran, killed for memory; `--cleanup-failed`

`9-child-oom` has a `child` task that runs a Python subprocess growing to 4 GB
(64 MB every 2 s), and a `final` task (default class) that needs it. With
`too-small.toml`, `child` gets 1 GB: the cgroup's OOM killer kills the
subprocess, not Jug, so the task fails, Jug exits 1 and PBS records
`Exit_status 1` with memory use at the limit, which jug_hpc reports as killed
for memory.

```bash
cd 9-child-oom
cp too-small.toml jug_hpc.toml
jug-hpc submit --interval=20 --max-interval=20 2>&1 | tee submit.log; echo "exit status: $?" >> submit.log
# once the job has ended:
jug-hpc status > status-blocked.txt 2>&1
../collect.sh child-oom $(python -c "import json; print(*(j['job_id'] for j in json.load(open('.jug_hpc/jobs.json'))['jobs']))")
cp fixed.toml jug_hpc.toml
jug-hpc submit --cleanup-failed --interval=30 > submit2.log 2>&1; echo "exit status: $?" >> submit2.log
jug-hpc status > status.txt 2>&1
cd ..
```

Look for: `submit` ends with `no task can run: failed tasks`, exit status 1.
`status-blocked.txt`: `[class.child]: 1 job killed for memory`, and the
failure note with `died with <Signals.SIGKILL: 9>` and `a process it ran was
killed with SIGKILL ... raise memory`. `submit2.log`: `cleaned up 1 failed
task`, then `every task is finished`, exit status 0. `status.txt`: no kill
reported (the job's resources differ from the class's now). If the kill is
not reported, the job's `resources_used.mem` in `recorded/child-oom-t.json`
was below 90% of its `mem` (sampled memory use missed the peak); the PBS
error file in `logs/scheduler/` may still say `Killed because of memory
limit`.

## Sending back the results

`run-everything.sh` leaves them in `pbs-results.tgz`. By hand:

```bash
tar czf pbs-results.tgz recorded run.log 1-static/*.txt \
    */*.log */*.txt */logs */.jug_hpc 5-exit-status/jhpc-* 7-kills/jug_hpc.toml \
    9-child-oom/jug_hpc.toml
```

Also note anything odd: error messages, jobs rejected by `qsub`, or a step that
needed a workaround.
