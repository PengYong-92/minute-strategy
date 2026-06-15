# Rolling Edge Precondition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a non-overfitted rolling edge layer that records and can optionally gate deteriorating setups before entry, without hard-disabling fixed months or historical weak time windows.

**Architecture:** Add a small `app/rolling_edge.py` module that computes edge from settled prior orders only. Backtests can use it as an optional pre-entry guard and report both baseline and guarded outcomes. Runtime can later consume the same module from persisted simulated orders, but this task keeps live behavior unchanged except for exposing tested utilities.

**Tech Stack:** Python dataclasses, existing `unittest`, existing backtest JSON reports, no third-party dependencies.

---

### Task 1: Rolling edge utility

**Files:**
- Create: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/rolling_edge.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_rolling_edge.py`

- [ ] **Step 1: Write failing tests**

Add tests for: grouping by `timeframe|segment|setup`, using only orders before current entry time, flagging when prior window has enough samples and fails break-even.

- [ ] **Step 2: Verify RED**

Run:
```bash
python3 -m unittest tests.test_rolling_edge -v
```
Expected: import failure for `app.rolling_edge`.

- [ ] **Step 3: Implement minimal utility**

Create `RollingEdgeConfig`, `RollingEdgeSnapshot`, `setup_key(order_or_signal)`, `rolling_edge_snapshot(...)`, and `should_degrade(...)`.

- [ ] **Step 4: Verify GREEN**

Run:
```bash
python3 -m unittest tests.test_rolling_edge -v
```
Expected: pass.

### Task 2: Backtest optional rolling guard

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/backtest.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add a test where prior losing orders for the same `timeframe|segment|setup` cause the next otherwise actionable signal to be recorded as `rolling_edge_degraded` and not opened when guard mode is enabled.

- [ ] **Step 2: Verify RED**

Run:
```bash
python3 -m unittest tests.test_backtest.BacktestTest.test_rolling_edge_guard_blocks_degraded_setup -v
```
Expected: `BacktestConfig` lacks rolling guard fields or result lacks rejection key.

- [ ] **Step 3: Implement minimal backtest support**

Add fields to `BacktestConfig`: `enable_rolling_edge_guard`, `rolling_edge_lookback_days`, `rolling_edge_min_samples`, `rolling_edge_min_win_rate`, `rolling_edge_min_ev`. In `run_backtest`, before opening, compute snapshot from already settled orders only. If degraded, increment `rejected_signals["rolling_edge_degraded"]` and continue.

- [ ] **Step 4: Verify GREEN**

Run:
```bash
python3 -m unittest tests.test_backtest.BacktestTest.test_rolling_edge_guard_blocks_degraded_setup -v
```
Expected: pass.

### Task 3: Generate guarded one-year report and docs

**Files:**
- Create report under `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/reports/`
- Update: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/docs/strategy-precondition-audit-2026-05-16.md`

- [ ] **Step 1: Run full tests**

Run:
```bash
python3 -m unittest discover -s tests -q
python3 -m py_compile app/*.py
```
Expected: all tests pass and compile succeeds.

- [ ] **Step 2: Run one-year guarded backtest**

Use the same 2025-05-16 to 2026-05-15 data range, with rolling guard enabled at 60 days / 15 samples.

- [ ] **Step 3: Record results**

Append guarded-vs-baseline outcome to `docs/strategy-precondition-audit-2026-05-16.md`, explicitly noting that this is still sample-internal and must be walk-forward validated before live enforcement.
