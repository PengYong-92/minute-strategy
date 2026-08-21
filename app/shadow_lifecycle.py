from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.shadow_models import ShadowEvaluationMetrics, ShadowParameterSnapshot


MIN_COMPLETE_DAYS = 7
MIN_SETTLED_ORDERS = 300
MIN_TOTAL_WIN_RATE = 0.60
MIN_DIRECTION_WIN_RATE = 0.5556
MIN_QUALIFIED_DAYS = 5
MIN_POSITIVE_EV_DAYS = 5
MIN_WIN_RATE_LIFT = 0.02
MIN_DAYS_BEATING_CHAMPION = 5
MIN_AVERAGE_ORDERS_PER_DAY = 35.0
CHAMPION_VOLUME_RETENTION = 0.70
TARGET_AVERAGE_ORDERS_PER_DAY = 50.0
_EPSILON = 1e-12


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    failures: tuple[str, ...]
    required_average_orders_per_day: float


@dataclass(frozen=True)
class ChallengerEvaluation:
    parameters: ShadowParameterSnapshot
    metrics: ShadowEvaluationMetrics
    complexity: int = 0

    def __post_init__(self) -> None:
        if type(self.complexity) is not int or self.complexity < 0:
            raise ValueError("complexity must be a non-negative integer")


@dataclass(frozen=True)
class RollbackDecision:
    should_rollback: bool
    reasons: tuple[str, ...]


def _below(actual: float, required: float) -> bool:
    return actual + _EPSILON < required


def _above(actual: float, limit: float) -> bool:
    return actual > limit + _EPSILON


def evaluate_promotion(
    candidate: ShadowEvaluationMetrics,
    champion: ShadowEvaluationMetrics,
) -> PromotionDecision:
    required_volume = max(
        MIN_AVERAGE_ORDERS_PER_DAY,
        champion.average_orders_per_day * CHAMPION_VOLUME_RETENTION,
    )
    failures: list[str] = []

    checks = (
        ("complete_days", candidate.complete_days >= MIN_COMPLETE_DAYS),
        ("settled_orders", candidate.settled_orders >= MIN_SETTLED_ORDERS),
        ("total_win_rate", not _below(candidate.win_rate, MIN_TOTAL_WIN_RATE)),
        ("long_win_rate", not _below(candidate.long_win_rate, MIN_DIRECTION_WIN_RATE)),
        ("short_win_rate", not _below(candidate.short_win_rate, MIN_DIRECTION_WIN_RATE)),
        (
            "qualified_win_rate_days",
            candidate.qualified_win_rate_days >= MIN_QUALIFIED_DAYS,
        ),
        ("positive_ev_days", candidate.positive_ev_days >= MIN_POSITIVE_EV_DAYS),
        (
            "win_rate_lift",
            not _below(candidate.win_rate - champion.win_rate, MIN_WIN_RATE_LIFT),
        ),
        (
            "days_beating_champion",
            candidate.days_beating_champion >= MIN_DAYS_BEATING_CHAMPION,
        ),
        (
            "average_orders_per_day",
            not _below(candidate.average_orders_per_day, required_volume),
        ),
        ("max_drawdown", not _above(candidate.max_drawdown, champion.max_drawdown)),
        (
            "max_loss_streak",
            candidate.max_loss_streak <= champion.max_loss_streak,
        ),
    )
    for name, passed in checks:
        if not passed:
            failures.append(name)
    return PromotionDecision(
        eligible=not failures,
        failures=tuple(failures),
        required_average_orders_per_day=required_volume,
    )


def _ranking_key(
    evaluation: ChallengerEvaluation,
    champion: ShadowEvaluationMetrics,
) -> tuple:
    metrics = evaluation.metrics
    return (
        -metrics.win_rate,
        -metrics.minimum_direction_win_rate,
        -metrics.worst_rolling_3d_win_rate,
        -(metrics.win_rate - champion.win_rate),
        abs(metrics.average_orders_per_day - TARGET_AVERAGE_ORDERS_PER_DAY),
        metrics.max_loss_streak,
        metrics.max_drawdown,
        -metrics.total_ev,
        -metrics.total_pnl,
        evaluation.complexity,
        evaluation.parameters.parameter_hash,
    )


def rank_eligible_challengers(
    evaluations: Iterable[ChallengerEvaluation],
    champion: ShadowEvaluationMetrics,
) -> tuple[ChallengerEvaluation, ...]:
    eligible = (
        evaluation
        for evaluation in evaluations
        if evaluate_promotion(evaluation.metrics, champion).eligible
    )
    return tuple(sorted(eligible, key=lambda item: _ranking_key(item, champion)))


def evaluate_rollback(
    current: ShadowEvaluationMetrics,
    old_champion: ShadowEvaluationMetrics,
) -> RollbackDecision:
    reasons: list[str] = []
    if current.current_loss_streak >= 8:
        reasons.append("consecutive_losses")
    if current.settled_orders >= 50 and _below(current.win_rate, 0.50):
        reasons.append("fifty_order_win_rate")
    if (
        current.settled_orders >= 100
        and _below(current.win_rate, MIN_DIRECTION_WIN_RATE)
        and _above(old_champion.win_rate - current.win_rate, 0.05)
    ):
        reasons.append("hundred_order_relative_underperformance")
    if (
        current.settled_orders >= 20
        and old_champion.settled_orders >= 20
        and _above(current.max_drawdown, old_champion.max_drawdown * 1.25)
    ):
        reasons.append("drawdown")
    return RollbackDecision(
        should_rollback=bool(reasons),
        reasons=tuple(reasons),
    )
