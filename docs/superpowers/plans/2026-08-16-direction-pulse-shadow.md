# Direction Pulse Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add causal N=12/N=16 direction-level pulse observation that refreshes after every independent observation settlement without changing live order admission.

**Architecture:** A pure evaluator derives rolling LONG/SHORT snapshots from already-settled, globally non-overlapping observation samples. `MonitorState` refreshes both directions immediately after settlement and attaches the current shadow decision to signals, orders, and observation records for later outcome analysis. The shadow state is exposed through `/api/state` but is never read by an order gate.

**Tech Stack:** Python 3.10 dataclasses, existing SQLite JSON payloads, `unittest`.

---

### Task 1: Direction pulse evaluator

**Files:**
- Create: `app/direction_pulse_shadow.py`
- Create: `tests/test_direction_pulse_shadow.py`

- [ ] Test N=12/N=16 warmup, NORMAL, WATCH, and DEGRADED boundaries.
- [ ] Test direction isolation, causal settlement cutoff, and global overlap removal.
- [ ] Implement the pure evaluator with no four-hour boundary or wall-clock cache.
- [ ] Run `python -m unittest tests.test_direction_pulse_shadow -v`.

### Task 2: Runtime refresh and audit propagation

**Files:**
- Modify: `app/models.py`
- Modify: `app/state.py`
- Modify: `app/simulator.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_simulator.py`
- Modify: `tests/test_storage.py`

- [ ] Test that one newly settled independent observation changes the runtime snapshot immediately.
- [ ] Test that shadow metadata reaches signals, simulated orders, observations, and SQLite JSON reloads.
- [ ] Refresh aggregate state after `_settle_observations`, attach candidate slot decisions, and preserve backward-compatible defaults.
- [ ] Prove shadow fields are never consulted by `open_allowed` or any blocking branch.

### Task 3: Documentation, verification, and release

**Files:**
- Modify: `docs/current-strategy.md`
- Modify: `docs/release-handoff.md`

- [ ] Document the causal sample source, N=12/N=16 thresholds, event-driven refresh, and observe-only boundary.
- [ ] Run focused tests, all 508 tests, compile checks, shell/JavaScript syntax checks, and `git diff --check`.
- [ ] Commit, merge to `main`, push, tag, package, deploy without clearing SQLite, and verify API/service/order continuity.
