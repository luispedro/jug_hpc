# Manual test on a real PBS cluster

These are the checks that CI cannot do (PLAN.md: milestone 2, and milestone 3
item 7). Each step says what to look for. `collect.sh` saves qstat output to
`recorded/`, which goes back into `tests/pbs_output/`.

Takes about an hour, plus time waiting in the queue. Every command below uses
`$RPID`, the project to charge jobs to (`qsub -P`): `export RPID=<project>`
first.

## All at once: `run-everything.sh`

Runs steps 0 to 6 one after the other, unattended, keeping every result in
this directory. On a login node, in `tmux`/`screen` (or with `nohup`):

```bash
export RPID=<project>
PYTHON=python3.12 ./run-everything.sh /path/to/jug_hpc   # the path is only needed the first time
```

- It creates `venv/` (with `install.sh`) if there is none, and
  `~/.config/jug_hpc/cluster.toml` from `cluster.toml.example` (with
  `-P $RPID`) if there is none. If the site needs more (`account`,
  `partition`, `module load` in `setup`), create that file yourself first (as
  in step 0) and edit it.
- Its output goes to `run.log`; `recorded/summary.txt` says which steps
  passed. For step 4, "OK" means the submitter stopped as it should.
- Each wait for jobs (queueing included) is capped at `WAIT` seconds (default
  4 hours).
- To run it again, first `./run-everything.sh --clean`. To rerun only some
  steps, adding to the results already here: `STEPS="2" ./run-everything.sh`.
- It records the queue limits (`qstat -Qf`) in `recorded/queues.txt`, for
  jobs that `qsub` rejects.

Then copy the whole directory back, without `venv/`:
`rsync -a --exclude venv/ ./ <machine>:<jug_hpc>/tests/manual/pbs/`.

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

Look for: it stops after 3 jobs, with an error that the workers keep failing
and exit status 1. It needs job history: if `submit` keeps submitting, or stops
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
see `logs/scheduler/`.

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
with status 0; no worker is submitted after it (`logs/submitter-<id>.log`); the
jobs end deleted (`substate` 91 in `recorded/cancel-t.json`). The messages of
the last `qdel` (a finished job, an unknown one) are what the backend expects
to ignore: if they differ, `jug-hpc cancel` fails on jobs that have ended.

## Sending back the results

```bash
tar czf pbs-results.tgz recorded 1-static/dry-run.txt \
    */submit.log */check.txt */logs 5-exit-status/*.o* 6-cancel/cancel.log 6-cancel/status.txt
```

Also note anything odd: error messages, jobs rejected by `qsub`, or a step that
needed a workaround.
