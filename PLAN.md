# jug_hpc: implementation plan

This plan turns [GOALS.md](GOALS.md) into ordered steps and milestones. Where
GOALS.md lists an open question, the plan gives a recommended answer and says
which step depends on it.

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
  only way to prove that the backend interface is easy to replace. GOALS.md
  has been updated to match.
- **Tooling.** Same as Jug: `uv` with `uv.lock`, `pytest`, a GitHub Actions
  matrix over every supported Python version, and the MIT license.
- **Submission strategy (main open question).** Recommendation: **a polling
  submitter** (strategy 2 in GOALS.md) as the only strategy for 1.0.
  - It is the only strategy that handles every task graph, including classes
    that alternate and graphs that grow after a `barrier()`.
  - Scheduler dependencies (strategy 1) fail silently on exactly the graphs
    that need jug_hpc most, and the hybrid needs both implementations.
  - The cost is a long-running process. This is kept small: one process per
    project, sleeping most of the time, and runnable either on a login node or
    as a small scheduler job (`jug-hpc submit --as-job`).
  - Strategy 1 can still be added after 1.0 as an optimization, behind the
    same interface.

  Milestones 0–2 don't depend on this choice, so it can be revisited until
  milestone 3 starts.

## Architecture

```
jug_hpc/
  config/        load, merge and validate cluster.toml and jug_hpc.toml
  units.py       memory and time parsing (canonical: bytes, seconds)
  classes.py     resource classes, task -> class mapping (uses Jug's matchers)
  jugstate.py    the only module that imports Jug internals: load the jugfile,
                 count ready/running/waiting/failed tasks per class
  worker.py      WorkerSpec: everything one worker needs, scheduler-independent
  schedulers/
    base.py      Scheduler interface, JobState/JobOutcome types, registry
    pbs.py       PBS backend (OpenPBS / PBS Pro)
    local.py     runs workers as local subprocesses (tests, laptops)
    slurm.py     (milestone 5)
  runner.py      runs external commands; replaced by a fake in tests
  state.py       per-project record of submitted jobs (.jug_hpc/ directory)
  submitter.py   the polling loop
  cli.py         jug-hpc config | check | submit | status | cancel
```

Two rules keep the design testable and the schedulers replaceable:

1. **Everything scheduler-specific lives in one backend module.** The rest of
   jug_hpc only builds `WorkerSpec`s and calls the interface.
2. **Backends separate pure functions from side effects.** Rendering a
   script, building a command line and parsing scheduler output are pure
   functions, tested against recorded output. Only `runner.py` executes
   anything.

### Scheduler interface (first draft)

```python
class Scheduler(ABC):
    name: ClassVar[str]                      # "pbs"; also the name of the raw-options table

    def __init__(self, cluster: ClusterSettings, runner: Runner): ...

    # pure
    def render_script(self, spec: WorkerSpec) -> str
    def submit_command(self, script: Path, spec: WorkerSpec) -> list[str]
    def parse_submit_output(self, stdout: str) -> JobId
    def validate_raw_options(self, raw: dict) -> None   # shape only, values are passed through

    # side effects (through self.runner)
    def submit(self, script: Path, spec: WorkerSpec) -> JobId
    def query(self, jobs: list[JobId]) -> dict[JobId, JobState]      # queued/running/done
    def outcome(self, jobs: list[JobId]) -> dict[JobId, JobOutcome]  # exit code, killed for mem/time
    def cancel(self, jobs: list[JobId]) -> None
```

`WorkerSpec` holds the class name, the portable resources in canonical units
(cpus, memory in bytes, wall time in seconds), the number of array elements,
the partition, the merged raw options for this backend, the setup snippets,
the log path template, and the `jug execute` command line. Backends are found
through the `jug_hpc.schedulers` entry point group, so a new scheduler can be
added in a separate package without changing jug_hpc.

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
exec jug execute --name=<selector> --nr-wait-cycles=15 --wait-cycle-time=12 \
     --jugdir=<jugdir> -- <jugfile>
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
- Queue state: `qstat -f -F json` (and `qstat -x` for finished jobs).
  Torque's `qstat` is different; Torque support is out of scope for 1.0 but
  must be possible as a separate backend.
- Job outcome: PBS `Exit_status`, where negative values are PBS's own codes
  (including walltime and memory kills) and values above 256 mean "killed by
  signal". The exact codes are taken from the PBS documentation and checked
  on a real cluster before being relied on.

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

### Milestone 0: project skeleton

1. `pyproject.toml` (package `jug_hpc`, console script `jug-hpc`,
   `requires-python = ">=3.11"`, dependency `jug>=2.7`), `uv.lock`, MIT
   license, README stub.
2. GitHub Actions: pytest on Python 3.11–3.15, plus a lint step.
3. `jug-hpc --version` and `--help` work.

**Done when:** CI is green on every supported version.

### Milestone 1: configuration and task classification (`config`, `check`)

1. `units.py`: parse memory (`512M`, `16G`, `16GB`, `1.5T`) to bytes and time
   (`MM`, `HH:MM:SS`, `D-HH:MM:SS`, plus `30m`/`12h`) to seconds, with clear
   errors. Property tests for round-tripping.
2. Cluster settings: XDG lookup, recursive key-by-key merge of the user file
   and the project's `cluster.toml`, provenance tracking (which file each
   value came from), and an error naming both paths if neither exists.
3. Project settings: `[default]`, `[class.*]` with inheritance from
   `[default]`, `match` or `pattern` (exactly one), `time_fraction` in (0, 1],
   positive `workers`. Cluster-only keys in `jug_hpc.toml` are an error.
   Unknown keys are errors (catches typos); raw scheduler tables are not
   checked beyond their shape.
4. `jug-hpc config`: print the merged settings with provenance.
5. `classes.py`: map each task name to a class with Jug's own matchers
   (`jug.utils.prepare_name_matcher` / `prepare_task_matcher`). Tasks matched
   by no class go to `default`; tasks matched by two classes are an error.
6. `jugstate.py`: load the jugfile with `jug.jug.init` and list its tasks.
7. `jug-hpc check`: tasks per class, overlaps (error), classes that match
   nothing (warning), and a note when the task list stops at a `barrier()`.

**Done when:** `jug-hpc check` gives correct results on Jug's example
jugfiles and on test jugfiles with overlapping classes and barriers.

### Milestone 2: PBS backend and static submission (`submit --dry-run`, `submit --all`)

1. `schedulers/base.py`: the interface above, `WorkerSpec`, `JobState`,
   `JobOutcome`, entry-point registry, and a clear error for an unknown
   `scheduler`.
2. `runner.py` with a real and a fake implementation.
3. Shared worker script template (setup, `JUG_MAX_*`, CPU variables, logs,
   `jug execute` line). Unit tests check the `floor`, the unsetting of
   `JUG_MAX_HOURS`/`JUG_MAX_MINUTES`, and the quoting of every value.
4. `schedulers/pbs.py`: script header, `qsub` command, parsing of `qsub`
   output, raw options (`select`/`resources`/`directives`), arrays.
   Golden-file tests of the rendered scripts.
5. `schedulers/local.py`: runs each worker as a subprocess with the same
   script. This allows end-to-end tests in CI without a cluster.
6. `state.py`: `.jug_hpc/jobs.json` (or SQLite) recording job ID, class,
   array size, script path and submission time. Scripts are kept in
   `.jug_hpc/scripts/` so a user can resubmit by hand.
7. `jug-hpc submit --dry-run`: write every script and print it with the exact
   `qsub` command, without submitting.
8. `jug-hpc submit --all`: submit every class at once, sized from the
   configuration. This is the "naive" mode: it works for flat graphs and is
   useful before the submitter exists.
9. Tests against a fake `qsub`/`qstat` (shell scripts on `PATH` replaying
   recorded output), plus a manual test on a real PBS cluster.

Notes for the remaining steps:

- Scheduler settings are checked against the installed backends by
  `schedulers/checks.py` (`check_scheduler_settings`), called by `jug-hpc
  config` (done) and to be called by `submit` (raise the first error). An
  unknown `scheduler` and invalid raw tables are errors; every table named
  after an installed backend is checked (so a project's `[class.x.slurm]` is
  checked on a PBS cluster when the slurm backend is installed); tables naming
  no installed backend, and cluster.toml tables for a backend other than the
  configured one, are warnings (ignored). `validate_raw_options` is a
  classmethod, so project tables are checked without cluster settings.
  `check` stays independent of the cluster settings.
- PBS backend (`schedulers/pbs.py`, done; its docstring has the details): raw
  `select`/`resources` tables may not set `ncpus`/`mem`/`walltime`/`select`
  (those come from `cpus`/`memory`/`time`); `directives` entries get `#PBS `
  prepended unless present. Job ID in the script is `${PBS_JOBID%%[.[]*}` (`1234`
  from both `1234.server` and `1234[2].server`), index `$PBS_ARRAY_INDEX`.
  `-o`/`-e` are `logs/scheduler/` relative to the project dir (qsub resolves
  them against its cwd, which is the project dir), so spaces in the project
  path don't matter. To check on a real cluster: that a directory is accepted
  for `-o`/`-e`, that single-element arrays are avoided correctly, and the
  `qsub` output format. `query`/`outcome`/`cancel` raise NotImplementedError
  (milestones 3–4).
- `submit` must create `logs/` and `logs/scheduler/` before `qsub` (the
  script's `mkdir -p` is too late for PBS's own `-o`/`-e`).
- `submit` should pass the jugfile's task names to `selector_args`, so the
  fallback exclusion regex for the default class is checked against the
  classifier.
- `submit` should reject `dict_store` and `dict_store:<file>` jugdirs (they
  can't be shared by concurrent workers); `check` should warn about them
  (see milestone 4, step 6).

**Done when:** `--dry-run` output can be submitted by hand on a real PBS
cluster and runs a one-class jugfile to completion. → **Release 0.1.**

### Milestone 3: the polling submitter

1. `jugstate.py`: per-class counts of ready/running/waiting/failed tasks
   (through the Jug API from "Changes needed in Jug", or the internal
   functions until it exists). Reload the jugfile on each poll, so tasks that
   appear after a `barrier()` are seen. Note: modules the jugfile imports
   (e.g. a `helper.py` next to it) stay cached in `sys.modules`, so changes
   to them are not seen on reload unless loading happens in a subprocess;
   decide which. `Task.store` is restored after loading, so use
   `t.can_load(state.store)`.
2. `query()` in the PBS backend: queued/running/finished per job and array
   element, from `qstat -f -F json`.
3. The submission rule, per class, at each poll:
   `wanted = min(ready, workers)`, `active = queued + running workers`,
   submit `wanted - active` new workers if positive. Running tasks hold a
   lock, so they aren't counted as ready.
4. Exit when every task is finished, or when nothing is ready or running and
   only failed tasks remain (with a report).
5. `jug-hpc submit` runs the loop in the foreground; `--as-job` submits the
   loop itself as a small, long job (1 CPU, little memory). Only one
   submitter per project is allowed (a lock in `.jug_hpc/`).
6. Poll interval: configurable, default about one minute, with backoff when
   nothing changes. Use Jug's status cache so a poll stays cheap for large
   graphs; measure on a jugfile with 100k tasks.
7. End-to-end tests with the local backend: a three-class chain, alternating
   classes (A→B→A), a graph with a `barrier()`, and workers that stop early
   because of `JUG_MAX_SECONDS` (the submitter must replace them).

**Done when:** the three-class jugfile from the success criteria runs to
completion on the local backend and on a real PBS cluster from a single
`jug-hpc submit`, and no worker waits more than a few minutes for
dependencies. → **Release 0.2.**

### Milestone 4: status, cancel and failure handling

1. `jug-hpc status`: Jug's task counts grouped by class, plus queued/running
   workers per class.
2. `jug-hpc cancel`: cancel every job recorded for the project, including the
   submitter job.
3. `outcome()` in the PBS backend: exit status of finished jobs, classified
   as success / failed / killed for memory / killed for wall time / other
   signal.
4. Report jobs killed by the scheduler, broken down by class, in `status` and
   in the submitter's log, with a suggestion (raise `memory`, raise `time`, or
   lower `time_fraction`).
5. Stale locks: when a job is known to be dead, find the locks its tasks left
   behind. Report them, and offer `jug-hpc status --release-stale` (wrapping
   Jug's lock removal) rather than removing them silently. Recommend the
   `file_keepalive` backend in the docs and in `check`.
6. Warn in `check` when the jugdir backend can't detect dead workers.

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
   `time_fraction`, `file_keepalive`).
5. Check the success criteria from GOALS.md on a real PBS cluster and a real
   SLURM cluster.

**Done when:** all success criteria hold on both schedulers. → **Release 1.0.**

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
