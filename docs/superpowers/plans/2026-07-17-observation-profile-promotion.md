# Observation Profile Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不放松 LONG 滚动守卫和过热边界的前提下，用近 7 天独立已结算观察样本动态恢复正 EV 时段，并开放 WD-02/WD-23 SHORT 小额实单。

**Architecture:** `MonitorState` 在静态时段画像返回 `SESSION_BLOCKED` 后，按 `timeframe + strategy_family + direction + threshold_segment` 汇总当前时点之前已结算且互不重叠的观察单。满足样本、胜率、EV 和边际阈值时构造可执行信号并重新经过订单、滚动优势和画像守卫；WD-02/WD-23 SHORT 可直接执行，但由模拟器的基础金额时段保护隔离滚单状态。

**Tech Stack:** Python 3.10+、dataclasses、unittest、SQLite、Bash。

---

### Task 1: 状态机回归测试

**Files:**
- Modify: `tests/test_state.py`
- Modify: `tests/test_simulator.py`

- [ ] 写入 8 个近 7 天同画像已结算观察样本，断言 `SESSION_BLOCKED` 转为 `OPENED`。
- [ ] 写入窗口外样本，断言仍为 `SESSION_BLOCKED`。
- [ ] 断言 WD-02/WD-23 SHORT 实单使用基础金额，其他 SHORT 继续返回 `SHORT_OBSERVE_ONLY`。
- [ ] 运行 `python3 -m unittest tests.test_state tests.test_simulator`，确认新测试因功能缺失而失败。

### Task 2: 动态观察画像与 SHORT 小口放行

**Files:**
- Modify: `app/state.py`
- Modify: `app/simulator.py`

- [ ] 在 `MonitorState` 增加可配置的 7 天、8 样本、68% 胜率、3U EV、8 分边际参数。
- [ ] 仅使用 `settled_at <= current_time` 的历史样本计算画像，防止未来数据泄漏。
- [ ] 仅覆盖 `SESSION_BLOCKED`，重新执行冷却、持仓、滚动优势和画像守卫；不覆盖 `OVERHEATED`、`ROLLING_EDGE_BLOCKED`。
- [ ] 开放配置时段内 SHORT；基础金额时段保护同时适用于 LONG 和 SHORT。
- [ ] 运行目标测试确认通过。

### Task 3: 启动参数和策略文档

**Files:**
- Modify: `app/server.py`
- Modify: `scripts/run.sh`
- Modify: `tests/test_packaging.py`
- Modify: `README.md`
- Modify: `docs/current-strategy.md`

- [ ] 增加动态观察画像和 SHORT 时段启动参数，帮助文本全部使用中文。
- [ ] 默认基础金额保护列表加入 WD-02/WD-23。
- [ ] 文档记录本次参数、放行顺序和不放宽的守卫。
- [ ] 运行打包参数测试。

### Task 4: 无前视回放与验证

**Files:**
- Modify: `scripts/replay_observation_candidates.py`（仅在现有脚本不能复用数据库候选时）

- [ ] 使用服务器 SQLite 首单至末单结束时间以及可用 K 线回放。
- [ ] 每个候选仅使用该时点前已结算观察样本，强制同一时间最多一单。
- [ ] 输出总订单、方向/时段分组、胜率、固定金额 PnL、EV、最大回撤和拦截原因。
- [ ] 运行完整单元测试并检查工作区差异。
