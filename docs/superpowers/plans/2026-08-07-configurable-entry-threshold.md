# Configurable Entry Threshold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an exact daily profile to use a configurable score threshold before the 1-minute wave guard, persist both effective and calculated thresholds, and expose them in the order table.

**Architecture:** `MonitorState` owns an optional numeric threshold override. Daily profile matching stays exact and only evaluates the current primary signal; a matched signal becomes actionable only after meeting the override, then the existing wave and order guards run. Models and order JSON add a backward-compatible calculated threshold field, while CLI, shell startup, API state, and the table expose the configuration.

**Tech Stack:** Python 3 dataclasses and unittest, SQLite JSON persistence, argparse, Bash, vanilla JavaScript/HTML.

---

### Task 1: Threshold Parsing And Runtime Configuration

**Files:**
- Modify: `app/server.py`
- Modify: `scripts/run.sh`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing parser tests**

Add tests that parse `auto`, empty input, `0`, `30`, `-1`, `96`, and nonnumeric input. Assert that `auto` maps to `None`, valid numbers map to floats, and invalid values raise `argparse.ArgumentTypeError`.

- [ ] **Step 2: Run parser tests and verify RED**

Run: `python3 -m unittest tests.test_server -v`

Expected: failures because `_trade_score_threshold` and `--trade-score-threshold` do not exist.

- [ ] **Step 3: Add parser and startup plumbing**

Implement a parser equivalent to:

```python
def _trade_score_threshold(value: str) -> float | None:
    clean = str(value or "").strip().lower()
    if clean in {"", "auto"}:
        return None
    try:
        threshold = float(clean)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("开单阈值必须是 auto 或 0-95 的数字") from exc
    if not 0.0 <= threshold <= 95.0:
        raise argparse.ArgumentTypeError("开单阈值必须在 0-95 之间")
    return threshold
```

Add `TRADE_SCORE_THRESHOLD`, `--trade-score-threshold`, Chinese help text, shell option parsing, validation, and forwarding to `MonitorState(trade_score_threshold=...)`.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run: `python3 -m unittest tests.test_server -v`

Expected: all server tests pass.

### Task 2: Daily Profile Threshold And Wave Ordering

**Files:**
- Modify: `app/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write failing state tests**

Cover these separate behaviors:

```text
auto + matching WAIT -> remains WAIT
override 0 + matching primary profile -> promoted to observed direction
override 30 + score 29 -> remains WAIT
override 30 + score 30 -> promoted
override 0 + unselected profile -> remains WAIT
override 0 + matching research candidate only -> remains WAIT
promoted LONG + UP_LEG -> opens
promoted LONG + DOWN_LEG -> WAVE_DIRECTION_BLOCKED
```

- [ ] **Step 2: Run state tests and verify RED**

Run: `python3 -m unittest tests.test_state.MonitorStateTest -v`

Expected: threshold promotion and post-profile wave tests fail.

- [ ] **Step 3: Implement minimal selection behavior**

Store a normalized `float | None` override on `MonitorState`. Change `_select_daily_profile_signal` to match only `primary_signal`; AUTO mode uses its executable direction, while override mode requires `observe_direction`, and both use the complete daily profile key. In override mode, save the original threshold, apply the override, and set the candidate direction only when `abs(score) >= override`.

Change update order from:

```text
wave -> daily profile
```

to:

```text
daily profile -> wave
```

so every promoted direction receives fresh wave metadata, batch ID, and direction validation.

- [ ] **Step 4: Run state tests and verify GREEN**

Run: `python3 -m unittest tests.test_state.MonitorStateTest -v`

Expected: all state tests pass.

### Task 3: Persist Threshold Evidence

**Files:**
- Modify: `app/models.py`
- Modify: `app/simulator.py`
- Modify: `app/state.py`
- Test: `tests/test_simulator.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write failing persistence tests**

Create a signal with `score=0`, `threshold=0`, and `calculated_threshold=79.1`. Assert the opened order and a storage round trip preserve all three values. Assert old JSON without `calculated_threshold` still loads with its dataclass default.

- [ ] **Step 2: Run persistence tests and verify RED**

Run: `python3 -m unittest tests.test_simulator tests.test_storage -v`

Expected: failures because the new field is absent.

- [ ] **Step 3: Add backward-compatible model fields**

Add `calculated_threshold: float = 0.0` to `Signal` and `SimulatedOrder`, copy it in `AccountSimulator.open_order_with_credit`, expose threshold policy in `snapshot()`, and include it in order entry snapshots.

- [ ] **Step 4: Run persistence tests and verify GREEN**

Run: `python3 -m unittest tests.test_simulator tests.test_storage -v`

Expected: all tests pass.

### Task 4: Order Table Threshold Column

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Test: `tests/test_packaging.py`

- [ ] **Step 1: Write failing page test**

Assert the order header contains `开单阈值`, JavaScript renders `order.threshold`, `order.score`, and `order.calculated_threshold`, and the empty order row uses `colspan="17"`.

- [ ] **Step 2: Run page test and verify RED**

Run: `python3 -m unittest tests.test_packaging -v`

Expected: failure because the column is absent.

- [ ] **Step 3: Add the table column**

Insert the column after session win rate/EV. Render the effective threshold as the main value and `评分 X · 原始 Y` as secondary text, falling back to `threshold` when old orders have no calculated value.

- [ ] **Step 4: Run page and JavaScript checks**

Run: `python3 -m unittest tests.test_packaging -v`

Run: `node --check app/static/app.js`

Expected: both pass.

### Task 5: Documentation And Full Verification

**Files:**
- Modify: `docs/current-strategy.md`
- Modify after deployment: `docs/release-handoff-2026-08-06-e824cf5.md`

- [ ] **Step 1: Update current strategy documentation**

Document the override semantics, exact profile matching, daily-profile-before-wave order, and three stored threshold fields. Mark the old “profiles can never promote WAIT” statement as superseded only when an explicit numeric override is configured.

- [ ] **Step 2: Run complete verification**

Run: `python3 -m unittest discover -s tests -v`

Run: `python3 -m compileall -q app tests`

Run: `node --check app/static/app.js`

Run: `git diff --check`

Expected: all tests and checks pass.

- [ ] **Step 3: Commit and push application changes**

Stage only the listed implementation, test, and strategy documentation files. Commit with `fix: make entry score threshold configurable` and push `feature/1m-wave-direction-guard`.

### Task 6: Production Deployment And Handoff

**Files:**
- Production: `/opt/victory-event-monitor/releases/event-contract-monitor-<commit>-<timestamp>`
- Production: `/etc/systemd/system/victory-event-monitor.service.d/60-trade-score-threshold.conf`
- Modify: `docs/release-handoff-2026-08-06-e824cf5.md`

- [ ] **Step 1: Build a minimal release package**

Exclude `.git`, caches, tests, reports, local data, SQLite, and secrets. Record the package SHA-256 and inspect its file list before upload.

- [ ] **Step 2: Deploy and configure threshold 0**

Upload and extract to a new immutable release directory, update `/opt/victory-event-monitor/current`, write:

```ini
[Service]
Environment=TRADE_SCORE_THRESHOLD=0
```

Run `systemctl daemon-reload` and restart `victory-event-monitor`. Do not clear observations or orders.

- [ ] **Step 3: Verify production**

Confirm `active/running`, `NRestarts=0`, HTTPS 200, SSL result 0, warmup `READY`, `last_error=null`, `trade_score_threshold.mode=OVERRIDE`, value `0`, and the order API exposes the new fields. Confirm current wave/profile state explains whether an immediate order is allowed.

- [ ] **Step 4: Complete handoff documentation**

Append the new commit, release directory, package hash, systemd drop-in, production validation, threshold adjustment procedure, and post-release sample boundary. Commit and push the documentation without redeploying the docs-only commit.
