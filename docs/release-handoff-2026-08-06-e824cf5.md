# 2026-08-06 生产发布交接文档

## 1. 文档目的

本文记录 `minute-strategy` 在 2026-08-06 发布的 1分钟波段方向与批次守卫版本，包括代码变更、生产配置、数据基线、验证结果、已知差异和后续观察要求。

接手人员应先阅读本文，再查看 `docs/current-strategy.md` 第32节和 `docs/superpowers/specs/2026-08-06-1m-wave-direction-guard-design.md`。

## 2. 发布身份

| 项目 | 当前值 |
|---|---|
| 仓库 | `PengYong-92/minute-strategy` |
| 生产代码分支 | `feature/1m-wave-direction-guard` |
| 生产提交 | `e824cf5e2c0e41038640fb50af26e188fc2e792f` |
| 提交摘要 | `fix: harden monitor runtime consistency` |
| 生产发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-e824cf5-20260806-233253` |
| 当前软链 | `/opt/victory-event-monitor/current` |
| systemd 服务 | `victory-event-monitor` |
| 内部监听 | `127.0.0.1:18080` |
| 公网域名 | `https://victory.easy-tx.com` |
| SQLite | `/opt/victory-event-monitor/shared/data/monitor.sqlite3` |
| 发布前版本 | `78c8eee`，目录 `event-contract-monitor-20260805-78c8eee` |

重要：截至本文记录时，`origin/main` 仍停留在 `78c8eee`。生产版本尚未合并到 `main`，不得从 `main` 直接重新部署，否则会回退本次策略修正。

## 3. 本次代码提交链

本次生产版本由以下连续提交组成：

| 提交 | 作用 |
|---|---|
| `891a844` | 禁止每日画像把实时 `WAIT` 或未过阈值信号提升为订单 |
| `9128c0d` | 新增只依赖已闭合1分钟K线的因果波段状态机 |
| `cd1017c` | 将1分钟波段方向守卫接入实时开单链路 |
| `57ed5f3` | 默认总未结订单上限从5降为2 |
| `e051433` | 新增首亏锁定、失败批次、全局冷却和恢复模式 |
| `f133ab8` | 恢复订单固定10U，不消费或生成18U资格 |
| `34d1aa0` | API、页面和订单画像增加波段与批次状态 |
| `799c360` | 加固波段锚点、资格取消、首次升级和重启恢复 |
| `e824cf5` | 修复币种切换竞态、重启开单间隔恢复和异步写入任务泄漏 |

## 4. 当前策略决策顺序

生产只开10分钟事件合约。1分钟K线是波段状态和实时指标的唯一行情输入，不加入30分钟、4小时或日线趋势投票。

开单链路顺序固定为：

1. 使用已闭合1分钟K线更新当前波段。
2. 原有10分钟量价、MACD、RSI和BOLL逻辑生成 `LONG`、`SHORT` 或 `WAIT`。
3. 波段守卫验证实时方向是否被当前波段允许。
4. 每日画像匹配同方向、同策略族、同策略标签和同WD/WE时段。
5. 检查评分、时段、并发、2分钟间隔、重复信号和风险守卫。
6. 波段批次守卫检查首亏锁定、失败批次、全局冷却和恢复状态。
7. 全部检查通过后，才分配10U基础金额或一个有效18U资格。
8. 原子保存订单和滚单资格，异步保存审计快照和观察记录。

任何后置模块都不能执行以下操作：

- 把 `WAIT` 改成 `LONG` 或 `SHORT`；
- 把 `LONG` 和 `SHORT` 互换；
- 用画像、滚单资格或守卫恢复状态绕过实时评分阈值。

## 5. 每日画像权限和生产门槛

每日画像键为：

```text
timeframe_minutes | strategy_family | strategy_tag | direction | threshold_segment
```

画像只验证已成立的实时方向，不提供方向，不降低评分阈值。

### 5.1 当前服务器门槛

服务器 drop-in：

```text
/etc/systemd/system/victory-event-monitor.service.d/20-daily-profile-selector.conf
```

关键值：

```text
DAILY_PROFILE_LOOKBACK_DAYS=7
DAILY_PROFILE_MIN_SAMPLES=20
DAILY_PROFILE_MIN_WIN_RATE=0.65
DAILY_PROFILE_MIN_EV=0
DAILY_PROFILE_EXIT_WIN_RATE=0.65
DAILY_PROFILE_EXIT_EV=0
DAILY_PROFILE_DEGRADED_RUNS=1
DAILY_PROFILE_EVALUATION_TIME=07:50
DAILY_PROFILE_ACTIVATION_TIME=08:00
```

含义：新画像和已启用画像都必须达到65%胜率且EV不低于0，样本数至少20。

### 5.2 65%配置的生效边界

65%门槛在 2026-08-06 23:56 重启后加载。它只修改了服务器 systemd 启动参数，没有修改仓库中的程序默认值。

因此当前存在明确配置差异：

- 服务器生产值：入选65%，退出65%；
- `app/server.py`、`scripts/run.sh` 和文档中的默认值：仍为60%。

2026-08-07 00:15 的生产快照仍是 `DPS-20260806-0800`，该快照在门槛调整前生成，共12个画像，其中包含低于65%的旧结果。这不是配置未加载。下一次评估时间为 2026-08-07 07:50，08:00 生效后才应全部满足65%。

## 6. 1分钟波段状态

### 6.1 计算窗口

- 主窗口：最近8根已闭合1分钟K线；
- ATR窗口：最近14根已闭合1分钟K线；
- 趋势切换：连续两次闭合分钟确认；
- 不读取未来K线，不使用事后重画的ZigZag。

主要计算：

```text
net = close[-1] - close[0]
path = sum(abs(close[i] - close[i-1]))
efficiency = abs(net) / path
direction_ratio = 与net同方向的分钟变化数 / 7
atr_strength = abs(net) / ATR14
range_position = (close[-1] - min(low)) / (max(high) - min(low))
```

趋势条件：

```text
efficiency >= 0.35
direction_ratio >= 0.60
atr_strength >= 0.50
```

### 6.2 状态到方向的映射

| 波段状态 | 允许方向 |
|---|---|
| `UP_LEG` | `LONG` |
| `DOWN_LEG` | `SHORT` |
| `RANGE_HIGH` | `SHORT`均值回归候选 |
| `RANGE_LOW` | `LONG`均值回归候选 |
| `TURN_UP` | 无，等待确认 |
| `TURN_DOWN` | 无，等待确认 |
| `RANGE_MID` | 无 |
| `UNKNOWN` | 无 |

波段只否决，不主动开单，也不把亏损方向机械反转。

## 7. 并发、批次守卫和金额叠加

### 7.1 并发和间隔

生产 drop-in：

```text
/etc/systemd/system/victory-event-monitor.service.d/30-order-concurrency.conf
```

当前值：

```text
MAX_OPEN_ORDERS=2
MIN_ORDER_GAP_MINUTES=2
```

基础单和18U第二级订单共同占用2单上限。

### 7.2 波段批次守卫

批次ID由以下字段组成：

```text
波段确认时间 | 波段状态 | 方向 | 时段 | 每日画像版本
```

规则：

- 同批次没有亏损且不足2单时，可以继续开单；
- 同批次第一笔已结算亏损后立即锁定，不再补位；
- 一赢一亏或两笔全亏都等待新波段；
- 60分钟内出现两个全亏批次时，全局冷却60分钟；
- 冷却后仅允许一笔恢复订单；
- 恢复盈利后解除恢复状态，恢复亏损后重新冷却60分钟。

旧按方向结算序列守卫已经关闭：

```text
RESULT_SEQUENCE_GUARD=0
```

兼容代码仍保留，但当前生产由波段批次守卫负责。

### 7.3 严格两阶段金额

```text
10U一级订单盈利 -> 产生一个18U资格
18U二级订单结算 -> 回到10U
```

约束：

- 最多并行1笔18U订单；
- 转折、新波段、画像版本切换、批次锁定和全局冷却会取消旧波段待用资格；
- 恢复订单固定10U；
- 恢复订单不消费18U资格，也不生成18U资格；
- 资格取消和订单开结算使用SQLite原子写，失败时返回 `STORAGE_ERROR` 并停止继续开单。

## 8. 重启和并发一致性修复

### 8.1 波段运行态恢复

SQLite `wave_runtime` 保存版本化波段快照、评估时间和确认锚点。重启后只重放快照之后的连续1分钟K线。

如果K线存在缺口，系统只使用缺口后的连续尾段建立新波段，不让旧18U资格跨缺口使用。首次升级或快照不可用时，系统保守继承持久化订单中的最近同状态锚点，优先保留首亏锁定。

### 8.2 币种切换隔离

轮询和预热请求会绑定 `symbol + generation`。请求返回时如果币种已经切换，旧响应直接丢弃，不能把BTC数据写到ETH状态或错误持久化到新币种。

### 8.3 开单间隔恢复

服务启动或切换币种时，从已持久化订单的最大 `opened_at` 恢复最后开单时间。重启不能绕过2分钟最小开单间隔。

### 8.4 异步写入回收

审计类异步写入 Future 完成后自动移除，不再无限增长。后台写入失败会进入 `last_error`，测试等待接口仍能获取失败。

## 9. 页面和审计字段

页面新增：

- `1分钟波段`状态；
- 当前允许方向；
- 波段确认次数和确认时间；
- `波段批次守卫`状态；
- 当前批次订单、胜负、冷却截止和恢复模式；
- 两单叠加状态。

信号、订单、观察记录和订单入口快照保存：

```text
wave_state
wave_raw_state
wave_window
wave_efficiency
wave_direction_ratio
wave_atr_strength
wave_confirmations
wave_confirmed_at
wave_batch_id
wave_guard_mode
wave_guard_status
wave_guard_reason
```

旧JSON缺少这些字段时使用兼容默认值，不阻断数据库恢复。

## 10. 发布和数据库基线

### 10.1 发布动作

- 发布包SHA-256：`2fbcf441534bf518e9ecf44bed37a8264e1e5958e2fadfa241c15898de1c354f`；
- 发布包不含 `data/`、SQLite、Git元数据或报告；
- 按要求未创建发布备份；
- nginx和证书配置未修改；
- 旧发布目录仍存在，但数据库订单清理没有备份，不能恢复。

### 10.2 清理结果

发布时在服务停止状态下，用一个SQLite事务清理：

| 表 | 清理前 | 清理后 |
|---|---:|---:|
| `orders` | 192 | 0 |
| `order_entry_snapshots` | 192 | 0 |
| `stake_progression_credits` | 82 | 0 |
| `stake_progression_runtime` | 1 | 0 |

保留：

| 表 | 发布时数量 |
|---|---:|
| `observation_signals` | 5716 |
| `daily_profile_selections` | 8 |

发布后的真实订单必须从新基线单独统计，不能与已清理的旧订单结果混合。

## 11. 验证记录

### 11.1 本地验证

- 完整单元测试：372项通过；
- Python `compileall`：通过；
- `node --check app/static/app.js`：通过；
- `git diff --check`：通过；
- 发布包解压后核心模块导入：通过；
- 按要求未运行历史回放或回测。

### 11.2 生产验证

发布和65%配置重启后的结果：

- systemd：`active/running`；
- `NRestarts=0`；
- 当前发布软链指向 `e824cf5`；
- HTTPS状态：200；
- SSL校验结果：0；
- 预热：`READY`；
- 预热K线：139680根；
- `last_error=null`；
- warning及以上日志：无；
- 订单总数：0；
- 未结订单：0；
- 待用18U资格：0；
- 最大未结订单：2；
- 最小开单间隔：120000毫秒；
- 旧结算序列守卫：`DISABLED`；
- 波段和批次守卫字段：已出现在API。

2026-08-07 00:15 的瞬时波段为 `TURN_DOWN`，只确认1次，因此允许方向为空。这是正常等待状态，不是故障。

## 12. 当前生产配置摘要

以下是交接时确认的关键生产值：

| 配置 | 当前值 | 说明 |
|---|---:|---|
| `MAX_OPEN_ORDERS` | 2 | 总未结订单上限 |
| `MIN_ORDER_GAP_MINUTES` | 2 | 最小开单间隔 |
| `STAKE` | 10 | 基础金额 |
| `STAKE_PROGRESSION` | 1 | 两阶段叠加开启 |
| `STAKE_PROGRESSION_MAX_ORDERS` | 2 | 固定两级 |
| `STAKE_PROGRESSION_MAX_ACTIVE` | 1 | 最多1笔18U |
| `DAILY_PROFILE_SELECTOR` | 1 | 每日画像开启 |
| `DAILY_PROFILE_LOOKBACK_DAYS` | 7 | 画像回看7天 |
| `DAILY_PROFILE_MIN_SAMPLES` | 20 | 最低独立样本数 |
| `DAILY_PROFILE_MIN_WIN_RATE` | 0.65 | 新画像门槛 |
| `DAILY_PROFILE_EXIT_WIN_RATE` | 0.65 | 已启用画像退出线 |
| `RESULT_SEQUENCE_GUARD` | 0 | 旧守卫关闭 |
| `ROLLING_EDGE_GUARD` | 0 | 旧滚动优势拦截关闭 |
| `PROFILE_GUARD` | 0 | 当前仅保留画像审计 |
| `OBSERVATION_PROFILE_PROMOTION` | 0 | 旧观察画像动态放行关闭 |
| `NO_WARMUP` | 0 | 历史预热开启 |
| `NO_PERSISTENCE` | 0 | SQLite开启 |
| `NO_WEBHOOK` | 1 | Webhook当前关闭 |

## 13. 日常检查命令

不要在命令、文档或日志中输出Webhook token和SSH私钥。

```bash
systemctl is-active victory-event-monitor
systemctl show victory-event-monitor --property=MainPID,NRestarts,ActiveEnterTimestamp --no-pager
readlink -f /opt/victory-event-monitor/current
journalctl -u victory-event-monitor --since=-12h -p warning --no-pager
curl -fsS https://victory.easy-tx.com/api/state
curl -fsS 'https://victory.easy-tx.com/api/orders?page=1&page_size=100'
curl -sS -o /dev/null -w 'http=%{http_code} ssl=%{ssl_verify_result}\n' https://victory.easy-tx.com/
```

每天08:00后至少检查：

1. `daily_profile_selection.version` 是否更新；
2. `selected_profiles` 是否全部满足胜率不低于65%；
3. `last_error` 是否为空；
4. `warmup.status` 是否为 `READY`；
5. `wave_state` 和 `wave_batch_guard` 是否存在；
6. 是否出现异常密集同方向订单；
7. 18U订单是否只来自有效10U赢单资格；
8. journal是否出现存储、预热或波段快照错误。

## 14. 后续样本统计口径

当前发布后订单数为0，尚不能评价新策略胜率或收益。

积累样本后应按以下维度分别统计：

- `LONG`、`SHORT`；
- WD/WE时段；
- 每日画像版本；
- `UP_LEG`、`DOWN_LEG`、`RANGE_HIGH`、`RANGE_LOW`；
- `wave_batch_id`；
- `NORMAL`、`RECOVERY`；
- 10U一级和18U二级；
- `WAVE_DIRECTION_BLOCKED`；
- `WAVE_BATCH_LOSS_LOCKED`；
- `WAVE_GLOBAL_COOLDOWN`；
- 实际订单的胜率、PnL、EV、最大回撤和连亏长度。

不要把观察单的事后结果当成实际订单，也不要因单日或单个时段小样本立即修改门槛。

## 15. 已知风险和待处理事项

1. 生产提交尚未合并到 `main`。应先合并或明确继续以功能分支作为发布源。
2. 65%门槛目前只在服务器启动配置中，仓库默认仍是60%。以后重新部署时必须保留 drop-in，或另行把默认值同步到代码。
3. 本节点按要求没有历史回测，只有单元测试和生产运行验证。
4. 新订单基线为0，批次守卫和18U实际效果需要生产样本验证。
5. 数据库订单清理未备份，旧192笔订单及关联资格不可从当前数据库恢复。
6. `WAVE_BATCH_UNAVAILABLE` 在当前没有可执行信号或批次ID时是兼容状态，不等于守卫失效。
7. `TURN_UP`、`TURN_DOWN`、`RANGE_MID` 和 `UNKNOWN` 允许方向为空是设计行为。

## 16. 建议下一步

1. 2026-08-07 08:00后验证65%画像快照，记录入选数量和多空分布。
2. 只以本次发布后新订单作为策略效果样本。
3. 样本达到预定规模后，先分析方向、波段、时段和批次，不先调参。
4. 将 `feature/1m-wave-direction-guard` 合并到主分支，避免生产版本长期游离。
5. 决定是否把65%默认值写回代码；在此之前保留服务器 drop-in。

## 17. 交接验收清单

- [ ] 确认生产软链提交为 `e824cf5`；
- [ ] 确认服务为 `active` 且 `NRestarts=0`；
- [ ] 确认预热为 `READY` 且 `last_error` 为空；
- [ ] 确认并发2、间隔2分钟；
- [ ] 确认画像入选和退出门槛均为65%；
- [ ] 确认旧结算序列守卫关闭；
- [ ] 确认波段与批次守卫API字段存在；
- [ ] 确认08:00后画像全部满足65%；
- [ ] 确认后续部署不从旧 `main` 覆盖生产；
- [ ] 确认新订单按本发布节点独立统计。
