import argparse
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.binance_client import BinanceKlineClient
from app.daily_profile_selector import DailyProfileSelectorConfig
from app.fear_greed import FearGreedProvider
from app.history import WarmupConfig, WarmupReport, warmup_history
from app.market_data import MarketDataCoordinator
from app.profile_degradation_guard import ProfileDegradationGuardConfig
from app.profile_health_guard import ProfileHealthGuardConfig
from app.profile_admission import ProfileAdmissionPolicy, baseline_policy
from app.result_sequence_guard import ResultSequenceGuardConfig
from app.shadow_supervisor import ShadowSupervisor
from app.state import DEFAULT_STRATEGY_BUILD_ID, MonitorState, strategy_source_build_id
from app.time_period_guard import TimePeriodGuardConfig
from app.webhook import DEFAULT_IMPORT_TOKEN, DEFAULT_WEBHOOK_URL, WebhookSignalProxy


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
STATIC_DIR = ROOT / "static"
DEFAULT_STAKE_PROGRESSION_BASE_ONLY_SEGMENTS = ""
DEFAULT_LIVE_SHORT_SEGMENTS = "WD-02,WD-23"
STATE_RESPONSE_CACHE_SECONDS = 2.0
ORDER_RESPONSE_CACHE_SECONDS = 300.0
OBSERVATION_RESPONSE_CACHE_SECONDS = 30.0
SUMMARY_RESPONSE_CACHE_SECONDS = 30.0


class _JsonResponseCache:
    def __init__(self, max_entries: int = 128):
        self._condition = threading.Condition()
        self._entries: dict[tuple, tuple[float, bytes]] = {}
        self._inflight: set[tuple] = set()
        self._max_entries = max_entries

    def get_or_create(self, key: tuple, ttl_seconds: float, producer) -> bytes:
        while True:
            with self._condition:
                now = time.monotonic()
                cached = self._entries.get(key)
                if cached is not None and cached[0] > now:
                    return cached[1]
                if key not in self._inflight:
                    self._inflight.add(key)
                    break
                self._condition.wait()

        try:
            body = _json_bytes(producer())
        except BaseException:
            with self._condition:
                self._inflight.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            now = time.monotonic()
            expired = [
                cached_key
                for cached_key, (expires_at, _) in self._entries.items()
                if expires_at <= now
            ]
            for cached_key in expired:
                self._entries.pop(cached_key, None)
            if len(self._entries) >= self._max_entries:
                oldest = min(self._entries, key=lambda item: self._entries[item][0])
                self._entries.pop(oldest, None)
            self._entries[key] = (now + ttl_seconds, body)
            self._inflight.discard(key)
            self._condition.notify_all()
        return body

    def clear(self) -> None:
        with self._condition:
            self._entries.clear()


class _ChineseHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, *args, **kwargs) -> str:
        return super()._format_usage(*args, **kwargs).replace(
            "usage: ",
            "用法: ",
            1,
        )


def start_market_data(
    state: MonitorState,
    client: BinanceKlineClient,
    *,
    poll_seconds: int,
    limit: int,
    enable_websocket: bool,
    shadow_publisher=None,
) -> MarketDataCoordinator:
    coordinator = MarketDataCoordinator(
        state,
        client,
        poll_seconds=poll_seconds,
        rest_limit=limit,
        enable_websocket=enable_websocket,
        shadow_publisher=shadow_publisher,
    )
    coordinator.start()
    return coordinator


def make_handler(state: MonitorState, warmup_loader=None, market_data=None):
    symbol_switch_lock = threading.Lock()
    response_cache = _JsonResponseCache()

    class MonitorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                self._send_cached_json(
                    ("state", getattr(state, "symbol", "")),
                    STATE_RESPONSE_CACHE_SECONDS,
                    lambda: state.snapshot(include_collections=False),
                )
                return
            if parsed.path == "/api/price":
                self._send_json(state.price_snapshot())
                return
            if parsed.path == "/api/shadow":
                self._send_json(state.shadow_optimizer_status())
                return
            if parsed.path == "/api/shadow/experiment":
                self._send_json(state.shadow_experiment_summary())
                return
            if parsed.path == "/api/shadow/orders":
                query = parse_qs(parsed.query)
                self._send_json(
                    state.page_shadow_orders(
                        arm_id=_query_text(query, "arm_id"),
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "page_size", 20),
                    )
                )
                return
            if parsed.path == "/api/orders":
                query = parse_qs(parsed.query)
                dashboard = _query_dashboard(query)
                kwargs = {
                    "page": _query_int(query, "page", 1),
                    "page_size": _query_int(query, "page_size", 20),
                    "direction": _query_text(query, "direction"),
                    "level": _query_text(query, "level"),
                    "segment": _query_text(query, "segment"),
                    "result": _query_text(query, "result"),
                    "dashboard": dashboard,
                }
                if dashboard:
                    self._send_cached_json(
                        (
                            "orders",
                            getattr(state, "symbol", ""),
                            _order_page_revision(state),
                            *tuple(kwargs.items()),
                        ),
                        ORDER_RESPONSE_CACHE_SECONDS,
                        lambda: state.page_orders(**kwargs),
                    )
                else:
                    self._send_json(state.page_orders(**kwargs))
                return
            if parsed.path == "/api/observations":
                query = parse_qs(parsed.query)
                dashboard = _query_dashboard(query)
                kwargs = {
                    "page": _query_int(query, "page", 1),
                    "page_size": _query_int(query, "page_size", 20),
                    "direction": _query_text(query, "direction"),
                    "family": _query_text(query, "family"),
                    "tag": _query_text(query, "tag"),
                    "segment": _query_text(query, "segment"),
                    "result": _query_text(query, "result"),
                    "candidate_origin": _query_text(query, "candidate_origin"),
                    "qualification_state": _query_text(query, "qualification_state"),
                    "adaptive_state": _query_text(query, "adaptive_state"),
                    "entry_structure_state": _query_text(query, "entry_structure_state"),
                    "entry_structure_bias": _query_text(query, "entry_structure_bias"),
                    "active_level_source": _query_text(query, "active_level_source"),
                    "dashboard": dashboard,
                }
                if dashboard:
                    self._send_cached_json(
                        (
                            "observations",
                            getattr(state, "symbol", ""),
                            *tuple(kwargs.items()),
                        ),
                        OBSERVATION_RESPONSE_CACHE_SECONDS,
                        lambda: state.page_observations(**kwargs),
                    )
                else:
                    self._send_json(state.page_observations(**kwargs))
                return
            if parsed.path == "/api/observation-summary":
                query = parse_qs(parsed.query)
                window = _query_text(query, "window") or "14d"
                self._send_cached_json(
                    ("observation-summary", getattr(state, "symbol", ""), window),
                    SUMMARY_RESPONSE_CACHE_SECONDS,
                    lambda: state.observation_summary(window=window),
                )
                return
            if parsed.path == "/api/order-profile":
                self._send_cached_json(
                    ("order-profile", getattr(state, "symbol", "")),
                    SUMMARY_RESPONSE_CACHE_SECONDS,
                    state.order_profile_summary,
                )
                return
            if parsed.path == "/api/signal-audit-summary":
                self._send_json(state.signal_audit_summary())
                return
            if parsed.path == "/api/config":
                query = parse_qs(parsed.query)
                symbol = query.get("symbol", [None])[0]
                configured_symbol = state.symbol
                if symbol:
                    with symbol_switch_lock:
                        if market_data is not None:
                            market_data.pause_updates()
                        try:
                            state.reset_symbol(symbol)
                            if warmup_loader is not None:
                                warmup_loader(state)
                        finally:
                            if market_data is not None:
                                market_data.request_symbol_refresh()
                        configured_symbol = state.symbol
                        response_cache.clear()
                self._send_json({"symbol": configured_symbol})
                return
            if parsed.path == "/":
                self._send_file(STATIC_DIR / "index.html")
                return
            if parsed.path.startswith("/static/"):
                relative = parsed.path.removeprefix("/static/")
                self._send_file(STATIC_DIR / relative)
                return
            self.send_error(404, "Not found")

        def log_message(self, fmt: str, *args) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def _send_json(self, payload: dict) -> None:
            self._send_json_body(_json_bytes(payload))

        def _send_cached_json(self, key: tuple, ttl_seconds: float, producer) -> None:
            self._send_json_body(
                response_cache.get_or_create(key, ttl_seconds, producer)
            )

        def _send_json_body(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_file(self, path: Path) -> None:
            try:
                resolved = path.resolve()
                if not str(resolved).startswith(str(STATIC_DIR.resolve())):
                    self.send_error(403, "Forbidden")
                    return
                body = resolved.read_bytes()
            except FileNotFoundError:
                self.send_error(404, "Not found")
                return

            content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            if resolved.suffix == ".html":
                content_type = "text/html; charset=utf-8"
            elif resolved.suffix == ".css":
                content_type = "text/css; charset=utf-8"
            elif resolved.suffix == ".js":
                content_type = "application/javascript; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return MonitorHandler


def _query_text(query: dict, name: str) -> str:
    return str(query.get(name, [""])[0] or "").strip()


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _query_dashboard(query: dict) -> bool:
    return _query_text(query, "dashboard").lower() not in {"0", "false", "no"}


def _order_page_revision(state) -> tuple:
    reader = getattr(state, "order_page_revision", None)
    if not callable(reader):
        return ()
    revision = reader()
    return tuple(revision) if isinstance(revision, (list, tuple)) else (revision,)


def _query_int(query: dict, name: str, default: int) -> int:
    try:
        return int(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


def apply_warmup(
    state: MonitorState,
    data_dir: Path,
    months: int,
    include_current_month_daily: bool,
    timeout: float,
) -> WarmupReport:
    symbol_context = state.capture_symbol_context()
    config = WarmupConfig(
        symbol=symbol_context[0],
        data_dir=data_dir,
        months=months,
        include_current_month_daily=include_current_month_daily,
        timeout=timeout,
    )
    klines, report = warmup_history(config)
    state.seed_klines(klines, report.to_dict(), expected_context=symbol_context)
    return report


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _strict_env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} 必须为布尔值，实际为 {value!r}")


def _env_float(name: str, default: float | None) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return int(value)


def _trade_score_threshold(value: str) -> float | None:
    normalized = str(value).strip().lower()
    if normalized in {"", "auto"}:
        return None
    try:
        threshold = float(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "开单评分阈值必须是 auto 或 0 到 95 的数字"
        ) from exc
    if not 0.0 <= threshold <= 95.0:
        raise argparse.ArgumentTypeError("开单评分阈值必须在 0 到 95 之间")
    return threshold


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _profile_admission_directions(value: str) -> tuple[str, ...]:
    directions = tuple(_split_csv(value))
    if not directions or set(directions) - {"LONG", "SHORT"}:
        raise argparse.ArgumentTypeError(
            "画像准入策略快速方向只能使用 LONG、SHORT，且不能为空"
        )
    return directions


def _profile_admission_optional_rate(value: str) -> float | None:
    normalized = str(value).strip().lower()
    if normalized in {"", "off", "none", "关闭"}:
        return None
    try:
        return float(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "画像准入胜率下限必须是 0 到 1 的数字或 off"
        ) from exc


def _clock_value(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = str(value).split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("时间必须使用 HH:MM 格式") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError("时间必须在 00:00 到 23:59 之间")
    return hour, minute


def _strategy_build_id(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise argparse.ArgumentTypeError("策略构建标识不能为空")
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(
        description="币安事件合约量价监控程序",
        add_help=False,
        formatter_class=_ChineseHelpFormatter,
    )
    parser._optionals.title = "参数"
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument("--symbol", default=os.getenv("SYMBOL", "BTCUSDT"), help="交易对，默认: BTCUSDT")
    parser.add_argument(
        "--strategy-build-id",
        type=_strategy_build_id,
        default=os.getenv("STRATEGY_BUILD_ID", DEFAULT_STRATEGY_BUILD_ID),
        help=(
            "策略构建标识，用于冻结运行配置和决策身份，可填写 commit 或 tag；"
            f"默认: {DEFAULT_STRATEGY_BUILD_ID}"
        ),
    )
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="监听地址，默认: 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")), help="页面端口，默认: 8000")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("POLL_SECONDS", "10")),
        help="币安轮询间隔秒数，默认: 10",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.getenv("KLINE_LIMIT", "300")),
        help="每次拉取的 1分钟K线数量，默认: 300",
    )
    parser.add_argument(
        "--no-websocket",
        action="store_true",
        default=_env_bool("NO_WEBSOCKET", False),
        help="关闭币安 WebSocket 实时行情，改用 REST 轮询兜底",
    )
    parser.add_argument(
        "--data-dir",
        default=os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")),
        help="本地 Binance Vision 缓存目录，默认: ./data",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "monitor.sqlite3")),
        help="SQLite 持久化路径，默认: ./data/monitor.sqlite3",
    )
    shadow_switch = parser.add_mutually_exclusive_group()
    shadow_switch.add_argument(
        "--enable-shadow-optimizer",
        dest="enable_shadow_optimizer",
        action="store_true",
        default=_env_bool("SHADOW_OPTIMIZER", False),
        help="启用独立进程持续影子参数优化，默认: 关闭",
    )
    shadow_switch.add_argument(
        "--no-shadow-optimizer",
        dest="enable_shadow_optimizer",
        action="store_false",
        help="关闭持续影子参数优化",
    )
    parser.add_argument(
        "--shadow-db-path",
        default=os.getenv(
            "SHADOW_DB_PATH",
            str(PROJECT_ROOT / "data" / "monitor.shadow.sqlite3"),
        ),
        help="影子参数独立 SQLite 路径，默认: ./data/monitor.shadow.sqlite3",
    )
    parser.add_argument(
        "--shadow-queue-size",
        type=int,
        default=int(os.getenv("SHADOW_QUEUE_SIZE", "120")),
        help="影子分钟事件有界队列长度，默认: 120",
    )
    parser.add_argument(
        "--shadow-max-challengers",
        type=int,
        choices=range(1, 8),
        default=int(os.getenv("SHADOW_MAX_CHALLENGERS", "7")),
        metavar="1-7",
        help="并行影子候选数量，范围1到7，默认: 7",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("WEBHOOK_URL", DEFAULT_WEBHOOK_URL),
        help="外部信号 webhook 地址",
    )
    parser.add_argument(
        "--webhook-token",
        default=os.getenv("WEBHOOK_TOKEN", DEFAULT_IMPORT_TOKEN),
        help="外部信号 importToken",
    )
    parser.add_argument(
        "--webhook-timeout",
        type=float,
        default=float(os.getenv("WEBHOOK_TIMEOUT", "5")),
        help="Webhook 超时秒数，默认: 5",
    )
    parser.add_argument(
        "--warmup-months",
        type=int,
        default=int(os.getenv("WARMUP_MONTHS", "3")),
        help="预热加载的完整月度文件数量，默认: 3",
    )
    parser.add_argument(
        "--warmup-timeout",
        type=float,
        default=float(os.getenv("WARMUP_TIMEOUT", "20")),
        help="单个历史文件下载超时秒数，默认: 20",
    )
    parser.add_argument("--stake", type=float, default=float(os.getenv("STAKE", "10")), help="基础下单金额，默认: 10")
    parser.add_argument(
        "--trade-score-threshold",
        type=_trade_score_threshold,
        default=os.getenv("TRADE_SCORE_THRESHOLD", "auto"),
        help="兼容审计参数，不改变开单资格；默认: auto",
    )
    parser.add_argument(
        "--max-open-orders",
        type=int,
        default=int(os.getenv("MAX_OPEN_ORDERS", "2")),
        help="最多同时持有的未结订单数，默认: 2",
    )
    parser.add_argument(
        "--max-open-long-orders",
        type=int,
        default=int(os.getenv("MAX_OPEN_LONG_ORDERS", "1")),
        help="最多同时持有的 LONG 未结订单数，默认: 1",
    )
    parser.add_argument(
        "--max-open-short-orders",
        type=int,
        default=int(os.getenv("MAX_OPEN_SHORT_ORDERS", "2")),
        help="最多同时持有的 SHORT 未结订单数，默认: 2",
    )
    parser.add_argument(
        "--min-order-gap-minutes",
        type=float,
        default=float(os.getenv("MIN_ORDER_GAP_MINUTES", "2")),
        help="同方向两次开单最小间隔分钟数，默认: 2",
    )
    parser.add_argument(
        "--win-return",
        type=float,
        default=_env_float("WIN_RETURN", None),
        help="赢单返还金额，默认: stake * 1.8",
    )
    parser.add_argument(
        "--stake-progression-max-orders",
        type=int,
        default=os.getenv("STAKE_PROGRESSION_MAX_ORDERS", "2"),
        help="兼容参数；两阶段固定为 2 级，默认: 2",
    )
    parser.add_argument(
        "--stake-progression-max-active",
        type=int,
        default=os.getenv("STAKE_PROGRESSION_MAX_ACTIVE", "1"),
        help="最多并行第二级订单数，默认: 1",
    )
    parser.add_argument(
        "--stake-progression-base-only-segments",
        default=os.getenv("STAKE_PROGRESSION_BASE_ONLY_SEGMENTS", DEFAULT_STAKE_PROGRESSION_BASE_ONLY_SEGMENTS),
        help=(
            "兼容参数；仅使用基础金额、不继承第二级金额的时段，逗号分隔；"
            "默认空，生产默认所有已入选时段均可参与"
        ),
    )
    parser.add_argument(
        "--no-current-month-daily",
        action="store_true",
        default=not _env_bool("WARMUP_CURRENT_MONTH_DAILY", True),
        help="跳过当前月份日线历史预热",
    )
    parser.add_argument("--no-warmup", action="store_true", default=_env_bool("NO_WARMUP", False), help="关闭历史预热")
    parser.add_argument(
        "--no-persistence",
        action="store_true",
        default=_env_bool("NO_PERSISTENCE", False),
        help="关闭 SQLite 订单和信号持久化",
    )
    parser.add_argument(
        "--no-webhook",
        action="store_true",
        default=_env_bool("NO_WEBHOOK", False),
        help="关闭外部 webhook 信号推送",
    )
    parser.add_argument(
        "--no-stake-progression",
        action="store_true",
        default=not _env_bool("STAKE_PROGRESSION", True),
        help="关闭两阶段金额叠加",
    )
    parser.add_argument(
        "--no-rolling-edge-guard",
        action="store_true",
        default=not _env_bool("ROLLING_EDGE_GUARD", True),
        help="关闭滚动优势守卫，仅保留状态观察",
    )
    parser.add_argument(
        "--no-result-sequence-guard",
        action="store_true",
        default=not _env_bool("RESULT_SEQUENCE_GUARD", True),
        help="关闭同方向连续亏损冷却守卫，默认启用",
    )
    parser.add_argument(
        "--no-time-period-guard",
        action="store_true",
        default=not _env_bool("TIME_PERIOD_GUARD", False),
        help="关闭北京时间12:00-18:00真实开单暂停；默认关闭，可用环境变量显式启用",
    )
    parser.add_argument(
        "--no-profile-health-guard",
        action="store_true",
        default=not _env_bool("PROFILE_HEALTH_GUARD", True),
        help="关闭24小时画像健康守卫；默认每4小时按方向评估一次",
    )
    parser.add_argument(
        "--result-sequence-loss-streak",
        type=int,
        default=int(os.getenv("RESULT_SEQUENCE_LOSS_STREAK", "3")),
        help="同方向连续已结算亏损触发笔数，默认: 3",
    )
    parser.add_argument(
        "--result-sequence-cooldown-minutes",
        type=int,
        default=int(os.getenv("RESULT_SEQUENCE_COOLDOWN_MINUTES", "20")),
        help="结算序列守卫触发后的冷却分钟数，默认: 20",
    )
    parser.add_argument(
        "--result-sequence-scope",
        type=str.upper,
        choices=("GLOBAL", "DIRECTION"),
        default=os.getenv("RESULT_SEQUENCE_SCOPE", "DIRECTION").upper(),
        help="结算序列统计范围：GLOBAL 全局，DIRECTION 同方向；默认: DIRECTION",
    )
    parser.add_argument(
        "--profile-guard",
        action="store_true",
        default=_env_bool("PROFILE_GUARD", False),
        help="开启画像守卫正式拦截，默认仅影子观察",
    )
    parser.add_argument(
        "--profile-guard-min-history",
        type=int,
        default=int(os.getenv("PROFILE_GUARD_MIN_HISTORY", "15")),
        help="画像守卫启用前需要的历史订单数量，默认: 15",
    )
    parser.add_argument(
        "--profile-guard-min-group-size",
        type=int,
        default=int(os.getenv("PROFILE_GUARD_MIN_GROUP_SIZE", "2")),
        help="画像守卫单个弱点最小历史样本数，默认: 2",
    )
    parser.add_argument(
        "--profile-degradation-cooldown-minutes",
        type=int,
        default=os.getenv("PROFILE_DEGRADATION_COOLDOWN_MINUTES", "60"),
        help="完整画像连续亏损3单后的冷却分钟数，0关闭，默认: 60",
    )
    parser.add_argument(
        "--no-observation-profile-promotion",
        action="store_true",
        default=not _env_bool("OBSERVATION_PROFILE_PROMOTION", True),
        help="关闭已结算观察画像对静态时段拦截的动态放行",
    )
    parser.add_argument(
        "--observation-profile-lookback-days",
        type=int,
        default=int(os.getenv("OBSERVATION_PROFILE_LOOKBACK_DAYS", "7")),
        help="观察画像滚动统计天数，默认: 7",
    )
    parser.add_argument(
        "--observation-profile-min-samples",
        type=int,
        default=int(os.getenv("OBSERVATION_PROFILE_MIN_SAMPLES", "12")),
        help="观察画像允许开单的最小独立已结算样本数，默认: 12",
    )
    parser.add_argument(
        "--observation-profile-min-win-rate",
        type=float,
        default=float(os.getenv("OBSERVATION_PROFILE_MIN_WIN_RATE", "0.72")),
        help="观察画像允许开单的最低胜率，默认: 0.72",
    )
    parser.add_argument(
        "--observation-profile-min-ev",
        type=float,
        default=float(os.getenv("OBSERVATION_PROFILE_MIN_EV", "4")),
        help="观察画像允许开单的最低单笔期望收益，默认: 4U",
    )
    parser.add_argument(
        "--observation-profile-min-edge",
        type=float,
        default=float(os.getenv("OBSERVATION_PROFILE_MIN_EDGE", "10")),
        help="观察画像动态放行要求的最低评分边际，默认: 10",
    )
    parser.add_argument(
        "--live-short-segments",
        default=os.getenv("LIVE_SHORT_SEGMENTS", DEFAULT_LIVE_SHORT_SEGMENTS),
        help=f"允许实际开 SHORT 的时段，逗号分隔；默认: {DEFAULT_LIVE_SHORT_SEGMENTS}",
    )
    deprecated_admission_env = "PROFILE_ADMISSION_RESIDENT_N12_MAX_WINS"
    if deprecated_admission_env in os.environ:
        parser.error(
            f"{deprecated_admission_env} 已废弃；请分别配置 LONG/SHORT N12 上限"
        )
    try:
        profile_admission_enabled = _strict_env_bool(
            "PROFILE_ADMISSION_ENABLE",
            False,
        )
    except ValueError as exc:
        parser.error(str(exc))
    parser.add_argument(
        "--enable-profile-admission",
        action="store_true",
        default=profile_admission_enabled,
        help=(
            "显式启用冻结画像准入候选；默认关闭并使用兼容基准。"
            "前向稳定性尚未证明，release_allowed 始终为 false"
        ),
    )
    parser.add_argument(
        "--profile-admission-resident-long-n12-max-wins",
        type=int,
        default=int(os.getenv("PROFILE_ADMISSION_RESIDENT_LONG_N12_MAX_WINS", "7")),
        help="LONG 常驻画像 N12 最大胜数，候选默认: 7",
    )
    parser.add_argument(
        "--profile-admission-resident-short-n12-max-wins",
        type=int,
        default=int(os.getenv("PROFILE_ADMISSION_RESIDENT_SHORT_N12_MAX_WINS", "9")),
        help="SHORT 常驻画像 N12 最大胜数，候选默认: 9",
    )
    parser.add_argument(
        "--profile-admission-resident-long-win-rate-floor",
        type=_profile_admission_optional_rate,
        default=os.getenv("PROFILE_ADMISSION_RESIDENT_LONG_WIN_RATE_FLOOR", "off"),
        help="LONG 常驻画像当日胜率下限，off 关闭，候选默认: off",
    )
    parser.add_argument(
        "--profile-admission-resident-short-win-rate-floor",
        type=_profile_admission_optional_rate,
        default=os.getenv("PROFILE_ADMISSION_RESIDENT_SHORT_WIN_RATE_FLOOR", "0.625"),
        help="SHORT 常驻画像当日胜率下限，off 关闭，候选默认: 0.625",
    )
    parser.add_argument(
        "--profile-admission-fast-directions",
        type=_profile_admission_directions,
        default=os.getenv("PROFILE_ADMISSION_FAST_DIRECTIONS", "SHORT"),
        help="快速通道允许方向，逗号分隔，候选默认: SHORT",
    )
    parser.add_argument(
        "--profile-admission-fast-n12-min-wins",
        type=int,
        default=int(os.getenv("PROFILE_ADMISSION_FAST_N12_MIN_WINS", "7")),
        help="快速通道 N12 最小胜数，候选默认: 7",
    )
    parser.add_argument(
        "--profile-admission-fast-n12-max-wins",
        type=int,
        default=int(os.getenv("PROFILE_ADMISSION_FAST_N12_MAX_WINS", "8")),
        help="快速通道 N12 最大胜数，候选默认: 8",
    )
    parser.add_argument(
        "--profile-admission-fast-n20-ev-min",
        type=float,
        default=float(os.getenv("PROFILE_ADMISSION_FAST_N20_EV_MIN", "0")),
        help="快速通道 N20 最低 EV，候选默认: 0",
    )
    parser.add_argument(
        "--no-daily-profile-selector",
        action="store_true",
        default=not _env_bool("DAILY_PROFILE_SELECTOR", True),
        help="关闭每日观察画像策略选择器，回退到静态主策略",
    )
    parser.add_argument(
        "--daily-profile-lookback-days",
        type=int,
        default=int(os.getenv("DAILY_PROFILE_LOOKBACK_DAYS", "7")),
        help="每日画像统计回看天数，默认: 7",
    )
    parser.add_argument(
        "--daily-profile-stable-lookback-days",
        type=int,
        default=_env_optional_int("DAILY_PROFILE_STABLE_LOOKBACK_DAYS"),
        help="每日画像稳定窗口天数；未指定时取 14 与快速窗口天数的较大值",
    )
    parser.add_argument(
        "--daily-profile-min-samples",
        type=int,
        default=int(os.getenv("DAILY_PROFILE_MIN_SAMPLES", "20")),
        help="工作日新画像入选所需最小独立样本数，默认: 20",
    )
    parser.add_argument(
        "--daily-profile-weekend-min-samples",
        type=int,
        default=int(os.getenv("DAILY_PROFILE_WEEKEND_MIN_SAMPLES", "10")),
        help="周末新画像入选所需最小独立样本数，默认: 10",
    )
    parser.add_argument(
        "--daily-profile-min-win-rate",
        type=float,
        default=float(os.getenv("DAILY_PROFILE_MIN_WIN_RATE", "0.60")),
        help="新画像入选最低胜率，默认: 0.60",
    )
    parser.add_argument(
        "--daily-profile-min-ev",
        type=float,
        default=float(os.getenv("DAILY_PROFILE_MIN_EV", "0")),
        help="新画像入选最低单笔期望收益，默认: 0U",
    )
    parser.add_argument(
        "--daily-profile-exit-win-rate",
        type=float,
        default=float(os.getenv("DAILY_PROFILE_EXIT_WIN_RATE", "0.60")),
        help="已启用画像退化胜率线，默认: 0.60",
    )
    parser.add_argument(
        "--daily-profile-exit-ev",
        type=float,
        default=float(os.getenv("DAILY_PROFILE_EXIT_EV", "0")),
        help="已启用画像退化EV线，默认: 0U",
    )
    parser.add_argument(
        "--daily-profile-degraded-runs",
        type=int,
        default=int(os.getenv("DAILY_PROFILE_DEGRADED_RUNS", "2")),
        help="兼容画像连续退化次数，默认: 2",
    )
    parser.add_argument(
        "--daily-profile-joint-failures-to-exit",
        type=int,
        default=_env_optional_int("DAILY_PROFILE_JOINT_FAILURES_TO_EXIT"),
        help="双窗口同时失败多少次后退出；未指定时沿用连续退化次数",
    )
    parser.add_argument(
        "--daily-profile-max-active",
        type=int,
        default=int(os.getenv("DAILY_PROFILE_MAX_ACTIVE", "0")),
        help="每天最多启用画像数量，0 表示不限制，默认: 0",
    )
    parser.add_argument(
        "--daily-profile-evaluation-time",
        type=_clock_value,
        default=os.getenv("DAILY_PROFILE_EVALUATION_TIME", "07:50"),
        help="每天北京时间画像评估时间，格式 HH:MM，默认: 07:50",
    )
    parser.add_argument(
        "--daily-profile-activation-time",
        type=_clock_value,
        default=os.getenv("DAILY_PROFILE_ACTIVATION_TIME", "08:00"),
        help="每天北京时间画像生效时间，格式 HH:MM，默认: 08:00",
    )
    args = parser.parse_args()
    try:
        candidate_admission_policy = ProfileAdmissionPolicy(
            resident_allowed_states=("ACTIVE", "WATCH"),
            resident_n12_max_wins=12,
            resident_long_n12_max_wins=(
                args.profile_admission_resident_long_n12_max_wins
            ),
            resident_short_n12_max_wins=(
                args.profile_admission_resident_short_n12_max_wins
            ),
            resident_long_daily_win_rate_floor=(
                args.profile_admission_resident_long_win_rate_floor
            ),
            resident_short_daily_win_rate_floor=(
                args.profile_admission_resident_short_win_rate_floor
            ),
            fast_enabled=True,
            fast_directions=args.profile_admission_fast_directions,
            fast_allowed_states=("ACTIVE",),
            fast_n12_min_wins=args.profile_admission_fast_n12_min_wins,
            fast_n12_max_wins=args.profile_admission_fast_n12_max_wins,
            fast_n20_ev_min=args.profile_admission_fast_n20_ev_min,
        )
    except (TypeError, ValueError) as exc:
        parser.error(f"画像准入策略参数无效：{exc}")
    profile_admission_policy = (
        candidate_admission_policy
        if args.enable_profile_admission
        else baseline_policy()
    )
    win_return = args.win_return if args.win_return is not None else round(args.stake * 1.8, 4)

    webhook = None
    if not args.no_webhook:
        webhook = WebhookSignalProxy(
            url=args.webhook_url,
            import_token=args.webhook_token,
            timeout_seconds=args.webhook_timeout,
        )

    state = MonitorState(
        symbol=args.symbol,
        strategy_build_id=args.strategy_build_id,
        max_open_orders=args.max_open_orders,
        max_open_long_orders=args.max_open_long_orders,
        max_open_short_orders=args.max_open_short_orders,
        min_order_gap_ms=round(args.min_order_gap_minutes * 60_000),
        fear_greed_provider=FearGreedProvider(),
        storage_path=None if args.no_persistence else args.db_path,
        webhook=webhook,
        stake=args.stake,
        win_return=win_return,
        trade_score_threshold=args.trade_score_threshold,
        enable_rolling_edge_guard=not args.no_rolling_edge_guard,
        result_sequence_guard_config=ResultSequenceGuardConfig(
            enabled=not args.no_result_sequence_guard,
            loss_streak=args.result_sequence_loss_streak,
            cooldown_minutes=args.result_sequence_cooldown_minutes,
            scope=args.result_sequence_scope,
        ),
        time_period_guard_config=TimePeriodGuardConfig(
            enabled=not args.no_time_period_guard,
        ),
        profile_health_guard_config=ProfileHealthGuardConfig(
            enabled=not args.no_profile_health_guard,
        ),
        enable_stake_progression=not args.no_stake_progression,
        stake_progression_max_orders=args.stake_progression_max_orders,
        stake_progression_max_active=args.stake_progression_max_active,
        stake_progression_base_only_segments=_split_csv(args.stake_progression_base_only_segments),
        enable_profile_guard=args.profile_guard,
        profile_guard_min_history=args.profile_guard_min_history,
        profile_guard_min_group_size=args.profile_guard_min_group_size,
        profile_degradation_guard_config=ProfileDegradationGuardConfig(
            cooldown_minutes=args.profile_degradation_cooldown_minutes,
        ),
        enable_observation_profile_promotion=not args.no_observation_profile_promotion,
        observation_profile_lookback_days=args.observation_profile_lookback_days,
        observation_profile_min_samples=args.observation_profile_min_samples,
        observation_profile_min_win_rate=args.observation_profile_min_win_rate,
        observation_profile_min_ev=args.observation_profile_min_ev,
        observation_profile_min_edge=args.observation_profile_min_edge,
        live_short_segments=_split_csv(args.live_short_segments),
        enable_daily_profile_selector=not args.no_daily_profile_selector,
        profile_admission_policy=profile_admission_policy,
        daily_profile_selector_config=DailyProfileSelectorConfig(
            lookback_days=args.daily_profile_lookback_days,
            stable_lookback_days=args.daily_profile_stable_lookback_days,
            min_samples=args.daily_profile_min_samples,
            weekend_min_samples=args.daily_profile_weekend_min_samples,
            min_win_rate=args.daily_profile_min_win_rate,
            min_ev=args.daily_profile_min_ev,
            exit_win_rate=args.daily_profile_exit_win_rate,
            exit_ev=args.daily_profile_exit_ev,
            degraded_runs_to_exit=args.daily_profile_degraded_runs,
            joint_failures_to_exit=args.daily_profile_joint_failures_to_exit,
            max_active_profiles=args.daily_profile_max_active,
            evaluation_hour=args.daily_profile_evaluation_time[0],
            evaluation_minute=args.daily_profile_evaluation_time[1],
            activation_hour=args.daily_profile_activation_time[0],
            activation_minute=args.daily_profile_activation_time[1],
        ),
    )
    data_dir = Path(args.data_dir)
    include_current_month_daily = not args.no_current_month_daily
    if not args.no_warmup:
        report = apply_warmup(
            state,
            data_dir=data_dir,
            months=args.warmup_months,
            include_current_month_daily=include_current_month_daily,
            timeout=args.warmup_timeout,
        )
        print(
            "warmup: "
            f"status={report.status} klines={report.loaded_klines} "
            f"cached={len(report.cached_files)} downloaded={len(report.downloaded_files)} "
            f"missing={len(report.missing_files)}"
        )

    shadow_optimizer = None
    if args.enable_shadow_optimizer:
        shadow_optimizer = ShadowSupervisor(
            seed_supplier=state.shadow_runtime_seed,
            database_path=args.shadow_db_path,
            queue_size=args.shadow_queue_size,
            max_challengers=args.shadow_max_challengers,
            lifecycle_handler=state.request_shadow_policy_change,
        )
        state.attach_shadow_status_provider(shadow_optimizer)
        state.attach_shadow_policy_audit_sink(
            shadow_optimizer.record_formal_policy_receipt
        )
        try:
            restored_request = shadow_optimizer.restored_policy_request()
            if restored_request is not None:
                state.request_shadow_policy_change(restored_request)
            shadow_optimizer.start()
        except Exception as exc:  # noqa: BLE001 - 影子启动失败不得阻断正式服务。
            recorder = getattr(state, "record_shadow_event_gap", None)
            if callable(recorder):
                recorder(f"shadow optimizer startup failed: {exc}")
            shadow_optimizer.stop()

    client = BinanceKlineClient()
    market_data = start_market_data(
        state,
        client,
        poll_seconds=args.poll_seconds,
        limit=args.limit,
        enable_websocket=not args.no_websocket,
        shadow_publisher=shadow_optimizer,
    )

    def warmup_loader(target_state: MonitorState) -> None:
        if args.no_warmup:
            return
        try:
            report = apply_warmup(
                target_state,
                data_dir=data_dir,
                months=args.warmup_months,
                include_current_month_daily=include_current_month_daily,
                timeout=args.warmup_timeout,
            )
            print(
                "warmup: "
                f"symbol={target_state.symbol} status={report.status} klines={report.loaded_klines} "
                f"cached={len(report.cached_files)} downloaded={len(report.downloaded_files)} "
                f"missing={len(report.missing_files)}"
            )
        except Exception as exc:  # noqa: BLE001 - 切换币种失败不能阻塞页面响应。
            target_state.record_error(f"warmup failed: {exc}")

    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            state,
            warmup_loader=warmup_loader,
            market_data=market_data,
        ),
    )
    source = "REST" if args.no_websocket else "WebSocket+REST"
    print(f"monitor: http://{args.host}:{args.port} symbol={state.symbol} market={source}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nmonitor stopped")
    finally:
        server.server_close()
        market_data.stop()
        if shadow_optimizer is not None:
            shadow_optimizer.stop()
        closer = getattr(state, "close", None)
        if closer is not None:
            closer()


if __name__ == "__main__":
    main()
