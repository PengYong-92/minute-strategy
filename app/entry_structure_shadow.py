from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from app.models import Kline


ENTRY_STRUCTURE_VERSION = "ENTRY_STRUCTURE_SHADOW_V1"


@dataclass(frozen=True)
class StructureConfig:
    bars: int = 240
    atr_period: int = 14
    pivot_left: int = 3
    pivot_right: int = 3
    cluster_atr: float = 0.25
    min_pivots: int = 2
    min_pivot_gap: int = 5
    minimum_bars: int = 20
    rejection_atr: float = 0.05

    def __post_init__(self) -> None:
        integer_fields = (
            "bars",
            "atr_period",
            "pivot_left",
            "pivot_right",
            "min_pivots",
            "min_pivot_gap",
            "minimum_bars",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        for name in ("cluster_atr", "rejection_atr"):
            value = getattr(self, name)
            if type(value) not in (int, float):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

        pivot_history = (
            self.pivot_left
            + self.pivot_right
            + 1
            + (self.min_pivots - 1) * self.min_pivot_gap
        )
        required_history = max(self.atr_period + 1, pivot_history)
        if self.minimum_bars < required_history:
            raise ValueError(
                "minimum_bars is insufficient for ATR and independent pivots"
            )
        if self.bars < self.minimum_bars:
            raise ValueError("bars must be at least minimum_bars")


def round_steps(symbol: str) -> tuple[float, float, float]:
    normalized = str(symbol).upper()
    if normalized.startswith("ETH"):
        return (10.0, 50.0, 100.0)
    return (100.0, 500.0, 1000.0)


def _atr(klines: Sequence[Kline], period: int) -> float:
    if len(klines) < period + 1:
        return 0.0
    true_ranges = []
    for index in range(1, len(klines)):
        item = klines[index]
        previous_close = klines[index - 1].close
        true_ranges.append(
            max(
                item.high - item.low,
                abs(item.high - previous_close),
                abs(item.low - previous_close),
            )
        )
    return sum(true_ranges[-period:]) / period


def _confirmed_pivots(
    klines: Sequence[Kline],
    config: StructureConfig,
) -> list[dict[str, object]]:
    pivots = []
    start = config.pivot_left
    stop = len(klines) - config.pivot_right
    for index in range(start, stop):
        current = klines[index]
        neighbors = (
            *klines[index - config.pivot_left : index],
            *klines[index + 1 : index + config.pivot_right + 1],
        )
        if all(current.low < item.low for item in neighbors):
            pivots.append(
                {
                    "kind": "LOW",
                    "index": index,
                    "price": float(current.low),
                    "confirmed_at": int(
                        klines[index + config.pivot_right].close_time
                    ),
                }
            )
        if all(current.high > item.high for item in neighbors):
            pivots.append(
                {
                    "kind": "HIGH",
                    "index": index,
                    "price": float(current.high),
                    "confirmed_at": int(
                        klines[index + config.pivot_right].close_time
                    ),
                }
            )
    return pivots


def _optimal_independent_pivots(
    pivots: Sequence[dict[str, object]],
    minimum_gap: int,
) -> list[dict[str, object]]:
    ordered = sorted(
        pivots,
        key=lambda item: (
            int(item["index"]),
            float(item["price"]),
            int(item["confirmed_at"]),
        ),
    )
    if not ordered:
        return []

    def selection_key(selection: tuple[dict[str, object], ...]) -> tuple:
        return (
            len(selection),
            int(selection[-1]["confirmed_at"]),
            tuple(int(item["index"]) for item in selection),
            tuple(float(item["price"]) for item in selection),
        )

    best_ending_at: list[tuple[dict[str, object], ...]] = []
    for index, current in enumerate(ordered):
        candidates = [(current,)]
        for previous_index in range(index):
            previous = ordered[previous_index]
            if int(current["index"]) - int(previous["index"]) >= minimum_gap:
                candidates.append((*best_ending_at[previous_index], current))
        best_ending_at.append(max(candidates, key=selection_key))
    return list(max(best_ending_at, key=selection_key))


def _touch_count(
    klines: Sequence[Kline],
    lower: float,
    upper: float,
    minimum_gap: int,
) -> tuple[int, tuple[int, ...]]:
    touches = []
    for index, item in enumerate(klines):
        if item.low <= upper and item.high >= lower:
            if not touches or index - touches[-1] >= minimum_gap:
                touches.append(index)
    return len(touches), tuple(touches)


def _cluster_pivots(
    klines: Sequence[Kline],
    pivots: Sequence[dict[str, object]],
    atr: float,
    config: StructureConfig,
) -> list[dict[str, object]]:
    candidates = []
    maximum_width = config.cluster_atr * atr
    for pivot_kind, level_kind in (("LOW", "SUPPORT"), ("HIGH", "RESISTANCE")):
        same_kind = sorted(
            (item for item in pivots if item["kind"] == pivot_kind),
            key=lambda item: (float(item["price"]), int(item["index"])),
        )
        seen_subsets = set()
        right = 0
        for left in range(len(same_kind)):
            right = max(right, left)
            while (
                right + 1 < len(same_kind)
                and float(same_kind[right + 1]["price"])
                - float(same_kind[left]["price"])
                <= maximum_width
            ):
                right += 1
            independent = _optimal_independent_pivots(
                same_kind[left : right + 1],
                config.min_pivot_gap,
            )
            if len(independent) < config.min_pivots:
                continue
            independent.sort(key=lambda item: int(item["index"]))
            subset_key = tuple(int(item["index"]) for item in independent)
            if subset_key in seen_subsets:
                continue
            seen_subsets.add(subset_key)
            prices = [float(item["price"]) for item in independent]
            lower = min(prices)
            upper = max(prices)
            touch_count, touch_indexes = _touch_count(
                klines,
                lower,
                upper,
                config.min_pivot_gap,
            )
            candidates.append(
                {
                    "id": (
                        f"{level_kind.lower()}-"
                        f"{independent[0]['index']}-{independent[-1]['index']}"
                    ),
                    "kind": level_kind,
                    "source": "PIVOT",
                    "lower": lower,
                    "upper": upper,
                    "pivot_count": len(independent),
                    "pivot_gap": int(independent[-1]["index"])
                    - int(independent[0]["index"]),
                    "pivot_indexes": tuple(int(item["index"]) for item in independent),
                    "pivots": tuple(dict(item) for item in independent),
                    "touch_count": max(touch_count, len(independent)),
                    "touch_indexes": touch_indexes,
                    "first_confirmed_at": int(independent[0]["confirmed_at"]),
                    "last_confirmed_at": int(independent[-1]["confirmed_at"]),
                    "round_level_price": None,
                    "round_level_step": None,
                }
            )

    dominant = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            item["kind"],
            -int(item["pivot_count"]),
            -int(item["last_confirmed_at"]),
            float(item["upper"]) - float(item["lower"]),
            str(item["id"]),
        ),
    ):
        overlaps = any(
            existing["kind"] == candidate["kind"]
            and float(candidate["lower"]) <= float(existing["upper"])
            and float(existing["lower"]) <= float(candidate["upper"])
            for existing in dominant
        )
        if not overlaps:
            dominant.append(candidate)
    return sorted(
        dominant,
        key=lambda item: (item["kind"], item["lower"], item["id"]),
    )


def _independent_rejections(
    klines: Sequence[Kline],
    price: float,
    atr: float,
    kind: str,
    config: StructureConfig,
) -> tuple[int, tuple[int, ...]]:
    indexes = []
    buffer = config.rejection_atr * atr
    for index, item in enumerate(klines):
        if kind == "SUPPORT":
            rejected = item.low <= price and item.close > price + buffer
        else:
            rejected = item.high >= price and item.close < price - buffer
        if rejected and (not indexes or index - indexes[-1] >= config.min_pivot_gap):
            indexes.append(index)
    return len(indexes), tuple(indexes)


def _round_candidates(
    symbol: str,
    klines: Sequence[Kline],
    atr: float,
    config: StructureConfig,
) -> list[dict[str, object]]:
    minimum = min(item.low for item in klines)
    maximum = max(item.high for item in klines)
    candidates: dict[float, float] = {}
    for step in round_steps(symbol):
        first = int(minimum // step) - 1
        last = int(maximum // step) + 1
        for multiple in range(first, last + 1):
            price = float(multiple * step)
            if minimum - atr <= price <= maximum + atr:
                candidates.setdefault(price, step)

    levels = []
    for price, step in sorted(candidates.items()):
        for kind in ("SUPPORT", "RESISTANCE"):
            count, indexes = _independent_rejections(
                klines,
                price,
                atr,
                kind,
                config,
            )
            levels.append(
                {
                    "id": f"round-{kind.lower()}-{price:g}",
                    "kind": kind,
                    "source": "ROUND",
                    "lower": price,
                    "upper": price,
                    "pivot_count": 0,
                    "pivot_gap": 0,
                    "pivot_indexes": (),
                    "pivots": (),
                    "touch_count": count,
                    "touch_indexes": indexes,
                    "first_confirmed_at": (
                        int(klines[indexes[0]].close_time) if indexes else 0
                    ),
                    "last_confirmed_at": (
                        int(klines[indexes[-1]].close_time) if indexes else 0
                    ),
                    "round_level_price": price,
                    "round_level_step": step,
                    "_independently_qualified": count >= config.min_pivots,
                }
            )
    return levels


def _merge_round_levels(
    pivot_levels: list[dict[str, object]],
    round_levels: list[dict[str, object]],
    atr: float,
    config: StructureConfig,
) -> list[dict[str, object]]:
    result = [dict(item) for item in pivot_levels]
    matches = []
    for pivot_index, level in enumerate(result):
        for round_index, round_level in enumerate(round_levels):
            if round_level["kind"] != level["kind"]:
                continue
            price = float(round_level["round_level_price"])
            distance = (
                float(level["lower"]) - price
                if price < float(level["lower"])
                else price - float(level["upper"])
                if price > float(level["upper"])
                else 0.0
            )
            if distance <= config.cluster_atr * atr:
                matches.append(
                    (
                        distance,
                        -int(level["pivot_count"]),
                        -int(level["last_confirmed_at"]),
                        str(level["id"]),
                        str(round_level["id"]),
                        pivot_index,
                        round_index,
                    )
                )

    matched_pivots = set()
    matched_rounds = set()
    for *_, pivot_index, round_index in sorted(matches):
        if pivot_index in matched_pivots or round_index in matched_rounds:
            continue
        level = result[pivot_index]
        round_level = round_levels[round_index]
        level["source"] = "MERGED"
        level["round_level_price"] = round_level["round_level_price"]
        level["round_level_step"] = round_level["round_level_step"]
        level["touch_count"] = max(
            int(level["touch_count"]), int(round_level["touch_count"])
        )
        result[pivot_index] = level
        matched_pivots.add(pivot_index)
        matched_rounds.add(round_index)

    result.extend(
        item
        for index, item in enumerate(round_levels)
        if index not in matched_rounds
        and item["_independently_qualified"]
    )
    for item in result:
        item.pop("_independently_qualified", None)
    return result


def _safe_snapshot(
    scoped: Sequence[Kline],
    *,
    evaluated_at: int = 0,
    atr: float = 0.0,
) -> dict[str, object]:
    return {
        "version": ENTRY_STRUCTURE_VERSION,
        "mode": "SHADOW_ONLY",
        "status": "INSUFFICIENT_DATA",
        "evaluated_at": evaluated_at,
        "bars": len(scoped),
        "atr": atr if math.isfinite(atr) and atr >= 0 else 0.0,
        "levels": [],
        "support": [],
        "resistance": [],
        "nearest_support": None,
        "nearest_resistance": None,
    }


def _valid_scoped_klines(klines: Sequence[Kline]) -> bool:
    for item in klines:
        if not isinstance(item, Kline):
            return False
        values = (item.open, item.high, item.low, item.close, item.close_time)
        if any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in values
        ):
            return False
        if (
            item.open <= 0
            or item.high <= 0
            or item.low <= 0
            or item.close <= 0
            or item.close_time < 0
            or item.high < item.low
        ):
            return False
    return True


def _distance(close: float, level: dict[str, object], atr: float) -> dict[str, float]:
    lower = float(level["lower"])
    upper = float(level["upper"])
    if close < lower:
        price = lower - close
    elif close > upper:
        price = close - upper
    else:
        price = 0.0
    return {
        "distance_price": price,
        "distance_bps": price / close * 10_000 if close > 0 else 0.0,
        "distance_atr": price / atr if atr > 0 else 0.0,
    }


class StructureDetector:
    def __init__(self, config: StructureConfig | None = None):
        self.config = config or StructureConfig()

    def detect(self, symbol: str, closed_klines: Sequence[Kline]) -> dict[str, object]:
        scoped = tuple(closed_klines[-self.config.bars :])
        if not _valid_scoped_klines(scoped):
            return _safe_snapshot(scoped)
        evaluated_at = int(scoped[-1].close_time) if scoped else 0
        atr = _atr(scoped, self.config.atr_period)
        if (
            len(scoped) < self.config.minimum_bars
            or not math.isfinite(atr)
            or atr <= 0
        ):
            return _safe_snapshot(scoped, evaluated_at=evaluated_at, atr=atr)

        pivots = _confirmed_pivots(scoped, self.config)
        pivot_levels = _cluster_pivots(scoped, pivots, atr, self.config)
        round_levels = _round_candidates(symbol, scoped, atr, self.config)
        levels = _merge_round_levels(
            pivot_levels,
            round_levels,
            atr,
            self.config,
        )
        close = float(scoped[-1].close)
        enriched = [
            {**item, **_distance(close, item, atr)}
            for item in levels
        ]
        support = sorted(
            (item for item in enriched if item["kind"] == "SUPPORT"),
            key=lambda item: (
                item["distance_atr"],
                -int(item["pivot_count"]),
                -int(item["last_confirmed_at"]),
                item["lower"],
            ),
        )
        resistance = sorted(
            (item for item in enriched if item["kind"] == "RESISTANCE"),
            key=lambda item: (
                item["distance_atr"],
                -int(item["pivot_count"]),
                -int(item["last_confirmed_at"]),
                item["lower"],
            ),
        )
        return {
            "version": ENTRY_STRUCTURE_VERSION,
            "mode": "SHADOW_ONLY",
            "status": "READY",
            "evaluated_at": evaluated_at,
            "bars": len(scoped),
            "atr": atr,
            "levels": sorted(
                enriched,
                key=lambda item: (item["kind"], item["lower"], item["id"]),
            ),
            "support": support,
            "resistance": resistance,
            "nearest_support": support[0] if support else None,
            "nearest_resistance": resistance[0] if resistance else None,
        }
