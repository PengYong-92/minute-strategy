# Profile Degradation Decision Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 精简10分钟订单决策流程，删除重叠的时段日内连亏暂停，并新增可重启恢复的完整画像实时退化冷却与基础金额试探机制。

**Architecture:** `OrderPolicy` 只负责机械准入，`MonitorState` 以候选解析、机械准入、风险守卫、金额决策和原子开单五个阶段编排。新增纯函数模块从正式订单推导画像退化状态；试探身份写入订单 JSON，因此无需新增数据库表即可跨重启恢复。

**Tech Stack:** Python 3 标准库、`dataclasses`、`unittest`、SQLite JSON payload、原生 HTML/CSS/JavaScript、Bash。

---

## File Map

- Create `app/profile_degradation_guard.py`: 完整画像退化状态机的配置、决策类型和纯函数。
- Create `tests/test_profile_degradation_guard.py`: 状态机边界、隔离和重启推导测试。
- Modify `app/order_policy.py`: 删除时段日内连亏和 `RISK_PAUSED`，仅保留机械准入。
- Modify `tests/test_order_policy.py`: 锁定机械准入职责。
- Modify `app/models.py`: 为信号和订单增加试探审计字段。
- Modify `app/simulator.py`: 将试探字段复制到订单，基础金额试探不消费滚单资格。
- Modify `tests/test_simulator.py`: 验证试探字段、基础金额和滚单资格。
- Modify `tests/test_storage.py`: 验证新字段持久化和旧订单默认兼容。
- Modify `app/state.py`: 精简开单编排，接入画像退化守卫并暴露状态。
- Modify `tests/test_state.py`: 锁定决策优先级、阻断、试探、跨画像和重启行为。
- Modify `app/server.py`: 增加唯一冷却参数并注入状态。
- Modify `scripts/run.sh`: 增加环境变量、中文帮助、命令行解析和参数转发。
- Modify `tests/test_packaging.py`: 验证配置转发、中文页面状态和前端格式化。
- Modify `app/static/index.html`: 增加画像实时守卫状态项。
- Modify `app/static/app.js`: 格式化和渲染画像守卫状态及订单试探标记。
- Modify `app/static/styles.css`: 复用现有状态颜色，仅补充必要的稳定布局规则。
- Modify `README.md`: 记录唯一参数及三类守卫的职责差异。
- Modify `docs/current-strategy.md`: 更新实际开单顺序和恢复规则。
- Modify `docs/release-handoff-2026-08-06-e824cf5.md`: 记录实现提交、验证结果和发布边界，不提前写部署结果。

### Task 1: Lock Mechanical Admission and Remove Segment-Day Pause

**Files:**
- Modify: `tests/test_order_policy.py:100-126`
- Modify: `app/order_policy.py:8-85`
- Modify: `app/state.py:515-565`
- Modify: `tests/test_state.py:2059-2096`

- [ ] **Step 1: Replace the old policy expectation with a failing responsibility test**

```python
def test_segment_losses_do_not_block_mechanical_admission(self):
    policy = OrderPolicy(max_open_orders=1, min_order_gap_ms=600_000)
    losses = [
        SimulatedOrder(
            id=idx + 1,
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="loss",
            entry_price=100.0,
            opened_at=1_800_000 + idx * 600_000,
            expires_at=2_400_000 + idx * 600_000,
            threshold_segment="WD-00",
            status="SETTLED",
            result="LOSS",
            exit_price=99.0,
            settled_at=2_400_000 + idx * 600_000,
            pnl=-10.0,
        )
        for idx in range(3)
    ]

    gate = policy.evaluate(signal(segment="WD-00"), kline(70), losses, None, set())

    self.assertTrue(gate.open_allowed)
    self.assertEqual(gate.code, "OPENED")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.test_order_policy.OrderPolicyTest.test_segment_losses_do_not_block_mechanical_admission -v`

Expected: FAIL because the current policy returns `RISK_PAUSED`.

- [ ] **Step 3: Remove risk state from the mechanical gate**

Change `OrderGate` to:

```python
@dataclass(frozen=True)
class OrderGate:
    code: str
    open_allowed: bool = False
    signal_key: tuple[int, int, str] | None = None
```

Delete the `risk_pause_reason` call, `RISK_PAUSED` return and the `risk_pause_reason` method. Keep the successful return:

```python
return OrderGate(code="OPENED", open_allowed=True, signal_key=signal_key)
```

Remove both `gate.risk_pause` reads from `MonitorState`. Clear `self.risk_pause` once at the start of `_maybe_open_order_locked` so a previous blocked decision cannot leak into a later successful decision; explicit block branches continue to set their own reason.

- [ ] **Step 4: Update the state characterization test**

Rename `test_risk_pause_after_three_segment_losses` to `test_segment_losses_are_left_to_explicit_risk_guards`, disable `ResultSequenceGuardConfig`, and assert `OPENED` plus an empty `risk_pause`. This proves the deleted behavior is intentional rather than an accidental regression.

- [ ] **Step 5: Run policy and affected state tests**

Run: `python3 -m unittest tests.test_order_policy tests.test_state.MonitorStateTest.test_segment_losses_are_left_to_explicit_risk_guards -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/order_policy.py app/state.py tests/test_order_policy.py tests/test_state.py
git commit -m "refactor: keep order policy mechanical"
```

### Task 2: Add the Pure Profile Degradation State Machine

**Files:**
- Create: `app/profile_degradation_guard.py`
- Create: `tests/test_profile_degradation_guard.py`

- [ ] **Step 1: Write failing state-machine tests**

Create a local order helper using `SimpleNamespace` and cover these exact cases:

```python
def order(order_id, result, settled_at, *, profile="p1", version="v1", status="SETTLED", probe=False, triggered_at=0):
    return SimpleNamespace(
        id=order_id,
        status=status,
        result=result,
        settled_at=settled_at,
        opened_at=max(0, settled_at - 600_000),
        profile_key=profile,
        daily_profile_version=version,
        profile_degradation_probe=probe,
        profile_degradation_triggered_at=triggered_at,
    )
```

Tests must assert:

```python
self.assertEqual(decision.status, "COOLDOWN")       # three trailing losses
self.assertEqual(decision.pause_until, 4_200_000)   # last loss + 60 minutes
self.assertEqual(decision.status, "RECOVERY_READY") # at exact pause boundary
self.assertFalse(decision.allow_progression)
self.assertEqual(decision.status, "RECOVERY_PENDING") # marked OPEN probe
self.assertEqual(decision.status, "NORMAL")         # probe WIN resets streak
self.assertEqual(decision.status, "COOLDOWN")       # probe LOSS restarts cooldown
```

Also test `cooldown_minutes=0`, fewer than three losses, a preceding win, another profile, another DPS version, future settlements and shuffled input order.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_profile_degradation_guard -v`

Expected: ERROR because `app.profile_degradation_guard` does not exist.

- [ ] **Step 3: Implement configuration and decision types**

```python
PROFILE_DEGRADATION_LOSS_STREAK = 3
MINUTE_MS = 60_000

@dataclass(frozen=True)
class ProfileDegradationGuardConfig:
    cooldown_minutes: int = 60

    def normalized(self):
        return ProfileDegradationGuardConfig(
            cooldown_minutes=max(0, int(self.cooldown_minutes))
        )

@dataclass(frozen=True)
class ProfileDegradationGuardDecision:
    status: str = "NORMAL"
    blocked: bool = False
    allow_progression: bool = True
    profile_key: str = ""
    daily_profile_version: str = ""
    consecutive_losses: int = 0
    last_loss_settled_at: int = 0
    pause_until: int = 0
    probe_order_id: int = 0
    triggered_at: int = 0
    reason: str = ""
```

- [ ] **Step 4: Implement deterministic evaluation**

`evaluate_profile_degradation_guard(...)` must:

1. Return `DISABLED` when normalized cooldown is zero.
2. Return `NOT_APPLICABLE` without an exact profile key and DPS version.
3. Filter by exact profile/version and `settled_at <= current_time`.
4. Sort settled orders by `(settled_at, id)` and count trailing losses.
5. Return `NORMAL` below three losses.
6. Detect an open marked probe for the same trigger and return `RECOVERY_PENDING`.
7. Return `COOLDOWN` while `current_time < pause_until`.
8. Return `RECOVERY_READY` at or after the boundary with `allow_progression=False`.

- [ ] **Step 5: Run the pure-function tests**

Run: `python3 -m unittest tests.test_profile_degradation_guard -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/profile_degradation_guard.py tests/test_profile_degradation_guard.py
git commit -m "feat: add profile degradation state machine"
```

### Task 3: Persist Probe Identity in Orders

**Files:**
- Modify: `app/models.py:82-97,130-155`
- Modify: `app/simulator.py:102-162`
- Modify: `tests/test_simulator.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write failing simulator audit test**

```python
def test_profile_probe_fields_are_copied_to_base_order(self):
    simulator = AccountSimulator(enable_stake_progression=True, stake_progression_activated_at=0)
    first = simulator.open_order(signal(timeframe_minutes=1), 100.0, 0)
    simulator.settle_expired_orders(60_000, 101.0)
    probe_signal = replace(
        signal(timeframe_minutes=1),
        profile_degradation_probe=True,
        profile_degradation_triggered_at=60_000,
    )

    probe, consumed = simulator.open_order_with_credit(
        probe_signal, 101.0, 120_000, allow_progression=False
    )

    self.assertTrue(probe.profile_degradation_probe)
    self.assertEqual(probe.profile_degradation_triggered_at, 60_000)
    self.assertEqual(probe.stake, simulator.stake)
    self.assertEqual(probe.stake_progression_step, 1)
    self.assertIsNone(consumed)
    self.assertEqual(simulator.stake_progression.credits[0].status, "PENDING")
```

- [ ] **Step 2: Run the simulator test and verify RED**

Run: `python3 -m unittest tests.test_simulator.SimulatorTest.test_profile_probe_fields_are_copied_to_base_order -v`

Expected: FAIL because `Signal` lacks the audit fields.

- [ ] **Step 3: Add backward-compatible model fields**

Add to both `Signal` and `SimulatedOrder`:

```python
profile_degradation_probe: bool = False
profile_degradation_triggered_at: int = 0
```

Copy both values in `AccountSimulator.open_order_with_credit`.

- [ ] **Step 4: Add storage compatibility tests**

Persist and reload a marked probe, then assert both values survive. Insert a legacy payload without these keys and assert the restored order uses `False` and `0` through dataclass defaults.

Add a simulator test that settles a winning profile probe and asserts it generates a new pending two-stage credit. This distinguishes profile recovery from `wave_guard_mode="RECOVERY"`, whose winning order intentionally does not generate credit.

- [ ] **Step 5: Run simulator and storage tests**

Run: `python3 -m unittest tests.test_simulator tests.test_storage -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/simulator.py tests/test_simulator.py tests/test_storage.py
git commit -m "feat: persist profile recovery probes"
```

### Task 4: Refactor the Order Decision Orchestrator Without New Behavior

**Files:**
- Modify: `app/state.py:494-656`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Add characterization coverage for precedence and side effects**

Add focused tests proving:

- `DAILY_PROFILE_NOT_SELECTED` precedes mechanical admission.
- `HOLD_OPEN_ORDER` and `COOLDOWN` precede risk guards.
- `RESULT_SEQUENCE_GUARD_BLOCKED` precedes `ROLLING_EDGE_BLOCKED`.
- a successful open writes the atomic order before observation, snapshot and Webhook.
- storage failure rolls back the in-memory order and does not send Webhook.

Use `RecordingStorage.atomic_calls`, `RecordingWebhook.calls`, `snapshot()["observations"]` and simulator order count rather than mocking internal helper methods.

- [ ] **Step 2: Run characterization tests before refactoring**

Run: `python3 -m unittest tests.test_state -v`

Expected: PASS, establishing the pre-refactor baseline after Task 1's intentional `RISK_PAUSED` removal.

- [ ] **Step 3: Extract a single block helper**

```python
def _block_order(self, signal, latest, code, reason, *, should_observe):
    if should_observe:
        self._record_observation(signal, latest, code)
    self.risk_pause = reason
    return code
```

Use it for daily profile, wave direction, wave batch, direction sequence, rolling edge and enabled legacy profile guard blocks.

- [ ] **Step 4: Extract candidate admission**

Move order-policy evaluation and the legacy observation-promotion fallback into `_admit_order_candidate`. It returns the possibly replaced signal and `OrderGate`; it must update `selected_signal` only when the signal actually changes.

- [ ] **Step 5: Extract atomic open execution**

Move `open_order_with_credit`, atomic persistence, rollback, observation, snapshot, Webhook and runtime-key updates into `_execute_open_order`. Pass `allow_progression` explicitly; do not recompute guards inside this method.

- [ ] **Step 6: Keep `_maybe_open_order_locked` as ordered orchestration**

The method should read as candidate metadata, explicit blocks, admission, guard evaluation and execution. Do not introduce a generic plugin framework or configurable guard list.

- [ ] **Step 7: Run state, policy, simulator and storage tests**

Run: `python3 -m unittest tests.test_state tests.test_order_policy tests.test_simulator tests.test_storage -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/state.py tests/test_state.py
git commit -m "refactor: stage the order decision flow"
```

### Task 5: Integrate the Profile Degradation Guard

**Files:**
- Modify: `app/state.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing cooldown integration test**

Build three settled losses with the same `profile_key` and `daily_profile_version`, use a daily-selected signal, disable the direction and rolling guards for isolation, and assert:

```python
self.assertEqual(decision, "PROFILE_DEGRADATION_BLOCKED")
self.assertEqual(snapshot["profile_degradation_guard"]["status"], "COOLDOWN")
self.assertEqual(snapshot["profile_degradation_guard"]["consecutive_losses"], 3)
self.assertEqual(snapshot["observations"][0]["source_decision"], "PROFILE_DEGRADATION_BLOCKED")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python3 -m unittest tests.test_state.MonitorStateTest.test_profile_degradation_cooldown_blocks_exact_profile -v`

Expected: FAIL because the state does not evaluate the new guard.

- [ ] **Step 3: Inject config and initialize status**

Add `profile_degradation_guard_config` to `MonitorState.__init__`, normalize it once, initialize an empty status, and reset that status in `reset_symbol`.

- [ ] **Step 4: Evaluate after wave batch and before direction sequence**

Call the pure evaluator with `self.simulator.orders`, current close time, `signal.profile_key` and `signal.daily_profile_version`. For `COOLDOWN` or `RECOVERY_PENDING`, use `_block_order` with code `PROFILE_DEGRADATION_BLOCKED`. For `RECOVERY_READY`, replace the signal with:

```python
signal = replace(
    signal,
    reason=f"{signal.reason}；画像退化试探单",
    profile_degradation_probe=True,
    profile_degradation_triggered_at=decision.triggered_at,
)
```

Combine progression permission as:

```python
allow_progression = batch_decision.allow_progression and decision.allow_progression
```

- [ ] **Step 5: Add integration cases**

Add tests for exact cooldown boundary, a base probe preserving a pending credit, blocking a second probe while the first is open, allowing another profile, probe win recovery, probe loss recooling and restart from SQLite producing the same status.

- [ ] **Step 6: Expose API state**

Add `profile_degradation_guard` to `snapshot()` and assert the server JSON contains `enabled`, `status`, `cooldown_minutes`, `profile_key`, `pause_until` and `probe_order_id`.

- [ ] **Step 7: Run focused integration tests**

Run: `python3 -m unittest tests.test_profile_degradation_guard tests.test_state tests.test_server -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/state.py tests/test_state.py tests/test_server.py
git commit -m "feat: guard degraded live profiles"
```

### Task 6: Add the Single Startup Parameter

**Files:**
- Modify: `app/server.py:420-570`
- Modify: `scripts/run.sh:20-60,61-180,300-580,700-770`
- Modify: `tests/test_packaging.py:220-390`

- [ ] **Step 1: Write failing forwarding assertions**

Set `PROFILE_DEGRADATION_COOLDOWN_MINUTES=75` in the fake-launch environment and assert:

```python
self.assertEqual(
    args[args.index("--profile-degradation-cooldown-minutes") + 1],
    "75",
)
```

Also assert the default forwarded value is `60` and the usage text contains `0关闭`.

- [ ] **Step 2: Run packaging test and verify RED**

Run: `python3 -m unittest tests.test_packaging.PackagingTest.test_run_script_handles_empty_extra_args_on_macos_bash -v`

Expected: FAIL because the argument is absent.

- [ ] **Step 3: Add server parser and state injection**

```python
parser.add_argument(
    "--profile-degradation-cooldown-minutes",
    type=int,
    default=int(os.getenv("PROFILE_DEGRADATION_COOLDOWN_MINUTES", "60")),
    help="完整画像连续亏损3单后的冷却分钟数，0关闭，默认: 60",
)
```

Construct `ProfileDegradationGuardConfig` with this value and pass it to `MonitorState`.

- [ ] **Step 4: Add one Bash variable, option and forwarding line**

Use only `PROFILE_DEGRADATION_COOLDOWN_MINUTES`; do not add enable, sample, win-rate or EV parameters. Add both `--profile-degradation-cooldown-minutes VALUE` and `--profile-degradation-cooldown-minutes=VALUE` parsing forms.

- [ ] **Step 5: Run packaging and shell syntax tests**

Run: `python3 -m unittest tests.test_packaging -v`

Run: `bash -n scripts/run.sh`

Expected: PASS for both.

- [ ] **Step 6: Commit**

```bash
git add app/server.py scripts/run.sh tests/test_packaging.py
git commit -m "feat: configure profile degradation cooldown"
```

### Task 7: Display the Guard and Probe Audit State

**Files:**
- Modify: `app/static/index.html:85-145`
- Modify: `app/static/app.js:60-155,450-515,920-970`
- Modify: `app/static/styles.css`
- Modify: `tests/test_packaging.py:13-85,103-155`

- [ ] **Step 1: Write failing dashboard contract assertions**

Assert the HTML contains `profile-degradation-guard-status`, the script contains `fmtProfileDegradationGuard`, and the order renderer contains `profile_degradation_probe`.

- [ ] **Step 2: Run dashboard tests and verify RED**

Run: `python3 -m unittest tests.test_packaging.PackagingTest.test_dashboard_contains_strategy_and_monitoring_sections -v`

Expected: FAIL because the status element is absent.

- [ ] **Step 3: Add concise status formatting**

Render:

- `DISABLED`: `关闭`
- `NORMAL` / `NOT_APPLICABLE`: `正常`
- `COOLDOWN`: `冷却 · <画像> · 连亏N`
- `RECOVERY_READY`: `待试探 · <画像>`
- `RECOVERY_PENDING`: `试探待结算 · 订单#ID`

Use existing `status-good`, `status-warn`, `status-risk` and `status-muted` classes. Do not add another explanatory paragraph or nested card.

- [ ] **Step 4: Mark probe orders in the existing strategy cell**

Append a compact `基础试探` label only when `order.profile_degradation_probe` is true. Do not add another table column.

- [ ] **Step 5: Run JavaScript and packaging verification**

Run: `node --check app/static/app.js`

Run: `python3 -m unittest tests.test_packaging -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/static/index.html app/static/app.js app/static/styles.css tests/test_packaging.py
git commit -m "feat: show profile degradation guard"
```

### Task 8: Document Stable Runtime Semantics

**Files:**
- Modify: `README.md`
- Modify: `docs/current-strategy.md`
- Modify: `docs/release-handoff-2026-08-06-e824cf5.md`

- [ ] **Step 1: Update the runtime parameter tables**

Document exactly one parameter:

```text
PROFILE_DEGRADATION_COOLDOWN_MINUTES=60
```

State that `0` disables it and that the loss threshold is fixed at three.

- [ ] **Step 2: Document guard responsibility boundaries**

Use one compact table:

| 层级 | 范围 | 触发 | 恢复 |
|---|---|---|---|
| 每日画像 | 完整画像/7天观察 | 60%与EV | 次日重评 |
| 实时画像退化 | 完整画像/当前DPS实单 | 固定连续亏损3单 | 配置冷却+基础试探 |
| 方向序列 | LONG或SHORT实单 | 连亏阈值 | 方向冷却 |
| 滚动优势 | 现有滚动key | 胜率/EV退化 | 滚动样本恢复 |

Explicitly state that the old segment-day `RISK_PAUSED` logic was removed.

- [ ] **Step 3: Add a handoff section without claiming deployment**

Record commit IDs, tests, config defaults and the intentional behavior change. Label server release directory, restart boundary and production sample boundary as “未部署” until a separate deployment command is approved and completed.

- [ ] **Step 4: Check documentation diff and commit**

Run: `git diff --check`

Expected: no output and exit code 0.

```bash
git add README.md docs/current-strategy.md docs/release-handoff-2026-08-06-e824cf5.md
git commit -m "docs: explain profile degradation guard"
```

### Task 9: Full Verification and Change Audit

**Files:**
- Verify all modified files

- [ ] **Step 1: Run focused decision tests**

Run: `python3 -m unittest tests.test_order_policy tests.test_profile_degradation_guard tests.test_result_sequence_guard tests.test_rolling_edge tests.test_simulator tests.test_storage tests.test_state tests.test_server tests.test_packaging -v`

Expected: all tests PASS.

- [ ] **Step 2: Run the complete suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests PASS with zero failures and zero errors.

- [ ] **Step 3: Run syntax checks**

Run: `python3 -m compileall -q app scripts tests`

Run: `node --check app/static/app.js`

Run: `bash -n scripts/run.sh`

Expected: all commands exit 0.

- [ ] **Step 4: Audit the final diff**

Run: `git diff --check`

Run: `git status --short`

Run: `git log --oneline -12`

Confirm:

- no new 30-minute order path;
- `DAILY_PROFILE_MIN_WIN_RATE` remains `0.60`;
- `MAX_OPEN_ORDERS` remains `2`;
- only one profile-degradation startup parameter exists;
- old segment-day `RISK_PAUSED` code is absent;
- deployment and order clearing have not occurred.

- [ ] **Step 5: Write the final implementation commit only if verification changes docs**

If verification requires no edits, do not create an empty commit. If handoff verification counts were updated, commit only those factual counts:

```bash
git add docs/release-handoff-2026-08-06-e824cf5.md
git commit -m "docs: record profile guard verification"
```
