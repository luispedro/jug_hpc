# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`jug_hpc` runs [Jug](https://jug.readthedocs.io) jugfiles on HPC clusters, submitting
workers sized per *resource class* of tasks (selected by Jug task name). Early development:
read `GOALS.md` (design, principles) and `PLAN.md` (architecture, milestones, decisions)
before making design-level changes. PLAN.md is the source of truth for what comes next.

Current state: milestones 0–1 are done; milestone 2 is done in code (`schedulers/`,
`runner.py`, `worker.py`, `script.py`, `selection.py`, `state.py`, `submit.py`,
`jug-hpc submit --all|--dry-run`; only the manual test on a real PBS cluster is left).
Milestone 3 is mostly done: task counts per class (`jugstate.task_counts`,
`classes.counts_by_class`, and `jugstate.subprocess_task_counts` for loading in a
subprocess), `Scheduler.query()` for PBS and local (a `JobStatus` per job, with
queued/running worker counts), the submission rule and exit condition (pure functions in
`submitter.py`), and the polling loop (`poll.py`, plain `jug-hpc submit`, with a
per-project lock), with end-to-end tests on the local backend (chain, three-class chain,
barrier, workers stopped by `JUG_MAX_SECONDS`, failed task), and `submit --as-job` (the
submitter as a job, sized by `[submitter]` in `jug_hpc.toml`), and backoff while nothing
changes (`--max-interval`). Jug's status cache is deliberately not used (PLAN.md).

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
update the docs in the same commit. Example output in the docs comes from real runs, with
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
  query, active workers per class, `workers_to_submit` (`min(ready, workers) - active`),
  and `progress` (keep polling / finished / blocked by failed tasks).
- **`poll.py`**: the loop around them. `Submitter.poll()` does one poll (counts in a
  subprocess, `query`, submissions via `submit.prepare_class`/`write_script`/
  `submit_worker`); the only state kept between polls is the set of settled job IDs.
  `run()` repeats it, doubling the wait while nothing changes (up to `max_interval`);
  first-poll errors are fatal, later `SchedulerError`/`JugfileError` are retried
  (`MAX_FAILED_POLLS`). `cli._poll` holds `ProjectState.submitter_lock()`.
  `cli._submit_as_job` submits the loop itself as a job (`worker.make_submitter_spec`,
  `submit.prepare_submitter`, `script.submitter_command`) after checking for an active
  submitter job (`submitter.active_submitter_jobs`) and the lock.
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
  `tests/test_poll.py::local_project` (local jobs go to `XDG_STATE_HOME` in `tmp_path`).
  They are the slow tests: a worker may wait one Jug wait cycle (12 s) for its last task.
- `tests/fake_pbs/qsub` is a fake `qsub` (put on `PATH`, state in `$FAKE_PBS_DIR`) that runs
  each job synchronously in a clean, PBS-like environment (or holds it, if a `hold` file
  exists); `tests/fake_pbs/qstat` serves `qstat -f -F json [-x]` for its jobs.
  `tests/test_fake_pbs.py` uses them for end-to-end PBS tests. Their header comments document
  their behaviour. Sample PBS command output for unit tests lives in `tests/pbs_output/`.
  Because of the clean environment, projects there need `setup = "export PATH=<venv
  bin>:$PATH"`, and a job can't run `qsub` itself (`env -i` drops `FAKE_PBS_DIR`), so
  `submit --as-job` has no fake-PBS end-to-end test. There is no fake `qdel`/`qrls`.
- Commit messages are short imperative summaries (`Add ...`, `Keep ...`).
