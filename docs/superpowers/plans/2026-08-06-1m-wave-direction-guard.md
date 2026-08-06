# 1分钟波段方向与画像权限修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 阻止每日画像和滚单资格绕过实时信号，使用单一1分钟波段状态约束方向，并把总未结订单降为2、增加可恢复的波段批次守卫。

**Architecture:** 新增纯函数 `app/wave_state.py` 计算因果波段快照，新增 `app/wave_batch_guard.py` 从已持久化订单恢复批次和全局冷却状态。`MonitorState` 按“实时信号 -> 波段否决 -> 每日画像验证 -> 批次/结算守卫 -> 金额分配”顺序执行；订单JSON载荷保存波段和守卫元数据，页面只展示运行状态。

**Tech Stack:** Python 3 dataclass、SQLite JSON载荷、标准库 `unittest`、原生HTML/CSS/JavaScript。

---

## 文件结构

- Create: `app/wave_state.py` — 只负责从已闭合1分钟K线生成波段快照和方向许可。
- Create: `app/wave_batch_guard.py` — 只负责从订单序列计算批次锁定、全局冷却和恢复模式。
- Create: `tests/test_wave_state.py` — 波段因果性、转折确认和方向映射测试。
- Create: `tests/test_wave_batch_guard.py` — 首亏锁定、全亏批次、恢复和重启恢复测试。
- Modify: `app/models.py` — Signal、SimulatedOrder、ObservationSignal增加波段/批次字段；修复 `actionable`。
- Modify: `app/state.py` — 调整画像选择顺序，接入波段和批次守卫，保存运行状态。
- Modify: `app/simulator.py` — 把波段元数据复制到订单，并支持禁用当前订单的滚单消费。
- Modify: `app/stake_progression.py` — 提供显式取消待用资格能力并保持可持久化状态。
- Modify: `app/storage.py` — 兼容旧JSON载荷，保存波段和批次元数据。
- Modify: `app/server.py`, `scripts/run.sh` — 默认总未结订单改为2。
- Modify: `app/static/index.html`, `app/static/app.js`, `app/static/styles.css` — 展示波段和批次守卫状态。
- Modify: `tests/test_state.py`, `tests/test_simulator.py`, `tests/test_storage.py`, `tests/test_server.py`, `tests/test_packaging.py` — 集成、持久化和页面契约测试。
- Modify: `docs/current-strategy.md` — 记录新决策顺序和启动默认值。

### Task 1: 修复画像覆盖WAIT和评分阈值

**Files:**
- Modify: `app/models.py`
- Modify: `app/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: 将现有“画像可直接开WAIT”测试改成失败测试**

把 `test_daily_selected_observation_profile_opens_without_static_score_or_short_whitelist` 改为断言：原始WAIT保持WAIT、`daily_profile_selected=False`、决策为 `BELOW_THRESHOLD` 或 `DAILY_PROFILE_NOT_SELECTED`、订单数为0。把研究观察候选测试改为断言WAIT候选不能执行。

```python
self.assertEqual(selected.direction, "WAIT")
self.assertFalse(selected.daily_profile_selected)
self.assertNotEqual(decision, "OPENED")
self.assertEqual(state.simulator.orders, [])
```

- [ ] **Step 2: 增加已过线同方向信号可被画像验证的测试**

```python
primary = Signal(
    "SHORT", 10, "A", "实时SHORT已过线", 100.0, now,
    score=-84.0, threshold=79.0, threshold_segment="WD-22",
    strategy_family="short_observe", strategy_tag="generic_short_observe",
    observe_direction="SHORT", observe_only=False,
)
selected, required = state._select_daily_profile_signal(primary, [], now)
self.assertTrue(required)
self.assertEqual(selected.direction, "SHORT")
self.assertTrue(selected.daily_profile_selected)
self.assertEqual(selected.score, -84.0)
```

- [ ] **Step 3: 运行聚焦测试并确认失败**

Run: `.venv/bin/python -m unittest tests.test_state.MonitorStateTest.test_daily_selected_observation_profile_opens_without_static_score_or_short_whitelist tests.test_state.MonitorStateTest.test_daily_selector_can_execute_matching_research_observation_candidate -v`

Expected: FAIL，显示WAIT仍被改写为方向。

- [ ] **Step 4: 修复可执行性和画像选择**

将 `Signal.actionable` 改为仅由实时方向和实时评分决定：

```python
@property
def actionable(self) -> bool:
    return self.direction in {"LONG", "SHORT"} and abs(self.score) >= self.threshold
```

修改 `_select_daily_profile_signal`：只检查 `primary_signal`；若其 `direction` 不是LONG/SHORT或不可执行，原样返回；匹配键使用 `primary_signal.direction`，不使用 `observe_direction`；`observation_candidates` 仅保留兼容参数，不参与选择。

- [ ] **Step 5: 运行画像相关测试**

Run: `.venv/bin/python -m unittest tests.test_state.MonitorStateTest -v`

Expected: PASS。

- [ ] **Step 6: 提交P0修复**

```bash
git add app/models.py app/state.py tests/test_state.py
git commit -m "fix: stop profiles promoting wait signals"
```

### Task 2: 实现纯函数1分钟波段识别

**Files:**
- Create: `app/wave_state.py`
- Create: `tests/test_wave_state.py`
- Modify: `app/models.py`

- [ ] **Step 1: 编写波段识别失败测试**

覆盖上涨、下跌、震荡高低位、幅度不足、转折一次待确认、转折两次确认、追加未来K线不改变历史快照。

```python
snapshot = analyze_wave(klines, previous=None)
self.assertEqual(snapshot.raw_state, "UP_LEG")
self.assertEqual(snapshot.state, "TURN_UP")
self.assertEqual(snapshot.confirmations, 1)
self.assertEqual(snapshot.allowed_directions, ())
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `.venv/bin/python -m unittest tests.test_wave_state -v`

Expected: ERROR，`app.wave_state` 尚不存在。

- [ ] **Step 3: 实现 `WaveSnapshot` 和波段算法**

在 `app/wave_state.py` 定义：

```python
@dataclass(frozen=True)
class WaveSnapshot:
    state: str
    raw_state: str
    window: int
    efficiency: float
    direction_ratio: float
    atr_strength: float
    range_position: float
    confirmations: int
    confirmed_at: int
    allowed_directions: tuple[str, ...]
```

实现 `analyze_wave(klines, previous=None, window=8, atr_window=14, min_efficiency=0.35, min_direction_ratio=0.60, min_atr_strength=0.50)`。只读取传入序列；计算净变化、路径效率、同方向比例、ATR强度和区间位置。趋势原始状态连续两次一致后确认；范围状态立即生效；趋势切换确认期间返回TURN状态且不允许方向。

- [ ] **Step 4: 在模型中加入波段元数据字段**

在 `Signal`、`SimulatedOrder`、`ObservationSignal` 尾部增加带默认值字段：

```python
wave_state: str = "UNKNOWN"
wave_raw_state: str = "UNKNOWN"
wave_window: int = 0
wave_efficiency: float = 0.0
wave_direction_ratio: float = 0.0
wave_atr_strength: float = 0.0
wave_confirmations: int = 0
wave_confirmed_at: int = 0
wave_batch_id: str = ""
wave_guard_mode: str = "NORMAL"
```

- [ ] **Step 5: 运行波段和模型测试**

Run: `.venv/bin/python -m unittest tests.test_wave_state tests.test_simulator -v`

Expected: PASS。

- [ ] **Step 6: 提交波段纯函数**

```bash
git add app/wave_state.py app/models.py tests/test_wave_state.py
git commit -m "feat: add causal one minute wave state"
```

### Task 3: 将波段方向接入实时信号和画像之间

**Files:**
- Modify: `app/state.py`
- Modify: `app/simulator.py`
- Test: `tests/test_state.py`
- Test: `tests/test_simulator.py`

- [ ] **Step 1: 编写波段方向集成失败测试**

测试SHORT实时信号在 `UP_LEG` 被改成WAIT并返回 `WAVE_DIRECTION_BLOCKED`；LONG在 `UP_LEG` 保持方向；TURN和RANGE_MID全部阻止；画像不能重新恢复被波段否决的方向。

```python
guarded = state._apply_wave_guard(short_signal, up_wave)
self.assertEqual(guarded.direction, "WAIT")
self.assertIn("波段方向冲突", guarded.reason)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m unittest tests.test_state.MonitorStateTest -v`

Expected: FAIL，缺少波段状态和守卫方法。

- [ ] **Step 3: 在 `MonitorState` 计算并应用波段**

在每次K线更新时调用 `analyze_wave`，保存 `self.wave_state`。为实时Signal复制波段字段并生成批次ID：

```python
batch_id = f"{wave.confirmed_at}|{wave.state}|{signal.direction}|{signal.threshold_segment}|{profile_version}"
```

先应用 `_apply_wave_guard`，再调用 `_select_daily_profile_signal`。方向不在 `allowed_directions` 时将方向改为WAIT、`observe_only=True`，原因追加“波段方向冲突”，并保持原始观察方向用于影子记录。

- [ ] **Step 4: 模拟器复制波段字段到订单**

扩展 `order_fields`，复制Task 2新增的全部波段字段和 `wave_guard_mode`。

- [ ] **Step 5: 运行状态与模拟器测试**

Run: `.venv/bin/python -m unittest tests.test_state tests.test_simulator -v`

Expected: PASS。

- [ ] **Step 6: 提交波段接入**

```bash
git add app/state.py app/simulator.py tests/test_state.py tests/test_simulator.py
git commit -m "feat: gate live directions by one minute waves"
```

### Task 4: 将默认总未结订单降为2

**Files:**
- Modify: `app/state.py`
- Modify: `app/simulator.py`
- Modify: `app/server.py`
- Modify: `scripts/run.sh`
- Modify: `tests/test_state.py`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: 把默认值测试改为2**

```python
self.assertEqual(state.order_policy.max_open_orders, 2)
self.assertEqual(state.snapshot()["order_policy"]["max_open_orders"], 2)
```

同时断言 `scripts/run.sh --help` 和默认参数输出为2。

- [ ] **Step 2: 运行测试并确认旧默认值导致失败**

Run: `.venv/bin/python -m unittest tests.test_state.MonitorStateTest.test_default_order_policy_supports_five_open_orders_two_minutes_apart tests.test_packaging.PackagingTest -v`

Expected: FAIL，实际默认值为5。

- [ ] **Step 3: 修改所有生产默认值**

将 `MonitorState`、`AccountSimulator`、`app/server.py` 的 `MAX_OPEN_ORDERS` 环境变量默认值和 `scripts/run.sh` 默认/帮助文本统一改为2；显式传参仍可覆盖。

- [ ] **Step 4: 运行默认配置测试**

Run: `.venv/bin/python -m unittest tests.test_state tests.test_packaging tests.test_server -v`

Expected: PASS。

- [ ] **Step 5: 提交并发修改**

```bash
git add app/state.py app/simulator.py app/server.py scripts/run.sh tests/test_state.py tests/test_packaging.py tests/test_server.py
git commit -m "feat: limit default open orders to two"
```

### Task 5: 实现可恢复的波段批次守卫

**Files:**
- Create: `app/wave_batch_guard.py`
- Create: `tests/test_wave_batch_guard.py`
- Modify: `app/state.py`
- Modify: `app/models.py`

- [ ] **Step 1: 编写批次守卫失败测试**

覆盖：无亏损正常、当前批次第一笔亏损后锁定、同批次一赢一亏仍锁定、两笔全亏标记失败、60分钟内两个全亏批次触发60分钟全局冷却、冷却后只允许一个恢复单、恢复赢解锁、恢复亏重新冷却、用恢复订单重新构造状态结果相同。

```python
decision = evaluate_wave_batch_guard(orders, current_time=now, batch_id="B2")
self.assertTrue(decision.blocked)
self.assertEqual(decision.status, "BATCH_LOCKED")
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `.venv/bin/python -m unittest tests.test_wave_batch_guard -v`

Expected: ERROR，`app.wave_batch_guard` 尚不存在。

- [ ] **Step 3: 实现纯决策模块**

定义不可变配置和结果：

```python
@dataclass(frozen=True)
class WaveBatchGuardConfig:
    failed_batches: int = 2
    failed_window_minutes: int = 60
    cooldown_minutes: int = 60

@dataclass(frozen=True)
class WaveBatchGuardDecision:
    blocked: bool
    status: str
    reason: str
    recovery_mode: bool
    failed_batch_count: int
    pause_until: int
```

`evaluate_wave_batch_guard` 只读取订单中的 `wave_batch_id`、`wave_guard_mode`、状态、结果和时间；同批次任一已结算亏损即锁定补单。两个各自最多2单且全部亏损的批次在窗口内触发全局冷却。冷却到期后如无未结恢复单，返回 `recovery_mode=True`；有未结恢复单则阻止其他订单。

- [ ] **Step 4: 在开单流水线接入批次守卫**

在结算序列守卫之前调用批次守卫。被阻止时记录 `WAVE_BATCH_GUARD_BLOCKED`；恢复订单把 `wave_guard_mode` 标记为 `RECOVERY`。状态快照增加 `wave_batch_guard`。

- [ ] **Step 5: 运行批次和状态测试**

Run: `.venv/bin/python -m unittest tests.test_wave_batch_guard tests.test_state -v`

Expected: PASS。

- [ ] **Step 6: 提交批次守卫**

```bash
git add app/wave_batch_guard.py app/models.py app/state.py tests/test_wave_batch_guard.py tests/test_state.py
git commit -m "feat: add restart safe wave batch guard"
```

### Task 6: 限制恢复状态的两阶段金额叠加

**Files:**
- Modify: `app/stake_progression.py`
- Modify: `app/simulator.py`
- Modify: `app/state.py`
- Modify: `app/storage.py`
- Test: `tests/test_stake_progression.py`
- Test: `tests/test_simulator.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: 编写恢复状态金额失败测试**

测试恢复订单存在待用资格时仍使用10U且资格不被消费；恢复订单盈利不生成资格；进入批次锁定时所有PENDING资格变为CANCELLED并保存；正常新波段10U赢单仍生成18U资格。

- [ ] **Step 2: 运行测试并确认恢复订单仍会消费资格**

Run: `.venv/bin/python -m unittest tests.test_stake_progression tests.test_simulator tests.test_storage -v`

Expected: FAIL，恢复模式尚未传入金额状态机。

- [ ] **Step 3: 为模拟器增加基础金额强制模式**

修改 `open_order_with_credit(..., allow_progression: bool = True)`。`allow_progression=False` 时直接使用基础 `OrderTerms`，不调用 `assign`。修改结算函数，`wave_guard_mode == "RECOVERY"` 的一级订单盈利不调用生成资格逻辑，或在状态机调用中显式传入 `allow_credit=False`。

- [ ] **Step 4: 锁定时原子取消并保存待用资格**

复用 `cancel_pending()`，由 `MonitorState` 在首次进入锁定/冷却状态时调用；通过现有 `save_stake_progression_credit` 保存每个CANCELLED资格。重复进入相同状态不得产生额外变更。

- [ ] **Step 5: 验证旧JSON载荷和重启恢复**

在 `tests/test_storage.py` 保存不含波段字段的旧订单JSON并加载，断言新增字段使用默认值；保存新订单后加载，断言批次ID和恢复模式保留。

- [ ] **Step 6: 运行金额与存储测试**

Run: `.venv/bin/python -m unittest tests.test_stake_progression tests.test_simulator tests.test_storage tests.test_state -v`

Expected: PASS。

- [ ] **Step 7: 提交恢复金额约束**

```bash
git add app/stake_progression.py app/simulator.py app/state.py app/storage.py tests/test_stake_progression.py tests/test_simulator.py tests/test_storage.py tests/test_state.py
git commit -m "feat: isolate progression from recovery orders"
```

### Task 7: API、数据库快照和页面展示

**Files:**
- Modify: `app/state.py`
- Modify: `app/order_profile.py`
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/styles.css`
- Modify: `tests/test_server.py`
- Modify: `tests/test_monitor_db_analysis.py`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: 编写API和页面契约失败测试**

断言 `/api/state` 含 `wave_state`、`wave_batch_guard`；订单入口快照含波段字段；HTML含 `wave-state-status`；JavaScript含 `fmtWaveState` 和 `fmtWaveBatchGuard`。

- [ ] **Step 2: 运行测试并确认字段缺失**

Run: `.venv/bin/python -m unittest tests.test_server tests.test_monitor_db_analysis tests.test_packaging -v`

Expected: FAIL，波段状态尚未输出和渲染。

- [ ] **Step 3: 输出波段状态和入口快照**

在 `snapshot()` 和 `_order_entry_snapshot()` 返回波段快照、批次状态；`order_profile.sample_from_entry_snapshot` 读取新增字段并对旧载荷使用默认值。

- [ ] **Step 4: 增加紧凑页面状态项**

在现有状态网格增加“1分钟波段”和“波段守卫”两项。格式示例：

```javascript
function fmtWaveState(wave) {
  if (!wave) return "-";
  return `${wave.state} · 允许${(wave.allowed_directions || []).join("/") || "无"} · 确认${wave.confirmations || 0}`;
}
```

批次守卫使用现有 `status-good/status-risk` 样式，不创建新的装饰卡片或嵌套卡片。

- [ ] **Step 5: 运行API和页面测试**

Run: `.venv/bin/python -m unittest tests.test_server tests.test_monitor_db_analysis tests.test_packaging -v`

Expected: PASS。

- [ ] **Step 6: 提交状态展示**

```bash
git add app/state.py app/order_profile.py app/static/index.html app/static/app.js app/static/styles.css tests/test_server.py tests/test_monitor_db_analysis.py tests/test_packaging.py
git commit -m "feat: expose wave and batch guard status"
```

### Task 8: 文档和全量验证

**Files:**
- Modify: `docs/current-strategy.md`

- [ ] **Step 1: 更新当前策略文档**

记录新的决策顺序、波段算法、画像权限、默认并发2、批次守卫、恢复10U和不回测约束。删除“画像可将观察信号直接提升为订单”的旧描述，并将启动示例 `--max-open-orders` 改为2。

- [ ] **Step 2: 扫描冲突描述和旧默认值**

Run: `rg -n "max.open.orders.*5|最多5|观察画像放行|画像.*提升|30m偏向|30分钟偏向" docs/current-strategy.md scripts/run.sh app/server.py`

Expected: 不再存在生产默认5或画像覆盖WAIT的描述；历史章节若保留旧值必须明确标注“历史行为”。

- [ ] **Step 3: 运行格式和聚焦测试**

Run: `git diff --check`

Expected: 无输出。

Run: `.venv/bin/python -m unittest tests.test_wave_state tests.test_wave_batch_guard tests.test_state tests.test_simulator tests.test_storage tests.test_server tests.test_packaging -v`

Expected: PASS。

- [ ] **Step 4: 运行全量单元测试**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: 全部PASS。不得运行 `scripts/replay_*`、`app/backtest.py` 或任何历史数据回放命令。

- [ ] **Step 5: 检查最终差异和仓库状态**

Run: `git status --short`

Expected: 仅包含本计划涉及的文件，没有报告、回放结果或临时数据库。

- [ ] **Step 6: 提交文档和最终修正**

```bash
git add docs/current-strategy.md
git commit -m "docs: document one minute wave strategy"
```

