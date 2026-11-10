# jug_hpc: implementation plan

This plan turns [GOALS.md](GOALS.md) into ordered steps and milestones. Finished
work is summarized only as far as later steps depend on it; the details are in
CLAUDE.md (where things live in the code), the module docstrings, the user
documentation and `git log`.

## Decisions taken up front

- **Python versions.** jug_hpc requires Python 3.11 or later, one version
  narrower than Jug (3.10+), so that TOML is read with `tomllib`. Otherwise
  jug_hpc follows Jug: when Jug drops or adds a version, jug_hpc does the same.
- **Minimum Jug version.** `jug >= 2.7` (the first release with `--name` and
  `--pattern`). The changes listed under "Changes needed in Jug" will raise it
  before jug_hpc 1.0.
- **Scheduler order.** PBS first (OpenPBS / PBS Professional), then SLURM
  before 1.0, because a second real backend is the only way to prove that the
  backend interface is easy to replace.
- **Submission strategy.** A polling submitter (strategy 2 in GOALS.md) is the
  only strategy for 1.0.
  - It is the only strategy that handles every task graph, including classes
    that alternate and graphs that grow after a `barrier()`.
  - Scheduler dependencies (strategy 1) fail silently on exactly the graphs
    that need jug_hpc most, and the hybrid needs both implementations.
  - The cost is a long-running process, kept small: one per project, sleeping
    most of the time, on a login node or as a small job (`submit --as-job`).
  - Strategy 1 can still be added after 1.0 as an optimization.

## Scheduler interface

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

PBS details (exit codes, substates, the memory and wall-time heuristics, what
was seen on PBS Professional 2026.1) are in `pbs.py`'s docstring. Torque's
`qstat` differs; Torque is out of scope for 1.0 but must be possible as a
separate backend.

## Changes needed in Jug

jug_hpc must not reimplement Jug (principle 2), but needs:

1. **Running the default class**: `jug execute --exclude-name X` (repeatable)
   and `--exclude-pattern`. Without them, `selection.py` falls back to a
   generated negative-lookahead `--pattern`, which is fragile.
2. **Task counts per state**: a public API for what `jug status` computes
   (`update_status`), and `jug status --json`. Until then `jugstate.py` calls
   Jug's internal functions.

Both are on `../jug`'s main branch but not released (PyPI has 2.7.0):
`--exclude-*` (f7df6cb) and `jug status --json` (2b295c0). Also unreleased
there (74d9756): Jug 2.7.0's `hash_update` runs `import numpy` on every call,
which without numpy installed made loading a 100k-task jugfile take 11–14 s
instead of about 1.3 s (`jug execute`/`jug status` too). Minor: formatting a
jugfile traceback raises a linecache `DeprecationWarning` (Jug's jugfile module
has no `__spec__`); `jugstate.py` suppresses it.

Candidate Jug changes for after 1.0 (see "Smart limits"): `jug execute
--task-log FILE` (per-task time and memory) and selecting tasks by hash.

## Milestones

Milestones 0–4 are done, each checked on a real PBS cluster (PBS Professional
2026.1, with `tests/manual/pbs/run-everything.sh`, rerun by hand for each
release): 0, skeleton and CI; 1, configuration and `check`; 2, the PBS backend
and static submission (release 0.1); 3, the polling submitter (release 0.2);
4, `status`, `cancel` and failure handling (release 0.3).

### Decisions that constrain later work

- **The submitter loads the jugfile in a subprocess** on each poll. In-process,
  the modules the jugfile imports stay in `sys.modules`, so changes to them are
  not seen, and tasks they create at import time are not created again, which
  breaks the dependency graph
  (`tests/test_jugstate.py::test_tasks_from_imported_module`). A subprocess
  also keeps the jugfile's memory and crashes out of the submitter.
- **Jug's status cache is not used.** It is fast only because it doesn't load
  the jugfile again, so it would miss tasks created after a `barrier()` and
  edits to the jugfile. The cost of a poll is hashing the tasks (about 85% of
  2 s and 150 MB per poll for 100k tasks), which needs the jugfile loaded. If
  polls get too slow, cache hashes keyed by something cheaper (not planned).
- **Locks are not tied to jobs**: Jug's lock files hold `PID ... on HOSTNAME`,
  no job ID. Stale locks are therefore found per class (locked tasks, no queued
  or running worker), and the submitter only warns: it can't tell a dead
  worker's lock from a `jug execute` run by hand. `status --release-stale`
  releases them on request. Likewise, failed tasks are tied to worker logs by
  parsing Jug's log text (`failures.py`), which is fragile. Per-task logs
  (Smart limits, phase A) would fix both.

### Deferred, or open

Add when users ask:

- `cancel --force` for a submitter running outside the scheduler;
  `--keep-workers` (don't cancel workers when a run ends `BLOCKED`/`STALLED`);
  `--max-failed-jobs`; per-class selection for `--cleanup-failed`.

Open:

- A submitter job that reaches its wall time is killed (workers keep running;
  the user submits again). It could resubmit itself first, releasing the lock
  and letting the new job wait briefly for it.
- Should `submit --all` refuse to run while the submitter lock is held? (It
  does not take the lock now.)
- Show the jugfile's own output (`PollResult.output`) on the first poll?
- A job that ran tasks and was then killed for wall time counts towards
  `MAX_FAILED_JOBS` if no task of the class ended between the poll before and
  the one that sees it end. Counting only jobs that ran no task needs per-job
  task counts. Kept as is for now (decided 2026-11-05).
- Kills of an array are reported once, by its worst worker; per-worker counts
  need `outcome()` to return each worker's outcome.
- Outdated kills and failures are detected only by recorded resources
  (`state.JobResources`), not by changes to `setup`.
- A SIGTERM or Ctrl-C between `submit` and `add_job` loses the job's record.
- Not done: reading the scheduler's error file (e.g. PBS `.ER`, where the
  cgroup hook writes `Killed because of memory limit`); it is backend- and
  site-specific.
- To do: a step in `tests/manual/pbs/` with a task whose subprocess goes over
  its memory, to record that `qstat` output at the next release check.

### Milestone 5: second scheduler and 1.0

1. `schedulers/slurm.py`: `sbatch`, `squeue`, `sacct`, `scancel`, raw
   `[slurm]` options as in GOALS.md. Any change it needs in `base.py` is made
   now, before the interface is frozen. Its raw keys go into
   `tests/test_docs.py`.
2. Document how to write a backend (the interface, the shared template, the
   entry point), using PBS and SLURM as examples.
3. Jug changes released, minimum Jug version raised, internal-function
   fallbacks removed.
4. User documentation (`docs/`). Written: tutorial, concepts, configuration
   reference, submitting (including `status`/`cancel`), schedulers (PBS,
   local), FAQ. The key tables are hand-written, not generated (their wording
   is richer than the code's descriptions, and `llms.md` is plain Markdown),
   and checked by `tests/test_docs.py`. Left: SLURM, the backend guide (step
   2), setting up the Read the Docs project. Keep the "Current limitations"
   section of `docs/submitting.md` in step with what is implemented.
5. Check the success criteria from GOALS.md on a real PBS cluster and a real
   SLURM cluster.

**Done when:** all success criteria hold on both schedulers. → **Release 1.0.**

### Smaller loose ends (any time)

- Old local-backend job directories (`$XDG_STATE_HOME/jug_hpc/local/<id>/`)
  are never cleaned up; maybe a `purge` subcommand.
- `.jug_hpc/scripts/` grows forever (each `--dry-run` adds scripts).

### After 1.0 (ordered by expected value)

- Named cluster profiles (`--cluster <name>`, `JUG_HPC_CLUSTER`) for home
  directories shared between clusters (or choosing a file from the hostname).
- Smart limits, phases A and B (below): recording task durations and memory
  per class, warning when a typical task is longer than the margin
  `(1 - time_fraction) × time`, and `jug-hpc suggest`.
- Smart limits, phase C: adapting resources automatically, including retry
  with more resources after a memory or wall-time kill (opt-in, with an
  upper bound).
- Scheduler dependencies (strategy 1) as an optimization for simple chains,
  avoiding the long-running submitter.
- More backends: Torque, SGE, LSF.

### Smart limits (after 1.0; brainstorm, nothing decided)

Use measurements from the first tasks of a class to size its later
workers. Built in three phases, each useful on its own and each validated on
real data before the next: **record → suggest → act**.

Ideas (roughly by expected value):

- Pilot batch: submit a few workers first, more once there is an estimate
  of time per task (`workers = ceil(remaining × mean task time / target
  makespan)`, capped by `workers`).
- Memory down: after N tasks, `memory = p95(peak) × headroom`, rounded to
  coarse steps (smaller requests start sooner on most clusters).
- Memory up after a kill: retry with `memory × factor`, up to a maximum.
- Wall time from task durations: size `time` for about K tasks per worker,
  between a minimum and a maximum (short jobs backfill better); maybe aim
  just under a queue's limit.
- `time_fraction = "auto"`: `JUG_MAX_SECONDS = time − p99(task time) ×
  safety`, instead of a fixed fraction.
- CPU efficiency: `cput / (walltime × ncpus)` per class; warn (or lower
  `cpus`) when tasks use far fewer CPUs than requested. Never raise it
  automatically (tasks must thread themselves).
- Startup overhead (job start to first task: setup, jugfile load): when it
  is large compared with task time, prefer fewer, longer workers.
- Leaks: RSS at the *start* of each task rising across a worker's tasks →
  suggest `--aggressive-unload` or shorter workers.
- Queue wait (`stime − qtime` per job shape, PBS `estimated.start_time`,
  `squeue --start`): weigh smaller shapes against longer waits.
- Lookahead: submit a class's workers before its tasks are ready when the
  predicted time until they are (from the dependency indices in
  `jugstate`) is about the queue wait.
- Cancel queued workers no longer needed (ready tasks fell below queued
  workers); needs no measurement, and the poll can already cancel
  (`PollResult.cancelled`).
- Tail: when few tasks are left, submit workers with `time` for one or two
  tasks. (No speculative execution: Jug's locks forbid running a task twice.)
- Stats across runs, per task name, in `.jug_hpc/`, so a rerun after
  `invalidate` starts with priors (weight recent runs more: code changes).
- Report uneven classes (bimodal or wide durations or memory; suggest one
  class per task name when a class covers several).
- Budget: `max_core_hours` per project or class; the submitter stops at it,
  `status` shows what was spent.

API sketch:

```toml
[default.adapt]                 # inherited from [default] like any field
mode = "suggest"                # "off" | "suggest" (log, status) | "auto"

[class.align]
match   = "align_*"
memory  = "16G"                 # the starting value, as now
time    = "4:00:00"
workers = 128                   # still the hard cap

[class.align.adapt]
mode           = "auto"
pilot          = 8              # workers before there is an estimate
memory         = { fit = 0.95, headroom = 1.3, min = "2G", max = "128G", after = 20 }
memory_on_kill = { factor = 2, max = "128G" }
time           = { tasks_per_worker = 50, min = "30:00", max = "24:00:00" }
time_fraction  = "auto"
```

- Plain keys keep their meaning (the starting value); adaptation lives in a
  subtable, so validation, provenance in `jug-hpc config` and inheritance
  are unchanged, and deleting the table turns it off.
- Values use the unit strings of `units.py` (`"2G"`, not `min_gb`).
- Reacting to kills (`memory_on_kill`) is separate from fitting to
  measurements (`memory`): different triggers, different risks.
- `adapt` must become a reserved subtable name (as `submitter` is a reserved
  class name), as subtables are otherwise scheduler raw options.
- `jug-hpc suggest`: prints a TOML diff for `jug_hpc.toml`, with reasons.

Phase A, record:

- Per worker, from the scheduler: a `usage` field on `JobOutcome`
  (`walltime`, `cput`, `mem_peak`, `queue_wait`). PBS already returns
  `resources_used` (`mem`, `cput`, `walltime`, `cpupercent`) and
  `qtime`/`stime` in the `qstat -x` call `outcome()` makes; SLURM: `sacct`;
  local: `os.wait4` rusage (so end-to-end tests cover it). Usage per array
  element, not combined like outcomes.
- Per task: needs Jug's `execute.task-pre-execute`/`task-executed1` hooks,
  which the jugfile must not register (principle 1). Either (a) a Jug
  option, `jug execute --task-log FILE` (preferred: the script stays a plain
  `jug execute`), or (b) a launcher (`python -m jug_hpc.probe execute ...`)
  that registers the hooks and calls Jug's main, as fallback for older Jug
  (as `selection.py` does for `--exclude-name`).
- One append-only JSON-lines file per worker (`.jug_hpc/usage/<jobid>.jsonl`,
  no locking): a `start` line (hash, name, time, RSS) *before* each task, an
  `end` line (duration, peak) after it. The last unmatched `start` of a
  killed worker is the task that was running: the culprit of an OOM kill,
  and the lock it left behind. This ties locks (and failures) to jobs, so
  stale locks could be found per dead job, not only per class without
  workers.
- Per-task memory peak: `ru_maxrss` only grows; on Linux, writing `5` to
  `/proc/self/clear_refs` before each task resets `VmHWM`. The cgroup peak
  (what the scheduler enforces) from the per-worker usage stays the
  deciding number.

Phase B, suggest:

- `estimate.py` (pure): records → per class and per name: n, median, p95,
  max of duration and memory, startup overhead, throughput. Explicit
  small-sample rules (below `after` samples: max × headroom, or nothing).
  Property tests with hypothesis.
- A column in `status`, warnings in the submitter's log (e.g. task time
  longer than the `time_fraction` margin), and `jug-hpc suggest`.

Phase C, act:

- `adapt.py` (pure): `(ClassSettings, Estimates, AdaptConfig) → (WorkerSpec,
  reasons)`, called from `submit.prepare_class`, so the kept script shows
  the values used and each change is logged with its reason.
- Hysteresis: round to steps (memory ~1.25×, time 15 min) and change only
  when the estimate moves past a threshold, so scripts and logs stay stable.
- `jobs.json` already records each job's resources (`state.JobResources`),
  but `kills.suggestion` (in the submitter's log) compares against the class
  as configured *now*, which is wrong once sizes change by themselves.
- Retry after a kill needs the killed task's lock released. Stale locks are
  released only by hand now (`status --release-stale`), as the submitter
  can't tell a dead worker's lock from a `jug execute` outside jug-hpc; the
  per-task log names the exact lock a killed job held, so the submitter
  could release just that one (`jugstate.release_locks`). With
  `file_keepalive`, the lock becomes *failed* after 30 minutes and the run
  ends `BLOCKED`: retry must release it before then, or clear the failure.
- `MAX_FAILED_JOBS`: a memory kill moves the class up a size instead of
  counting as a failure, until `max`; then the current rule applies.
- Escalation is class-wide: giving only the killed task a bigger worker
  needs Jug to select tasks by hash (e.g. `jug execute --hashes FILE`);
  `--name`/`--pattern` match names only. Another candidate Jug change.
- Tests: a jugfile whose tasks allocate X MB and sleep T s; the local
  backend reporting rusage; `fake_pbs/qstat` writing `resources_used`
  (format as in `tests/pbs_output/pbspro-2026.1/`).

## Risks

- **Polling cost on large graphs.** Measured: about 2 s per poll for 100k
  tasks; mitigated by backoff (`--max-interval`).
- **PBS variants.** PBS Pro, OpenPBS and Torque differ in arrays, `qstat`
  output and exit codes. Mitigation: target OpenPBS/PBS Pro only, keep all
  parsing in pure functions with recorded fixtures from each site we test on.
- **Login-node policies.** Some sites kill long-running processes on login
  nodes. Mitigation: `submit --as-job`.
- **Access to test clusters.** CI cannot run a real scheduler. Mitigation: the
  local backend and fake `qsub`/`qstat` cover CI; each release is checked by
  hand on at least one real PBS cluster (and a SLURM cluster from 1.0 on).
