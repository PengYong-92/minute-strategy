# minute-strategy 发布交接文档

## 1. 文档目的

本文汇总 `minute-strategy` 截至2026-08-12的生产发布、策略变更、运行配置、数据基线、验证结果、已知差异和后续观察要求。各发布章节保留当时的事实快照，章节中的“当前”仅指该章节记录时点。

`docs/release-handoff.md` 是项目唯一的发布交接文档。后续发布只更新或追加本文，不再创建带日期、提交号或版本号的交接文档副本。

接手人员应先阅读 `docs/current-strategy.md` 掌握现行逻辑，再使用本文核对发布边界。历史设计和实施过程不再保留在工作树，可通过 Git 历史及 `v2026.08.10-profile-degradation-guard` 标签追溯。

### 1.1 当前阅读基线

| 项目 | 当前值 |
|---|---|
| 当前功能基线 | `07cfc3f`（Webhook最低延迟异步发送，尚未发布） |
| 固化标签 | `v2026.08.12-optimization-observability`（指向 `02369bf`） |
| 当前生产提交 | `02369bf2d2f17372486eb378f3473f3cf154b108` |
| 当前生产事实 | 第29节 |
| 本地未部署功能 | 第30节Webhook最低延迟异步发送；第28.6节候选继续只观察，不启用真实准入 |

第2至28节是历史发布、本地开发或较早分析快照；如与第29节冲突，以第29节为准。生产运行代码来自 `main`，`feature/1m-wave-direction-guard` 保留为历史功能分支。第27至28节记录的统计与观察能力已随第29节发布，但其中明确标记为观察候选的规则仍未启用真实拦截。

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

### 28.1 已优化：第二并发LONG独立画像与观察准入

状态：`本地实现完成，未部署；只增加独立记录和统计，不启用真实拦截`。

`2026-08-12 17:05:34 CST` 重新读取线上全部324笔订单，其中323笔已结、1笔未结。以下统计只使用已结订单，并按每笔开单时是否已有尚未到期订单重建 `FIRST/SECOND`，不使用10U/18U或 `stake_progression_step` 代替并发位置：

| 上下文 | 已结 | 胜率 | PnL | EV |
|---|---:|---:|---:|---:|
| LONG_FIRST | 41 | 65.85% | `+104.8U` | `+2.56U` |
| LONG_SECOND | 116 | 50.00% | `-148.0U` | `-1.28U` |
| SHORT_FIRST | 36 | 55.56% | `-25.6U` | `-0.71U` |
| SHORT_SECOND | 130 | 67.69% | `+341.6U` | `+2.63U` |

交接文档上次313笔边界之后新增10笔已结订单，4胜6负、胜率40.00%、PnL `-52.0U`。其中第二并发LONG新增8笔，3胜5负、胜率37.50%、PnL `-42.0U`；问题没有反转，第二并发LONG已从108笔50.93%/-106.0U进一步下降到116笔50.00%/-148.0U。

但优化单位不能是“全部第二并发LONG”。当前 `DPS-20260812-0800` 从08:00生效后已结35笔，17胜18负、胜率48.57%、PnL `-58.4U`。同一DPS内不同完整画像的第二并发表现明显分化：

| 完整画像与并发位置 | 已结 | 胜率 | PnL | 结论 |
|---|---:|---:|---:|---|
| LONG WD-05 SECOND | 11 | 63.64% | `+35.2U` | 保留，不应被全局规则误伤 |
| LONG WD-08 SECOND | 8 | 37.50% | `-42.0U` | 重点观察，尚未达到10笔独立样本 |
| LONG WD-01 SECOND | 1 | 0.00% | `-10.0U` | 样本不足 |
| SHORT WD-02 SECOND | 8 | 50.00% | `-9.6U` | 继续观察，不能用LONG规则处理 |

已完成内容：

1. LONG首单继续使用现行逻辑；
2. 不全局禁用或统一收紧第二并发LONG，按 `完整画像键 × DPS版本 × FIRST/SECOND` 独立统计；
3. 信号、订单、入口快照、观察样本和画像统计明确记录 `FIRST/SECOND`，并与滚单阶段分开；旧订单按开单和到期时间恢复该字段；
4. 第二并发画像不复用混合了首单的胜率和EV；
5. 独立统计与当前DPS生效区间对齐，不混用08:00前后的画像；
6. 第二并发LONG由自身画像与当前1分钟波段共同评估，不用大周期趋势；
7. `/api/order-profile.by_profile_slot` 对单一 `画像 × SECOND` 少于10笔标记 `COLLECTING`；达到10笔标记 `WATCH`，同时覆盖至少两个DPS才标记 `cross_dps_ready=true`；
8. `WD-05 SECOND` 当前为正样本，不能因 `WD-08 SECOND` 亏损被一并关闭。

当前继续验证“第二并发LONG仅在1分钟 `UP_LEG` 放行”，但仍不直接部署：

| 第二并发LONG波段 | 已结 | 胜率 | PnL | EV |
|---|---:|---:|---:|---:|
| UP_LEG | 15 | 73.33% | `+51.2U` | `+3.41U` |
| 非UP_LEG | 101 | 46.53% | `-199.2U` | `-1.97U` |

当前DPS内 `UP_LEG` 第二并发LONG只有3笔、2胜1负、PnL `+12.4U`。上次313笔之后新增的8笔第二并发LONG全部不是 `UP_LEG`，因此坏样本继续增加，但 `UP_LEG` 解法样本仍停留在15笔，尚不足以证明跨画像、跨DPS稳定。原完整因果回放结果252笔、胜率61.90%、PnL `+296.0U`、最大回撤86.4U、最长连亏4笔继续作为候选证据，不替代线上独立样本。

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

这些候选只有在新版本发布后，按完整画像键、DPS版本和并发位置积累独立样本并完成因果回放，才能重新进入代码修改范围。

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
