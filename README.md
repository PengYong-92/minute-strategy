# 币安事件合约量价预警监控

功能：

- 直接拉取币安现货 1分钟K线。
- 按量价关系生成 10分钟候选分析。
- 使用动态评分阈值选择是否开10分钟单；30分钟只保留为多周期偏向观察，不再开单。
- 10分钟信号严格使用最近10根已收盘1分钟K线分析后10分钟方向。
- 默认最多同时持有1单，避免每根K线重复开单。
- 使用滚动守卫 + 三单叠加金额模拟下单，基础金额 10U，连续赢单金额为 `10U -> 18U -> 32.4U`，亏损或第三单后重置。
- 独立 Web 页面查看点位、预警、订单、胜负、盈亏和胜率。
- 页面高亮当前选择信号、时段是否允许、时段胜率/EV、入场和出场时间。
- Fear & Greed 实盘动态获取并缓存，用作方向风险阈值调节，不直接决定方向。
- SHORT 信号仍按 `WD/WE + UTC小时` 动态生成 MACD、RSI、BOLL 阈值并记录审计，但当前处于观察模式：不创建模拟订单、不触发 webhook、不参与滚单。

## 启动

```bash
python3 -m app.server --symbol BTCUSDT --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

可选参数：

```bash
python3 -m app.server --symbol ETHUSDT --poll-seconds 10 --limit 300
```

也可以使用跨平台启动脚本，适用于 macOS 和 Linux：

```bash
bash scripts/run.sh --symbol BTCUSDT --port 8000
```

常用写法：

```bash
bash scripts/run.sh
bash scripts/run.sh ETHUSDT 8080
bash scripts/run.sh --symbol BTCUSDT --host 0.0.0.0 --port 8000 --poll-seconds 10 --limit 300
bash scripts/run.sh --no-warmup
bash scripts/run.sh --db-path data/monitor.sqlite3
bash scripts/run.sh --webhook-url https://event.easy-tx.com/api/signals/ingest
```

运行要求：

- Python `3.10+`
- 无第三方 Python 依赖，仅使用标准库
- 运行时需要能访问 Binance 现货 K线接口和 Fear & Greed 接口

## SQLite 持久化

默认启用 SQLite：

- 默认路径：`data/monitor.sqlite3`
- 持久化模拟订单，重启后会恢复未结/已结订单和累计统计。
- 持久化每轮选择信号与开单决策，方便复盘为什么开单或为什么被过滤。
- 切换币种时按 `symbol` 隔离订单和信号审计记录。

指定数据库路径：

```bash
bash scripts/run.sh --db-path data/monitor.sqlite3
```

关闭持久化：

```bash
bash scripts/run.sh --no-persistence
```

## Webhook 推送

默认启用外部信号推送，只在模拟订单实际开单时触发，不会在 WAIT、冷却、重复信号、时段过滤时触发。

默认接口：

```text
https://event.easy-tx.com/api/signals/ingest
```

默认 payload：

```json
{
  "importToken": "your-import-token",
  "direction": "LONG",
  "symbol": "BTCUSDT",
  "timeIncrements": "TEN_MINUTE",
  "amount": 10.0,
  "message": "放量急跌反抽：回测显示急跌后后续窗口更偏反弹，动态评分偏多"
}
```

字段说明：

- `direction`: `LONG` 或 `SHORT`
- `symbol`: 当前交易对，默认 `BTCUSDT`
- `timeIncrements`: 当前只开10分钟单，固定为 `TEN_MINUTE`
- `amount`: 本单金额；当前实盘模拟使用滚动守卫 + 三单叠加，金额序列为 `10U -> 18U -> 32.4U`，亏损或第三单后重置
- `message`: 实际开单原因，来自策略分析结果

配置：

```bash
bash scripts/run.sh \
  --webhook-url https://event.easy-tx.com/api/signals/ingest \
  --webhook-token "$WEBHOOK_TOKEN" \
  --webhook-timeout 5
```

关闭 webhook：

```bash
bash scripts/run.sh --no-webhook
```

## 预热数据

实盘启动会先做历史K线预热：

- 默认读取 `data/` 下的 Binance Vision ZIP。
- 如果本地没有对应历史文件，会自动从 `https://data.binance.vision/` 下载。
- 默认下载/读取最近 `3` 个已完成月份的 `1m` K线。
- 默认额外下载/读取当前月份已完成自然日的 `1m` 日文件，用于避免只有上个月数据导致阈值画像断层。
- 预热完成后再合并 Binance 实时 REST K线；后续轮询不会覆盖预热历史。
- 页面顶部会显示：预热状态、预热K线数量、缓存文件数、下载文件数、缺失文件数。
- 页面顶部会显示当前 regime、风控暂停原因。
- 动态量能、波动、MACD、RSI、BOLL 指标画像会使用最近最多 `30` 天的已预热/实时K线，而不是只用 REST 接口最近几百根K线。

预热相关参数：

```bash
bash scripts/run.sh \
  --symbol BTCUSDT \
  --data-dir data \
  --warmup-months 3 \
  --warmup-timeout 20
```

关闭预热：

```bash
bash scripts/run.sh --no-warmup
```

只用已完成月份，不下载当前月日文件：

```bash
bash scripts/run.sh --no-current-month-daily
```

## 打包 / macOS 与 Linux 部署

生成可在 macOS 和 Linux 解压运行的源码包：

```bash
bash scripts/package.sh
```

产物默认输出到 `dist/`：

- `event-contract-monitor-YYYYMMDD-HHMMSS.tar.gz`
- `event-contract-monitor-YYYYMMDD-HHMMSS.zip`

部署到另一台 macOS/Linux 机器：

```bash
tar -xzf dist/event-contract-monitor-*.tar.gz
cd event-contract-monitor-*
bash scripts/run.sh --symbol BTCUSDT --port 8000
```

可选参数：

```bash
bash scripts/package.sh --output-dir release --name btc-event-monitor
bash scripts/package.sh --include-reports
```

默认不会打包 `.venv/`、`data/`、`reports/`、`dist/`、缓存和日志文件，避免把三个月K线数据和回测报告一起带走。部署后首次启动会自动下载预热数据；需要报告时使用 `--include-reports`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 离线回测

下载 Binance Vision 月度1分钟K线 ZIP 后运行：

```bash
python3 -m app.backtest \
  data/BTCUSDT-1m-2026-04.zip \
  reports/btcusdt_2026_04_final.json
```

回测支持可选订单叠加结算：基础 stake 为 `10U`，胜单按 `1.8x` 总返还；若上一单胜，下一单使用上一单总返还额作为 stake，最多连续三单，任一亏损或完成第三单后重置为 `10U`。固定 stake 仍是默认行为。

当前已验证数据：

- 文件：`data/BTCUSDT-1m-2026-04.zip`
- K线：`43200` 根，覆盖 `2026-04-01 00:00:00 UTC` 到 `2026-04-30 23:59:00 UTC`
- 当前最终策略：`97` 单，胜率 `69.07%`，盈亏 `+236U`
- 10分钟：`72` 单，胜率 `73.61%`，盈亏 `+234U`
- 30分钟：`25` 单，胜率 `56.00%`，盈亏 `+2U`

## 开单控制

- 每次只选择10分钟候选信号；30分钟不再作为开单周期。
- 候选信号必须满足：`abs(score) >= dynamic_threshold`。
- 动态阈值使用历史10分钟窗口。
- 10分钟使用独立的 `周期 + 工作日/周末 + UTC小时` 时段画像。
- 当前时段必须有本地三个月回测覆盖到事件合约赔率，否则只预警不开单。
- Fear & Greed 每小时缓存刷新一次；请求失败时使用缓存，首次失败时降级为 Neutral，不阻塞行情监控。
- SHORT 当前仅观察不下单；系统仍计算更严格的短空确认，要求 MACD 柱为负且继续走弱、RSI 未过冷/过热、BOLL 位置没有贴近动态下轨、没有明显下影承接，用于后续样本分析。
- MACD、RSI、BOLL 不再使用单一固定阈值，而是从最近历史中按 `周期 + WD/WE + UTC小时` 提取指标画像；同小时样本不足时回退到全局画像。
- 极度恐惧或恐惧低于30日均值时提高 SHORT 阈值；极度贪婪或贪婪高于30日均值时提高 LONG 阈值。
- `低位放量上涨` 已降级为仅预警观察；该形态在一年回测中容易把低位反弹叙事当成确定性机会，未覆盖事件合约赔率。
- 评分边际过高也不开单：`abs(score) - threshold >= 27` 视为追行情/过热区间，等待下一轮确认。
- 自省不按“某月/某年度低胜率”做永久剔除；低胜率只用于生成开单前可观测的滚动弱化标签。当前实盘模拟已启用滚动守卫：同一 `周期|时段|形态` 最近60天、至少5个已结样本若胜率低于62%或EV<=0.5U，则本次不开单。
- 实盘模拟金额使用三单叠加：`10U -> 18U -> 32.4U`，亏损或第三单后重置为 `10U`；webhook payload 会携带本单 `amount`。
- 有未结订单时不再开新单。
- 新单最小间隔默认10分钟。

## 规则摘要

- 高位放量滞涨：仅观察，暂不开单。
- 高位放量下跌：仅在 MACD/RSI/BOLL 与短空时段确认时生成 SHORT 观察信号，当前不实际开空。
- 高位无量上涨：10分钟短线看多。
- 低位缩量下跌：等待。
- 低位放量上涨：仅观察，暂不开单。
- 低位放量承接：仅观察，暂不开单。
- 量增价升：仅观察，暂不开单。
- 中位/非高位放量急跌：按反抽逻辑看多，但评分边际过热不开单。
- 量增价平：中高位转阴预警。

页面会展示：

- 当前10分钟选择信号。
- 动态评分和阈值。
- 量比和动态放量阈值。
- 阈值时段，例如 `WD-12`、`WE-12`。
- 价格位置。
- 收盘强度。
- 聚合10分钟/30分钟趋势偏向。
- Fear & Greed 值、分类、趋势和阈值调整。
- MACD/RSI/BOLL 当前值、动态阈值、指标画像时段和样本数。
- 时段样本、时段胜率、时段EV、时段最小分数边际。
- 入场时间和出场时间。

## 三个月分时段验证

当前已下载并验证：

- `data/BTCUSDT-1m-2026-02.zip`
- `data/BTCUSDT-1m-2026-03.zip`
- `data/BTCUSDT-1m-2026-04.zip`
- `data/BTCUSDT-30m-2026-02.zip`
- `data/BTCUSDT-30m-2026-03.zip`
- `data/BTCUSDT-30m-2026-04.zip`

说明：

- Binance Vision 有原生 `30m` K线，已下载并校验。
- Binance Vision 没有原生 `10m` K线，本项目从1分钟K线聚合生成10分钟K线。
- 聚合出的30分钟K线已与 Binance Vision 原生30m K线逐项校验，三个月均无差异。

分时段逻辑：

- 10分钟信号：当前10分钟总量，对比历史同 `工作日/周末 + UTC小时` 的10分钟滚动总量。
- 30分钟不再生成开单信号，仅保留聚合偏向和校验数据作为观察字段。
- 波动阈值也按同 `工作日/周末 + UTC小时` 的历史窗口计算。
- MACD、RSI、BOLL 阈值也按同 `工作日/周末 + UTC小时` 的历史指标分布计算；样本不足时回退到全局指标分布。
- 若某个 `WD/WE + UTC小时` 在三个月回测中未覆盖事件合约赔率，自动不开单，仅保留预警观察。
- 开单前会计算聚合10分钟和30分钟趋势偏向；本轮回测显示直接把该偏向加入评分会降低收益，因此当前只作为观察/自省字段展示，不强行改变开单方向。

三个月自省前后：

- 以下为旧版 10/30 双周期实验结果，仅作诊断留档：
  - 分时段阈值但不做时段胜率过滤：`1357` 单，胜率 `54.46%`，盈亏 `-268U`
  - 增加时段过滤后：`599` 单，胜率 `60.77%`，盈亏 `+562U`
  - 分周期时段过滤后：`565` 单，胜率 `62.30%`，盈亏 `+686U`
  - 旧版低质量时段剔除实验：`353` 单，胜率 `68.56%`，盈亏 `+826U`（仅作诊断，不作为永久月份/时段黑名单）
  - 降级亏损形态后：`344` 单，胜率 `68.02%`，盈亏 `+772U`
  - MCP 指标过滤 SHORT 后：`348` 单，胜率 `68.68%`，盈亏 `+822U`
  - 指标阈值分时段动态化后：`347` 单，胜率 `68.59%`，盈亏 `+814U`

旧版分周期诊断：

- 10分钟：`246` 单，胜率 `69.11%`，盈亏 `+600U`
- 30分钟：`101` 单，胜率 `67.33%`，盈亏 `+214U`

策略自查说明：

- `低位放量承接` 在三个月回测中未覆盖赔率，已降级为仅预警观察。
- `高位放量下跌` 不能直接按口诀放开做空；无指标过滤时会出现追空和下轨反弹风险。
- MCP 指标复查后，`高位放量下跌` 只在 MACD/RSI/BOLL 与短空专属时段同时确认时生成 SHORT；当前版本将 SHORT 改为观察样本，不计入模拟订单。
- 当前三个月离线回测未引入历史 Fear & Greed 序列，F&G 是实盘风险覆盖层；若要验证历史 F&G 对胜率的影响，需要额外拉取历史 F&G 并按订单时间回放。
- 当前实际开单只执行 LONG，主要有效形态是 `放量急跌反抽`；SHORT 是小样本观察方向，不放宽为主策略。

### 心理纪律复核后的1年回测（2026-05-18）

参考 `交易.md` 中列出的《投资交易心理分析》以及《交易心理分析 / Trading in the Zone》的交易纪律原则，本轮不按月份做剔除，而是只调整开单前可见、可解释的行为规则：

- `低位放量上涨` 降级为观察，避免用“低位反弹”的故事替代已验证优势。
- 评分边际 `>=27` 视为过热追单区间，等待下一次非极端确认。
- 保留 10分钟-only、滚动守卫、三单叠加。

最新一年数据已补齐到 `2026-05-17`：

| 口径 | 数据区间 UTC | 订单 | 胜率 | PnL | 总投入 | ROI | 最大回撤 | 最长连亏 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 心理纪律优化 + 滚动守卫 + 三单叠加 | 2025-05-18 至 2026-05-17 | 580 | 60.52% | +873.20U | 9635.20U | 9.06% | -384.16U | 8 |
| 同订单序列固定10U参考 | 2025-05-18 至 2026-05-17 | 580 | 60.52% | +518.00U | 5800.00U | 8.93% | - | - |

形态拆分：

- `放量急跌反抽`：574单，胜率60.10%，三单叠加 PnL `+794.48U`。
- `高位放量下跌`：6单，胜率100.00%，三单叠加 PnL `+78.72U`。

报告：`reports/btcusdt_1y_2025_05_18_2026_05_17_psychology_low_rise_observe_chase27_20260518.json`

最终回测报告：

- `reports/btcusdt_2026_02_dynamic_fng_indicators.json`
- `reports/btcusdt_2026_03_dynamic_fng_indicators.json`
- `reports/btcusdt_2026_04_dynamic_fng_indicators.json`
- `reports/indicator_audit_reclaim_fix.json`

## 胜负结算

- LONG：到期价 > 入场价为胜，否则负。
- SHORT：到期价 < 入场价为胜，否则负。
- 胜：`+8U`。
- 负：`-10U`。
