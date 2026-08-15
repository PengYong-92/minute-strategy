from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from app.daily_profile_selector import profile_key


SHANGHAI = ZoneInfo("Asia/Shanghai")
LOOKBACK_HOURS = 24
EVALUATION_INTERVAL_HOURS = 4
MIN_SAMPLES = 12
WATCH_MIN_WIN_RATE = 0.50
HEALTHY_MIN_WIN_RATE = 10.0 / 18.0


@dataclass(frozen=True)
class ProfileHealthGuardConfig:
    enabled: bool = True


@dataclass(frozen=True)
class ProfileHealthGuardDecision:
    enabled: bool
    status: str
    direction: str
    evaluated_at: int
    next_evaluation_at: int
    lookback_start: int
    lookback_end: int
    sample_size: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    pnl: float = 0.0
    ev: float = 0.0
    blocked: bool = False
    allow_second_order: bool = True
    allow_progression: bool = True
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_profile_health_guard(
    observations: Sequence[Any],
    *,
    current_time: int,
    direction: str,
    selected_profiles: Sequence[dict],
    config: ProfileHealthGuardConfig | None = None,
) -> ProfileHealthGuardDecision:
    resolved = config or ProfileHealthGuardConfig()
    target_direction = str(direction or "").upper()
    evaluated_at, next_evaluation_at = _evaluation_boundaries(int(current_time))
    lookback_start = evaluated_at - LOOKBACK_HOURS * 60 * 60 * 1000
    scope = {
        "enabled": bool(resolved.enabled),
        "direction": target_direction,
        "evaluated_at": evaluated_at,
        "next_evaluation_at": next_evaluation_at,
        "lookback_start": lookback_start,
        "lookback_end": evaluated_at,
    }
    if not resolved.enabled:
        return ProfileHealthGuardDecision(
            status="DISABLED",
            reason="画像短窗健康守卫已关闭",
            **scope,
        )

    selected_keys = {
        str(item.get("key", ""))
        for item in selected_profiles
        if str(item.get("direction", "")).upper() == target_direction
        and item.get("key")
    }
    if target_direction not in {"LONG", "SHORT"} or not selected_keys:
        return ProfileHealthGuardDecision(
            status="NOT_APPLICABLE",
            reason="当前方向没有已启用每日画像",
            **scope,
        )

    candidates = []
    for item in observations:
        opened_at = _int_value(_get(item, "opened_at", 0))
        settled_at = _int_value(_get(item, "settled_at", 0))
        result = str(_get(item, "result", "") or "")
        item_direction = str(_get(item, "direction", "") or "").upper()
        if (
            str(_get(item, "status", "") or "") != "SETTLED"
            or result not in {"WIN", "LOSS"}
            or item_direction != target_direction
            or opened_at < lookback_start
            or opened_at >= evaluated_at
            or settled_at <= 0
            or settled_at > evaluated_at
        ):
            continue
        key = profile_key(
            _int_value(_get(item, "timeframe_minutes", 0)),
            str(_get(item, "strategy_family", "unknown") or "unknown"),
            str(_get(item, "strategy_tag", "unknown") or "unknown"),
            item_direction,
            str(_get(item, "threshold_segment", "GLOBAL") or "GLOBAL"),
        )
        if key in selected_keys:
            candidates.append((key, item))

    samples = []
    next_independent_at: dict[str, int] = {}
    for key, item in sorted(
        candidates,
        key=lambda pair: (
            _int_value(_get(pair[1], "opened_at", 0)),
            str(_get(pair[1], "observation_key", "") or ""),
        ),
    ):
        opened_at = _int_value(_get(item, "opened_at", 0))
        if opened_at < next_independent_at.get(key, 0):
            continue
        samples.append(item)
        next_independent_at[key] = _int_value(_get(item, "expires_at", opened_at))

    sample_size = len(samples)
    wins = sum(1 for item in samples if _get(item, "result", "") == "WIN")
    pnl = round(sum(float(_get(item, "pnl", 0.0) or 0.0) for item in samples), 4)
    win_rate = wins / sample_size if sample_size else 0.0
    ev = round(pnl / sample_size, 4) if sample_size else 0.0
    metrics = {
        "sample_size": sample_size,
        "wins": wins,
        "losses": sample_size - wins,
        "win_rate": round(win_rate, 6),
        "pnl": pnl,
        "ev": ev,
    }
    if sample_size < MIN_SAMPLES:
        return ProfileHealthGuardDecision(
            status="WARMUP",
            reason=f"画像短窗独立样本 {sample_size} < {MIN_SAMPLES}，保持原开单口径",
            **scope,
            **metrics,
        )
    if win_rate >= HEALTHY_MIN_WIN_RATE and ev >= 0:
        return ProfileHealthGuardDecision(
            status="HEALTHY",
            reason="画像短窗胜率和EV健康，保持原开单口径",
            **scope,
            **metrics,
        )
    if win_rate >= WATCH_MIN_WIN_RATE:
        return ProfileHealthGuardDecision(
            status="WATCH",
            allow_second_order=False,
            allow_progression=False,
            reason="画像短窗接近但低于盈亏平衡，仅允许基础首单",
            **scope,
            **metrics,
        )
    return ProfileHealthGuardDecision(
        status="DEGRADED",
        blocked=True,
        allow_second_order=False,
        allow_progression=False,
        reason="画像短窗胜率低于50%，暂停该方向至下一次4小时评估",
        **scope,
        **metrics,
    )


def _evaluation_boundaries(current_time: int) -> tuple[int, int]:
    current = datetime.fromtimestamp(current_time / 1000, tz=SHANGHAI)
    evaluated = current.replace(
        hour=(current.hour // EVALUATION_INTERVAL_HOURS) * EVALUATION_INTERVAL_HOURS,
        minute=0,
        second=0,
        microsecond=0,
    )
    return (
        int(evaluated.timestamp() * 1000),
        int((evaluated + timedelta(hours=EVALUATION_INTERVAL_HOURS)).timestamp() * 1000),
    )


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
