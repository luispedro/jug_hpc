# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`jug_hpc` runs [Jug](https://jug.readthedocs.io) jugfiles on HPC clusters, submitting
workers sized per *resource class* of tasks (selected by Jug task name). Early development:
read `GOALS.md` (design, principles) and `PLAN.md` (architecture, milestones, decisions)
before making design-level changes. PLAN.md is the source of truth for what comes next.

Current state: milestone 1 (`config`, `check`) is done; milestone 2 is in progress
(`schedulers/base.py`, `schedulers/pbs.py`, `schedulers/local.py`, `schedulers/checks.py`,
`runner.py`, `worker.py`, `script.py`, `selection.py` exist; `state.py`, `submitter.py` and
`jug-hpc submit` do not yet).

## Commands

```bash
uv sync                                   # create .venv with jug_hpc and dev tools
uv run pytest                             # all tests
uv run pytest tests/test_script.py::test_name   # a single test
uv run ruff check .                       # lint (line length 100)
uv run jug-hpc --help                     # CLI (also: python -m jug_hpc)
```

CI (`.github/workflows/test.yml`) runs `uv sync --locked`, ruff, pytest on Python 3.11–3.15,
and `jug-hpc --version`. Keep `uv.lock` in sync when changing dependencies. Python ≥ 3.11 is
required because TOML is read with `tomllib`; Jug is the only runtime dependency.

- Dev tools (pytest, ruff, hypothesis) are in `[dependency-groups] dev`, so plain `uv sync`
  installs them. The locked ruff also enforces import sorting (I001) and sorted `__all__`
  (RUF022): run `uv run ruff check --fix .` before committing.
- Other Pythons through uv: `uv run --python 3.11 --isolated pytest`.
- A sibling Jug checkout lives at `../jug` (newer than PyPI's 2.7.0). One test (the
  `--exclude-*` one) is skipped with released Jug; run it with
  `uv run --isolated --with ../jug pytest`. Jug's examples (`../jug/examples`) are useful
  for trying `check` by hand (they need `--with numpy --with requests`).

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
  - Subtables named after a scheduler (`[pbs]`, `[class.x.pbs]`) are raw options passed
    through unchecked except for shape; class values win over cluster values
    (`worker._merge_raw`).
  - `ConfigError` messages always name the file and key, so they are shown to users as is.
- **`classes.py`** works only on task *names* (no jugfile loading). Unmatched names → default
  class; a name matched by two classes is an error (so class order never matters).
- **`jugstate.py` is the only module that imports Jug internals** (`jug.jug`, `jug.task`).
  `load_jugfile` resets and then restores Jug's process-wide globals (`alltasks`,
  `Task.store`, `sys.modules`, `sys.path`) so it can be called repeatedly. Keep Jug-internal
  access confined here.
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
- Commit messages are short imperative summaries (`Add ...`, `Keep ...`).
