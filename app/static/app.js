const $ = (id) => document.getElementById(id);
const DASH = "-";
let currentSymbol = "BTCUSDT";
let ordersPage = 1;
let ordersTotalPages = 1;
let lastFilterOptions = null;

function num(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
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
  if (webhook.last_error) return `ERROR · ${webhook.last_error}`;
  if (webhook.last_sent_at_ms) return `ON · ${fmtTime(webhook.last_sent_at_ms)}`;
  return "ON";
}

function fmtRollingEdge(edge) {
  if (!edge || edge.status === "UNKNOWN") return DASH;
  const mode = edge.observe_only ? "观察" : "守卫";
  return `${mode} ${edge.status} · ${edge.sample_size}样本 · 胜率${fmtPct(edge.win_rate)} · EV ${fmtMoney(edge.ev)}`;
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

function webhookClass(webhook) {
  if (!webhook || !webhook.enabled) return "status-muted";
  return webhook.last_error ? "status-risk" : "status-good";
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
];

const summaryFields = {
  symbol: (state) => state.symbol,
  price: (state) => fmtPrice(state.latest_price),
  balance: (state) => fmtMoney(state.stats.balance),
  "win-rate": (state) => `${(num(state.stats.win_rate) * 100).toFixed(2)}%`,
  "total-orders": (state) => `${state.stats.total_orders} / 未结 ${state.stats.open_orders}`,
  "updated-at": (state) => fmtTime(state.updated_at_ms),
  "fear-greed": (state) => fmtFearGreed(state.fear_greed),
  "warmup-status": (state) => fmtWarmup(state.warmup),
  regime: (state) => (state.selected_signal ? state.selected_signal.regime || "UNKNOWN" : DASH),
  "risk-pause": (state) => state.risk_pause || DASH,
  "rolling-edge-status": (state) => fmtRollingEdge(state.rolling_edge),
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
};

const filterIds = ["direction-filter", "level-filter", "segment-filter", "result-filter"];

function setText(id, value) {
  $(id).textContent = value ?? DASH;
}

function renderSelectedSignal(signal, decision) {
  $("selected-signal").innerHTML = signal
    ? renderSignalCard(signal, `选择周期 ${signal.timeframe_minutes}分钟 · ${signal.level} · 决策 ${decision}`, selectedSignalFields, true)
    : "";
}

function renderOrders(orders) {
  $("orders").innerHTML = orders.length ? orders.map((order) => `
    <tr class="${orderTone(order)}">
      <td>${escapeHtml(order.id)}</td>
      <td class="${directionClass(order.direction)}">${escapeHtml(order.direction)}</td>
      <td>${escapeHtml(order.timeframe_minutes)}分钟</td>
      <td>${escapeHtml(order.level)}</td>
      <td>${escapeHtml(order.threshold_segment || DASH)}</td>
      <td>${fmtPct(order.session_win_rate)} / ${fmtMoney(order.session_ev)}</td>
      <td>${fmtMoney(order.stake)}</td>
      <td>${fmtPrice(order.entry_price)}</td>
      <td>${fmtTime(order.opened_at)}</td>
      <td>${fmtPrice(order.exit_price)}</td>
      <td>${fmtTime(order.settled_at)}</td>
      <td>${escapeHtml(order.status)}</td>
      <td>${escapeHtml(order.result || DASH)}</td>
      <td class="${num(order.pnl) >= 0 ? "long" : "short"}">${order.status === "SETTLED" ? fmtMoney(order.pnl) : DASH}</td>
      <td class="reason">${escapeHtml(order.reason)}<br><span>状态 ${escapeHtml(order.regime || "UNKNOWN")}</span></td>
    </tr>
  `).join("") : `<tr><td colspan="15" class="empty-row">没有符合筛选条件的订单</td></tr>`;
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

async function loadState() {
  const response = await fetch("/api/state");
  const state = await response.json();
  currentSymbol = state.symbol;
  Object.entries(summaryFields).forEach(([id, getValue]) => setText(id, getValue(state)));
  $("symbol-input").value = state.symbol;
  $("webhook-status").className = webhookClass(state.webhook);
  $("warmup-status").className = warmupClass(state.warmup);
  $("rolling-edge-status").className = rollingEdgeClass(state.rolling_edge);
  $("short-extension-status").className = shortExtensionClass(state);
  $("last-error").textContent = state.last_error || "";
  renderSelectedSignal(state.selected_signal, state.order_decision);
  if (lastFilterOptions === null) {
    await loadOrders();
  }
}

$("symbol-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const symbol = $("symbol-input").value.trim().toUpperCase();
  if (!symbol) return;
  await fetch(`/api/config?symbol=${encodeURIComponent(symbol)}`);
  currentSymbol = symbol;
  ordersPage = 1;
  lastFilterOptions = null;
  await loadState();
  await loadOrders();
});

for (const id of [...filterIds, "page-size-filter"]) {
  $(id).addEventListener("change", async () => {
    ordersPage = 1;
    await loadOrders();
  });
}

$("prev-page").addEventListener("click", async () => {
  ordersPage = Math.max(1, ordersPage - 1);
  await loadOrders();
});

$("next-page").addEventListener("click", async () => {
  ordersPage = Math.min(ordersTotalPages, ordersPage + 1);
  await loadOrders();
});

loadState();
loadOrders();
setInterval(loadState, 3000);
setInterval(loadOrders, 10000);
