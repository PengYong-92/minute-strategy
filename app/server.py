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
from app.fear_greed import FearGreedProvider
from app.history import WarmupConfig, WarmupReport, warmup_history
from app.state import MonitorState
from app.webhook import DEFAULT_IMPORT_TOKEN, DEFAULT_WEBHOOK_URL, WebhookSignalProxy


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
STATIC_DIR = ROOT / "static"


def start_polling(state: MonitorState, client: BinanceKlineClient, poll_seconds: int, limit: int) -> threading.Thread:
    def loop() -> None:
        while True:
            try:
                klines = client.get_klines(state.symbol, interval="1m", limit=limit)
                state.update_from_klines(klines)
            except Exception as exc:  # noqa: BLE001 - 临时网络错误不能中断监控循环。
                state.record_error(str(exc))
            time.sleep(poll_seconds)

    thread = threading.Thread(target=loop, name="binance-kline-poller", daemon=True)
    thread.start()
    return thread


def make_handler(state: MonitorState, warmup_loader=None):
    class MonitorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                self._send_json(state.snapshot())
                return
            if parsed.path == "/api/orders":
                query = parse_qs(parsed.query)
                self._send_json(
                    state.page_orders(
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "page_size", 20),
                        direction=_query_text(query, "direction"),
                        level=_query_text(query, "level"),
                        segment=_query_text(query, "segment"),
                        result=_query_text(query, "result"),
                    )
                )
                return
            if parsed.path == "/api/config":
                query = parse_qs(parsed.query)
                symbol = query.get("symbol", [None])[0]
                if symbol:
                    state.reset_symbol(symbol)
                    if warmup_loader is not None:
                        warmup_loader(state)
                self._send_json({"symbol": state.symbol})
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
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return MonitorHandler


def _query_text(query: dict, name: str) -> str:
    return str(query.get(name, [""])[0] or "").strip()


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
    config = WarmupConfig(
        symbol=state.symbol,
        data_dir=data_dir,
        months=months,
        include_current_month_daily=include_current_month_daily,
        timeout=timeout,
    )
    klines, report = warmup_history(config)
    state.seed_klines(klines, report.to_dict())
    return report


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float | None) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="币安事件合约量价监控程序")
    parser.add_argument("--symbol", default=os.getenv("SYMBOL", "BTCUSDT"), help="交易对，默认: BTCUSDT")
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
        "--data-dir",
        default=os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")),
        help="本地 Binance Vision 缓存目录，默认: ./data",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "monitor.sqlite3")),
        help="SQLite 持久化路径，默认: ./data/monitor.sqlite3",
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
        "--win-return",
        type=float,
        default=_env_float("WIN_RETURN", None),
        help="赢单返还金额，默认: stake * 1.8",
    )
    parser.add_argument(
        "--stake-progression-max-orders",
        type=int,
        default=int(os.getenv("STAKE_PROGRESSION_MAX_ORDERS", "3")),
        help="最大连续滚单次数，默认: 3",
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
        help="关闭赢单返还滚单",
    )
    args = parser.parse_args()
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
        fear_greed_provider=FearGreedProvider(),
        storage_path=None if args.no_persistence else args.db_path,
        webhook=webhook,
        stake=args.stake,
        win_return=win_return,
        enable_stake_progression=not args.no_stake_progression,
        stake_progression_max_orders=args.stake_progression_max_orders,
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

    client = BinanceKlineClient()
    start_polling(state, client, poll_seconds=args.poll_seconds, limit=args.limit)

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

    server = ThreadingHTTPServer((args.host, args.port), make_handler(state, warmup_loader=warmup_loader))
    print(f"monitor: http://{args.host}:{args.port} symbol={state.symbol} poll={args.poll_seconds}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nmonitor stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
