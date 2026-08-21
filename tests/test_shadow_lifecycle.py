import dataclasses
import unittest

from app.shadow_lifecycle import (
    ChallengerEvaluation,
    evaluate_promotion,
    evaluate_rollback,
    rank_eligible_challengers,
)
from app.shadow_models import ShadowEvaluationMetrics, ShadowParameterSnapshot


def metrics(**overrides) -> ShadowEvaluationMetrics:
    values = {
        "complete_days": 7,
        "settled_orders": 300,
        "wins": 186,
        "long_orders": 150,
        "long_wins": 90,
        "short_orders": 150,
        "short_wins": 96,
        "qualified_win_rate_days": 5,
        "positive_ev_days": 5,
        "days_beating_champion": 5,
        "average_orders_per_day": 42.86,
        "worst_rolling_3d_win_rate": 0.58,
        "total_ev": 100.0,
        "total_pnl": 80.0,
        "max_drawdown": 30.0,
        "max_loss_streak": 4,
        "current_loss_streak": 0,
    }
    values.update(overrides)
    return ShadowEvaluationMetrics(**values)


def champion(**overrides) -> ShadowEvaluationMetrics:
    values = {
        "complete_days": 7,
        "settled_orders": 350,
        "wins": 210,
        "long_orders": 175,
        "long_wins": 105,
        "short_orders": 175,
        "short_wins": 105,
        "qualified_win_rate_days": 4,
        "positive_ev_days": 4,
        "days_beating_champion": 0,
        "average_orders_per_day": 50.0,
        "worst_rolling_3d_win_rate": 0.56,
        "total_ev": 70.0,
        "total_pnl": 56.0,
        "max_drawdown": 30.0,
        "max_loss_streak": 4,
        "current_loss_streak": 0,
    }
    values.update(overrides)
    return ShadowEvaluationMetrics(**values)


def evaluation(name: str, candidate_metrics: ShadowEvaluationMetrics, complexity: int = 1):
    return ChallengerEvaluation(
        parameters=ShadowParameterSnapshot(
            family="profile_admission",
            version="v1",
            parameters={"name": name},
        ),
        metrics=candidate_metrics,
        complexity=complexity,
    )


class PromotionLifecycleTest(unittest.TestCase):
    def test_exact_promotion_boundaries_pass(self):
        candidate = metrics(
            wins=180,
            long_wins=84,
            short_wins=96,
            average_orders_per_day=35.0,
        )
        current = champion(
            settled_orders=300,
            wins=174,
            long_orders=150,
            long_wins=87,
            short_orders=150,
            short_wins=87,
        )

        decision = evaluate_promotion(candidate, current)

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.failures, ())
        self.assertEqual(decision.required_average_orders_per_day, 35.0)

    def test_each_promotion_gate_is_enforced(self):
        base = metrics()
        cases = {
            "complete_days": dataclasses.replace(base, complete_days=6),
            "settled_orders": dataclasses.replace(
                base,
                settled_orders=299,
                wins=185,
                long_orders=149,
                long_wins=89,
                short_wins=96,
            ),
            "total_win_rate": dataclasses.replace(base, wins=179, long_wins=83),
            "long_win_rate": dataclasses.replace(base, long_wins=83, short_wins=103),
            "short_win_rate": dataclasses.replace(base, long_wins=103, short_wins=83),
            "qualified_win_rate_days": dataclasses.replace(base, qualified_win_rate_days=4),
            "positive_ev_days": dataclasses.replace(base, positive_ev_days=4),
            "win_rate_lift": dataclasses.replace(base, wins=185, short_wins=95),
            "days_beating_champion": dataclasses.replace(base, days_beating_champion=4),
            "average_orders_per_day": dataclasses.replace(base, average_orders_per_day=34.99),
            "max_drawdown": dataclasses.replace(base, max_drawdown=30.01),
            "max_loss_streak": dataclasses.replace(base, max_loss_streak=5),
        }

        for expected_failure, candidate in cases.items():
            with self.subTest(expected_failure=expected_failure):
                decision = evaluate_promotion(candidate, champion())
                self.assertFalse(decision.eligible)
                self.assertIn(expected_failure, decision.failures)

    def test_volume_gate_tracks_seventy_percent_of_higher_volume_champion(self):
        decision = evaluate_promotion(
            metrics(average_orders_per_day=41.99),
            champion(average_orders_per_day=60.0),
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.required_average_orders_per_day, 42.0)
        self.assertIn("average_orders_per_day", decision.failures)


class ChallengerRankingTest(unittest.TestCase):
    def test_ranking_is_deterministic_across_input_order(self):
        current = champion(
            settled_orders=300,
            wins=180,
            long_orders=150,
            long_wins=90,
            short_orders=150,
            short_wins=90,
        )
        first = evaluation("first", metrics(total_ev=110.0))
        second = evaluation("second", metrics(total_ev=105.0))

        forward = rank_eligible_challengers([second, first], current)
        reverse = rank_eligible_challengers([first, second], current)

        self.assertEqual(
            [item.parameters.parameter_hash for item in forward],
            [item.parameters.parameter_hash for item in reverse],
        )
        self.assertEqual(forward[0].parameters.parameters["name"], "first")

    def test_ranking_uses_quality_volume_risk_value_complexity_then_hash(self):
        current = champion(
            settled_orders=300,
            wins=180,
            long_orders=150,
            long_wins=90,
            short_orders=150,
            short_wins=90,
        )
        candidates = [
            evaluation("base", metrics()),
            evaluation("total-rate", metrics(wins=189, long_wins=93, short_wins=96)),
            evaluation("direction-floor", metrics(long_wins=91, short_wins=95)),
            evaluation("rolling", metrics(worst_rolling_3d_win_rate=0.59)),
            evaluation("near-volume", metrics(average_orders_per_day=49.0)),
            evaluation("short-streak", metrics(max_loss_streak=3)),
            evaluation("low-drawdown", metrics(max_drawdown=29.0)),
            evaluation("high-ev", metrics(total_ev=101.0)),
            evaluation("simple", metrics(), complexity=0),
        ]

        ranked = rank_eligible_challengers(candidates, current)
        names = [item.parameters.parameters["name"] for item in ranked]

        self.assertEqual(names[0], "total-rate")
        self.assertLess(names.index("direction-floor"), names.index("rolling"))
        self.assertLess(names.index("rolling"), names.index("near-volume"))
        self.assertLess(names.index("near-volume"), names.index("short-streak"))
        self.assertLess(names.index("short-streak"), names.index("low-drawdown"))
        self.assertLess(names.index("low-drawdown"), names.index("high-ev"))
        self.assertLess(names.index("high-ev"), names.index("simple"))
        self.assertLess(names.index("simple"), names.index("base"))

    def test_ranking_excludes_ineligible_challengers(self):
        current = champion(
            settled_orders=300,
            wins=180,
            long_orders=150,
            long_wins=90,
            short_orders=150,
            short_wins=90,
        )
        passing = evaluation("passing", metrics())
        failing = evaluation("failing", metrics(complete_days=6))

        ranked = rank_eligible_challengers([failing, passing], current)

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].parameters.parameters["name"], "passing")


class RollbackLifecycleTest(unittest.TestCase):
    def test_each_rollback_rule_triggers_independently(self):
        old = champion(max_drawdown=40.0)
        cases = {
            "consecutive_losses": metrics(current_loss_streak=8),
            "fifty_order_win_rate": metrics(
                settled_orders=50,
                wins=24,
                long_orders=25,
                long_wins=12,
                short_orders=25,
                short_wins=12,
            ),
            "hundred_order_relative_underperformance": metrics(
                settled_orders=100,
                wins=54,
                long_orders=50,
                long_wins=27,
                short_orders=50,
                short_wins=27,
            ),
            "drawdown": metrics(max_drawdown=50.01),
        }

        for expected_reason, current in cases.items():
            with self.subTest(expected_reason=expected_reason):
                decision = evaluate_rollback(current, old)
                self.assertTrue(decision.should_rollback)
                self.assertIn(expected_reason, decision.reasons)

    def test_rollback_boundaries_do_not_trigger(self):
        old = champion(max_drawdown=40.0)
        current = metrics(
            settled_orders=100,
            wins=55,
            long_orders=50,
            long_wins=27,
            short_orders=50,
            short_wins=28,
            current_loss_streak=7,
            max_drawdown=50.0,
        )

        decision = evaluate_rollback(current, old)

        self.assertFalse(decision.should_rollback)
        self.assertEqual(decision.reasons, ())

    def test_drawdown_comparison_requires_twenty_forward_orders_per_side(self):
        current = metrics(
            complete_days=1,
            settled_orders=1,
            wins=0,
            long_orders=1,
            long_wins=0,
            short_orders=0,
            short_wins=0,
            qualified_win_rate_days=0,
            positive_ev_days=0,
            days_beating_champion=0,
            average_orders_per_day=1.0,
            max_drawdown=10.0,
            max_loss_streak=1,
            current_loss_streak=1,
        )
        old = champion(
            complete_days=1,
            settled_orders=1,
            wins=1,
            long_orders=1,
            long_wins=1,
            short_orders=0,
            short_wins=0,
            qualified_win_rate_days=1,
            positive_ev_days=1,
            average_orders_per_day=1.0,
            max_drawdown=0.0,
            max_loss_streak=0,
        )

        decision = evaluate_rollback(current, old)

        self.assertFalse(decision.should_rollback)


if __name__ == "__main__":
    unittest.main()
