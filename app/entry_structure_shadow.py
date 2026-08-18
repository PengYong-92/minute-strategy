from __future__ import annotations

from dataclasses import dataclass
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


def _price_groups(
    pivots: Sequence[dict[str, object]],
    maximum_width: float,
) -> list[list[dict[str, object]]]:
    groups: list[list[dict[str, object]]] = []
    for pivot in sorted(pivots, key=lambda item: (float(item["price"]), int(item["index"]))):
        placed = False
        for group in groups:
            prices = [float(item["price"]) for item in group]
            if max([*prices, float(pivot["price"])]) - min(
                [*prices, float(pivot["price"])]
            ) <= maximum_width:
                group.append(pivot)
                placed = True
                break
        if not placed:
            groups.append([pivot])
    return groups


def _independent_pivots(
    pivots: Sequence[dict[str, object]],
    minimum_gap: int,
) -> list[dict[str, object]]:
    selected = []
    for pivot in sorted(pivots, key=lambda item: int(item["index"])):
        if not selected or int(pivot["index"]) - int(selected[-1]["index"]) >= minimum_gap:
            selected.append(pivot)
    return selected


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
    levels = []
    maximum_width = config.cluster_atr * atr
    for pivot_kind, level_kind in (("LOW", "SUPPORT"), ("HIGH", "RESISTANCE")):
        same_kind = [item for item in pivots if item["kind"] == pivot_kind]
        for group in _price_groups(same_kind, maximum_width):
            independent = _independent_pivots(group, config.min_pivot_gap)
            if len(independent) < config.min_pivots:
                continue
            prices = [float(item["price"]) for item in independent]
            lower = min(prices)
            upper = max(prices)
            touch_count, touch_indexes = _touch_count(
                klines,
                lower,
                upper,
                config.min_pivot_gap,
            )
            levels.append(
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
    return levels


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
    merged_round_ids = set()
    result = [dict(item) for item in pivot_levels]
    for index, level in enumerate(result):
        matches = []
        for round_level in round_levels:
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
                matches.append((distance, price, round_level))
        if matches:
            _, _, closest = min(matches, key=lambda item: (item[0], item[1]))
            merged_round_ids.add(closest["id"])
            level["source"] = "MERGED"
            level["round_level_price"] = closest["round_level_price"]
            level["round_level_step"] = closest["round_level_step"]
            level["touch_count"] = max(
                int(level["touch_count"]), int(closest["touch_count"])
            )
            result[index] = level
    result.extend(
        item
        for item in round_levels
        if item["id"] not in merged_round_ids
        and item["_independently_qualified"]
    )
    for item in result:
        item.pop("_independently_qualified", None)
    return result


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
        evaluated_at = int(scoped[-1].close_time) if scoped else 0
        atr = _atr(scoped, self.config.atr_period)
        if len(scoped) < self.config.minimum_bars or atr <= 0:
            return {
                "version": ENTRY_STRUCTURE_VERSION,
                "mode": "SHADOW_ONLY",
                "status": "INSUFFICIENT_DATA",
                "evaluated_at": evaluated_at,
                "bars": len(scoped),
                "atr": atr,
                "levels": [],
                "support": [],
                "resistance": [],
                "nearest_support": None,
                "nearest_resistance": None,
            }

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
