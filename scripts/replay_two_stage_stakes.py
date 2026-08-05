#!/usr/bin/env python3
import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.stake_progression import TWO_STAGE_VERSION, TwoStageStakeProgression


LEGACY_THREE_STAGE_VERSION = "LEGACY_THREE_STAGE_V1"
MAX_OPEN_ORDERS = 5


def reprice_trades(
    rows: Iterable[Mapping[str, Any]],
    *,
    base_stake: float = 10.0,
    base_win_return: float = 18.0,
    max_active: int = 1,
) -> dict[str, Any]:
    trades = _normalize_trades(rows)
    activated_at = min((item["opened_at"] for item in trades), default=0)
    ledger = TwoStageStakeProgression(
        enabled=True,
        base_stake=base_stake,
        base_win_return=base_win_return,
        max_active=max_active,
        max_open_orders=MAX_OPEN_ORDERS,
        activated_at=activated_at,
    )

    def open_trade(item: dict[str, Any], event_time: int) -> None:
        terms, _credit = ledger.assign(item["id"], event_time)
        item["stake"] = terms.stake
        item["win_return"] = terms.win_return
        item["stake_progression_step"] = terms.step
        item["stake_progression_source_order_id"] = terms.source_order_id
        item["stake_progression_version"] = terms.version

    def settle_trade(item: dict[str, Any], event_time: int) -> None:
        _apply_pnl(item)
        ledger.settle(
            item["id"],
            item["opened_at"],
            item["stake_progression_step"],
            item["result"],
            event_time,
        )

    _run_timeline(
        trades,
        on_open=open_trade,
        on_settle=settle_trade,
    )
    return _build_result(
        trades,
        {
            "policy": "two_stage",
            "base_stake": ledger.base_stake,
            "base_win_return": ledger.base_win_return,
            "max_active": ledger.max_active,
            "max_open_orders": ledger.max_open_orders,
            "version": TWO_STAGE_VERSION,
        },
    )


def _reprice_fixed(
    rows: Iterable[Mapping[str, Any]],
    *,
    base_stake: float,
    base_win_return: float,
) -> dict[str, Any]:
    trades = _normalize_trades(rows)
    terms = _validated_base_terms(base_stake, base_win_return)
    for item in trades:
        item["stake"] = terms[0]
        item["win_return"] = terms[1]
        item["stake_progression_step"] = 1
        item["stake_progression_source_order_id"] = None
        item["stake_progression_version"] = ""
        _apply_pnl(item)
    return _build_result(
        trades,
        {
            "policy": "fixed",
            "base_stake": terms[0],
            "base_win_return": terms[1],
            "max_active": 0,
            "version": "",
        },
    )


def _reprice_legacy_three_stage(
    rows: Iterable[Mapping[str, Any]],
    *,
    base_stake: float,
    base_win_return: float,
) -> dict[str, Any]:
    trades = _normalize_trades(rows)
    normalized_stake, normalized_return = _validated_base_terms(
        base_stake,
        base_win_return,
    )
    gross_return_ratio = normalized_return / normalized_stake
    next_stake = normalized_stake
    next_step = 1
    next_source_order_id = None

    def open_trade(item: dict[str, Any], _event_time: int) -> None:
        item["stake"] = round(next_stake, 4)
        item["win_return"] = round(next_stake * gross_return_ratio, 4)
        item["stake_progression_step"] = next_step
        item["stake_progression_source_order_id"] = next_source_order_id
        item["stake_progression_version"] = LEGACY_THREE_STAGE_VERSION

    def settle_trade(item: dict[str, Any], _event_time: int) -> None:
        nonlocal next_source_order_id, next_stake, next_step
        _apply_pnl(item)
        if item["result"] == "WIN" and item["stake_progression_step"] < 3:
            next_stake = item["win_return"]
            next_step = item["stake_progression_step"] + 1
            next_source_order_id = item["id"]
        else:
            next_stake = normalized_stake
            next_step = 1
            next_source_order_id = None

    _run_timeline(trades, on_open=open_trade, on_settle=settle_trade)
    return _build_result(
        trades,
        {
            "policy": "legacy_three_stage",
            "base_stake": normalized_stake,
            "base_win_return": normalized_return,
            "max_orders": 3,
            "version": LEGACY_THREE_STAGE_VERSION,
        },
    )


def _run_timeline(
    trades: Sequence[dict[str, Any]],
    *,
    on_open: Callable[[dict[str, Any], int], None],
    on_settle: Callable[[dict[str, Any], int], None],
) -> None:
    openings: dict[int, list[dict[str, Any]]] = defaultdict(list)
    settlements: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in trades:
        openings[item["opened_at"]].append(item)
        settlements[item["settled_at"]].append(item)

    for event_time in sorted(set(openings) | set(settlements)):
        due_before_open = [
            item
            for item in settlements.get(event_time, [])
            if item["opened_at"] < event_time
        ]
        for item in sorted(due_before_open, key=lambda row: row["id"]):
            on_settle(item, event_time)

        for item in sorted(openings.get(event_time, []), key=lambda row: row["id"]):
            on_open(item, event_time)

        zero_duration = [
            item
            for item in settlements.get(event_time, [])
            if item["opened_at"] == event_time
        ]
        for item in sorted(zero_duration, key=lambda row: row["id"]):
            on_settle(item, event_time)


def _normalize_trades(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(rows, (str, bytes, Mapping)):
        raise TypeError("rows must be an iterable of trade mappings")
    try:
        source_rows = list(rows)
    except TypeError as error:
        raise TypeError("rows must be an iterable of trade mappings") from error

    trades = []
    seen_ids = set()
    for index, source in enumerate(source_rows, start=1):
        if not isinstance(source, Mapping):
            raise TypeError(f"trade row {index} must be a mapping")
        item = dict(deepcopy(source))
        order_id = _positive_int(item.get("id", index), f"trade row {index} id")
        if order_id in seen_ids:
            raise ValueError(f"duplicate trade id: {order_id}")
        seen_ids.add(order_id)

        opened_value = _first_value(item, "opened_at", "entry_time")
        settled_value = _first_value(item, "settled_at", "exit_time", "expires_at")
        opened_at = _timestamp(opened_value, f"trade {order_id} opened_at")
        settled_at = _timestamp(settled_value, f"trade {order_id} settled_at")
        if settled_at < opened_at:
            raise ValueError(f"trade {order_id} settled_at must be >= opened_at")

        status = item.get("status")
        if status is not None and str(status).upper() != "SETTLED":
            raise ValueError(f"trade {order_id} must be SETTLED, got {status}")
        result = str(item.get("result", "")).upper()
        if result not in {"WIN", "LOSS"}:
            raise ValueError(f"trade {order_id} has invalid result: {item.get('result')}")
        direction = str(item.get("direction", "")).upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError(f"trade {order_id} has invalid direction: {item.get('direction')}")

        item["id"] = order_id
        item["opened_at"] = opened_at
        item["settled_at"] = settled_at
        item["direction"] = direction
        item["result"] = result
        trades.append(item)
    return sorted(trades, key=lambda item: (item["opened_at"], item["id"]))


def _validated_base_terms(base_stake: float, base_win_return: float) -> tuple[float, float]:
    ledger = TwoStageStakeProgression(
        enabled=False,
        base_stake=base_stake,
        base_win_return=base_win_return,
        max_active=1,
        max_open_orders=MAX_OPEN_ORDERS,
    )
    return round(ledger.base_stake, 4), round(ledger.base_win_return, 4)


def _first_value(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    raise ValueError(f"missing required field; expected one of: {', '.join(keys)}")


def _positive_int(value: Any, name: str) -> int:
    normalized = _integer(value, name)
    if normalized < 1:
        raise ValueError(f"{name} must be >= 1")
    return normalized


def _timestamp(value: Any, name: str) -> int:
    normalized = _integer(value, name)
    if normalized < 0:
        raise ValueError(f"{name} must be >= 0")
    return normalized


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return normalized


def _apply_pnl(item: dict[str, Any]) -> None:
    stake = float(item["stake"])
    win_return = float(item["win_return"])
    item["pnl"] = (
        round(win_return - stake, 4)
        if item["result"] == "WIN"
        else round(-stake, 4)
    )


def _build_result(trades: Sequence[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: (item["opened_at"], item["id"]))
    return {
        "config": config,
        "summary": _summary(ordered),
        "by_direction": _group(ordered, "direction"),
        "by_session": _group(ordered, "threshold_segment"),
        "by_profile": _group(ordered, "profile_key"),
        "trade_rows": ordered,
    }


def _summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(item.get("result") == "WIN" for item in rows)
    losses = sum(item.get("result") == "LOSS" for item in rows)
    pnl = round(sum(float(item.get("pnl", 0.0)) for item in rows), 4)
    total_staked = round(sum(float(item.get("stake", 0.0)) for item in rows), 4)
    second_stage = [
        item
        for item in rows
        if int(item.get("stake_progression_step", 1)) == 2
    ]
    second_stage_wins = sum(item.get("result") == "WIN" for item in second_stage)
    return {
        "orders": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(rows), 6) if rows else 0.0,
        "pnl": pnl,
        "ev": round(pnl / len(rows), 4) if rows else 0.0,
        "total_staked": total_staked,
        "roi": round(pnl / total_staked, 6) if total_staked else 0.0,
        "max_drawdown": _max_drawdown(rows),
        "peak_open_stake": _peak_open_stake(rows),
        "second_stage_orders": len(second_stage),
        "second_stage_wins": second_stage_wins,
        "second_stage_win_rate": (
            round(second_stage_wins / len(second_stage), 6)
            if second_stage
            else 0.0
        ),
    }


def _max_drawdown(rows: Sequence[dict[str, Any]]) -> float:
    balance = 0.0
    peak = 0.0
    drawdown = 0.0
    for item in sorted(rows, key=lambda row: (row["settled_at"], row["id"])):
        balance = round(balance + float(item.get("pnl", 0.0)), 4)
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    return round(drawdown, 4)


def _peak_open_stake(rows: Sequence[dict[str, Any]]) -> float:
    openings: dict[int, list[dict[str, Any]]] = defaultdict(list)
    settlements: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in rows:
        openings[item["opened_at"]].append(item)
        settlements[item["settled_at"]].append(item)

    active: dict[int, float] = {}
    peak = 0.0
    for event_time in sorted(set(openings) | set(settlements)):
        for item in settlements.get(event_time, []):
            if item["opened_at"] < event_time:
                active.pop(item["id"], None)
        for item in openings.get(event_time, []):
            active[item["id"]] = float(item.get("stake", 0.0))
        peak = max(peak, sum(active.values()))
        for item in settlements.get(event_time, []):
            if item["opened_at"] == event_time:
                active.pop(item["id"], None)
    return round(peak, 4)


def _group(rows: Sequence[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in rows:
        groups[str(item.get(field) or "UNKNOWN")].append(item)
    return [
        {field: value, **_summary(items)}
        for value, items in sorted(groups.items())
    ]


def _extract_trade_rows(payload: Any) -> tuple[list[Mapping[str, Any]], str]:
    if isinstance(payload, list):
        return payload, "root"
    if not isinstance(payload, Mapping):
        raise ValueError("input JSON must be an object or a trade list")
    for field in ("trade_rows", "traded", "orders"):
        value = payload.get(field)
        if isinstance(value, list):
            return value, field
    raise ValueError("input JSON does not contain a trade list in trade_rows, traded, or orders")


def _trade_signature(result: Mapping[str, Any]) -> list[tuple[int, str, str]]:
    return [
        (item["id"], item["direction"], item["result"])
        for item in result["trade_rows"]
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reprice a daily-profile replay with stake policies")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    rows, trade_field = _extract_trade_rows(payload)
    policies = {
        "fixed_10u": _reprice_fixed(
            rows,
            base_stake=10.0,
            base_win_return=18.0,
        ),
        "legacy_three_stage": _reprice_legacy_three_stage(
            rows,
            base_stake=10.0,
            base_win_return=18.0,
        ),
    }
    policies.update(
        {
            f"two_stage_max_active_{max_active}": reprice_trades(
                rows,
                base_stake=10.0,
                base_win_return=18.0,
                max_active=max_active,
            )
            for max_active in range(1, MAX_OPEN_ORDERS + 1)
        }
    )

    expected_signature = _trade_signature(policies["fixed_10u"])
    for name, result in policies.items():
        if _trade_signature(result) != expected_signature:
            raise AssertionError(
                f"stake policy {name} changed ordered (id, direction, result)"
            )

    report = {
        "input": {
            "path": str(args.input),
            "trade_field": trade_field,
            "orders": len(rows),
        },
        "policies": policies,
    }
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
