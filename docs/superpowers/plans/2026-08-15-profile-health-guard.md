# Profile Health Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Disable fixed time-period blocking by default and add a direction-scoped 24-hour profile health guard evaluated every four hours.

**Architecture:** Keep the existing seven-day daily selector unchanged. Add a pure evaluator in `app/profile_health_guard.py`, integrate its decision into `MonitorState`, persist structured audit fields through existing JSON payloads, and expose one disable switch through the server and launch script.

**Tech Stack:** Python dataclasses, unittest, SQLite JSON payloads, argparse, Bash.

---

### Task 1: Pure profile health evaluator

**Files:**
- Create: `app/profile_health_guard.py`
- Create: `tests/test_profile_health_guard.py`

- [ ] Write failing tests for fixed Shanghai four-hour boundaries, 24-hour lookback, exclusion of unsettled/future rows, per-key independent samples, direction isolation, and all four statuses.
- [ ] Run `python3 -m unittest tests.test_profile_health_guard -v` and verify failures are caused by the missing module.
- [ ] Implement immutable config/decision dataclasses and `evaluate_profile_health_guard()` with fixed defaults.
- [ ] Re-run the focused tests and verify they pass.

### Task 2: MonitorState integration and audit fields

**Files:**
- Modify: `app/models.py`
- Modify: `app/simulator.py`
- Modify: `app/state.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_simulator.py`
- Modify: `tests/test_storage.py`

- [ ] Add failing tests proving DEGRADED blocks, WATCH allows only a base first order, WATCH blocks a second order, HEALTHY preserves behavior, and snapshot/order/observation fields are structured.
- [ ] Run focused state/simulator/storage tests and verify the new assertions fail.
- [ ] Add backward-compatible model fields, evaluator integration, reason attachment, progression restriction, and snapshot serialization.
- [ ] Re-run focused tests and verify they pass.

### Task 3: Disable fixed time guard by default and wire the new switch

**Files:**
- Modify: `app/server.py`
- Modify: `scripts/run.sh`
- Modify: `tests/test_server.py`
- Modify: `tests/test_packaging.py`
- Modify: `tests/test_time_period_guard.py`

- [ ] Add failing tests for time guard default-off, explicit time guard opt-in, profile health default-on, and `--no-profile-health-guard`.
- [ ] Run focused server/packaging/time guard tests and verify failures.
- [ ] Implement argparse and Bash defaults while preserving explicit rollback switches.
- [ ] Re-run focused tests and verify they pass.

### Task 4: Documentation and release handoff

**Files:**
- Modify: `README.md`
- Modify: `docs/current-strategy.md`
- Modify: `docs/release-handoff.md`

- [ ] Document fixed health thresholds, guard ordering, API fields, time guard default-off behavior, write the actual release identity after the commit exists, and record the production sample boundary after deployment.
- [ ] Scan documentation for statements that still claim the time guard is default-on and correct them.

### Task 5: Verification, integration, tag, and deployment

**Files:**
- No additional production files.

- [ ] Run `python3 -m unittest discover -s tests` and require zero failures.
- [ ] Run `python3 -m compileall -q app scripts tests`, `bash -n scripts/run.sh`, `node --check app/static/app.js`, and `git diff --check`.
- [ ] Commit the feature branch, merge it into `main` non-interactively, and push `main`.
- [ ] Create and push an annotated release tag after the pushed `main` commit is confirmed.
- [ ] Build the minimal package from the tagged commit, deploy without clearing SQLite, and restart `victory-event-monitor`.
- [ ] Verify service state, prewarm readiness, order continuity, `time_period_guard.enabled=false`, `profile_health_guard.enabled=true`, current DPS version, and HTTPS APIs.
- [ ] Append exact commit, tag, release path, restart timestamp, order boundary, and verification output to the single `docs/release-handoff.md`, then push that documentation commit if deployment facts require a post-release record.
