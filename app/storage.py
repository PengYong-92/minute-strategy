import json
import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from app.models import ObservationSignal, Signal, SimulatedOrder
from app.order_profile import sample_from_entry_snapshot, summarize_order_samples_with_guard
from app.stake_progression import TWO_STAGE_VERSION, StakeProgressionCredit


ORDER_PAGE_SIZES = (10, 20, 30, 50, 100)
OBSERVATION_SUMMARY_LIMIT = 5000
OBSERVATION_PROMOTE_SAMPLE = 30
OBSERVATION_WATCH_SAMPLE = 10


def page_order_list(
    orders: list[SimulatedOrder],
    *,
    page: int = 1,
    page_size: int = 20,
    direction: str = "",
    level: str = "",
    segment: str = "",
    result: str = "",
) -> dict[str, Any]:
    ordered = sorted(orders, key=lambda item: (item.opened_at, item.id), reverse=True)
    filter_options = _order_filter_options(ordered)
    filters = {
        "direction": _clean_filter(direction),
        "level": _clean_filter(level),
        "segment": _clean_filter(segment),
        "result": _clean_filter(result),
    }
    filtered = [order for order in ordered if _order_matches(order, filters)]
    normalized_page_size = _normalize_page_size(page_size)
    total = len(filtered)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(max(1, int(page or 1)), total_pages)
    start = (normalized_page - 1) * normalized_page_size
    end = start + normalized_page_size
    return {
        "orders": [order.to_dict() for order in filtered[start:end]],
        "total": total,
        "page": normalized_page,
        "page_size": normalized_page_size,
        "total_pages": total_pages,
        "filters": filters,
        "filter_options": filter_options,
    }


def page_observation_list(
    observations: list[ObservationSignal],
    *,
    page: int = 1,
    page_size: int = 20,
    direction: str = "",
    family: str = "",
    tag: str = "",
    segment: str = "",
    result: str = "",
) -> dict[str, Any]:
    ordered = sorted(observations, key=lambda item: (item.opened_at, item.observation_key), reverse=True)
    filter_options = _observation_filter_options(ordered)
    filters = {
        "direction": _clean_filter(direction),
        "family": _clean_filter(family),
        "tag": _clean_filter(tag),
        "segment": _clean_filter(segment),
        "result": _clean_filter(result),
    }
    filtered = [observation for observation in ordered if _observation_matches(observation, filters)]
    normalized_page_size = _normalize_page_size(page_size)
    total = len(filtered)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(max(1, int(page or 1)), total_pages)
    start = (normalized_page - 1) * normalized_page_size
    end = start + normalized_page_size
    return {
        "observations": [observation.to_dict() for observation in filtered[start:end]],
        "total": total,
        "page": normalized_page,
        "page_size": normalized_page_size,
        "total_pages": total_pages,
        "filters": filters,
        "filter_options": filter_options,
    }


def summarize_observations(
    observations: list[ObservationSignal],
    *,
    group_limit: int = 50,
) -> dict[str, Any]:
    total = _empty_observation_stats()
    groups: dict[tuple[int, str, str, str, str], dict[str, Any]] = {}
    for observation in observations:
        _accumulate_observation_stats(total, observation)
        key = (
            observation.timeframe_minutes,
            observation.strategy_family,
            observation.strategy_tag,
            observation.direction,
            observation.threshold_segment,
        )
        group = groups.setdefault(
            key,
            _empty_observation_stats(
                timeframe_minutes=observation.timeframe_minutes,
                strategy_family=observation.strategy_family,
                strategy_tag=observation.strategy_tag,
                direction=observation.direction,
                threshold_segment=observation.threshold_segment,
            ),
        )
        _accumulate_observation_stats(group, observation)

    finalized_groups = [_finalize_observation_group(group) for group in groups.values()]
    finalized_groups.sort(
        key=lambda item: (
            _observation_action_rank(item["action"]),
            -item["settled"],
            -item["ev"],
            item["strategy_family"],
            item["strategy_tag"],
            item["direction"],
            item["threshold_segment"],
        )
    )
    action_counts: dict[str, int] = {}
    for group in finalized_groups:
        action_counts[group["action"]] = action_counts.get(group["action"], 0) + 1

    return {
        "total": _finalize_observation_stats(total),
        "groups": finalized_groups[:group_limit],
        "group_limit": group_limit,
        "action_counts": action_counts,
        "rules": {
            "promote_sample": OBSERVATION_PROMOTE_SAMPLE,
            "watch_sample": OBSERVATION_WATCH_SAMPLE,
            "promote_win_rate": 0.6,
            "promote_ev": 0.8,
            "block_win_rate": 0.5,
            "block_ev": -1.0,
        },
    }


def _normalize_page_size(page_size: int) -> int:
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        return 20
    return value if value in ORDER_PAGE_SIZES else 20


def _clean_filter(value: str | None) -> str:
    return str(value or "").strip().upper()


def _order_matches(order: SimulatedOrder, filters: dict[str, str]) -> bool:
    if filters["direction"] and order.direction.upper() != filters["direction"]:
        return False
    if filters["level"] and order.level.upper() != filters["level"]:
        return False
    if filters["segment"] and order.threshold_segment.upper() != filters["segment"]:
        return False
    if filters["result"]:
        if filters["result"] == "OPEN":
            return order.status.upper() == "OPEN"
        return str(order.result or "").upper() == filters["result"]
    return True


def _order_filter_options(orders: list[SimulatedOrder]) -> dict[str, list[str]]:
    result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
    level_order = {"S": 0, "A": 1, "B": 2}
    results = {
        "OPEN" if order.status.upper() == "OPEN" else str(order.result or "").upper()
        for order in orders
    }
    results.discard("")
    return {
        "direction": sorted({order.direction.upper() for order in orders if order.direction}),
        "level": sorted({order.level.upper() for order in orders if order.level}, key=lambda item: level_order.get(item, 99)),
        "segment": sorted({order.threshold_segment.upper() for order in orders if order.threshold_segment}),
        "result": sorted(results, key=lambda item: result_order.get(item, 99)),
        "page_size": list(ORDER_PAGE_SIZES),
    }


def _observation_matches(observation: ObservationSignal, filters: dict[str, str]) -> bool:
    if filters["direction"] and observation.direction.upper() != filters["direction"]:
        return False
    if filters["family"] and observation.strategy_family.upper() != filters["family"]:
        return False
    if filters["tag"] and observation.strategy_tag.upper() != filters["tag"]:
        return False
    if filters["segment"] and observation.threshold_segment.upper() != filters["segment"]:
        return False
    if filters["result"]:
        if filters["result"] == "OPEN":
            return observation.status.upper() == "OPEN"
        return str(observation.result or "").upper() == filters["result"]
    return True


def _observation_filter_options(observations: list[ObservationSignal]) -> dict[str, list[str]]:
    result_order = {"OPEN": 0, "WIN": 1, "LOSS": 2}
    results = {
        "OPEN" if observation.status.upper() == "OPEN" else str(observation.result or "").upper()
        for observation in observations
    }
    results.discard("")
    return {
        "direction": sorted({observation.direction.upper() for observation in observations if observation.direction}),
        "family": sorted(
            {observation.strategy_family.upper() for observation in observations if observation.strategy_family}
        ),
        "tag": sorted({observation.strategy_tag.upper() for observation in observations if observation.strategy_tag}),
        "segment": sorted(
            {observation.threshold_segment.upper() for observation in observations if observation.threshold_segment}
        ),
        "result": sorted(results, key=lambda item: result_order.get(item, 99)),
        "page_size": list(ORDER_PAGE_SIZES),
    }


def _empty_observation_stats(
    *,
    timeframe_minutes: int | None = None,
    strategy_family: str = "",
    strategy_tag: str = "",
    direction: str = "",
    threshold_segment: str = "",
) -> dict[str, Any]:
    return {
        "timeframe_minutes": timeframe_minutes,
        "strategy_family": strategy_family,
        "strategy_tag": strategy_tag,
        "direction": direction,
        "threshold_segment": threshold_segment,
        "signals": 0,
        "open": 0,
        "settled": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "first_opened_at": None,
        "last_opened_at": None,
    }


def _accumulate_observation_stats(stats: dict[str, Any], observation: ObservationSignal) -> None:
    stats["signals"] += 1
    stats["first_opened_at"] = _min_optional(stats["first_opened_at"], observation.opened_at)
    stats["last_opened_at"] = _max_optional(stats["last_opened_at"], observation.opened_at)
    result = str(observation.result or "").upper()
    if observation.status.upper() == "OPEN" or result not in {"WIN", "LOSS"}:
        stats["open"] += 1
        return
    stats["settled"] += 1
    if result == "WIN":
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    stats["pnl"] += _observation_result_pnl(observation)


def _finalize_observation_stats(stats: dict[str, Any]) -> dict[str, Any]:
    settled = stats["settled"]
    wins = stats["wins"]
    win_rate = wins / settled if settled else 0.0
    ev = stats["pnl"] / settled if settled else 0.0
    return {
        **stats,
        "win_rate": win_rate,
        "ev": ev,
        "pnl": round(stats["pnl"], 4),
    }


def _finalize_observation_group(stats: dict[str, Any]) -> dict[str, Any]:
    finalized = _finalize_observation_stats(stats)
    finalized["action"] = _observation_action(finalized)
    finalized["confidence"] = _observation_confidence(finalized["settled"])
    return finalized


def _observation_result_pnl(observation: ObservationSignal) -> float:
    if observation.pnl:
        return observation.pnl
    return 8.0 if observation.result == "WIN" else -10.0


def _observation_action(stats: dict[str, Any]) -> str:
    settled = stats["settled"]
    win_rate = stats["win_rate"]
    ev = stats["ev"]
    if settled < OBSERVATION_WATCH_SAMPLE:
        return "COLLECTING"
    if settled >= OBSERVATION_PROMOTE_SAMPLE and win_rate >= 0.6 and ev >= 0.8:
        return "PROMOTE_WATCH"
    if settled >= OBSERVATION_PROMOTE_SAMPLE and (win_rate < 0.5 or ev <= -1.0):
        return "BLOCK_WATCH"
    if win_rate >= 0.56 and ev > 0:
        return "WATCH_UPSIDE"
    if win_rate < 0.5 or ev < 0:
        return "WATCH_RISK"
    return "WATCH"


def _observation_confidence(settled: int) -> str:
    if settled >= 100:
        return "HIGH"
    if settled >= OBSERVATION_PROMOTE_SAMPLE:
        return "MEDIUM"
    return "LOW"


def _observation_action_rank(action: str) -> int:
    ranks = {
        "PROMOTE_WATCH": 0,
        "WATCH_UPSIDE": 1,
        "WATCH": 2,
        "WATCH_RISK": 3,
        "BLOCK_WATCH": 4,
        "COLLECTING": 5,
    }
    return ranks.get(action, 99)


def _min_optional(current: int | None, value: int) -> int:
    return value if current is None else min(current, value)


def _max_optional(current: int | None, value: int) -> int:
    return value if current is None else max(current, value)


class SQLiteMonitorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def save_order(self, order: SimulatedOrder, symbol: str) -> None:
        with self._connect() as connection:
            self._upsert_order(connection, order, symbol)

    def _upsert_order(
        self,
        connection: sqlite3.Connection,
        order: SimulatedOrder,
        symbol: str,
    ) -> None:
        connection.execute(
            """
            insert into orders(symbol, order_id, status, result, opened_at, settled_at, payload)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(symbol, order_id) do update set
                status=excluded.status,
                result=excluded.result,
                opened_at=excluded.opened_at,
                settled_at=excluded.settled_at,
                payload=excluded.payload,
                updated_at_ms=strftime('%s','now') * 1000
            """,
            (
                symbol.upper(),
                order.id,
                order.status,
                order.result,
                order.opened_at,
                order.settled_at,
                json.dumps(order.to_dict(), ensure_ascii=False),
            ),
        )

    def _upsert_progression_credit(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        credit: StakeProgressionCredit,
    ) -> None:
        connection.execute(
            """
            insert into stake_progression_credits(
                symbol, version, credit_id, source_order_id, status, created_at,
                consumed_order_id, consumed_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(symbol, version, source_order_id) do update set
                credit_id=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.credit_id
                    else stake_progression_credits.credit_id
                end,
                status=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.status
                    else stake_progression_credits.status
                end,
                created_at=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.created_at
                    else stake_progression_credits.created_at
                end,
                consumed_order_id=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.consumed_order_id
                    else stake_progression_credits.consumed_order_id
                end,
                consumed_at=case
                    when stake_progression_credits.status = 'PENDING'
                    then excluded.consumed_at
                    else stake_progression_credits.consumed_at
                end,
                updated_at_ms=strftime('%s','now') * 1000
            """,
            (
                symbol.upper(),
                credit.version,
                credit.credit_id,
                credit.source_order_id,
                credit.status,
                credit.created_at,
                credit.consumed_order_id,
                credit.consumed_at,
            ),
        )

    def save_stake_progression_credit(
        self,
        symbol: str,
        credit: StakeProgressionCredit,
    ) -> None:
        with self._connect() as connection:
            self._upsert_progression_credit(connection, symbol, credit)

    def load_stake_progression_credits(
        self,
        symbol: str,
        version: str = TWO_STAGE_VERSION,
    ) -> list[StakeProgressionCredit]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select source_order_id, created_at, credit_id, consumed_at,
                       consumed_order_id, version, status
                from stake_progression_credits
                where symbol = ? and version = ?
                order by created_at, credit_id
                """,
                (symbol.upper(), version),
            ).fetchall()
        return [
            StakeProgressionCredit(
                source_order_id=row["source_order_id"],
                created_at=row["created_at"],
                credit_id=row["credit_id"],
                consumed_at=row["consumed_at"],
                consumed_order_id=row["consumed_order_id"],
                version=row["version"],
                status=row["status"],
            )
            for row in rows
        ]

    def save_settled_order_with_credit(
        self,
        order: SimulatedOrder,
        symbol: str,
        credit: StakeProgressionCredit | None,
    ) -> None:
        self._validate_settlement_credit(order, credit)
        with self._connect() as connection:
            self._upsert_order(connection, order, symbol)
            if credit is not None:
                self._upsert_progression_credit(connection, symbol, credit)

    def save_open_order_with_credit(
        self,
        order: SimulatedOrder,
        symbol: str,
        credit: StakeProgressionCredit | None,
    ) -> None:
        self._validate_open_credit(order, credit)
        with self._connect() as connection:
            self._upsert_order(connection, order, symbol)
            if credit is not None:
                self._upsert_progression_credit(connection, symbol, credit)
                persisted = connection.execute(
                    """
                    select credit_id, status, consumed_order_id, consumed_at
                    from stake_progression_credits
                    where symbol = ? and version = ? and source_order_id = ?
                    """,
                    (symbol.upper(), credit.version, credit.source_order_id),
                ).fetchone()
                if (
                    persisted is None
                    or persisted["status"] != "CONSUMED"
                    or persisted["consumed_order_id"] != credit.consumed_order_id
                    or persisted["consumed_at"] != credit.consumed_at
                    or persisted["credit_id"] != credit.credit_id
                ):
                    raise ValueError("credit consumption conflicts with persisted terminal state")

    @staticmethod
    def _validate_settlement_credit(
        order: SimulatedOrder,
        credit: StakeProgressionCredit | None,
    ) -> None:
        if credit is None:
            return
        if order.stake_progression_step != 1:
            raise ValueError("settlement credit requires a first-stage order")
        if credit.source_order_id != order.id:
            raise ValueError("settlement credit source_order_id must match order.id")
        if credit.status not in {"PENDING", "CANCELLED"}:
            raise ValueError("settlement credit must be PENDING or CANCELLED")
        if credit.version != order.stake_progression_version:
            raise ValueError("settlement credit version must match order version")

    @staticmethod
    def _validate_open_credit(
        order: SimulatedOrder,
        credit: StakeProgressionCredit | None,
    ) -> None:
        if credit is None:
            if order.stake_progression_step == 2:
                raise ValueError("second-stage order requires a consumed credit")
            return
        if order.stake_progression_step != 2:
            raise ValueError("consumed credit requires a second-stage order")
        if credit.status != "CONSUMED":
            raise ValueError("open-order credit must be CONSUMED")
        if credit.consumed_order_id != order.id:
            raise ValueError("credit consumed_order_id must match order.id")
        if credit.source_order_id != order.stake_progression_source_order_id:
            raise ValueError("credit source_order_id must match order source")
        if credit.version != order.stake_progression_version:
            raise ValueError("open-order credit version must match order version")

    def prepare_stake_progression(
        self,
        symbol: str,
        version: str,
        enabled: bool,
        activated_at: int,
    ) -> int:
        normalized_symbol = str(symbol).strip().upper()
        normalized_version = str(version).strip()
        if not normalized_symbol:
            raise ValueError("symbol must not be empty")
        if not normalized_version:
            raise ValueError("version must not be empty")
        try:
            requested_activation = int(activated_at)
        except (TypeError, ValueError) as error:
            raise ValueError("activated_at must be an integer") from error
        if requested_activation < 0:
            raise ValueError("activated_at must be >= 0")
        normalized_enabled = bool(enabled)

        with self._connect() as connection:
            runtime = connection.execute(
                """
                select version, activated_at, enabled
                from stake_progression_runtime
                where symbol = ?
                """,
                (normalized_symbol,),
            ).fetchone()
            should_cancel = False
            if runtime is None:
                actual_activation = requested_activation
                should_cancel = not normalized_enabled
            else:
                version_changed = runtime["version"] != normalized_version
                reenabled = not bool(runtime["enabled"]) and normalized_enabled
                disabling = bool(runtime["enabled"]) and not normalized_enabled
                should_cancel = version_changed or reenabled or disabling
                actual_activation = (
                    requested_activation
                    if version_changed or reenabled
                    else int(runtime["activated_at"])
                )

            if should_cancel:
                connection.execute(
                    """
                    update stake_progression_credits
                    set status = 'CANCELLED',
                        consumed_order_id = null,
                        consumed_at = null,
                        updated_at_ms = strftime('%s','now') * 1000
                    where symbol = ? and status = 'PENDING'
                    """,
                    (normalized_symbol,),
                )
            connection.execute(
                """
                insert into stake_progression_runtime(
                    symbol, version, activated_at, enabled
                )
                values (?, ?, ?, ?)
                on conflict(symbol) do update set
                    version=excluded.version,
                    activated_at=excluded.activated_at,
                    enabled=excluded.enabled,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    normalized_symbol,
                    normalized_version,
                    actual_activation,
                    int(normalized_enabled),
                ),
            )
        return actual_activation

    def load_orders(self, symbol: str) -> list[SimulatedOrder]:
        accepted = {field.name for field in fields(SimulatedOrder)}
        with self._connect() as connection:
            rows = connection.execute(
                "select payload from orders where symbol = ? order by order_id",
                (symbol.upper(),),
            ).fetchall()
        orders = []
        for row in rows:
            payload = json.loads(row["payload"])
            clean_payload = {key: value for key, value in payload.items() if key in accepted}
            orders.append(SimulatedOrder(**clean_payload))
        return orders

    def save_observation(self, observation: ObservationSignal, symbol: str) -> None:
        payload = observation.to_dict()
        with self._connect() as connection:
            connection.execute(
                """
                insert into observation_signals(
                    symbol, observation_key, status, result, direction,
                    strategy_family, strategy_tag, timeframe_minutes,
                    threshold_segment, opened_at, expires_at, settled_at, payload
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, observation_key) do update set
                    status=excluded.status,
                    result=excluded.result,
                    direction=excluded.direction,
                    strategy_family=excluded.strategy_family,
                    strategy_tag=excluded.strategy_tag,
                    timeframe_minutes=excluded.timeframe_minutes,
                    threshold_segment=excluded.threshold_segment,
                    opened_at=excluded.opened_at,
                    expires_at=excluded.expires_at,
                    settled_at=excluded.settled_at,
                    payload=excluded.payload,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    symbol.upper(),
                    observation.observation_key,
                    observation.status,
                    observation.result,
                    observation.direction,
                    observation.strategy_family,
                    observation.strategy_tag,
                    observation.timeframe_minutes,
                    observation.threshold_segment,
                    observation.opened_at,
                    observation.expires_at,
                    observation.settled_at,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def load_observations(self, symbol: str, limit: int = 500) -> list[ObservationSignal]:
        accepted = {field.name for field in fields(ObservationSignal)}
        with self._connect() as connection:
            rows = connection.execute(
                """
                select payload
                from observation_signals
                where symbol = ?
                order by opened_at desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        observations = []
        for row in rows:
            payload = json.loads(row["payload"])
            clean_payload = {key: value for key, value in payload.items() if key in accepted}
            observations.append(ObservationSignal(**clean_payload))
        return observations

    def load_observations_for_profile(
        self,
        symbol: str,
        *,
        lookback_days: int = 7,
    ) -> list[ObservationSignal]:
        normalized_symbol = symbol.upper()
        with self._connect() as connection:
            latest = connection.execute(
                "select max(opened_at) as latest_opened_at from observation_signals where symbol = ?",
                (normalized_symbol,),
            ).fetchone()["latest_opened_at"]
            if latest is None:
                return []
            cutoff = int(latest) - (max(1, int(lookback_days)) + 1) * 86_400_000
            rows = connection.execute(
                """
                select payload
                from observation_signals
                where symbol = ? and (status = 'OPEN' or opened_at >= ?)
                order by opened_at desc
                """,
                (normalized_symbol, cutoff),
            ).fetchall()
        accepted = {field.name for field in fields(ObservationSignal)}
        observations = []
        for row in rows:
            payload = json.loads(row["payload"])
            clean_payload = {key: value for key, value in payload.items() if key in accepted}
            observations.append(ObservationSignal(**clean_payload))
        return observations

    def save_daily_profile_selection(self, symbol: str, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                """
                insert into daily_profile_selections(
                    symbol, effective_from, effective_until, status, evaluated_at, payload
                )
                values (?, ?, ?, ?, ?, ?)
                on conflict(symbol, effective_from) do update set
                    effective_until=excluded.effective_until,
                    status=excluded.status,
                    evaluated_at=excluded.evaluated_at,
                    payload=excluded.payload,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    symbol.upper(),
                    int(snapshot["effective_from"]),
                    int(snapshot["effective_until"]),
                    str(snapshot.get("status", "READY")),
                    int(snapshot.get("evaluated_at", 0)),
                    payload,
                ),
            )

    def load_daily_profile_selection(self, symbol: str, effective_at_ms: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select payload
                from daily_profile_selections
                where symbol = ? and effective_from <= ? and effective_until > ?
                order by effective_from desc
                limit 1
                """,
                (symbol.upper(), int(effective_at_ms), int(effective_at_ms)),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_latest_daily_profile_selection(self, symbol: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select payload
                from daily_profile_selections
                where symbol = ?
                order by effective_from desc
                limit 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def page_orders(
        self,
        symbol: str,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        level: str = "",
        segment: str = "",
        result: str = "",
    ) -> dict[str, Any]:
        return page_order_list(
            self.load_orders(symbol),
            page=page,
            page_size=page_size,
            direction=direction,
            level=level,
            segment=segment,
            result=result,
        )

    def page_observations(
        self,
        symbol: str,
        *,
        page: int = 1,
        page_size: int = 20,
        direction: str = "",
        family: str = "",
        tag: str = "",
        segment: str = "",
        result: str = "",
    ) -> dict[str, Any]:
        return page_observation_list(
            self.load_observations(symbol, limit=5000),
            page=page,
            page_size=page_size,
            direction=direction,
            family=family,
            tag=tag,
            segment=segment,
            result=result,
        )

    def observation_summary(self, symbol: str, limit: int = OBSERVATION_SUMMARY_LIMIT) -> dict[str, Any]:
        return summarize_observations(self.load_observations(symbol, limit=limit))

    def order_profile_summary(
        self,
        symbol: str,
        limit: int = 5000,
        *,
        profile_guard_min_history: int = 15,
        profile_guard_min_group_size: int = 2,
    ) -> dict[str, Any]:
        snapshots = self.load_order_entry_snapshots(symbol, limit=limit)
        samples = [sample_from_entry_snapshot(snapshot) for snapshot in reversed(snapshots)]
        return summarize_order_samples_with_guard(
            samples,
            profile_guard_min_history=profile_guard_min_history,
            profile_guard_min_group_size=profile_guard_min_group_size,
        )

    def save_signal(self, symbol: str, signal: Signal, decision: str, created_at_ms: int) -> None:
        payload = signal.to_dict()
        with self._connect() as connection:
            connection.execute(
                """
                insert into signal_audit(
                    symbol, created_at_ms, decision, direction, timeframe_minutes,
                    threshold_segment, regime, score, threshold, reason, payload
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    created_at_ms,
                    decision,
                    signal.direction,
                    signal.timeframe_minutes,
                    signal.threshold_segment,
                    signal.regime,
                    signal.score,
                    signal.threshold,
                    signal.reason,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def save_order_entry_snapshot(self, order: SimulatedOrder, symbol: str, entry_snapshot: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into order_entry_snapshots(
                    symbol, order_id, direction, timeframe_minutes, opened_at, expires_at,
                    entry_price, stake, win_return, stake_progression_step,
                    threshold_segment, regime, score, threshold, edge,
                    result, settled_at, exit_price, pnl, entry_payload
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, order_id) do update set
                    direction=excluded.direction,
                    timeframe_minutes=excluded.timeframe_minutes,
                    opened_at=excluded.opened_at,
                    expires_at=excluded.expires_at,
                    entry_price=excluded.entry_price,
                    stake=excluded.stake,
                    win_return=excluded.win_return,
                    stake_progression_step=excluded.stake_progression_step,
                    threshold_segment=excluded.threshold_segment,
                    regime=excluded.regime,
                    score=excluded.score,
                    threshold=excluded.threshold,
                    edge=excluded.edge,
                    entry_payload=excluded.entry_payload,
                    updated_at_ms=strftime('%s','now') * 1000
                """,
                (
                    symbol.upper(),
                    order.id,
                    order.direction,
                    order.timeframe_minutes,
                    order.opened_at,
                    order.expires_at,
                    order.entry_price,
                    order.stake,
                    order.win_return,
                    order.stake_progression_step,
                    order.threshold_segment,
                    order.regime,
                    order.score,
                    order.threshold,
                    round(abs(order.score) - order.threshold, 4),
                    order.result,
                    order.settled_at,
                    order.exit_price,
                    order.pnl,
                    json.dumps(entry_snapshot, ensure_ascii=False),
                ),
            )

    def update_order_entry_snapshot_settlement(self, order: SimulatedOrder, symbol: str) -> None:
        settlement_payload = {
            "status": order.status,
            "result": order.result,
            "settled_at": order.settled_at,
            "exit_price": order.exit_price,
            "pnl": order.pnl,
        }
        with self._connect() as connection:
            connection.execute(
                """
                update order_entry_snapshots
                set result = ?,
                    settled_at = ?,
                    exit_price = ?,
                    pnl = ?,
                    settlement_payload = ?,
                    updated_at_ms = strftime('%s','now') * 1000
                where symbol = ? and order_id = ?
                """,
                (
                    order.result,
                    order.settled_at,
                    order.exit_price,
                    order.pnl,
                    json.dumps(settlement_payload, ensure_ascii=False),
                    symbol.upper(),
                    order.id,
                ),
            )

    def load_order_entry_snapshots(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select *
                from order_entry_snapshots
                where symbol = ?
                order by order_id desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["entry_payload"] = json.loads(item["entry_payload"])
            item["settlement_payload"] = json.loads(item["settlement_payload"]) if item["settlement_payload"] else None
            result.append(item)
        return result

    def load_recent_signals(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select symbol, created_at_ms, decision, direction, timeframe_minutes,
                       threshold_segment, regime, score, threshold, reason
                from signal_audit
                where symbol = ?
                order by id desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                create table if not exists orders (
                    symbol text not null,
                    order_id integer not null,
                    status text not null,
                    result text,
                    opened_at integer not null,
                    settled_at integer,
                    payload text not null,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, order_id)
                )
                """
            )
            connection.execute(
                """
                create table if not exists stake_progression_runtime (
                    symbol text primary key,
                    version text not null,
                    activated_at integer not null,
                    enabled integer not null check(enabled in (0, 1)),
                    updated_at_ms integer not null default (strftime('%s','now') * 1000)
                )
                """
            )
            connection.execute(
                """
                create table if not exists stake_progression_credits (
                    symbol text not null,
                    version text not null,
                    credit_id text not null,
                    source_order_id integer not null,
                    status text not null check(status in ('PENDING', 'CONSUMED', 'CANCELLED')),
                    created_at integer not null,
                    consumed_order_id integer,
                    consumed_at integer,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, version, source_order_id),
                    unique(symbol, version, credit_id),
                    unique(symbol, version, consumed_order_id)
                )
                """
            )
            connection.execute(
                """
                create table if not exists signal_audit (
                    id integer primary key autoincrement,
                    symbol text not null,
                    created_at_ms integer not null,
                    decision text not null,
                    direction text not null,
                    timeframe_minutes integer not null,
                    threshold_segment text not null,
                    regime text not null,
                    score real not null,
                    threshold real not null,
                    reason text not null,
                    payload text not null
                )
                """
            )
            connection.execute("create index if not exists idx_signal_audit_symbol_time on signal_audit(symbol, created_at_ms)")
            connection.execute(
                """
                create table if not exists observation_signals (
                    symbol text not null,
                    observation_key text not null,
                    status text not null,
                    result text,
                    direction text not null,
                    strategy_family text not null,
                    strategy_tag text not null,
                    timeframe_minutes integer not null,
                    threshold_segment text not null,
                    opened_at integer not null,
                    expires_at integer not null,
                    settled_at integer,
                    payload text not null,
                    created_at_ms integer not null default (strftime('%s','now') * 1000),
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, observation_key)
                )
                """
            )
            connection.execute(
                "create index if not exists idx_observation_signals_symbol_opened on observation_signals(symbol, opened_at)"
            )
            connection.execute(
                "create index if not exists idx_observation_signals_symbol_result on observation_signals(symbol, result)"
            )
            connection.execute(
                "create index if not exists idx_observation_signals_symbol_family on observation_signals(symbol, strategy_family)"
            )
            connection.execute(
                """
                create table if not exists daily_profile_selections (
                    symbol text not null,
                    effective_from integer not null,
                    effective_until integer not null,
                    status text not null,
                    evaluated_at integer not null,
                    payload text not null,
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, effective_from)
                )
                """
            )
            connection.execute(
                "create index if not exists idx_daily_profile_selections_symbol_effective "
                "on daily_profile_selections(symbol, effective_from, effective_until)"
            )
            connection.execute(
                """
                create table if not exists order_entry_snapshots (
                    symbol text not null,
                    order_id integer not null,
                    direction text not null,
                    timeframe_minutes integer not null,
                    opened_at integer not null,
                    expires_at integer not null,
                    entry_price real not null,
                    stake real not null,
                    win_return real not null,
                    stake_progression_step integer not null,
                    threshold_segment text not null,
                    regime text not null,
                    score real not null,
                    threshold real not null,
                    edge real not null,
                    result text,
                    settled_at integer,
                    exit_price real,
                    pnl real not null default 0.0,
                    entry_payload text not null,
                    settlement_payload text,
                    created_at_ms integer not null default (strftime('%s','now') * 1000),
                    updated_at_ms integer not null default (strftime('%s','now') * 1000),
                    primary key(symbol, order_id)
                )
                """
            )
            connection.execute(
                "create index if not exists idx_order_entry_snapshots_symbol_opened on order_entry_snapshots(symbol, opened_at)"
            )
            connection.execute(
                "create index if not exists idx_order_entry_snapshots_symbol_result on order_entry_snapshots(symbol, result)"
            )
