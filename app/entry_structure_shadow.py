from __future__ import annotations

from dataclasses import dataclass
import math
import sys
from typing import Sequence

from app.models import Kline


ENTRY_STRUCTURE_VERSION = "ENTRY_STRUCTURE_SHADOW_V1"
MAX_STRUCTURE_BARS = 240
MAX_PIVOT_LEVELS_PER_KIND = 24
MAX_ROUND_CANDIDATES = 96


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
        if self.bars > MAX_STRUCTURE_BARS:
            raise ValueError(f"bars must be at most {MAX_STRUCTURE_BARS}")


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

    selected = []
    for current in reversed(ordered):
        if (
            not selected
            or int(selected[-1]["index"]) - int(current["index"]) >= minimum_gap
        ):
            selected.append(current)
    return list(reversed(selected))


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
            price_token = "-".join(format(price, ".15g") for price in prices)
            confirmation_token = "-".join(
                str(int(item["confirmed_at"])) for item in independent
            )
            candidates.append(
                {
                    "id": (
                        f"{level_kind.lower()}-{price_token}-{confirmation_token}"
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
    limited = []
    for kind in ("SUPPORT", "RESISTANCE"):
        same_kind = [item for item in dominant if item["kind"] == kind]
        limited.extend(same_kind[:MAX_PIVOT_LEVELS_PER_KIND])
    return sorted(limited, key=lambda item: (item["kind"], item["lower"], item["id"]))


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
    if not klines:
        return []

    buffer = config.rejection_atr * atr
    latest_close = float(klines[-1].close)
    candidates: dict[float, dict[str, float | int]] = {}

    def add_nearby(anchor: float, step: float) -> None:
        if not math.isfinite(anchor):
            return
        quotient = anchor / step
        if not math.isfinite(quotient):
            return
        lower_multiple = math.floor(quotient)
        for multiple in (lower_multiple, lower_multiple + 1):
            try:
                price = float(multiple * step)
            except OverflowError:
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            existing = candidates.get(price)
            if existing is None:
                candidates[price] = {"step": step, "occurrences": 1}
            else:
                existing["step"] = max(float(existing["step"]), step)
                existing["occurrences"] = int(existing["occurrences"]) + 1

    for step in round_steps(symbol):
        for item in klines:
            # Rejection counts change only at these interval boundaries. Sampling
            # adjacent multiples keeps work bounded even for malformed price spans.
            for anchor in (
                float(item.low),
                float(item.close) - buffer,
                float(item.close),
                float(item.close) + buffer,
                float(item.high),
            ):
                add_nearby(anchor, step)

    ranked_candidates = sorted(
        candidates.items(),
        key=lambda item: (
            -int(item[1]["occurrences"]),
            _safe_ratio(abs(item[0] - latest_close), latest_close),
            -float(item[1]["step"]),
            item[0],
        ),
    )[:MAX_ROUND_CANDIDATES]

    levels = []
    for price, metadata in sorted(ranked_candidates):
        step = float(metadata["step"])
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


def _zero_cost() -> tuple[float, int, int, int, int]:
    return (0.0, 0, 0, 0, 0)


def _add_cost(
    left: tuple[float, int, int, int, int],
    right: tuple[float, int, int, int, int],
) -> tuple[float, int, int, int, int]:
    return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]


def _negate_cost(
    cost: tuple[float, int, int, int, int],
) -> tuple[float, int, int, int, int]:
    return tuple(-value for value in cost)  # type: ignore[return-value]


def _maximum_round_matching(
    pivot_levels: Sequence[dict[str, object]],
    round_levels: Sequence[dict[str, object]],
    atr: float,
    config: StructureConfig,
) -> dict[int, int]:
    pivot_order = sorted(
        range(len(pivot_levels)),
        key=lambda index: str(pivot_levels[index]["id"]),
    )
    round_order = sorted(
        range(len(round_levels)),
        key=lambda index: str(round_levels[index]["id"]),
    )
    pivot_rank = {index: rank for rank, index in enumerate(pivot_order)}
    round_rank = {index: rank for rank, index in enumerate(round_order)}

    source = 0
    pivot_offset = 1
    round_offset = pivot_offset + len(pivot_levels)
    sink = round_offset + len(round_levels)
    graph: list[list[list[object]]] = [[] for _ in range(sink + 1)]

    def add_edge(
        start: int,
        end: int,
        cost: tuple[float, int, int, int, int],
    ) -> list[object]:
        forward: list[object] = [end, len(graph[end]), 1, cost]
        reverse: list[object] = [start, len(graph[start]), 0, _negate_cost(cost)]
        graph[start].append(forward)
        graph[end].append(reverse)
        return forward

    for pivot_index in pivot_order:
        add_edge(source, pivot_offset + pivot_index, _zero_cost())
    for round_index in round_order:
        add_edge(round_offset + round_index, sink, _zero_cost())

    match_edges: list[tuple[int, int, list[object]]] = []
    maximum_distance = config.cluster_atr * atr
    for pivot_index in pivot_order:
        level = pivot_levels[pivot_index]
        for round_index in round_order:
            round_level = round_levels[round_index]
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
            if distance > maximum_distance:
                continue
            cost = (
                distance,
                -int(level["pivot_count"]),
                -int(level["last_confirmed_at"]),
                pivot_rank[pivot_index],
                round_rank[round_index],
            )
            edge = add_edge(
                pivot_offset + pivot_index,
                round_offset + round_index,
                cost,
            )
            match_edges.append((pivot_index, round_index, edge))

    node_count = len(graph)
    while True:
        distances: list[tuple[float, int, int, int, int] | None] = [
            None
        ] * node_count
        previous: list[tuple[int, int] | None] = [None] * node_count
        distances[source] = _zero_cost()
        for _ in range(node_count - 1):
            changed = False
            for start, edges in enumerate(graph):
                if distances[start] is None:
                    continue
                for edge_index, edge in enumerate(edges):
                    if int(edge[2]) <= 0:
                        continue
                    end = int(edge[0])
                    candidate = _add_cost(distances[start], edge[3])  # type: ignore[arg-type]
                    if distances[end] is None or candidate < distances[end]:
                        distances[end] = candidate
                        previous[end] = (start, edge_index)
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            break

        node = sink
        while node != source:
            start, edge_index = previous[node]  # type: ignore[misc]
            edge = graph[start][edge_index]
            edge[2] = int(edge[2]) - 1
            reverse = graph[node][int(edge[1])]
            reverse[2] = int(reverse[2]) + 1
            node = start

    return {
        pivot_index: round_index
        for pivot_index, round_index, edge in match_edges
        if int(edge[2]) == 0
    }


def _merge_round_levels(
    pivot_levels: list[dict[str, object]],
    round_levels: list[dict[str, object]],
    atr: float,
    config: StructureConfig,
) -> list[dict[str, object]]:
    result = [dict(item) for item in pivot_levels]
    copied_round_levels = [dict(item) for item in round_levels]
    matching = _maximum_round_matching(
        result,
        copied_round_levels,
        atr,
        config,
    )
    matched_rounds = set(matching.values())
    for pivot_index, round_index in matching.items():
        level = result[pivot_index]
        round_level = copied_round_levels[round_index]
        level["source"] = "MERGED"
        level["round_level_price"] = round_level["round_level_price"]
        level["round_level_step"] = round_level["round_level_step"]
        level["touch_count"] = max(
            int(level["touch_count"]), int(round_level["touch_count"])
        )
        result[pivot_index] = level

    result.extend(
        dict(item)
        for index, item in enumerate(copied_round_levels)
        if index not in matched_rounds
        and item.get("_independently_qualified", False)
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
    previous_open_time = None
    previous_close_time = None
    for item in klines:
        if not isinstance(item, Kline):
            return False
        values = (item.open, item.high, item.low, item.close)
        if any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in values
        ):
            return False
        if type(item.open_time) is not int or type(item.close_time) is not int:
            return False
        if (
            item.open <= 0
            or item.high <= 0
            or item.low <= 0
            or item.close <= 0
            or item.open_time < 0
            or item.open_time >= item.close_time
            or item.high < item.low
        ):
            return False
        if previous_open_time is not None and item.open_time <= previous_open_time:
            return False
        if previous_close_time is not None and item.close_time <= previous_close_time:
            return False
        previous_open_time = item.open_time
        previous_close_time = item.close_time
    return True


def _safe_ratio(numerator: float, denominator: float, scale: float = 1.0) -> float:
    if numerator <= 0 or denominator <= 0:
        return 0.0
    value = numerator / denominator
    if not math.isfinite(value) or value > sys.float_info.max / scale:
        return sys.float_info.max
    return value * scale


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
        "distance_bps": _safe_ratio(price, close, 10_000.0),
        "distance_atr": _safe_ratio(price, atr),
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
