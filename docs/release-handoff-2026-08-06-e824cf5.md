# 2026-08-06 至 2026-08-07 生产发布交接文档

## 1. 文档目的

本文记录 `minute-strategy` 在 2026-08-06 发布的1分钟波段方向与批次守卫，以及2026-08-07发布的可配置开单评分阈值版本，包括代码变更、生产配置、数据基线、验证结果、已知差异和后续观察要求。

接手人员应先阅读本文，再查看 `docs/current-strategy.md` 第32至33节、`docs/superpowers/specs/2026-08-06-1m-wave-direction-guard-design.md` 和 `docs/superpowers/specs/2026-08-07-configurable-entry-threshold-design.md`。

## 2. 发布身份

| 项目 | 当前值 |
|---|---|
| 仓库 | `PengYong-92/minute-strategy` |
| 生产代码分支 | `feature/1m-wave-direction-guard` |
| 生产提交 | `75a3745cb53c39637e82663c9de1a065b8d3307e` |
| 提交摘要 | `fix: clarify overridden threshold reasons` |
| 生产发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-75a3745-20260807-114208` |
| 当前软链 | `/opt/victory-event-monitor/current` |
| systemd 服务 | `victory-event-monitor` |
| 内部监听 | `127.0.0.1:18080` |
| 公网域名 | `https://victory.easy-tx.com` |
| SQLite | `/opt/victory-event-monitor/shared/data/monitor.sqlite3` |
| 发布前版本 | `bc5fa35`，目录 `event-contract-monitor-bc5fa35-20260807-113613` |

重要：截至本文记录时，`origin/main` 仍停留在 `78c8eee`，生产功能分支为 `feature/1m-wave-direction-guard`。生产版本尚未合并到 `main`，不得从 `main` 直接重新部署，否则会回退波段守卫、运行态加固和可配置阈值。

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
| `d3806a3` | 固化可配置开单阈值的设计与实施计划 |
| `2dad304` | 新增阈值启动参数、画像先于波段的执行顺序、阈值审计字段和订单列表列 |
| `bc5fa35` | 未入选画像也暴露实际开单阈值，同时保留原始动态阈值 |
| `75a3745` | 清除覆盖模式下旧“动态阈值未达线”拦截文案，避免页面误读 |

## 4. 当前策略决策顺序

生产只开10分钟事件合约。1分钟K线是波段状态和实时指标的唯一行情输入，不加入30分钟、4小时或日线趋势投票。

开单链路顺序固定为：

1. 使用已闭合1分钟K线更新当前波段。
2. 原有10分钟量价、MACD、RSI和BOLL逻辑计算评分、动态阈值、主画像标签和观察方向。
3. 每日画像精确匹配周期、策略族、策略标签、观察方向和WD/WE时段。
4. `auto` 使用原动态阈值；数值模式使用启动阈值，达到后才形成画像方向。
5. 波段守卫验证画像方向是否被当前1分钟波段允许。
6. 检查时段、并发、2分钟间隔、重复信号和风险守卫。
7. 波段批次守卫检查首亏锁定、失败批次、全局冷却和恢复状态。
8. 全部检查通过后，才分配10U基础金额或一个有效18U资格。
9. 原子保存订单和滚单资格，异步保存审计快照和观察记录。

任何模块都不能执行以下操作：

- 从研究观察候选列表直接生成订单方向；
- 在数值模式缺少明确 `observe_direction` 时生成方向；
- 把 `LONG` 和 `SHORT` 互换；
- 用阈值覆盖绕过每日画像精确匹配；
- 用画像或滚单资格绕过1分钟波段、并发、间隔和批次守卫。

## 5. 每日画像权限和生产门槛

每日画像键为：

```text
timeframe_minutes | strategy_family | strategy_tag | direction | threshold_segment
```

`TRADE_SCORE_THRESHOLD=auto` 时，画像只验证已成立且达到动态阈值的实时方向。显式数值模式只允许当天已入选的主画像使用其 `observe_direction` 形成候选方向；研究观察候选仍然不能执行。

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

开单评分阈值使用独立 drop-in：

```text
/etc/systemd/system/victory-event-monitor.service.d/60-trade-score-threshold.conf
```

当前内容：

```ini
[Service]
Environment=TRADE_SCORE_THRESHOLD=0
```

仓库默认仍是 `auto`。生产值0只对当天精确入选画像移除评分限制，不移除画像、波段、并发、间隔、滚动和批次守卫。

### 5.2 65%配置的生效边界

本小节记录2026-08-06重启后的历史边界；当前2026-08-07快照和验证结果见第18节。

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

### 11.2 2026-08-06历史生产验证

发布和65%配置重启后的结果：

- systemd：`active/running`；
- `NRestarts=0`；
- 当时发布软链指向 `e824cf5`；
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
| `TRADE_SCORE_THRESHOLD` | 0 | 已入选画像实际开单评分阈值 |
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

2026-08-07阈值版本的生产样本边界为 `2026-08-07 11:15:54 CST`。该时刻订单数为0、观察样本5850；验收结束时订单仍为0、观察样本5851。不得把此边界前的订单用于评价阈值0版本。

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
3. 阈值0节点按要求没有历史回测，只有380项单元测试、独立代码复审和生产运行验证。
4. 新订单基线为0，阈值0的开单数量、胜率、批次守卫和18U实际效果都需要生产样本验证。
5. 数据库订单清理未备份，旧192笔订单及关联资格不可从当前数据库恢复。
6. `WAVE_BATCH_UNAVAILABLE` 在当前没有可执行信号或批次ID时是兼容状态，不等于守卫失效。
7. `TURN_UP`、`TURN_DOWN`、`RANGE_MID` 和 `UNKNOWN` 允许方向为空是设计行为。

## 16. 建议下一步

1. 只以 `2026-08-07 11:15:54 CST` 后的新订单作为阈值0效果样本。
2. 逐单检查订单列表中的评分、实际开单阈值和原始动态阈值。
3. 样本达到预定规模后，先按方向、画像、波段、阈值和批次分析，再决定是否从0向上提高阈值。
4. 将 `feature/1m-wave-direction-guard` 合并到主分支，避免生产版本长期游离。
5. 决定是否把65%默认值写回代码；在此之前保留服务器 drop-in。

## 17. 交接验收清单

- [x] 确认生产软链提交为 `75a3745`；
- [x] 确认服务为 `active` 且 `NRestarts=0`；
- [x] 确认预热为 `READY` 且 `last_error` 为空；
- [x] 确认并发2、间隔2分钟；
- [x] 确认画像入选和退出门槛均为65%；
- [x] 确认旧结算序列守卫关闭；
- [x] 确认波段与批次守卫API字段存在；
- [x] 确认08:00后画像全部满足65%；
- [ ] 确认后续部署不从旧 `main` 覆盖生产；
- [x] 确认新订单按 `2026-08-07 11:15:54 CST` 节点独立统计。

## 18. 2026-08-07 可配置阈值发布记录

### 18.1 发布制品

| 项目 | 值 |
|---|---|
| 代码提交 | `2dad3040eddb5918e9c9175140f40bd278546266` |
| 功能分支 | `feature/1m-wave-direction-guard` |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-2dad304-20260807-111248` |
| 最小包 | `event-contract-monitor-2dad304-20260807-111248-minimal.tar.gz` |
| SHA-256 | `4a61d186fe00a7aead43fd876a9680b945403cf6743aa22820fd6f3f0c7d4938` |
| 服务启动边界 | `2026-08-07 11:15:54 CST` |

最小包只包含 `app/`、`scripts/run.sh`、`README.md` 和 `.gitignore`，不包含测试、研究报告、本地数据、SQLite、Git元数据或密钥。服务器解压后已执行导入和阈值状态断言。按要求未创建发布备份，未清空订单或观察样本，未修改nginx和证书配置。

### 18.2 功能变化

1. 新增 `TRADE_SCORE_THRESHOLD=auto|0..95` 和 `--trade-score-threshold`，空值等同 `auto`。
2. `auto` 保持旧动态阈值；数值模式只对每日精确入选且具有明确观察方向的当前主画像生效。
3. 每日画像先确定候选方向，再执行1分钟波段方向守卫，画像不能绕过波段。
4. 研究观察候选、未入选画像和缺少观察方向的信号不能因阈值0生成订单。
5. 信号和订单新增 `calculated_threshold`；旧订单API缺字段时回退到 `threshold`。
6. 订单列表新增“开单阈值”列，显示实际阈值、评分和原始动态阈值。
7. `/api/state.trade_score_threshold` 明确返回 `AUTO` 或 `OVERRIDE` 模式及数值。

阈值0以后仍顺序执行画像、波段、最大2笔未结订单、2分钟间隔、重复信号、批次首亏锁定、全局冷却、滚动优势和画像守卫。它不是全局无条件开单开关。

### 18.3 发布验证

发布后确认：

```text
service=active
NRestarts=0
release=/opt/victory-event-monitor/releases/event-contract-monitor-2dad304-20260807-111248
https=200
ssl_verify_result=0
warmup.status=READY
warmup.loaded_klines=141120
kline_count=140000
last_error=null
trade_score_threshold.mode=OVERRIDE
trade_score_threshold.value=0.0
orders=0
observations=5851
journal warning+=0
```

当前每日画像为 `DPS-20260807-0800`，共5个：

| 方向 | 时段 | 样本 | 胜率 | EV |
|---|---|---:|---:|---:|
| SHORT | WD-11 | 25 | 72.00% | 2.96U |
| SHORT | WD-07 | 27 | 70.37% | 2.67U |
| LONG | WD-01 | 26 | 69.23% | 2.46U |
| SHORT | WD-08 | 24 | 66.67% | 2.00U |
| LONG | WD-04 | 26 | 65.38% | 1.77U |

验收时当前主画像为 `LONG WD-03`，波段为 `RANGE_LOW` 并允许LONG，但WD-03未入选，因此决策是 `DAILY_PROFILE_NOT_SELECTED`。从服务启动边界到验收时，审计记录只有该决策，没有 `BELOW_THRESHOLD`。这证明阈值0已生效，同时未绕过每日画像。

### 18.4 后续调高阈值

只修改 drop-in 中的数值，例如：

```ini
[Service]
Environment=TRADE_SCORE_THRESHOLD=20
```

然后执行：

```bash
systemctl daemon-reload
systemctl restart victory-event-monitor
```

每次调整必须记录新值、服务 `ActiveEnterTimestamp`、订单起始ID和当日画像版本。调整后先确认 `/api/state.trade_score_threshold`，再把新边界后的订单单独统计。不要同时修改65%画像门槛、波段参数或订单守卫，否则无法归因开单量和胜率变化。

## 19. 2026-08-07 阈值显示修复

阈值0首次发布后，未入选画像仍在最新K线分析卡显示原动态阈值，例如 `0.0 / 78.5`，原因文本也保留“动态阈值78.5，不开单”。启动配置实际已经是0，真实决策为 `DAILY_PROFILE_NOT_SELECTED`，但页面表达容易被理解为阈值覆盖未生效。

修复分两步发布：

| 提交 | 发布时间 | 作用 |
|---|---|---|
| `bc5fa35` | `2026-08-07 11:37:24 CST` | 未入选画像返回 `threshold=0`、`calculated_threshold=原动态阈值` |
| `75a3745` | `2026-08-07 11:43:11 CST` | 删除旧动态阈值拦截后缀，原因首句直接说明实际阈值和画像结果 |

当前发布：

```text
release=/opt/victory-event-monitor/releases/event-contract-monitor-75a3745-20260807-114208
sha256=89a653c9270829d093046223e30b94d155adfc0e3724ac5e2d505d912253f5d1
service=active
NRestarts=0
warmup.status=READY
last_error=null
```

验收时状态：

```text
score=0.0
threshold=0.0
calculated_threshold=78.5
decision=DAILY_PROFILE_NOT_SELECTED
reason=低位平量横盘：信号不足；开单阈值0.0（原始动态阈值78.5，仅记录）；当前画像未入选 ... WD-03
```

这两次修复只改变未入选画像的阈值审计和页面文案，不改变画像选择、方向、波段守卫或开单集合。因此策略效果样本边界仍使用 `2026-08-07 11:15:54 CST`；分析信号审计字段时，需把 `2026-08-07 11:43:11 CST` 作为新显示口径边界。

## 20. 2026-08-08 订单量恢复发布

### 20.1 回归原因

本节点确认订单量下降不是单一阈值问题，而是四项规则叠加：

1. 每日画像选择器只检查主信号，已经积累出高胜率的研究观察画像不能成为订单。
2. `TRADE_SCORE_THRESHOLD=0` 反而把未成立的主 `WAIT` 信号按画像方向强制提升。清理前12笔订单中2胜10负、PnL -84U，11笔评分为0，说明该放行路径质量明显低于原观察画像路径。
3. 1分钟波段方向守卫和波段批次守卫默认直接否决方向或后续订单，进一步压缩订单量。
4. 画像窗口改为7天后仍统一要求20个独立样本；每个周末小时在7天内理论上最多约12个独立10分钟样本，因此所有 `WE` 画像都不可能入选。

### 20.2 当前决策链

本次恢复后的生产行为：

1. 每日画像同时检查已成立主信号和研究观察候选。
2. 观察候选按 `周期|策略族|策略标签|方向|时段` 完整键入选后可以成为正式订单。
3. 未成立的主 `WAIT` 信号不能再由数值评分阈值提升。
4. `TRADE_SCORE_THRESHOLD` 保留启动兼容和API审计，数值模式返回 `AUDIT_ONLY`，不改变开单集合；生产使用 `auto`。
5. 1分钟波段继续计算、持久化和展示，但默认不改变订单方向。
6. 波段批次守卫默认关闭；同方向连续3亏冷却20分钟和滚动优势守卫恢复启用。
7. 并发上限仍为2，最小开单间隔仍为2分钟，10U/18U两阶段金额逻辑不变。
8. 每日画像胜率入选和退出线均为60%，EV不低于0；工作日最少20样本，周末最少10样本。
9. 同一天修改画像配置时立即重评当天快照，配置不变时仍每天只评估一次。

保留的加固和审计包括：异步SQLite保存、订单入口快照、`calculated_threshold`、订单阈值页面列、波段运行态、币种切换隔离、开单间隔恢复和异步写入错误上报。

### 20.3 提交与发布

| 项目 | 值 |
|---|---|
| 恢复观察画像订单提交 | `0ff3b11` |
| 配置变化当天重评提交 | `f3a12ff` |
| 周末独立样本门槛提交 | `fa5f1bc` |
| 分支 | `feature/1m-wave-direction-guard` |
| 最终发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-fa5f1bc-20260808-132214` |
| 最小包 | `event-contract-monitor-fa5f1bc-20260808-132214-minimal.tar.gz` |
| 包大小 | `116KB` |
| SHA-256 | `218ba061519d8c0d04c159a8a4c929cca903575347b4f4f9fa2f6d1c73c101f8` |
| 最终服务边界 | `2026-08-08 13:23:43 CST` |

按要求没有创建数据库备份，没有修改nginx和SSL。最终启动前清空模拟订单、订单入口快照和金额叠加资格/运行态；观察样本和每日画像历史均保留。

### 20.4 最终生产参数

| 参数 | 值 |
|---|---:|
| `DAILY_PROFILE_LOOKBACK_DAYS` | 7 |
| `DAILY_PROFILE_MIN_SAMPLES` | 20 |
| `DAILY_PROFILE_WEEKEND_MIN_SAMPLES` | 10 |
| `DAILY_PROFILE_MIN_WIN_RATE` | 0.60 |
| `DAILY_PROFILE_EXIT_WIN_RATE` | 0.60 |
| `DAILY_PROFILE_MIN_EV` / `EXIT_EV` | 0 |
| `TRADE_SCORE_THRESHOLD` | auto |
| `MAX_OPEN_ORDERS` | 2 |
| `MIN_ORDER_GAP_MINUTES` | 2 |
| `ROLLING_EDGE_GUARD` | 1 |
| `RESULT_SEQUENCE_GUARD` | 1 |
| `RESULT_SEQUENCE_LOSS_STREAK` | 3 |
| `RESULT_SEQUENCE_COOLDOWN_MINUTES` | 20 |
| `RESULT_SEQUENCE_SCOPE` | DIRECTION |
| `PROFILE_GUARD` | 0 |
| `STAKE_PROGRESSION` | 1 |
| `STAKE_PROGRESSION_MAX_ACTIVE` | 1 |

### 20.5 发布验收和新样本边界

最终验收结果：

```text
service=active
NRestarts=0
warmup.status=READY
warmup.loaded_klines=142560
last_error=null
https=200
ssl_verify_result=0
orders=0
open_orders=0
observation_signals=6161
daily_profile_selections=10
trade_score_threshold.mode=AUTO
rolling_edge.observe_only=false
result_sequence_guard.enabled=true
wave_state.enabled=false
wave_batch_guard.enabled=false
daily_profiles=22（WD 13 / WE 9）
```

当前9个周末画像：

| 方向 | 时段 | 样本 | 胜率 | EV |
|---|---|---:|---:|---:|
| SHORT | WE-10 | 12 | 75.00% | 3.50U |
| LONG | WE-20 | 11 | 72.73% | 3.09U |
| LONG | WE-21 | 10 | 70.00% | 2.60U |
| SHORT | WE-14 | 10 | 70.00% | 2.60U |
| SHORT | WE-16 | 10 | 70.00% | 2.60U |
| SHORT | WE-22 | 10 | 70.00% | 2.60U |
| LONG | WE-04 | 12 | 66.67% | 2.00U |
| SHORT | WE-15 | 11 | 63.64% | 1.45U |
| SHORT | WE-13 | 10 | 60.00% | 0.80U |

`2026-08-08 13:23:43 CST` 为新的实际样本边界，订单ID从1重新开始。当前时段 `WE-05` 未入选，因此验收时决策仍为 `DAILY_PROFILE_NOT_SELECTED`；这只表示当前完整画像键未命中，不代表周末或全局暂停。后续只使用该边界后的订单评价本节点，先观察订单量、LONG/SHORT胜率、PnL、连亏长度和守卫命中情况，再调整其他参数。
