# minute-strategy 发布交接文档

## 1. 文档目的

本文汇总 `minute-strategy` 截至2026-08-19的生产发布、策略变更、运行配置、数据基线、验证结果、已知差异和后续观察要求。各发布章节保留当时的事实快照，章节中的“当前”仅指该章节记录时点。

`docs/release-handoff.md` 是项目唯一的发布交接文档。后续发布只更新或追加本文，不再创建带日期、提交号或版本号的交接文档副本。

接手人员应先阅读 `docs/current-strategy.md` 掌握现行逻辑，再使用本文核对发布边界。历史设计和实施过程不再保留在工作树，可通过 Git 历史及 `v2026.08.10-profile-degradation-guard` 标签追溯。

### 1.1 当前阅读基线

| 项目 | 当前值 |
|---|---|
| 当前功能基线 | `6474dd81b005ca8c1d011a739bca71335f26b1e6`（方向脉冲影子与结算即更新） |
| 固化标签 | `v2026.08.16-direction-pulse-shadow` |
| 当前生产提交 | `6474dd81b005ca8c1d011a739bca71335f26b1e6` |
| 当前生产事实 | 第41节 |
| 本地未部署功能 | `feature/adaptive-resident-profiles` 的自适应常驻画像、价格结构影子、V2审计/容量治理及严格因果发布门槛，见第42节 |
| 下一开发检查点 | 使用复制的SQLite完成第42节回放；硬门槛全部通过前不得发布 |

第2至41节是历史发布、本地开发或生产事实快照；生产现状以第41节为准。第42节只记录当前功能分支的本地实现和发布前门槛，不代表生产已经切换。生产运行代码来自 `main`，`feature/1m-wave-direction-guard` 和 `feature/websocket-low-latency` 保留为历史功能分支。第27至28节记录的统计与观察能力已经发布，但其中明确标记为观察候选的规则仍未启用真实拦截。

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
3. 未成立的主 `WAIT` 信号不能由数值评分阈值独立提升；但具有明确观察方向且完整画像键已入选时，由每日画像赋予开单资格。
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

## 21. 2026-08-08 完整恢复每日画像放行

### 21.1 故障现象与根因

`fa5f1bc` 发布后长时间没有订单。生产库聚合审计显示，从 `2026-08-08 13:23:43 CST` 到本次修复前共有598个独立信号事件，全部被标记为 `DAILY_PROFILE_NOT_SELECTED`。其中88个事件的完整画像键实际已经入选：`WE-13` 30次、`WE-10` 25次、`WE-14` 21次、`WE-15` 12次。

根因是上次恢复只允许“已成立主信号”和研究观察候选参与每日画像匹配，主 `WAIT` 信号仍被提前排除。因此即使主信号已经给出明确的 `observe_direction`，且 `周期|策略族|策略标签|方向|时段` 完整键命中每日画像，也无法获得开单资格，造成画像选择结果与实际决策相互矛盾。

### 21.2 修复后的规则

1. 主信号始终参与每日画像完整键匹配，方向优先使用 `observe_direction`，缺失时才使用原始 `direction`。
2. 主 `WAIT` 信号只有在观察方向明确且完整画像键已入选时，才能由每日画像提升为正式订单。
3. 研究观察候选继续按完整画像键参与匹配。
4. `TRADE_SCORE_THRESHOLD` 仍是审计参数，评分阈值本身不能提升 `WAIT` 信号。
5. 未入选画像、方向不匹配或完整键不匹配时仍保持拦截；滚动优势守卫、同方向连亏守卫、并发上限和最小开单间隔继续生效。

### 21.3 提交、发布与验证

| 项目 | 值 |
|---|---|
| 修复提交 | `0c39518` |
| 分支 | `feature/1m-wave-direction-guard` |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-0c39518-20260808-232457` |
| 最小包 | `event-contract-monitor-0c39518-20260808-232457-minimal.tar.gz` |
| 包大小 | `116KB` |
| SHA-256 | `80033239cb82af8add368a7204395f163f46a8028e94d0a6b8ebb0afcb9e7992` |
| 服务重启边界 | `2026-08-08 23:26:13 CST` |

修复前先新增回归测试，证明主 `WAIT` 信号在画像完整命中时仍不能开单；实现修复后全量383项测试通过，并通过 Python 编译、前端 JavaScript 语法、启动脚本语法和差异检查。

生产重启后连续产生两笔订单，证明放行链路和并发上限均已恢复：

```text
ID  方向   时段   开单时间                 金额  状态
1   SHORT  WE-15  2026-08-08 23:25:59 CST  10U  OPEN
2   SHORT  WE-15  2026-08-08 23:27:59 CST  10U  OPEN

strategy_family=short_observe
strategy_tag=generic_short_observe
profile_sample_count=11
profile_win_rate=63.64%
profile_ev=1.45U
```

第一笔订单使用服务启动时最近一根已闭合K线，因此K线时间比服务重启边界早14秒。第二笔订单间隔2分钟产生，随后达到 `MAX_OPEN_ORDERS=2`，不再继续开单。数据库没有再次清空：修复前订单数为0，新订单继续从ID 1记录。后续评估以本节发布边界和订单ID 1为起点。

## 22. 2026-08-09 删除30分钟订单死分支

### 22.1 变更边界

本次只删除生产从未调用的30分钟订单周期能力：

- 30分钟 LONG session edge 表；
- 非10分钟 `analyze_volume_price` 分析入口；
- 30分钟阈值和最小边际加成；
- 30分钟 regime 阈值修正；
- 30分钟候选排序惩罚。

10分钟开单条件、`generic_long_observe` / `generic_short_observe` 画像身份、每日画像匹配、滚动 edge、并发、2分钟间隔、两阶段金额和现有守卫均未修改。`mtf_30m_bias` 仍参与当前10分钟判断，因此本节点明确保留，不把活跃策略变更混入死代码清理。

### 22.2 等价性验证

旧代码中被删除的阈值、最小边际、regime 和候选排序分支只在 `timeframe_minutes == 30` 时生效；生产入口固定使用 `LIVE_TRADE_TIMEFRAMES = (10,)`，10分钟对应旧加成为0。新增特征测试锁定通用 LONG/SHORT 的策略族、标签、观察方向、画像键和不可直接开单语义。

发布前验证结果：

```text
聚焦测试：131项通过
全量测试：384项通过
Python compileall：通过
JavaScript node --check：通过
Shell bash -n：通过
```

### 22.3 发布身份

| 项目 | 值 |
|---|---|
| 生产代码提交 | `12e33e0` |
| 分支 | `feature/1m-wave-direction-guard` |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-12e33e0-20260809-231155` |
| 最小包 | `event-contract-monitor-12e33e0-20260809-minimal.tar.gz` |
| 包大小 | `154KB` |
| SHA-256 | `12211eccb73f486454b9160c92b033536ea175d4017cda413d8e3833c9fc7fb0` |
| `strategy.py` SHA-256 | `116a394b3ea0accafc04024274412e9baf642c74eaead58a82eeef947d01c91a` |
| 服务重启边界 | `2026-08-09 23:13:54 CST` |

### 22.4 发布后核对

发布未清空订单、观察画像、滚单资格或预热缓存，也未修改 systemd、Nginx和SSL配置。发布前后状态对比：

```text
service=active
NRestarts=0
warning日志=0
https页面=200
api/state=200
last_error=null
orders=70（41胜 / 29负 / 未结0）
pending_credits=1
daily_profile_version=DPS-20260809-0800
daily_profile_min_win_rate=60%
trade_score_threshold=AUTO
max_open_orders=2
min_order_gap_ms=120000
result_sequence_guard=ON / DIRECTION / 3亏 / 20分钟
wave_state=OFF
wave_batch_guard=OFF
profile_guard=OFF
```

预热K线由发布前142560根增加到发布后144000根，是重启时从共享缓存加载了更完整的数据范围；预热状态始终为 `READY`，不是策略或配置变化。线上 `strategy.py` 与本地 `12e33e0` 文件哈希一致。

## 23. 2026-08-10 实时画像退化守卫（本地未部署）

### 23.1 变更内容

本地分支已完成订单决策流程精简和实时画像退化守卫，但本节不代表生产发布。机械准入层删除旧 segment-day 的 `OrderPolicy.risk_pause_reason` / `RISK_PAUSED`；新守卫按“完整画像键 + 当前 DPS 版本”读取已结算真实订单，固定连续亏损3单后进入冷却。

冷却结束只允许一笔10U基础试探，不消费已有18U资格；试探未结算时阻止同画像继续开单。试探赢后恢复 `NORMAL`，并允许生成下一笔18U资格，即使该单同时属于波段恢复；试探亏从该单结算时间重新进入完整冷却。唯一新增启动参数为 `PROFILE_DEGRADATION_COOLDOWN_MINUTES=60`，`0` 表示关闭，不新增 enable、最小样本、胜率或EV参数。

### 23.2 控制层边界

| 层级 | 范围 | 触发 | 恢复 |
|---|---|---|---|
| 每日画像 | 完整画像/7天观察 | 60%与EV | 次日重评 |
| 实时画像退化 | 完整画像/当前DPS实单 | 固定连续亏损3单 | 配置冷却+基础试探 |
| 方向序列 | LONG或SHORT实单 | 连亏阈值 | 方向冷却 |
| 滚动优势 | 现有滚动key | 胜率/EV退化 | 滚动样本恢复 |

### 23.3 提交与验证

| 功能 | 提交 |
|---|---|
| 机械准入只保留并发、间隔和重复信号 | `def817a` |
| 画像退化纯状态机及输入加固 | `d0f5674` / `4738b81` |
| 试探字段持久化与18U资格语义 | `d099f22` / `9ab84a6` |
| 开单决策分阶段编排 | `b2bbe36` |
| 状态接入与状态快照刷新 | `70d6490` / `4d01a14` |
| 唯一启动参数及 argparse 校验 | `1c02d89` / `7485996` |
| 页面状态卡与订单试探标记 | `d4f778c` |
| 测试补强 | `cae48f8` / `fc1c88e` |
| 未来试探过滤、恢复资格保留与关闭路径精简 | `e2707eb` |

本地全量测试共428项通过。当前有意保持的默认值：

| 参数 | 默认值 |
|---|---:|
| `DAILY_PROFILE_MIN_WIN_RATE` | `0.60` |
| `DAILY_PROFILE_EXIT_WIN_RATE` | `0.60` |
| `MAX_OPEN_ORDERS` | `2` |
| `PROFILE_DEGRADATION_COOLDOWN_MINUTES` | `60` |

### 23.4 发布边界

| 项目 | 状态 |
|---|---|
| 发布目录 | 未部署 |
| 服务重启边界 | 未部署 |
| 生产样本边界 | 未部署 |

本节没有清空模拟订单，没有修改服务器启动参数，也没有改动 systemd、Nginx或SSL配置。第22节记录的生产事实保持不变。

## 24. 项目目录边界

为避免运行代码、研究产物和历史过程文档混淆，工作树按以下边界维护：

| 路径 | 用途 | 维护规则 |
|---|---|---|
| `app/` | 服务端、策略、持久化和页面运行代码 | 全部保留并纳入测试 |
| `scripts/` | 启动、打包、回放和离线研究工具 | 全部保留；不得把离线脚本接入生产开单链路 |
| `tests/` | 单元、集成和打包回归测试 | 全部保留 |
| `docs/current-strategy.md` | 当前策略事实 | 策略逻辑变化时同步更新 |
| `docs/release-handoff.md` | 发布与交接事实 | 每次发布后追加记录 |
| `data/` | 本地行情、SQLite和研究输入 | Git忽略，保留在本机，不进入发布包 |
| `reports/` | 回放和分析结果 | Git忽略，保留在本机，不进入发布包 |
| `.venv/` | 本地Python环境 | Git忽略，保留在本机，不进入发布包 |
| `dist/` | 可重新生成的发布包 | Git忽略；发布包生成和使用后可清理 |

旧过程计划、设计稿和早期策略前置审计已从工作树删除。需要复盘时使用 Git 历史或固化标签，不再复制回当前文档目录。

## 25. 2026-08-10 实时画像退化守卫与当天统计发布

### 25.1 发布内容

本次将第23节已经完成但未部署的实时画像退化守卫正式发布，并增加页面与API的当天交易统计。10分钟开单画像、观察候选放行、滚动优势、方向连亏守卫、两单并发、2分钟间隔和10U/18U两阶段金额逻辑均保持不变。

新增生产行为：

1. 按“完整画像键 + 当前DPS版本”检查已结算真实订单；同画像连续亏损3笔后冷却60分钟。
2. 冷却结束只允许一笔10U基础试探；试探未结算前阻止该画像继续开单。
3. 试探盈利恢复正常并可生成下一笔18U资格；试探亏损从结算时间重新冷却60分钟。
4. API `stats.today` 按北京时间结算日期统计当天PnL、已结订单、胜负和胜率。
5. 页面同时展示总盈亏、总胜率、今日盈亏和今日胜率；总统计口径未改变。

### 25.2 发布身份

| 项目 | 值 |
|---|---|
| 生产代码提交 | `23fc78dca05bff549f2470268764b18088f4363d` |
| 生产代码分支 | `main`，功能分支代码同步 |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-23fc78d-20260810-161745` |
| 最小包 | `event-contract-monitor-23fc78d-20260810-161745-minimal.tar.gz` |
| 包大小 | `122134 bytes` |
| SHA-256 | `71350abe7dbfe6b680bc80458974f98dd4cc85047a2e15666b4a51587a9c2548` |
| 服务启动边界 | `2026-08-10 16:22:20 CST` |

最小包仅包含 `app/`、`scripts/run.sh`、`README.md` 和 `.gitignore`。本次未修改systemd、Nginx或SSL配置，未清空、迁移或备份SQLite；订单、观察样本、每日画像和滚单资格沿用共享数据目录。

### 25.3 发布前后数据一致性

发布前后总统计保持一致：

```text
total_orders=141
settled_orders=141
open_orders=0
wins=88
losses=53
win_rate=62.41%
total_pnl=+198.0U
```

发布后首次返回的北京时间当天统计：

```text
date=2026-08-10
settled_orders=67
wins=43
losses=24
win_rate=64.18%
pnl=+115.2U
```

上述当天统计由现有订单的 `settled_at` 即时聚合，不新增统计表，也不改变订单结算结果。

### 25.4 生产验收

```text
release=/opt/victory-event-monitor/releases/event-contract-monitor-23fc78d-20260810-161745
service=active
NRestarts=0
https=200
ssl_verify_result=0
warmup.status=READY
warmup.loaded_klines=145440
kline_count=140000
last_error=null
profile_degradation_guard=ON / NORMAL / 60分钟
daily_profile_version=DPS-20260810-0800
daily_profiles=24
daily_profile_min_win_rate=60%
trade_score_threshold=AUTO
max_open_orders=2
min_order_gap_ms=120000
result_sequence_guard=ON / DIRECTION / 3亏 / 20分钟
rolling_edge_guard=ON
warning日志=0
```

本地发布前验证为429项测试通过，Python编译、JavaScript语法、Shell语法、Git差异检查和最小包服务器导入均通过。

### 25.5 后续观察口径

`2026-08-10 16:22:20 CST` 是实时画像退化守卫的生产启用边界。评估该守卫时只使用此时间之后新结算的订单，重点记录完整画像键、DPS版本、连续亏损数、冷却命中、试探订单结果和后续18U资格；不要把边界前141笔订单计入守卫效果样本。

当天统计是展示口径，不是新的策略筛选条件。日切按北京时间00:00执行，总收益和总胜率继续覆盖该币种数据库中的全部订单。

## 26. 2026-08-12 线上订单复核与LONG预优化结论

### 26.1 当前生产事实

本节只记录线上订单分析，没有修改代码、服务器参数、数据库或生产服务。当前生产代码仍为 `23fc78dca05bff549f2470268764b18088f4363d`。

截至 `2026-08-12 09:55 CST`：

```text
total_orders=290
settled_orders=290
open_orders=0
wins=176
losses=114
win_rate=60.69%
total_pnl=+311.2U
today_settled=22
today_win_rate=54.55%
today_pnl=+13.6U
```

当前运行参数仍为最大未结2笔、最小间隔2分钟、方向连续3亏冷却20分钟、同画像同DPS连续3亏冷却60分钟；1分钟波段方向守卫和波段批次守卫关闭。当前每日画像版本为 `DPS-20260812-0800`，线上画像门槛为60%。

### 26.2 LONG预优化未触发

按北京时间结算口径，今日LONG为20单12胜8负、胜率60.00%、PnL `+33.6U`；已有LONG未结时开出的第二笔LONG为14单9胜5负、胜率64.29%、PnL `+34.8U`。今日没有 `TURN_UP` LONG，也没有“已有LONG后上涨超过10bps继续追多”的订单。

因此上一轮候选优化没有触发：LONG没有继续拖累，第二笔LONG也没有恶化。当前不收紧最大持仓、不收紧第二仓准入、不恢复完整趋势守卫，也不调整连续亏损阈值。

最新5笔LONG连续亏损由两个画像版本组成：旧版 `DPS-20260811-0800` 的WD-23连续亏3笔，新版 `DPS-20260812-0800` 的WD-01亏2笔。它们不是同一完整画像键和同一DPS版本的连续5亏，不能合并评价。新版只有2笔样本，继续观察，不据此修改策略。

### 26.3 统计口径问题

北京时间00:00自然日统计会同时包含08:00前旧DPS和08:00后新DPS，适合财务日展示，但不适合单独评价当前画像版本。后续策略评估应保留总统计和00:00自然日统计，并新增与当前画像 `effective_from`、`effective_until`、`version` 精确对齐的画像周期统计。该建议对应第27节本地修改。

### 26.4 保留的LONG预优化候选

以下项目继续保持 `OBSERVE_ONLY`，当前没有编码或部署：

1. 只使用当前DPS画像周期样本判断，不混入08:00前旧画像订单；
2. 当前DPS已结LONG不少于20单、胜率低于55.56%且PnL小于0时，才确认LONG继续拖累；
3. 已有LONG未结时产生的第二笔LONG不少于10单，且该组胜率低于55.56%或PnL小于0时，才收紧第二仓；
4. 若上述条件成立，优先评估禁止 `TURN_UP` LONG和禁止已有LONG后上涨超过10bps继续追多；
5. 不全局降低最大未结订单数，不按 `DOWN_LEG` 机械禁止LONG，不修改当前高质量SHORT第二仓；
6. 若当前DPS样本不足，只累计数据，不因少量连亏调整策略。

任何候选项实施后都必须记录提交、服务重启边界和新样本起点，不能与修改前订单混合评价。

## 27. 2026-08-12 画像周期表现统计（本地未部署）

### 27.1 变更内容

本地在累计表现和北京时间自然日表现之外新增当前画像周期表现：

1. `AccountSimulator.stats()` 接受当前已激活画像周期描述；
2. 订单必须同时满足结算时间位于 `[effective_from, effective_until)` 且 `daily_profile_version` 精确一致；
3. `/api/state.stats.profile_period` 返回激活状态、DPS版本、生效区间、PnL、订单数、胜负和胜率；
4. 页面顶部新增“画像周期盈亏”和“画像周期胜率”；
5. 没有已激活画像时API返回结构化零值，页面显示 `-`。

累计统计和00:00自然日统计保持不变。新增统计不参与开单、结算、画像选择、滚动优势、金额叠加或风险守卫。

### 27.2 代码范围

| 文件 | 作用 |
|---|---|
| `app/simulator.py` | 聚合当前DPS画像周期订单 |
| `app/state.py` | 把当前已激活画像传入统计层 |
| `app/static/index.html` | 增加两张画像周期卡片 |
| `app/static/app.js` | 展示画像周期PnL和胜率 |
| `tests/test_simulator.py` | 锁定08:00边界、DPS版本隔离和无画像零值 |
| `tests/test_server.py` | 锁定API结构 |
| `tests/test_packaging.py` | 锁定页面发布内容 |

### 27.3 当前边界

本节状态为本地已实现、未部署服务器：没有发布目录、没有服务重启时间、没有清空订单，也没有修改systemd、Nginx或SSL。生产仍运行第26节记录的 `23fc78d`。正式发布后必须追加提交号、发布目录、服务重启边界、API验收结果和新的样本起点。

## 28. 2026-08-12 当前优化状态

### 28.1 已优化：第二并发位置识别与LONG综合准入观测基础

状态：`本地实现完成，未部署；只增加独立记录和统计，不启用真实拦截`。

`2026-08-12 17:05:34 CST` 重新读取线上全部324笔订单，其中323笔已结、1笔未结。以下统计只使用已结订单，并按每笔开单时是否已有尚未到期订单重建 `FIRST/SECOND`，不使用10U/18U或 `stake_progression_step` 代替并发位置：

| 上下文 | 已结 | 胜率 | PnL | EV |
|---|---:|---:|---:|---:|
| LONG_FIRST | 41 | 65.85% | `+104.8U` | `+2.56U` |
| LONG_SECOND | 116 | 50.00% | `-148.0U` | `-1.28U` |
| SHORT_FIRST | 36 | 55.56% | `-25.6U` | `-0.71U` |
| SHORT_SECOND | 130 | 67.69% | `+341.6U` | `+2.63U` |

交接文档上次313笔边界之后新增10笔已结订单，4胜6负、胜率40.00%、PnL `-52.0U`。其中第二并发LONG新增8笔，3胜5负、胜率37.50%、PnL `-42.0U`；问题没有反转，第二并发LONG已从108笔50.93%/-106.0U进一步下降到116笔50.00%/-148.0U。

但不能把“全部第二并发LONG”视为同质样本，也不能反向把某个画像直接变成第二单开关。当前 `DPS-20260812-0800` 从08:00生效后已结35笔，17胜18负、胜率48.57%、PnL `-58.4U`。同一DPS内不同完整画像的第二并发表现明显分化。下表只用于证明画像是综合评估维度之一，不是未来准入规则：

| 完整画像与并发位置 | 已结 | 胜率 | PnL | 结论 |
|---|---:|---:|---:|---|
| LONG WD-05 SECOND | 11 | 63.64% | `+35.2U` | 保留，不应被全局规则误伤 |
| LONG WD-08 SECOND | 8 | 37.50% | `-42.0U` | 重点观察，尚未达到10笔独立样本 |
| LONG WD-01 SECOND | 1 | 0.00% | `-10.0U` | 样本不足 |
| SHORT WD-02 SECOND | 8 | 50.00% | `-9.6U` | 继续观察，不能用LONG规则处理 |

已完成内容：

1. LONG首单继续使用现行逻辑；
2. 不全局禁用或统一收紧第二并发LONG；`完整画像键 × DPS版本 × FIRST/SECOND` 仅作为回溯统计切片，不作为单独准入开关；
3. 信号、订单、入口快照、观察样本和画像统计明确记录 `FIRST/SECOND`，并与滚单阶段分开；旧订单按开单和到期时间恢复该字段；
4. 画像胜率和EV按并发位置独立统计，作为未来LONG第二单综合评估的一个输入，不单独决定放行或拦截；
5. 独立统计与当前DPS生效区间对齐，不混用08:00前后的画像；
6. LONG第二单的目标是开单前综合评估：画像只是一个维度，还需结合当前1分钟量价指标、波段质量、已有首单上下文、两单关系和近期结果；不用4小时或日线趋势；
7. `/api/order-profile.by_profile_slot` 对单一 `画像 × SECOND` 少于10笔标记 `COLLECTING`；达到10笔标记 `WATCH`，同时覆盖至少两个DPS才标记 `cross_dps_ready=true`；
8. `WD-05 SECOND` 当前为正样本，不能因 `WD-08 SECOND` 亏损被一并关闭；同理，不能只因WD编号或画像历史胜率直接关闭任何LONG第二单。

下表继续保留为单维证据，用于检验1分钟波段在综合评分中的解释力；它不表示“第二并发LONG仅在 `UP_LEG` 放行”，也不能替代多维准入评估：

| 第二并发LONG波段 | 已结 | 胜率 | PnL | EV |
|---|---:|---:|---:|---:|
| UP_LEG | 15 | 73.33% | `+51.2U` | `+3.41U` |
| 非UP_LEG | 101 | 46.53% | `-199.2U` | `-1.97U` |

当前DPS内 `UP_LEG` 第二并发LONG只有3笔、2胜1负、PnL `+12.4U`。上次313笔之后新增的8笔第二并发LONG全部不是 `UP_LEG`，说明波段状态可能具有区分能力，但 `UP_LEG` 样本仍只有15笔，尚不足以证明跨画像、跨DPS稳定。原完整因果回放结果252笔、胜率61.90%、PnL `+296.0U`、最大回撤86.4U、最长连亏4笔继续作为候选证据，不替代线上独立样本，也不把波段状态升级为单条件硬门槛。

验收时固定以胜率为第一要素，其次检查收益和EV、订单量、最大回撤及最长连亏。回放必须按时间顺序重新计算并发位置和10U/18U资格；发布后从新边界重新累计实际样本。

### 28.2 已优化但未发布：当前画像周期统计

第27节本地代码已增加当前DPS画像周期盈亏和胜率统计，目前尚未提交、合并或部署，且不参与开单权限判断。

后续动作：

1. 完成本地测试和代码审查；
2. 提交并合并 `main` 后发布服务器；
3. 已增加“方向 × 并发位置”的 `by_direction_slot` 观察口径；
4. 发布后记录提交号、服务重启边界和新样本起点。

### 28.3 已优化：按并发位置审计现有守卫

状态：`本地审计能力实现完成，继续保持现有参数，未部署`。

实时画像退化守卫继续以 `2026-08-10 16:22:20 CST` 为生产样本边界。边界后共有182笔已结订单，胜率57.69%、PnL `+74.8U`。线上订单中没有 `profile_degradation_probe=true` 的试探单；仅凭订单无法统计被守卫拦截而未开出的候选，后续仍需在信号审计中按完整画像键、DPS版本和并发位置记录守卫命中。

对“连续2亏后立即收紧”的候选做了单步影子匹配。该匹配不重算被拦截后的完整订单路径，只用于判断可能误伤的下一批实际订单：

| 2亏候选 | 全部命中实际订单 | 命中组胜率/PnL | 守卫上线边界后命中 | 边界后命中组胜率/PnL |
|---|---:|---:|---:|---:|
| 按方向，冷却20分钟 | 32 | 62.50% / `+40.0U` | 15 | 66.67% / `+30.0U` |
| 按方向×并发位置，冷却20分钟 | 22 | 68.18% / `+69.2U` | 13 | 69.23% / `+44.8U` |
| 按画像×DPS×并发位置，冷却60分钟 | 16 | 68.75% / `+50.8U` | 10 | 70.00% / `+38.8U` |

这些2亏候选会优先拦掉盈利恢复单，不符合“胜率优先”。因此方向连续3亏冷却20分钟、同画像同DPS连续3亏冷却60分钟继续保持；不收紧为2亏。2分钟开单间隔和最大2笔并发也暂不改变。

本地已新增 `/api/signal-audit-summary`：最近5000条信号按决策、完整画像键、DPS版本、并发位置，以及方向连亏、画像退化、波段批次和滚动优势状态汇总。`/api/order-profile.profile_degradation_probes` 单独汇总退化试探单结算结果。发布后可直接统计守卫命中、恢复就绪、试探输赢和并发位置；后续参数修改仍需完整因果回放，不能与第二并发LONG真实准入同时上线。

现有画像守卫继续保持禁用、仅观察。线上已结样本中，影子规则标记为 `would_block` 的106笔胜率63.21%、PnL `+179.6U`，未标记的217笔胜率58.06%、PnL `+93.2U`。当前规则拦截的并不是退化组，直接启用会过滤更多盈利订单；在重新设计规则并完成因果回放前，不允许提升为真实准入守卫。

### 28.4 已优化但未发布：影子质量评分记录

状态：`本地已实现、仅记录、未部署`。

新增 `QS_V1_SHADOW` 质量评分，固定模式为 `SHADOW_ONLY`。评分以50分为基准，记录画像样本/胜率/EV、量比、涨跌幅、价格位置、收盘强度、MACD柱及变化、RSI、BOLL位置、1分钟波段状态、波段效率、方向占比和ATR强度。每个分项及评分时原始输入均随记录保存。

评分按开单前的方向和并发位置区分四种上下文：`LONG_FIRST`、`LONG_SECOND`、`SHORT_FIRST`、`SHORT_SECOND`。实际订单、入口快照和对应观察记录沿用同一个开单前上下文；独立观察候选也在当次决策前计算并记录。旧订单和旧观察数据没有评分字段时按空值兼容加载，统计中标记为 `UNSCORED`。

该评分不参与以下任何路径：

1. `Signal.actionable`；
2. 每日画像选择或胜率门槛；
3. LONG/SHORT方向判断；
4. 动态阈值和 `TRADE_SCORE_THRESHOLD`；
5. 1分钟波段方向、批次、连续亏损、画像退化或滚动优势守卫；
6. 最大并发、开单间隔、重复信号和10U/18U滚单资格。

评分入口具有故障隔离：计算异常只写入 `last_error` 并返回原信号，现有订单决策继续执行，不允许影子记录故障中断行情更新或开单主流程。

同时修正订单分析的负 `edge` 分箱：低于首个截点的值现在归入 `<0`，不再错误落入 `>=100`。订单分析新增质量评分分箱和方向/并发上下文汇总，为后续比较各评分区间的真实胜率、EV和订单量提供数据；在达到独立样本要求前，不把质量评分升级为准入条件。

代码范围：`app/quality_score.py`、`app/models.py`、`app/state.py`、`app/simulator.py`、`app/order_profile.py`。测试覆盖评分不改变既有 `score`、`threshold`、`daily_profile_selected` 和 `actionable`，相同K线流启用/禁用评分时订单身份与决策一致，评分异常不阻断开单，首单/第二单上下文、订单与观察一致性、入口快照、SQLite往返和负edge分箱。

### 28.5 已优化：订单画像统计排除未结订单

状态：`本地修复完成，未部署`。

`2026-08-12 17:05:43 CST` 的线上状态为324笔总订单、323笔已结、1笔未结，真实结果为193胜130负；但 `/api/order-profile` 返回324笔、193胜131负，把未结订单计入 `orders` 后通过 `losses = orders - wins` 间接当成亏损。该问题不影响真实订单结算和主页面累计统计，但会压低订单画像的胜率和EV，并污染分时段、波段、画像守卫及后续影子质量评分评估。

已完成内容：

1. `/api/order-profile` 的胜负、胜率、PnL和EV只使用 `result in {WIN, LOSS}` 的已结入口快照；
2. 未结入口快照单独返回 `open_orders`，不并入亏损；
3. 分组、特征分箱、风险提示和守卫回放统一使用同一已结样本集合；
4. 增加包含未结订单的回归测试，锁定 `orders = wins + losses`；
5. 修复后重新生成线上订单画像，再评价任何画像或评分阈值。

修复后的接口同时返回 `snapshot_count`、已结 `sample_count` 和 `open_orders`。所有结果分组统一使用已结集合，`orders = wins + losses`；未结订单不再进入画像胜率、EV、风险提示、守卫回放或影子评分分箱。

### 28.6 本次待优化闭环边界

本地代码已完成第28节所有可执行项：画像周期统计、`FIRST/SECOND`独立记录和旧数据恢复、完整画像/DPS/并发位置分组、守卫信号审计、退化试探结果、`QS_V1_SHADOW`记录、负edge分箱以及未结订单统计修复。

以下不是未完成代码项，而是必须等待新线上样本的观察结论：

1. 不启用“第二并发LONG仅在 `UP_LEG` 放行”；
2. 不关闭全部第二并发LONG；
3. 不把质量评分升级为准入阈值；
4. 不把连续亏损守卫从3亏收紧为2亏；
5. 不启用当前画像守卫影子规则。

这些候选只有在新版本发布后，按并发位置记录完整开单前上下文并完成因果回放，才能重新进入代码修改范围。画像键和DPS版本用于切片验证稳定性，不能被误解为唯一优化单位或直接开关。

### 28.7 仓库固化与验证

功能提交：`863ff5d168262e9cb2fc7754b838107f3cbd1f2a`（`feat: add strategy optimization observability`）。固化标签：`v2026.08.12-optimization-observability`，指向包含本节最终交接记录的文档提交。

提交前验证：

```text
python3 -m unittest discover -s tests -p 'test_*.py'
Ran 446 tests in 8.092s
OK

python3 -m compileall -q app scripts tests
exit=0

git diff --check
exit=0

./scripts/package.sh --output-dir /tmp/minute-strategy-package-check --name minute-strategy-verify
tar.gz与zip均生成成功；归档包含app/quality_score.py、静态页面和唯一交接文档，不包含.git、data或__pycache__。
```

本次只提交并推送仓库及标签，不部署服务器、不重启服务、不清空模拟订单。当前生产仍是 `23fc78dca05bff549f2470268764b18088f4363d`；后续部署必须从本标签或其对应 `main` 提交发布，并另行记录生产提交、发布目录、服务重启边界及新样本起点。

## 29. 2026-08-12 优化观测版本生产发布

### 29.1 发布身份与边界

| 项目 | 发布值 |
|---|---|
| 生产提交 | `02369bf2d2f17372486eb378f3473f3cf154b108` |
| 固化标签 | `v2026.08.12-optimization-observability` |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-02369bf-20260812-175520` |
| 发布前目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-23fc78d-20260810-161745` |
| 当前软链 | `/opt/victory-event-monitor/current` |
| 服务重启边界 | `2026-08-12 17:57:54 CST` |
| systemd服务 | `victory-event-monitor`，`active/running` |
| 内部监听 | `127.0.0.1:18080` |
| 公网域名 | `https://victory.easy-tx.com` |
| SQLite | `/opt/victory-event-monitor/shared/data/monitor.sqlite3` |

本次采用不含 `data/` 的发布包原子切换软链，保留发布前目录作为快速回退点。没有清空、复制、迁移或替换SQLite，没有修改systemd启动参数、Nginx逻辑或SSL配置。

发布前后订单连续性检查：

```text
orders rows: 324 -> 324
max order_id: 324 -> 324
open orders: 0 -> 0
```

因此既有模拟订单完整保留。服务继续直接使用原共享数据库路径，新订单从324号之后连续编号。

### 29.2 验收结果

发布前重新执行：

```text
python3 -m unittest discover -s tests -v
Ran 446 tests in 8.133s
OK

python3 -m compileall -q app scripts tests
exit=0
```

发布包为 `event-contract-monitor-02369bf-20260812-175520.tar.gz`，SHA-256：

```text
7f8871892235648c5edb1bf22e76274218920915a6592decb7630538792dcafb
```

包内容检查确认不含 `data/` 和 `monitor.sqlite3`。服务器端解包后编译通过，服务状态为 `active/running`，最近服务警告为空。

运行态验收：

```text
symbol=BTCUSDT
kline_count=140000
last_error=null
total_orders=324
open_orders=0
active_profile=DPS-20260812-0800
order_profile.snapshot_count=324
order_profile.sample_count=324
signal_audit.sample_count=5000
```

`/api/state` 已返回画像周期统计，`/api/order-profile` 已返回 `FIRST/SECOND` 与完整画像/DPS分组，`/api/signal-audit-summary` 已返回守卫和并发位置汇总。HTTP入口正常跳转HTTPS，TLS域名为 `victory.easy-tx.com`，HTTPS `/api/state` 返回 `200`。预热数据、订单恢复和新页面静态资源均已加载。

### 29.3 新样本口径

`2026-08-12 17:57:54 CST` 是本次观测字段的生产起点。该时间之后的新信号、观察样本和订单开始原生记录 `QS_V1_SHADOW`、开单前 `FIRST/SECOND` 上下文及完整守卫审计；旧订单的并发位置可按开单和到期时间重建，但旧订单没有影子质量评分时必须继续标记为 `UNSCORED`。

本次发布不启用以下真实拦截：第二并发LONG仅限 `UP_LEG`、质量评分准入、2亏冷却、画像影子守卫。后续必须按本节边界累计新样本，先比较胜率，再比较PnL、EV、订单量、最大回撤和最长连亏，不能把发布前后的观测字段完整度混为同一批样本。

## 30. 2026-08-12 Webhook最低延迟异步发送设计

状态：`本地已实现并验证，未发布服务器`。

功能提交：`07cfc3f`（`perf: dispatch webhooks without blocking`）。

### 30.1 现状检查

生产 systemd 主服务配置仍为 `NO_WEBHOOK=1`，运行接口返回 `webhook.enabled=false`，因此当前不会对外发送Webhook。本次只修改仓库代码，不修改服务器配置、不重启服务、不发布。

修改前代码在订单持久化成功后，于行情更新线程和 `MonitorState` 全局锁内同步执行 `urllib.request.urlopen()`，默认最多等待5秒并读取完整响应体。接收端变慢时会同时阻塞下一轮行情处理、状态读取和币种切换。服务器到接收域名的无副作用HEAD探测总耗时约43至73毫秒，当时的主要结构风险是同步等待，不是网络连接速度。

### 30.2 已确认设计

采用每个有效开单信号一个独立守护线程的方案：

1. 订单成功写入SQLite后立即构造Webhook请求并启动守护线程；无持久化模式则以内存开单成功为边界；
2. 每个信号独立触发，不进入单线程队列，慢请求不能阻塞后一条信号；
3. 主决策线程只承担payload构造和线程启动，不等待DNS、TCP、TLS、HTTP响应或响应体；
4. 后台线程不重试、不回写SQLite、不更新成功时间、payload、错误或页面状态，发送异常直接丢弃；
5. HTTP实现只负责发出POST，不读取响应体，也不根据返回状态改变订单或策略状态；
6. `NO_WEBHOOK=1`、Webhook URL、token、金额和10分钟周期payload保持兼容；
7. WAIT、守卫拦截、观察信号和持久化失败仍不发送，避免把未成立订单误推给接收端。

该设计以最低主流程延迟为目标，明确接受进程退出、网络错误和接收端失败导致的信号丢失；不实现队列、确认、重试、补偿或outbox。

### 30.3 实施与验收计划

已完成：

1. 阻塞传输测试证明调用方在后台传输释放前已经返回；
2. 两条并发测试证明后一条信号能在前一条仍阻塞时启动，不存在单队列串行等待；
3. 后台传输异常静默丢弃，不进入订单、策略、页面或运行错误状态；
4. 默认HTTP传输不读取响应体；兼容保留的发送结果字段固定为 `null`，不记录真实结果；
5. Webhook在订单原子提交并更新去重/间隔状态后、入口画像汇总之前立即触发；
6. 慢传输集成测试证明行情更新线程和 `MonitorState` 状态锁不会等待接收端；
7. payload构造、JSON序列化、线程启动和后台传输异常均静默丢弃，不穿透开单主流程；
8. WAIT、守卫拦截、观察信号和订单持久化失败不触发Webhook的既有测试继续通过；
9. 现行策略说明、README和页面Webhook状态展示已同步最低延迟语义。

本地验证：

```text
python3 -m unittest discover -s tests -v
Ran 454 tests in 7.663s
OK

python3 -m compileall -q app scripts tests
exit=0

node --check app/static/app.js
exit=0

git diff --check
exit=0
```

本次只提交并推送 `main`，不创建发布标签，不发布服务器，不修改生产 `NO_WEBHOOK=1`，不重启服务，也不触碰模拟订单。

## 31. 2026-08-12 LONG第二单优化定义澄清

### 31.1 本节是后续工作的唯一有效解释

本节修正第26节、第28节中可能把“画像统计”误读为“画像准入”的表述。后续讨论、设计、回放和实现LONG第二单优化时，以本节为准。

产生理解偏差的原因已经明确：`by_profile_slot`、`by_profile_dps_slot` 和“独立画像”是为了回溯分析而增加的统计切片，但此前把统计切片误当成了未来决策单位。二者必须严格分开：

- **统计维度**回答“过去哪些条件下表现好或差”；
- **准入机制**回答“当前这一笔LONG第二单，综合当前信息后是否值得开”；
- 统计中的高胜率或低胜率画像只能提供一个特征，不能单独生成白名单、黑名单或永久开关。

### 31.2 固定范围

本优化只处理 `LONG_SECOND`，定义为：候选方向为LONG，且开单前已经存在一笔未结LONG订单；已有SHORT不改变LONG的并发位置。

以下路径明确不属于本优化：

1. `LONG_FIRST` 保持现有逻辑；
2. `SHORT_FIRST` 和 `SHORT_SECOND` 保持现有逻辑，不增加额外步骤、不改变参数；
3. 不降低全局最大并发2笔，不关闭所有LONG第二单；
4. 不按WD/WE时段、完整画像、DPS版本或单一波段状态直接关闭LONG第二单；
5. 不引入4小时、日线或其他大周期趋势，只使用当前10分钟事件信号及1分钟实时量价上下文；
6. 不把10U/18U滚单阶段误当成FIRST/SECOND并发位置。

### 31.3 正确目标

LONG第二单不是第一单的机械复制。候选通过现有基础信号、每日画像、通用守卫、最大并发和开单间隔后，如果开单前已有一笔未结LONG订单，还必须执行一次独立的LONG第二单综合准入评估。

该评估需要综合以下维度：

| 维度 | 作用 | 是否已记录 |
|---|---|---|
| 当前信号基础质量 | 量比、涨跌幅、价格位置、收盘强度、MACD及变化、RSI、BOLL | 已由 `QS_V1_SHADOW` 记录 |
| 1分钟实时状态 | 波段状态、效率、方向占比、ATR强度 | 已由 `QS_V1_SHADOW` 记录 |
| 当前画像证据 | 当前并发位置下的样本数、胜率、EV和DPS版本 | 部分记录；只作为一个分项 |
| 已有首单上下文 | 首单方向、入场价、已持有时间、当前浮动方向/幅度、剩余到期时间 | 尚未完整记录 |
| 两单关系 | 同向叠加还是反向对冲、第二信号相对首单是否提供新增信息 | 尚未完整记录 |
| 近期结果上下文 | 最近已结订单的方向、胜负、结算时间及与当前方向的关系 | 尚未进入质量评分 |
| 风险状态 | 方向连亏、画像退化、滚动优势、当前风险暂停状态 | 已有独立守卫状态，需作为准入上下文读取 |

画像的作用必须保持有限：它可以提高或降低综合可信度，但不能越过其他维度直接决定开单。例如，同一画像在首单已经明显逆向、第二信号没有新增信息时不能仅凭历史胜率放行；低样本画像在当前量价、波段和首单状态一致时，也不能仅凭画像样本不足永久关闭。

### 31.4 当前代码完成度

生产提交 `02369bf` 已在 `2026-08-12 17:57:54 CST` 上线以下观测基础：

1. 候选、观察样本、订单和入口快照能够区分 `FIRST/SECOND`；
2. `QS_V1_SHADOW` 能按 `LONG_SECOND` 上下文记录当前信号、画像分项和1分钟量价/波段分项；
3. `/api/order-profile` 能按方向、并发位置、画像和DPS切片分析；
4. `/api/signal-audit-summary` 能查看候选经过现有守卫时的决策状态。

当前尚未完成的是真实LONG第二单综合准入：

1. `QS_V1_SHADOW` 仍为纯观察，不参与 `open_allowed`；
2. 评分尚未完整包含已有首单和两单关系；
3. 最近已结订单结果尚未作为LONG第二单上下文输入；
4. 尚未定义并验证最终的 `ALLOW/WAIT` 边界；
5. 发布边界后尚无新的真实订单，无法用原生新字段评价LONG第二单。

因此不能表述为“LONG第二单优化已经生效”。准确状态是：**综合评估框架的第一阶段观测能力已上线，真实开单前准入尚未启用，仍需补齐上下文并验证。**

### 31.5 后续实现约束

后续实现必须采用单独的LONG第二单准入组件，放在现有基础候选成立之后、实际写入订单之前。输入是当前LONG候选、已有未结订单、最近已结订单和现有风险状态；输出只允许：

- `ALLOW`：综合条件支持开第二笔LONG；
- `WAIT`：当前证据不足或两单风险过于集中，本次不开单并记录具体原因。

每次评估必须保存总分、各维度分项、首单上下文、最近结果上下文和最终原因，确保可以回放“为什么放行”和“为什么等待”。不得只保存最终分数，也不得使用单一画像或单一波段条件短路全部判断。

SHORT路径在该组件之前直接按现有逻辑继续执行，不能调用LONG第二单专用门槛，避免破坏当前 `SHORT_SECOND` 的正收益表现。

### 31.6 验证与升级条件

真实准入启用前必须完成按时间顺序的因果回放，重建每一时刻的未结订单、最近已结订单、并发位置和10U/18U资格。评价顺序固定为：

1. LONG第二单胜率；
2. LONG第二单PnL和EV；
3. 总策略胜率；
4. 订单减少比例；
5. 最大回撤和最长连亏。

必须同时展示 `ALLOW` 组和 `WAIT` 组的真实结果。如果 `WAIT` 组并未显著弱于 `ALLOW` 组，说明综合评估没有区分力，不能上线。任何阈值都只能从分层结果和样本稳定性中确定，不能先指定画像胜率63%、质量分或 `UP_LEG` 再倒推结论。

本节只修正文档定义，不修改代码、不发布服务器、不重启服务、不清空订单，也不改变当前线上开单逻辑。

## 32. 2026-08-13 LONG/SHORT方向独立控制实施方案

### 32.1 已确认目标

在不修改10分钟信号、每日画像、波段判断和既有风险守卫的前提下，将订单容量、开单冷却、并发位置和两单滚动资格按方向拆分。第一阶段采用保守容量：全局最多2笔，LONG最多1笔，SHORT最多2笔；已有反方向订单不占用当前方向自己的名额和冷却时间，但仍受全局2笔上限约束。

### 32.2 固定语义

1. `LONG_FIRST/LONG_SECOND` 只由未结LONG订单决定，SHORT不改变LONG并发位置；SHORT同理；
2. LONG开单只读取最近一笔LONG开单时间，SHORT开单只读取最近一笔SHORT开单时间；两个方向使用同一个冷却分钟配置；
3. LONG盈利只生成LONG两单滚动资格，SHORT盈利只生成SHORT资格；资格领取、占用、取消和恢复均按方向隔离；
4. 全局上限继续作为最终风险边界，因此第一阶段不会出现3笔同时在途；
5. 新订单写入方向槽位口径版本；旧订单不改写历史槽位，缺失口径时在加载和统计中显式标记为 `LEGACY_GLOBAL_V1`，后续只使用新口径订单评价本次修改；
6. 本次不启用LONG第二单综合准入，因为LONG方向上限为1；第31节保留为未来放开LONG第二单时的设计约束。

### 32.3 实施与验收顺序

1. 先用单元测试固定方向限额、反方向不触发冷却、同方向槽位和全局上限的组合边界；
2. 再用账本和重启恢复测试固定滚单资格不能跨方向消费或互相取消；
3. 增加 `MAX_OPEN_LONG_ORDERS`、`MAX_OPEN_SHORT_ORDERS` 启动参数和中文说明，默认值分别为1、2；
4. 状态接口同时展示全局、LONG和SHORT上限以及按方向冷却状态；
5. 运行全量测试、编译检查和差异审查。本次只修改仓库，不发布服务器、不重启服务、不清空订单。

### 32.4 本地实施结果

代码已按32.1至32.3完成本地实现：订单门禁使用全局2、LONG 1、SHORT 2的两层上限；开单冷却、FIRST/SECOND槽位和10U/18U资格均按方向隔离；SQLite自动迁移资格方向字段；状态接口提供方向上限、方向冷却和方向滚单状态，页面滚单徽标分别显示LONG和SHORT在途及待用数量。旧订单继续按历史全局并发语义恢复并标记 `LEGACY_GLOBAL_V1`，新订单标记 `DIRECTION_V2`；订单画像和画像周期统计均提供按口径拆分的方向槽位结果。

本次没有修改信号生成、每日画像、方向判断、评分阈值、1分钟波段、方向连亏守卫、画像退化守卫、滚动优势守卫或Webhook。行为层面的预期变化只有三项：LONG第二笔被方向上限拦截；SHORT仍可并行两笔；LONG与SHORT不再互相触发2分钟冷却或消费对方18U资格。全局2笔上限仍可拦截任意第三笔。

本次保持本地状态，未提交、未推送、未发布服务器、未重启服务、未清空模拟订单。发布后必须从首笔 `DIRECTION_V2` 订单重新统计，不得把旧口径FIRST/SECOND样本混入效果评价。

### 32.5 验收记录

`2026-08-13` 本地最终验收结果：

```text
python3 -m unittest discover -s tests -q
Ran 470 tests in 8.226s
OK

PYTHONPYCACHEPREFIX=/tmp/minute-strategy-pycache python3 -m compileall -q app scripts tests
bash -n scripts/run.sh
node --check app/static/app.js
git diff --check
exit=0
```

关键回归已覆盖：全局与方向上限组合、反方向不触发冷却、主开单路径方向隔离、同方向FIRST/SECOND、旧全局槽位恢复、旧SQLite表自动迁移、旧资格方向推断、跨方向资格拒绝、方向待用资格独立取消、原子存储拒绝跨方向绑定、重启恢复和启动脚本参数透传。测试输出仍有Python 3.14归档解压弃用提示和测试夹具SQLite `ResourceWarning`，不影响本次测试结果；未发现本次引入的失败。

## 33. 2026-08-13 方向独立控制发布评估

### 33.1 发布前线上边界

本次评估基于生产接口在发布前读取的369笔已结订单，订单ID为1至369。原始结果为218胜151负、胜率59.08%、PnL `+282.0U`、EV `+0.76U/笔`。按方向拆分后差异明显：

| 方向 | 已结 | 胜 | 负 | 胜率 | PnL | EV |
|---|---:|---:|---:|---:|---:|---:|
| LONG | 173 | 92 | 81 | 53.18% | `-72.4U` | `-0.42U` |
| SHORT | 196 | 126 | 70 | 64.29% | `+354.4U` | `+1.81U` |

旧的全局并发位置统计进一步表明，当前主要拖累来自第二并发LONG，而不是所有第二并发订单：

| 方向与旧并发位置 | 已结 | 胜 | 负 | 胜率 | PnL | EV |
|---|---:|---:|---:|---:|---:|---:|
| LONG_FIRST | 48 | 31 | 17 | 64.58% | `+111.6U` | `+2.33U` |
| LONG_SECOND | 125 | 61 | 64 | 48.80% | `-184.0U` | `-1.47U` |
| SHORT_FIRST | 44 | 24 | 20 | 54.55% | `-36.8U` | `-0.84U` |
| SHORT_SECOND | 152 | 102 | 50 | 67.11% | `+391.2U` | `+2.57U` |

最近1天56笔为29胜27负、胜率51.79%、PnL `-42.8U`；最近2天124笔为68胜56负、胜率54.84%、PnL `-6.4U`；最近3天228笔为130胜98负、胜率57.02%、PnL `+84.0U`。当前 `DPS-20260813-0800` 已结21笔，7胜14负、胜率33.33%、PnL `-72.8U`，说明短期状态明显差于全量平均，发布后不能只看总历史胜率判断新控制是否有效。

### 33.2 因果门禁回放

最近45笔带原生并发位置字段的订单为24胜21负、胜率53.33%、PnL `+1.2U`。按本次“全局2、LONG 1、SHORT 2、方向冷却”规则按时间顺序重建在途状态后，放行38笔、22胜16负、胜率57.89%、按原订单金额PnL `+35.2U`；被拦截的7笔均为旧口径 `LONG_SECOND`，2胜5负、PnL `-34.0U`。最大回撤由 `118.0U` 降至 `98.0U`，最长连亏由11笔降至9笔。

当前DPS内原始21笔为7胜14负、PnL `-72.8U`；新门禁会放行17笔、6胜11负、PnL `-50.8U`，拦截4笔旧口径 `LONG_SECOND`，其中1胜3负、PnL `-22.0U`。这说明方向上限对当前LONG第二单拖累有正向隔离作用，但不能把整体低胜率恢复到目标水平。

使用新方向账本重新按10U/18U计价属于反事实资金模型，只作为风险估算：最近45笔放行组PnL `+51.2U`、EV `+1.35U/笔`、最大回撤 `98.0U`；当前DPS放行组PnL `-57.2U`、EV `-3.36U/笔`、最大回撤 `98.0U`。实际发布效果必须从首笔 `DIRECTION_V2` 订单开始重新统计。

### 33.3 发布结论与风险边界

本次可以发布，理由是线上数据支持将LONG与SHORT容量、冷却、并发位置和滚单资格拆开，并直接限制历史上显著负收益的第二笔LONG。它不会修改当前信号、画像、阈值、方向预测、1分钟波段或现有守卫，因而影响范围集中在订单暴露和资金资格归属。

但本次不能被表述为解决了近期连亏。ID 359至369形成11笔连续亏损，其中SHORT 7笔、LONG 4笔；新规则只会拦截其中两笔 `LONG_SECOND`，仍会放行9笔。特别是当前SHORT连亏来自 `WD-02` 和 `WD-04`，与历史SHORT整体正收益同时存在，后续必须等待新口径真实样本分析，不能在本次发布中追加临时SHORT收紧规则。

发布计划固定为：从 `main` 创建 `v2026.08.13-directional-order-controls` 标签；生产保留现有SQLite和全部模拟订单；不清库、不改变 `NO_WEBHOOK=1`、评分阈值、画像门槛及既有systemd策略参数；发布后核验服务、预热、订单连续性、方向限额、方向冷却和方向资格状态。

### 33.4 正式发布记录

| 项目 | 发布值 |
|---|---|
| 生产提交 | `26241a57df17bab77e2fa2b33c596e4f591f32bb` |
| 固化标签 | `v2026.08.13-directional-order-controls` |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-26241a5-20260813-154555` |
| 发布前目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-02369bf-20260812-175520` |
| 最小包 | `event-contract-monitor-26241a5-20260813-154555.tar.gz` |
| SHA-256 | `1e7dd8309e470ab291c612d6e75411fd5b18eb8504fed0ea75a12cbd3389bcaf` |
| 服务重启边界 | `2026-08-13 15:47:03 CST` |
| systemd服务 | `victory-event-monitor`，`active/running`，`NRestarts=0` |
| 公网域名 | `https://victory.easy-tx.com`，HTTP 200，SSL校验0 |
| SQLite | `/opt/victory-event-monitor/shared/data/monitor.sqlite3` |

最小包从发布标签对应提交直接生成，只包含 `app/`、`scripts/run.sh`、`README.md` 和 `.gitignore`，不包含 `data/`、测试、文档、Git元数据、SQLite或密钥。服务器端SHA-256一致，解包后Python编译和关键模块导入通过，再原子切换 `/opt/victory-event-monitor/current` 并重启服务。未修改Nginx、SSL或systemd drop-in。

订单连续性和数据库迁移结果：

```text
orders rows: 369 -> 369
max order_id: 369 -> 369
open orders: 0 -> 0
stake_progression_credits.direction: present
```

因此本次没有清空、复制或替换共享SQLite，既有模拟订单完整保留。资格表原地新增 `direction` 列，旧资格按代码中的兼容规则恢复；发布时LONG和SHORT均无待用或在途18U资格。

### 33.5 线上验收结果

服务启动后的首次状态请求发生在预热初始化窗口，随后约15秒内恢复稳定。最终验收为：

```text
release=/opt/victory-event-monitor/releases/event-contract-monitor-26241a5-20260813-154555
service=active/running
NRestarts=0
warmup.status=READY
warmup.loaded_klines=149760
warmup.missing_files=[]
warmup.errors=[]
last_error=null
daily_profile_selection.status=READY
daily_profile_selection.version=DPS-20260813-0800
orders=369
open_orders=0
max_order_id=369
max_open_orders=2
max_open_long_orders=1
max_open_short_orders=2
min_order_gap_ms=120000
stake_progression.max_active_scope=DIRECTION
stake_progression.max_active_per_direction=1
webhook.enabled=false
journal warnings since release=0
https=200
ssl_verify_result=0
```

状态接口已分别返回LONG和SHORT的最近开单时间、下次允许时间、待用资格和在途二级资格。画像与订单统计新增 `by_direction_slot_scope`，发布前369笔全部明确归入 `LEGACY_GLOBAL_V1`；第一笔发布后新订单开始使用 `DIRECTION_V2`，后续效果评估必须按该字段隔离。

服务器本次保留实际启动参数，评分阈值为 `AUTO`，Webhook关闭。画像选择器实际配置为7天、工作日最少20笔、周末最少10笔、新增和退出胜率门槛均为60%、EV不低于0；这不是本次代码发布造成的变化。文档较早章节中的65%服务器门槛是历史快照，当前运行事实以本节60%为准。

### 33.6 发布后观察要求

1. 以服务重启边界后的首笔 `DIRECTION_V2` 订单作为新样本起点，不用旧 `LEGACY_GLOBAL_V1` 并发位置评价本次修改；
2. 分别统计 `LONG_FIRST`、`SHORT_FIRST`、`SHORT_SECOND` 的订单数、胜率、PnL、EV、最大回撤和最长连亏；第一阶段正常情况下不应出现 `LONG_SECOND|DIRECTION_V2`；
3. 检查反方向订单是否能够独立通过2分钟冷却，同时验证任意时刻全局仍不超过2笔；
4. 检查18U资格的来源方向与消费方向一致，不能跨LONG/SHORT转移；
5. 重点观察近期 `WD-02`、`WD-04` SHORT连亏是否延续，但在新口径样本不足前不追加方向守卫或画像临时黑名单；
6. 第30节异步Webhook代码已随本次版本部署，但生产 `NO_WEBHOOK=1`，因此实际发送仍关闭。

## 34. 下一开发计划：10分钟入场价格结构确认

### 34.1 计划目标和问题定义

下一开发项是在现有10分钟事件合约候选与真实开单之间增加一层“当前价格结构确认”。该层回答的不是未来大趋势向上还是向下，而是当前候选准备开单时，价格是否正处于有效支撑、压力、整数关口、突破、回踩或假突破结构中，以及该结构对候选方向是支持、冲突还是证据不足。

本计划的直接背景是2026-08-13订单ID 359至371形成13笔连续已结亏损，合计 `-138.0U`；分析期间又产生372号LONG在途订单。359至371由7笔SHORT和6笔LONG组成，跨 `WD-02/WD-04/WD-06/WD-08/WD-11`，不能归因于单一方向、单一时段或本次方向容量拆分。共同问题是：订单原始实时结论全部包含“信号不足”或“量能不足，等待”，`score=0`，但完整每日画像命中后仍被提升为正式订单；程序没有判断多次入场是否发生在同一价格结构、前次试探是否失败、候选方向前方是否存在未突破关口，也没有要求突破站稳或回踩确认。

该结构层用于补齐当前入场证据，不能被表述为单独解决全部连亏。20分钟方向连亏守卫到期后会自动恢复、每日画像可以提升实时WAIT候选等问题仍需独立处理；不能用支撑压力模块掩盖这些机制缺陷。

### 34.2 当前已有能力和明确缺口

当前正式策略已经计算：

1. `price_position`：当前收盘价在最近最多1440根1分钟历史K线最高价与最低价之间的位置；
2. `bollinger_position`：当前价格在20周期BOLL上下轨之间的位置；
3. 10分钟窗口收盘强度、单根1分钟K线上影拒绝和下影收回；
4. 10分钟涨跌幅、量比、MACD、RSI、BOLL、1分钟波段状态和波段质量；
5. 研究模块中的20分钟突破、VCP突破、趋势回调和120分钟假突破原型。

这些能力不等于真实价格结构判断：

- `price_position` 只提供近24小时大区间内的粗位置，不识别最近有效支撑或压力；
- BOLL是统计波动带，不是经过重复交易验证的水平价位；
- 上下影和收盘强度只描述当前K线或当前10分钟窗口，不维护“接近、突破、站稳、回踩、失效”的连续状态；
- 正式开单链路没有整数关口；
- 120分钟假突破候选的时段白名单当前为空，线上不会触发；历史两年全候选和多个walk-forward选择结果为负，禁止直接恢复；
- `research_strategy.py` 中突破、VCP和回调逻辑只用于研究回放，不能误认为线上已有功能。

### 34.3 不可偏离的范围边界

1. 只服务10分钟事件合约，行情输入只使用已经闭合的1分钟K线；不引入30分钟、4小时、日线或外部趋势投票；
2. 价格结构是当前入场确认层，不是独立方向生成器。它不能在没有现有候选时自行创建LONG或SHORT；
3. 不使用未来K线确认当前拐点。所有摆动点必须等右侧确认K线闭合后才能进入结构集合，回放和实盘必须使用同一因果口径；
4. LONG和SHORT使用完全镜像的结构定义与参数，不按历史亏损临时为某个方向增加特例；
5. 整数价格只是一种潜在结构来源，不能因价格接近整数就直接开单或反向；
6. 不恢复旧假突破固定时段白名单，不直接把研究模块策略接入生产；
7. 不把 `QS_V1_SHADOW` 直接改成硬门槛。发布观测后的47笔评分样本中，高分组没有表现出更高胜率，评分仍不具备单调区分能力；
8. 不以ID 359至372为目标反向调参。该区间只用于缺陷复现和解释，参数必须由完整因果回放及样本外结果确定；
9. 第一阶段只增加计算、持久化、页面展示和回放，不改变线上开单结果；只有满足第34.10节升级条件后，才进入真实准入阶段；
10. 不修改现有全局2笔、LONG 1笔、SHORT 2笔、方向冷却和方向10U/18U资格语义。

### 34.4 价格结构模型

新增独立组件 `entry_structure`，输入为当前交易对、已闭合1分钟K线和当前候选，输出一个不可变结构快照。组件不能读取订单结果、每日画像未来状态或尚未闭合K线，避免价格结构本身混入结果拟合。

结构来源分为三类：

1. **确认摆动点**：从1分钟高低点识别局部高点和局部低点。摆动点只有在左右窗口都已闭合时才确认，确认时间必须单独保存；
2. **重复触及价格区**：将相近确认摆动点按ATR归一化距离聚类为价格带，不使用单一精确价格线。区间权重至少包含触及次数、最近触及时间、拒绝幅度和是否发生有效穿越；
3. **整数关口**：BTC候选步长研究集合为100、500、1000，ETH候选步长研究集合为10、50、100。整数关口必须与ATR距离、摆动点重合度和价格反应共同评价，不能单独获得高置信度。

所有距离同时记录三种口径：绝对价格、基点 `bps` 和1分钟ATR倍数。实际判断以ATR倍数为主，避免BTC与ETH、低波动与高波动阶段使用同一个固定美元距离。聚类宽度、接近距离、突破缓冲和失效距离不得先写死为唯一最佳值；第一轮回放至少覆盖：

```text
价格区聚类宽度: 0.15 / 0.25 / 0.35 ATR
接近结构距离:   0.20 / 0.35 / 0.50 ATR
突破收盘缓冲:   0.05 / 0.10 / 0.20 ATR
确认闭合K线数:  1 / 2 / 3 根
回踩等待窗口:   3 / 5 / 10 根1分钟K线
```

参数网格只是待验证范围，不是生产默认值。最终值必须在训练区间确定，再原样用于后续样本外区间。

### 34.5 因果结构状态机

每次只根据截至当前已闭合1分钟K线的数据更新状态，至少支持：

| 状态 | 含义 | 不能误解为 |
|---|---|---|
| `NO_NEARBY_LEVEL` | ATR距离内没有足够可信的结构 | 自动允许开单 |
| `APPROACHING_SUPPORT` | 正接近下方支撑带，尚未出现反应 | 已确认LONG |
| `APPROACHING_RESISTANCE` | 正接近上方压力带，尚未出现反应 | 已确认SHORT |
| `SUPPORT_REJECTED` | 触及或短暂跌入支撑后收回，收盘和实体支持拒跌 | 永久有效支撑 |
| `RESISTANCE_REJECTED` | 触及或短暂越过压力后收回，收盘和实体支持受阻 | 永久有效压力 |
| `BREAKOUT_PENDING` | 收盘刚越过结构，但闭合数量、距离或量能尚未确认 | 真突破 |
| `BREAKOUT_CONFIRMED` | 连续闭合站在结构外，满足缓冲和确认条件 | 可以无条件追单 |
| `RETEST_PENDING` | 突破后回到原结构附近，尚未证明守住 | 回踩成功 |
| `RETEST_HELD` | 回踩原结构后重新收在突破方向一侧 | 后续一定延续 |
| `FALSE_BREAKOUT` | 越过结构后在确认窗口内重新收回结构内或反侧 | 所有情况下都应反向开单 |
| `LEVEL_INVALIDATED` | 结构被持续穿越，旧支撑/压力已失去当前约束力 | 删除历史数据 |

突破不能只看最高价或最低价刺穿，至少使用闭合价相对结构边界的ATR缓冲。量能、实体方向和收盘强度只作为确认分项；任何单一分项不得独立宣布真突破。回踩必须发生在此前已确认突破之后，不能把普通靠近结构误标为回踩。

### 34.6 候选方向准入矩阵

第一阶段只输出下列影子判断，不影响 `open_allowed`：

| 当前结构 | LONG候选 | SHORT候选 |
|---|---|---|
| 接近未突破压力 | `CONFLICT`，不应追多 | `NEUTRAL`，等待拒绝确认 |
| 压力拒绝 | `CONFLICT` | `CONFIRMED` |
| 向上突破待确认 | `PENDING` | `CONFLICT` |
| 向上突破确认/回踩守住 | `CONFIRMED` | `CONFLICT` |
| 接近未跌破支撑 | `NEUTRAL`，等待拒跌确认 | `CONFLICT`，不应追空 |
| 支撑拒跌 | `CONFIRMED` | `CONFLICT` |
| 向下突破待确认 | `CONFLICT` | `PENDING` |
| 向下突破确认/回踩守住 | `CONFLICT` | `CONFIRMED` |
| 假向上突破 | `CONFLICT` | `CONFIRMED`候选，但不能自行生成SHORT |
| 假向下突破 | `CONFIRMED`候选，但不能自行生成LONG | `CONFLICT` |
| 无附近结构或结构失效 | `NEUTRAL` | `NEUTRAL` |

真实准入阶段必须区分两类来源，不能一刀切：

1. **每日画像提升的实时WAIT或研究观察候选**：因为原始实时信号没有成立，未来要执行时必须获得与方向一致的 `CONFIRMED` 结构；`NEUTRAL/PENDING/CONFLICT` 均保持WAIT。这是本计划优先解决的路径；
2. **原生实时LONG/SHORT且已通过自身动态阈值的候选**：结构层不要求每笔都必须位于支撑或突破点，避免订单量被一次性压死。完成回放后，第一版最多只将明确 `CONFLICT` 作为否决，`NEUTRAL` 继续沿用原逻辑，`PENDING` 是否等待确认由样本外结果决定。

整数关口只有在产生 `REJECTED/BREAKOUT_CONFIRMED/RETEST_HELD/FALSE_BREAKOUT` 等真实价格反应时才参与上述矩阵；单纯接近整数位最多标记 `APPROACHING_*`。

### 34.7 数据记录与可解释性

结构快照必须在开单判断前生成，并沿同一个快照写入信号审计、观察样本、实际订单入口快照和订单API，不能在订单结算后重算。至少记录：

```text
entry_structure_version
entry_structure_evaluated_at
entry_structure_state
entry_structure_bias              # CONFIRMED / CONFLICT / PENDING / NEUTRAL
nearest_support_lower/upper
nearest_resistance_lower/upper
support_distance_price/bps/atr
resistance_distance_price/bps/atr
active_level_source               # SWING / ROUND / MERGED
active_level_touch_count
active_level_first_seen_at
active_level_last_seen_at
active_level_confirmed_at
breakout_direction
breakout_closed_bars
breakout_buffer_atr
retest_status
round_level_price
round_level_step
entry_structure_reason
candidate_origin                  # NATIVE_ACTIONABLE / PROFILE_PROMOTED_WAIT / RESEARCH_OBSERVATION
```

订单列表至少显示一个紧凑“价格结构”列，展示状态、最近结构价、ATR距离和判断；最新K线分析卡只增加一组结构标签，不增加第二行重复卡片。API保留完整字段，页面只展示高信号信息。所有字段都必须能解释“为什么确认”“为什么冲突”“为什么证据不足”，不能只保存一个最终分数。

### 34.8 接入顺序和组件边界

现行主流程中的建议接入顺序固定为：

```text
已闭合1分钟K线
 -> 现有10分钟主信号与研究观察候选
 -> 计算并附加同一时点的价格结构快照
 -> 每日画像完整键选择候选
 -> 根据候选来源生成影子结构判断
 -> 现有全局/方向容量与方向冷却
 -> 现有画像退化、方向连亏、滚动优势等风险守卫
 -> 金额资格与原子开单
```

结构识别、结构状态机和候选准入必须拆成三个独立单元：

- `StructureDetector`：只从K线生成支撑、压力和整数结构；
- `StructureStateMachine`：只维护突破、回踩、假突破和失效状态；
- `EntryStructureGate`：只将结构快照与候选方向、候选来源映射为 `CONFIRMED/CONFLICT/PENDING/NEUTRAL`。

这三个单元不得读取每日画像历史胜率、订单盈亏或滚单资格。`MonitorState` 只负责编排和持久化，避免继续把所有判断堆入单一开单函数。

### 34.9 回放和对照设计

必须使用两年1分钟K线按时间顺序逐根回放，结构点确认、画像选择、未结订单、方向冷却和10U/18U资格都只能使用当时已知信息。生产数据库中的订单和观察样本用于验证字段与真实路径，不替代K线级因果回放。

至少输出以下互斥对照：

1. 当前逻辑基准；
2. 只记录结构、不拦截，验证实现是否完全不改变基准订单；
3. 仅对 `PROFILE_PROMOTED_WAIT` 要求 `CONFIRMED`；
4. 仅拦截所有候选中的 `CONFLICT`；
5. 第3项与第4项组合；
6. 按结构来源拆分：摆动点、整数关口、二者重合；
7. 按状态拆分：拒绝、突破确认、回踩守住、假突破、无结构；
8. 按LONG/SHORT、FIRST/SECOND、WD/WE、DPS版本和 `DIRECTION_V2/LEGACY_GLOBAL_V1` 拆分；
9. 单独列出ID 359至372在每个方案下的因果判断，但不能把该区间用于选择参数。

统计顺序继续坚持胜率优先：

```text
1. 总胜率及LONG/SHORT分别胜率
2. PROFILE_PROMOTED_WAIT允许组与拦截组胜率差
3. PnL和EV
4. 保留订单数、日均订单数和订单减少比例
5. 最大回撤和最长连亏
6. 各滚动样本外窗口的一致性
```

参数选择采用walk-forward：训练窗口只选择结构参数，紧随其后的验证窗口只评价，不重新拟合。报告必须同时展示训练和验证结果，禁止用两年全样本最优参数直接宣称可上线。

### 34.10 从观察升级为真实准入的条件

只有同时满足以下条件，才可以提交真实拦截设计供确认：

1. 影子模式与当前基准订单ID、方向、入场时间和金额完全一致，证明第一阶段没有暗改开单；
2. 两年样本外组合中，允许组总胜率不低于60%，LONG和SHORT分别不低于事件合约盈亏平衡胜率55.56%，且两方向EV均大于0；
3. `PROFILE_PROMOTED_WAIT` 的 `CONFIRMED` 组胜率至少比其 `NEUTRAL/PENDING/CONFLICT` 组高3个百分点，并且差异不是由单一WD/WE时段贡献；
4. 组合方案总订单至少保留当前基准80%，LONG和SHORT分别至少保留各自基准70%，避免以接近停单换取表面胜率；若达不到，只能继续观察，不能降低胜率要求倒推上线；
5. 最大回撤和最长连亏均不得劣于当前基准；
6. 至少三分之二的样本外滚动窗口EV为正，不能只依赖某一段行情；
7. BTC和ETH分别报告，不允许一个币种的正结果掩盖另一个币种的负结果；
8. 所有最终阈值均有版本号、启动配置、API状态和回放报告，能够从任意订单复现当时结构判断；
9. 用户确认真实准入方案后才修改 `open_allowed`，未确认前保持 `SHADOW_ONLY`。

### 34.11 实施阶段

下一次开发按以下顺序推进，不合并跳步：

1. 用测试固定因果摆动点、ATR价格区聚类、整数位、突破、回踩、假突破和失效定义；
2. 实现三个独立组件及结构快照，不接入真实门禁；
3. 扩展Signal、观察样本、订单入口快照、SQLite兼容迁移、API和页面结构字段；
4. 验证影子模式订单与当前基准完全一致；
5. 完成两年因果回放、walk-forward参数选择和详细分组报告；
6. 依据第34.10节决定继续观察、否决该方案或提出真实准入设计；
7. 真实准入必须作为单独提交、单独标签和单独生产样本边界发布，不能与画像门槛、连亏守卫、评分系统或订单容量同时修改。

### 34.12 2026-08-17整合决定

本节保留为价格结构设计的历史来源。其完整有效定义已整合到 `docs/superpowers/specs/2026-08-16-adaptive-resident-profiles-design.md`，后续开发、测试和验收只以该统一设计文档为准；如本节旧表述与统一设计冲突，以统一设计为准。

当前决定：

- 开发分支为`feature/adaptive-resident-profiles`；
- 自适应画像与价格结构在同一开发文档、同一分支实施；
- 价格结构第一阶段固定为`SHADOW_ONLY`，只记录和统计，不影响任何开单结果；
- 画像采用7天快速入选、14天稳定保留和双窗口连续失效退出，不再以14天单窗替代7天；
- N12/N20先降级第二席位和18U，只有N12与完整N20同时恶化才暂停全部正式单；
- 价格结构不加入画像键，不参与7天/14天资格、N12/N20状态、评分或动态阈值；
- 胜率和订单量双优先，正式方案总订单至少保留基准80%，后续价格结构真实门禁也沿用该底线；
- 所有实际影响开单的动态值、阈值、模式、版本和守卫结论统一写入`DECISION_CONTEXT_V2`；完整启动配置按`runtime_config_hash`去重保存，后续必须能从任意候选复现当时决策；
- 当前仅完成设计整合，尚未修改业务代码、运行回放、发布服务器或创建标签。

## 35. WebSocket低延迟行情分支（2026-08-14完成，2026-08-15发布）

### 35.1 目标和边界

本次工作位于 `feature/websocket-low-latency`，基于 `main@bf697ae`。目标是缩短行情到达、闭合K线触发和页面当前点位刷新延迟；当前未合并 `main`、未推送生产、未重启线上服务、未清空或修改模拟订单。

以下开单行为保持不变：

1. 仍只使用已闭合1分钟K线生成10分钟事件信号；
2. 量价、MACD、RSI、BOLL、Fear & Greed、每日画像、动态评分、方向容量、方向间隔、滚动优势、结算序列守卫、画像退化守卫和两阶段金额规则均未修改；
3. `MonitorState.update_from_klines()` 仍是唯一策略入口；
4. 原子订单/资格SQLite提交仍发生在Webhook触发之前，未为追求延迟改变事务语义；
5. 未闭合K线和 `miniTicker` 只刷新实时点位，绝不进入策略K线窗口、信号计算或开单路径。

### 35.2 行情链路

默认订阅币安现货组合流：

```text
{symbol}@kline_1m
{symbol}@miniTicker
```

闭合K线链路：

```text
WebSocket kline(x=true)
 -> symbol/generation校验
 -> open_time去重和缺口检查
 -> 单消费者队列
 -> 原有MonitorState.update_from_klines()
 -> 原有结算、信号、守卫、订单和Webhook链路
```

实时点位链路：

```text
WebSocket miniTicker或未闭合kline
 -> 仅更新独立realtime price状态
 -> GET /api/price
 -> 页面每1秒轻量刷新“当前点位”
```

REST不再作为正常信号的10秒主轮询，保留以下职责：

- 服务启动时补齐最近闭合K线；
- WebSocket断线或消息停滞时补偿；
- 检测到闭合K线缺口时恢复；
- 使用 `--no-websocket` 或 `NO_WEBSOCKET=1` 时作为纯REST回退。

WebSocket与REST不能并发调用策略状态。所有闭合K线统一进入一个消费线程；相同 `open_time` 只处理一次。币种切换会增加generation、清空实时价、关闭旧连接并重订阅，旧币种迟到消息和旧页面响应均被拒绝；并发切币请求串行执行，不能互相提前解除预热暂停。

消费游标仅在 `update_from_klines()` 明确成功后推进。若SQLite或策略状态处理失败，协调器保留原批次、立即唤醒REST补偿并持续重试；不会因为WebSocket闭合事件只发送一次而永久漏掉该分钟。停机后尚未开始处理的队列任务直接丢弃，不再产生新订单。

### 35.3 页面与接口

新增轻量接口：

```text
GET /api/price
```

仅返回 `symbol`、`latest_price`、`event_time_ms`、`received_at_ms`、`stale` 和 `stream_status`，响应设置 `Cache-Control: no-store`。完整 `/api/state` 保持3秒刷新，不再覆盖“当前点位”；价格接口每1秒刷新，并阻止重叠请求和切币后的旧响应覆盖。

### 35.4 依赖和启动

新增锁定依赖：

```text
websocket-client==1.9.0
```

部署包继续不包含虚拟环境，发布前必须执行：

```bash
python3 -m pip install -r requirements.txt
```

启动脚本新增 `--no-websocket` 和 `NO_WEBSOCKET`，其他策略启动参数及默认值未变。`--poll-seconds` 现在表示REST补偿/纯REST回退间隔。

### 35.5 验证结果

2026-08-14在隔离工作树完成：

- 完整测试：`485 tests`，全部通过；
- JavaScript语法：`node --check app/static/app.js` 通过；
- Binance WebSocket真实握手：HTTP 101成功，收到 `BTCUSDT 24hrMiniTicker`；
- 本地端到端：`/api/price` 状态为 `CONNECTED`，约1.8秒内价格从 `63493.99` 更新到 `63504.0`；
- 币种切换：BTC切换ETH后返回 `ETHUSDT 1886.86`，旧BTC流未覆盖新状态；
- `/api/state` 同时正常返回最新闭合K线和原有策略状态。

本次没有进行回测，因为策略、参数、守卫和开单资格均未改变；验证重点是消息隔离、闭合K线去重、单消费者串行化、接口契约和真实网络链路。

### 35.6 发布核验要求

后续若确认发布，必须在不清订单的前提下：

1. 安装 `requirements.txt`；
2. 保持现有systemd全部策略环境变量不变，仅默认启用WebSocket；
3. 重启后检查 `/api/price` 的 `stream_status=CONNECTED` 且 `received_at_ms` 持续更新；
4. 检查 `/api/state` 的预热、闭合K线数量、画像和守卫状态；
5. 切换BTC/ETH各验证一次订阅和页面点位；
6. 若生产网络无法建立WebSocket，临时设置 `NO_WEBSOCKET=1` 回退REST，不调整任何策略阈值。

## 36. 今日盈亏与胜率08:00日界线修正（2026-08-14完成，2026-08-15发布）

### 36.1 问题与根因

页面“今日盈亏”和“今日胜率”读取 `/api/state.stats.today`，但原实现仍按北京时间自然日 `[00:00, 次日00:00)` 聚合。此前要求与每日画像生效时间对齐到08:00时，代码实际新增了 `stats.profile_period`，没有替换 `stats.today` 的日界线。本节修正覆盖交接文档第24、26、27节中“今日统计保持00:00自然日”的旧口径。

修正前线上快照为：北京时间自然日35笔已结、13胜22负、胜率37.14%、PnL `-136.8U`；按08:00重新聚合同一数据库为7笔已结、3胜4负、胜率42.86%、PnL `-9.6U`。差异来自00:00至08:00订单被旧口径计入，不是页面格式化错误。

### 36.2 修正口径

1. `stats.today` 改为北京时间 `[当日08:00, 次日08:00)`；
2. 当前时间早于08:00时，统计区间回退到前一日08:00至当日08:00；
3. 仍按 `settled_at` 归属，只统计已结订单，不按开单时间归属；
4. 不限制 `daily_profile_version`，该点与 `stats.profile_period` 保持区别；
5. 总盈亏、总胜率、画像周期统计、开单、结算、画像、守卫、金额和Webhook逻辑均不改变。

测试锁定07:59:59排除、08:00包含、次日08:00排除，以及08:00前使用上一交易日的边界行为。该修正已随第39节生产版本发布；2026-08-15 00:52的线上快照正确显示统计日期为2026-08-14，证明08:00前已使用上一交易日边界。

## 37. 待优化：08:00交易日内按方向动态调节开单口径（暂缓实施）

本项只记录为后续待办。必须等第35节WebSocket低延迟行情和第36节08:00今日统计修正发布、运行稳定并积累新的生产样本后再设计和实现；当前不修改代码、不回放、不发布服务器，也不改变现有LONG/SHORT开单逻辑。

目标不是永久设置“LONG严格、SHORT宽松”，也不是只把两个方向的并发上限拆开。目标是在同一个北京时间08:00至次日08:00交易日内，分别使用当时已经结算的LONG和SHORT结果判断当日方向质量：某方向当日表现持续较强时放宽该方向口径，表现持续较弱时收紧该方向口径；另一方向独立统计，不能因一个方向较弱就自动把另一个方向判为强势。

后续设计必须满足以下边界：

1. 统计严格按08:00交易日划分，只使用当前候选产生前已经结算的订单，禁止使用未来结果；
2. LONG和SHORT分别维护样本数、胜率、EV、连续结果及状态，交易日切换后重新进入样本积累状态；
3. 样本不足时保持中性口径，不能用1至2笔偶然结果立即放宽或收紧；
4. 状态至少区分“样本积累、正常、放宽、收紧”，并设置进入/退出滞回，避免一次输赢造成频繁切换；
5. “口径”具体控制画像最低胜率、方向并发或其他哪一个准入维度，必须先用因果回放单变量比较后确定，不能一次同时改多项参数；
6. 页面、API、订单入口快照和信号审计必须记录当时的方向状态、样本、胜率、EV、实际使用口径和变更原因；
7. 本项只负责单日LONG/SHORT强弱的动态调节，不替代每日画像、完整画像退化守卫、方向连续亏损守卫或后续价格结构判断；
8. 发布时必须作为独立提交、独立标签和独立生产样本边界，不能与第34节价格结构计划或其他策略改动混发。

## 38. 已发布：12:00-18:00真实开单暂停与影子观察

2026-08-14重新读取线上452笔订单，其中450笔已结算。按订单开仓时间转换为北京时间统计：12:00-18:00共58笔，22胜36负，胜率37.93%；其中10U基础单47笔、胜率40.43%，18U订单11笔、胜率27.27%。剔除整个12:00-18:00时段后，历史总体胜率由57.56%提升为60.46%。该结果只用于确定当前暂停边界，不宣称固定时段具有永久因果性。

本次已确认的实现边界：

1. 北京时间12:00:00（含）至18:00:00（不含）暂停创建真实模拟订单；11:59:59仍可开单，18:00:00恢复原逻辑；
2. 时段判断放在现有画像、信号资格、并发、间隔、画像退化、连续结果和滚动优势守卫全部通过之后，确保影子样本只代表原逻辑本来会实际开出的订单；
3. 命中后返回独立决策码 `TIME_PERIOD_SHADOW_ONLY`，不创建真实订单、不占用并发、不消费18U资格、不发送Webhook；
4. 命中信号仍按10分钟观察单保存并使用到期K线结算，作为后续恢复评估的数据源；
5. 当前不自动依据影子胜率恢复，不修改画像、评分、方向、金额、叠加、冷却和其他守卫；恢复规则必须等新影子样本积累后另行确认；
6. 仅增加一个启动开关，默认启用；`--no-time-period-guard` 或 `TIME_PERIOD_GUARD=0` 可完整关闭该时段守卫；
7. 页面沿用现有开单决策和风险暂停原因展示，API额外返回当前时段守卫状态，便于确认是否正在暂停。

实施顺序采用测试先行：先锁定北京时间边界、真实订单/Webhook隔离和影子结算，再接入开单入口、启动脚本、API状态和策略文档，最后运行定向测试与完整测试。本节与第37节方向动态口径相互独立，第37节仍保持暂缓。

实现提交为 `721388a`，已推送 `main` 并按第39节发布。验证结果：时段守卫定向测试7项通过；`python3 -m unittest discover -s tests` 共492项通过；`bash -n scripts/run.sh`、Python编译检查和 `git diff --check` 均通过。完整测试只出现既有SQLite连接 `ResourceWarning` 和Python 3.14 tar解压弃用提示，没有测试失败。

## 39. 2026-08-15生产发布：低延迟行情、08:00统计和时段影子守卫

### 39.1 发布身份

| 项目 | 实际值 |
|---|---|
| 生产功能提交 | `721388ac2ece2a9e3d87015646807d5aaf25a637` |
| 发布标签 | `v2026.08.15-time-period-shadow-guard` |
| 生产分支 | `main` |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-721388a-20260815-004710` |
| 当前软链 | `/opt/victory-event-monitor/current` |
| systemd服务 | `victory-event-monitor` |
| 服务重启时间 | `2026-08-15 00:51:58 CST` |
| 发布包SHA-256 | `929efdb817a7f0372b2a9d4b7a55c2fb62995c597936e4a8f562e7d1aba3927b` |

本次生产发布累计带入三个已经分别验证的功能边界：`efa8353` 的Binance WebSocket实时价格、闭合1分钟K线串行消费和REST补偿；`fe3ba28` 的北京时间08:00“今日”统计日界线；`721388a` 的北京时间12:00-18:00真实开单暂停与影子观察。第37节按方向动态调节和第34节价格结构确认均未实现、未混入本次发布。

### 39.2 部署边界

1. 发布前生产目录为 `/opt/victory-event-monitor/releases/event-contract-monitor-26241a5-20260813-154555`；新目录解压和导入检查通过后，使用新软链原子替换 `current`；
2. SQLite继续使用 `/opt/victory-event-monitor/shared/data/monitor.sqlite3`，没有清空、迁移或覆盖模拟订单；
3. 所有既有systemd策略参数保持不变：画像最低胜率60%、总并发2、LONG并发1、SHORT并发2、最小间隔2分钟、同方向3亏冷却20分钟、两单金额叠加和Webhook关闭状态均保留；
4. `websocket-client==1.9.0` 安装在 `/opt/victory-event-monitor/shared/python`，通过 `05-pythonpath.conf` 注入 `PYTHONPATH`，没有使用 `--break-system-packages` 修改系统Python；
5. Nginx、SSL、域名和监听端口未修改；服务仍由 `/usr/bin/python3` 在 `127.0.0.1:18080` 启动；
6. 新时段守卫使用服务默认值 `TIME_PERIOD_GUARD=1`，未新增覆盖参数。紧急关闭可设置 `TIME_PERIOD_GUARD=0` 或使用 `--no-time-period-guard`。

### 39.3 发布前后数据连续性

发布前 `/api/state` 为465笔总订单、463笔已结、2笔在途，预热状态 `READY`、149,760根K线。发布后总订单仍为465，没有新增ID或丢失历史记录；原在途订单464和465按各自10分钟到期K线正常结算为一负一胜，首个稳定快照为465笔已结、0笔在途。发布过程没有清空订单。

发布后预热状态为 `READY`，加载151,200根K线，15个缓存文件、本次下载1个文件、缺失0、错误0。页面和API均返回HTTP 200，服务保持 `active/running`，启动日志没有异常退出。

### 39.4 功能验收

1. `/api/price` 返回 `stream_status=CONNECTED`、`stale=false`；连续两次请求中价格从`63134.58`更新到`63125.34`，事件时间从`1786726437016`推进到`1786726446028`，确认页面1秒点位接口使用实时WebSocket数据；
2. 00:52线上快照的 `stats.today.date=2026-08-14`，42笔已结、20胜22负，符合北京时间08:00前归入上一交易日的口径；
3. `/api/state.time_period_guard.enabled=true`，窗口为`12:00-18:00`；验收时本地小时为0，状态为`TIME_PERIOD_ALLOWED`，说明默认配置已经进入生产；
4. 时段守卫只在原信号通过其他门禁后阻止真实订单，并记录 `source_decision=TIME_PERIOD_SHADOW_ONLY` 的10分钟影子观察；不发送Webhook、不占并发、不消耗18U资格；
5. 发布前本地完整验证为492项测试通过，`git diff --check`、`bash -n scripts/run.sh` 和Python编译检查通过。

### 39.5 后续观察

从本版本开始，以发布后新增的12:00-18:00影子样本作为独立观察边界。恢复真实开单前至少分方向、画像完整键、10U/18U资格和日期统计样本量、胜率及EV；当前不设置基于影子结果的自动恢复，也不把影子结论用于第37节方向动态调节。其他时段的开单逻辑、画像和守卫继续按现有参数运行。

## 40. 2026-08-15画像短窗健康守卫与固定时段撤销

### 40.1 修改原因

本节覆盖第38、39节中“12:00-18:00默认拦截”的当前状态，但保留其历史发布记录。线上新影子样本显示固定时段守卫拦截的6个SHORT候选为5胜1负，固定北京时间时段不具备稳定的跨日因果性，因此不再作为默认真实门禁。当前需要解决的是每日7日画像对短期状态切换反应慢，而不是继续按固定小时删单。

### 40.2 当前设计

1. `TimePeriodGuard` 模块保留，服务和启动脚本默认改为关闭；设置 `TIME_PERIOD_GUARD=1` 才恢复旧的12:00-18:00影子拦截。
2. 每日画像仍按最近7天独立观察样本在07:50评估、08:00生效，60%最低胜率、工作日20样本、周末10样本和EV条件均不修改。
3. 新增第二层 `ProfileHealthGuard`，在北京时间每4小时边界按LONG/SHORT分别统计当前已启用画像过去24小时的已结算独立观察样本。
4. 少于12笔为 `WARMUP`，维持原行为；胜率至少55.56%且EV非负为 `HEALTHY`，维持原行为。
5. 胜率50%-55.56%或EV为负为 `WATCH`，只允许该方向10U首单，禁止同方向第二席位和18U资格。
6. 胜率低于50%为 `DEGRADED`，暂停该方向真实订单至下一4小时边界。
7. 守卫只会收紧当前已入选画像，不会启用未入选画像、改变方向、改变评分或修改10分钟候选生成。
8. `PROFILE_HEALTH_GUARD=0` 或 `--no-profile-health-guard` 是唯一关闭开关；窗口、边界和阈值均固定在代码中。

### 40.3 审计与兼容

`/api/state` 新增 `profile_health_guard`。信号、模拟订单和观察记录新增 `profile_health_status`、`profile_health_sample_size`、`profile_health_win_rate`、`profile_health_ev`、`profile_health_evaluated_at`。SQLite继续使用JSON载荷，新字段有默认值，不需要迁移或清空历史订单。

新增决策码：

- `PROFILE_HEALTH_BLOCKED`：当前方向短窗状态为DEGRADED；
- `PROFILE_HEALTH_SECOND_ORDER_BLOCKED`：当前方向短窗状态为WATCH且候选是第二席位。

两类阻止都继续保存观察记录，不创建模拟订单、不发送Webhook、不占用并发、不消费18U资格。

### 40.4 实施与发布状态

状态：`已合并、已推送、已打标签并发布生产`。

| 项目 | 实际结果 |
|---|---|
| 合并提交 | `20330fe72c4b7ab1282329583e918d0033133469` |
| 发布标签 | `v2026.08.15-profile-health-guard`，固定指向 `20330fe` |
| 发布目录 | `/opt/victory-event-monitor/releases/event-contract-monitor-20330fe-20260815-223010` |
| 发布包SHA-256 | `133f2e414f8bcc133cbff27a7b45529d0028953b17c21a779e01ed5d22675798` |
| 服务重启时间 | `2026-08-15 22:36:30 CST` |
| 服务状态 | `active`，重启后主进程PID `1199411`，`NRestarts=0` |

发布没有清空SQLite和历史订单。发布前快照为491笔总订单、490笔已结、1笔未结；旧服务在重启前自然结算ID 491为WIN并创建ID 492，因此重启后为492笔总订单、491笔已结、1笔未结。ID 492在旧进程中创建，其画像健康字段为空属于兼容预期；新进程保留该在途订单，没有重复结算或丢单。

生产验收结果：

1. 预热状态为 `READY`，共152640根，缓存16批、下载1批、缺失0、错误0；
2. `/api/state.time_period_guard.enabled=false`、状态码为 `DISABLED`，确认固定12:00-18:00拦截已默认撤销；
3. `/api/state.profile_health_guard.enabled=true`，首次边界评估LONG为 `DEGRADED`：过去24小时18笔、7胜11负、胜率38.89%、PnL `-54.0U`、EV `-3.0U`，当前阻止该方向至下一4小时边界；
4. 每日画像版本仍为 `DPS-20260815-0800`，已选21个画像，证明新守卫没有替换或重算每日画像；
5. `/api/price` 返回 `stream_status=CONNECTED`、`stale=false`，两次请求的事件时间从 `1786804628017` 推进到 `1786804679014`，确认实时WebSocket行情持续更新；
6. 发布前完整测试为508项通过；Python编译、`bash -n scripts/run.sh`、`node --check app/static/app.js` 和 `git diff --check` 均通过。

标签严格对应服务器运行代码。上述生产证据作为标签之后的文档提交继续写入 `main`，不移动或重建发布标签。

## 41. 2026-08-16方向脉冲影子与结算即更新

### 41.1 发布目标

现有每日画像和24小时画像健康守卫对短时方向切换的反应粒度不同。新增 `DIRECTION_PULSE_V1_SHADOW`，分别计算LONG/SHORT最近12和16个方向级独立观察结果。N12/N16少样本为`WARMUP`，胜率不低于50%为`NORMAL`，胜率不低于40%且低于50%为`WATCH`，低于40%为`DEGRADED`。

该功能只做前向观测：`WATCH`推演关闭同方向第二席位，`DEGRADED`推演暂停该方向，但两者均不进入真实开单门禁。现有每日画像60%门槛、LONG并发1、SHORT并发2、总并发2、2分钟同方向间隔、10U/18U金额、所有既有守卫及Webhook状态均保持不变。

### 41.2 即时刷新与审计

方向脉冲不使用4小时边界。每批闭合K线完成观察结算后，只要有新观察变为`SETTLED`，运行态立即按当前已经结算的方向级独立样本重算LONG/SHORT的N12/N16；同方向重叠的10分钟观察只计一次。重启和币种切换从SQLite观察历史恢复，不需要等待新4小时边界。

`/api/state.direction_pulse_shadow`和页面状态卡展示两个方向的窗口状态。每个信号、模拟订单和观察记录固化开单前的窗口指标、FIRST/SECOND席位、假设动作与`would_block`，结算后可直接关联真实胜负。SQLite继续使用JSON载荷，不新增表、不迁移数据库、不清空订单。

### 41.3 安全边界

代码中没有任何订单门禁读取`direction_pulse_shadow`或`would_block`。专项测试覆盖`DEGRADED + would_block=true`仍按原路径成功开单，证明该字段只沿信号审计、订单和观察持久化链传播。24小时`ProfileHealthGuard`继续保持原4小时正式评估边界，本次没有暗改其参数或行为。

发布前代码审查额外修复四个隔离边界：影子计算异常全部在影子层捕获，不能终止行情更新或真实开单；重启单独恢复最近5000条观察供N16去重，避免稀疏方向退回`WARMUP`；延迟或回放候选按自身K线时点剔除未来结算；同批结算观察先用一个SQLite事务持久化，再发布新脉冲快照。上述改动只增强影子审计一致性，不把影子状态接入真实开单判断。

### 41.4 发布记录

1. 功能提交为`2664412`，安全隔离修复为`0973c0c`，`main`非快进发布提交为`6474dd8`；GitHub `main`已经推送。
2. 发布标签为`v2026.08.16-direction-pulse-shadow`，标签解引用后严格指向`6474dd81b005ca8c1d011a739bca71335f26b1e6`。
3. 精简发布包只包含`.gitignore`、`README.md`、`requirements.txt`、`app/`和`scripts/`，大小180805字节，SHA-256为`b0a1511203d5b402258d730ef5453935c80843527a291f68a4f01cc426f5a670`。
4. 生产目录为`/opt/victory-event-monitor/releases/event-contract-monitor-6474dd8-20260816-143053`，`current`已原子切换；服务于2026-08-16 14:32:53 CST重启，`active`、`NRestarts=0`，启动参数未变化。
5. 发布未清空或迁移SQLite。发布前后均为529笔订单、最新ID 529、0笔在途订单、总PnL `-22.4U`、胜率55.39%；并发仍为总2、LONG 1、SHORT 2，同方向间隔仍为120000毫秒。
6. 预热为`READY`、154080根，`last_error=null`。页面和`/api/state.direction_pulse_shadow`均已上线；恢复结果为LONG N12/N16均50.00%，SHORT N12 58.33%，SHORT N16 43.75%/`WATCH`，但真实订单口径未被收紧。
7. 合并后的完整测试为523项通过；Python编译、`node --check app/static/app.js`、`bash -n scripts/run.sh`及`git diff --check`通过。生产发布目录也通过Python编译和模块导入检查。
8. 生产事件证据已确认结算即更新：服务启动快照`evaluated_at=1786861974285`；下一笔LONG独立观察于2026-08-16 14:34:59 CST结算后，`evaluated_at`和LONG `last_settled_at`均立即推进到`1786862099999`，未等待下一4小时边界，`last_error`仍为空。该证据作为标签后的文档提交，不移动发布标签。

## 42. 2026-08-19自适应常驻画像严格因果回放（本地未部署）

### 42.1 分支、版本与实现状态

| 项目 | 当前值 |
|---|---|
| 开发分支 | `feature/adaptive-resident-profiles` |
| Task 17起点 | `bcc33c246eb684d700812eb7cf9e8b816e93e2b7` |
| 决策上下文 | `DECISION_CONTEXT_V2` |
| 信号审计 | `SIGNAL_AUDIT_V2` |
| 每日资格 | `DAILY_PROFILE_QUALIFICATION_V2` |
| 即时画像 | `ADAPTIVE_PROFILE_STATE_V1`，N12/N20逐笔结算更新 |
| 价格结构 | `ENTRY_STRUCTURE_SHADOW_V1`，保持`SHADOW_ONLY` |
| 金额叠加 | `TWO_STAGE_V1` |
| 当前状态 | 本地实现完成，**未部署**、未合并`main`、未推送、未打标签、未清空订单 |

`scripts/replay_daily_profile_selector.py`现在拒绝任何缺少生产执行参数的运行，不再沿用旧`max_open_orders=5`。全局并发、LONG/SHORT方向并发、同方向冷却/最小开单间隔、基础`stake`、`win_return`、金额叠加开关、级数、并行第二级上限、第二级金额及仅基础金额时段均必须显式提供。

回放按`settled_at -> opened_at -> observation_key`生成结算事件时间线。每个候选只读取其开单时点已经结算的事件；每日07:50快照严格排除`settled_at >= 07:50`的数据。baseline、structure-shadow和adaptive candidate使用相同生产配置独立执行；结构 equality 报告真实逐笔比较订单ID、方向、开结算/到期时间、stake、progression字段和Webhook计数，不使用固定相等结论。

### 42.2 迁移与容量行为

本分支的SQLite变更保持增量兼容：V2表、索引和列通过事务迁移增加，迁移失败整体回滚；旧订单、旧观察和旧审计继续可读，不重写、不删除、不执行固定TTL清理。Task 17回放本身只读取指定SQLite并写JSON报告，不修改数据库，不生成生产订单，不发送真实Webhook。正式复核必须先复制源SQLite，再对副本运行回放。

单文件容量门槛保持：

| 状态 | 文件大小 | 行为 |
|---|---:|---|
| `NORMAL` | `< 2.5GiB` | 正常写入V2核心数据和紧凑审计 |
| `WARNING` | `>= 2.5GiB` | API和页面持续告警 |
| `COMPACT_ONLY` | `>= 2.75GiB` | 停止普通WAIT可选审计，保留订单、观察、状态变化和决定性阻止 |
| `HARD_LIMIT` | `>= 3GiB`（`3221225472`字节） | 禁止继续分配SQLite页面；核心持久化失败时暂停新开单 |

3GiB上限内至少为核心表保留256MiB。容量降级不得删除历史订单或观察，也不得创建无法持久化的订单。

### 42.3 可复现回放命令

以下命令以复制到`/private/tmp`的SQLite为输入，参数对应当前生产总并发2、LONG 1、SHORT 2、同方向间隔2分钟和10U/18U两阶段金额：

```bash
python3 scripts/replay_daily_profile_selector.py \
  --db-path /private/tmp/monitor-replay.sqlite3 \
  --symbol BTCUSDT \
  --lookback-days 7 \
  --stable-lookback-days 14 \
  --min-samples 20 \
  --min-win-rate 0.60 \
  --min-ev 0 \
  --degraded-runs-to-exit 2 \
  --joint-failures-to-exit 2 \
  --max-open-orders 2 \
  --max-open-long-orders 1 \
  --max-open-short-orders 2 \
  --min-order-gap-minutes 2 \
  --stake 10 \
  --win-return 18 \
  --stake-progression \
  --stake-progression-max-orders 2 \
  --stake-progression-max-active 1 \
  --stake-progression-second-stake 18 \
  --stake-progression-base-only-segments "" \
  --output /private/tmp/adaptive-profile-release-gates.json
```

不得直接对生产SQLite做试验性写入。命令缺少任一显式并发、冷却、金额或progression参数时必须由CLI返回错误，不得猜测生产值。

### 42.4 发布验收门槛

| 门槛 | 最低要求 |
|---|---:|
| 样本外总胜率 | `>= 60%` |
| LONG样本外胜率 | `>= 55.56%` |
| SHORT样本外胜率 | `>= 55.56%` |
| 总订单保留率 | `>= 80%` |
| LONG订单保留率 | `>= 70%` |
| SHORT订单保留率 | `>= 70%` |
| 10U基础首单保留率 | `>= 85%` |
| 总EV、LONG EV、SHORT EV | 各自`>= 0` |
| 三个按时间顺序的OOS窗口 | 至少2个窗口EV为正 |
| 最大回撤 | 不得高于baseline |
| 最长连亏 | 不得长于baseline |

所有硬门槛通过的配置才进入排序，顺序固定为总胜率降序、订单数降序、最大回撤升序。报告同时输出baseline和candidate的总计、LONG/SHORT、胜率、EV、PnL、最大回撤、最长连亏、每日最佳/最差、每类guard rejection、基础首单和三个OOS窗口。任一门槛失败时`acceptance.passed=false`，本分支继续保持**未部署**。

### 42.5 本地验证

Task 17采用测试先行，先确认旧实现因缺少显式生产参数、N12/N20结算时间线、方向并发、发布门槛、配置排序和结构 equality 比较而失败，再实现回放。当前定向结果为`python3 -m unittest tests.test_daily_profile_replay -v`共11项通过；`python3 scripts/replay_daily_profile_selector.py --help`退出码为0并列出全部必填生产执行参数。生产SQLite的复制回放与完整测试属于后续发布检查点，未完成前不改变本节的**未部署**结论。
