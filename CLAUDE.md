# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What klok is

`klok` is a command-line time tracker with a plaintext backend. It is distributed on PyPI as **`klok-cli`** (the name `klok` was taken), installs the **`klok`** console command, and depends on **nothing but the Python standard library** (`dependencies = []`, `requires-python >= 3.9`). Keep it that way: adding a third-party runtime dependency is a deliberate design change, not a casual one.

The data model is a single record type, the `Frame` (a tracked interval: project, `+tags`, note, sheet), stored one-JSON-object-per-line in `frames.jsonl`. Everything else — reports, exports, interval surgery, focus timers, mindfulness features — operates on frames.

## Commands

There is no Makefile, tox, nox, or pre-commit config, and **no linter/formatter/type-checker is configured** — do not assume `ruff`/`black`/`mypy` are available. Run from the repo root with the package importable via `PYTHONPATH=.`.

Run the CLI from source:
```sh
PYTHONPATH=. python3 -m klok status
```

Run the full test suite (stdlib `unittest`):
```sh
python -m unittest discover -s tests -t .
```

Run a single test module / class / method:
```sh
python -m unittest tests.test_cli
python -m unittest tests.test_cli.TrackingTests
python -m unittest tests.test_cli.TrackingTests.test_start_then_stop_records_the_interval
```

Byte-compile sanity check (also run in CI):
```sh
python -m compileall -q klok
```

Build and validate distributions (matches the publish workflow):
```sh
python -m build && twine check dist/*
```

## Architecture

Flat-layout package in `klok/` (no `src/`). Classic layered CLI: a thin dataclass model, a lock-protected plaintext store, pure helper/render modules, and one large command layer that wires them together. Read `cli.py` first when changing behavior.

- **`model.py`** — the one record type, `Frame` (dataclass with `start`/`stop`/`project`/`tags`/`note`/`sheet`/`id`/`updated_at`). Holds derived logic (`running`, `duration`, `overlaps`, `spans`, `label`). Its `from_dict` is intentionally **tolerant** (accepts `end` for `stop`, `annotation` for `note`, string-or-list tags) — this is what lets hand-edited files and imports from other trackers load.

- **`storage.py`** — `Store` reads/writes `frames.jsonl` (sorted by start). Cross-cutting mechanisms live here and matter for any mutation: a cross-platform advisory `FileLock` (O_CREAT|O_EXCL, 5s timeout, 30s stale reclaim), atomic writes (`.tmp` + `os.replace`), and **undo** via `_snapshot` into `undo.jsonl` (capped at `UNDO_LIMIT = 100`). Active-sheet state is separate, in `state.json`. The query layer (`by_id`, `select`, `overlaps`, `gaps`, `_matches`) resolves `@`/`@N`/id-prefix selectors and project filters (exact / `parent.child` prefix / glob).

- **`config.py`** — `Config` wraps `config.ini` over a hard-coded `DEFAULTS` dict and owns every on-disk path. **`data_home()` resolves `KLOK_HOME` → `$XDG_DATA_HOME/klok` → `~/.local/share/klok`.** `KLOK_HOME` is the isolation mechanism — the test suite and CI smoke tests point it at a temp dir.

- **`timeparse.py`** — all time/duration/range parsing and formatting (the "type what a human would type" UX: `parse_datetime_ex`, `parse_duration`, `parse_range` handling `since monday`, `A to B`, `last N days`, etc.). Timestamps are timezone-aware, defaulting to local tz.

- **`formats.py`** — machine-readable I/O: exporters (`to_json`/`csv`/`tsv`/`ical`/`timeclock`/`ledger`/`markdown`) dispatched by `render(...)`, plus `parse_import`. `FORMATS` is the canonical format list. These exports are klok's intended extension point (there is no plugin API).

- **`report.py`** — human-readable presentation: a `Renderer` carrying shared settings and the `render_*` functions (log, summary, report, totals, charts, punchcard, gaps) with aggregation helpers (`group_by`, `by_project`, `by_tag`, `by_day`, `by_sheet`).

- **`utils.py`** — terminal helpers: `Term` (color auto/always/never, honors `NO_COLOR` and `isatty`, deterministic per-project colors), `render_table`, ANSI-aware width helpers, and `die(message, code)`.

- **`focus.py`** / **`mindful.py`** — focus timers (`countdown`, `pomodoro_plan`, best-effort `notify()` that never raises) and mindfulness features (`breathe`, `meditate`, mood `Checkin`/`CheckinStore` backed by a separate `checkins.jsonl`). Timer functions take injected `sleep=`/`clock=` so tests run instantly instead of actually waiting.

### CLI command layer (`cli.py`)

- `main(argv)` is the single entry point (`[project.scripts]` → `klok = "klok.cli:main"`). Empty argv defaults to `status`; it runs `_preprocess(argv)`, parses, builds a `Context`, dispatches to `args.func(ctx)`, and **centrally catches** `StoreError`/`TimeParseError`/`ValueError`/`KeyError`/`FileNotFoundError`, routing them through `die()`. So `cmd_*` functions should **raise** on error rather than print-and-return.
- `Context` is the dependency bundle passed to every command: `Config`, `Store`, resolved `now`, `Term`, `Renderer`, plus selection/range/sheet resolution helpers. A hidden `--now` flag overrides the clock — the tests rely on it.
- Commands are ~45 `cmd_*(ctx) -> int` functions. `build_parser()` registers each via a local `add(...)` helper that also files it into a named help group (`GROUP_ORDER`); shared argparse parents (`sheet_opts`, `filter_opts`, `range_opts`, `format_opts`) are composed in. `help=` is deliberately omitted so argparse doesn't dump a wall of commands; a curated grouped epilog is shown instead.
- Two parsing hacks to respect when adding options: `KlokArgumentParser.error` adds `difflib` "did you mean" suggestions, and `_preprocess` folds `--at -15m` into `--at=-15m` and rewrites the `tag` removal marker so argparse doesn't read leading-`-` values as flags (see the `TIME_OPTIONS` set near the top of the file).

## Testing conventions

Framework is stdlib `unittest` (no pytest). Tests live in `tests/`.

- `test_cli.py` drives the real `main()` in-process, capturing stdout/stderr, with each test given a fresh temp home via `--home`, a fixed clock via `--now`, and `--no-color`. Use the `CliTestCase` helpers: `assertOk`, `run_klok`, `run_klok_exit` (for argparse-level `SystemExit` outcomes), and `ids(...)`.
- Timer/mindfulness tests inject a `FakeClock` whose `sleep` advances a virtual clock, so nothing actually sleeps. Preserve the `sleep=`/`clock=` injection points when editing `focus.py`/`mindful.py`.

## CI & release

`.github/workflows/ci.yml` runs the unittest suite + `compileall` on a Python 3.9–3.13 matrix (ubuntu), plus macOS/Windows on 3.12, then an `install` job that `pip install .` and runs an end-to-end `klok` smoke test under a temp `KLOK_HOME`. `.github/workflows/publish.yml` builds, `twine check`s, and publishes to PyPI via **Trusted Publishing (OIDC)** on GitHub Release — there is no stored API token.

## Gotcha: version is maintained in two places

`klok/__init__.py` sets `__version__` (what `klok version` / `klok --version` print) and `pyproject.toml` sets the published version **independently, by hand**. They are currently out of sync (`__init__.py` says `1.0.0`, `pyproject.toml` says `1.0.1`). When bumping the version, update **both**.
