# Remove Inactive 30-Minute Order Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除不会被生产调用的30分钟订单周期分支，同时保证当前10分钟候选、画像键和最终开单链路不变。

**Architecture:** 生产入口继续固定使用 `LIVE_TRADE_TIMEFRAMES = (10,)`。先用特征测试锁定当前10分钟通用 LONG/SHORT 画像身份，再让公开策略分析函数拒绝非10分钟周期，最后删除只服务30分钟订单周期的 edge 表和条件分支。`mtf_30m_bias` 当前仍参与10分钟评分与方向判断，本阶段保留，避免把代码清理伪装成策略变更。

**Tech Stack:** Python 3、`unittest`、原生 JavaScript、Shell。

---

### Task 1: 锁定当前10分钟生产候选

**Files:**
- Modify: `tests/test_strategy.py`

- [x] **Step 1: 添加通用画像身份特征测试**

构造上影拒绝和下影收复的已闭合1分钟K线，断言10分钟分析输出继续使用：

```python
self.assertEqual(
    (signal.strategy_family, signal.strategy_tag, signal.observe_direction),
    ("short_observe", "generic_short_observe", "SHORT"),
)
```

LONG 对称断言为 `("long_observe", "generic_long_observe", "LONG")`。同时断言 `profile_key`、`timeframe_minutes` 和最终 `actionable` 语义不变。

- [x] **Step 2: 运行特征测试确认当前实现通过**

Run: `python3 -m unittest tests.test_strategy.StrategyTest.test_generic_short_profile_identity_is_stable tests.test_strategy.StrategyTest.test_generic_long_profile_identity_is_stable -v`

Expected: `OK`。这些是重构保护测试，必须在删除代码前通过。

- [x] **Step 3: 添加仅允许10分钟分析的失败测试**

```python
with self.assertRaisesRegex(ValueError, "only 10-minute analysis is supported"):
    analyze_volume_price(klines, timeframe_minutes=30)
```

- [x] **Step 4: 运行失败测试确认 RED**

Run: `python3 -m unittest tests.test_strategy.StrategyTest.test_volume_price_analysis_rejects_non_10m_timeframes -v`

Expected: FAIL，当前实现仍会分析30分钟。

### Task 2: 删除30分钟订单周期分支

**Files:**
- Modify: `app/strategy.py`
- Modify: `tests/test_strategy.py`

- [x] **Step 1: 在策略入口拒绝非10分钟周期**

在 `analyze_volume_price` 的数据检查前加入：

```python
if timeframe_minutes not in LIVE_TRADE_TIMEFRAMES:
    raise ValueError("only 10-minute analysis is supported")
```

- [x] **Step 2: 删除30分钟订单周期静态配置**

删除 `SESSION_EDGE_BY_TIMEFRAME[30]`，并删除以下只处理30分钟候选的逻辑：

```python
timeframe_minutes == 30
timeframe_penalty = 1.5 if signal.timeframe_minutes == 30 else 0.0
```

`_session_adjusted_threshold` 和 `_session_min_edge` 保留10分钟当前结果，只把初始30分钟加成改为固定0和固定 `MIN_TRADE_EDGE`。`_candidate_rank` 返回 `edge + session_quality`。

- [x] **Step 3: 删除旧30分钟策略测试**

删除直接调用 `analyze_volume_price(..., timeframe_minutes=30)`、验证30分钟 edge 表、30分钟分析窗口和30分钟候选排序的测试。保留所有10分钟测试以及新添加的非10分钟拒绝测试。

- [x] **Step 4: 运行策略测试确认 GREEN**

Run: `python3 -m unittest tests.test_strategy -v`

Expected: 所有策略测试通过。

- [x] **Step 5: 检查10分钟订单决策相关差异**

Run: `git diff -- app/strategy.py tests/test_strategy.py`

Expected: 不修改 `_score_setup`、`_strategy_identity`、`_select_daily_profile_signal`、画像键、阈值值、LONG/SHORT 条件或任何守卫。

### Task 3: 文档化阶段边界并验证

**Files:**
- Modify: `docs/current-strategy.md`
- Modify: `docs/superpowers/specs/2026-08-09-10m-only-profile-batch-guard-design.md`

- [x] **Step 1: 更新当前策略文档**

明确生产和策略入口只接受10分钟，不再保留30分钟订单 edge、阈值或候选分支。同时记录 `mtf_30m_bias` 仍是10分钟内部依赖，待后续独立做等价性评估后再删除，本阶段不得改动。

- [x] **Step 2: 在原设计文档添加阶段说明**

记录用户将实施拆分为两阶段，本次只执行不改变10分钟行为的30分钟订单死代码清理；完整画像批次守卫及活跃30分钟偏向删除不在本次提交。

- [x] **Step 3: 运行聚焦和全量验证**

Run: `python3 -m unittest tests.test_strategy tests.test_state tests.test_server -v`

Expected: `OK`。

Run: `python3 -m unittest discover -s tests -v`

Expected: 全量测试通过，测试数不低于删除旧30分钟测试后的基线。

Run: `python3 -m compileall -q app tests`

Expected: exit 0。

Run: `node --check app/static/app.js`

Expected: exit 0。

Run: `bash -n scripts/run.sh`

Expected: exit 0。

- [x] **Step 4: 静态范围核对**

Run: `rg -n "timeframe_minutes == 30|SESSION_EDGE_BY_TIMEFRAME.*30|30分钟.*开单|30分钟.*候选" app tests docs/current-strategy.md`

Expected: 不再存在30分钟订单周期分支；允许保留明确标注为10分钟内部兼容依赖的 `mtf_30m_bias`。

- [x] **Step 5: 提交**

```bash
git add app/strategy.py tests/test_strategy.py docs/current-strategy.md docs/superpowers/specs/2026-08-09-10m-only-profile-batch-guard-design.md docs/superpowers/plans/2026-08-09-remove-inactive-30m-order-path.md
git commit -m "refactor: remove inactive 30m order path"
```
