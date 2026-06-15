# 当前策略说明

更新时间：2026-06-15
代码范围：`app/strategy.py`、`app/indicators.py`、`app/state.py`、`app/order_policy.py`、`app/simulator.py`、`app/history.py`、`app/storage.py`

## 1. 策略目标与运行周期

当前程序是 BTC/USDT 等币安现货 1分钟K线驱动的事件合约监控策略。实盘运行只开 10分钟事件合约，30分钟周期目前不作为开单周期，只保留在历史参数和多周期趋势偏向计算中。

核心目标：

- 使用最近已收盘 1分钟K线识别量价形态。
- 用动态阈值、分时段 edge、滚动守卫过滤低质量信号。
- 只在信号满足方向、时段、边际、风控、订单间隔条件时模拟开单。
- 开单后通过 webhook 推送方向、周期和本单金额。
- 对所有开单记录入场快照，便于后续分析亏损订单和优化策略。
- SHORT 当前作为观察信号记录审计，不创建模拟订单、不推送 webhook、不参与滚单。

当前实盘开单周期：

```text
LIVE_TRADE_TIMEFRAMES = (10,)
```

每次轮询会对合并后的历史K线计算 10分钟候选信号，并从候选中选择最优信号。由于目前只有 10分钟候选，最终选择信号就是当前 10分钟分析结果。

## 2. 数据来源与运行流程

### 2.1 启动参数

主入口为：

```bash
bash scripts/run.sh
```

核心参数：

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `--symbol` | `BTCUSDT` | 交易对 |
| `--poll-seconds` | `10` | REST K线轮询间隔，单位秒 |
| `--limit` | `300` | 每次从币安 REST 拉取的 1分钟K线数量 |
| `--data-dir` | `data` | Binance Vision 历史文件缓存目录 |
| `--warmup-months` | `3` | 启动时加载的已完成月度文件数量 |
| `--warmup-timeout` | `20` | 单个预热文件下载超时秒数 |
| `--db-path` | `data/monitor.sqlite3` | SQLite 持久化路径 |
| `--stake` | `10` | 基础下单金额 |
| `--win-return` | `stake * 1.8` | 赢单总返还金额 |
| `--stake-progression-max-orders` | `3` | 最大连续滚单次数 |
| `--no-stake-progression` | 关闭参数 | 关闭赢单返还滚单 |
| `--no-warmup` | 关闭参数 | 关闭历史预热 |
| `--no-current-month-daily` | 关闭参数 | 不加载当前月份已完成日文件 |
| `--no-persistence` | 关闭参数 | 关闭 SQLite 持久化 |
| `--no-webhook` | 关闭参数 | 关闭 webhook 推送 |

### 2.2 历史预热

启动时默认执行历史预热：

1. 根据当前 UTC 日期生成预热目标。
2. 加载最近 `warmup_months` 个已完成月份的 Binance Vision 月度 `1m` ZIP。
3. 默认额外加载当前月份从 1号到昨日的日度 `1m` ZIP。
4. 本地文件存在则作为缓存使用。
5. 本地文件不存在则从 Binance Vision 下载。
6. 下载失败或单个文件损坏不会中断监控，只记录在 `WarmupReport.errors`。
7. 所有加载到的K线按 `open_time` 去重并排序。

预热 URL 规则：

```text
https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-YYYY-MM.zip
https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-YYYY-MM-DD.zip
```

预热报告字段：

- `status`: `READY`、`PARTIAL`、`ERROR`、`EMPTY`
- `loaded_klines`
- `cached_files`
- `downloaded_files`
- `missing_files`
- `errors`
- `from_time_ms`
- `to_time_ms`

### 2.3 实时K线

实时轮询使用币安 REST：

```text
GET https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit={limit}
```

返回数据会转换为 `Kline`：

- `open_time`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `close_time`

只保留已收盘K线：

```text
kline.close_time <= 当前时间
```

实时K线和预热K线按 `open_time` 合并。相同 `open_time` 后来的数据覆盖旧数据。内存最多保留：

```text
max_klines = 140000
```

## 3. 时间分段

策略使用 `WD/WE + UTC小时` 作为分时段画像键。

计算逻辑：

```text
hour = floor(timestamp_ms / 3600000) % 24
day = floor(timestamp_ms / 86400000)
weekday = (day + 3) % 7
day_type = WE if weekday >= 5 else WD
segment = "{day_type}-{hour:02d}"
```

示例：

- `WD-12`: 工作日 UTC 12点
- `WE-03`: 周末 UTC 03点

该分段用于：

- session edge 白名单
- 分时段胜率/EV
- 动态指标画像
- 滚动守卫 key
- 过热 edge 上限

## 4. 10分钟分析窗口

若当前周期为 10分钟：

```text
recent_size = 10
recent = 最近10根1分钟K线
history = recent之前的历史K线
```

最低历史要求：

```text
len(klines) >= timeframe_minutes * 3
```

10分钟信号使用最近10根已收盘1分钟K线判断后续10分钟方向。

## 5. 基础量价指标

### 5.1 量能基准与量比

量能计算使用最近最多 30天历史：

```text
DYNAMIC_PROFILE_LOOKBACK_MINUTES = 30 * 24 * 60
```

先在同一 `threshold_segment` 下计算滚动窗口成交量和：

```text
rolling_volume_sum = sum(window.volume)
```

若同分段样本少于 20，则回退到全局历史窗口。

基础量能：

```text
baseline = median(baseline_volumes)
recent_volume = sum(recent.volume)
volume_ratio = recent_volume / baseline
```

量能噪声：

```text
q75 = percentile(baseline_volumes, 75)
q25 = percentile(baseline_volumes, 25)
mad = median(abs(x - baseline))
volume_noise = mad / baseline
```

放量阈值：

```text
high_threshold = clamp(q75 / baseline + 0.2 + volume_noise * 0.35, 1.25, 2.4)
```

缩量阈值：

```text
low_threshold = clamp(q25 / baseline - 0.1, 0.45, 0.8)
```

量能状态：

```text
volume_state = HIGH   if volume_ratio >= high_threshold
volume_state = LOW    if volume_ratio <= low_threshold
volume_state = NORMAL otherwise
```

### 5.2 价格位置

价格位置使用最近最多 1440 根历史K线：

```text
prior_high = max(history[-1440:].high)
prior_low = min(history[-1440:].low)
price_position = clamp((latest_close - prior_low) / (prior_high - prior_low), 0, 1)
```

位置分桶：

```text
HIGH if price_position >= 0.8
LOW  if price_position <= 0.2
MID  otherwise
```

### 5.3 价格变化与方向

窗口涨跌幅：

```text
price_change_pct = (recent[-1].close - recent[0].open) / recent[0].open
```

动态波动阈值基于历史同周期窗口收益：

```text
window_return = (window[-1].close - window[0].open) / window[0].open
typical = median(abs(window_return))
floor = 0.0015 * sqrt(window_size / 10)
move_threshold_pct = max(floor, typical * 1.6)
```

方向：

```text
UP   if price_change_pct >= move_threshold_pct
DOWN if price_change_pct <= -move_threshold_pct
FLAT otherwise
```

### 5.4 收盘强度

窗口收盘强度：

```text
close_strength = (latest_close - recent_low) / (recent_high - recent_low)
```

单根K线收盘强度：

```text
candle_strength = (latest.close - latest.low) / (latest.high - latest.low)
```

上影压制：

```text
has_upper_rejection =
  candle_strength <= 0.35
  and (latest.high - latest.close) > (latest.close - latest.low) * 1.2
```

下影承接：

```text
has_lower_reclaim =
  candle_strength >= 0.65
  and (latest.close - latest.low) > (latest.high - latest.close) * 1.2
```

### 5.5 趋势一致性

统计 recent 内相邻收盘价变化：

```text
trend_score = (up_count - down_count) / (up_count + down_count)
```

无涨跌变化时为 `0`。

## 6. 聚合周期偏向

虽然当前只开 10分钟，但仍计算 10分钟和 30分钟聚合趋势偏向。

聚合规则：

- 按 `timeframe_minutes * 60000` 对齐 bucket。
- bucket 内必须有完整数量的 1分钟K线。
- open 取第一根 open。
- high 取最高。
- low 取最低。
- close 取最后一根 close。
- volume 求和。

趋势偏向：

```text
change = (last.close - first.open) / first.open
consistency = (up_closes - down_closes) / max(up_closes + down_closes, 1)
trend_bias = change * 100 + consistency
```

默认使用最近 3 根聚合K线。

字段：

- `mtf_10m_bias`
- `mtf_30m_bias`

## 7. MACD / RSI / BOLL 指标

### 7.1 技术上下文

技术上下文使用最近 `history[-240:] + recent` 生成。

MACD：

```text
ema_fast = EMA(close, 12)
ema_slow = EMA(close, 26)
macd_line = ema_fast - ema_slow
signal_line = EMA(macd_line, 9)
macd_histogram = macd_line - signal_line
macd_histogram_delta = latest_histogram - previous_histogram
```

RSI：

```text
period = 14
gain = sum(max(change, 0)) / 14
loss = sum(max(-change, 0)) / 14
RSI = 100 - 100 / (1 + gain / loss)
```

若 loss 为 0，RSI 为 100。

BOLL：

```text
period = 20
middle = SMA(close, 20)
std = sqrt(variance)
upper = middle + 2 * std
lower = middle - 2 * std
bollinger_position = (latest_close - lower) / (upper - lower)
bollinger_width = (upper - lower) / middle
```

### 7.2 SHORT 动态指标画像

只有在需要空单确认时才构建动态指标画像：

- 高位放量下跌。
- 放量下跌。
- 平量下跌。
- 当前时段属于 SHORT session edge 表。

画像样本来自最近 30天历史。

先尝试当前 `threshold_segment`：

```text
len(session_indices) >= max(12, timeframe_minutes)
```

满足则使用同分段画像，否则回退全局画像。

动态阈值：

```text
rsi_lower = clamp(percentile(rsi_sample, 20), 25, 45)
rsi_upper = clamp(percentile(rsi_sample, 85), 60, 82)
bollinger_lower = clamp(percentile(bollinger_sample, 25), 0.25, 0.50)
bollinger_upper = clamp(percentile(bollinger_sample, 90), 0.75, 0.95)
macd_histogram_threshold = min(0, percentile(macd_sample, 45))
macd_delta_threshold = min(0, percentile(macd_delta_sample, 55))
```

默认固定边界：

```text
SHORT_MIN_RSI = 35
SHORT_MAX_RSI = 70
SHORT_MIN_BOLLINGER_POSITION = 0.35
SHORT_MAX_BOLLINGER_POSITION = 0.85
```

### 7.3 SHORT 确认条件

SHORT 必须通过 `confirm_short_setup`：

```text
macd_histogram < indicator_profile.macd_histogram_threshold
macd_histogram_delta < indicator_profile.macd_delta_threshold
indicator_profile.rsi_lower < rsi < indicator_profile.rsi_upper
mtf_10m_bias < 2.0
has_lower_reclaim == False
```

高位放量下跌还要求 BOLL 位置有空间：

```text
indicator_profile.bollinger_lower <= bollinger_position <= indicator_profile.bollinger_upper
```

平量下跌 `require_bollinger_room=False`，不强制 BOLL 空间。

SHORT 指标加分：

```text
+7  if macd_histogram < 0
+5  if macd_histogram_delta < 0
+4  if 35 < RSI < 70
+4  if 0.35 <= BOLL位置 <= 0.85
+clamp(max(-mtf_10m_bias, 0) * 4, 0, 8)
+clamp(max(-mtf_30m_bias, 0) * 2, 0, 4)
```

## 8. 评分与形态判断

### 8.1 通用分数项

量能分：

```text
volume_points = clamp(24 + (volume_ratio / volume_threshold - 1) * 34, 0, 42)
```

波动分：

```text
move_points = clamp(abs(price_change_pct) / move_threshold_pct * 12, 0, 30)
```

趋势分在不同形态中按方向使用：

```text
max(trend_score, 0)
max(-trend_score, 0)
```

### 8.2 形态规则

#### 缩量

若 `volume_state == LOW`：

- 低位下跌：WAIT，理由为买盘未明显承接。
- 其他缩量：WAIT，理由为量能不足。

#### 高位放量下跌

条件：

```text
position == HIGH
volume_state == HIGH
direction == DOWN
```

若 SHORT 确认不足：WAIT。

若确认通过：

```text
score = -(
  30
  + volume_points
  + move_points
  + max(-trend_score, 0) * 10
  + (1 - close_strength) * 8
  + indicator_points
)
direction = SHORT
```

#### 高位放量滞涨

条件：

```text
position == HIGH
volume_state == HIGH
(has_upper_rejection or direction == FLAT)
```

结果：WAIT，仅预警观察。

#### 低位放量上涨

条件：

```text
position == LOW
volume_state == HIGH
direction == UP
```

结果：WAIT，仅预警观察。

#### 低位放量承接

条件：

```text
position == LOW
volume_state == HIGH
direction == DOWN
has_lower_reclaim == True
```

结果：WAIT，仅预警观察。

#### 量增价升

条件：

```text
volume_state == HIGH
direction == UP
```

结果：WAIT，仅观察不开单。

#### 放量急跌反抽

条件：

```text
volume_state == HIGH
direction == DOWN
```

非高位放量急跌按反抽做多：

```text
score =
  34
  + volume_points
  + move_points
  + max(-trend_score, 0) * 10
  + (1 - close_strength) * 6
direction = LONG
```

#### 量增价平

条件：

```text
volume_state == HIGH
direction == FLAT
position != LOW
```

结果：WAIT。

#### 量平价升

条件：

```text
volume_state == NORMAL
direction == UP
```

```text
score = 20 + move_points * 0.8 + max(trend_score, 0) * 8
direction = LONG
```

#### 量平价跌

条件：

```text
volume_state == NORMAL
direction == DOWN
```

若 SHORT 确认不足：WAIT。

若确认通过：

```text
score = -(
  18
  + move_points * 0.8
  + max(-trend_score, 0) * 8
  + indicator_points
)
direction = SHORT
```

### 8.3 分数裁剪

最终分数裁剪：

```text
score = clamp(score, -100, 100)
```

方向判断使用：

```text
score_abs = abs(score)
```

等级：

```text
S if score_abs >= threshold + 18
A if score_abs >= threshold
B otherwise
```

## 9. 动态阈值

基础动态开单阈值：

```text
typical = median(abs(window_returns)) if exists else 0.001
volatility_penalty = clamp(typical * 2500, 0, 18)
volume_penalty = clamp(volume_noise * 14, 0, 8)
base_threshold = 64 + volatility_penalty + volume_penalty
```

SHORT 方向额外提高：

```text
SHORT_THRESHOLD_PREMIUM = 8
threshold += 8 if direction == SHORT
```

30分钟额外提高 2，但当前实盘不启用 30分钟开单。

最终阈值会再经过：

1. 分时段 edge 调整。
2. Fear & Greed 调整。
3. regime 调整。
4. 总裁剪到 `[58, 95]`。

## 10. 分时段 edge 白名单

信号必须有对应方向和周期的 session edge，否则不开单。

### 10.1 LONG session edge

10分钟 LONG：

| 时段 | 样本 | 胜率 | EV |
|---|---:|---:|---:|
| WD-00 | 32 | 68.75% | 2.3750 |
| WD-08 | 23 | 60.87% | 0.9565 |
| WD-12 | 37 | 67.57% | 2.1622 |
| WD-18 | 26 | 65.38% | 1.7692 |
| WD-20 | 30 | 63.33% | 1.4000 |
| WD-22 | 30 | 63.33% | 1.4000 |
| WE-02 | 9 | 77.78% | 4.0000 |
| WE-03 | 8 | 75.00% | 3.5000 |
| WE-08 | 7 | 57.14% | 0.2857 |
| WE-13 | 6 | 66.67% | 2.0000 |
| WE-17 | 10 | 70.00% | 2.6000 |

30分钟 LONG 表仍保留在代码中，但当前不会开 30分钟单：

| 时段 | 样本 | 胜率 | EV |
|---|---:|---:|---:|
| WD-00 | 11 | 63.64% | 1.4545 |
| WD-05 | 15 | 66.67% | 2.0000 |
| WD-15 | 36 | 63.89% | 1.5000 |
| WE-21 | 8 | 75.00% | 3.5000 |
| WE-23 | 8 | 75.00% | 3.5000 |

### 10.2 SHORT session edge

10分钟 SHORT：

| 时段 | 样本 | 胜率 | EV |
|---|---:|---:|---:|
| WD-13 | 8 | 75.00% | 3.5000 |
| WD-21 | 8 | 87.50% | 5.7500 |
| WD-22 | 6 | 83.33% | 5.0000 |

### 10.3 分时段阈值调整

若 session edge 不存在：

```text
threshold += 8
session_allowed = False
```

若该时段质量高：

```text
win_rate >= 0.68 and ev >= 2.0
threshold -= 3
```

若该时段偏弱：

```text
win_rate < 0.60 or ev < 1.0
threshold += 3
```

阈值调整后裁剪到 `[58, 88]`，之后再叠加 Fear & Greed 和 regime 调整并最终裁剪到 `[58, 95]`。

## 11. 分数边际与过热过滤

分数边际：

```text
edge = abs(score) - threshold
```

基础最小边际：

```text
MIN_TRADE_EDGE = 10
```

30分钟会加 2，但当前实盘不启用 30分钟。

SHORT 额外最小边际：

```text
SHORT_EDGE_PREMIUM = 2
```

若时段质量高：

```text
session_edge_min -= 2
```

若时段偏弱：

```text
session_edge_min += 4
```

最终最小边际裁剪到 `[8, 18]`。

开单必须满足：

```text
session_edge_min <= edge < max_trade_edge
```

默认过热上限：

```text
MAX_TRADE_EDGE = 30
```

LONG 分时段特殊过热上限：

| 时段 | max_trade_edge |
|---|---:|
| WD-00 | 16 |
| WD-08 | 26 |
| WD-20 | 14 |
| WD-22 | 25 |
| WE-02 | 24 |
| WE-03 | 33 |
| WE-08 | 25 |
| WE-17 | 36 |

SHORT 当前统一使用默认 `30`。

若 `edge >= max_trade_edge`，视为极端过热或追行情，不开单。

## 12. Fear & Greed 作用

Fear & Greed 来自：

```text
https://api.alternative.me/fng/?limit=30&format=json
```

缓存：

```text
ttl_seconds = 3600
timeout_seconds = 5
```

请求失败：

- 若已有缓存，继续使用旧缓存。
- 若首次失败，降级为 Neutral。

计算字段：

```text
value = latest.value
average_30d = 最近返回值平均
trend = rising if latest >= average + 3
trend = falling if latest <= average - 3
trend = flat otherwise
```

### 12.1 情绪阈值调整

SHORT：

```text
value <= 25: +6
value <= 45: +3
value <= average_30d - 10: +1
trend == falling: +1
```

LONG：

```text
value >= 75: +6
value >= 60: +3
value >= average_30d + 10: +1
trend == rising: +1
```

单独 Fear & Greed 调整裁剪到 `[0, 9]`。

### 12.2 regime 标签

若有 Fear & Greed：

```text
value <= 45:
  falling -> FEAR_FALLING
  rising  -> FEAR_RISING
  else    -> FEAR_FLAT

value >= 60:
  falling -> GREED_FALLING
  rising  -> GREED_RISING
  else    -> GREED_FLAT
```

若无明显情绪：

```text
bollinger_width >= 0.02 -> HIGH_VOL
bollinger_width <= 0.002 and abs(mtf_30m_bias) < 1 -> LOW_VOL_RANGE
otherwise -> NEUTRAL
```

### 12.3 regime 阈值调整

```text
FEAR_FALLING + SHORT: +4
FEAR_FALLING + LONG + 30分钟: +3
FEAR_RISING + LONG + bollinger_position >= 0.88: +3
GREED_RISING + LONG: +2
HIGH_VOL + 30分钟: +2
```

当前实盘只开 10分钟，所以 30分钟相关调整不影响开单。

## 13. 最终开单条件

一个信号要实际开单，必须同时满足：

```text
raw_direction in {LONG, SHORT}
session_allowed == True
edge >= session_edge_min
edge < max_trade_edge
signal.actionable == True
direction != SHORT
无未结订单
距离上一单 >= 10分钟
非重复 signal_key
未触发同日同分段连续亏损暂停
未触发滚动守卫 DEGRADED
```

其中 `signal.actionable` 定义为：

```text
direction in {LONG, SHORT}
and abs(score) >= threshold
```

订单策略拒绝原因：

| code | 含义 |
|---|---|
| `BELOW_THRESHOLD` | 分数低于动态阈值 |
| `EDGE_TOO_SMALL` | 分数过阈但边际不足，或尚未达到过热上限 |
| `SESSION_BLOCKED` | 当前时段没有对应 session edge |
| `OVERHEATED` | edge 超过过热上限 |
| `HOLD_OPEN_ORDER` | 已有未结订单 |
| `COOLDOWN` | 距离上一单不足 10分钟 |
| `DUPLICATE_SIGNAL` | 同一信号已开过 |
| `RISK_PAUSED` | 同日同分段连续亏损达到 3 单 |
| `SHORT_OBSERVE_ONLY` | SHORT 观察模式，只记录信号和决策，不开单 |
| `ROLLING_EDGE_BLOCKED` | 滚动守卫判定该 setup 衰退 |
| `OPENED` | 成功开单 |

## 14. 滚动守卫

滚动守卫 key：

```text
{timeframe_minutes}|{threshold_segment}|{setup_name}
```

`setup_name` 从 reason 中取第一个中文冒号 `：` 之前的形态名。例如：

```text
10|WD-12|放量急跌反抽
10|WD-21|高位放量下跌
```

默认配置：

```text
lookback_days = 60
min_samples = 5
min_win_rate = 0.62
min_ev = 0.5
```

只使用当前时间之前、同 key、已结算且结果为 `WIN/LOSS` 的订单。

统计：

```text
wins = WIN数量
losses = LOSS数量
sample_size = wins + losses
pnl = sum(order.pnl)
win_rate = wins / sample_size
ev = pnl / sample_size
```

衰退条件：

```text
sample_size >= min_samples
and (win_rate < min_win_rate or ev <= min_ev)
```

衰退时不再开单，返回 `ROLLING_EDGE_BLOCKED`。

## 15. 同日同分段连续亏损暂停

风控会检查当天已结算订单：

```text
day = latest.close_time // 86400000
```

只看同一个 `threshold_segment` 的订单，从最新往前统计连续亏损。若达到 3 单：

```text
RISK_PAUSED
```

该规则按 UTC 自然日计算。

## 16. 订单模型与盈亏结算

开单字段：

- `direction`
- `timeframe_minutes`
- `entry_price`
- `opened_at`
- `expires_at = opened_at + timeframe_minutes * 60000`
- `threshold_segment`
- `score`
- `threshold`
- `session_*`
- `regime`
- `stake`
- `win_return`
- `stake_progression_step`

结算：

```text
LONG 胜利条件: exit_price > entry_price
SHORT 胜利条件: exit_price < entry_price
平价按 LOSS
```

盈亏：

```text
WIN:  pnl = win_return - stake
LOSS: pnl = -stake
```

默认事件合约赔率：

```text
stake = 10
win_return = 18
净赢 = +8
净亏 = -10
盈亏平衡胜率 = 10 / 18 = 55.56%
```

## 17. 滚单资金策略

默认开启滚单：

```text
enable_stake_progression = True
stake_progression_max_orders = 3
```

默认金额序列：

```text
10 -> 18 -> 32.4 -> 重置10
```

规则：

- 第一单使用基础 `stake`。
- 若上一单赢，下一单 stake 使用上一单 `win_return`。
- 若上一单亏，重置为基础 `stake`。
- 达到 `stake_progression_max_orders` 后重置。
- 若关闭滚单，每单固定使用基础 `stake` 和基础 `win_return`。

若启动：

```bash
bash scripts/run.sh --stake 20 --stake-progression-max-orders 3
```

默认 `win_return = 20 * 1.8 = 36`，金额序列：

```text
20 -> 36 -> 64.8 -> 重置20
```

## 18. Webhook

只有实际开单时发送 webhook，WAIT、风控拒绝、冷却、重复信号等不发送。

payload：

```json
{
  "importToken": "...",
  "direction": "LONG",
  "symbol": "BTCUSDT",
  "timeIncrements": "TEN_MINUTE",
  "amount": 10.0,
  "message": "开单原因"
}
```

`amount` 使用实际订单 stake，因此会随滚单变化。

## 19. SQLite 持久化与缓存

默认数据库：

```text
data/monitor.sqlite3
```

### 19.1 orders

保存模拟订单完整 payload。

主键：

```text
(symbol, order_id)
```

重启时按 symbol 恢复订单，累计统计和未结订单会恢复。

### 19.2 signal_audit

每轮保存一次选择信号和最终决策。

字段：

- `symbol`
- `created_at_ms`
- `decision`
- `direction`
- `timeframe_minutes`
- `threshold_segment`
- `regime`
- `score`
- `threshold`
- `reason`
- `payload`

该表用于复盘为什么开单或为什么不开单。

### 19.3 order_entry_snapshots

每次实际开单保存入场快照，结算后补充结果。

主要字段：

- `symbol`
- `order_id`
- `direction`
- `timeframe_minutes`
- `opened_at`
- `expires_at`
- `entry_price`
- `stake`
- `win_return`
- `stake_progression_step`
- `threshold_segment`
- `regime`
- `score`
- `threshold`
- `edge`
- `result`
- `settled_at`
- `exit_price`
- `pnl`
- `entry_payload`
- `settlement_payload`

`entry_payload` 包含：

- 完整 signal
- rolling_edge 快照
- latest_kline
- fear_greed
- stake_config
- order_policy

写入方式：

- 使用单线程 `ThreadPoolExecutor(max_workers=1)` 异步写 SQLite。
- 提交异步任务前会冻结订单副本，避免订单后续结算污染入场快照。
- 测试中可通过 `wait_for_storage_writes()` 等待异步写入完成。

## 20. 回测口径

回测模块使用 ZIP 中的 Binance 1分钟K线，按时间顺序模拟。

默认回测配置：

```text
warmup_minutes = 360
max_open_orders = 1
min_order_gap_minutes = 10
strategy_history_limit = 1440
stake = 10
win_return = 18
enable_stake_progression = False
stake_progression_max_orders = 3
enable_rolling_edge_guard = False
rolling_edge_lookback_days = 60
rolling_edge_min_samples = 5
rolling_edge_min_win_rate = 0.62
rolling_edge_min_ev = 0.5
short_observe_only = True
```

注意：

- 普通 `BacktestConfig` 默认不开滚单和滚动守卫。
- 普通 `BacktestConfig` 默认与实盘一致：SHORT 只观察，不进入订单统计。
- 研究脚本中会显式开启滚动守卫和三单叠加。
- 回测结算逻辑与实盘模拟一致。
- 若到期价等于入场价，视为 LOSS。

回测统计：

- 总单数
- 胜负数
- 胜率
- balance
- avg_pnl
- total_staked
- roi
- break_even_win_rate
- max_drawdown
- max_loss_streak
- max_win_streak
- 按周期、方向、时段、月份、regime 分组
- rejected_signals

## 21. 当前策略特征总结

当前策略的主要开单来源是 10分钟 LONG，尤其是“放量急跌反抽”。SHORT 逻辑存在，但当前只作为观察信号，不实际开空；系统仍保留严格的 MACD、RSI、BOLL、多周期偏向和下影承接确认，用于后续积累短空样本。

当前策略不是“看到下跌就追空”，而是：

- 非高位放量急跌更倾向按反抽做多。
- 高位放量下跌才考虑生成 SHORT 观察信号。
- 平量下跌只有在短空确认充分时才生成 SHORT 观察信号。
- 低位放量上涨、低位承接、高位滞涨、量增价升等形态只观察不开单。

最终是否开单由以下层级共同决定：

```text
K线窗口
 -> 量能/价格/技术指标
 -> 形态识别和方向评分
 -> 动态阈值
 -> 分时段 session edge
 -> Fear & Greed / regime 调整
 -> 最小 edge 和过热上限
 -> 订单状态 / 冷却 / 重复信号
 -> SHORT观察拦截
 -> 同日连续亏损风控
 -> 滚动守卫
 -> 模拟开单 + webhook + 入场快照
```

## 22. 2026-06-15 策略节点记录

本节点用于后续重新分析修改后的新增样本：

- 数据基线：生产接口截至 `2026-06-15 21:10:47` 共有 `72` 单，胜率 `52.78%`，累计 `-50.08U`。
- 上次发布后新增样本：`7` 单，`4W/3L`，累计 `+18.32U`。
- 其中新增亏损主要集中在 `LONG WD-18` 与 `LONG WD-12`；用本节点滚动守卫参数复盘时，这两单在开单前已有同 key 衰退迹象。
- 本节点修改滚动守卫默认值为 `60天 / 至少5样本 / 胜率>=62% / EV>0.5U`。
- 本节点将 SHORT 改为观察模式：保留信号审计与页面决策，返回 `SHORT_OBSERVE_ONLY`，但不创建订单、不推送 webhook、不消耗滚单状态。
- webhook 状态接口会对 `importToken` 做脱敏展示，内部 payload 不变。

后续分析口径：只把部署本节点后的订单作为“修改后样本”，与本节点之前的生产样本分开统计。
