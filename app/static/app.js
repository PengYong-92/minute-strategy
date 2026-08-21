const $ = (id) => document.getElementById(id);
const DASH = "-";
let currentSymbol = "BTCUSDT";
let ordersPage = 1;
let ordersTotalPages = 1;
let lastFilterOptions = null;
let observationsPage = 1;
let observationsTotalPages = 1;
let lastObservationFilterOptions = null;
let lastState = null;
let lastOrderProfile = null;
let lastObservationSummary = null;
let stateRequestInFlight = false;
let priceRequestInFlight = false;
let symbolRevision = 0;

function num(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function optionalNum(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function escapeHtml(value) {
  return String(value ?? DASH)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtPrice(value) {
  if (value === null || value === undefined) return DASH;
  return num(value).toLocaleString("en-US", { maximumFractionDigits: 8 });
}

function fmtMoney(value) {
  if (value === null || value === undefined) return DASH;
  const number = num(value);
  const sign = number > 0 ? "+" : "";
  return `${sign}${number.toFixed(2)}U`;
}

function fmtPct(value) {
  if (value === null || value === undefined) return DASH;
  return `${(num(value) * 100).toFixed(2)}%`;
}

function fmtTime(ms) {
  if (!ms) return DASH;
  return new Date(ms).toLocaleString();
}

function formatAdaptiveProfile(profile) {
  if (!profile || typeof profile !== "object" || !Object.keys(profile).length) return DASH;
  const parts = [];
  const qualification = profile.qualification_state;
  if (qualification && qualification !== "UNKNOWN") parts.push(String(qualification));

  const formatWindow = (label, window) => {
    if (!window || typeof window !== "object" || window.sample_size === undefined) return "";
    return `${label} N${num(window.sample_size)} ${fmtPct(window.win_rate)} EV ${fmtMoney(window.ev)}`;
  };
  const fast = formatWindow("7d", profile.fast_7d);
  const stable = formatWindow("14d", profile.stable_14d);
  if (fast) parts.push(fast);
  if (stable) parts.push(stable);

  const status = profile.status || profile.state;
  if (status && status !== "UNKNOWN") {
    let immediate = String(status);
    const n12 = profile.n12;
    if (n12 && typeof n12 === "object" && n12.sample_size !== undefined) {
      immediate += ` N12 ${num(n12.wins)}/${num(n12.sample_size)}`;
    }
    const n20 = profile.n20;
    if (n20 && typeof n20 === "object" && n20.sample_size !== undefined) {
      immediate += ` N20 EV ${fmtMoney(n20.ev)}`;
    }
    parts.push(immediate);
  }
  return parts.length ? parts.join(" · ") : DASH;
}

function fmtShadowOptimizer(shadow) {
  if (!shadow || shadow.status === "DISABLED") return "已关闭";
  if (shadow.status === "FAILED") return `异常 · ${shadow.error || `退出码${shadow.exit_code ?? DASH}`}`;
  const sample = Number(shadow.settled_orders || 0);
  const days = Number(shadow.complete_days || 0);
  const winRate = Number(shadow.win_rate || 0) * 100;
  const arms = Number(shadow.active_arms ?? shadow.arms ?? 0);
  const capacity = shadow.capacity_status || "UNKNOWN";
  return `${shadow.status || "UNKNOWN"} · ${arms}组 · ${days}日/${sample}单 · 胜率${winRate.toFixed(2)}% · ${capacity}`;
}

function formatEntryStructure(structure) {
  if (!structure || typeof structure !== "object" || !Object.keys(structure).length) return DASH;
  const values = [
    structure.entry_structure_state || structure.state,
    structure.entry_structure_bias || structure.bias,
    structure.active_level_source,
    structure.candidate_origin,
  ].filter((value) => value && value !== "UNKNOWN");
  return values.length ? values.map(String).join(" · ") : DASH;
}

function fmtFearGreed(fearGreed) {
  if (!fearGreed || fearGreed.value === null || fearGreed.value === undefined) return DASH;
  return `${fearGreed.value} ${fearGreed.classification || ""} · ${fearGreed.trend || "unknown"}`;
}

function fmtWarmup(warmup) {
  if (!warmup) return DASH;
  const cached = Array.isArray(warmup.cached_files) ? warmup.cached_files.length : 0;
  const downloaded = Array.isArray(warmup.downloaded_files) ? warmup.downloaded_files.length : 0;
  const missing = Array.isArray(warmup.missing_files) ? warmup.missing_files.length : 0;
  return `${warmup.status} · ${warmup.loaded_klines || 0}根 · 缓存${cached}/下载${downloaded}/缺失${missing}`;
}

function fmtWebhook(webhook) {
  if (!webhook || !webhook.enabled) return "OFF";
  return "ON";
}

function fmtRollingEdge(edge) {
  if (!edge || edge.status === "UNKNOWN") return DASH;
  const mode = edge.observe_only ? "观察" : "守卫";
  return `${mode} ${edge.status} · ${edge.sample_size}样本 · 胜率${fmtPct(edge.win_rate)} · EV ${fmtMoney(edge.ev)}`;
}

function fmtResultSequenceGuard(guard) {
  if (!guard) return DASH;
  if (!guard.enabled) return "OFF";
  const scope = guard.scope === "GLOBAL" ? "全局" : "同方向";
  if (guard.status === "PAUSED") {
    const directions = Array.isArray(guard.paused_directions) ? guard.paused_directions.join("/") : guard.direction;
    return `${directions || scope} PAUSED · ${guard.consecutive_losses || 0}连败 · 至 ${fmtTime(guard.pause_until)}`;
  }
  return `NORMAL · ${scope}${guard.loss_streak || 0}连败 / ${guard.cooldown_minutes || 0}分钟`;
}

function fmtWaveState(wave) {
  if (!wave) return DASH;
  if (!wave.enabled) return "OFF";
  const allowed = Array.isArray(wave.allowed_directions) && wave.allowed_directions.length
    ? wave.allowed_directions.join("/")
    : "无";
  return `${wave.state || "UNKNOWN"} · 允许${allowed} · 确认${wave.confirmations || 0}`;
}

function fmtWaveBatchGuard(guard) {
  if (!guard) return DASH;
  if (!guard.enabled) return "OFF";
  const batch = `${guard.batch_orders || 0}单 ${guard.batch_wins || 0}胜/${guard.batch_losses || 0}负`;
  if (guard.mode === "COOLDOWN") return `COOLDOWN · 至 ${fmtTime(guard.pause_until)}`;
  if (guard.mode === "RECOVERY") return `RECOVERY · 仅固定10U`;
  if (guard.blocked) return `${guard.mode || "BLOCKED"} · ${batch}`;
  return `${guard.mode || "PENDING"} · ${batch}`;
}

function fmtProfileDegradationGuard(guard) {
  if (!guard) return DASH;
  if (guard.enabled === false || guard.status === "DISABLED") return "关闭";
  if (guard.enabled !== true || !guard.status) return DASH;
  if (["NORMAL", "NOT_APPLICABLE"].includes(guard.status)) return "正常";
  const profileKey = guard.profile_key || DASH;
  if (guard.status === "COOLDOWN") {
    return `冷却 · ${profileKey} · 连亏${guard.consecutive_losses ?? 0}`;
  }
  if (guard.status === "RECOVERY_READY") return `待试探 · ${profileKey}`;
  if (guard.status === "RECOVERY_PENDING") {
    return `试探待结算 · 订单#${guard.probe_order_id || DASH}`;
  }
  return DASH;
}

function fmtDirectionPulseShadow(shadow) {
  if (!shadow || shadow.mode !== "SHADOW_ONLY") return DASH;
  const directions = shadow.directions || {};
  const formatDirection = (direction, label) => {
    const windows = directions[direction] || {};
    const formatWindow = (size) => {
      const item = windows[String(size)] || {};
      return `N${size} ${item.status || "WARMUP"} ${fmtPct(item.win_rate)}`;
    };
    return `${label} ${formatWindow(12)} / ${formatWindow(16)}`;
  };
  return `${formatDirection("LONG", "L")} · ${formatDirection("SHORT", "S")} · 结算即更新`;
}

function directionPulseShadowClass(shadow) {
  const directions = shadow && shadow.directions ? shadow.directions : {};
  const statuses = ["LONG", "SHORT"].flatMap((direction) =>
    ["12", "16"].map((window) => ((directions[direction] || {})[window] || {}).status),
  );
  if (statuses.includes("DEGRADED")) return "status-risk";
  if (statuses.includes("WATCH")) return "status-warn";
  if (statuses.some((status) => status === "NORMAL")) return "status-good";
  return "status-muted";
}

function fmtStakeProgression(progression) {
  if (!progression) return "两单叠加 · 状态数据不完整";
  const secondStake = optionalNum(progression.second_stake);
  const active = optionalNum(progression.active_second_orders);
  const maxActive = optionalNum(progression.max_active);
  const pending = optionalNum(progression.pending_credits);
  if (!progression.enabled) {
    if (active === null) return "两单叠加 · 状态数据不完整";
    if (active <= 0) return "两单叠加 OFF";
    if (secondStake === null) return "两单叠加 · 状态数据不完整";
    const amount = `${Number.isInteger(secondStake) ? secondStake.toFixed(0) : secondStake.toFixed(2)}U`;
    return `两单叠加 OFF · 未结${amount}订单 ${active}`;
  }
  if (secondStake === null || active === null || maxActive === null || pending === null) {
    return "两单叠加 · 状态数据不完整";
  }
  const stakeLabel = Number.isInteger(secondStake) ? secondStake.toFixed(0) : secondStake.toFixed(2);
  const byDirection = progression.by_direction;
  if (byDirection && byDirection.LONG && byDirection.SHORT) {
    const longActive = num(byDirection.LONG.active_second_orders);
    const shortActive = num(byDirection.SHORT.active_second_orders);
    const longMax = num(byDirection.LONG.max_active, maxActive);
    const shortMax = num(byDirection.SHORT.max_active, maxActive);
    const longPending = num(byDirection.LONG.pending_credits);
    const shortPending = num(byDirection.SHORT.pending_credits);
    return `两单叠加 · ${stakeLabel}U在途 L${longActive}/${longMax} S${shortActive}/${shortMax} · 待用 L${longPending} S${shortPending}`;
  }
  return `两单叠加 · ${stakeLabel}U订单 ${active}/${maxActive} · 待用资格 ${pending}`;
}

function fmtClock(hour, minute) {
  return `${String(num(hour)).padStart(2, "0")}:${String(num(minute)).padStart(2, "0")}`;
}

function renderStrategySummary(state) {
  const selection = state.daily_profile_selection || {};
  const config = selection.config || {};
  const maxOpenOrders = num(state.order_policy && state.order_policy.max_open_orders, 2);
  const selectionLabel = selection.enabled ? "每日画像选策" : "静态策略";
  setText(
    "strategy-summary",
    `币安现货 1分钟K线 · 仅10分钟事件合约 · ${selectionLabel} · 最多${maxOpenOrders}个未结订单`,
  );
  setText(
    "daily-profile-schedule-badge",
    selection.enabled
      ? `每日${fmtClock(config.evaluation_hour, config.evaluation_minute)}画像评估`
      : "每日画像选策 OFF",
  );
  setText("result-sequence-guard-badge", fmtResultSequenceGuard(state.result_sequence_guard));
  setText("wave-batch-guard-badge", fmtWaveBatchGuard(state.wave_batch_guard));
}

function hasRiskFlag(signal, flag) {
  return String(signal && signal.risk_flags ? signal.risk_flags : "")
    .split(",")
    .map((item) => item.trim())
    .includes(flag);
}

function fmtShortExtension(state) {
  const signal = state.selected_signal;
  const matched = hasRiskFlag(signal, "NORMAL_DOWN_SHORT_EXTENSION");
  if (matched) {
    return `${signal.threshold_segment} ACTIVE · ${fmtPct(signal.session_win_rate)} · EV ${fmtMoney(signal.session_ev)}`;
  }
  return "待触发 · 仅 WD-02/WD-23 量平价跌";
}

function fmtStrategyProfile(signal) {
  if (!signal) return DASH;
  return `${signal.strategy_family || "unknown"} · ${signal.strategy_tag || "unknown"} · ${signal.profile_key || DASH}`;
}

function fmtObservationProfile(summary) {
  const total = summary && summary.total ? summary.total : null;
  if (!total || !total.signals) return DASH;
  return `${total.settled || 0}结算 · 胜率${fmtPct(total.win_rate)} · EV ${fmtMoney(total.ev)}`;
}

function fmtOrderProfile(summary) {
  const total = summary && summary.total ? summary.total : null;
  if (!total || !total.orders) return DASH;
  const riskHints = Array.isArray(summary.risk_hints) ? summary.risk_hints.length : 0;
  return `${total.orders}单 · 胜率${fmtPct(total.win_rate)} · 弱点${riskHints}`;
}

function fmtProfileGuardShadow(state, summary) {
  const signal = state && state.selected_signal ? state.selected_signal : null;
  const guard = summary && summary.profile_guard ? summary.profile_guard : null;
  const recommended = guard ? guard.recommended_key_subset || guard.recommended_walk_forward || guard.walk_forward_combined : null;
  if (!signal) return DASH;
  if (!recommended) return "无画像";
  const activeKeys = new Set(recommended.final_active_keys || recommended.risk_keys || []);
  const hitKeys = currentRiskKeys(signal).filter((key) => activeKeys.has(key));
  const params = recommended.min_history || recommended.min_group_size
    ? `H${recommended.min_history || DASH}/G${recommended.min_group_size || DASH}`
    : DASH;
  const mode = state && state.profile_guard && state.profile_guard.enabled ? "正式" : "影子";
  return hitKeys.length ? `${mode}拦截 ${hitKeys.length}项 · ${params}` : `PASS · ${params}`;
}

function fmtReplayGuard(title, variant) {
  if (!variant || !variant.traded) return "";
  const traded = variant.traded || {};
  const blocked = variant.blocked || {};
  const delta = num(variant.delta_pnl);
  const params = (
    variant.min_history || variant.min_group_size
      ? ` · H${variant.min_history || DASH}/G${variant.min_group_size || DASH}`
      : ""
  );
  return `
    <span class="${delta >= 0 ? "profile-guard-good" : "profile-guard-risk"}">
      ${escapeHtml(title)}${escapeHtml(params)}
      · 交易${escapeHtml(traded.orders || 0)}
      · 胜率${escapeHtml(fmtPct(traded.win_rate))}
      · 盈亏${escapeHtml(fmtMoney(traded.pnl))}
      · 拦截${escapeHtml(blocked.orders || 0)}
      · 改善${escapeHtml(fmtMoney(delta))}
    </span>
  `;
}

function fmtKeySubsetGuard(title, variant) {
  if (!variant || !variant.traded) return "";
  const keys = variant.candidate_risk_keys || variant.allowed_risk_keys || variant.risk_keys || [];
  const labels = keys.map((key) => riskHintLabel(key)).join("、") || DASH;
  const training = variant.training || {};
  const validation = variant.validation || {};
  const policy = variant.selection_policy || {};
  const bothStable = Boolean(training.stable && validation.stable);
  const stableText = validation.orders
    ? `训练${training.stable ? "稳" : "弱"} / 验证${validation.stable ? "稳" : "弱"} · ${validation.orders}单 · 改善${fmtMoney(validation.delta_pnl)} · ${validation.reason || DASH}`
    : "验证待积累";
  const policyText = policy.name
    ? `稳定带 · ${policy.eligible || 0}/${policy.stable_candidates || 0}候选 · ${policy.reason || DASH}`
    : "稳定带待评估";
  return `
    ${fmtReplayGuard(title, variant)}
    <span class="${bothStable ? "profile-guard-good" : "risk-hit-neutral"}">
      ${escapeHtml(title)}稳定性 · ${escapeHtml(stableText)}
    </span>
    <span class="risk-hit-neutral">
      ${escapeHtml(title)}选择 · ${escapeHtml(policyText)}
    </span>
    <span class="risk-hit-neutral">
      ${escapeHtml(title)}key · ${escapeHtml(labels)}
    </span>
  `;
}

function warmupClass(warmup) {
  if (!warmup) return "status-risk";
  if (warmup.status === "READY") return "status-good";
  if (warmup.status === "PARTIAL") return "status-warn";
  return "status-risk";
}

function rollingEdgeClass(edge) {
  if (!edge || edge.status === "UNKNOWN") return "status-muted";
  return edge.status === "DEGRADED" ? "status-risk" : "status-good";
}

function resultSequenceGuardClass(guard) {
  if (!guard || !guard.enabled) return "";
  return guard.status === "PAUSED" ? "status-risk" : "status-good";
}

function waveStateClass(wave) {
  if (!wave || !wave.enabled || wave.state === "UNKNOWN") return "status-muted";
  return Array.isArray(wave.allowed_directions) && wave.allowed_directions.length
    ? "status-good"
    : "status-warn";
}

function waveBatchGuardClass(guard) {
  if (!guard || !guard.enabled) return "status-muted";
  if (guard.blocked || guard.mode === "COOLDOWN") return "status-risk";
  if (guard.mode === "RECOVERY") return "status-warn";
  return "status-good";
}

function profileDegradationGuardClass(guard) {
  if (!guard || guard.enabled !== true || !guard.status || guard.status === "DISABLED") {
    return "status-muted";
  }
  if (["NORMAL", "NOT_APPLICABLE"].includes(guard.status)) return "status-good";
  if (guard.status === "COOLDOWN") return "status-risk";
  if (["RECOVERY_READY", "RECOVERY_PENDING"].includes(guard.status)) return "status-warn";
  return "status-muted";
}

function webhookClass(webhook) {
  if (!webhook || !webhook.enabled) return "status-muted";
  return "status-good";
}

function shortExtensionClass(state) {
  return hasRiskFlag(state.selected_signal, "NORMAL_DOWN_SHORT_EXTENSION") ? "status-risk" : "status-muted";
}

function directionClass(direction) {
  return { LONG: "long", SHORT: "short" }[direction] || "wait";
}

function signalTone(direction) {
  return { LONG: "signal-long", SHORT: "signal-short" }[direction] || "signal-wait";
}

function sessionTone(signal) {
  if (!signal) return "";
  const scorePassed = Math.abs(num(signal.score)) >= num(signal.threshold);
  if (signal.session_allowed && scorePassed) return "session-allowed";
  if (!signal.session_allowed && scorePassed) return "session-blocked";
  return "";
}

function orderTone(order) {
  if (order.status === "OPEN") return "row-open";
  if (order.result === "WIN") return "row-win";
  if (order.result === "LOSS") return "row-loss";
  return "";
}

function observationActionLabel(action) {
  return {
    PROMOTE_WATCH: "重点观察",
    WATCH_UPSIDE: "优势观察",
    WATCH: "继续观察",
    WATCH_RISK: "风险观察",
    BLOCK_WATCH: "建议屏蔽",
    COLLECTING: "继续采样",
  }[action] || action || DASH;
}

function dailySelectionLabel(state) {
  return {
    ACTIVE: "今日启用",
    SELECTED: "待生效",
    RETAINED: "继续启用",
    RETAINED_DEGRADED: "退化保留",
    DEGRADED_EXIT: "退化退出",
    RANKED_OUT: "排名未入选",
    INSUFFICIENT_SAMPLES: "样本不足",
    LOW_WIN_RATE: "胜率不足",
    LOW_EV: "EV不足",
    NOT_EVALUATED: "尚未评估",
  }[state] || state || DASH;
}

function dailySelectionClass(state) {
  if (["ACTIVE", "SELECTED", "RETAINED"].includes(state)) return "status-good";
  if (["RETAINED_DEGRADED", "RANKED_OUT"].includes(state)) return "status-warn";
  if (["DEGRADED_EXIT", "LOW_WIN_RATE", "LOW_EV"].includes(state)) return "status-risk";
  return "status-muted";
}

function observationActionClass(action) {
  if (["PROMOTE_WATCH", "WATCH_UPSIDE"].includes(action)) return "status-good";
  if (["BLOCK_WATCH", "WATCH_RISK"].includes(action)) return "status-risk";
  if (action === "WATCH") return "status-warn";
  return "status-muted";
}

function riskHintLabel(key) {
  return {
    HIGH_RSI_REBOUND: "高RSI反抽",
    DUAL_UP_BIAS_REBOUND: "双周期偏多反抽",
    WEAK_SEGMENT_WD00_WD18_WD22: "弱时段 WD-00/18/22",
    LEVEL_A_REBOUND: "A级反抽",
    MID_POSITION_REBOUND: "中位反抽",
    SHALLOW_DROP_REBOUND: "浅跌反抽",
  }[key] || key || DASH;
}

function sampleWeakFlagToRiskKey(flag) {
  if (flag === "SAMPLE_WEAK_LEVEL_A_REBOUND") return "LEVEL_A_REBOUND";
  if (flag === "SAMPLE_WEAK_MID_POSITION_REBOUND") return "MID_POSITION_REBOUND";
  if (flag === "SAMPLE_WEAK_SHALLOW_DROP_REBOUND") return "SHALLOW_DROP_REBOUND";
  if (flag === "SAMPLE_WEAK_HIGH_RSI_REBOUND") return "HIGH_RSI_REBOUND";
  if (flag === "SAMPLE_WEAK_DUAL_UP_BIAS_REBOUND") return "DUAL_UP_BIAS_REBOUND";
  if (flag && flag.startsWith("SAMPLE_WEAK_SEGMENT_")) return "WEAK_SEGMENT_WD00_WD18_WD22";
  return "";
}

function currentRiskKeys(signal) {
  const flags = String(signal && signal.risk_flags ? signal.risk_flags : "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return [...new Set(flags.map(sampleWeakFlagToRiskKey).filter(Boolean))];
}

function metric(label, value) {
  return `<span>${escapeHtml(label)} ${escapeHtml(value)}</span>`;
}

function renderMetrics(signal, fields) {
  return `<div class="metrics">${fields.map(([label, getValue]) => metric(label, getValue(signal))).join("")}</div>`;
}

function renderSignalCard(signal, subtitle, fields, selected = false) {
  return `
    <article class="signal ${selected ? "selected" : ""} ${signalTone(signal.direction)} ${sessionTone(signal)}">
      <div class="signal-title">
        <strong class="${directionClass(signal.direction)}">${escapeHtml(signal.direction)}</strong>
        <span>${escapeHtml(subtitle)}</span>
      </div>
      <p>${escapeHtml(signal.reason)}</p>
      ${renderMetrics(signal, fields)}
    </article>
  `;
}

const selectedSignalFields = [
  ["点位", (s) => fmtPrice(s.price)],
  ["分析窗口", (s) => `${s.analysis_window_minutes}分钟`],
  ["阈值窗口", (s) => `${s.threshold_window_minutes}分钟`],
  ["评分/阈值", (s) => `${Math.abs(num(s.score)).toFixed(1)} / ${num(s.threshold).toFixed(1)}`],
  ["时段", (s) => s.threshold_segment],
  ["时段允许", (s) => (s.session_allowed ? "YES" : "NO")],
  ["样本", (s) => s.session_sample_size],
  ["时段胜率", (s) => fmtPct(s.session_win_rate)],
  ["时段EV", (s) => fmtMoney(s.session_ev)],
  ["最小边际", (s) => num(s.session_edge_min).toFixed(1)],
  ["量比", (s) => s.volume_ratio],
  ["放量阈值", (s) => s.volume_threshold],
  ["涨跌幅", (s) => `${(num(s.price_change_pct) * 100).toFixed(3)}%`],
  ["价格位置", (s) => s.price_position],
  ["收盘强度", (s) => s.close_strength],
  ["10m偏向", (s) => s.mtf_10m_bias],
  ["30m偏向", (s) => s.mtf_30m_bias],
  ["MACD柱", (s) => num(s.macd_histogram).toFixed(4)],
  ["MACD变化", (s) => num(s.macd_histogram_delta).toFixed(4)],
  ["RSI", (s) => num(s.rsi).toFixed(2)],
  ["BOLL位置", (s) => num(s.bollinger_position).toFixed(3)],
  ["指标画像", (s) => s.indicator_profile_segment],
  ["画像样本", (s) => s.indicator_profile_sample_size],
  ["RSI阈值", (s) => `${num(s.rsi_lower_threshold).toFixed(1)}-${num(s.rsi_upper_threshold).toFixed(1)}`],
  ["BOLL阈值", (s) => `${num(s.bollinger_lower_threshold).toFixed(3)}-${num(s.bollinger_upper_threshold).toFixed(3)}`],
  ["MACD阈值", (s) => `${num(s.macd_histogram_threshold).toFixed(4)} / ${num(s.macd_delta_threshold).toFixed(4)}`],
  ["F&G", (s) => `${s.fear_greed_value ?? DASH} ${s.fear_greed_classification || ""}`],
  ["F&G调整", (s) => `+${num(s.fear_greed_adjustment).toFixed(1)}`],
  ["行情状态", (s) => s.regime || "UNKNOWN"],
  ["风控标记", (s) => s.risk_flags || DASH],
  ["策略族", (s) => s.strategy_family || "unknown"],
  ["策略标签", (s) => s.strategy_tag || "unknown"],
  ["画像键", (s) => s.profile_key || DASH],
  ["1m波段", (s) => s.wave_state || "UNKNOWN"],
  ["波段确认", (s) => `${s.wave_confirmations || 0} / ${num(s.wave_efficiency).toFixed(3)}`],
  ["波段批次", (s) => s.wave_batch_id || DASH],
  ["波段守卫", (s) => `${s.wave_guard_status || "UNKNOWN"} · ${s.wave_guard_mode || "NORMAL"}`],
  ["守卫原因", (s) => s.wave_guard_reason || DASH],
  ["自适应画像", (s) => formatAdaptiveProfile(s.adaptive_profile_state)],
  ["价格结构", (s) => formatEntryStructure(s.entry_structure_shadow)],
];

const signalFields = [
  ["分数/阈值", (s) => `${Math.abs(num(s.score)).toFixed(1)} / ${num(s.threshold).toFixed(1)}`],
  ["窗口", (s) => `${s.analysis_window_minutes}分钟`],
  ["时段", (s) => s.threshold_segment],
  ["允许", (s) => (s.session_allowed ? "YES" : "NO")],
  ["胜率", (s) => fmtPct(s.session_win_rate)],
  ["EV", (s) => fmtMoney(s.session_ev)],
  ["边际", (s) => num(s.session_edge_min).toFixed(1)],
  ["点位", (s) => fmtPrice(s.price)],
  ["量比", (s) => s.volume_ratio],
  ["阈值", (s) => s.volume_threshold],
  ["位置", (s) => s.price_position],
  ["10m", (s) => s.mtf_10m_bias],
  ["30m", (s) => s.mtf_30m_bias],
  ["MACD", (s) => num(s.macd_histogram).toFixed(4)],
  ["RSI", (s) => num(s.rsi).toFixed(2)],
  ["BOLL", (s) => num(s.bollinger_position).toFixed(3)],
  ["画像", (s) => `${s.indicator_profile_segment} / ${s.indicator_profile_sample_size}`],
  ["F&G调整", (s) => `+${num(s.fear_greed_adjustment).toFixed(1)}`],
  ["状态", (s) => s.regime || "UNKNOWN"],
  ["策略", (s) => s.strategy_tag || "unknown"],
];

const summaryFields = {
  symbol: (state) => state.symbol,
  balance: (state) => fmtMoney(state.stats.balance),
  "win-rate": (state) => `${(num(state.stats.win_rate) * 100).toFixed(2)}%`,
  "today-balance": (state) => fmtMoney((state.stats.today || {}).pnl),
  "today-win-rate": (state) => `${(num((state.stats.today || {}).win_rate) * 100).toFixed(2)}%`,
  "profile-period-balance": (state) => {
    const period = state.stats.profile_period || {};
    return period.active ? fmtMoney(period.pnl) : DASH;
  },
  "profile-period-win-rate": (state) => {
    const period = state.stats.profile_period || {};
    return period.active ? `${(num(period.win_rate) * 100).toFixed(2)}%` : DASH;
  },
  "total-orders": (state) => `${state.stats.total_orders} / 未结 ${state.stats.open_orders}`,
  "updated-at": (state) => fmtTime(state.updated_at_ms),
  "fear-greed": (state) => fmtFearGreed(state.fear_greed),
  "warmup-status": (state) => fmtWarmup(state.warmup),
  regime: (state) => (state.selected_signal ? state.selected_signal.regime || "UNKNOWN" : DASH),
  "risk-pause": (state) => state.risk_pause || DASH,
  "rolling-edge-status": (state) => fmtRollingEdge(state.rolling_edge),
  "wave-state-status": (state) => fmtWaveState(state.wave_state),
  "wave-batch-guard-status": (state) => fmtWaveBatchGuard(state.wave_batch_guard),
  "profile-degradation-guard-status": (state) => fmtProfileDegradationGuard(state.profile_degradation_guard),
  "direction-pulse-shadow-status": (state) => fmtDirectionPulseShadow(state.direction_pulse_shadow),
  "shadow-optimizer-status": (state) => fmtShadowOptimizer(state.shadow_optimizer),
  "result-sequence-guard-status": (state) => fmtResultSequenceGuard(state.result_sequence_guard),
  "webhook-status": (state) => fmtWebhook(state.webhook),
  "order-decision": (state) => state.order_decision || DASH,
  "score-threshold": (state) => (
    state.selected_signal
      ? `${Math.abs(num(state.selected_signal.score)).toFixed(1)} / ${num(state.selected_signal.threshold).toFixed(1)}`
      : DASH
  ),
  "session-profile": (state) => (
    state.selected_signal
      ? `${state.selected_signal.timeframe_minutes}m ${state.selected_signal.threshold_segment} ${fmtPct(state.selected_signal.session_win_rate)} EV ${fmtMoney(state.selected_signal.session_ev)}`
      : DASH
  ),
  "short-extension-status": (state) => fmtShortExtension(state),
  "strategy-profile": (state) => fmtStrategyProfile(state.selected_signal),
  "daily-profile-status": (state) => {
    const selection = state.daily_profile_selection || {};
    if (!selection.enabled) return "已关闭";
    return `${selection.status || "PENDING"} · ${selection.selected_count || 0}个画像`;
  },
  "profile-guard-shadow": (state) => fmtProfileGuardShadow(state, lastOrderProfile),
};

const filterIds = ["direction-filter", "level-filter", "segment-filter", "result-filter"];
const observationFilterIds = [
  "obs-direction-filter",
  "obs-family-filter",
  "obs-tag-filter",
  "obs-segment-filter",
  "obs-result-filter",
  "obs-structure-state-filter",
  "obs-structure-bias-filter",
  "obs-origin-filter",
];

function setText(id, value) {
  $(id).textContent = value ?? DASH;
}

function renderSelectedSignal(signal, decision) {
  $("selected-signal").innerHTML = signal
    ? renderSignalCard(signal, `选择周期 ${signal.timeframe_minutes}分钟 · ${signal.level} · 决策 ${decision}`, selectedSignalFields, true)
    : "";
}

function orderCalculatedThreshold(order) {
  const calculated = num(order.calculated_threshold);
  return calculated > 0 ? calculated : num(order.threshold);
}

function renderOrders(orders) {
  $("orders").innerHTML = orders.length ? orders.map((order) => `
    <tr class="${orderTone(order)}">
      <td>${escapeHtml(order.id)}</td>
      <td class="${directionClass(order.direction)}">${escapeHtml(order.direction)}</td>
      <td>${escapeHtml(order.timeframe_minutes)}分钟</td>
      <td>${escapeHtml(order.level)}</td>
      <td>${escapeHtml(order.threshold_segment || DASH)}</td>
      <td>${escapeHtml(order.strategy_tag || "unknown")}<br><span>${escapeHtml(order.strategy_family || "unknown")}</span><br><span>${escapeHtml(order.wave_state || "UNKNOWN")} · ${escapeHtml(order.wave_guard_status || "UNKNOWN")} · ${escapeHtml(order.wave_guard_mode || "NORMAL")}</span>${order.profile_degradation_probe === true ? '<br><span class="order-probe-label">基础试探</span>' : ""}</td>
      <td>${escapeHtml(fmtPct(order.session_win_rate))} / ${escapeHtml(fmtMoney(order.session_ev))}</td>
      <td>${escapeHtml(num(order.threshold).toFixed(1))}<br><span>评分 ${escapeHtml(Math.abs(num(order.score)).toFixed(1))} · 原始 ${escapeHtml(orderCalculatedThreshold(order).toFixed(1))}</span></td>
      <td data-column="entry-structure" class="diagnostic-cell">${escapeHtml(formatEntryStructure(order.entry_structure_shadow))}</td>
      <td>${escapeHtml(fmtMoney(order.stake))}</td>
      <td>${escapeHtml(fmtPrice(order.entry_price))}</td>
      <td>${escapeHtml(fmtTime(order.opened_at))}</td>
      <td>${escapeHtml(fmtPrice(order.exit_price))}</td>
      <td>${escapeHtml(fmtTime(order.settled_at))}</td>
      <td>${escapeHtml(order.status)}</td>
      <td>${escapeHtml(order.result || DASH)}</td>
      <td class="${num(order.pnl) >= 0 ? "long" : "short"}">${escapeHtml(order.status === "SETTLED" ? fmtMoney(order.pnl) : DASH)}</td>
      <td class="reason">${escapeHtml(order.reason)}<br><span>状态 ${escapeHtml(order.regime || "UNKNOWN")}</span></td>
    </tr>
  `).join("") : `<tr><td colspan="18" class="empty-row">没有符合筛选条件的订单</td></tr>`;
}

function renderObservations(observations) {
  $("observations").innerHTML = observations.length ? observations.map((item) => `
    <tr class="${orderTone(item)}">
      <td class="${directionClass(item.direction)}">${escapeHtml(item.direction)}</td>
      <td>${escapeHtml(item.strategy_family || "unknown")}</td>
      <td>${escapeHtml(item.strategy_tag || "unknown")}</td>
      <td>${escapeHtml(item.timeframe_minutes)}分钟</td>
      <td>${escapeHtml(item.threshold_segment || DASH)}</td>
      <td>${escapeHtml(num(item.edge).toFixed(1))}</td>
      <td>${escapeHtml(fmtPrice(item.entry_price))}</td>
      <td>${escapeHtml(fmtTime(item.opened_at))}</td>
      <td>${escapeHtml(fmtPrice(item.exit_price))}</td>
      <td>${escapeHtml(fmtTime(item.settled_at))}</td>
      <td>${escapeHtml(item.status)}</td>
      <td>${escapeHtml(item.result || DASH)}</td>
      <td class="${num(item.pnl) >= 0 ? "long" : "short"}">${escapeHtml(item.status === "SETTLED" ? fmtMoney(item.pnl) : DASH)}</td>
      <td class="reason">${escapeHtml(item.reason)}<br><span>${escapeHtml(item.source_decision || "OBSERVE")}</span><br><span>画像 ${escapeHtml(formatAdaptiveProfile(item.adaptive_profile_state))} · 结构 ${escapeHtml(formatEntryStructure(item.entry_structure_shadow || item))}</span></td>
    </tr>
  `).join("") : `<tr><td colspan="14" class="empty-row">没有符合筛选条件的观察信号</td></tr>`;
}

function renderObservationSummary(summary) {
  lastObservationSummary = summary;
  const groups = summary && Array.isArray(summary.groups) ? summary.groups : [];
  $("observation-summary").innerHTML = groups.length ? groups.map((item) => `
    <tr>
      <td>${escapeHtml(item.strategy_family || "unknown")}</td>
      <td>${escapeHtml(item.strategy_tag || "unknown")}</td>
      <td class="${directionClass(item.direction)}">${escapeHtml(item.direction || DASH)}</td>
      <td>${escapeHtml(item.timeframe_minutes || DASH)}分钟</td>
      <td>${escapeHtml(item.threshold_segment || DASH)}</td>
      <td>${escapeHtml(item.signals || 0)} / 未结 ${escapeHtml(item.open || 0)}</td>
      <td>${escapeHtml(item.settled || 0)}</td>
      <td>${escapeHtml(fmtPct(item.win_rate))}</td>
      <td>${escapeHtml(fmtMoney(item.ev))}</td>
      <td class="${num(item.pnl) >= 0 ? "long" : "short"}">${escapeHtml(fmtMoney(item.pnl))}</td>
      <td class="${observationActionClass(item.action)}">${escapeHtml(observationActionLabel(item.action))}</td>
      <td class="${dailySelectionClass(item.selection_state)}" title="${escapeHtml(item.selection_reason || "")}">${escapeHtml(dailySelectionLabel(item.selection_state))}</td>
      <td>${escapeHtml(item.confidence || DASH)}</td>
      <td>${escapeHtml(fmtTime(item.last_opened_at))}</td>
    </tr>
  `).join("") : `<tr><td colspan="14" class="empty-row">暂无观察画像统计</td></tr>`;

  $("observation-profile").textContent = fmtObservationProfile(summary);
  renderObservationSummaryInfo(summary);
}

function renderObservationSummaryInfo(summary) {
  const total = summary && summary.total ? summary.total : {};
  const actionCounts = summary && summary.action_counts ? summary.action_counts : {};
  const windowLabel = summary && summary.window ? summary.window : ($("obs-window-filter").value || "14d");
  $("observation-summary-info").textContent = (
    `窗口 ${windowLabel} · 订单画像缓存 ${profileCacheStatusLabel(lastOrderProfile)} · `
    + `信号 ${total.signals || 0} · 结算 ${total.settled || 0} · `
    + `胜率 ${fmtPct(total.win_rate)} · EV ${fmtMoney(total.ev)} · `
    + `重点 ${actionCounts.PROMOTE_WATCH || 0} / 风险 ${actionCounts.BLOCK_WATCH || 0}`
  );
}

function renderDailyProfileSelection(selection) {
  const current = selection || {};
  const active = Array.isArray(current.selected_profiles) ? current.selected_profiles : [];
  const pending = Array.isArray(current.pending_profiles) ? current.pending_profiles : [];
  const displayed = active.length ? active : pending;
  const prefix = active.length ? "生效" : (pending.length ? "待08:00生效" : "无启用画像");
  $("daily-profile-window").textContent = current.effective_from
    ? `${prefix} · ${fmtTime(current.effective_from)} 至 ${fmtTime(current.effective_until)}`
    : (current.reason || DASH);
  $("daily-profile-list").innerHTML = displayed.length ? displayed.map((item, index) => `
    <div class="daily-profile-row">
      <span class="daily-profile-rank">${escapeHtml(index + 1)}</span>
      <strong class="${directionClass(item.direction)}">${escapeHtml(item.direction || DASH)}</strong>
      <span>${escapeHtml(item.threshold_segment || DASH)}</span>
      <span class="daily-profile-strategy">${escapeHtml(item.strategy_tag || item.strategy_family || "unknown")}</span>
      <span>N ${escapeHtml(item.sample_size || 0)}</span>
      <span>胜率 ${escapeHtml(fmtPct(item.win_rate))}</span>
      <span>EV ${escapeHtml(fmtMoney(item.ev))}</span>
      <span class="${dailySelectionClass(active.length ? "ACTIVE" : item.selection_state)}">${escapeHtml(dailySelectionLabel(active.length ? "ACTIVE" : (item.selection_state || "SELECTED")))}</span>
    </div>
  `).join("") : `<div class="daily-profile-empty">${escapeHtml(current.reason || "当前没有达到启用条件的画像")}</div>`;
}

function profileCacheStatusLabel(summary) {
  const labels = {
    PREPARING: "准备中",
    STALE: "已陈旧",
    READY: "已就绪",
  };
  const status = summary && summary.cache_status ? summary.cache_status : "PREPARING";
  const source = summary && summary.source_revision !== null
    && summary.source_revision !== undefined
    ? summary.source_revision
    : DASH;
  const current = summary && summary.current_revision !== null
    && summary.current_revision !== undefined
    ? summary.current_revision
    : DASH;
  return `${labels[status] || status} · 摘要版本 ${source} / 当前版本 ${current}`;
}

function renderOrderProfile(summary) {
  lastOrderProfile = summary;
  const hints = summary && Array.isArray(summary.risk_hints) ? summary.risk_hints : [];
  const cacheStatus = summary && summary.cache_status ? summary.cache_status : "PREPARING";
  const emptyMessage = cacheStatus === "PREPARING"
    ? "订单弱点画像正在准备"
    : (cacheStatus === "STALE" ? "当前显示旧版本画像，后台正在更新" : "暂无订单弱点画像");
  $("order-profile-summary").innerHTML = hints.length ? hints.map((item) => `
    <tr class="${num(item.ev) < 0 ? "row-loss" : "row-win"}">
      <td>${escapeHtml(riskHintLabel(item.key))}<br><span>${escapeHtml(item.key || DASH)}</span></td>
      <td>${escapeHtml(item.orders || 0)}</td>
      <td>${escapeHtml(fmtPct(item.win_rate))}</td>
      <td>${escapeHtml(fmtMoney(item.ev))}</td>
      <td class="${num(item.pnl) >= 0 ? "long" : "short"}">${escapeHtml(fmtMoney(item.pnl))}</td>
    </tr>
  `).join("") : `<tr><td colspan="5" class="empty-row">${escapeHtml(emptyMessage)}</td></tr>`;

  const total = summary && summary.total ? summary.total : {};
  $("order-profile").textContent = fmtOrderProfile(summary);
  $("order-profile-info").textContent = (
    `${profileCacheStatusLabel(summary)} · 订单 ${total.orders || 0} · 胜率 ${fmtPct(total.win_rate)} · `
    + `EV ${fmtMoney(total.ev)} · 弱点 ${hints.length}`
  );
  renderProfileGuard(summary && summary.profile_guard ? summary.profile_guard : null);
  renderProfileGuardShadowSummary(summary && summary.profile_guard_shadow ? summary.profile_guard_shadow : null);
  renderProfileGuardPolicySummary(summary && summary.profile_guard_policy ? summary.profile_guard_policy : null);
  renderProfileGuardCompareSummary(
    summary && summary.profile_guard_shadow_compare ? summary.profile_guard_shadow_compare : null
  );
  renderCurrentRiskProfile(lastState, summary);
  if (lastObservationSummary) renderObservationSummaryInfo(lastObservationSummary);
}

function renderProfileGuard(guard) {
  if (!guard || !guard.baseline) {
    $("profile-guard-summary").innerHTML = `<span class="risk-hit-neutral">暂无画像守卫回放</span>`;
    return;
  }
  const baseline = guard.baseline || {};
  const replayUpgrade = guard.replay_upgrade || {};
  const walkForward = guard.walk_forward_combined || {};
  const contribution = Array.isArray(walkForward.blocked_key_contribution)
    ? walkForward.blocked_key_contribution.slice(0, 3)
    : [];
  const upgradeClass = replayUpgrade.action === "READY_TO_BLOCK"
    ? "profile-guard-good"
    : (replayUpgrade.action === "KEEP_OBSERVING" ? "profile-guard-risk" : "profile-guard-base");
  $("profile-guard-summary").innerHTML = `
    <span class="profile-guard-base">
      基准滚单 · 交易${escapeHtml(baseline.orders || 0)}
      · 胜率${escapeHtml(fmtPct(baseline.win_rate))}
      · 盈亏${escapeHtml(fmtMoney(baseline.pnl))}
      · EV ${escapeHtml(fmtMoney(baseline.ev))}
    </span>
    ${fmtReplayGuard("静态组合", guard.static_combined)}
    ${fmtReplayGuard("默认滚动", guard.walk_forward_combined)}
    ${fmtReplayGuard("推荐滚动", guard.recommended_walk_forward)}
    ${fmtKeySubsetGuard("候选子集", guard.recommended_key_subset)}
    <span class="${upgradeClass}">
      回放升级建议 · ${escapeHtml(replayUpgrade.action || DASH)}
      · ${escapeHtml(replayUpgrade.confidence || DASH)}
      · ${escapeHtml(replayUpgrade.reason || DASH)}
    </span>
    ${contribution.map((item) => `
      <span class="${num(item.ev) < 0 ? "profile-guard-good" : "profile-guard-risk"}">
        回放拦截贡献 · ${escapeHtml(riskHintLabel(item.key))}
        · ${escapeHtml(item.orders || 0)}单
        · 胜率${escapeHtml(fmtPct(item.win_rate))}
        · EV ${escapeHtml(fmtMoney(item.ev))}
      </span>
    `).join("")}
    <span class="risk-hit-neutral">静态仅诊断，推荐滚动来自参数扫描</span>
  `;
}

function renderProfileGuardShadowSummary(shadow) {
  const observed = shadow && shadow.observed ? shadow.observed : {};
  const wouldBlock = shadow && shadow.would_block ? shadow.would_block : {};
  const passed = shadow && shadow.pass ? shadow.pass : {};
  const upgrade = shadow && shadow.upgrade ? shadow.upgrade : {};
  if (!observed.orders) {
    const reason = upgrade.reason ? ` · ${upgrade.reason}` : "";
    $("profile-guard-shadow-summary").innerHTML = `<span class="risk-hit-neutral">影子守卫历史：待积累新订单样本${escapeHtml(reason)}</span>`;
    return;
  }
  const upgradeClass = upgrade.action === "READY_TO_BLOCK"
    ? "profile-guard-good"
    : (upgrade.action === "KEEP_OBSERVING" ? "profile-guard-risk" : "profile-guard-base");
  $("profile-guard-shadow-summary").innerHTML = `
    <span class="profile-guard-base">
      影子历史 · 样本${escapeHtml(observed.orders || 0)}
      · 覆盖${escapeHtml(fmtPct(shadow.coverage))}
    </span>
    <span class="${num(wouldBlock.ev) < 0 ? "profile-guard-good" : "profile-guard-risk"}">
      影子会拦 · ${escapeHtml(wouldBlock.orders || 0)}单
      · 胜率${escapeHtml(fmtPct(wouldBlock.win_rate))}
      · EV ${escapeHtml(fmtMoney(wouldBlock.ev))}
      · 盈亏${escapeHtml(fmtMoney(wouldBlock.pnl))}
    </span>
    <span class="${num(passed.ev) >= 0 ? "profile-guard-good" : "profile-guard-risk"}">
      影子放行 · ${escapeHtml(passed.orders || 0)}单
      · 胜率${escapeHtml(fmtPct(passed.win_rate))}
      · EV ${escapeHtml(fmtMoney(passed.ev))}
      · 盈亏${escapeHtml(fmtMoney(passed.pnl))}
    </span>
    <span class="${upgradeClass}">
      升级建议 · ${escapeHtml(upgrade.action || DASH)}
      · ${escapeHtml(upgrade.confidence || DASH)}
      · ${escapeHtml(upgrade.reason || DASH)}
    </span>
  `;
}

function renderProfileGuardPolicySummary(policy) {
  const byPolicy = policy && Array.isArray(policy.by_policy) ? policy.by_policy : [];
  const bySelectedKey = policy && Array.isArray(policy.by_selected_key) ? policy.by_selected_key : [];
  if (!byPolicy.length && !bySelectedKey.length) {
    $("profile-guard-policy-summary").innerHTML = `<span class="risk-hit-neutral">策略版本表现：待新订单样本记录</span>`;
    return;
  }
  const policyItems = byPolicy.slice(0, 4).map((item) => `
    <span class="${num(item.ev) >= 0 ? "profile-guard-good" : "profile-guard-risk"}">
      策略版本 · ${escapeHtml(item.key || DASH)}
      · ${escapeHtml(item.orders || 0)}单
      · 胜率${escapeHtml(fmtPct(item.win_rate))}
      · EV ${escapeHtml(fmtMoney(item.ev))}
      · 盈亏${escapeHtml(fmtMoney(item.pnl))}
    </span>
  `).join("");
  const keyItems = bySelectedKey.slice(0, 6).map((item) => `
    <span class="${num(item.ev) >= 0 ? "risk-hit-neutral" : "risk-hit-bad"}">
      选中key · ${escapeHtml(riskHintLabel(item.key))}
      · ${escapeHtml(item.orders || 0)}单
      · 胜率${escapeHtml(fmtPct(item.win_rate))}
      · EV ${escapeHtml(fmtMoney(item.ev))}
    </span>
  `).join("");
  $("profile-guard-policy-summary").innerHTML = `${policyItems}${keyItems}`;
}

function fmtGuardCompareItem(label, item, goodWhenNegative = true) {
  const ev = num(item && item.ev);
  const tone = goodWhenNegative ? (ev < 0 ? "profile-guard-good" : "profile-guard-risk") : (ev >= 0 ? "profile-guard-good" : "profile-guard-risk");
  return `
    <span class="${tone}">
      ${escapeHtml(label)}
      · ${escapeHtml(item && item.orders || 0)}单
      · 胜率${escapeHtml(fmtPct(item && item.win_rate))}
      · EV ${escapeHtml(fmtMoney(item && item.ev))}
      · 盈亏${escapeHtml(fmtMoney(item && item.pnl))}
    </span>
  `;
}

function renderProfileGuardCompareSummary(compare) {
  const observed = compare && compare.observed ? compare.observed : {};
  if (!observed.orders) {
    $("profile-guard-compare-summary").innerHTML = `<span class="risk-hit-neutral">守卫对照：待新订单同时记录推荐/默认影子</span>`;
    return;
  }
  const upgrade = compare && compare.upgrade ? compare.upgrade : {};
  const upgradeAction = upgrade.action || DASH;
  const upgradeClass = upgradeAction === "PROMOTE_RECOMMENDED_GUARD"
    ? "profile-guard-good"
    : (upgradeAction === "KEEP_DEFAULT_GUARD" ? "profile-guard-risk" : "risk-hit-neutral");
  $("profile-guard-compare-summary").innerHTML = `
    <span class="profile-guard-base">
      守卫对照 · 样本${escapeHtml(observed.orders || 0)}
      · 覆盖${escapeHtml(fmtPct(compare.coverage))}
    </span>
    <span class="${upgradeClass}">
      对照升级建议 · ${escapeHtml(upgradeAction)}
      · ${escapeHtml(upgrade.confidence || DASH)}
      · ${escapeHtml(upgrade.reason || "")}
    </span>
    ${fmtGuardCompareItem("推荐会拦", compare.recommended_block)}
    ${fmtGuardCompareItem("默认会拦", compare.default_block)}
    ${fmtGuardCompareItem("仅推荐会拦", compare.recommended_block_default_pass)}
    ${fmtGuardCompareItem("仅默认会拦", compare.recommended_pass_default_block)}
  `;
}

function renderCurrentRiskProfile(state, summary) {
  const signal = state && state.selected_signal ? state.selected_signal : null;
  const keys = currentRiskKeys(signal);
  const hints = summary && Array.isArray(summary.risk_hints) ? summary.risk_hints : [];
  const byKey = Object.fromEntries(hints.map((item) => [item.key, item]));
  $("current-risk-profile").textContent = keys.length ? `${keys.length}项命中` : "未命中";
  setText("profile-guard-shadow", fmtProfileGuardShadow(state, summary));
  $("current-risk-hits").innerHTML = keys.length ? keys.map((key) => {
    const item = byKey[key] || { key, orders: 0, win_rate: 0, ev: 0, pnl: 0 };
    return `
      <span class="${num(item.ev) < 0 ? "risk-hit-bad" : "risk-hit-neutral"}">
        ${escapeHtml(riskHintLabel(key))}
        · ${escapeHtml(item.orders || 0)}单
        · 胜率${escapeHtml(fmtPct(item.win_rate))}
        · EV ${escapeHtml(fmtMoney(item.ev))}
      </span>
    `;
  }).join("") : `<span class="risk-hit-neutral">当前信号未命中历史弱点画像</span>`;
}

function selectLabel(type, value) {
  if (type === "result" && value === "OPEN") return "未结";
  if (type === "result" && value === "WIN") return "赢";
  if (type === "result" && value === "LOSS") return "亏";
  return value;
}

function fillFilter(id, options, type) {
  const select = $(id);
  const current = select.value;
  select.innerHTML = `<option value="">全部</option>` + options
    .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(selectLabel(type, value))}</option>`)
    .join("");
  if (options.includes(current)) {
    select.value = current;
  }
}

function applyFilterOptions(options) {
  if (!options) return;
  lastFilterOptions = options;
  fillFilter("direction-filter", options.direction || [], "direction");
  fillFilter("level-filter", options.level || [], "level");
  fillFilter("segment-filter", options.segment || [], "segment");
  fillFilter("result-filter", options.result || [], "result");
}

function applyObservationFilterOptions(options) {
  if (!options) return;
  lastObservationFilterOptions = options;
  fillFilter("obs-direction-filter", options.direction || [], "direction");
  fillFilter("obs-family-filter", options.family || [], "family");
  fillFilter("obs-tag-filter", options.tag || [], "tag");
  fillFilter("obs-segment-filter", options.segment || [], "segment");
  fillFilter("obs-result-filter", options.result || [], "result");
  fillFilter("obs-structure-state-filter", options.entry_structure_state || [], "entry_structure_state");
  fillFilter("obs-structure-bias-filter", options.entry_structure_bias || [], "entry_structure_bias");
  fillFilter("obs-origin-filter", options.candidate_origin || options.origin || [], "candidate_origin");
}

function orderQuery() {
  const params = new URLSearchParams({
    page: String(ordersPage),
    page_size: $("page-size-filter").value,
  });
  for (const id of filterIds) {
    const value = $(id).value;
    if (value) params.set(id.replace("-filter", "").replace("direction", "direction").replace("level", "level").replace("segment", "segment").replace("result", "result"), value);
  }
  return params.toString();
}

function observationQuery() {
  const params = new URLSearchParams({
    page: String(observationsPage),
    page_size: $("obs-page-size-filter").value,
  });
  const names = {
    "obs-direction-filter": "direction",
    "obs-family-filter": "family",
    "obs-tag-filter": "tag",
    "obs-segment-filter": "segment",
    "obs-result-filter": "result",
    "obs-structure-state-filter": "entry_structure_state",
    "obs-structure-bias-filter": "entry_structure_bias",
    "obs-origin-filter": "candidate_origin",
  };
  for (const id of observationFilterIds) {
    const value = $(id).value;
    if (value) params.set(names[id], value);
  }
  return params.toString();
}

function observationSummaryQuery() {
  return new URLSearchParams({ window: $("obs-window-filter").value || "14d" }).toString();
}

async function loadOrders() {
  const response = await fetch(`/api/orders?${orderQuery()}`);
  const page = await response.json();
  ordersPage = page.page;
  ordersTotalPages = page.total_pages;
  renderOrders(page.orders || []);
  applyFilterOptions(page.filter_options);
  $("orders-page-info").textContent = `共 ${page.total} 条`;
  $("page-status").textContent = `第 ${page.page} / ${page.total_pages} 页 · 每页 ${page.page_size} 条`;
  $("prev-page").disabled = page.page <= 1;
  $("next-page").disabled = page.page >= page.total_pages;
}

async function loadObservations() {
  const response = await fetch(`/api/observations?${observationQuery()}`);
  const page = await response.json();
  observationsPage = page.page;
  observationsTotalPages = page.total_pages;
  renderObservations(page.observations || []);
  applyObservationFilterOptions(page.filter_options);
  $("observations-page-info").textContent = `共 ${page.total} 条`;
  $("obs-page-status").textContent = `第 ${page.page} / ${page.total_pages} 页 · 每页 ${page.page_size} 条`;
  $("obs-prev-page").disabled = page.page <= 1;
  $("obs-next-page").disabled = page.page >= page.total_pages;
}

async function loadObservationSummary() {
  const response = await fetch(`/api/observation-summary?${observationSummaryQuery()}`);
  const summary = await response.json();
  renderObservationSummary(summary);
}

async function loadOrderProfile() {
  const response = await fetch("/api/order-profile");
  const summary = await response.json();
  lastOrderProfile = summary;
  renderOrderProfile(summary);
}

async function loadState() {
  if (stateRequestInFlight) return;
  stateRequestInFlight = true;
  const requestedRevision = symbolRevision;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const state = await response.json();
    if (requestedRevision !== symbolRevision) return;
    currentSymbol = state.symbol;
    lastState = state;
    Object.entries(summaryFields).forEach(([id, getValue]) => setText(id, getValue(state)));
    $("symbol-input").value = state.symbol;
    $("webhook-status").className = webhookClass(state.webhook);
    $("warmup-status").className = warmupClass(state.warmup);
    $("rolling-edge-status").className = rollingEdgeClass(state.rolling_edge);
    $("wave-state-status").className = waveStateClass(state.wave_state);
    $("wave-batch-guard-status").className = waveBatchGuardClass(state.wave_batch_guard);
    $("profile-degradation-guard-status").className = profileDegradationGuardClass(state.profile_degradation_guard);
    $("direction-pulse-shadow-status").className = directionPulseShadowClass(state.direction_pulse_shadow);
    $("result-sequence-guard-status").className = resultSequenceGuardClass(state.result_sequence_guard);
    $("short-extension-status").className = shortExtensionClass(state);
    setText("stake-progression-badge", fmtStakeProgression(state.stake_progression));
    renderStrategySummary(state);
    $("last-error").textContent = state.last_error || "";
    renderSelectedSignal(state.selected_signal, state.order_decision);
    renderDailyProfileSelection(state.daily_profile_selection);
    renderCurrentRiskProfile(state, lastOrderProfile);
    if (lastFilterOptions === null) {
      await loadOrders();
    }
    if (lastObservationFilterOptions === null) {
      await loadObservations();
    }
  } catch (_error) {
    return;
  } finally {
    stateRequestInFlight = false;
  }
}

async function loadPrice() {
  if (priceRequestInFlight) return;
  priceRequestInFlight = true;
  const requestedSymbol = currentSymbol;
  const requestedRevision = symbolRevision;
  try {
    const response = await fetch("/api/price", { cache: "no-store" });
    const price = await response.json();
    if (requestedRevision !== symbolRevision || price.symbol !== requestedSymbol) return;
    setText("price", fmtPrice(price.latest_price));
  } catch (_error) {
    return;
  } finally {
    priceRequestInFlight = false;
  }
}

$("symbol-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const symbol = $("symbol-input").value.trim().toUpperCase();
  if (!symbol) return;
  await fetch(`/api/config?symbol=${encodeURIComponent(symbol)}`);
  currentSymbol = symbol;
  symbolRevision += 1;
  ordersPage = 1;
  observationsPage = 1;
  lastFilterOptions = null;
  lastObservationFilterOptions = null;
  await loadState();
  await loadPrice();
  await loadOrders();
  await loadObservations();
  await loadObservationSummary();
  await loadOrderProfile();
});

for (const id of [...filterIds, "page-size-filter"]) {
  $(id).addEventListener("change", async () => {
    ordersPage = 1;
    await loadOrders();
  });
}

for (const id of [...observationFilterIds, "obs-page-size-filter"]) {
  $(id).addEventListener("change", async () => {
    observationsPage = 1;
    await loadObservations();
  });
}

$("obs-window-filter").addEventListener("change", loadObservationSummary);

$("prev-page").addEventListener("click", async () => {
  ordersPage = Math.max(1, ordersPage - 1);
  await loadOrders();
});

$("next-page").addEventListener("click", async () => {
  ordersPage = Math.min(ordersTotalPages, ordersPage + 1);
  await loadOrders();
});

$("obs-prev-page").addEventListener("click", async () => {
  observationsPage = Math.max(1, observationsPage - 1);
  await loadObservations();
});

$("obs-next-page").addEventListener("click", async () => {
  observationsPage = Math.min(observationsTotalPages, observationsPage + 1);
  await loadObservations();
});

loadState();
loadPrice();
loadOrders();
loadObservations();
loadObservationSummary();
loadOrderProfile();
setInterval(loadState, 3000);
setInterval(loadPrice, 1000);
setInterval(loadOrders, 10000);
setInterval(loadObservations, 10000);
setInterval(loadObservationSummary, 10000);
setInterval(loadOrderProfile, 10000);
