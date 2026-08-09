# 当前策略说明

更新时间：2026-08-10
代码范围：`app/server.py`、`app/strategy.py`、`app/indicators.py`、`app/state.py`、`app/profile_degradation_guard.py`、`app/wave_state.py`、`app/wave_batch_guard.py`、`app/order_policy.py`、`app/simulator.py`、`app/history.py`、`app/storage.py`、`scripts/run.sh`

## 1. 策略目标与运行周期

当前程序是 BTC/USDT 等币安现货 1分钟K线驱动的事件合约监控策略。策略分析入口和实盘运行只接受 10分钟事件合约；30分钟订单周期的 edge、阈值修正、候选排序和分析入口已经删除。`mtf_30m_bias` 仍作为当前10分钟策略的内部偏向输入保留，本阶段不改写该活跃依赖。

核心目标：

- 使用最近已收盘 1分钟K线识别量价形态。
- 计算动态阈值和分时段 edge 作为画像与审计字段，由每日画像和风险守卫决定正式资格。
- 只在每日画像完整键、机械准入和启用中的风险守卫全部通过时模拟开单。
- 开单后通过 webhook 推送方向、周期和本单金额。
- 对所有开单记录入场快照，便于后续分析亏损订单和优化策略。
- LONG/SHORT 候选均持续记录观察结果，主程序每天从最近7天观察画像中选出当日启用策略。
- 每日画像是当前策略来源；完整键入选后，主信号的明确 `observe_direction` 或研究观察候选可以成为正式候选，`TRADE_SCORE_THRESHOLD` 只作审计。
- 默认总未结订单上限为2；方向结算序列守卫默认启用，1分钟波段方向否决和波段批次守卫默认关闭。

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
| `--max-open-orders` | `2` | 最多同时持有的未结订单数 |
| `--min-order-gap-minutes` | `2` | 两次开单最小间隔分钟数 |
| `--stake-progression-max-orders` | `2` | 兼容参数；生产固定为两阶段 |
| `--stake-progression-max-active` | `1` | 最多并行第二级订单数 |
| `--stake-progression-base-only-segments` | 空 | 兼容参数；默认所有已入选时段均可参与 |
| `--no-stake-progression` | 关闭参数 | 关闭赢单返还滚单 |
| `--no-warmup` | 关闭参数 | 关闭历史预热 |
| `--no-current-month-daily` | 关闭参数 | 不加载当前月份已完成日文件 |
| `--no-persistence` | 关闭参数 | 关闭 SQLite 持久化 |
| `--no-webhook` | 关闭参数 | 关闭 webhook 推送 |
| `--no-daily-profile-selector` | 关闭参数 | 关闭每日观察画像选策，回退静态主策略 |
| `--no-result-sequence-guard` | 关闭参数 | 关闭默认启用的按方向结算序列守卫 |
| `--profile-degradation-cooldown-minutes` | `60` | 完整画像连续3笔真实亏损后的冷却分钟数；`0` 关闭 |
| `--daily-profile-lookback-days` | `7` | 每日画像统计窗口 |
| `--daily-profile-min-samples` | `20` | 新画像入选最小独立样本 |
| `--daily-profile-weekend-min-samples` | `10` | 周末画像入选最小独立样本 |
| `--daily-profile-min-win-rate` | `0.60` | 新画像入选最低胜率，低于该值不入选 |
| `--daily-profile-min-ev` | `0` | 新画像入选最低EV，单位U |
| `--daily-profile-exit-win-rate` | `0.60` | 已启用画像退化胜率线 |
| `--daily-profile-degraded-runs` | `1` | 低于退出条件时当次日评估退出 |
| `--daily-profile-max-active` | `0` | 每天启用画像数量，`0` 表示不限制 |
| `--daily-profile-evaluation-time` | `07:50` | 北京时间每日评估时间 |
| `--daily-profile-activation-time` | `08:00` | 北京时间每日生效时间 |

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

虽然当前只开10分钟，但仍计算10分钟和30分钟现有指标偏向；30分钟不是订单周期，也不作为独立趋势投票。

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

30分钟 LONG session edge 已从当前运行代码删除。

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
FEAR_RISING + LONG + bollinger_position >= 0.88: +3
GREED_RISING + LONG: +2
```

## 13. 最终开单条件

当前现行顺序以第34节为准。量价、指标、评分和动态阈值负责形成主信号、研究观察候选、完整画像身份与审计字段；`TRADE_SCORE_THRESHOLD` 不独立决定资格。每日画像启用时，一个候选要实际开单必须同时满足：

```text
主信号已成立为 LONG/SHORT，或主 WAIT 具有明确 observe_direction，或属于研究观察候选
周期 + 策略族 + 策略标签 + 方向 + WD/WE时段完整画像已入选
入选后 daily_profile_selected == True，使明确方向候选 actionable
未结订单数 < 2
距离上一单 >= 2分钟
非重复 signal_key
若启用1分钟波段方向守卫，候选方向必须与波段一致
若启用波段批次守卫，未触发批次锁定、满额或全局冷却
未触发完整画像实时退化冷却或试探等待
未触发方向结算序列冷却
未触发滚动守卫 DEGRADED
若显式启用旧画像特征守卫，未被该守卫阻断
```

当前 `Signal.actionable` 定义为：

```text
direction in {LONG, SHORT}
and (daily_profile_selected == True or abs(score) >= threshold)
```

因此，完整画像命中可以让主 `WAIT` 的明确观察方向或研究观察候选成为正式候选；评分与原动态阈值继续保留用于审计。关闭每日画像选择器时，程序才回退到旧静态信号和观察画像兼容路径。

订单策略拒绝原因：

| code | 含义 |
|---|---|
| `BELOW_THRESHOLD` | 分数低于动态阈值 |
| `EDGE_TOO_SMALL` | 分数过阈但边际不足，或尚未达到过热上限 |
| `SESSION_BLOCKED` | 当前时段没有对应 session edge |
| `OVERHEATED` | edge 超过过热上限 |
| `WAVE_DIRECTION_BLOCKED` | 启用1分钟波段方向守卫后，波段不允许当前候选方向 |
| `DAILY_PROFILE_NOT_SELECTED` | 实时信号未进入今日启用画像 |
| `HOLD_OPEN_ORDER` | 未结订单已达到2单 |
| `COOLDOWN` | 距离上一单不足2分钟 |
| `DUPLICATE_SIGNAL` | 同一信号已开过 |
| `PROFILE_DEGRADATION_BLOCKED` | 当前完整画像处于退化冷却，或基础试探单尚未结算 |
| `SHORT_OBSERVE_ONLY` | 静态兼容模式下非实单时段的 SHORT，只记录观察 |
| `WAVE_BATCH_LOSS_LOCKED` | 启用波段批次守卫后，当前批次已有亏损，不再补单 |
| `WAVE_BATCH_FULL` | 启用波段批次守卫后，当前批次已达到2单 |
| `WAVE_GLOBAL_COOLDOWN` | 启用波段批次守卫后，失败批次或恢复单亏损触发全局冷却 |
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

## 15. 风险控制层与实时画像退化

当前风险控制按不同统计范围分层，不能把一个层级的样本或恢复状态复用于另一个层级：

| 层级 | 范围 | 触发 | 恢复 |
|---|---|---|---|
| 每日画像 | 完整画像/7天观察 | 60%与EV | 次日重评 |
| 实时画像退化 | 完整画像/当前DPS实单 | 固定连续亏损3单 | 配置冷却+基础试探 |
| 方向序列 | LONG或SHORT实单 | 连亏阈值 | 方向冷却 |
| 滚动优势 | 现有滚动key | 胜率/EV退化 | 滚动样本恢复 |

实时画像退化守卫只读取 `SETTLED` 且结果为 `WIN/LOSS` 的真实模拟订单，并按“完整画像键 + 当前 `daily_profile_version`（DPS版本）”精确隔离。连续亏损触发数固定为3；唯一启动参数是：

```text
PROFILE_DEGRADATION_COOLDOWN_MINUTES=60
```

`0` 表示关闭，不提供额外的 enable、最小样本、胜率或EV参数。冷却结束后只允许一笔10U基础试探，不消费已有18U资格；试探未结算时阻止同画像继续开单。试探赢后恢复 `NORMAL`，并允许按两阶段金额规则生成下一笔18U资格，即使该单同时属于波段恢复；试探亏则从该单结算时间重新进入完整冷却。

旧 segment-day 规则及 `OrderPolicy.risk_pause_reason` / `RISK_PAUSED` 已移除。它们按 UTC 日期和时段统计，不再代表当前开单逻辑。

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

## 17. 两阶段金额叠加策略

生产默认开启严格两阶段金额叠加：

```text
enable_stake_progression = True
stake_progression_max_orders = 2
stake_progression_max_active = 1
stake_progression_base_only_segments = ()
```

默认金额序列：

```text
10 -> 18 -> 重置10
```

规则：

- 第一级订单使用基础 `stake=10U`。
- 第一级赢后产生一次第二级资格；消费该资格的订单使用 `stake=18U`。
- 第二级订单无论胜负，结算后都不会产生第三级资格，后续订单回到基础 `stake`。
- 默认最多并行 1 个第二级订单。
- `stake_progression_base_only_segments` 默认为空，所有已入选时段均可参与两阶段金额叠加。
- `stake_progression_max_orders` 与 `stake_progression_base_only_segments` 继续作为兼容参数被接受；生产金额状态机固定为 2 级。
- 若关闭金额叠加，每单固定使用基础 `stake` 和基础 `win_return`。

若启动：

```bash
bash scripts/run.sh \
  --stake 20 \
  --stake-progression-max-orders 2 \
  --stake-progression-max-active 1 \
  --stake-progression-base-only-segments ""
```

默认 `win_return = 20 * 1.8 = 36`，金额序列：

```text
20 -> 36 -> 重置20
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

`amount` 使用实际订单 stake，因此当前只会是第一级金额或第二级金额，不会出现第三级。

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

本节保留旧研究脚本的历史三单回测配置，用于复现既有报告和结果；它不代表当前生产金额策略。当前生产配置以第17节的严格两阶段为准。

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

- 普通 `BacktestConfig` 默认不开金额叠加和滚动守卫。
- 普通 `BacktestConfig` 默认与实盘一致：SHORT 只观察，不进入订单统计。
- 历史研究脚本会显式开启滚动守卫和三单叠加，保留 `stake_progression_max_orders = 3` 仅用于复现旧结果。
- 历史三单结果不按当前两阶段配置重算或改写。
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

当前策略只做10分钟事件合约，每日画像是策略来源。原量价和技术指标生成主信号、明确的 `observe_direction`、研究观察候选、动态阈值和完整画像标签；完整画像入选后，可以把主 `WAIT` 的明确观察方向或研究观察候选变为正式候选。`TRADE_SCORE_THRESHOLD` 当前仅保留为审计参数，不独立改变资格。每日画像生效时不使用固定 SHORT 时段白名单；关闭每日画像后才回退静态兼容路径。

当前代码资金管理严格使用 `10U -> 18U -> 重置10U` 两阶段；第一级赢后才产生一次第二级资格，不存在第三级，最多并行第二级订单数默认是1，且所有已入选时段默认均可参与。总未结订单上限为2；批次锁定、全局冷却和普通波段恢复单不消费或生成18U资格，画像退化试探例外。

当前策略不是“看到下跌就追空”，而是：

- 非高位放量急跌更倾向按反抽做多。
- 高位放量下跌才考虑生成 SHORT 观察信号。
- 平量下跌只有在短空确认充分时才生成 SHORT 观察信号。
- 低位放量上涨、低位承接、高位滞涨、量增价升等形态只观察不开单。

最终是否开单由以下层级共同决定：

```text
K线窗口
 -> 10分钟量能/价格/技术指标
 -> 主信号明确观察方向 + 研究观察候选
 -> 完整画像标签、评分和动态阈值审计
 -> 最近7天每日画像精确匹配并赋予正式资格
 -> 订单状态 / 冷却 / 重复信号
 -> 1分钟波段方向否决（默认关闭，按配置启用）
 -> 波段批次 / 全局恢复守卫（默认关闭，按配置启用）
 -> 完整画像实时退化冷却 / 基础试探
 -> 方向结算序列守卫（默认启用）
 -> 滚动守卫
 -> 旧画像特征守卫（仅显式启用）
 -> 10U/18U金额分配
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

## 23. 2026-06-18 假突破观察候选节点

本节点验证的是“假突破观察候选”。经 walk-forward 审计后，本候选暂不进入运行观察层，也不进入正式开单、不推送 webhook、不消耗滚单状态。

候选来源：

- `failed_high_120m_short_observe`
  - 10分钟窗口冲高突破前 120分钟高点后收回。
  - 指标确认：`BOLL >= 0.50` 且 `MACD柱 < 0`。
  - 方向：SHORT，仅观察。
  - 研究固定白名单：`WD-14`、`WE-15`、`WD-18`、`WE-06`、`WD-21`。
- `failed_low_120m_long_observe`
  - 10分钟窗口跌破前 120分钟低点后收回。
  - 指标确认：`BOLL <= 0.35` 且 10分钟窗口收盘位置 `<= 0.35`。
  - 方向：LONG，仅观察。
  - 研究固定白名单：`WD-08`、`WE-09`。

两年回放结论：

```text
范围: 2024-06-18 -> 2026-06-17
全候选: 9391单, 胜率52.93%, pnl=-4432U, EV=-0.47U
研究固定白名单子集: 827单, 胜率59.98%, pnl=+658U, EV=+0.80U

failed_high_120m_short_observe: 667单, 胜率59.97%, pnl=+530U, EV=+0.79U
failed_low_120m_long_observe: 160单, 胜率60.00%, pnl=+128U, EV=+0.80U
```

滚动观察守卫回放：

```text
30天 / 至少20样本 / 胜率>=58% / EV>=0.5: 354单, 胜率61.58%, pnl=+384U, EV=+1.08U
60天 / 至少30样本 / 胜率>=58% / EV>=0.5: 376单, 胜率61.97%, pnl=+434U, EV=+1.15U
```

进一步 walk-forward 时段选择审计：

```text
90天训练 / 14天更新 / tag+segment: 861单, 胜率52.50%, pnl=-474U, EV=-0.55U
180天训练 / 30天更新 / tag+segment: 1089单, 胜率52.62%, pnl=-576U, EV=-0.53U
365天训练 / 30天更新 / tag+segment: 887单, 胜率51.75%, pnl=-608U, EV=-0.69U
180天训练 / 30天更新 / segment: 1203单, 胜率51.12%, pnl=-960U, EV=-0.80U
365天训练 / 30天更新 / segment: 680单, 胜率52.06%, pnl=-428U, EV=-0.63U
```

注意：

- 未过滤的全候选长期为负，因此不能把假突破逻辑整体升级为开单策略。
- 固定白名单子集长期为正，但该白名单来自全样本筛选，存在前视选择风险。
- 真正只用历史窗口滚动选择时段时，多个参数组合均为负。
- 因此当前运行策略不记录该假突破观察候选；只保留 `scripts/replay_observation_candidates.py` 作为研究脚本。
- 后续如果要重新启用，必须先找到 walk-forward 为正的训练窗口、更新周期和 key 粒度。

## 24. 2026-06-18 简单特征规则挖掘结论

为避免继续依赖人工假设，本节点新增 `scripts/mine_10m_feature_rules.py`，用两年 1分钟K线构造 10分钟事件合约规则并做训练/样本外验证。

验证范围：

```text
训练段: 2024-06-18 -> 2025-06-17
样本外: 2025-06-18 -> 2026-06-17
规则数: 20
```

规则覆盖：

- 极端上涨反转 SHORT：`ret5z`、`RSI`、`BOLL`、上影。
- 极端下跌反转 LONG：`ret5z`、`RSI`、`BOLL`、下影。
- 20分钟突破/跌破后的假突破反向。
- 压缩突破。
- 趋势延续。
- 放量/缩量配合 1分钟方向。

结论：

```text
样本外 EV 为正的规则数: 0
滚动守卫 EV 为正的规则数: 0

相对最不差:
short__break_up20__upper_rejection
训练: 1117单, EV=-0.17U
样本外: 1239单, 胜率53.35%, pnl=-492U, EV=-0.40U
滚动守卫: 75单, pnl=-138U, EV=-1.84U
```

因此：

- 单纯依靠常见技术特征组合，不足以覆盖 10U亏 / 8U赢 的事件合约赔率。
- 这类规则不能进入运行观察层，更不能升级开单。
- 后续应优先分析真实订单/观察数据库中的“错杀、错放、连续亏损前兆”，而不是继续盲目枚举通用技术指标组合。

## 25. 2026-06-18 订单错放 / 连续亏损前兆分析

本节点新增 `scripts/analyze_loss_precursors.py`，只读取 `order_entry_snapshots` 做离线分析，不改变正式开单逻辑。

历史口径说明：本节以及第26、27节中的三单滚单和弱时段基础金额结果，均是对应日期当时的历史回放快照。所有订单数、胜率、PnL 和 EV 原样保留，未按当前两阶段策略重算，也不代表当前生产配置。

分析口径：

- `RISK_NOW:*`：订单开出当下已经存在的风险画像，例如高 RSI 反抽、双周期偏多、弱时段等。
- `DEGRADED:*`：只使用当前订单之前的样本，在滚动窗口内统计同类 key 的胜率 / EV 是否转弱。
- `LOSS_STREAK:*`：只使用当前订单之前的样本，统计同类 key 是否已连续亏损。
- `reverse_direction_fixed_stake`：同一入场点位、同一到期点位，假设反向开单并固定 10U，仅用于观察是否存在 SHORT 候选，不等同于直接上线开空。

默认参数：

```text
lookback_days=7
min_samples=5
degraded_win_rate=50%
degraded_ev=0
global_loss_streak=2
key_loss_streak=2
```

当时本地数据库样本：

```text
样本: 49
区间: 2026-05-22T08:35:59.999000+00:00 -> 2026-06-05T12:48:59.999000+00:00
实盘记录: 胜率53.06%, pnl=-36.72U, EV=-0.75U
历史三单滚单重算: pnl=-40.56U, EV=-0.83U
同点位全部反向固定10U: 胜率46.94%, pnl=-76.00U, EV=-1.55U
```

结论一：不能把所有 LONG 亏损简单反过来开 SHORT。全样本同点位反向更差，说明 SHORT 只能从局部形态候选中观察，不应该替代主策略。

当前较强的 SHORT 观察候选：

```text
RISK_NOW:HIGH_RSI_REBOUND
orders=6, LONG实际胜率0.00%, actual_pnl=-90.40U
同点位反向胜率100.00%, reverse_pnl=+48.00U

RISK_NOW:DUAL_UP_BIAS_REBOUND
orders=7, LONG实际胜率14.29%, actual_pnl=-98.40U
同点位反向胜率85.71%, reverse_pnl=+38.00U

DEGRADED:RISK:SHALLOW_DROP_REBOUND
orders=9, LONG实际胜率11.11%, actual_pnl=-106.88U
同点位反向胜率88.89%, reverse_pnl=+54.00U
```

这些候选样本数仍小，只适合进入观察层，不适合直接实盘开空。

结论二：7天 / 至少5样本的前置弱化信号能提前捕获多数亏损，但也会误伤不少盈利单。

```text
ANY_PRIOR_WARNING:
拦截33单，拦截组实际pnl=-115.68U
剩余交易16单，胜率68.75%, pnl=+79.60U, EV=+4.97U
相对历史三单滚单基准 delta=+120.16U

错放覆盖:
loss=23, prior_warned_loss=18, prior_coverage=78.26%, prior_loss_pnl=-279.20U

错杀风险:
win=26, prior_warned_win=15, prior_coverage=57.69%, prior_win_pnl=+163.52U
```

因此 `DEGRADED` / `LOSS_STREAK` 更适合作为“暂停叠加、降频、观察拦截”的依据，不能只看 delta 就直接硬拦。

结论三：当前更值得继续验证的是：

- 高 RSI 反抽仍开 LONG 的错放。
- 10m / 30m 同时偏多但策略仍按急跌反抽开 LONG 的错放。
- 浅跌反抽在近7天同类样本转弱后的错放。
- 弱时段 `WD-00` / `WD-18` / `WD-22` 的持续劣化。

下一步样本增加后，应以当前节点为基准重新跑：

```bash
python3 scripts/analyze_loss_precursors.py --no-write
python3 scripts/analyze_monitor_db.py --no-write --min-group-size 2
```

只有当某个 SHORT 观察候选在新增样本中继续满足“LONG实际负EV、同点位反向正EV、错杀可控”，才考虑从观察层升级。

## 26. 2026-06-24 服务器 76 单新增样本复评

本节点通过 `https://victory.easy-tx.com/api/orders?page=1&page_size=100` 获取服务器公开订单样本。由于服务器当前包未开放 `/api/order-profile`，本次只能使用订单级字段分析，缺少开单瞬间 RSI、BOLL、risk_flags、profile_guard 影子快照。

样本范围：

```text
订单: 76
时间: 2026-05-20T20:22:59.999Z -> 2026-06-20T17:46:59.999Z
方向: LONG 74单, SHORT 2单
总结果: 胜率53.95%, pnl=-11.76U, EV=-0.15U

旧样本 <=49:
胜率53.06%, pnl=-36.72U, EV=-0.75U

新增样本 >=50:
胜率55.56%, pnl=+24.96U, EV=+0.92U

新增中段 50-69:
胜率45.00%, pnl=-61.68U, EV=-3.08U

新增尾段 70-76:
胜率85.71%, pnl=+86.64U, EV=+12.38U
```

关键发现：

- 新增样本整体转正，但收益高度依赖 70-76 的连续周末盈利；不能因此放宽全部 LONG 条件。
- 50-69 出现明显连续亏损，主要集中在 `WD-08`、`WD-12`、`WD-18`。
- `WD-00`、`WD-08`、`WD-12`、`WD-18`、`WD-22` 全量订单合计 54 单，胜率 44.44%，实际 pnl=-201.44U；直接硬拦会提高回放，但前视选择风险过高。
- SHORT 扩展当前只有 2 单：`WD-02` 赢、`WD-23` 亏，合计 -2U；样本不足，不升级也不删除。
- 该历史节点的三单滚单规则校验无误：第三单赢后重置。当时服务器实际 pnl 与本地校正回放一致。

本次策略优化不删除时段，而是先做资金层保护：

```text
默认弱段: WD-00,WD-08,WD-12,WD-18,WD-22
规则: 这些 LONG 时段仍可开单，但只使用基础金额 10U，不继承 18U/32.4U 滚单金额，也不推进或重置全局滚单状态。
```

订单级回放：

```text
当时全局三单叠加（历史口径）:
全量76单 pnl=-11.76U
旧样本<=49 pnl=-36.72U
新增>=50 pnl=+24.96U

弱广义时段基础金额保护:
全量76单 pnl=+35.28U
旧样本<=49 pnl=+18.64U
新增>=50 pnl=+16.64U

弱核心时段基础金额保护(WD-08/WD-12/WD-18):
全量76单 pnl=+35.60U
旧样本<=49 pnl=-21.36U
新增>=50 pnl=+56.96U
```

当时上线取保守版本：

- 当时默认 `STAKE_PROGRESSION_BASE_ONLY_SEGMENTS=WD-00,WD-08,WD-12,WD-18,WD-22`。
- 可通过 `--stake-progression-base-only-segments` 修改。
- 这是资金管理层调整，不改变方向判断、不新增趋势判断、不直接禁用时段。
- 后续如果服务器开放 SQLite 快照，应继续用 `analyze_loss_precursors.py` 复核弱段背后的 RSI/BOLL/risk_flags，而不是只按时段长期硬编码。

## 27. 2026-07-17 服务器 79 单复评

本节点再次通过服务器公开订单接口获取样本：

```text
接口: https://victory.easy-tx.com/api/orders?page=1&page_size=100
订单: 79
时间: 2026-05-20T20:22:59.999Z -> 2026-07-12T18:03:59.999Z
方向: LONG 77单, SHORT 2单
总结果: 胜率55.70%, pnl=+36.56U, EV=+0.46U
```

相比 2026-06-24 的 76 单，新增 3 单：

```text
77 LONG WE-17 WIN +8.00U
78 LONG WE-17 WIN +14.40U
79 LONG WE-17 WIN +25.92U
```

新增样本结论：

- 新增 3 单全部来自 `WE-17`，且连续三单全胜。
- `WE-17` 全量提升到 7 单 7 胜，pnl=+122.56U，EV=+17.51U。
- 强周末时段继续有效，不能因为工作日弱段亏损而削弱 `WE-17` 的滚单。
- SHORT 仍只有 2 单，`WD-02` 赢、`WD-23` 亏，样本不足，不做升级或删除。
- `/api/order-profile` 和 `/api/observation-summary` 仍返回 404，说明服务器还未运行本地新版，无法从服务端直接读取订单快照画像。

79 单订单级回放：

```text
当时全局三单叠加（历史口径）:
全量79单 pnl=+36.56U
旧样本<=49 pnl=-36.72U
新增>=50 pnl=+73.28U

弱广义时段基础金额保护(WD-00/WD-08/WD-12/WD-18/WD-22):
全量79单 pnl=+83.60U
旧样本<=49 pnl=+18.64U
新增>=50 pnl=+64.96U

弱核心时段基础金额保护(WD-08/WD-12/WD-18):
全量79单 pnl=+83.92U
旧样本<=49 pnl=-21.36U
新增>=50 pnl=+105.28U
```

本次不新增代码策略：

- 不新增趋势判断。
- 不把 `WE-17` 单独做更激进加仓，避免 7 单样本过拟合。
- 不删除 `WD-00/08/12/18/22`，只保留已实现的基础金额保护。
- 优先部署当前本地新版，让服务器后续样本具备 `/api/order-profile` 和观察画像接口。

## 28. 2026-07-17 独立观察画像与 WD-02/WD-23 SHORT 小口

### 28.1 问题与根因

服务器 SQLite 显示，2026-06-21 之后仍保持约 8000 次/日信号判断，但 27 天只产生 4 单。主要拦截不是评分不足，而是 `SESSION_BLOCKED`：静态 session edge 没有时段画像时不开单，不开单又无法产生新的真实订单样本，形成自我收缩。

直接放松 LONG 守卫不可行。无前视候选回放中：

```text
ROLLING_EDGE_BLOCKED LONG: 胜率28.57%, EV -4.86U
OVERHEATED LONG:           胜率36.59%, EV -3.41U
```

因此本次不降低动态阈值，不开放滚动优势衰退和过热候选。

### 28.2 动态观察画像

静态时段拦截、SHORT 观察等候选继续异步写入 `observation_signals`，10分钟后按固定赔率结算。每次准备开单时，只统计当前时点之前已经结算的数据：

```text
画像键: timeframe_minutes + strategy_family + direction + threshold_segment
窗口: 最近7天
最小独立样本: 12
最低胜率: 72%
最低EV: 4U
最低当前评分边际: 10
```

满足条件后只覆盖 `SESSION_BLOCKED`，生成带画像样本数、胜率和 EV 的可执行信号，再完整经过以下守卫：

1. 最大持仓数与10分钟冷却。
2. 重复信号检查。
3. 同时段同日三连亏暂停。
4. 评分过热上界。
5. 60天滚动优势守卫。
6. 订单弱点画像守卫。

观察单要求 `settled_at <= 当前候选时间`，回放和实盘都不能读取未来结算结果。同一画像在上一候选的10分钟到期前不新增观察单；对旧数据库中的重叠行，画像统计也只保留非重叠样本。该机制不是根据低胜率月份删单，而是让同一微观形态在近期重新证明正 EV 后恢复资格。

模拟订单和观察单均查找 `close_time == expires_at` 的1分钟 K 线精确结算。停机或漏轮询后如果到期分钟数据尚未补齐，订单保持未结，不使用下一分钟或恢复时的最新价格代替到期价格。

服务重启时，画像恢复不再使用页面接口的最近500条上限。SQLite 会按库内最新 `opened_at` 向前完整加载7天记录，并额外保留所有未结观察单；实际统计时仍按当前候选时间再次裁剪，避免旧数据或未来结算进入画像。

### 28.3 SHORT 实单边界

完整订单区间 `2026-05-21 04:22:59+08:00` 至 `2026-07-13 02:03:59+08:00` 的非重叠 SHORT 候选回放：

```text
WD-02: 6单, 4胜2负, 胜率66.67%, PnL +12U
WD-23: 5单, 4胜1负, 胜率80.00%, PnL +22U
组合新增: 11单, 8胜3负, 胜率72.73%, PnL +34U
```

为控制小样本风险：

```text
允许实单 SHORT 时段: WD-02,WD-23
金额: 基础金额
滚单: 不继承、不推进、不重置原滚单序列
其他 SHORT: 继续返回 SHORT_OBSERVE_ONLY
```

这不是把 LONG 亏损单直接反向。SHORT 仍必须先通过原有量能、MACD、RSI、BOLL、下影承接和评分边际判断。

### 28.4 启动参数

```bash
bash scripts/run.sh \
  --observation-profile-lookback-days 7 \
  --observation-profile-min-samples 12 \
  --observation-profile-min-win-rate 0.72 \
  --observation-profile-min-ev 4 \
  --observation-profile-min-edge 10 \
  --live-short-segments WD-02,WD-23
```

可用 `--no-observation-profile-promotion` 临时关闭动态放行。该历史节点当时的默认基础金额保护列表已加入 `WD-02/WD-23`。

原始逐分钟观察行按 `8/68%/3U/8` 回放会新增 37 个 LONG，但新增组仅 45.95% 胜率、固定金额 PnL -64U。改为10分钟独立样本后，该配置只新增 1 个 LONG 且亏损；默认提高到 `12/72%/4U/10` 后当前区间不新增 LONG。说明本次开单增量应来自已验证的 SHORT 小口，不应靠放松 LONG 画像门槛获得。

## 29. 2026-07-30 每日观察画像策略选择器

本节点替代第28节的“单条信号动态放行”。观察画像现在负责选择主程序当天采用的策略，而不是只在 `SESSION_BLOCKED` 后临时放行某条信号。

> 历史说明：本节后续正文记录2026-07-30节点行为，不代表当前代码。当前画像执行权限、阈值作用和候选来源统一以第34节为准。

完整画像键：

```text
timeframe_minutes + strategy_family + strategy_tag + direction + threshold_segment
```

每天北京时间07:50使用 `[7天前07:50, 当天07:50)` 内已结算且互不重叠的观察样本生成快照，08:00生效并保持到次日08:00。服务重启后按数据库恢复当日快照；当天快照缺失时补算，数据库失败则沿用上一版本。

新画像默认入选条件：

```text
独立样本 >= 20
胜率 >= 60%
EV >= 0U
每天最多启用4个
```

不再限制每天启用画像数量。已启用画像在胜率低于60%、EV不大于0或样本不足时，于当次日评估退出，确保低于60%的画像不会继续留在主程序。

服务从数据库恢复观察画像时会额外加载 1 天缓冲数据，随后仍由每日选择器按北京时间 07:50 截止点精确截取近 7 天。该缓冲用于保证服务在任意时刻重启后重算结果一致，不会扩大实际统计窗口。

每轮K线分析只把已成立的主实时信号与当天快照精确匹配。研究观察候选仅入库统计，不参与实际选择。匹配成功只能增加 `daily_profile_selected` 元数据，原始方向、`score`、`threshold`、MACD、RSI和BOLL条件必须已经成立。

每日画像只验证同方向实时信号，不替代动态评分阈值和技术指标资格，也不绕过以下通用控制：

1. 最大持仓和2分钟最小开单间隔。
2. 重复信号和同日同画像三连亏暂停。
3. 滚动优势守卫与订单弱点画像守卫。
4. 基础金额与严格两阶段金额叠加规则；第二级并行上限默认是1，所有已入选时段默认均可参与。
5. 订单、Webhook和入场快照记录。

SQLite 表 `daily_profile_selections` 按 `symbol + effective_from` 幂等保存每日候选、入选结果、连续退化次数、评估时间和生效区间。订单保存完整画像键和每日版本，后续真实样本从该版本生效时间开始独立评价。

## 30. 2026-08-05 两阶段金额叠加回放

本节点从服务器只读 API 获取 458 笔已结算模拟订单和最近 5000 条观察信号，其中 4997 条已结算。观察样本覆盖 `2026-07-19 00:13:59` 至 `2026-08-05 12:58:59`，严格走前画像回放区间为 `2026-07-27 08:13:59` 至 `2026-08-05 11:51:59`；所有策略使用完全相同的订单、方向、开单时间和胜负序列。

```text
策略                      订单  胜率    PnL       EV       ROI      最大回撤  峰值未结金额
固定10U                   331   53.47%  -124.00U  -0.37U   -3.75%   226.00U   20.00U
历史三阶段                331   53.47%  -494.88U  -1.50U   -9.46%   644.40U   64.80U
两阶段/第二级上限1        331   53.47%  -231.20U  -0.70U   -5.46%   398.80U   28.00U
两阶段/第二级上限2        331   53.47%  -232.80U  -0.70U   -5.47%   392.40U   28.00U
两阶段/第二级上限3至5     331   53.47%  -232.80U  -0.70U   -5.47%   392.40U   28.00U
```

默认上限1产生 116 笔第二级订单，其中 57 胜59负，第二级胜率 `49.14%`。方向拆分：

```text
LONG:  89单, 胜率50.56%, PnL -160.00U, 第二级28单/胜率35.71%
SHORT: 242单, 胜率54.55%, PnL  -71.20U, 第二级88单/胜率53.41%
```

该结果说明金额叠加没有改变信号胜率，但当前赢单后的下一笔订单不具备正向条件概率，尤其 LONG 第二级明显低于赔率盈亏平衡线。因此：

1. 两阶段实现保留严格 `10U -> 18U -> 10U`，不存在第三级；若继续模拟观察，第二级并发上限保持1，不提高到2至5。
2. 金额叠加与滚动守卫解耦，滚动守卫始终使用基础金额等价盈亏，确保开单集合不会因18U资金结果发生变化。
3. 在用于真实资金前，应先让每日画像走前基准恢复到正EV，再单独验证第二级条件胜率；不能把加金额当作修复低胜率的方法。
4. 当前主要优化对象是每日画像的样本外稳定性。该区间中 `SHORT WD-18`、`SHORT WD-00`、`LONG WD-01` 表现较好，而 `WD-10/WD-11/WD-13` 等画像退化明显，后续应基于每日评估前可见数据设计置信下界或连续退化门控，不能按本次完整区间结果直接静态删时段。

详细可复现结果保存在 `two-stage-sensitivity-20260805.json`，源走前回放保存在 `daily-profile-source-20260805.json`。

## 31. 2026-08-05 结算序列冷却守卫

### 31.1 否决方向预测序列

使用 `2024-05-01 08:00` 至 `2026-06-18 07:59` 的 `1,120,320` 根1分钟K线，对10分钟涨跌方向、连续状态、量能桶和RSI桶做每日7天滚动选择。参数只按前80%日期选择，后20%不参与选参。

训练最优参数 `move_run / N30 / 62%` 仍然失败：

```text
训练段: 5588单, 胜率50.39%, PnL -5192U, EV -0.93U
留出段: 1185单, 胜率50.55%, PnL -1068U, EV -0.90U
```

因此不把价格连涨/连跌状态用于预测 LONG/SHORT，也不把 LONG 亏损机械翻转为 SHORT。该实验仅保留为离线研究代码，不接入生产开单链路。

### 31.2 严格因果修正

旧 `analyze_loss_precursors.py` 的连续亏损统计按开单时间遍历后立即写入订单最终胜负。在最多5单重叠时，下一笔开单实际上还不知道上一笔10分钟后的结果，因此该统计不能作为生产证据。

新回放遵守以下约束：

1. 只有 `settled_at <= 当前 opened_at` 的已接受订单可以更新结算序列。
2. 被守卫拦截的历史订单不再把其事后胜负写回状态。
3. 守卫只暂停或恢复现有信号，不改变方向、不生成反向单。
4. 服务重启后直接从SQLite已结算订单恢复，无需额外运行时表。

### 31.3 服务器484单回放

样本覆盖 `2026-07-30` 至 `2026-08-05`，已结算484单，固定10U基准为251胜233负、胜率51.86%、PnL `-322U`。前4个有订单日期用于参数比较，末2日只用于验证。

最终采用保守参数：

```text
scope=DIRECTION
loss_streak=3
cooldown_minutes=20
```

结果：

| 区间 | 基准订单/PnL | 守卫后订单/胜率/PnL | 保留率 | PnL改善 |
|---|---:|---:|---:|---:|
| 训练 | 273 / -174U | 240 / 54.17% / -60U | 87.91% | +114U |
| 末两日验证 | 211 / -148U | 203 / 51.72% / -140U | 96.21% | +8U |
| 全部 | 484 / -322U | 443 / 53.05% / -200U | 91.53% | +122U |

守卫只能削减连续亏损，不能修复基准策略本身低于55.56%赔率盈亏平衡线的问题。后续应从本节点清空订单重新积累，分别统计 `RESULT_SEQUENCE_GUARD_BLOCKED`、实际放行订单和当日画像版本；不能仅用胜率提高就宣称策略转为正EV。

启动参数：

```bash
bash scripts/run.sh \
  --result-sequence-loss-streak 3 \
  --result-sequence-cooldown-minutes 20 \
  --result-sequence-scope DIRECTION
```

设置 `--no-result-sequence-guard` 可关闭。严格回放报告为 `reports/result-sequence-guard-server-20260805.json`，两年否决报告为 `reports/market-sequence-two-year-20260805.json`；`reports/` 默认不提交仓库。

> 当前状态：本节只保留历史回放与参数选择事实。当前代码默认以第34节为准：方向结算序列守卫启用，波段批次守卫关闭。

## 32. 2026-08-06 1分钟波段方向与批次守卫

> 历史说明：本节正文记录2026-08-06节点，当时关于画像不能提升 `WAIT`、波段方向强制否决和波段批次拦截的描述均不代表当前默认。当前代码以第34节为准：每日画像可赋予明确观察方向候选资格，方向否决和波段批次均默认关闭。

### 32.1 本节点当时的决策顺序

2026-08-06本节点当时的流水线为：

1. 只使用已闭合1分钟K线计算当前波段。
2. 原有10分钟量价、MACD、RSI和BOLL逻辑生成实时 `LONG`、`SHORT` 或 `WAIT`。
3. 1分钟波段守卫只校验该实时方向；冲突时改为 `WAIT`，但保留原方向用于影子观察。
4. 每日画像只匹配主实时信号的相同方向、策略族、策略标签和时段。
5. 执行评分边际、时段、并发、2分钟间隔、重复信号、波段批次、滚动优势和画像守卫。
6. 所有检查通过后才分配10U或已有的18U资格，并保存订单、入场快照和Webhook。

本节点当时规定后置模块不能把 `WAIT` 改成 `LONG/SHORT`，也不能互换方向；当时 `daily_profile_selected` 不绕过评分。该限制已被第34节记录的现行画像赋权逻辑替代。

### 32.2 1分钟波段计算

主窗口固定为最近8根已闭合1分钟K线，ATR窗口为最近14根1分钟K线。本节点不使用30分钟、4小时或日线趋势投票，也不使用会事后重画的ZigZag。

计算量：

```text
net = close[-1] - close[0]
path = sum(abs(close[i] - close[i-1]))
efficiency = abs(net) / path
direction_ratio = 与net同方向的分钟变化数 / 7
atr_strength = abs(net) / ATR14
range_position = (close[-1] - min(low)) / (max(high) - min(low))
```

趋势成立条件：

```text
efficiency >= 0.35
direction_ratio >= 0.60
atr_strength >= 0.50
```

状态和方向许可：

| 状态 | 判断 | 允许方向 |
|---|---|---|
| `UP_LEG` | 趋势条件成立且净变化向上，连续两根闭合分钟确认 | `LONG` |
| `DOWN_LEG` | 趋势条件成立且净变化向下，连续两根闭合分钟确认 | `SHORT` |
| `TURN_UP` | 上涨候选仅确认一次 | 无，等待 |
| `TURN_DOWN` | 下跌候选仅确认一次 | 无，等待 |
| `RANGE_HIGH` | 无明确趋势且区间位置不低于0.70 | `SHORT`均值回归候选 |
| `RANGE_LOW` | 无明确趋势且区间位置不高于0.30 | `LONG`均值回归候选 |
| `RANGE_MID` | 无明确趋势且位于区间中部 | 无，等待 |

波段守卫只做否决，不主动生成信号或反向开仓。即使波段允许方向，实时量能、评分和指标未成立仍然不开单。

### 32.3 每日画像权限

每日画像键保持：

```text
timeframe_minutes | strategy_family | strategy_tag | direction | threshold_segment
```

画像职责仅为：

- 验证已经成立的同方向实时信号；
- 对样本数、胜率或EV不达标的画像执行否决；
- 填充画像版本、样本数、胜率和EV元数据。

`observe_direction` 和研究观察候选只用于统计。原始信号为 `WAIT` 时保持 `WAIT`；原始LONG不能匹配SHORT画像，原始SHORT也不能匹配LONG画像。波段细分结果会存储并按组报告，但当前不加入每日画像键，避免样本被过度拆散。

关闭每日画像选择器后，旧观察画像兼容路径也遵守相同权限边界：只能验证已达到实时阈值的原方向，不读取 `observe_direction` 生成订单。`WAIT` 的最终决策保持为实时信号不足或原静态时段拦截。

### 32.4 并发与波段批次

生产默认总未结订单上限为2，基础单和18U第二级订单共同占用上限。两次开单最小间隔仍为2分钟。

波段批次ID：

```text
波段确认时间 | 波段状态 | 方向 | WD/WE时段 | 每日画像版本
```

同一波段状态未改变时确认时间保持不变，因此属于同一批次。规则：

- 当前批次无亏损且不足2单：可继续按通用门槛开单。
- 当前批次出现第一笔已结算亏损：立即锁定，不补位。
- 一赢一亏：旧波段继续锁定，等待新的已确认波段。
- 两笔全亏：记为失败批次，只允许新波段恢复。
- 60分钟内出现两个全亏批次：进入60分钟全局冷却。
- 冷却结束：只允许一笔恢复单；恢复单未结算前不允许其他订单。
- 恢复单盈利：解除全局恢复状态；恢复单亏损：重新冷却60分钟。

守卫每次都从订单中的批次、状态、结果和时间重建，不依赖仅存在内存中的计数。旧订单没有批次ID时不会被错误归为同一批次。

每根已闭合1分钟K线都会把波段状态、最后评估时间和 `confirmed_at` 同步写入SQLite `wave_runtime`。启动时先恢复同一交易对的版本化快照，再只对快照之后的预热K线按时间顺序增量重放。快照后的K线必须严格按1分钟连续；出现缺口时无法证明波段连续性，系统会只用缺口后的连续尾段建立新波段身份，旧18U资格不得跨缺口消费。

首次升级、版本变化或快照损坏导致没有有效快照时，系统从现有预热K线完整重建，并从持久化订单保守继承同状态的最近旧锚点，优先保持首亏锁定；空预热、不足15根得到 `UNKNOWN`，或恰好进入 `TURN_UP/TURN_DOWN` 的单根确认态时，都不会写入首个快照，也不会结束首次升级保护。由于此时无法证明资格连续性，所有旧 `PENDING` 18U资格必须在获得稳定波段后于预热阶段原子取消成功，才允许写入新快照。取消失败时保持无快照状态并暂停开单，下一次启动仍会重试。这样即使连续重启或同一波段持续超过默认300根预热K线，也不能绕过首亏锁定或消费升级前资格。

如果数据库快照比本次历史预热的最后一根K线更新，系统保留较新的快照和评估时间，不用旧历史反向重建或覆盖；实时数据追平后再恢复增量计算。运行态写入失败时返回 `STORAGE_ERROR` 并暂停开单。

### 32.5 两阶段金额叠加

正常状态保留严格两阶段：

```text
10U一级订单盈利 -> 产生一个18U资格
18U二级订单结算 -> 资格结束，回到10U
```

转折、新波段、画像版本切换、批次锁定或全局冷却时，旧波段所有尚未消费的 `PENDING` 资格变为 `CANCELLED`。取消操作先在SQLite单个事务中提交，成功后才修改内存；事务失败则保留内存 `PENDING`、返回 `STORAGE_ERROR` 并暂停开单，防止重启后资格复活。普通波段恢复单固定10U，即使数据库中存在待用资格也不消费；普通波段恢复单盈利也不生成18U资格。若该单同时标记为画像退化试探，试探赢按实时画像退化规则恢复 `NORMAL` 并允许生成下一笔18U资格。

### 32.6 保存与页面

`Signal`、`SimulatedOrder`、`ObservationSignal` 和订单入口快照保存：

- `wave_state`、`wave_raw_state`、`wave_window`；
- `wave_efficiency`、`wave_direction_ratio`、`wave_atr_strength`；
- `wave_confirmations`、`wave_confirmed_at`；
- `wave_batch_id`、`wave_guard_mode`；
- `wave_guard_status`、`wave_guard_reason`。

旧JSON载荷缺少这些字段时使用 `UNKNOWN`、空批次和 `NORMAL` 默认值。`/api/state` 输出 `wave_state` 与 `wave_batch_guard`；页面显示当前波段、允许方向、确认次数、批次订单胜负、冷却截止和恢复模式。数据库分析按波段状态和守卫模式分组，但不直接把这些小样本分组用于生产选策。

### 32.7 本节点验证边界

本节点按要求不执行历史回放或回测。参数采用已确认设计值，只运行纯函数、状态、模拟器、SQLite、API和页面单元测试。后续实际样本积累后，应分别统计 `WAVE_DIRECTION_BLOCKED`、`WAVE_BATCH_LOSS_LOCKED`、`WAVE_GLOBAL_COOLDOWN`、恢复单和正常订单，不能用被否决信号的事后结果反向污染生产守卫。

## 33. 2026-08-07 可配置开单评分阈值

> 历史说明：本节正文记录2026-08-07节点，保留当时阈值放行与生产调整事实。当前代码中 `TRADE_SCORE_THRESHOLD` 为审计参数，不独立改变资格；候选来源和默认守卫状态以第34节为准。

### 33.1 启动参数

新增启动参数和环境变量：

```text
--trade-score-threshold auto|0..95
TRADE_SCORE_THRESHOLD=auto|0..95
```

仓库默认值为 `auto`，保持2026-08-06严格行为。数值模式只覆盖当天已入选、且与当前主信号的周期、策略族、策略标签、方向和时段完整一致的画像。生产首次使用 `0` 放开评分限制，便于从实际订单逐步提高阈值；这不放开未入选画像，也不执行研究观察候选。

### 33.2 本节点当时的决策顺序

2026-08-07本节点当时的顺序调整为：

1. 原有10分钟量价、MACD、RSI和BOLL计算评分、原始动态阈值及主画像标签。
2. 每日画像只对当前主信号做完整键匹配，不从观察候选列表挑选订单。
3. `auto` 要求原信号已经达到动态阈值；数值模式要求 `abs(score) >= TRADE_SCORE_THRESHOLD`，满足后使用画像方向形成候选。
4. 候选方向进入1分钟波段守卫；冲突、转折未确认或区间中部仍改为 `WAIT`。
5. 继续执行订单上限、2分钟间隔、重复信号、波段批次、结算序列、滚动优势和画像守卫。
6. 全部通过后才开模拟订单、分配10U/18U金额并推送Webhook。

因此阈值为0只移除评分门槛，不移除策略选择和风险控制。未入选画像、画像键不匹配、波段方向冲突、批次首亏锁定、全局冷却、并发已满或间隔不足都仍然不能开单。

### 33.3 阈值记录

每个新信号和订单保留三个值：

```text
score                 实际开单评分
threshold             本次真正执行的开单阈值
calculated_threshold  原策略计算的动态阈值
```

旧订单没有 `calculated_threshold` 时页面回退显示原 `threshold`，不迁移SQLite表。模拟订单列表新增“开单阈值”列，主值显示实际阈值，次行显示评分和原始动态阈值。订单入口快照额外保存阈值模式，`/api/state.trade_score_threshold` 返回：

```json
{"mode":"AUTO","value":null}
```

或：

```json
{"mode":"OVERRIDE","value":0.0}
```

### 33.4 本节点当时的调整原则

该节点要求生产阈值从低到高逐步调整，每次只修改 `TRADE_SCORE_THRESHOLD` 并记录服务重启时间。后续分析以该时间作为样本边界，按实际阈值、评分、原始动态阈值、方向、画像键和波段状态分组；不能把不同阈值阶段的订单直接合并评价，也不能因订单数量增加而放松每日画像65%入选门槛或波段守卫。

以上是2026-08-07节点当时的历史约束，不代表当前代码默认。当前每日画像入选门槛为60%，波段方向守卫和波段批次守卫均默认关闭，现行语义以第34节为准。

## 34. 2026-08-08 订单量恢复节点

本节替代第21节、第32节和第33节中关于画像执行权限、数值评分阈值、1分钟波段方向否决和波段批次拦截的当前生产描述。历史设计和审计字段继续保留，但不再作为默认开单链路。

### 34.1 每日画像成为策略来源

每天07:50仍使用前7天独立已结算观察样本评估画像，08:00生效。画像键继续完整匹配：

```text
周期 | 策略族 | 策略标签 | 方向 | WD/WE时段
```

默认入选和退出胜率均为60%，工作日最少20个独立样本，周末最少10个独立样本，EV必须不低于0。7天窗口内每个周末小时理论上最多约12个独立10分钟样本，因此不能沿用工作日20样本门槛。入选后：

1. 已达到自身动态阈值的主信号可以按完整键匹配画像。
2. 研究观察候选也可以按其 `observe_direction` 匹配画像并成为正式订单信号。
3. 未入选画像仍只记录观察，不开单。
4. 主信号为 `WAIT` 时，数值评分阈值本身不能提升订单；但主信号具有明确 `observe_direction` 且完整画像键已入选时，每日画像可以赋予开单资格。
5. 画像入选候选保留原始 `score`、`threshold` 和 `calculated_threshold`，订单列表继续显示这些审计值。

画像是基于独立结算样本的策略选择层，因此入选观察候选不再要求当前评分超过原动态阈值；`Signal.actionable` 对 `daily_profile_selected=true` 的候选直接成立。

同一天修改画像窗口或入选、退出参数时，程序会比较已保存快照中的完整配置并立即重评；配置未变化时仍保持每天只评估一次。重评使用同一版本键覆盖当天快照，不删除历史日期记录。

### 34.2 当前开单顺序

```text
1分钟K线形成10分钟量价与指标画像
 -> 主信号和研究观察候选
 -> 每日画像完整键筛选
 -> 最大2笔未结订单 / 最小2分钟间隔 / 重复信号
 -> SHORT静态兼容限制（仅每日画像关闭时生效）
 -> 波段批次守卫（按配置启用）
 -> 完整画像实时退化守卫（当前DPS实单连续3亏）
 -> 同方向连续3亏冷却20分钟
 -> 滚动优势守卫
 -> 画像守卫（默认影子观察）
 -> 10U/18U两阶段金额；退化恢复只放行10U基础试探
 -> 模拟开单、Webhook和入口快照
```

### 34.3 波段与阈值的现行作用

- 1分钟波段仍计算、持久化并展示状态、允许方向、确认次数和批次ID，但默认不修改订单方向。
- 波段批次首亏锁定、批次满额和全局恢复逻辑默认关闭，只保留状态字段和可选实现。
- `TRADE_SCORE_THRESHOLD=auto|0..95` 继续兼容旧启动配置和API，但数值模式为 `AUDIT_ONLY`，不独立改变任何信号的开单资格；每日画像命中仍可提升主 `WAIT` 或研究观察候选。
- 实际订单阈值列继续展示候选原动态阈值，不能再把该列理解为全局放行开关。

### 34.4 当前风险边界

- 最多同时2笔，最小间隔2分钟。
- 同一完整画像键在当前DPS版本内连续3笔真实亏损后，默认冷却60分钟；冷却后只允许一笔10U基础试探，未结算时阻止同画像。
- 画像退化试探赢恢复正常并可产生下一笔18U资格，即使同时属于波段恢复；试探亏重新冷却。
- 同方向连续3笔已结算亏损后冷却20分钟，默认启用。
- 滚动优势守卫默认启用。
- 两阶段金额仍为第一笔10U，赢后下一笔18U，随后重置；最多一个并行18U资格。
- 每日画像的60%门槛负责先保证有足够订单，再根据新节点实际样本逐步优化，不以单个波段状态一次性关闭整类信号。
