from __future__ import annotations

from typing import Any, Sequence


DIRECTION_PULSE_SHADOW_VERSION = "DIRECTION_PULSE_V1_SHADOW"
DIRECTION_PULSE_WINDOWS = (12, 16)
NORMAL_MIN_WIN_RATE = 0.50
WATCH_MIN_WIN_RATE = 0.40


def empty_direction_pulse_shadow(*, current_time: int = 0) -> dict:
    return {
        "version": DIRECTION_PULSE_SHADOW_VERSION,
        "mode": "SHADOW_ONLY",
        "refresh_mode": "SETTLEMENT_EVENT",
        "evaluated_at": _safe_int(current_time),
        "windows": list(DIRECTION_PULSE_WINDOWS),
        "directions": {
            direction: {
                str(window): _empty_window(window)
                for window in DIRECTION_PULSE_WINDOWS
            }
            for direction in ("LONG", "SHORT")
        },
    }


def evaluate_direction_pulse_shadow(
    observations: Sequence[Any],
    *,
    current_time: int,
) -> dict:
    evaluated_at = _safe_int(current_time)
    snapshot = empty_direction_pulse_shadow(current_time=evaluated_at)
    snapshot["directions"] = {
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
    }
    return snapshot


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
        raw_source = direction_windows.get(str(window), {})
        source = dict(raw_source) if isinstance(raw_source, dict) else _empty_window(window)
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
        "evaluated_at": _safe_int(snapshot.get("evaluated_at", 0)),
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
    pnl = round(sum(_safe_float(_get(item, "pnl", 0.0)) for item in samples), 4)
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
        "first_opened_at": _safe_int(_get(samples[0], "opened_at", 0)) if samples else 0,
        "last_settled_at": max(
            (_safe_int(_get(item, "settled_at", 0)) for item in samples),
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
    candidates = []
    for item in observations:
        settled_at = _optional_int(_get(item, "settled_at", 0))
        opened_at = _optional_int(_get(item, "opened_at", 0))
        expires_at = _optional_int(_get(item, "expires_at", 0))
        if (
            str(_get(item, "status", "") or "") == "SETTLED"
            and str(_get(item, "result", "") or "") in {"WIN", "LOSS"}
            and str(_get(item, "direction", "") or "").upper() == direction
            and settled_at is not None
            and opened_at is not None
            and expires_at is not None
            and settled_at <= current_time
        ):
            candidates.append(item)
    independent = []
    next_independent_at = 0
    for item in sorted(
        candidates,
        key=lambda row: (
            _safe_int(_get(row, "opened_at", 0)),
            str(_get(row, "observation_key", "") or ""),
        ),
    ):
        opened_at = _safe_int(_get(item, "opened_at", 0))
        if opened_at < next_independent_at:
            continue
        independent.append(item)
        next_independent_at = _safe_int(_get(item, "expires_at", opened_at), opened_at)
    return independent


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _empty_window(window: int) -> dict:
    return {
        "window_size": window,
        "status": "WARMUP",
        "sample_size": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "pnl": 0.0,
        "ev": 0.0,
        "first_opened_at": 0,
        "last_settled_at": 0,
        "hypothetical_action": "ALLOW",
    }


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    converted = _optional_int(value)
    return default if converted is None else converted


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default
