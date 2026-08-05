# Daily Observation Profile Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-signal observation promotion with a daily 07:50 profile selection snapshot that drives the main 10-minute strategy from 08:00 to the next 08:00.

**Architecture:** Add a pure `daily_profile_selector` module for deterministic schedule, grouping, ranking, and hysteresis. Persist one JSON snapshot per symbol/effective day in SQLite, load it into `MonitorState`, and convert only matching observation candidates into actionable signals while preserving all common order and risk guards. Expose the active snapshot through existing APIs and the dashboard.

**Tech Stack:** Python 3 standard library, dataclasses, `zoneinfo`, SQLite, `unittest`, vanilla HTML/CSS/JavaScript.

---

### Task 1: Pure daily selector

**Files:**
- Create: `app/daily_profile_selector.py`
- Create: `tests/test_daily_profile_selector.py`

- [ ] **Step 1: Write failing schedule and identity tests**

Cover exact identity `10|family|tag|SHORT|WD-02`, Shanghai 07:50 cutoff, 08:00 effective range, before-cutoff previous-day selection, and after-08:00 restart catch-up.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.test_daily_profile_selector -v`

Expected: import failure because `app.daily_profile_selector` does not exist.

- [ ] **Step 3: Implement schedule and exact profile identity**

Add `DailyProfileSelectorConfig`, `profile_key`, and `selection_window`. Use `ZoneInfo("Asia/Shanghai")`; return integer millisecond fields `evaluated_at`, `lookback_start`, `lookback_end`, `effective_from`, and `effective_until`.

- [ ] **Step 4: Write failing grouping and ranking tests**

Build settled `ObservationSignal` rows that prove:

- grouping includes strategy tag and direction;
- overlapping rows are deduplicated per profile;
- rows outside `[lookback_start, lookback_end)` are excluded;
- minimum sample, 60% win rate, and non-negative EV gates are enforced;
- selected profiles are sorted by win rate, EV, sample count, then key and capped at four.

- [ ] **Step 5: Implement `build_daily_selection`**

Return a serializable snapshot with version, status, schedule, config, all candidate statistics, selected profiles, and Chinese selection reasons.

- [ ] **Step 6: Add hysteresis tests and implementation**

An active profile below 60% or EV at/below zero exits at that daily evaluation. All qualifying profiles are selected; there is no fixed count cap.

- [ ] **Step 7: Run focused tests GREEN**

Run: `python3 -m unittest tests.test_daily_profile_selector -v`

Expected: all daily selector tests pass.

### Task 2: Persist daily snapshots

**Files:**
- Modify: `app/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write failing storage tests**

Test `save_daily_profile_selection`, idempotent replacement by `symbol + effective_from`, loading the selection effective at a timestamp, and loading the latest valid selection for failure fallback.

- [ ] **Step 2: Run storage tests and verify RED**

Run: `python3 -m unittest tests.test_storage.StorageTests.test_persists_daily_profile_selection -v`

Expected: missing storage method.

- [ ] **Step 3: Add schema and storage methods**

Create `daily_profile_selections(symbol, effective_from, effective_until, status, evaluated_at, payload, updated_at_ms)` with primary key `(symbol, effective_from)` and an effective-time index. Store complete snapshots as UTF-8 JSON and return defensive dictionaries.

- [ ] **Step 4: Run storage tests GREEN**

Run: `python3 -m unittest tests.test_storage -v`

Expected: all storage tests pass.

### Task 3: Integrate daily selection into live state

**Files:**
- Modify: `app/models.py`
- Modify: `app/simulator.py`
- Modify: `app/state.py`
- Modify: `tests/test_simulator.py`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Write failing model and simulator traceability tests**

Add signal/order assertions for `daily_profile_selected` and `daily_profile_version`. A selected WAIT observation signal must be actionable without overwriting its original score and threshold; the opened order must retain the version.

- [ ] **Step 2: Implement explicit execution override fields**

Add backward-compatible default fields to `Signal` and `SimulatedOrder`, update `Signal.actionable`, and copy the version in `AccountSimulator.open_order`.

- [ ] **Step 3: Write failing state scheduling tests**

Cover no evaluation before 07:50, one evaluation after 07:50, no second evaluation that day, 08:00 activation, startup catch-up, symbol reset restore, and storage failure fallback.

- [ ] **Step 4: Implement `_refresh_daily_profile_selection`**

Settle observations first, evaluate from in-memory settled rows at the fixed cutoff, save synchronously before activation, and expose `READY`, `FALLBACK`, or disabled status. On evaluation failure retain the latest valid snapshot; do not create an empty failure snapshot.

- [ ] **Step 5: Write failing signal-selection tests**

Prove that an exact selected primary or research observation profile becomes the cycle's executable signal, an unselected profile returns `DAILY_PROFILE_NOT_SELECTED`, and static order policy, cooldown, risk pause, rolling guard, profile guard, amount, and progression remain in force.

- [ ] **Step 6: Replace per-signal promotion with snapshot selection**

Choose from the primary signal plus observation candidates according to the selected profile order. Apply the explicit execution override and attach selected profile N/win-rate/EV/version to the signal. Disable `_observation_profile_promoted_signal` whenever the daily selector is enabled.

- [ ] **Step 7: Add API summary enrichment**

Expose `daily_profile_selection` in `/api/state`; annotate observation summary groups with `selection_state` and `selection_reason` using exact keys.

- [ ] **Step 8: Run state and simulator tests GREEN**

Run: `python3 -m unittest tests.test_state tests.test_simulator -v`

Expected: all tests pass.

### Task 4: Add startup configuration

**Files:**
- Modify: `app/server.py`
- Modify: `scripts/run.sh`
- Modify: `README.md`
- Modify: `docs/current-strategy.md`
- Modify: `tests/test_server.py`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: Write failing parser and packaging tests**

Require Chinese help text and environment/CLI options for enable switch, 7-day lookback, 20 samples, 60% entry win rate, 0U entry EV, 60% exit win rate, immediate degraded exit, unlimited active profiles, 07:50 evaluation, and 08:00 activation.

- [ ] **Step 2: Implement arguments and pass config into state**

Enable daily profile selection by default. Keep legacy observation-promotion arguments parseable, but daily selection takes precedence and the old promotion path remains inactive.

- [ ] **Step 3: Update startup and strategy documentation**

Document that the selector evaluates once daily in Shanghai time and that the selected set is fixed intraday. Keep all script parameter descriptions in Chinese.

- [ ] **Step 4: Run server and packaging tests GREEN**

Run: `python3 -m unittest tests.test_server tests.test_packaging -v`

Expected: all tests pass.

### Task 5: Dashboard visibility

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/styles.css`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Add failing static-response assertions**

Assert the page contains a current daily profile status field and a compact active-profile list; assert `PROMOTE_WATCH` remains labelled as observation rather than automatic release.

- [ ] **Step 2: Implement compact selector status UI**

Display selector status, last evaluation, effective range, selected count, and up to four active profiles with direction, WD segment, sample count, win rate, and EV. Add selected/candidate/degraded/not-qualified states to observation summary rows without introducing nested cards.

- [ ] **Step 3: Run frontend-related server tests GREEN**

Run: `python3 -m unittest tests.test_server -v`

Expected: all tests pass.

### Task 6: Full verification and direct deployment

**Files:**
- Modify only if verification exposes a defect in files above.

- [ ] **Step 1: Run complete tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

- [ ] **Step 2: Run static checks**

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 3: Deploy a minimal release to the existing server**

Copy application code and static assets into a timestamped `/opt/victory-event-monitor/releases/` directory, preserve the shared SQLite database and data cache, point `current` to the release, restart only the existing monitor service, and leave nginx/certbot unchanged.

- [ ] **Step 4: Verify live behavior**

Confirm service health, HTTPS 200, warmup READY, daily selector enabled, one persisted current snapshot, active profile details visible, existing observations/orders retained, and new signal audit rows include the selector decision/version.

- [ ] **Step 5: Record release boundary**

Report release path, service start time, selector version/effective range, selected profiles, retained order/observation counts, and the exact timestamp from which future real-order evaluation must begin.
