from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import sys
from typing import Sequence

from app.models import Kline, Signal


ENTRY_STRUCTURE_VERSION = "ENTRY_STRUCTURE_SHADOW_V1"
MAX_STRUCTURE_BARS = 240
MAX_QUALIFIED_ROUND_LEVELS_PER_KIND = 96


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
    approach_atr: float = 0.35
    breakout_atr: float = 0.10
    breakout_confirm_bars: int = 2
    retest_window_bars: int = 5
    invalidation_atr: float = 0.35
    invalidation_bars: int = 3

    def __post_init__(self) -> None:
        integer_fields = (
            "bars",
            "atr_period",
            "pivot_left",
            "pivot_right",
            "min_pivots",
            "min_pivot_gap",
            "minimum_bars",
            "breakout_confirm_bars",
            "retest_window_bars",
            "invalidation_bars",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        for name in (
            "cluster_atr",
            "rejection_atr",
            "approach_atr",
            "breakout_atr",
            "invalidation_atr",
        ):
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
            price_token = "-".join(format(price, ".17g") for price in prices)
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
    pivot_levels: Sequence[dict[str, object]] = (),
) -> list[dict[str, object]]:
    if not klines:
        return []

    buffer = config.rejection_atr * atr
    latest_close = float(klines[-1].close)
    maximum_merge_distance = config.cluster_atr * atr
    candidates: dict[float, float] = {}
    merge_candidates = set()

    def add_nearby(anchor: float, step: float, *, merge_candidate: bool = False) -> None:
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
            candidates[price] = max(candidates.get(price, 0.0), step)
            if merge_candidate:
                merge_candidates.add(price)

    for step in round_steps(symbol):
        add_nearby(latest_close, step)
        for item in klines:
            # Rejection truth changes only at these interval boundaries. The
            # current close contributes the nearest discrete level on each side.
            for anchor in (
                float(item.low),
                float(item.close) - buffer,
                float(item.close),
                float(item.close) + buffer,
                float(item.high),
            ):
                add_nearby(anchor, step)

        for level in pivot_levels:
            kind = str(level.get("kind", ""))
            if kind not in ("SUPPORT", "RESISTANCE"):
                continue
            lower = float(level["lower"])
            upper = float(level["upper"])
            for anchor in (
                lower - maximum_merge_distance,
                lower,
                (lower + upper) / 2.0,
                upper,
                upper + maximum_merge_distance,
            ):
                add_nearby(anchor, step, merge_candidate=True)

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
                    "id": f"round-{kind.lower()}-{format(price, '.17g')}",
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
                    "_emit_independently": False,
                }
            )

    selected: dict[tuple[str, float], dict[str, object]] = {}
    for kind in ("SUPPORT", "RESISTANCE"):
        qualified = sorted(
            (
                level
                for level in levels
                if level["kind"] == kind
                and level["_independently_qualified"]
            ),
            key=lambda level: (
                abs(float(level["round_level_price"]) - latest_close),
                -int(level["touch_count"]),
                -int(level["last_confirmed_at"]),
                -float(level["round_level_step"]),
                float(level["round_level_price"]),
            ),
        )[:MAX_QUALIFIED_ROUND_LEVELS_PER_KIND]
        for level in qualified:
            level["_emit_independently"] = True
            selected[(kind, float(level["round_level_price"]))] = level

    zones_by_kind = {
        kind: [level for level in pivot_levels if level.get("kind") == kind]
        for kind in ("SUPPORT", "RESISTANCE")
    }
    for level in levels:
        price = float(level["round_level_price"])
        kind = str(level["kind"])
        if price in merge_candidates and any(
            _within_distance_limit(
                _price_to_zone_distance(price, zone),
                maximum_merge_distance,
            )
            for zone in zones_by_kind[kind]
        ):
            selected[(kind, price)] = level

    return sorted(
        selected.values(),
        key=lambda level: (
            str(level["kind"]),
            float(level["round_level_price"]),
            str(level["id"]),
        ),
    )


def _price_to_zone_distance(price: float, level: dict[str, object]) -> float:
    lower = float(level["lower"])
    upper = float(level["upper"])
    if price < lower:
        return lower - price
    if price > upper:
        return price - upper
    return 0.0


def _within_distance_limit(distance: float, maximum_distance: float) -> bool:
    if (
        not math.isfinite(distance)
        or not math.isfinite(maximum_distance)
        or distance < 0
        or maximum_distance < 0
    ):
        return False
    if distance <= maximum_distance:
        return True
    return math.isclose(
        distance,
        maximum_distance,
        rel_tol=1e-12,
        abs_tol=0.0,
    )


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
            distance = _price_to_zone_distance(price, level)
            if not _within_distance_limit(distance, maximum_distance):
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
        and item.get("_emit_independently", False)
    )
    for item in result:
        item.pop("_independently_qualified", None)
        item.pop("_emit_independently", None)
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
            or not item.low <= item.open <= item.high
            or not item.low <= item.close <= item.high
        ):
            return False
        if previous_open_time is not None and item.open_time <= previous_open_time:
            return False
        if previous_close_time is not None and item.close_time <= previous_close_time:
            return False
        if previous_close_time is not None and item.open_time < previous_close_time:
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
        round_levels = _round_candidates(
            symbol,
            scoped,
            atr,
            self.config,
            pivot_levels,
        )
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


def _finite_number(value: object, *, minimum: float = 0.0) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(float(value))
        and float(value) >= minimum
    )


def _error_evidence(reason_code: str) -> dict[str, object]:
    return {
        "id": "structure-error",
        "kind": "",
        "source": "",
        "lower": 0.0,
        "upper": 0.0,
        "pivot_count": 0,
        "touch_count": 0,
        "last_confirmed_at": 0,
        "distance_price": 0.0,
        "distance_bps": 0.0,
        "distance_atr": 0.0,
        "round_level_price": None,
        "round_level_step": None,
        "state": "ERROR",
        "breakout_direction": "NONE",
        "breakout_closed_bars": 0,
        "breakout_buffer_atr": 0.10,
        "retest_status": "NOT_APPLICABLE",
        "reason_code": reason_code,
    }


def _valid_level(level: object, evaluated_at: int) -> bool:
    if not isinstance(level, Mapping):
        return False
    kind = level.get("kind")
    lower = level.get("lower")
    upper = level.get("upper")
    touches = level.get("touch_count")
    confirmed_at = level.get("last_confirmed_at")
    if kind not in ("SUPPORT", "RESISTANCE"):
        return False
    if not _finite_number(lower) or not _finite_number(upper):
        return False
    if float(lower) <= 0 or float(upper) < float(lower):
        return False
    if type(touches) is not int or touches < 0:
        return False
    if (
        type(confirmed_at) is not int
        or confirmed_at < 0
        or confirmed_at > evaluated_at
    ):
        return False
    for name in ("distance_price", "distance_bps", "distance_atr"):
        if not _finite_number(level.get(name)):
            return False
    if not str(level.get("id", "")):
        return False
    return True


def _causal_bars(
    closed_klines: Sequence[Kline],
    evaluated_at: int,
) -> tuple[Kline, ...] | None:
    causal = []
    for item in closed_klines:
        if not isinstance(item, Kline) or type(item.close_time) is not int:
            return None
        if item.close_time <= evaluated_at:
            causal.append(item)
    scoped = tuple(causal[-MAX_STRUCTURE_BARS:])
    if (
        not scoped
        or scoped[-1].close_time != evaluated_at
        or not _valid_scoped_klines(scoped)
    ):
        return None
    return scoped


def _at_or_below(value: float, boundary: float) -> bool:
    return value < boundary or math.isclose(
        value,
        boundary,
        rel_tol=1e-12,
        abs_tol=0.0,
    )


def _at_or_above(value: float, boundary: float) -> bool:
    return value > boundary or math.isclose(
        value,
        boundary,
        rel_tol=1e-12,
        abs_tol=0.0,
    )


def _classify_level_state(
    level: Mapping[str, object],
    bars: Sequence[Kline],
    atr: float,
    config: StructureConfig,
) -> dict[str, object]:
    result = dict(level)
    kind = str(level["kind"])
    lower = float(level["lower"])
    upper = float(level["upper"])
    confirmed_at = int(level["last_confirmed_at"])
    relevant = tuple(item for item in bars if item.close_time >= confirmed_at)
    if not relevant:
        return _error_evidence("STRUCTURE_LEVEL_HAS_NO_CAUSAL_BARS")

    breakout_direction = "DOWN" if kind == "SUPPORT" else "UP"
    breakout_buffer = config.breakout_atr * atr
    rejection_buffer = config.rejection_atr * atr
    invalidation_buffer = config.invalidation_atr * atr
    lifecycle = "IDLE"
    state = "NO_NEARBY_LEVEL"
    retest_status = "NOT_APPLICABLE"
    breakout_count = 0
    confirmed_index: int | None = None
    deep_count = 0

    def is_breakout(item: Kline) -> bool:
        if kind == "SUPPORT":
            return _at_or_below(item.close, lower - breakout_buffer)
        return _at_or_above(item.close, upper + breakout_buffer)

    def is_deep_invalidation(item: Kline) -> bool:
        if kind == "SUPPORT":
            return _at_or_below(item.close, lower - invalidation_buffer)
        return _at_or_above(item.close, upper + invalidation_buffer)

    def is_reclaimed(item: Kline) -> bool:
        if kind == "SUPPORT":
            return _at_or_above(item.close, lower)
        return _at_or_below(item.close, upper)

    def is_retest_held(item: Kline) -> bool:
        if kind == "SUPPORT":
            return _at_or_below(item.close, lower - rejection_buffer)
        return _at_or_above(item.close, upper + rejection_buffer)

    for index, item in enumerate(relevant):
        deep_count = deep_count + 1 if is_deep_invalidation(item) else 0
        if deep_count >= config.invalidation_bars:
            state = "LEVEL_INVALIDATED"
            lifecycle = "INVALIDATED"
            retest_status = "FAILED"
            break

        breakout = is_breakout(item)
        intersects = item.low <= upper and item.high >= lower

        if lifecycle in ("IDLE", "FALSE"):
            if breakout:
                lifecycle = "PENDING"
                breakout_count = 1
                confirmed_index = None
                state = "BREAKOUT_PENDING"
                retest_status = "NOT_APPLICABLE"
                continue
            if lifecycle == "FALSE":
                state = "FALSE_BREAKOUT"
                retest_status = "FAILED"
                continue
            rejected = (
                intersects
                and (
                    _at_or_above(item.close, upper + rejection_buffer)
                    if kind == "SUPPORT"
                    else _at_or_below(item.close, lower - rejection_buffer)
                )
            )
            if rejected:
                state = (
                    "SUPPORT_REJECTED"
                    if kind == "SUPPORT"
                    else "RESISTANCE_REJECTED"
                )
            elif _within_distance_limit(
                _price_to_zone_distance(item.close, result),
                config.approach_atr * atr,
            ):
                state = (
                    "APPROACHING_SUPPORT"
                    if kind == "SUPPORT"
                    else "APPROACHING_RESISTANCE"
                )
            else:
                state = "NO_NEARBY_LEVEL"
            retest_status = "NOT_APPLICABLE"
            continue

        if lifecycle == "PENDING":
            if breakout:
                breakout_count += 1
                if breakout_count >= config.breakout_confirm_bars:
                    lifecycle = "CONFIRMED"
                    confirmed_index = index
                    state = "BREAKOUT_CONFIRMED"
                    retest_status = "AWAITING"
                else:
                    state = "BREAKOUT_PENDING"
                continue
            if is_reclaimed(item):
                lifecycle = "FALSE"
                state = "FALSE_BREAKOUT"
                retest_status = "FAILED"
            else:
                breakout_count = 0
                state = "BREAKOUT_PENDING"
            continue

        if lifecycle in ("CONFIRMED", "RETEST_PENDING", "HELD"):
            bars_after_confirmation = (
                index - confirmed_index if confirmed_index is not None else 0
            )
            inside_retest_window = (
                1 <= bars_after_confirmation <= config.retest_window_bars
            )
            if lifecycle == "HELD":
                state = "RETEST_HELD"
                retest_status = "HELD"
                continue
            if not inside_retest_window:
                lifecycle = "CONFIRMED"
                state = "BREAKOUT_CONFIRMED"
                retest_status = (
                    "AWAITING"
                    if bars_after_confirmation <= config.retest_window_bars
                    else "NOT_APPLICABLE"
                )
                continue
            if is_reclaimed(item):
                lifecycle = "FALSE"
                state = "FALSE_BREAKOUT"
                retest_status = "FAILED"
                continue
            if not intersects:
                state = (
                    "RETEST_PENDING"
                    if lifecycle == "RETEST_PENDING"
                    else "BREAKOUT_CONFIRMED"
                )
                retest_status = (
                    "PENDING" if lifecycle == "RETEST_PENDING" else "AWAITING"
                )
                continue
            if is_retest_held(item):
                lifecycle = "HELD"
                state = "RETEST_HELD"
                retest_status = "HELD"
            else:
                lifecycle = "RETEST_PENDING"
                state = "RETEST_PENDING"
                retest_status = "PENDING"

    latest = relevant[-1]
    distances = _distance(float(latest.close), result, atr)
    result.update(distances)
    result.update(
        {
            "state": state,
            "breakout_direction": (
                breakout_direction
                if state
                in {
                    "BREAKOUT_PENDING",
                    "BREAKOUT_CONFIRMED",
                    "RETEST_PENDING",
                    "RETEST_HELD",
                    "FALSE_BREAKOUT",
                    "LEVEL_INVALIDATED",
                }
                else "NONE"
            ),
            "breakout_closed_bars": breakout_count,
            "breakout_buffer_atr": config.breakout_atr,
            "retest_status": retest_status,
            "reason_code": f"STRUCTURE_STATE_{state}",
        }
    )
    return result


class StructureStateMachine:
    STATES = {
        "INSUFFICIENT_DATA",
        "NO_NEARBY_LEVEL",
        "APPROACHING_SUPPORT",
        "APPROACHING_RESISTANCE",
        "SUPPORT_REJECTED",
        "RESISTANCE_REJECTED",
        "BREAKOUT_PENDING",
        "BREAKOUT_CONFIRMED",
        "RETEST_PENDING",
        "RETEST_HELD",
        "FALSE_BREAKOUT",
        "LEVEL_INVALIDATED",
        "ERROR",
    }

    def __init__(self, config: StructureConfig | None = None):
        self.config = config or StructureConfig()

    def evaluate(
        self,
        detected: dict[str, object],
        closed_klines: Sequence[Kline],
    ) -> list[dict[str, object]]:
        if not isinstance(detected, Mapping):
            return [_error_evidence("STRUCTURE_SNAPSHOT_INVALID")]
        if detected.get("status") != "READY":
            return []
        evaluated_at = detected.get("evaluated_at")
        atr = detected.get("atr")
        if type(evaluated_at) is not int or evaluated_at <= 0:
            return [_error_evidence("STRUCTURE_EVALUATED_AT_INVALID")]
        if not _finite_number(atr) or float(atr) <= 0:
            return [_error_evidence("STRUCTURE_ATR_INVALID")]
        causal = _causal_bars(closed_klines, evaluated_at)
        if causal is None:
            return [_error_evidence("STRUCTURE_CAUSAL_WINDOW_INVALID")]

        nearest = []
        for kind, key, fallback_key in (
            ("SUPPORT", "nearest_support", "support"),
            ("RESISTANCE", "nearest_resistance", "resistance"),
        ):
            level = detected.get(key)
            if level is None:
                fallback = detected.get(fallback_key, [])
                if isinstance(fallback, Sequence) and fallback:
                    level = fallback[0]
            if level is None:
                continue
            if not _valid_level(level, evaluated_at):
                return [_error_evidence("STRUCTURE_LEVEL_INVALID")]
            if str(level.get("kind")) != kind:
                return [_error_evidence("STRUCTURE_LEVEL_KIND_INVALID")]
            nearest.append(level)

        if not nearest:
            return []
        return [
            _classify_level_state(level, causal, float(atr), self.config)
            for level in nearest
        ]


_BREAKOUT_STATES = {
    "BREAKOUT_PENDING",
    "BREAKOUT_CONFIRMED",
    "RETEST_PENDING",
    "RETEST_HELD",
    "FALSE_BREAKOUT",
}


def _invalid_mapped_evidence(
    evidence: object,
    reason_code: str = "STRUCTURE_EVIDENCE_INVALID",
) -> dict[str, object]:
    result = dict(evidence) if isinstance(evidence, Mapping) else {}
    result.update(_error_evidence(reason_code))
    result["bias"] = "NEUTRAL"
    return result


def map_direction_bias(
    direction: str,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    if direction not in ("LONG", "SHORT") or not isinstance(evidence, Mapping):
        return _invalid_mapped_evidence(evidence)
    state = str(evidence.get("state", ""))
    if state not in StructureStateMachine.STATES:
        return _invalid_mapped_evidence(evidence)
    if state in ("INSUFFICIENT_DATA", "NO_NEARBY_LEVEL", "ERROR"):
        result = dict(evidence)
        result["bias"] = "NEUTRAL"
        result.setdefault("reason_code", f"STRUCTURE_STATE_{state}")
        return result
    if not _valid_level(evidence, sys.maxsize):
        return _invalid_mapped_evidence(evidence)

    kind = str(evidence.get("kind"))
    breakout_direction = str(evidence.get("breakout_direction", "NONE"))
    consistent = True
    if state in ("APPROACHING_SUPPORT", "SUPPORT_REJECTED"):
        consistent = kind == "SUPPORT" and breakout_direction == "NONE"
    elif state in ("APPROACHING_RESISTANCE", "RESISTANCE_REJECTED"):
        consistent = kind == "RESISTANCE" and breakout_direction == "NONE"
    elif state in _BREAKOUT_STATES:
        expected = "DOWN" if kind == "SUPPORT" else "UP"
        consistent = breakout_direction == expected
    elif state == "LEVEL_INVALIDATED":
        consistent = breakout_direction in (
            "NONE",
            "DOWN" if kind == "SUPPORT" else "UP",
        )
    if not consistent:
        return _invalid_mapped_evidence(
            evidence,
            "STRUCTURE_EVIDENCE_INCONSISTENT",
        )

    if state == "APPROACHING_SUPPORT":
        bias = "NEUTRAL" if direction == "LONG" else "CONFLICT"
    elif state == "APPROACHING_RESISTANCE":
        bias = "CONFLICT" if direction == "LONG" else "NEUTRAL"
    elif state == "SUPPORT_REJECTED":
        bias = "CONFIRMED" if direction == "LONG" else "CONFLICT"
    elif state == "RESISTANCE_REJECTED":
        bias = "CONFLICT" if direction == "LONG" else "CONFIRMED"
    elif state == "LEVEL_INVALIDATED":
        bias = "NEUTRAL"
    else:
        up = breakout_direction == "UP"
        follows = (direction == "LONG" and up) or (direction == "SHORT" and not up)
        if state in ("BREAKOUT_PENDING", "RETEST_PENDING"):
            bias = "PENDING" if follows else "CONFLICT"
        elif state in ("BREAKOUT_CONFIRMED", "RETEST_HELD"):
            bias = "CONFIRMED" if follows else "CONFLICT"
        else:
            bias = "CONFLICT" if follows else "CONFIRMED"

    result = dict(evidence)
    result["bias"] = bias
    result["reason_code"] = f"STRUCTURE_{state}_{direction}_{bias}"
    return result


def _payload_number(value: object) -> float | int | None:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _level_payload(prefix: str, level: object) -> dict[str, object]:
    source = level if isinstance(level, Mapping) else {}
    return {
        f"{prefix}_id": str(source.get("id", "")),
        f"{prefix}_kind": str(source.get("kind", "")),
        f"{prefix}_source": str(source.get("source", "")),
        f"{prefix}_lower": _payload_number(source.get("lower")),
        f"{prefix}_upper": _payload_number(source.get("upper")),
        f"{prefix}_distance_price": _payload_number(
            source.get("distance_price")
        ),
        f"{prefix}_distance_bps": _payload_number(source.get("distance_bps")),
        f"{prefix}_distance_atr": _payload_number(source.get("distance_atr")),
        f"{prefix}_touch_count": _payload_number(source.get("touch_count")),
        f"{prefix}_last_confirmed_at": _payload_number(
            source.get("last_confirmed_at")
        ),
    }


class EntryStructureGate:
    _BIAS_PRIORITY = {
        "CONFLICT": 0,
        "PENDING": 1,
        "CONFIRMED": 2,
        "NEUTRAL": 3,
    }

    def __init__(
        self,
        detector: StructureDetector | None = None,
        state_machine: StructureStateMachine | None = None,
    ):
        self.detector = detector or StructureDetector()
        self.state_machine = state_machine or StructureStateMachine(
            self.detector.config
        )

    def rank(
        self,
        evidence: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        def key(item: Mapping[str, object]) -> tuple[object, ...]:
            distance = item.get("distance_atr")
            safe_distance = (
                float(distance)
                if _finite_number(distance)
                else sys.float_info.max
            )
            touches = item.get("touch_count")
            safe_touches = touches if type(touches) is int and touches >= 0 else 0
            confirmed = item.get("last_confirmed_at")
            safe_confirmed = (
                confirmed if type(confirmed) is int and confirmed >= 0 else 0
            )
            return (
                self._BIAS_PRIORITY.get(str(item.get("bias", "")), 4),
                safe_distance,
                -safe_touches,
                -safe_confirmed,
                str(item.get("id", "")),
            )

        return sorted((dict(item) for item in evidence), key=key)

    def attach(
        self,
        signal: Signal,
        market_snapshot: dict[str, object],
        candidate_origin: str,
    ) -> dict[str, object]:
        status = str(market_snapshot.get("status", "ERROR"))
        states = market_snapshot.get("states", [])
        mapped = []
        if status == "READY" and isinstance(states, Sequence):
            mapped = [
                map_direction_bias(signal.direction, item)
                for item in states
                if isinstance(item, Mapping)
            ]
        if status == "INSUFFICIENT_DATA":
            active = _error_evidence("STRUCTURE_INSUFFICIENT_DATA")
            active["state"] = "INSUFFICIENT_DATA"
            active["bias"] = "NEUTRAL"
        elif status != "READY":
            active = _invalid_mapped_evidence(
                {}, "STRUCTURE_SNAPSHOT_NOT_READY"
            )
        elif mapped:
            active = self.rank(mapped)[0]
        else:
            active = _error_evidence("STRUCTURE_NO_NEARBY_LEVEL")
            active["state"] = "NO_NEARBY_LEVEL"
            active["bias"] = "NEUTRAL"

        state = str(active.get("state", "ERROR"))
        bias = str(active.get("bias", "NEUTRAL"))
        reason_code = str(
            active.get("reason_code", f"STRUCTURE_STATE_{state}")
        )
        evaluated_at = market_snapshot.get("evaluated_at", 0)
        safe_evaluated_at = evaluated_at if type(evaluated_at) is int else 0
        payload = {
            "entry_structure_version": ENTRY_STRUCTURE_VERSION,
            "entry_structure_mode": "SHADOW_ONLY",
            "entry_structure_evaluated_at": safe_evaluated_at,
            "entry_structure_state": state,
            "entry_structure_bias": bias,
            "entry_structure_reason_code": reason_code,
            "version": ENTRY_STRUCTURE_VERSION,
            "mode": "SHADOW_ONLY",
            "status": state,
            "evaluated_at": safe_evaluated_at,
            "state": state,
            "bias": bias,
            "reason_code": reason_code,
            "audit_only": True,
            "candidate_origin": str(candidate_origin),
            "candidate_direction": str(signal.direction),
            "active_level_source": str(active.get("source", "")),
            "breakout_direction": str(
                active.get("breakout_direction", "NONE")
            ),
            "breakout_closed_bars": _payload_number(
                active.get("breakout_closed_bars", 0)
            ),
            "breakout_buffer_atr": _payload_number(
                active.get("breakout_buffer_atr", self.state_machine.config.breakout_atr)
            ),
            "retest_status": str(
                active.get("retest_status", "NOT_APPLICABLE")
            ),
        }
        payload.update(_level_payload("active_level", active))
        payload["active_level_confirmed_at"] = _payload_number(
            active.get("last_confirmed_at")
        )
        payload.update(
            _level_payload("nearest_support", market_snapshot.get("nearest_support"))
        )
        payload.update(
            _level_payload(
                "nearest_resistance", market_snapshot.get("nearest_resistance")
            )
        )
        payload.update(
            {
                "support_distance_price": payload["nearest_support_distance_price"],
                "support_distance_bps": payload["nearest_support_distance_bps"],
                "support_distance_atr": payload["nearest_support_distance_atr"],
                "resistance_distance_price": payload[
                    "nearest_resistance_distance_price"
                ],
                "resistance_distance_bps": payload[
                    "nearest_resistance_distance_bps"
                ],
                "resistance_distance_atr": payload[
                    "nearest_resistance_distance_atr"
                ],
                "round_level_price": _payload_number(
                    active.get("round_level_price")
                ),
                "round_level_step": _payload_number(
                    active.get("round_level_step")
                ),
            }
        )
        return payload

    def evaluate(
        self,
        signal: Signal,
        symbol: str,
        closed_klines: Sequence[Kline],
        candidate_origin: str = "NATIVE_ACTIONABLE",
    ) -> dict[str, object]:
        try:
            detected = self.detector.detect(symbol, closed_klines)
        except Exception as exc:
            code = f"DETECTOR_ERROR_{type(exc).__name__.upper()}"
            payload = self.attach(
                signal,
                {
                    "status": "ERROR",
                    "evaluated_at": 0,
                    "states": [_error_evidence(code)],
                    "nearest_support": None,
                    "nearest_resistance": None,
                },
                candidate_origin,
            )
            payload["entry_structure_reason_code"] = code
            payload["reason_code"] = code
            payload["error_detail"] = str(exc)[:240]
            return payload

        try:
            states = self.state_machine.evaluate(detected, closed_klines)
            market = {**detected, "states": states}
            return self.attach(signal, market, candidate_origin)
        except Exception as exc:
            code = f"STATE_MACHINE_ERROR_{type(exc).__name__.upper()}"
            payload = self.attach(
                signal,
                {
                    "status": "ERROR",
                    "evaluated_at": detected.get("evaluated_at", 0),
                    "states": [_error_evidence(code)],
                    "nearest_support": detected.get("nearest_support"),
                    "nearest_resistance": detected.get("nearest_resistance"),
                },
                candidate_origin,
            )
            payload["entry_structure_reason_code"] = code
            payload["reason_code"] = code
            payload["error_detail"] = str(exc)[:240]
            return payload
