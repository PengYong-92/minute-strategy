# Market Sequence Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, validate, enable, publish, and deploy a causal 10-minute market-sequence strategy that does not fall back to the legacy opening rules.

**Architecture:** A pure `app/market_sequence.py` module derives causal states and daily snapshots from 1-minute klines. `MonitorState` gives an enabled sequence signal precedence over the legacy selector, while the existing strategy continues to populate research observations. A standalone replay script performs daily walk-forward and final holdout evaluation before production defaults are selected.

**Tech Stack:** Python 3.10 standard library, dataclasses, SQLite, unittest, existing HTTP dashboard, Bash deployment script.

---

### Task 1: Pure sequence model

**Files:**
- Create: `app/market_sequence.py`
- Create: `tests/test_market_sequence.py`

- [ ] Write failing tests for 10-minute outcome labels, run buckets, causal cutoff exclusion, non-overlapping training rows, state qualification, two-minute candidate alignment and `WAIT` behavior.
- [ ] Run `.venv/bin/python -m unittest tests.test_market_sequence -v` and confirm failures are caused by the missing module/API.
- [ ] Implement immutable configuration, feature rows, daily selection windows, state aggregation, Wilson diagnostics and signal selection.
- [ ] Re-run the focused tests and confirm all pass.

### Task 2: Strict walk-forward replay

**Files:**
- Create: `scripts/replay_market_sequence.py`
- Create: `tests/test_market_sequence_replay.py`

- [ ] Write failing tests proving that each day trains only before its cutoff, final holdout dates are not used to select parameters, and concurrency variants enforce 1/2/5 open-order limits.
- [ ] Run `.venv/bin/python -m unittest tests.test_market_sequence_replay -v` and confirm expected failures.
- [ ] Implement zip loading, daily replay, parameter matrix ranking, direction/day/state breakdowns and JSON output.
- [ ] Re-run focused replay tests.
- [ ] Run the replay against available BTCUSDT Kline archives and select a stable parameter plateau only if the final holdout is positive.

### Task 3: Runtime integration and persistence metadata

**Files:**
- Modify: `app/models.py`
- Modify: `app/simulator.py`
- Modify: `app/state.py`
- Modify: `app/server.py`
- Modify: `tests/test_simulator.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_server.py`

- [ ] Write failing tests showing sequence metadata is copied to orders, sequence mode never falls back to legacy openings, and the snapshot is rebuilt once per daily cutoff.
- [ ] Run focused tests and verify the expected failures.
- [ ] Add sequence fields to signals/orders and copy them through the simulator.
- [ ] Give sequence mode precedence in `MonitorState`, expose status in snapshots, and add Chinese CLI parameters with validated defaults.
- [ ] Re-run focused tests.

### Task 4: Startup script, dashboard and documentation

**Files:**
- Modify: `scripts/run.sh`
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/styles.css`
- Modify: `README.md`
- Modify: `docs/current-strategy.md`
- Modify: `tests/test_packaging.py`

- [ ] Write failing packaging tests for sequence defaults, Chinese help and dashboard status elements.
- [ ] Run the focused packaging tests and confirm expected failures.
- [ ] Add sequence environment/CLI forwarding, compact dashboard status and strategy documentation.
- [ ] Re-run packaging tests.

### Task 5: Verification and publication

**Files:**
- Modify only files required by failures found during verification.

- [ ] Run `.venv/bin/python -m unittest discover -s tests -v` outside the restricted socket sandbox and require zero failures.
- [ ] Run `bash scripts/package.sh` and inspect the archive contents.
- [ ] Review `git diff --check`, `git status --short`, and the complete diff for accidental secrets or unrelated reversions.
- [ ] Commit all intended existing and new work, then push `main` to `origin`.

### Task 6: Production deployment with clean order baseline

**Files:**
- Server application directory and SQLite database discovered from the active service.

- [ ] Inspect the active service, application path, environment and database path before changing anything.
- [ ] Stop the application and create timestamped backups of the release directory and SQLite database.
- [ ] Deploy the pushed commit without changing nginx or certificate configuration.
- [ ] In one SQLite transaction delete `orders`, `order_entry_snapshots`, `stake_progression_credits` and `stake_progression_runtime`; preserve observations and daily selections.
- [ ] Start the service and verify the process, logs, HTTPS API, `warmup.status`, sequence snapshot, zero orders and continuing observation settlement.
