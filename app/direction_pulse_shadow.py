from __future__ import annotations

from typing import Any, Sequence


DIRECTION_PULSE_SHADOW_VERSION = "DIRECTION_PULSE_V1_SHADOW"
DIRECTION_PULSE_WINDOWS = (12, 16)
NORMAL_MIN_WIN_RATE = 0.50
WATCH_MIN_WIN_RATE = 0.40


def evaluate_direction_pulse_shadow(
    observations: Sequence[Any],
    *,
    current_time: int,
) -> dict:
    evaluated_at = int(current_time)
    return {
        "version": DIRECTION_PULSE_SHADOW_VERSION,
        "mode": "SHADOW_ONLY",
        "refresh_mode": "SETTLEMENT_EVENT",
        "evaluated_at": evaluated_at,
        "windows": list(DIRECTION_PULSE_WINDOWS),
        "directions": {
            direction: {
                str(window): _evaluate_window(
                    observations,
                    current_time=evaluated_at,
                    direction=direction,
                    window=window,
                )
                for window in DIRECTION_PULSE_WINDOWS
            }
            for direction in ("LONG", "SHORT")
        },
    }


def attach_candidate_shadow(
    snapshot: dict,
    *,
    direction: str,
    order_slot: str,
) -> dict:
    normalized_direction = str(direction or "").upper()
    normalized_slot = "SECOND" if str(order_slot or "").upper() == "SECOND" else "FIRST"
    direction_windows = (snapshot.get("directions") or {}).get(normalized_direction, {})
    windows = {}
    for window in DIRECTION_PULSE_WINDOWS:
        source = dict(direction_windows.get(str(window), {}))
        action = str(source.get("hypothetical_action", "ALLOW"))
        source["would_block"] = action == "BLOCK_DIRECTION" or (
            action == "BLOCK_SECOND" and normalized_slot == "SECOND"
        )
        windows[str(window)] = source
    return {
        "version": snapshot.get("version", DIRECTION_PULSE_SHADOW_VERSION),
        "mode": "SHADOW_ONLY",
        "refresh_mode": "SETTLEMENT_EVENT",
        "direction": normalized_direction,
        "order_slot": normalized_slot,
        "evaluated_at": int(snapshot.get("evaluated_at", 0) or 0),
        "windows": windows,
    }


def _evaluate_window(
    observations: Sequence[Any],
    *,
    current_time: int,
    direction: str,
    window: int,
) -> dict:
    samples = _independent_direction_samples(
        observations,
        current_time=current_time,
        direction=direction,
    )[-window:]
    sample_size = len(samples)
    wins = sum(1 for item in samples if _get(item, "result", "") == "WIN")
    pnl = round(sum(float(_get(item, "pnl", 0.0) or 0.0) for item in samples), 4)
    win_rate = wins / sample_size if sample_size else 0.0
    if sample_size < window:
        status = "WARMUP"
        action = "ALLOW"
    elif win_rate >= NORMAL_MIN_WIN_RATE:
        status = "NORMAL"
        action = "ALLOW"
    elif win_rate >= WATCH_MIN_WIN_RATE:
        status = "WATCH"
        action = "BLOCK_SECOND"
    else:
        status = "DEGRADED"
        action = "BLOCK_DIRECTION"
    return {
        "window_size": window,
        "status": status,
        "sample_size": sample_size,
        "wins": wins,
        "losses": sample_size - wins,
        "win_rate": round(win_rate, 6),
        "pnl": pnl,
        "ev": round(pnl / sample_size, 4) if sample_size else 0.0,
        "first_opened_at": int(_get(samples[0], "opened_at", 0)) if samples else 0,
        "last_settled_at": max(
            (int(_get(item, "settled_at", 0) or 0) for item in samples),
            default=0,
        ),
        "hypothetical_action": action,
    }


def _independent_direction_samples(
    observations: Sequence[Any],
    *,
    current_time: int,
    direction: str,
) -> list[Any]:
    candidates = [
        item
        for item in observations
        if str(_get(item, "status", "") or "") == "SETTLED"
        and str(_get(item, "result", "") or "") in {"WIN", "LOSS"}
        and str(_get(item, "direction", "") or "").upper() == direction
        and int(_get(item, "settled_at", 0) or 0) <= current_time
    ]
    independent = []
    next_independent_at = 0
    for item in sorted(
        candidates,
        key=lambda row: (
            int(_get(row, "opened_at", 0) or 0),
            str(_get(row, "observation_key", "") or ""),
        ),
    ):
        opened_at = int(_get(item, "opened_at", 0) or 0)
        if opened_at < next_independent_at:
            continue
        independent.append(item)
        next_independent_at = int(_get(item, "expires_at", opened_at) or opened_at)
    return independent


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)
