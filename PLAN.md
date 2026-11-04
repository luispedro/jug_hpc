# jug_hpc: implementation plan

This plan turns [GOALS.md](GOALS.md) into ordered steps and milestones. Where
GOALS.md lists an open question, the plan gives a recommended answer and says
which step depends on it. Finished work is summarized only as far as later
steps depend on it; the details are in the module docstrings and `git log`.

## Decisions taken up front

- **Language and versions.** jug_hpc is pure Python and requires
  **Python 3.11 or later** (`requires-python = ">=3.11"`), currently
  3.11–3.15. This is one version narrower than Jug (3.10+), so that TOML is
  read with the standard library's `tomllib`. Otherwise jug_hpc follows Jug:
  when Jug drops or adds a version, jug_hpc does the same. Jug is the only
  runtime dependency.
- **Minimum Jug version.** `jug >= 2.7` (the first release with `--name` and
  `--pattern`). Some steps need small additions to Jug (see "Changes needed in
  Jug"), which will raise this minimum before jug_hpc 1.0.
- **Scheduler order.** **PBS first** (OpenPBS / PBS Professional), then SLURM.
  The SLURM backend is planned before 1.0 because a second real backend is the
  only way to prove that the backend interface is easy to replace.
- **Tooling.** Same as Jug: `uv` with `uv.lock`, `pytest`, a GitHub Actions
  matrix over every supported Python version, and the MIT license.
- **Submission strategy.** **A polling submitter** (strategy 2 in GOALS.md)
  is the only strategy for 1.0.
  - It is the only strategy that handles every task graph, including classes
    that alternate and graphs that grow after a `barrier()`.
  - Scheduler dependencies (strategy 1) fail silently on exactly the graphs
    that need jug_hpc most, and the hybrid needs both implementations.
  - The cost is a long-running process. This is kept small: one process per
    project, sleeping most of the time, and runnable either on a login node or
    as a small scheduler job (`jug-hpc submit --as-job`).
  - Strategy 1 can still be added after 1.0 as an optimization, behind the
    same interface.

## Architecture

```
jug_hpc/
  config/        load, merge and validate cluster.toml and jug_hpc.toml
  units.py       memory and time parsing (canonical: bytes, seconds)
  classes.py     resource classes, task -> class mapping (uses Jug's matchers)
  jugstate.py    the only module that imports Jug internals: load the jugfile,
                 count waiting/ready/running/failed/finished tasks per name
  selection.py   the `jug execute` arguments that select a class's tasks
  worker.py      WorkerSpec: everything one worker needs, scheduler-independent
  script.py      the worker script body shared by every backend
  schedulers/
    base.py      Scheduler interface, JobStatus/JobOutcome types, registry
    checks.py    scheduler settings checked against the installed backends
    pbs.py       PBS backend (OpenPBS / PBS Pro)
    local.py     runs workers as local subprocesses (tests, laptops)
    slurm.py     (milestone 5)
  runner.py      runs external commands; replaced by a fake in tests
  state.py       per-project record of submitted jobs (.jug_hpc/ directory)
  check.py       jug-hpc check
  submit.py      static submission (submit --all / --dry-run)
  submitter.py   the polling submitter's decisions (pure functions)
  poll.py        the polling loop
  cli.py         jug-hpc config | check | submit | status | cancel
```

Two rules keep the design testable and the schedulers replaceable:

1. **Everything scheduler-specific lives in one backend module.** The rest of
   jug_hpc only builds `WorkerSpec`s and calls the interface.
2. **Backends separate pure functions from side effects.** Rendering a
   script, building a command line and parsing scheduler output are pure
   functions, tested against recorded output. Only `runner.py` executes
   anything.

### Scheduler interface

As in `schedulers/base.py`; it may still change until milestone 5, when the
SLURM backend is written.

```python
class Scheduler(ABC):
    name: ClassVar[str]                      # "pbs"; also the name of the raw-options table

    def __init__(self, cluster: ClusterSettings, runner: Runner): ...

    # pure
    def render_script(self, spec: WorkerSpec) -> str
    def submit_command(self, script: Path, spec: WorkerSpec) -> list[str]
    def parse_submit_output(self, stdout: str) -> JobId
    @classmethod
    def validate_raw_options(cls, raw: dict, where) -> None  # shape only, values are passed through

    # side effects (through self.runner)
    def submit(self, script: Path, spec: WorkerSpec) -> JobId
    def query(self, jobs: list[JobId]) -> dict[JobId, JobStatus]     # state + active workers
    def outcome(self, jobs: list[JobId]) -> dict[JobId, JobOutcome]  # exit code, killed for mem/time
    def cancel(self, jobs: list[JobId]) -> None
```

`WorkerSpec` holds the class name, the portable resources in canonical units
(cpus, memory in bytes, wall time in seconds), the number of array elements,
the partition, the merged raw options for this backend, the setup snippets,
the log path template, and the command line. Backends are found through the
`jug_hpc.schedulers` entry point group, so a new scheduler can be added in a
separate package without changing jug_hpc.

The worker script body is the same for every scheduler and is produced by a
shared template; a backend adds only its directive header and the names of its
environment variables (job ID, array index, allocated CPUs):

```sh
#!/bin/bash
#PBS ...                                  # backend-specific header
set -e                                    # not -u: conda/module scripts use unset vars
cd -- <workdir>
JUG_HPC_JOB_ID="<backend expression>"
mkdir -p -- <logs>
exec >><logs>/<class>-"$JUG_HPC_JOB_ID".log 2>&1   # append, written live
export JUG_HPC_CLASS=<class> JUG_HPC_JOB_ID JUG_HPC_CPUS=<cpus> OMP_NUM_THREADS=<cpus>
<cluster setup>                           # after the CPU variables: may override them
<project setup>
unset JUG_MAX_HOURS JUG_MAX_MINUTES       # after setup: nothing changes the limit
export JUG_MAX_SECONDS=<floor(time_fraction * time)>
exec jug execute --keep-failed --name=<selector> --nr-wait-cycles=15 \
     --wait-cycle-time=12 --jugdir=<jugdir> -- <jugfile>
```

(As implemented in `script.py`, whose docstring explains the choices. The
jugdir is always passed explicitly because Jug's jugrc files are ignored.)

The worker redirects its own output so that log names are the same on every
scheduler and logs are readable while the job runs (PBS otherwise spools
output until the job ends). The scheduler's own output files go to
`logs/scheduler/` to catch failures before the redirect (e.g. a bad `setup`).

Workers use few wait cycles (a few minutes in total, not Jug's default of
30 minutes). The submitter only starts workers when tasks are ready, so a
worker that finds nothing to do should exit quickly rather than hold its
allocation.

Workers run `jug execute --keep-failed`. Without it, Jug releases a failed
task's lock and the task is ready again, so the submitter would submit
workers for it forever and never see a failed task. With it, failed tasks
stay failed until `jug cleanup --failed-only`.

### PBS specifics

- Resources: `-l select=1:ncpus=<cpus>:mem=<mem>`, `-l walltime=HH:MM:SS`,
  `-q <partition>`, `-A <account>`, `-N jug-<class>-<project>`.
- Workers > 1: one array job, `-J 1-<n>`, index in `PBS_ARRAY_INDEX`.
  Workers = 1: a plain job (some PBS installations reject single-element
  arrays; to be checked).
- Memory is rendered in PBS units (`kb`/`mb`/`gb`), e.g. `16G` → `16gb`.
- Raw options (`[pbs]` in cluster.toml, `[class.<name>.pbs]` in jug_hpc.toml)
  have three sub-keys, because PBS options don't map to one flat namespace:
  - `select`: extra chunk resources, appended to the `select` statement
    (e.g. `ngpus = 1`);
  - `resources`: extra job-wide `-l` resources (e.g. `place = "scatter"`);
  - `directives`: a list of raw `#PBS` lines, passed through verbatim.
- Queue state: `qstat -f -F json -x JOB...`. Torque's `qstat` is different;
  Torque support is out of scope for 1.0 but must be possible as a separate
  backend.
- Job outcome: PBS `Exit_status`, where negative values are PBS's own codes
  (including walltime and memory kills) and values above 256 mean "killed by
  signal". The exact codes are taken from the PBS documentation and checked
  on a real cluster before being relied on.

The rest (what raw tables may set, the job-ID expression, `-o`/`-e` paths,
`qstat` error handling) is in `pbs.py`'s docstring.

## Changes needed in Jug

jug_hpc must not reimplement Jug (principle 2), but two things it needs are
not yet available:

1. **Running the default class.** Its workers must run every task *not*
   matched by any class, and `jug execute` can only include tasks, not
   exclude them. Proposal: `jug execute --exclude-name X` (repeatable) and
   `--exclude-pattern`, with the same matching rules as `--name`/`--pattern`.
   Fallback for older Jug: a generated `--pattern '/^(?!...)/'` regex that
   reproduces the `--name` rules. The fallback is fragile, so the Jug option
   is preferred.
2. **Task counts per state.** The submitter needs, for each task name, the
   number of tasks that are ready, running, waiting, failed and finished: the
   numbers `jug status` already computes (`update_status` in
   `jug/subcommands/status.py`). Proposal: a small public Python API (e.g.
   `jug.status.task_counts(jugfile, jugdir)`), plus `jug status --json`. The
   API should support Jug's status cache, because the submitter calls it
   repeatedly.

Both are small and independent of jug_hpc, and they go into the next Jug
release. Until then, `jugstate.py` calls the internal functions directly and
pins the Jug version it was tested with.

Status: both are on the sibling `../jug` checkout's main branch but not yet released
(PyPI has 2.7.0): `--exclude-name`/`--exclude-pattern` (f7df6cb; `selection.py`
detects them and falls back to the regex otherwise) and `jug status --json`
(2b295c0). Minor, also for Jug: formatting a jugfile traceback raises a
linecache `DeprecationWarning` (Jug's jugfile module has no `__spec__`);
`jugstate.py` suppresses it.

## Milestones and steps

### Milestone 0: project skeleton — done, except CI

Package, `uv.lock`, license, CI workflow, `jug-hpc --version`/`--help`.

**Done when:** CI is green on every supported version. Not yet checked:
nothing has been pushed to `origin`, so CI has never run.

### Milestone 1: configuration and task classification (`config`, `check`) — done

### Milestone 2: PBS backend and static submission (`submit --dry-run`, `submit --all`)

Done in code: the scheduler interface and registry, runner, worker script,
PBS and local backends, `.jug_hpc/` state, `submit --dry-run|--all`, and a
fake `qsub`. Left: the manual test on a real PBS cluster. Things to check
there: that a directory is accepted for `-o`/`-e`, that single-element arrays
are avoided correctly, and the `qsub` output format.

**Done when:** `--dry-run` output can be submitted by hand on a real PBS
cluster and runs a one-class jugfile to completion. → **Release 0.1.**

### Milestone 3: the polling submitter

Steps 1–5 are done (task counts per class, `query()`, the submission rule,
the exit condition, `jug-hpc submit` and `--as-job`), and most of step 7.
Decisions taken there that later steps depend on:

- **The submitter loads the jugfile in a subprocess** on each poll
  (`jugstate.subprocess_task_counts`). Loading in-process keeps the modules
  the jugfile imports in `sys.modules`, so changes to them are not seen, and
  tasks they create at import time are not created again on the next load,
  which breaks the dependency graph
  (`tests/test_jugstate.py::test_tasks_from_imported_module`). A subprocess
  also keeps the jugfile's memory and crashes out of the long-running
  submitter.
- `query()` returns a `JobStatus` per job: its state plus the number of its
  workers queued and running, because an array whose elements end at
  different times must count only its remaining workers.
- A job reported unknown within `SUBMIT_GRACE` (300 s) of its submission
  counts all its workers as queued, since a scheduler may not list a job it
  has just accepted.
- Workers still queued or running don't delay the exit. A dead worker's lock
  keeps its task "running" forever, so the submitter never exits until
  milestone 4 step 5 handles stale locks.
- The `--as-job` submitter job's resources are a `[submitter]` table in
  `jug_hpc.toml`; nothing is inherited from `[default]`, and `submitter` is a
  reserved class name.

Remaining:

6. Poll interval: configurable (done, `--interval`, default 60 s), with
   backoff when nothing changes. Use Jug's status cache so a poll stays cheap
   for large graphs; measure on a jugfile with 100k tasks (each poll runs a
   subprocess that loads the jugfile).
7. End-to-end tests with the local backend: still missing a three-class
   chain (the A→B→A chain, a `barrier()`, workers stopped by
   `JUG_MAX_SECONDS` and a failed task are covered in `tests/test_poll.py`).
8. Open questions:
   - A submitter job that reaches its wall time is killed (workers keep
     running; the user submits again). It could resubmit itself before then;
     it would have to release the lock first, and the new job wait briefly
     for it.
   - Should `submit --all` refuse to run while the submitter lock is held?
     (It does not take the lock now.)
   - Show the jugfile's own output (`PollResult.output`) on the first poll?
9. To check on a real PBS cluster (with milestone 2's test):
   - record real `qstat -f -F json -x` output (plain job and array; queued,
     running and finished; an unknown ID) into `tests/pbs_output/`, replacing
     `qstat-f-json.json`, which is modelled on OpenPBS 23, not recorded. Check
     that the JSON is valid (older PBS versions had escaping bugs; parsing
     already uses `strict=False`), the attribute names, and qstat's exit
     status and messages for unknown jobs;
   - plain `jug-hpc submit` and `--as-job`: that compute nodes may run
     `qsub`/`qstat`, and that the job finds the same `cluster.toml` (a
     non-default `XDG_CONFIG_HOME` is not passed to PBS jobs).

**Done when:** the three-class jugfile from the success criteria runs to
completion on the local backend and on a real PBS cluster from a single
`jug-hpc submit`, and no worker waits more than a few minutes for
dependencies. → **Release 0.2.**

### Milestone 4: status, cancel and failure handling

1. `jug-hpc status`: Jug's task counts grouped by class, plus queued/running
   workers per class.
2. `jug-hpc cancel`: cancel every job recorded for the project, including the
   submitter job. (The fake PBS has no `qdel`/`qrls` yet: held jobs stay
   held.)
3. `outcome()` in the PBS backend: exit status of finished jobs, classified
   as success / failed / killed for memory / killed for wall time / other
   signal. The local backend gives an array its worst worker's outcome
   (`local.combine_outcomes`); PBS may want the same rule, moved to
   `base.py`.
4. Report jobs killed by the scheduler, broken down by class, in `status` and
   in the submitter's log, with a suggestion (raise `memory`, raise `time`, or
   lower `time_fraction`).
5. Stale locks: when a job is known to be dead, find the locks its tasks left
   behind. Report them, and offer `jug-hpc status --release-stale` (wrapping
   Jug's lock removal) rather than removing them silently. Recommend the
   `file_keepalive` backend in the docs and in `check`.
6. Warn in `check` when the jugdir backend can't detect dead workers (plain
   directory, redis) and recommend `file_keepalive:`; decide, with step 5,
   whether it is a note instead, as it would show for most projects.
   (`dict_store`, which workers cannot share at all, is already a warning in
   `check` and an error in `submit`.)
7. When the submitter stops because of failed tasks, cancel the workers left
   waiting for tasks that depend on them: each waits `NR_WAIT_CYCLES ×
   WAIT_CYCLE_TIME` (3 min) before exiting, holding its allocation.

**Done when:** a test jugfile with a task that exceeds its memory and one that
exceeds its wall time produces a correct per-class report on PBS. →
**Release 0.3.**

### Milestone 5: second scheduler and 1.0

1. `schedulers/slurm.py`: `sbatch`, `squeue`, `sacct`, `scancel`, raw
   `[slurm]` options as in GOALS.md. Any change it needs in `base.py` is made
   now, before the interface is frozen.
2. Document how to write a backend (the interface, the shared template, the
   entry point), using PBS and SLURM as examples.
3. Jug changes released, minimum Jug version raised, internal-function
   fallbacks removed.
4. User documentation: tutorial, configuration reference (generated from the
   schema), the dry-run workflow, FAQ (CPU propagation, choosing
   `time_fraction`, `file_keepalive`). Until then, the README could get a
   short section on `config`/`check`/`submit` and the two TOML files.
5. Check the success criteria from GOALS.md on a real PBS cluster and a real
   SLURM cluster.

**Done when:** all success criteria hold on both schedulers. → **Release 1.0.**

### Smaller loose ends (any time)

- Old local-backend job directories (`$XDG_STATE_HOME/jug_hpc/local/<id>/`)
  are never cleaned up; maybe a `purge` subcommand.
- `.jug_hpc/scripts/` grows forever (each `--dry-run` adds scripts).

### After 1.0 (ordered by expected value)

- Named cluster profiles (`--cluster <name>`, `JUG_HPC_CLUSTER`) for home
  directories shared between clusters.
- Recording task durations per class, and warning when a typical task is
  longer than the margin `(1 - time_fraction) × time`.
- Automatic retry with more resources after a memory or wall-time kill
  (opt-in, with an upper bound).
- Scheduler dependencies (strategy 1) as an optimization for simple chains,
  avoiding the long-running submitter.
- More backends: Torque, SGE, LSF.

## Answers to the open questions in GOALS.md

| Question | Answer in this plan |
|---|---|
| Submission strategy | Polling submitter (milestone 3); scheduler dependencies after 1.0 |
| Classes for tasks after `barrier()` | Classes are name patterns, and the submitter reloads the jugfile on each poll, so new tasks are classified when they appear; covered by a test in milestone 3 |
| Resources by input size | Out of scope for 1.0. Users can split such tasks into two `TaskGenerator`s with different names; documented in the FAQ |
| Shared home directories | Named profiles, after 1.0 |
| Learning task durations | Warnings only, after 1.0 |

## Risks

- **Polling cost on large graphs.** Loading a jugfile with many tasks on every
  poll may be slow. Mitigation: Jug's status cache, backoff, and measuring
  early (milestone 3, step 6).
- **PBS variants.** PBS Pro, OpenPBS and Torque differ in arrays, `qstat`
  output and exit codes. Mitigation: target OpenPBS/PBS Pro only, keep all
  parsing in pure functions with recorded fixtures from each site we test on.
- **Login-node policies.** Some sites kill long-running processes on login
  nodes. Mitigation: `submit --as-job`.
- **Access to test clusters.** CI cannot run a real scheduler. Mitigation: the
  local backend and fake `qsub`/`qstat` cover CI; each release is checked by
  hand on at least one real PBS cluster (and a SLURM cluster from 1.0 on).
