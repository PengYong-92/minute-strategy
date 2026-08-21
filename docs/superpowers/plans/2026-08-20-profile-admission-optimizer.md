# Profile Admission Optimizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建生产与回放共用的画像准入纯函数，自动搜索常驻画像与SHORT快速通道参数，并以胜率优先、日均约50单和前向稳定性门槛决定是否允许发布。

**Architecture:** `app/profile_admission.py`拥有策略、上下文、准入决定、候选排序和固定32组网格；`MonitorState`与严格因果回放只负责把已有每日资格和开单前N12/N20转换成共享上下文。回放自动评估并排名全部配置，生产只加载冻结配置，不在运行中选参；价格结构继续保持影子。

**Tech Stack:** Python 3标准库、`dataclasses`、`hashlib`、`json`、现有`unittest`、SQLite只读回放和原生Shell启动脚本。

---

## Invariants

- 不部署、不推送、不合并`main`、不创建tag、不清空订单。
- 不增加30分钟、4小时或日线判断，不修改已有方向。
- 快速通道只能提升已存在的观察候选，不能从无方向WAIT创建订单。
- `PAUSED`始终阻止，`WARMUP`不得快速准入，FAST和WATCH不得使用第二席位或金额叠加。
- 生产和回放使用同一策略哈希、准入函数和候选排序键。
- 报告区分聚合通过、稳定性证明和最终发布许可；任一不足时`release_allowed=false`。

### Task 1: Shared Admission Contract

**Files:**
- Create: `app/profile_admission.py`
- Create: `tests/test_profile_admission.py`

- [x] **Step 1: Write failing policy validation and canonical hash tests**

Add tests which import `ProfileAdmissionPolicy`, `ProfileAdmissionContext`, `evaluate_profile_admission`, `candidate_policy`, and `policy_grid`. Assert that equivalent set ordering produces the same hash, non-finite EV and inverted N12 bounds fail, and `policy_grid()` returns 32 unique hashes in deterministic order.

- [x] **Step 2: Run the new tests and confirm RED**

Run: `python3 -m unittest tests.test_profile_admission -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.profile_admission'`.

- [x] **Step 3: Implement immutable policy and context types**

Implement:

```python
PROFILE_ADMISSION_VERSION = "PROFILE_ADMISSION_V1"

@dataclass(frozen=True)
class ProfileAdmissionPolicy:
    version: str = PROFILE_ADMISSION_VERSION
    resident_allowed_states: tuple[str, ...] = ("ACTIVE", "WATCH")
    resident_n12_max_wins: int = 12
    resident_daily_win_rate_floor: float | None = None
    fast_enabled: bool = False
    fast_directions: tuple[str, ...] = ("SHORT",)
    fast_allowed_states: tuple[str, ...] = ("ACTIVE",)
    fast_n12_min_wins: int = 7
    fast_n12_max_wins: int = 8
    fast_n20_ev_min: float = 0.0
    fast_allow_second_order: bool = False
    fast_allow_progression: bool = False
    watch_allow_first_order: bool = True
    watch_allow_second_order: bool = False
    watch_allow_progression: bool = False

@dataclass(frozen=True)
class ProfileAdmissionContext:
    profile_key: str
    direction: str
    order_slot: str
    daily_selected: bool
    qualification_state: str
    daily_rank: int | None
    daily_win_rate: float
    adaptive_state: str
    adaptive_transition: str
    adaptive_evaluated_at: int
    n12_sample_size: int
    n12_wins: int
    n20_sample_size: int
    n20_ev: float
    candidate_origin: str
    candidate_ordinal: int
```

Expose canonical `to_dict()`, JSON, hash, baseline policy, current candidate policy and deterministic 32-policy grid.

- [x] **Step 4: Add failing admission matrix and ranking tests**

Cover resident ACTIVE/WATCH/PAUSED/WARMUP, resident N12 overheat, unselected SHORT ACTIVE fast admission, LONG fast rejection, second-slot rejection, and deterministic `RESIDENT` before `FAST` ranking.

- [x] **Step 5: Implement pure admission and ranking**

Return a frozen `ProfileAdmissionDecision` containing `allowed`, `channel`, `code`, `allow_second_order`, `allow_progression`, `policy_version`, `policy_hash`, and `rank_key`. Codes must distinguish daily missing, state blocked, resident overheated, fast direction/state/N12/N20 rejection, WATCH second slot and admitted RESIDENT/FAST.

- [x] **Step 6: Run focused tests and commit**

Run: `python3 -m unittest tests.test_profile_admission -v`

Expected: PASS.

Commit: `feat: add shared profile admission policy`

### Task 2: Causal Grid Search Replay

**Files:**
- Modify: `app/adaptive_profile_state.py`
- Modify: `scripts/replay_daily_profile_selector.py`
- Modify: `tests/test_daily_profile_replay.py`

- [x] **Step 1: Write failing replay tests for shared selection**

Add fixtures where a blocked first resident falls through to the next resident, where no resident exists and a SHORT ACTIVE fast candidate is selected, and where LONG/WARMUP/PAUSED candidates remain rejected. Assert the selected observation key, admission channel/code, and progression prohibition.

- [x] **Step 2: Verify RED against the current daily-only selector**

Run: `python3 -m unittest tests.test_daily_profile_replay.DailyProfileReplayTest.test_replay_fast_lane_uses_shared_admission tests.test_daily_profile_replay.DailyProfileReplayTest.test_blocked_resident_falls_through -v`

Expected: FAIL because `_execute_replay` only chooses the first daily-selected profile.

- [x] **Step 3: Preserve full adaptive before-state inputs**

Add `transition` to compact adaptive event rows and timeline snapshots. Build every execution candidate's `ProfileAdmissionContext` from the latest strictly earlier settlement snapshot; never use the current event's result or after-state.

- [x] **Step 4: Replace replay-only admission branches**

Change `_execute_replay(..., admission_policy=...)` to evaluate all candidates in each opened-time group through `select_admitted_candidate`. Preserve existing concurrency, cooldown, three-loss ledger and `TwoStageStakeProgression`; use the decision's `allow_progression` and record policy hash/channel/code on each trade.

- [x] **Step 5: Write failing automatic-search and precision tests**

Assert exactly32 configurations, hard constraints without rounded values, deterministic ranking, no release policy when none passes, and distinct `aggregate_gates_passed`, `stability_proven`, `release_allowed` fields. Add a fixture where aggregate metrics pass but the seven-day forward requirement is absent; expected release remains blocked.

- [x] **Step 6: Implement search report**

Expose `search_profile_admission_policies(...)` which reuses one causal schedule/timeline/execution plan, evaluates each policy independently, calculates active OOS duration, orders/day, full-day metrics and three chronological windows, then ranks by:

```text
hard_constraints_passed desc
minimum_window_win_rate desc
abs(orders_per_day - 50) asc
maximum_drawdown asc
longest_loss_streak asc
policy_complexity asc
policy_hash asc
```

The CLI report must include all32 summaries, best candidate, exact failed gates, equivalence scope and stability status. Keep the baseline and structure equality executions unchanged.

- [x] **Step 7: Run replay tests and commit**

Run: `python3 -m unittest tests.test_daily_profile_replay tests.test_adaptive_profile_state -v`

Expected: PASS.

Commit: `feat: add causal profile admission search`

### Task 3: Runtime Uses The Frozen Policy

**Files:**
- Modify: `app/state.py`
- Modify: `app/server.py`
- Modify: `scripts/run.sh`
- Modify: `tests/test_state.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_packaging.py`

- [x] **Step 1: Write failing runtime candidate-selection tests**

Add state tests proving: a non-selected SHORT observation with ACTIVE N12 7-8 becomes an actionable FAST candidate; non-selected LONG remains observation-only; resident N12 above the configured maximum falls through; FAST/WATCH never consumes progression; the frozen decision context contains policy version/hash/channel/code.

- [x] **Step 2: Verify RED**

Run: `python3 -m unittest tests.test_state -k profile_admission -v`

Expected: FAIL because `_select_daily_profile_signal` only scans selected profiles and `_maybe_open_order_locked` blocks every unselected profile.

- [x] **Step 3: Inject policy and share candidate selection**

Add `profile_admission_policy: ProfileAdmissionPolicy | None` to `MonitorState`; default to baseline compatibility. Rewrite `_select_daily_profile_signal` to build contexts for all primary/observation candidates and call the shared selector. Attach the selected decision under `adaptive_profile_state["admission"]`; `_maybe_open_order_locked` trusts only a recomputed matching policy hash and permits FAST without setting `daily_profile_selected=True`.

- [x] **Step 4: Freeze audit values and failure behavior**

Include policy data in runtime config snapshots and decision traces. Invalid/missing/unknown policy input must fail startup or explicitly use baseline policy; it must never silently enable FAST. Keep entry structure `SHADOW_ONLY`.

- [x] **Step 5: Add Chinese CLI/startup parameters**

Add explicit server/run.sh options for admission enablement, LONG/SHORT resident N12 maxima and daily win-rate floors, FAST directions, N12 range and N20 EV minimum. Defaults on this feature branch represent the current candidate (LONG `7`, SHORT `9`, SHORT daily floor `0.625`, FAST SHORT `7-8`, EV `0`) but startup state reports `release_allowed=false` until forward stability is proven.

- [x] **Step 6: Run runtime and packaging regressions and commit**

Run: `python3 -m unittest tests.test_state tests.test_server tests.test_packaging -v`

Expected: PASS.

Run: `bash -n scripts/run.sh`

Expected: exit0.

Commit: `feat: apply frozen profile admission policy`

### Task 4: Real Replay, Documentation And Review

**Files:**
- Modify: `docs/release-handoff.md`
- Verify: all implementation and test files

- [x] **Step 1: Run copied-database search**

Run the existing explicit production command against `/private/tmp/monitor-replay.sqlite3`, adding the automatic profile admission search output path `/private/tmp/profile-admission-optimizer-report.json`. Record source SHA-256 before and after.

Expected: 4997 settled observations, zero leakage, unchanged SQLite hash, exactly32 evaluated policies and a deterministic best candidate.

- [x] **Step 2: Verify target and stability separately**

Confirm the report's raw counts, LONG/SHORT metrics, orders/day, EV, PnL, drawdown, daily distribution and OOS windows. Do not mark release allowed unless both aggregate and seven-full-day forward gates pass.

- [x] **Step 3: Update the single handoff**

Append one section to `docs/release-handoff.md` with policy hash, search grid, baseline/candidate metrics, failed stability evidence, exact replay command, explicit equivalence scope and `未部署` status.

- [x] **Step 4: Request independent code review**

Review the diff from `840076a` through HEAD for P1/P2 correctness, production/replay drift, future leakage, candidate ordering, progression credit, report precision and configuration safety. Resolve findings and rerun affected tests.

- [x] **Step 5: Run final verification**

Run:

```bash
python3 -m compileall -q app scripts tests
bash -n scripts/run.sh
node --check app/static/app.js
python3 -m unittest discover -s tests
git diff --check
git status --short
```

Expected: all commands pass and the branch is clean.

- [ ] **Step 6: Stop before release**

Report the automatically selected result and failed/pass gates. Do not deploy, push, merge, tag or clear orders.
