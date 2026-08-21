from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.models import FearGreedContext, Kline


MARKET_EVENT_VERSION = "MARKET_EVENT_V1"
SHADOW_PARAMETER_SNAPSHOT_VERSION = "SHADOW_PARAMETER_SNAPSHOT_V1"


def _canonicalize(value: Any, *, path: str = "parameters") -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} keys must be strings")
            normalized[key] = _canonicalize(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _canonicalize(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{path} contains unsupported value {type(value).__name__}")


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class MarketEvent:
    symbol: str
    generation: int
    kline: Kline
    fear_greed: FearGreedContext
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol must not be empty")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if not isinstance(self.kline, Kline):
            raise ValueError("kline must be a Kline")
        if self.kline.close_time - self.kline.open_time != 59_999:
            raise ValueError("kline must be a closed 1-minute Kline")
        if not isinstance(self.fear_greed, FearGreedContext):
            raise ValueError("fear_greed must be a FearGreedContext snapshot")

        payload = {
            "version": MARKET_EVENT_VERSION,
            "symbol": symbol,
            "generation": self.generation,
            "kline": asdict(self.kline),
        }
        canonical = _canonical_json(_canonicalize(payload, path="event"))
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self,
            "event_id",
            hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "version": MARKET_EVENT_VERSION,
            "symbol": self.symbol,
            "generation": self.generation,
            "kline": asdict(self.kline),
            "fear_greed": asdict(self.fear_greed),
        }


@dataclass(frozen=True, init=False)
class ShadowParameterSnapshot:
    family: str
    version: str
    _parameters_json: str = field(repr=False, compare=False)
    _canonical_json: str = field(repr=False, compare=False)
    parameter_hash: str

    def __init__(
        self,
        *,
        family: str,
        version: str,
        parameters: Mapping[str, Any],
    ) -> None:
        normalized_family = str(family).strip()
        normalized_version = str(version).strip()
        if not normalized_family:
            raise ValueError("family must not be empty")
        if not normalized_version:
            raise ValueError("version must not be empty")
        if not isinstance(parameters, Mapping):
            raise ValueError("parameters must be a mapping")

        normalized_parameters = _canonicalize(parameters)
        parameters_json = _canonical_json(normalized_parameters)
        canonical_json = _canonical_json(
            {
                "snapshot_version": SHADOW_PARAMETER_SNAPSHOT_VERSION,
                "family": normalized_family,
                "version": normalized_version,
                "parameters": normalized_parameters,
            }
        )
        object.__setattr__(self, "family", normalized_family)
        object.__setattr__(self, "version", normalized_version)
        object.__setattr__(self, "_parameters_json", parameters_json)
        object.__setattr__(self, "_canonical_json", canonical_json)
        object.__setattr__(
            self,
            "parameter_hash",
            hashlib.sha256(canonical_json.encode("ascii")).hexdigest(),
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return json.loads(self._parameters_json)

    def to_json(self) -> str:
        return self._canonical_json

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": SHADOW_PARAMETER_SNAPSHOT_VERSION,
            "family": self.family,
            "version": self.version,
            "parameters": self.parameters,
            "parameter_hash": self.parameter_hash,
        }


@dataclass(frozen=True)
class ShadowEvaluationMetrics:
    complete_days: int = 0
    settled_orders: int = 0
    wins: int = 0
    long_orders: int = 0
    long_wins: int = 0
    short_orders: int = 0
    short_wins: int = 0
    qualified_win_rate_days: int = 0
    positive_ev_days: int = 0
    days_beating_champion: int = 0
    average_orders_per_day: float = 0.0
    worst_rolling_3d_win_rate: float = 0.0
    total_ev: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_loss_streak: int = 0
    current_loss_streak: int = 0

    def __post_init__(self) -> None:
        count_fields = (
            "complete_days",
            "settled_orders",
            "wins",
            "long_orders",
            "long_wins",
            "short_orders",
            "short_wins",
            "qualified_win_rate_days",
            "positive_ev_days",
            "days_beating_champion",
            "max_loss_streak",
            "current_loss_streak",
        )
        for name in count_fields:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.wins > self.settled_orders:
            raise ValueError("wins must not exceed settled_orders")
        if self.long_wins > self.long_orders:
            raise ValueError("long_wins must not exceed long_orders")
        if self.short_wins > self.short_orders:
            raise ValueError("short_wins must not exceed short_orders")
        if self.long_orders + self.short_orders != self.settled_orders:
            raise ValueError("direction orders must equal settled_orders")
        if self.long_wins + self.short_wins != self.wins:
            raise ValueError("direction wins must equal wins")
        for name in (
            "qualified_win_rate_days",
            "positive_ev_days",
            "days_beating_champion",
        ):
            if getattr(self, name) > self.complete_days:
                raise ValueError(f"{name} must not exceed complete_days")

        for name in (
            "average_orders_per_day",
            "worst_rolling_3d_win_rate",
            "total_ev",
            "total_pnl",
            "max_drawdown",
        ):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if self.average_orders_per_day < 0:
            raise ValueError("average_orders_per_day must be non-negative")
        if not 0.0 <= self.worst_rolling_3d_win_rate <= 1.0:
            raise ValueError("worst_rolling_3d_win_rate must be between 0 and 1")
        if self.max_drawdown < 0:
            raise ValueError("max_drawdown must be a non-negative magnitude")

    @staticmethod
    def _rate(wins: int, orders: int) -> float:
        return 0.0 if orders == 0 else wins / orders

    @property
    def win_rate(self) -> float:
        return self._rate(self.wins, self.settled_orders)

    @property
    def long_win_rate(self) -> float:
        return self._rate(self.long_wins, self.long_orders)

    @property
    def short_win_rate(self) -> float:
        return self._rate(self.short_wins, self.short_orders)

    @property
    def minimum_direction_win_rate(self) -> float:
        return min(self.long_win_rate, self.short_win_rate)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "win_rate": self.win_rate,
            "long_win_rate": self.long_win_rate,
            "short_win_rate": self.short_win_rate,
            "minimum_direction_win_rate": self.minimum_direction_win_rate,
        }
