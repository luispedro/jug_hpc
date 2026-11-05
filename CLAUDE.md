# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`jug_hpc` runs [Jug](https://jug.readthedocs.io) jugfiles on HPC clusters, submitting
workers sized per *resource class* of tasks (selected by Jug task name). Early development:
read `GOALS.md` (design, principles) and `PLAN.md` (architecture, milestones, decisions)
before making design-level changes. PLAN.md is the source of truth for what comes next.

Current state: milestones 0–1 are done; milestone 2 is done in code (`schedulers/`,
`runner.py`, `worker.py`, `script.py`, `selection.py`, `state.py`, `submit.py`,
`jug-hpc submit --all|--dry-run`; also checked on a real PBS cluster).
Milestone 3 is done (also checked on a real PBS cluster): task counts per class
(`jugstate.task_counts`, `classes.counts_by_class`, and `jugstate.subprocess_task_counts`
for loading in a subprocess), `Scheduler.query()` for PBS and local (a `JobStatus` per job, with
queued/running worker counts), the submission rule and exit condition (pure functions in
`submitter.py`), and the polling loop (`poll.py`, plain `jug-hpc submit`, with a
per-project lock), with end-to-end tests on the local backend (chain, three-class chain,
barrier, workers stopped by `JUG_MAX_SECONDS`, failed task), and `submit --as-job` (the
submitter as a job, sized by `[submitter]` in `jug_hpc.toml`), and backoff while nothing
changes (`--max-interval`). Jug's status cache is deliberately not used (PLAN.md).
Milestone 4 has started: `Scheduler.outcome()` for PBS and local (a `JobOutcome` per
finished job; arrays combined by `schedulers.base.combine_outcomes`, worst worker wins),
used by the submitter to stop submitting workers for a class whose jobs keep failing
(step 8: `submitter.update_failing`/`stopped_classes`, `Progress.STALLED`), and
to report jobs killed by the scheduler in the submitter's log, with what to change
(step 4, `kills.py`), and `jug-hpc status` (step 1, `status.py`: tasks and workers per
class, the submitter, and the kills of every recorded job), and `jug-hpc cancel`
(step 2, `cancel.py`; PBS `cancel()` with `qdel`).

## Commands

```bash
uv sync                                   # create .venv with jug_hpc and dev tools
uv run pytest                             # all tests
uv run pytest tests/test_script.py::test_name   # a single test
uv run ruff check .                       # lint (line length 100)
uv run jug-hpc --help                     # CLI (also: python -m jug_hpc)
uv run --with-requirements docs/requirements.txt sphinx-build -W docs docs/_build/html  # docs
```

CI (`.github/workflows/test.yml`) runs `uv sync --locked`, ruff, the docs build (`-W`), pytest
on Python 3.11–3.15, and `jug-hpc --version`. Keep `uv.lock` in sync when changing
dependencies. Python ≥ 3.11 is required because TOML is read with `tomllib`; Jug is the only
runtime dependency.

- Dev tools (pytest, ruff, hypothesis) are in `[dependency-groups] dev`, so plain `uv sync`
  installs them. The locked ruff also enforces import sorting (I001) and sorted `__all__`
  (RUF022): run `uv run ruff check --fix .` before committing.
- Other Pythons through uv: `uv run --python 3.11 --isolated pytest`.
- A sibling Jug checkout lives at `../jug` (newer than PyPI's 2.7.0). One test (the
  `--exclude-*` one) is skipped with released Jug; run it with
  `uv run --isolated --with ../jug pytest`. Jug's examples (`../jug/examples`) are useful
  for trying `check` by hand (they need `--with numpy --with requests`).

User documentation lives in `docs/` (Sphinx + MyST Markdown, like `../jug/docs`; Read the
Docs builds it with `.readthedocs.yaml`, failing on warnings). When behaviour that users see
changes (keys, defaults, CLI options, script contents, messages quoted there, limitations),
update the docs in the same commit, including `docs/llms.md` (the dense page for LLMs,
also served as `/llms.txt` by a hook in `docs/conf.py`; plain Markdown, no MyST roles, so it
reads well raw). Example output in the docs comes from real runs, with
paths replaced by `/home/me/rnaseq`.

## Design principles that constrain code

1. **The jugfile stays unchanged.** All scheduling info lives in TOML config; config files
   never run code or import the jugfile.
2. **Don't reimplement Jug.** Workers are plain `jug execute --name/--pattern` processes.
   Task-to-class matching uses Jug's own matchers (`jug.utils.prepare_name_matcher` /
   `prepare_task_matcher`) so a class selects exactly what its workers will run.
3. **Be transparent.** Generated scheduler scripts must be inspectable and submittable by hand.

## Architecture

- **Two config files** (`jug_hpc/config/`):
  - `cluster.toml` (*how* to submit): user file at `$XDG_CONFIG_HOME/jug_hpc/cluster.toml`,
    deep-merged key-by-key with an optional project-dir `cluster.toml`. Provenance (which
    file each leaf came from) is tracked via source maps in `config/toml.py` and shown by
    `jug-hpc config`.
  - `jug_hpc.toml` (*what* the project needs): `[default]` + `[class.<name>]` tables;
    classes inherit unset fields from `[default]`. Cluster-only keys (`scheduler`,
    `account`) are rejected here. Unknown keys are errors (with did-you-mean suggestions).
  - `[submitter]` in `jug_hpc.toml` (`SubmitterSettings`): resources of the `--as-job`
    submitter job; inherits nothing from `[default]`; `submitter` is a reserved class name
    (`config.project.SUBMITTER`), used as the class name of that job everywhere.
  - Subtables named after a scheduler (`[pbs]`, `[class.x.pbs]`) are raw options passed
    through unchecked except for shape; class values win over cluster values
    (`worker._merge_raw`).
  - `ConfigError` messages always name the file and key, so they are shown to users as is.
- **`classes.py`** works only on task *names* (no jugfile loading). Unmatched names → default
  class; a name matched by two classes is an error (so class order never matters).
- **`jugstate.py` is the only module that imports Jug internals** (`jug.jug`, `jug.task`).
  `load_jugfile` resets and then restores Jug's process-wide globals (`alltasks`,
  `Task.store`, `sys.modules`, `sys.path`) so it can be called repeatedly. It also computes
  task hashes and dependency indices while the jugfile module is loaded (hashing can pickle
  jugfile functions); `task_counts` feeds them to Jug's `update_status` for per-name
  `TaskCounts` (waiting/ready/running/failed/finished). Keep Jug-internal access confined
  here. Modules imported by the jugfile stay cached across in-process loads, so the
  submitter must load in a subprocess: `subprocess_task_counts` runs `python -P -m
  jug_hpc.jugstate counts` (through a `Runner`) and parses its JSON.
- **`selection.py`**: the `jug execute` args selecting a class's tasks. The default class
  uses `--exclude-name/--exclude-pattern` when the installed Jug supports them, otherwise a
  generated negative-lookahead `--pattern` regex that is verified against the classifier.
- **`worker.py`** `WorkerSpec`: scheduler-independent description of one worker/array, in
  canonical units (memory in bytes, time in seconds; parsing/formatting in `units.py`).
- **`script.py`**: the shared worker script body (cd, log redirect, CPU env vars, setup
  snippets, `unset JUG_MAX_HOURS JUG_MAX_MINUTES`, `JUG_MAX_SECONDS = floor(time_fraction ×
  time)`, `exec jug execute`). Backends supply only the directive header and `ScriptVars`
  (shell expressions for job ID / array index). All values are `shlex.quote`d. The module
  docstring explains ordering choices (e.g. why `set -e` but not `set -u`).
- **`schedulers/`**: `Scheduler` ABC in `base.py`. Backends are discovered through the
  `jug_hpc.schedulers` entry-point group (name must equal `Scheduler.name`). Two rules:
  everything scheduler-specific lives in one backend module, and backends keep pure functions
  (render script, build command, parse output — tested against recorded output) separate
  from side effects, which go only through a `Runner`.
- **`schedulers/checks.py`**: `check_scheduler_settings(cluster, project)` collects errors
  (unknown `scheduler`, raw tables rejected by their backend's `validate_raw_options`) and
  warnings (tables naming no installed backend). Used by `jug-hpc config`; `check` does not
  use it, as it never reads the cluster settings.
- **`schedulers/local.py`**: runs workers on this machine. Its "scheduler" is the module
  itself (`python -m jug_hpc.schedulers.local submit|status|cancel`), invoked through the
  runner, so the backend follows the same pure/side-effect split as PBS. End-to-end tests
  use it with a `SubprocessRunner` and a `jobs_dir` in `tmp_path`.
- **`runner.py`**: `SubprocessRunner` for real use; `FakeRunner` records calls and replays
  canned output in tests. Backends never call `subprocess` directly.
- **`state.py`**: `ProjectState` for the project's `.jug_hpc/` directory: kept scripts
  (`scripts/<class>-<n>.sh`) and `jobs.json` (submitted jobs, versioned format, atomic
  replace under a `jobs.lock` flock). Raises `StateError` naming the file.
- **`submit.py`**: static submission (`submit --all`/`--dry-run`) in three steps:
  `prepare` (pure: settings checks, classification, one `PreparedWorker` per class with
  tasks, workers capped at the task count unless the jugfile hit a barrier),
  `write_scripts` (saves scripts, creates `logs/scheduler/`), `submit_worker` (submit +
  record). Submit commands name the script relative to the project dir (their cwd), so
  `--dry-run` prints exactly what `--all` runs.
- **`submitter.py`**: the polling submitter's decisions, pure (no I/O): which jobs to
  query, active workers per class, `workers_to_submit` (`min(ready, workers) - active`,
  skipping stopped classes), `update_failing`/`stopped_classes` (a class whose last
  `MAX_FAILED_JOBS` jobs ended in error with no task of it ending gets no new workers), and
  `progress` (keep polling / finished / blocked by failed tasks / stalled by failing
  workers).
- **`poll.py`**: the loop around them. `Submitter.poll()` does one poll (`query`, then
  `outcome` of the jobs it saw end, then counts in a subprocess, then submissions via
  `submit.prepare_class`/`write_script`/`submit_worker`); state kept between polls: settled
  and watched job IDs, the previous counts, and the failing jobs per class (committed only
  after everything that can fail was read, so a failed poll can be redone).
  `run()` repeats it, doubling the wait while nothing changes (up to `max_interval`);
  first-poll errors are fatal, later `SchedulerError`/`JugfileError` are retried
  (`MAX_FAILED_POLLS`). `cli._poll` holds `ProjectState.submitter_lock()`.
  `cli._submit_as_job` submits the loop itself as a job (`worker.make_submitter_spec`,
  `submit.prepare_submitter`, `script.submitter_command`) after checking for an active
  submitter job (`submitter.active_submitter_jobs`) and the lock.
- **`kills.py`**: pure functions on `(JobRecord, JobOutcome)` pairs: which jobs the
  scheduler killed (`KILLED_*`; `Outcome.CANCELLED` is a user's `qdel`), counts per class,
  and `suggestion(outcome, rc)` (raise `memory`/`time`, lower `time_fraction`; SIGKILL
  gets the memory one as a guess). `cli._poll` warns for each killed job in
  `PollResult.ended` and sums them up per class when the run is over.
- **`status.py`**: `jug-hpc status`, pure like `check.py`: `worker_counts` (queued/running
  per class, `SUBMIT_GRACE` as in `active_workers`) and `status(...)` → `StatusReport`
  (table text, kill warnings per class and kind, notes). `cli.cmd_status` reads everything
  (all jobs of the backend in `jobs.json`, `query` then `outcome`, task counts in a
  subprocess, `ProjectState.submitter_lock_holder()`) and holds no lock.
- **`cancel.py`**: `jug-hpc cancel`. `cancel(scheduler, project_state, ...)` cancels the
  active jobs (`submitter.active_jobs`): a submitter job first, then waits (`CANCEL_WAIT`)
  until it is no longer listed and `submitter.lock` is free, rereads `jobs.json`, and
  cancels the workers; `CancelError` (nothing cancelled) while a submitter runs outside
  the scheduler. `clock`/`sleep` are injectable for tests. `cli.cmd_cancel` reads only
  `cluster.toml` and `jobs.json`.
- Workers run `jug execute --keep-failed` (`script.jug_execute_command`), so failed tasks
  stay failed and the submitter can stop instead of resubmitting them forever.
- **`cli.py`**: argparse subcommands dispatched via the `COMMANDS` dict; command
  implementations import lazily. Listings go to stdout, `jug-hpc: error|warning|note: ...`
  to stderr. `-C DIR` sets where to start searching upward for `jug_hpc.toml`.

## Code and test conventions

- Docstrings use `'''` and numpydoc sections (Parameters / Returns / Raises); string
  literals use single quotes. Modules declare `__all__`.
- Tests: pytest + hypothesis (property tests, e.g. unit round-trips). Golden files for
  rendered scripts live in `tests/golden/<backend>/`; regenerate with
  `JUG_HPC_UPDATE_GOLDEN=1 uv run pytest` and review the diff. Test jugfiles live in
  `tests/jugfiles/` and are copied into `tmp_path` per test. Cluster config is isolated with
  `monkeypatch.setenv('XDG_CONFIG_HOME', ...)` or an explicit `environ=` argument; the
  scheduler registry is faked by monkeypatching `schedulers.base._entry_points`.
  A `redis://` jugdir can't be used in tests that load the jugfile (Jug connects).
- End-to-end tests of the submitter use the local backend through
  `tests/test_poll.py::local_project` (local jobs go to `XDG_STATE_HOME` in `tmp_path`;
  `tests/test_cancel.py` imports it from there).
  They are the slow tests: a worker may wait one Jug wait cycle (12 s) for its last task.
- `tests/fake_pbs/qsub` is a fake `qsub` (put on `PATH`, state in `$FAKE_PBS_DIR`) that runs
  each job synchronously in a clean, PBS-like environment (or holds it, if a `hold` file
  exists); `tests/fake_pbs/qstat` serves `qstat -f -F json [-x] [-t]` for its jobs.
  `tests/fake_pbs/qdel` deletes held jobs (they become finished, substate 91).
  `tests/test_fake_pbs.py` uses them for end-to-end PBS tests. Real `qstat` output, recorded on
  PBS Professional 2026.1 (names replaced), is in `tests/pbs_output/pbspro-2026.1/`;
  `tests/manual/pbs/` is the kit that recorded it (`run-everything.sh`, see its README),
  to be rerun by hand on a real cluster for each release. Their header comments document
  their behaviour. Sample PBS command output for unit tests lives in `tests/pbs_output/`.
  Because of the clean environment, projects there need `setup = "export PATH=<venv
  bin>:$PATH"`, and a job can't run `qsub` itself (`env -i` drops `FAKE_PBS_DIR`), so
  `submit --as-job` has no fake-PBS end-to-end test. There is no fake `qrls`.
- Commit messages are short imperative summaries (`Add ...`, `Keep ...`).
