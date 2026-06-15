import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from app.models import FearGreedContext, Kline, Signal
from app.order_policy import OrderPolicy
from app.rolling_edge import RollingEdgeConfig, RollingEdgeSnapshot, rolling_edge_snapshot, should_degrade
from app.simulator import AccountSimulator
from app.storage import SQLiteMonitorStore, page_order_list
from app.strategy import LIVE_TRADE_TIMEFRAMES, analyze_volume_price, choose_trade_signal


class MonitorState:
    def __init__(
        self,
        symbol: str,
        max_open_orders: int = 1,
        min_order_gap_ms: int = 10 * 60_000,
        fear_greed_provider=None,
        max_klines: int = 140_000,
        storage_path: str | Path | None = None,
        storage: SQLiteMonitorStore | None = None,
        webhook=None,
        rolling_edge_config: RollingEdgeConfig | None = None,
        enable_rolling_edge_guard: bool = True,
        stake: float = 10.0,
        win_return: float = 18.0,
        enable_stake_progression: bool = True,
        stake_progression_max_orders: int = 3,
    ):
        self.symbol = symbol.upper()
        self.order_policy = OrderPolicy(max_open_orders=max_open_orders, min_order_gap_ms=min_order_gap_ms)
        self.max_klines = max_klines
        self.storage = storage or (SQLiteMonitorStore(storage_path) if storage_path else None)
        self.webhook = webhook
        self.rolling_edge_config = rolling_edge_config or RollingEdgeConfig()
        self.enable_rolling_edge_guard = enable_rolling_edge_guard
        self.stake = stake
        self.win_return = win_return
        self.enable_stake_progression = enable_stake_progression
        self.stake_progression_max_orders = stake_progression_max_orders
        self.webhook_error: str | None = None
        restored_orders = self.storage.load_orders(self.symbol) if self.storage else []
        self.simulator = AccountSimulator(
            stake=self.stake,
            win_return=self.win_return,
            orders=restored_orders,
            enable_stake_progression=self.enable_stake_progression,
            stake_progression_max_orders=self.stake_progression_max_orders,
        )
        self.fear_greed_provider = fear_greed_provider
        self.fear_greed = None
        self.warmup: dict | None = None
        self.risk_pause: str = ""
        self.klines: list[Kline] = []
        self.signals: list[Signal] = []
        self.selected_signal: Signal | None = None
        self.order_decision = "WAIT"
        self.rolling_edge: dict = self._empty_rolling_edge()
        self.last_error: str | None = None
        self.updated_at_ms = 0
        self._opened_signal_keys: set[tuple[int, int, str]] = set()
        self._last_order_opened_at: int | None = None
        self._lock = threading.Lock()
        self._storage_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor-storage")
        self._storage_futures: list[Future] = []

    def update_from_klines(self, klines: Sequence[Kline]) -> None:
        if not klines:
            return

        with self._lock:
            existing = list(self.klines)

        merged_klines = self._merge_klines(existing, klines)
        latest = merged_klines[-1]
        fear_greed = self._fear_greed_context()
        new_signals = [
            analyze_volume_price(merged_klines, timeframe_minutes=minutes, fear_greed=fear_greed)
            for minutes in LIVE_TRADE_TIMEFRAMES
        ]
        selected_signal = choose_trade_signal(merged_klines, fear_greed=fear_greed)

        with self._lock:
            self.fear_greed = fear_greed
            self.klines = merged_klines
            self.signals = new_signals
            self.selected_signal = selected_signal
            self.updated_at_ms = int(time.time() * 1000)
            self.last_error = None
            settled_orders = self.simulator.settle_expired_orders(latest.close_time, latest.close)
            if self.storage:
                for order in settled_orders:
                    self._save_order(order)
                    self._update_order_entry_snapshot_settlement(order)

            self.order_decision = self._maybe_open_order(selected_signal, latest)
            if self.storage:
                self._save_signal(selected_signal, self.order_decision, self.updated_at_ms)

    def seed_klines(self, klines: Sequence[Kline], warmup_report: dict | None = None) -> None:
        with self._lock:
            self.klines = self._merge_klines(self.klines, klines)
            self.warmup = warmup_report
            self.updated_at_ms = int(time.time() * 1000)

    def record_error(self, message: str) -> None:
        with self._lock:
            self.last_error = message
            self.updated_at_ms = int(time.time() * 1000)

    def _fear_greed_context(self) -> FearGreedContext:
        if self.fear_greed_provider is None:
            return FearGreedContext(
                value=50,
                classification="Neutral",
                average_30d=50.0,
                trend="unknown",
                updated_at_ms=int(time.time() * 1000),
                source="neutral",
            )
        return self.fear_greed_provider.get_context()

    def reset_symbol(self, symbol: str) -> None:
        with self._lock:
            self.symbol = symbol.upper()
            restored_orders = self.storage.load_orders(self.symbol) if self.storage else []
            self.simulator = AccountSimulator(
                stake=self.stake,
                win_return=self.win_return,
                orders=restored_orders,
                enable_stake_progression=self.enable_stake_progression,
                stake_progression_max_orders=self.stake_progression_max_orders,
            )
            self.fear_greed = None
            self.warmup = None
            self.risk_pause = ""
            self.webhook_error = None
            self.klines = []
            self.signals = []
            self.selected_signal = None
            self.order_decision = "WAIT"
            self.rolling_edge = self._empty_rolling_edge()
            self.last_error = None
            self.updated_at_ms = int(time.time() * 1000)
            self._opened_signal_keys.clear()
            self._last_order_opened_at = None

    def _merge_klines(self, existing: Sequence[Kline], incoming: Sequence[Kline]) -> list[Kline]:
        merged = {item.open_time: item for item in existing}
        for item in incoming:
            merged[item.open_time] = item
        ordered = sorted(merged.values(), key=lambda item: item.open_time)
        if self.max_klines > 0 and len(ordered) > self.max_klines:
            return ordered[-self.max_klines :]
        return ordered

    def _maybe_open_order(self, signal: Signal, latest: Kline) -> str:
        self.rolling_edge = self._rolling_edge_status(signal, latest)
        gate = self.order_policy.evaluate(
            signal,
            latest,
            self.simulator.orders,
            self._last_order_opened_at,
            self._opened_signal_keys,
        )
        self.risk_pause = gate.risk_pause
        if not gate.open_allowed:
            return gate.code
        if signal.direction == "SHORT":
            self.risk_pause = "SHORT观察模式：仅记录信号，不开模拟订单，不推送Webhook"
            return "SHORT_OBSERVE_ONLY"
        if self.enable_rolling_edge_guard and self.rolling_edge["status"] == "DEGRADED":
            self.risk_pause = (
                f"滚动优势衰退 {self.rolling_edge['key']} "
                f"样本 {self.rolling_edge['sample_size']} 胜率 {self.rolling_edge['win_rate']:.2%} "
                f"EV {self.rolling_edge['ev']:.2f}，暂停开单"
            )
            return "ROLLING_EDGE_BLOCKED"

        order = self.simulator.open_order(signal, entry_price=latest.close, opened_at=latest.close_time)
        if self.storage:
            self._save_order(order)
            self._save_order_entry_snapshot(order, signal, latest)
        self._send_webhook(signal, order)
        if gate.signal_key:
            self._opened_signal_keys.add(gate.signal_key)
        self._last_order_opened_at = latest.close_time
        return gate.code

    def _rolling_edge_status(self, signal: Signal, latest: Kline) -> dict:
        current_item = {
            "entry_time": latest.close_time,
            "timeframe_minutes": signal.timeframe_minutes,
            "threshold_segment": signal.threshold_segment,
            "reason": signal.reason,
        }
        snapshot = rolling_edge_snapshot(self.simulator.orders, current_item, self.rolling_edge_config)
        degraded = should_degrade(snapshot, self.rolling_edge_config)
        return self._rolling_edge_to_dict(snapshot, degraded, self.enable_rolling_edge_guard)

    @staticmethod
    def _rolling_edge_to_dict(snapshot: RollingEdgeSnapshot, degraded: bool, guard_enabled: bool) -> dict:
        return {
            "observe_only": not guard_enabled,
            "status": "DEGRADED" if degraded else "NORMAL",
            "key": snapshot.key,
            "sample_size": snapshot.sample_size,
            "wins": snapshot.wins,
            "losses": snapshot.losses,
            "win_rate": snapshot.win_rate,
            "pnl": snapshot.pnl,
            "ev": snapshot.ev,
        }

    def _save_order(self, order) -> None:
        order_snapshot = replace(order)
        self._submit_storage_write(lambda: self.storage.save_order(order_snapshot, self.symbol))

    def _save_signal(self, signal: Signal, decision: str, created_at_ms: int) -> None:
        self._submit_storage_write(lambda: self.storage.save_signal(self.symbol, signal, decision, created_at_ms))

    def _save_order_entry_snapshot(self, order, signal: Signal, latest: Kline) -> None:
        order_snapshot = replace(order)
        entry_snapshot = {
            "signal": signal.to_dict(),
            "rolling_edge": dict(self.rolling_edge),
            "latest_kline": latest.to_dict(),
            "fear_greed": self.fear_greed.to_dict() if self.fear_greed else None,
            "stake_config": {
                "stake": self.stake,
                "win_return": self.win_return,
                "stake_progression_enabled": self.enable_stake_progression,
                "stake_progression_max_orders": self.stake_progression_max_orders,
            },
            "order_policy": {
                "max_open_orders": self.order_policy.max_open_orders,
                "min_order_gap_ms": self.order_policy.min_order_gap_ms,
            },
        }
        self._submit_storage_write(
            lambda: self.storage.save_order_entry_snapshot(order_snapshot, self.symbol, entry_snapshot)
        )

    def _update_order_entry_snapshot_settlement(self, order) -> None:
        order_snapshot = replace(order)
        self._submit_storage_write(
            lambda: self.storage.update_order_entry_snapshot_settlement(order_snapshot, self.symbol)
        )

    def _submit_storage_write(self, func) -> None:
        if not self.storage:
            return
        self._storage_futures.append(self._storage_executor.submit(func))

    def wait_for_storage_writes(self) -> None:
        pending = list(self._storage_futures)
        self._storage_futures.clear()
        for future in pending:
            future.result()

    def _empty_rolling_edge(self) -> dict:
        return {
            "observe_only": not self.enable_rolling_edge_guard,
            "status": "UNKNOWN",
            "key": "",
            "sample_size": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "ev": 0.0,
        }

    def _send_webhook(self, signal: Signal, order=None) -> None:
        if not self.webhook:
            return
        try:
            self.webhook.send_signal(self.symbol, signal, amount=order.stake if order else None)
        except Exception as exc:  # noqa: BLE001 - 外部推送只是副作用，不能中断监控。
            self.webhook_error = str(exc)
        else:
            self.webhook_error = None

    def snapshot(self) -> dict:
        with self._lock:
            latest = self.klines[-1] if self.klines else None
            orders = list(reversed(self.simulator.orders[-100:]))
            return {
                "symbol": self.symbol,
                "updated_at_ms": self.updated_at_ms,
                "last_error": self.last_error,
                "latest_price": latest.close if latest else None,
                "latest_kline": latest.to_dict() if latest else None,
                "fear_greed": self.fear_greed.to_dict() if self.fear_greed else None,
                "warmup": self.warmup,
                "risk_pause": self.risk_pause,
                "rolling_edge": self.rolling_edge,
                "webhook": self.webhook.status() if self.webhook else {"enabled": False, "last_error": None},
                "webhook_error": self.webhook_error,
                "signals": [signal.to_dict() for signal in self.signals],
                "selected_signal": self.selected_signal.to_dict() if self.selected_signal else None,
                "order_decision": self.order_decision,
                "stats": self.simulator.stats(),
                "orders": [order.to_dict() for order in orders],
                "kline_count": len(self.klines),
            }

    def page_orders(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        level: str = "",
        segment: str = "",
        result: str = "",
    ) -> dict:
        if self.storage:
            return self.storage.page_orders(
                self.symbol,
                page=page,
                page_size=page_size,
                direction=direction,
                level=level,
                segment=segment,
                result=result,
            )
        with self._lock:
            orders = list(self.simulator.orders)
        return page_order_list(
            orders,
            page=page,
            page_size=page_size,
            direction=direction,
            level=level,
            segment=segment,
            result=result,
        )
