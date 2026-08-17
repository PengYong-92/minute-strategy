# Adaptive Resident Profiles And Entry Structure Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不部署、不清空订单、不改变价格结构影子模式下，建立可复现的完整决策记录、受3GiB硬上限保护的SQLite存储、自适应常驻画像，以及严格因果的1分钟价格结构影子。

**Architecture:** 先以`DECISION_CONTEXT_V2`和增量Schema建立唯一审计事实源，再将7天/14天资格与N12/N20即时状态接入现有完整画像键；价格结构作为`ENTRY_STRUCTURE_SHADOW_V1`只读附加到同一决策上下文。所有行为变更先由纯函数和因果回放验证，存储通过事务包保证订单、观察、上下文与审计一致；页面只消费兼容API，不创建重复分析卡片。

**Tech Stack:** Python 3标准库（`dataclasses`、`hashlib`、`json`、`sqlite3`、`unittest`）、现有HTTP服务与原生JavaScript、SQLite、现有1分钟WebSocket闭合K线数据流。

---

## Scope And Invariants

- 实施分支固定为`feature/adaptive-resident-profiles`，基线为`main@2750f10`。
- 本计划结束时不合并`main`、不创建tag、不打包、不上传服务器、不清空线上或本地模拟订单。
- 交易周期仍只有10分钟；价格结构只读取已闭合1分钟K线，不增加4小时、日线或30分钟逻辑。
- `ENTRY_STRUCTURE_SHADOW_V1`不得改变`order_decision`、方向、评分、阈值、守卫、金额、并发、冷却、滚单或Webhook。
- 自适应画像是本计划唯一允许改变正式准入的模块；变更必须通过计划末尾的样本外门槛。
- 历史SQLite只做增量建表和加列，不回填、不重算、不删除、不`VACUUM`。
- 完整动态输入只存于`decision_contexts`一次；订单、观察、入口快照和审计通过`decision_id`引用。
- 普通WAIT仅在没有形成正式候选或独立观察候选时允许10分钟聚合；所有候选逐条保存。

## File Responsibility Map

**新增文件**

- `app/decision_context.py`：规范配置哈希、决策ID、输入冻结、顺序轨迹和最终结果。
- `app/storage_schema.py`：`PRAGMA user_version`增量迁移，不承载业务查询。
- `app/storage_capacity.py`：3GiB页数上限、容量状态和核心写入预留策略。
- `app/adaptive_profile_state.py`：完整画像键的N12/N20纯状态机和重启重放。
- `app/entry_structure_shadow.py`：结构检测器、状态机、方向映射和影子快照。
- `tests/test_decision_context.py`、`tests/test_storage_schema.py`、`tests/test_storage_capacity.py`、`tests/test_adaptive_profile_state.py`、`tests/test_entry_structure_shadow.py`：新模块的纯单元测试。

**主要修改文件**

- `app/models.py`：为信号、观察和订单增加引用及版本化快照字段。
- `app/indicators.py`：暴露已计算但当前丢弃的MACD线/信号线，并增加ATR归一化字段。
- `app/strategy.py`：将真实读取的指标、阈值和评分输入装入候选上下文，不改变评分公式。
- `app/storage.py`：上下文存取、原子事务包、审计聚合、SQL分页汇总和兼容读取。
- `app/daily_profile_selector.py`：7天快速资格、14天稳定资格和连续两次联合失败退出。
- `app/state.py`：统一候选来源、顺序决策轨迹、自适应准入、影子结构附加和容量阻断。
- `app/simulator.py`：把同一`decision_id`下的自适应与结构快照冻结到订单。
- `app/server.py`、`scripts/run.sh`：配置接线、API窗口和筛选参数、中文启动说明。
- `app/static/index.html`、`app/static/app.js`、`app/static/styles.css`：紧凑状态展示和价格结构筛选。
- `scripts/replay_daily_profile_selector.py`：严格按结算事件推进双窗口与N12/N20状态。
- `tests/test_strategy.py`、`tests/test_storage.py`、`tests/test_state.py`、`tests/test_simulator.py`、`tests/test_server.py`、`tests/test_daily_profile_selector.py`、`tests/test_daily_profile_replay.py`、`tests/test_packaging.py`：集成与回归。

## Phase 1: Reproducible Decision Data

### Task 1: Define `DECISION_CONTEXT_V2`

**Files:**
- Create: `app/decision_context.py`
- Create: `tests/test_decision_context.py`

- [ ] **Step 1: Write failing tests for canonical configuration and immutable decision input**

```python
class DecisionContextTests(unittest.TestCase):
    def test_runtime_config_hash_is_order_independent_and_excludes_credentials(self):
        left = {"threshold": 60.0, "webhook_url": "secret", "nested": {"b": 2, "a": 1}}
        right = {"nested": {"a": 1, "b": 2}, "threshold": 60.0, "webhook_url": "other"}
        self.assertEqual(runtime_config_snapshot(left).hash, runtime_config_snapshot(right).hash)
        self.assertNotIn("secret", runtime_config_snapshot(left).canonical_payload)

    def test_builder_freezes_inputs_then_appends_ordered_trace_and_outcome(self):
        builder = DecisionContextBuilder.new("BTCUSDT", 1_000, "NATIVE_ACTIONABLE", "hash")
        builder.capture_inputs({"score": 61.0, "threshold": 60.0})
        builder.trace("SCORE", "PASS", {"margin": 1.0})
        context = builder.finish("OPENED", "OPENED", True, True)
        self.assertEqual(context.decision_trace[0]["stage"], "SCORE")
        self.assertEqual(context.first_decisive_block, "")
        with self.assertRaises(RuntimeError):
            builder.capture_inputs({"score": 99.0})
```

- [ ] **Step 2: Run the new test and confirm the missing-module failure**

Run: `python3 -m unittest tests.test_decision_context -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.decision_context'`.

- [ ] **Step 3: Implement the versioned context contract**

```python
CONTEXT_VERSION = "DECISION_CONTEXT_V2"
REDACTED_CONFIG_KEYS = frozenset({"webhook_url", "webhook_token", "api_key", "api_secret"})

@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    hash: str
    canonical_payload: str
    strategy_build_id: str

@dataclass(frozen=True)
class DecisionContext:
    decision_id: str
    context_version: str
    runtime_config_hash: str
    strategy_build_id: str
    symbol: str
    closed_kline_at_ms: int
    candidate_origin: str
    inputs: dict[str, object]
    decision_trace: Sequence[dict[str, object]]
    first_decisive_block: str
    final_decision: str
    final_reason: str
    open_allowed: bool
    observation_allowed: bool

def runtime_config_snapshot(config: Mapping[str, object], strategy_build_id: str = "UNKNOWN") -> RuntimeConfigSnapshot:
    payload = _redact_and_normalize(config)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return RuntimeConfigSnapshot(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical, strategy_build_id)
```

Implement `DecisionContextBuilder.new()`, single-use `capture_inputs()`, ordered `trace()`, and `finish()`; derive stable unique IDs from symbol, closed-kline time, candidate origin, profile key and candidate ordinal.

- [ ] **Step 4: Verify the contract tests pass**

Run: `python3 -m unittest tests.test_decision_context -v`

Expected: PASS.

- [ ] **Step 5: Commit the isolated contract**

```bash
git add app/decision_context.py tests/test_decision_context.py
git commit -m "feat: add reproducible decision context contract"
```

### Task 2: Expose Every Indicator Input Actually Read By Strategy

**Files:**
- Modify: `app/indicators.py`
- Modify: `app/models.py`
- Modify: `app/strategy.py`
- Modify: `tests/test_strategy.py`
- Modify: `tests/test_simulator.py`

- [ ] **Step 1: Add failing tests for MACD line/signal, ATR normalization and model copying**

```python
def test_signal_records_complete_indicator_inputs_without_changing_score():
    signal = analyze_volume_price(trending_klines(), timeframe_minutes=10)
    self.assertIn("macd_line", signal.decision_inputs["indicators"])
    self.assertIn("macd_signal_line", signal.decision_inputs["indicators"])
    self.assertIn("macd_histogram_atr", signal.decision_inputs["indicators"])
    self.assertIn("macd_delta_atr", signal.decision_inputs["indicators"])
    self.assertIn("volume_baseline", signal.decision_inputs["volume_price"])
    self.assertEqual(signal.score, EXPECTED_BASELINE_SCORE)

def test_simulator_freezes_decision_references_on_order():
    signal = Signal(
        direction="LONG",
        timeframe_minutes=10,
        level="B",
        reason="test",
        price=100.0,
        open_time=1_000,
        decision_id="d-1",
        runtime_config_hash="h-1",
    )
    order = AccountSimulator().open_order(signal, signal.price, signal.open_time)
    self.assertEqual((order.decision_id, order.runtime_config_hash), ("d-1", "h-1"))
```

- [ ] **Step 2: Verify the tests fail on missing fields**

Run: `python3 -m unittest tests.test_strategy tests.test_simulator -v`

Expected: FAIL because `TechnicalContext`, `Signal`, and `SimulatedOrder` do not expose the new fields.

- [ ] **Step 3: Extend the existing calculations without changing formulas**

```python
@dataclass(frozen=True)
class TechnicalContext:
    macd_line: float = 0.0
    macd_signal_line: float = 0.0
    macd_histogram: float = 0.0
    macd_histogram_delta: float = 0.0
    atr: float = 0.0
    macd_histogram_atr: float = 0.0
    macd_delta_atr: float = 0.0
    rsi: float = 50.0
    bollinger_position: float = 0.5
    bollinger_width: float = 0.0
```

Change `_macd_histogram_context()` to return line, signal, histogram and delta; derive ATR14 and divide only when ATR is positive. Add to all three models: `decision_id`, `context_version`, `runtime_config_hash`, `strategy_build_id`, `candidate_origin`, `decision_inputs`, `decision_trace`, `first_decisive_block`, `adaptive_profile_state`, and `entry_structure_shadow`. Preserve all current defaults so old JSON remains loadable. `AccountSimulator.open_order_with_credit()` must copy these fields exactly.

- [ ] **Step 4: Run focused strategy and simulator regression tests**

Run: `python3 -m unittest tests.test_strategy tests.test_simulator -v`

Expected: PASS, including unchanged baseline score and direction assertions.

- [ ] **Step 5: Commit complete candidate inputs**

```bash
git add app/indicators.py app/models.py app/strategy.py app/simulator.py tests/test_strategy.py tests/test_simulator.py
git commit -m "feat: capture complete strategy indicator inputs"
```

### Task 3: Add An Incremental V2 SQLite Schema

**Files:**
- Create: `app/storage_schema.py`
- Create: `tests/test_storage_schema.py`
- Modify: `app/storage.py`

- [ ] **Step 1: Write failing migration tests against a synthetic V1 database**

```python
def test_v1_database_is_upgraded_without_rewriting_old_rows(self):
    create_legacy_database(path, signal_payload='{"decision":"WAIT"}')
    before = read_legacy_payload(path)
    SQLiteMonitorStore(path)
    self.assertEqual(read_legacy_payload(path), before)
    self.assertEqual(read_user_version(path), 2)
    self.assertTrue(table_exists(path, "runtime_config_snapshots"))
    self.assertTrue(table_exists(path, "decision_contexts"))
    self.assertIn("decision_id", table_columns(path, "orders"))
    self.assertIsNone(read_legacy_order(path)["decision_id"])

def test_v2_migration_is_idempotent():
    SQLiteMonitorStore(path)
    SQLiteMonitorStore(path)
    self.assertEqual(read_user_version(path), 2)
```

- [ ] **Step 2: Confirm migration tests fail before the migrator exists**

Run: `python3 -m unittest tests.test_storage_schema -v`

Expected: FAIL on missing `app.storage_schema` or missing V2 tables.

- [ ] **Step 3: Implement `user_version=2` as additive DDL only**

Create `runtime_config_snapshots` and `decision_contexts`; add nullable reference and denormalized filter columns to `orders`, `observation_signals`, `order_entry_snapshots`, and `signal_audit`. Add `record_version`, `event_kind`, time range, `occurrences`, score range and nullable `aggregation_key` to `signal_audit`, with a partial unique index where `aggregation_key IS NOT NULL`. Run DDL inside one transaction and set `PRAGMA user_version=2` only after success.

```python
def migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("pragma user_version").fetchone()[0]
    if version >= 2:
        return
    with connection:
        _create_v2_tables(connection)
        _add_missing_v2_columns(connection)
        _create_v2_indexes(connection)
        connection.execute("pragma user_version = 2")
```

- [ ] **Step 4: Run migration and existing storage tests**

Run: `python3 -m unittest tests.test_storage_schema tests.test_storage -v`

Expected: PASS; old payload bytes and row counts remain unchanged.

- [ ] **Step 5: Commit the migration boundary**

```bash
git add app/storage_schema.py app/storage.py tests/test_storage_schema.py tests/test_storage.py
git commit -m "feat: add additive decision storage schema"
```

### Task 4: Persist Runtime Configuration And Decision Context Once

**Files:**
- Modify: `app/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Add failing storage round-trip and collision tests**

```python
def test_runtime_snapshot_is_deduplicated_and_decision_context_round_trips(self):
    store.save_runtime_config_snapshot(snapshot)
    store.save_runtime_config_snapshot(snapshot)
    store.save_decision_context(context)
    self.assertEqual(store.count_runtime_configs(), 1)
    self.assertEqual(store.load_decision_context("BTCUSDT", context.decision_id), context.to_dict())

def test_same_hash_with_different_payload_is_rejected(self):
    store.save_runtime_config_snapshot(snapshot)
    with self.assertRaises(ValueError):
        store.save_runtime_config_snapshot(replace(snapshot, canonical_payload='{"changed":true}'))
```

- [ ] **Step 2: Run storage tests and confirm missing-method failures**

Run: `python3 -m unittest tests.test_storage.SQLiteMonitorStoreTests.test_runtime_snapshot_is_deduplicated_and_decision_context_round_trips tests.test_storage.SQLiteMonitorStoreTests.test_same_hash_with_different_payload_is_rejected -v`

Expected: FAIL because the persistence methods do not exist.

- [ ] **Step 3: Implement normalized persistence and legacy defaults**

Use `INSERT OR IGNORE` for configuration, then compare stored canonical payload and build ID. Store input and outcome payloads separately and reject a second write that changes frozen inputs. Readers must return `LEGACY`, `UNKNOWN`, empty dictionaries, or `occurrences=1` for absent old fields; they must never synthesize old inputs from current configuration.

```python
def save_runtime_config_snapshot(self, snapshot: RuntimeConfigSnapshot) -> None:
    with self._connect() as connection:
        connection.execute(
            "insert or ignore into runtime_config_snapshots values (?, ?, ?, ?, ?, ?)",
            (snapshot.hash, CONTEXT_VERSION, snapshot.strategy_build_id,
             snapshot.canonical_payload, len(snapshot.canonical_payload.encode("utf-8")), self._now_ms()),
        )
        stored = connection.execute(
            "select canonical_payload, strategy_build_id from runtime_config_snapshots where runtime_config_hash = ?",
            (snapshot.hash,),
        ).fetchone()
        if stored != (snapshot.canonical_payload, snapshot.strategy_build_id):
            raise ValueError("runtime config hash collision")

def save_decision_context(self, context: DecisionContext) -> None:
    with self._connect() as connection:
        self._insert_decision_context(connection, context)

def load_decision_context(self, symbol: str, decision_id: str) -> dict[str, Any] | None:
    with self._connect() as connection:
        row = connection.execute(
            "select input_payload, outcome_payload from decision_contexts where symbol = ? and decision_id = ?",
            (symbol, decision_id),
        ).fetchone()
    return None if row is None else {"inputs": json.loads(row[0]), "outcome": json.loads(row[1])}
```

- [ ] **Step 4: Verify focused and full storage tests**

Run: `python3 -m unittest tests.test_storage -v`

Expected: PASS.

- [ ] **Step 5: Commit normalized context storage**

```bash
git add app/storage.py tests/test_storage.py
git commit -m "feat: persist normalized decision contexts"
```

## Phase 2: Bounded And Consistent Storage

### Task 5: Enforce The 3GiB SQLite Capacity Contract

**Files:**
- Create: `app/storage_capacity.py`
- Create: `tests/test_storage_capacity.py`
- Modify: `app/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write failing boundary and PRAGMA tests**

```python
def test_capacity_boundaries_and_core_reserve():
    self.assertEqual(classify_capacity(GIB(2.49)), "NORMAL")
    self.assertEqual(classify_capacity(GIB(2.50)), "WARNING")
    self.assertEqual(classify_capacity(GIB(2.75)), "COMPACT_ONLY")
    self.assertEqual(classify_capacity(GIB(3.00)), "HARD_LIMIT")
    self.assertEqual(CORE_RESERVE_BYTES, 256 * 1024 * 1024)

def test_store_sets_max_page_count_from_actual_page_size():
    store = SQLiteMonitorStore(path)
    page_size = pragma(path, "page_size")
    self.assertEqual(pragma(path, "max_page_count"), (3 * 1024**3) // page_size)
```

- [ ] **Step 2: Verify failure before capacity control exists**

Run: `python3 -m unittest tests.test_storage_capacity tests.test_storage -v`

Expected: FAIL on missing module and absent `max_page_count` contract.

- [ ] **Step 3: Implement capacity states and write classes**

```python
MAX_DATABASE_BYTES = 3 * 1024**3
WARNING_BYTES = int(2.5 * 1024**3)
COMPACT_ONLY_BYTES = int(2.75 * 1024**3)
CORE_RESERVE_BYTES = 256 * 1024**2

@dataclass(frozen=True)
class StorageCapacity:
    status: str
    database_bytes: int
    max_database_bytes: int
    core_reserve_bytes: int
    ordinary_audit_allowed: bool
    core_write_allowed: bool
```

Every connection applies the page cap. `COMPACT_ONLY` rejects only ordinary WAIT heartbeat writes and continues core orders, independent observations, state transitions and decisive blocks. `HARD_LIMIT`, or an SQLite full error on a core write, returns a synchronous capacity failure that the order path can block before opening.

- [ ] **Step 4: Run capacity and storage regression tests**

Run: `python3 -m unittest tests.test_storage_capacity tests.test_storage -v`

Expected: PASS, including simulated `SQLITE_FULL` behavior.

- [ ] **Step 5: Commit bounded storage behavior**

```bash
git add app/storage_capacity.py app/storage.py tests/test_storage_capacity.py tests/test_storage.py
git commit -m "feat: enforce bounded sqlite capacity"
```

### Task 6: Implement `SIGNAL_AUDIT_V2` And True SQL Observation Queries

**Files:**
- Modify: `app/storage.py`
- Modify: `app/state.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Add failing tests for weighted audit counts and data beyond row 5000**

```python
def test_signal_audit_summary_weights_v2_occurrences_and_legacy_rows():
    insert_legacy_audit(decision="WAIT")
    insert_v2_audit(decision="WAIT", occurrences=9)
    summary = store.signal_audit_summary("BTCUSDT")
    self.assertEqual(summary["total"], 10)
    self.assertEqual(summary["storage_rows"], 2)

def test_observation_summary_and_page_query_all_rows():
    insert_observations(5_101)
    self.assertEqual(store.observation_summary("BTCUSDT", window="all")["total"]["settled"], 5_101)
    page = store.page_observations("BTCUSDT", page=256, page_size=20)
    self.assertEqual(page["total"], 5_101)
    self.assertEqual(len(page["observations"]), 1)
```

- [ ] **Step 2: Confirm existing 5000-row limit and physical-row counting fail the tests**

Run: `python3 -m unittest tests.test_storage -v`

Expected: FAIL on total counts or page coverage.

- [ ] **Step 3: Implement compact audit upsert and SQL-filtered observation statistics**

Build `aggregation_key` from symbol, 10-minute bucket, final decision, reason code, profile key, direction, context/config versions and decisive block. Aggregate only non-candidates with ordinary `WAIT/BELOW_THRESHOLD`; update `first_at_ms`, `last_at_ms`, `occurrences`, `score_min`, and `score_max`. Store formal candidates and independent observations as individual V2 rows. Rewrite observation paging with `COUNT(*)` plus filtered `LIMIT/OFFSET`, and summary with SQL aggregates for `7d`, `14d`, `30d`, and `all` while preserving existing response keys.

- [ ] **Step 4: Run storage and state audit tests**

Run: `python3 -m unittest tests.test_storage tests.test_state -v`

Expected: PASS; direction-pulse history may still use its separate 5000-row bounded loader.

- [ ] **Step 5: Commit compact audit and complete observation queries**

```bash
git add app/storage.py app/state.py tests/test_storage.py tests/test_state.py
git commit -m "feat: compact signal audit and query full observations"
```

### Task 7: Save Each Decision As An Atomic Bundle

**Files:**
- Modify: `app/storage.py`
- Modify: `app/state.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_webhook.py`

- [ ] **Step 1: Write failing rollback and webhook-ordering tests**

```python
def test_open_bundle_failure_rolls_back_order_credit_context_and_webhook():
    store.fail_bundle_step = "entry_snapshot"
    decision = state._maybe_open_order(signal, latest)
    self.assertEqual(decision, "STORAGE_ERROR")
    self.assertEqual(state.simulator.orders, [])
    self.assertEqual(state.simulator.stake_progression.pending_credits(), [])
    self.assertIsNone(store.load_decision_context("BTCUSDT", signal.decision_id))
    webhook.assert_not_called()

def test_observation_bundle_commits_context_observation_and_audit_together():
    state._record_observation(signal, latest, "RESEARCH_OBSERVE")
    self.assertEqual(store.bundle_members(signal.decision_id), {"context", "observation", "audit"})
```

- [ ] **Step 2: Run focused tests and observe partial-write behavior**

Run: `python3 -m unittest tests.test_storage tests.test_state tests.test_webhook -v`

Expected: FAIL because context, order, snapshot, audit and observation currently use separate writes.

- [ ] **Step 3: Implement transaction bundle methods and reorder side effects**

```python
def save_open_order_decision(self, *, config, context, order, credit, entry_snapshot, audit, observation) -> None:
    with self._connect() as connection:
        with connection:
            self._insert_runtime_config(connection, config)
            self._insert_decision_context(connection, context)
            self._upsert_order(connection, order)
            self._apply_progression_credit(connection, credit)
            self._insert_order_entry_snapshot(connection, order, entry_snapshot)
            self._upsert_signal_audit(connection, audit)
            if observation is not None:
                self._upsert_observation(connection, observation, order.symbol)

def save_decision_bundle(self, *, config, context, audit, observation=None) -> None:
    with self._connect() as connection:
        with connection:
            self._insert_runtime_config(connection, config)
            self._insert_decision_context(connection, context)
            self._upsert_signal_audit(connection, audit)
            if observation is not None:
                self._upsert_observation(connection, observation, context.symbol)
```

Within one SQLite transaction, validate the config reference, insert the context, save order and progression credit, save entry snapshot, save individual audit and optional observation. `state.py` may create the in-memory order first, but must call `rollback_open_order()` if the transaction fails. Update open-time keys and trigger the existing fire-and-forget webhook only after commit. Settlement remains linked by `decision_id` and updates settlement payload only; frozen inputs never mutate.

- [ ] **Step 4: Verify transaction and webhook tests pass**

Run: `python3 -m unittest tests.test_storage tests.test_state tests.test_webhook -v`

Expected: PASS.

- [ ] **Step 5: Commit atomic decision persistence**

```bash
git add app/storage.py app/state.py tests/test_storage.py tests/test_state.py tests/test_webhook.py
git commit -m "feat: persist decisions as atomic bundles"
```

### Task 8: Trace Every Existing Order Gate Without Changing Its Result

**Files:**
- Modify: `app/state.py`
- Modify: `app/order_policy.py`
- Modify: `app/models.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_order_policy.py`

- [ ] **Step 1: Add table-driven failing tests for all decisive branches**

```python
@parameterized.expand([
    ("BELOW_THRESHOLD", "SCORE"),
    ("SESSION_BLOCKED", "SESSION"),
    ("PROFILE_NOT_SELECTED", "DAILY_PROFILE"),
    ("PROFILE_GUARD_BLOCKED", "PROFILE_HEALTH"),
    ("WAVE_BLOCKED", "WAVE_GUARD"),
    ("DIRECTION_COOLDOWN", "COOLDOWN"),
    ("MAX_OPEN_ORDERS", "CAPACITY"),
])
def test_decision_trace_names_first_decisive_branch(self, expected_decision, expected_stage):
    result = run_fixture(expected_decision)
    self.assertEqual(result.order_decision, expected_decision)
    self.assertEqual(result.selected_signal.first_decisive_block, expected_stage)
    self.assertEqual(result.selected_signal.decision_trace[-1]["result"], "BLOCK")
```

- [ ] **Step 2: Run state and order policy tests and confirm missing trace data**

Run: `python3 -m unittest tests.test_state tests.test_order_policy -v`

Expected: FAIL because early returns do not append standardized trace records.

- [ ] **Step 3: Thread one builder through the existing decision chain**

At candidate creation, capture identity, closed 1-minute kline, price/volume, all score inputs and thresholds, F&G, 10-minute bias/regime, profile identity, capacity/cooldown, stake configuration, all enabled modes and module versions. Each existing guard appends `{stage, result, reason_code, decisive_values}` before returning. Do not reorder guards and do not replace their formulas. Freeze one final outcome under the same `decision_id` and copy it to formal order or independent observation.

The canonical input payload must contain these concrete groups before `capture_inputs()` freezes it:

- `identity`: symbol, decision ID, context/build/config versions, candidate origin, profile key, family, tag, direction, segment, level and slot.
- `market`: closed-kline time, candidate time, entry price, OHLCV, 10-minute analysis/threshold windows, price change, position, window close strength, single-candle strength and upper/lower wick ratios.
- `score`: signed/raw score, base/dynamic/calculated threshold, edge, every threshold adjustment and each scoring component.
- `volume_price`: current volume, baseline volume, ratio, high/low ratio thresholds, move thresholds and values used by the volume-price branch.
- `indicators`: MACD line/signal/histogram/delta, ATR14, ATR-normalized histogram/delta and their thresholds; RSI value and bounds; BOLL position, width and bounds; indicator-profile segment, sample count, version and match codes.
- `context`: 10-minute bias, regime, F&G value/class/trend/average/adjustment/time, daily 7d/14d stats and version, N12/N20 stats/state/reason/time, wave and direction-pulse snapshots.
- `admission`: every enabled guard's status/reason/decisive values, global and direction open counts/limits, cooldown timestamps, stake amount, return, progression step/source/limits, and storage capacity.
- `entry_structure`: the complete versioned shadow payload. Its values are audit-only in this release.

The outcome payload must contain the ordered trace, first decisive block, final decision/reason, `open_allowed`, `observation_allowed`, and any selected order terms. `quality_score_inputs` may remain as a compatibility view, but it must reference this canonical payload rather than storing a second divergent input set.

- [ ] **Step 4: Prove all existing order outcomes are unchanged**

Run: `python3 -m unittest tests.test_state tests.test_order_policy tests.test_strategy tests.test_simulator -v`

Expected: PASS, including existing exact decision, direction, stake, cooldown and progression assertions.

- [ ] **Step 5: Commit non-behavioral decision tracing**

```bash
git add app/state.py app/order_policy.py app/models.py tests/test_state.py tests/test_order_policy.py
git commit -m "feat: trace complete order decision path"
```

## Phase 3: Adaptive Resident Profiles

### Task 9: Add 7-Day Fast And 14-Day Stable Qualification

**Files:**
- Modify: `app/daily_profile_selector.py`
- Modify: `tests/test_daily_profile_selector.py`

- [ ] **Step 1: Write failing dual-window and migration tests**

```python
def test_dual_window_selection_matrix(self):
    cases = [
        ("new_fast_pass", None, stats(7, 20, 12, 0.4), stats(14, 30, 16, 0.2), "SELECTED", 0),
        ("selected_stable_only", selected(), stats(7, 20, 11, -0.1), stats(14, 30, 18, 0.1), "RETAINED", 0),
        ("first_joint_failure", selected(), stats(7, 20, 11, -0.1), stats(14, 30, 17, -0.1), "QUALIFICATION_WATCH", 1),
        ("second_joint_failure", watched(1), stats(7, 20, 11, -0.1), stats(14, 30, 17, -0.1), "DEGRADED_EXIT", 2),
    ]
    for name, previous, fast, stable, expected_state, expected_runs in cases:
        with self.subTest(name=name):
            result = select_from_summaries(fast, stable, previous)
            self.assertEqual(result["selection_state"], expected_state)
            self.assertEqual(result["joint_failure_runs"], expected_runs)

def test_legacy_selected_profile_migrates_without_a_failure(self):
    result = select_from_summaries(failing_stats(), failing_stats(), legacy_selected())
    self.assertEqual(result["qualification_state"], "QUALIFIED")
    self.assertEqual(result["joint_failure_runs"], 0)
```

Add local fixture builders `stats()`, `selected()`, `watched()`, `legacy_selected()`, and `select_from_summaries()` in the test module. They must construct real non-overlapping`ObservationSignal` rows and invoke`build_daily_selection()`; they must not bypass selector production code.

Each fixture must use non-overlapping settled observations and separately cover WD minimum20 and WE minimum10 with win rate at least60% and EV at least0.

- [ ] **Step 2: Run selector tests and verify single-window behavior fails them**

Run: `python3 -m unittest tests.test_daily_profile_selector -v`

Expected: FAIL because the selector only computes one 7-day window and exits after one degraded run.

- [ ] **Step 3: Implement fast entry and stable exit**

Extend configuration with `stable_lookback_days=14` and `joint_failures_to_exit=2`. Compute `fast_7d` and `stable_14d` summaries independently using the same overlap exclusion and strict cutoff. Evaluation remains daily at07:50 Asia/Shanghai and the snapshot becomes effective at08:00. New profiles enter on qualifying 7d; selected profiles remain if either window qualifies; first joint failure becomes `QUALIFICATION_WATCH`; second consecutive daily joint failure exits. Recover previous state from all prior candidates, and migrate legacy selected rows to `QUALIFIED` with `joint_failure_runs=0`.

- [ ] **Step 4: Run selector tests**

Run: `python3 -m unittest tests.test_daily_profile_selector -v`

Expected: PASS.

- [ ] **Step 5: Commit dual-window daily selection**

```bash
git add app/daily_profile_selector.py tests/test_daily_profile_selector.py
git commit -m "feat: add dual-window profile qualification"
```

### Task 10: Implement N12/N20 Immediate Profile State

**Files:**
- Create: `app/adaptive_profile_state.py`
- Create: `tests/test_adaptive_profile_state.py`

- [ ] **Step 1: Write failing pure-state tests**

```python
def test_adaptive_state_matrix(self):
    cases = [
        (11, 6, None, 0.0, None, "WARMUP"),
        (12, 7, 12, 0.0, None, "ACTIVE"),
        (20, 7, 20, 0.1, None, "ACTIVE"),
        (20, 6, 20, -0.1, None, "WATCH"),
        (20, 5, 20, -0.1, None, "PAUSED"),
        (20, 6, 20, -0.1, "PAUSED", "WATCH"),
        (20, 5, 20, 0.0, "PAUSED", "WATCH"),
    ]
    for samples, n12_wins, n20_size, n20_ev, previous, expected in cases:
        with self.subTest(expected=expected, previous=previous):
            rows = adaptive_rows(samples, n12_wins, n20_size, n20_ev)
            result = evaluate_adaptive_profile_state(rows, PROFILE_KEY, CUTOFF, previous)
            self.assertEqual(result["status"], expected)

def test_state_excludes_overlap_future_and_other_profile_keys(self):
    rows = mixed_causality_rows()
    result = evaluate_adaptive_profile_state(rows, PROFILE_KEY, CUTOFF)
    self.assertEqual(result["n12"]["sample_size"], 1)
```

Define `adaptive_rows()` and `mixed_causality_rows()` in the test module using real observations; include a separate rebuild test asserting that settlement-order replay returns`PAUSED`then`WATCH` rather than a direct stateless final state.

- [ ] **Step 2: Confirm the module is absent**

Run: `python3 -m unittest tests.test_adaptive_profile_state -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement exact-key state evaluation**

```python
@dataclass(frozen=True)
class AdaptiveProfileStateConfig:
    warmup_samples: int = 12
    active_n12_wins: int = 7
    paused_n12_max_wins: int = 5
    full_window_samples: int = 20

def evaluate_adaptive_profile_state(observations, profile_key, evaluated_at, previous=None, config=None) -> dict:
    samples = independent_settled_samples(observations, profile_key, evaluated_at)[-20:]
    return classify_profile_state(samples, previous=previous, config=config or AdaptiveProfileStateConfig())

def rebuild_adaptive_profile_states(observations, evaluated_at, config=None) -> dict[str, dict]:
    states: dict[str, dict] = {}
    for settlement in sorted_settlement_events(observations, evaluated_at):
        key = observation_profile_key(settlement)
        states[key] = evaluate_adaptive_profile_state(observations, key, settlement.settled_at, states.get(key), config)
    return states
```

Use the complete `10|family|tag|direction|WD/WE-hour` key. Select independent samples by opened/expiry intervals and `settled_at < evaluated_at`. `ACTIVE` requires N12 wins>=7 and either N20 is not full or full N20 EV>=0. `PAUSED` requires N12 wins<=5 and a full negative-EV N20. Every other mature state is`WATCH`; recovery follows the documented hysteresis.

- [ ] **Step 4: Run the pure state tests**

Run: `python3 -m unittest tests.test_adaptive_profile_state -v`

Expected: PASS.

- [ ] **Step 5: Commit the immediate state machine**

```bash
git add app/adaptive_profile_state.py tests/test_adaptive_profile_state.py
git commit -m "feat: add immediate adaptive profile state"
```

### Task 11: Integrate Adaptive State Into Runtime Admission

**Files:**
- Modify: `app/models.py`
- Modify: `app/state.py`
- Modify: `app/simulator.py`
- Modify: `app/storage.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_simulator.py`

- [ ] **Step 1: Add failing end-to-end adaptive admission tests**

```python
def test_adaptive_admission_matrix(self):
    cases = [
        ("WARMUP", "FIRST", True, True, 10.0),
        ("ACTIVE", "SECOND", True, True, 18.0),
        ("WATCH", "FIRST", True, False, 10.0),
        ("WATCH", "SECOND", False, False, 0.0),
        ("PAUSED", "FIRST", False, False, 0.0),
    ]
    for status, slot, opens, progression, stake in cases:
        with self.subTest(status=status, slot=slot):
            state = adaptive_state_fixture(status, slot)
            decision = state._maybe_open_order(state.selected_signal, latest_kline())
            self.assertEqual(decision == "OPENED", opens)
            self.assertEqual(state.progression_was_consumed(), progression)
            self.assertEqual(state.last_opened_stake(), stake)

def test_settlement_commit_precedes_single_key_refresh(self):
    state._settle_observations(CUTOFF, 101.0, expiry_klines())
    self.assertEqual(storage.events[:2], ["settlement_committed", f"refresh:{PROFILE_KEY}"])
```

Add restart and failure tests using a temporary`SQLiteMonitorStore`: restart must equal the pre-restart state from15 days of rows; an injected refresh exception must preserve the prior state and set`last_error`.

- [ ] **Step 2: Run integration tests and verify existing profile health cannot satisfy them**

Run: `python3 -m unittest tests.test_state tests.test_storage tests.test_simulator -v`

Expected: FAIL because current profile health is direction-level/24-hour behavior rather than exact-key N12/N20 state.

- [ ] **Step 3: Wire state refresh and admission rules**

Rebuild exact-key state during construction and symbol reset from at least15 days of settled observations. After `_settle_observations()` commits settlement, recalculate only affected keys. Attach 7d/14d qualification and N12/N20 state before `_maybe_open_order_locked()`. Preserve current behavior for `WARMUP/ACTIVE`; for `WATCH`, allow only a first 10U order and call `open_order_with_credit(signal, latest.close, latest.close_time, allow_progression=False)`; for `PAUSED`, return a distinct block reason while still recording an independent observation. Keep `profile_health_guard.py` separate and do not silently replace it.

- [ ] **Step 4: Run adaptive runtime regressions**

Run: `python3 -m unittest tests.test_state tests.test_storage tests.test_simulator -v`

Expected: PASS.

- [ ] **Step 5: Commit runtime adaptive admission**

```bash
git add app/models.py app/state.py app/simulator.py app/storage.py tests/test_state.py tests/test_storage.py tests/test_simulator.py
git commit -m "feat: apply adaptive profile admission states"
```

## Phase 4: Causal Entry Structure Shadow

### Task 12: Detect Causal Support, Resistance And Round Levels

**Files:**
- Create: `app/entry_structure_shadow.py`
- Create: `tests/test_entry_structure_shadow.py`

- [ ] **Step 1: Add failing detector tests**

```python
def test_pivot_is_causal_and_cluster_requires_independent_confirmations(self):
    before_confirmation = detector.detect("BTCUSDT", pivot_fixture()[:PIVOT_INDEX + 3])
    after_confirmation = detector.detect("BTCUSDT", pivot_fixture()[:PIVOT_INDEX + 4])
    self.assertNotIn(PIVOT_INDEX, support_pivot_indexes(before_confirmation))
    self.assertIn(PIVOT_INDEX, support_pivot_indexes(after_confirmation))
    self.assertEqual(after_confirmation["support"][0]["pivot_count"], 2)
    self.assertGreaterEqual(after_confirmation["support"][0]["pivot_gap"], 5)

def test_round_level_rules_and_deterministic_rebuild(self):
    self.assertEqual(round_steps("BTCUSDT"), (100, 500, 1000))
    self.assertEqual(round_steps("ETHUSDT"), (10, 50, 100))
    first = detector.detect("BTCUSDT", closed_240_bar_fixture())
    second = detector.detect("BTCUSDT", closed_240_bar_fixture())
    self.assertEqual(first, second)
    self.assertNotIn(unconfirmed_round_level(), active_levels(first))
```

Add separate assertions that support clusters contain only pivot lows, resistance clusters only pivot highs, cluster width is at most`0.25 * ATR14`, and an ordinary touch does not increase`pivot_count`.

- [ ] **Step 2: Confirm detector tests fail**

Run: `python3 -m unittest tests.test_entry_structure_shadow -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement the detector portion of the module**

```python
ENTRY_STRUCTURE_VERSION = "ENTRY_STRUCTURE_SHADOW_V1"

@dataclass(frozen=True)
class StructureConfig:
    bars: int = 240
    atr_period: int = 14
    pivot_left: int = 3
    pivot_right: int = 3
    cluster_atr: float = 0.25
    min_pivots: int = 2
    min_pivot_gap: int = 5

class StructureDetector:
    def detect(self, symbol: str, closed_klines: Sequence[Kline]) -> dict[str, object]:
        scoped = tuple(closed_klines[-self.config.bars:])
        atr = atr14(scoped)
        pivots = confirmed_causal_pivots(scoped, self.config)
        zones = cluster_pivots(pivots, atr, self.config)
        return merge_qualified_round_levels(symbol, scoped, zones, atr, self.config)
```

Use only the last240 closed 1-minute bars. A pivot becomes visible only after the third right bar closes. Emit nearest support/resistance zones, touches, first/last confirmation, ATR distances, and round-level source. Treat a pure round number as a zero-width level until merged with a real zone.

- [ ] **Step 4: Run detector tests**

Run: `python3 -m unittest tests.test_entry_structure_shadow -v`

Expected: PASS for detector cases.

- [ ] **Step 5: Commit causal structure detection**

```bash
git add app/entry_structure_shadow.py tests/test_entry_structure_shadow.py
git commit -m "feat: detect causal entry price structure"
```

### Task 13: Add Structure State Machine And Direction Mapping

**Files:**
- Modify: `app/entry_structure_shadow.py`
- Modify: `tests/test_entry_structure_shadow.py`

- [ ] **Step 1: Add failing state and priority tests**

```python
def test_structure_state_transition_matrix(self):
    cases = [
        (approach_support(0.35), "APPROACHING_SUPPORT"),
        (support_reclaim(0.05), "SUPPORT_REJECTED"),
        (breakout_closes(1, 0.10), "BREAKOUT_PENDING"),
        (breakout_closes(2, 0.10), "BREAKOUT_CONFIRMED"),
        (retest_within_bars(5), "RETEST_HELD"),
        (failed_breakout(), "FALSE_BREAKOUT"),
        (invalidating_closes(3, 0.35), "LEVEL_INVALIDATED"),
    ]
    for bars, expected in cases:
        with self.subTest(expected=expected):
            self.assertEqual(machine.evaluate(DETECTED_LEVELS, bars)[0]["state"], expected)

def test_conservative_priority_and_tie_breaks(self):
    ordered = gate.rank([neutral(), confirmed(), pending(), conflict()])
    self.assertEqual([item["bias"] for item in ordered], ["CONFLICT", "PENDING", "CONFIRMED", "NEUTRAL"])
    self.assertEqual(gate.rank(equal_bias_levels())[0]["id"], "nearest-more-touches-newer")
```

Add one error-path test asserting both insufficient data and a detector exception return mode`SHADOW_ONLY`, state`INSUFFICIENT_DATA`or`ERROR`, and bias`NEUTRAL`.

- [ ] **Step 2: Run tests and confirm state transitions are absent**

Run: `python3 -m unittest tests.test_entry_structure_shadow -v`

Expected: FAIL on missing `StructureStateMachine` and `EntryStructureGate`.

- [ ] **Step 3: Implement state transitions and conservative candidate choice**

```python
class StructureStateMachine:
    STATES = {"INSUFFICIENT_DATA", "NO_NEARBY_LEVEL", "APPROACHING_SUPPORT", "APPROACHING_RESISTANCE", "SUPPORT_REJECTED", "RESISTANCE_REJECTED", "BREAKOUT_PENDING", "BREAKOUT_CONFIRMED", "RETEST_PENDING", "RETEST_HELD", "FALSE_BREAKOUT", "LEVEL_INVALIDATED"}
    def evaluate(self, detected: dict[str, object], closed_klines: Sequence[Kline]) -> list[dict[str, object]]:
        return [classify_level_state(level, closed_klines, detected["atr"]) for level in detected["levels"]]

class EntryStructureGate:
    def attach(self, signal: Signal, market_snapshot: dict[str, object], candidate_origin: str) -> dict[str, object]:
        evidence = [map_direction_bias(signal.direction, item) for item in market_snapshot["states"]]
        decisive = sorted(evidence, key=conservative_evidence_sort_key)[0] if evidence else neutral_evidence()
        return build_shadow_payload(signal, market_snapshot, decisive, candidate_origin)
```

Map each level state to `CONFIRMED`, `CONFLICT`, `PENDING`, or `NEUTRAL` for the candidate direction. Choose decisive evidence by `CONFLICT > PENDING > CONFIRMED > NEUTRAL`, then nearest ATR distance, more confirmed touches, and newer confirmation. All outputs remain `SHADOW_ONLY` and include reason code, source, distances, breakout/retest state and candidate origin.

- [ ] **Step 4: Run complete structure unit tests**

Run: `python3 -m unittest tests.test_entry_structure_shadow -v`

Expected: PASS.

- [ ] **Step 5: Commit structure shadow state mapping**

```bash
git add app/entry_structure_shadow.py tests/test_entry_structure_shadow.py
git commit -m "feat: classify entry structure shadow states"
```

### Task 14: Attach One Structure Snapshot To Every Candidate Path

**Files:**
- Modify: `app/state.py`
- Modify: `app/models.py`
- Modify: `app/simulator.py`
- Modify: `app/storage.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_simulator.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_webhook.py`

- [ ] **Step 1: Add failing source mapping, persistence and equivalence tests**

```python
def test_candidate_origin_and_snapshot_identity(self):
    cases = [
        (native_actionable_signal(), "NATIVE_ACTIONABLE"),
        (profile_promoted_wait(), "PROFILE_PROMOTED_WAIT"),
        (research_observation_signal(), "RESEARCH_OBSERVATION"),
    ]
    for signal, expected_origin in cases:
        result = run_candidate(signal)
        self.assertEqual(result.signal.entry_structure_shadow["candidate_origin"], expected_origin)
        self.assertEqual(result.signal.entry_structure_shadow, result.context.inputs["entry_structure"])
        if result.observation is not None:
            self.assertEqual(result.signal.entry_structure_shadow, result.observation.entry_structure_shadow)
        if result.order is not None:
            self.assertEqual(result.signal.entry_structure_shadow, result.order.entry_structure_shadow)

def test_shadow_mode_and_detector_error_are_order_equivalent(self):
    baseline = run_closed_bars(structure_enabled=False)
    shadow = run_closed_bars(structure_enabled=True)
    error = run_closed_bars(structure_enabled=True, detector_raises=True)
    self.assertEqual(order_identity(shadow.orders), order_identity(baseline.orders))
    self.assertEqual(order_identity(error.orders), order_identity(baseline.orders))
    self.assertEqual(shadow.webhook_payloads, baseline.webhook_payloads)
    self.assertEqual(error.webhook_payloads, baseline.webhook_payloads)
```

- [ ] **Step 2: Run state, simulator, storage and webhook tests**

Run: `python3 -m unittest tests.test_state tests.test_simulator tests.test_storage tests.test_webhook -v`

Expected: FAIL because structure is not attached or persisted.

- [ ] **Step 3: Integrate once-per-kline detection and per-candidate direction mapping**

In `update_from_klines()`, detect the market snapshot once after merging closed klines. Mark original actionable candidates before daily promotion as`NATIVE_ACTIONABLE`, research candidates as`RESEARCH_OBSERVATION`, and any promoted WAIT from `_select_daily_profile_signal()` or `_observation_profile_promoted_signal()` as`PROFILE_PROMOTED_WAIT`; remap structure after final direction is known. Freeze the identical dictionary into the decision context, signal, observation and order. Add denormalized observation columns for state, bias, origin and active level source. Catch detector errors and emit `ERROR/NEUTRAL` without changing any existing branch.

- [ ] **Step 4: Prove shadow equality and persistence**

Run: `python3 -m unittest tests.test_entry_structure_shadow tests.test_state tests.test_simulator tests.test_storage tests.test_webhook -v`

Expected: PASS; the only shadow-on/off differences are structure fields and derived observation statistics.

- [ ] **Step 5: Commit runtime structure shadow integration**

```bash
git add app/state.py app/models.py app/simulator.py app/storage.py tests/test_state.py tests/test_simulator.py tests/test_storage.py tests/test_webhook.py
git commit -m "feat: attach entry structure shadow to decisions"
```

## Phase 5: API, UI And Causal Replay

### Task 15: Expose Adaptive, Structure And Storage Status Through Compatible APIs

**Files:**
- Modify: `app/server.py`
- Modify: `app/state.py`
- Modify: `scripts/run.sh`
- Modify: `tests/test_server.py`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: Add failing API and CLI contract tests**

```python
def test_adaptive_structure_and_capacity_api_contract(self):
    state = get_json("/api/state")
    self.assertIn("fast_7d", state["daily_profile_selection"])
    self.assertIn("stable_14d", state["daily_profile_selection"])
    self.assertIn("immediate_state", state["daily_profile_selection"])
    self.assertIn(state["storage_capacity"]["status"], {"NORMAL", "WARNING", "COMPACT_ONLY", "HARD_LIMIT"})

def test_observation_windows_and_structure_filters(self):
    for window in ("7d", "14d", "30d", "all"):
        self.assertEqual(get_json(f"/api/observation-summary?window={window}")["window"], window)
    page = get_json("/api/observations?entry_structure_bias=CONFLICT&candidate_origin=PROFILE_PROMOTED_WAIT")
    self.assertTrue(all(row["entry_structure_bias"] == "CONFLICT" for row in page["observations"]))
```

Add compatibility assertions for a legacy row (`context_version=LEGACY`, structure/adaptive state`UNKNOWN`) and a subprocess assertion that`scripts/run.sh --help`contains the Chinese descriptions for14-day window and joint-failure count.

- [ ] **Step 2: Run API and packaging tests**

Run: `python3 -m unittest tests.test_server tests.test_packaging -v`

Expected: FAIL on missing response fields, query parameters, or CLI options.

- [ ] **Step 3: Add compatible endpoints and configuration**

Keep existing response keys and add 7d/14d qualification, N12/N20 state/reason/evaluated time, structure shadow and capacity status. Include both UTC segment labels and Asia/Shanghai evaluation/activation times so the page does not conflate market segment with daily selector time. Accept observation `window`, `entry_structure_state`, `entry_structure_bias`, `candidate_origin`, and `active_level_source`. Add startup options for 14-day window and joint failure count, with defaults14 and2; document every option in Chinese. Do not add a switch that can make structure enforce orders in this release.

- [ ] **Step 4: Verify server and packaging contracts**

Run: `python3 -m unittest tests.test_server tests.test_packaging -v`

Expected: PASS.

- [ ] **Step 5: Commit API and startup wiring**

```bash
git add app/server.py app/state.py scripts/run.sh tests/test_server.py tests/test_packaging.py
git commit -m "feat: expose adaptive profile diagnostics"
```

### Task 16: Render Compact Diagnostics Without Duplicating The K-Line Card

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/styles.css`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: Add failing static-contract assertions**

```python
def test_ui_has_one_kline_analysis_container_and_structure_column():
    html = read_static("index.html")
    self.assertEqual(html.count('id="latest-analysis"'), 1)
    self.assertIn('data-column="entry-structure"', html)
    self.assertIn('id="obs-structure-state-filter"', html)

def test_javascript_renders_adaptive_windows_and_structure_safely():
    js = read_static("app.js")
    self.assertIn("formatAdaptiveProfile", js)
    self.assertIn("formatEntryStructure", js)
    self.assertIn("escapeHtml", js)
```

- [ ] **Step 2: Run packaging tests and syntax check**

Run: `python3 -m unittest tests.test_packaging -v`

Expected: FAIL on absent UI elements.

- [ ] **Step 3: Add compact UI rendering**

Keep the latest K-line analysis as one full-width card and append a compact structure tag group to its existing field row. Add one `价格结构` order column. Extend observation filters with structure state, bias and origin, plus the 7/14/30/all window selector. Render missing legacy data as`-`; escape every dynamic label. Use fixed/minimum column widths so the filter and order rows do not shift.

- [ ] **Step 4: Verify static JavaScript and packaging**

Run: `node --check app/static/app.js`

Expected: exit0.

Run: `python3 -m unittest tests.test_packaging -v`

Expected: PASS.

- [ ] **Step 5: Commit diagnostics UI**

```bash
git add app/static/index.html app/static/app.js app/static/styles.css tests/test_packaging.py
git commit -m "feat: render adaptive and structure diagnostics"
```

### Task 17: Build A Strictly Causal Replay And Release Gate Report

**Files:**
- Modify: `scripts/replay_daily_profile_selector.py`
- Modify: `tests/test_daily_profile_replay.py`
- Modify: `docs/release-handoff.md`

- [ ] **Step 1: Add failing replay causality and metric tests**

```python
def test_replay_is_settlement_causal_and_cutoff_safe(self):
    result = replay_daily_profile_selection(causal_fixture(), production_config())
    self.assertEqual(result["events"][0]["adaptive_state_before"], "WARMUP")
    self.assertEqual(result["events"][0]["adaptive_state_after"], "ACTIVE")
    self.assertNotIn("settled_after_0750", result["daily_snapshots"][0]["sample_keys"])

def test_report_has_release_gate_metrics(self):
    report = replay_daily_profile_selection(causal_fixture(), production_config())
    required = {"total", "by_direction", "base_first_retention", "maximum_drawdown", "longest_loss_streak", "daily_best", "daily_worst", "guard_rejections", "oos_windows"}
    self.assertTrue(required.issubset(report))
    self.assertEqual(report["config"]["max_open_orders"], 2)
    self.assertEqual(sum(window["ev"] > 0 for window in report["oos_windows"]), 2)
```

`production_config()` must specify global/direction concurrency, cooldown, stake, win return, and progression settings explicitly; the replay entry point must reject an omitted value rather than using its current five-order default.

- [ ] **Step 2: Run replay tests and confirm missing metrics/causality**

Run: `python3 -m unittest tests.test_daily_profile_replay -v`

Expected: FAIL because current replay does not advance N12/N20 on each settlement and defaults to five open orders.

- [ ] **Step 3: Implement deterministic event replay and explicit release gates**

Sort events by settlement time, then opened time and observation key; calculate every candidate using only prior events. Require explicit production settings for global and direction concurrency, cooldown, stake and progression. Report baseline and candidate totals, direction totals, 10U first-order retention, win rate, EV, PnL, maximum drawdown, longest loss streak, daily best/worst, each guard rejection count, and three chronological OOS windows.

```python
ACCEPTANCE = {
    "total_win_rate_min": 0.60,
    "direction_win_rate_min": 0.5556,
    "total_order_retention_min": 0.80,
    "direction_order_retention_min": 0.70,
    "base_first_order_retention_min": 0.85,
    "ev_min": 0.0,
    "positive_oos_windows_min": 2,
}
```

Apply `ev_min` independently to total, LONG and SHORT. Reject when maximum drawdown or longest loss streak is worse than baseline. For configurations that all pass, rank by total win rate, then order count, then lower maximum drawdown. Structure shadow must produce a separate equality report proving identical order IDs, directions, times, stakes, progression and Webhook counts.

- [ ] **Step 4: Run replay tests and generate a local report without deployment**

Run: `python3 -m unittest tests.test_daily_profile_replay -v`

Expected: PASS.

Run: `python3 scripts/replay_daily_profile_selector.py --help`

Expected: exit0 and list explicit concurrency/cooldown/progression arguments.

- [ ] **Step 5: Record implementation and validation instructions in the single handoff**

Update `docs/release-handoff.md` with the branch name, module versions, migration behavior, capacity thresholds, replay command, acceptance gates, current implementation status and explicit`未部署`. Keep rejected ideas out of the handoff.

- [ ] **Step 6: Commit replay and handoff changes**

```bash
git add scripts/replay_daily_profile_selector.py tests/test_daily_profile_replay.py docs/release-handoff.md
git commit -m "test: add causal adaptive profile release gates"
```

### Task 18: Full Verification And Review Checkpoint

**Files:**
- Verify: all changed application, script, test and documentation files

- [ ] **Step 1: Run syntax and focused subsystem verification**

Run: `python3 -m compileall -q app scripts tests`

Expected: exit0.

Run: `bash -n scripts/run.sh`

Expected: exit0.

Run: `node --check app/static/app.js`

Expected: exit0.

- [ ] **Step 2: Run the full unit test suite**

Run: `python3 -m unittest discover -s tests`

Expected: all tests PASS with zero errors and zero failures.

- [ ] **Step 3: Run deterministic equality and causal replay against a copied local database**

Use a copy of the source SQLite file. Run the baseline and candidate with identical symbol, time range, two-order production concurrency, direction limits, cooldown, stake and progression. Confirm the structure-shadow equality report has zero differences and the adaptive candidate either passes every gate or remains explicitly blocked from release.

- [ ] **Step 4: Inspect repository hygiene**

Run: `git diff --check`

Expected: no output.

Run: `git status --short`

Expected: only intentional implementation/report files before the final commit, then no output after commit.

- [ ] **Step 5: Request code review before any integration decision**

Use `superpowers:requesting-code-review` against the implementation diff. Resolve correctness findings, rerun the affected focused tests, then rerun the full suite.

- [ ] **Step 6: Stop at the non-deployment checkpoint**

Report commits, test counts, replay gates and residual risks. Do not merge `main`, create a tag, package, upload, restart, or clear orders until the user gives a separate release instruction.

## Execution Order And Parallelism

- Tasks1-8 are the shared critical path and should be executed sequentially because they define the context and storage contract.
- After Task8, Task9-11 (adaptive profile) and Task12-13 (pure structure algorithm) may be developed by separate agents with disjoint files; integration still waits for both.
- Task14 is the single runtime integration point and must be done by one worker to avoid concurrent edits to`app/state.py`.
- Tasks15 and16 may run in parallel after Task14, provided one worker owns Python API files and the other owns static files.
- Tasks17-18 are sequential release-gate work. They never authorize deployment.

## Definition Of Done

- Every formal candidate and independent observation candidate has one immutable`DECISION_CONTEXT_V2` with a unique`decision_id`.
- The database enforces a 3GiB main-file page cap, reports capacity status, preserves256MiB for core writes, and never rewrites legacy rows.
- Observation queries and summaries cover all rows and 7/14/30/all windows; old and V2 audit counts are compatible.
- Daily qualification implements 7-day fast entry, 14-day stable retention and two consecutive joint failures before exit.
- Exact-key N12/N20 states refresh immediately after persisted settlement and enforce only the documented WARMUP/ACTIVE/WATCH/PAUSED behavior.
- Entry structure uses only240 closed 1-minute bars and remains provably shadow-only.
- API and UI expose enough fields to reproduce and filter decisions without duplicating the K-line analysis card.
- Full tests pass, replay is causal, structure equality has zero order/Webhook differences, and adaptive release gates are explicitly reported.
- Branch remains undeployed and unmerged until a later user instruction.
