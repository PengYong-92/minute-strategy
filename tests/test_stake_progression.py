import unittest
import sys

from app.stake_progression import (
    TWO_STAGE_VERSION,
    OrderTerms,
    StakeProgressionCredit,
    TwoStageStakeProgression,
)


class TwoStageStakeProgressionTest(unittest.TestCase):
    def ledger(
        self,
        *,
        enabled=True,
        max_active=1,
        credits=(),
        active_second_orders=None,
        active_second_order_ids=None,
        activated_at=1_000,
    ):
        return TwoStageStakeProgression(
            base_stake=10.0,
            base_win_return=18.0,
            enabled=enabled,
            max_active=max_active,
            max_open_orders=5,
            activated_at=activated_at,
            credits=credits,
            active_second_orders=active_second_orders,
            active_second_order_ids=active_second_order_ids,
        )

    def win_first_stage(self, ledger, order_id=1, opened_at=1_000, settled_at=601_000):
        return ledger.settle(
            order_id,
            order_opened_at=opened_at,
            step=1,
            result="WIN",
            settled_at=settled_at,
        )

    def test_first_stage_win_creates_credit_and_next_order_uses_18u(self):
        ledger = self.ledger()

        credit = self.win_first_stage(ledger)
        terms, consumed = ledger.assign(order_id=2, opened_at=602_000)

        self.assertIsNotNone(credit)
        self.assertEqual(credit.source_order_id, 1)
        self.assertEqual(credit.credit_id, f"{TWO_STAGE_VERSION}:1")
        self.assertEqual(
            terms,
            OrderTerms(
                stake=18.0,
                win_return=32.4,
                step=2,
                source_order_id=1,
                version=TWO_STAGE_VERSION,
            ),
        )
        self.assertIs(consumed, credit)
        self.assertEqual(consumed.status, "CONSUMED")
        self.assertEqual(consumed.consumed_order_id, 2)
        self.assertEqual(consumed.consumed_at, 602_000)

    def test_first_stage_loss_keeps_next_order_at_10u(self):
        ledger = self.ledger()

        created = ledger.settle(1, 1_000, 1, "LOSS", 601_000)
        terms, consumed = ledger.assign(2, 602_000)

        self.assertIsNone(created)
        self.assertIsNone(consumed)
        self.assertEqual((terms.stake, terms.win_return, terms.step), (10.0, 18.0, 1))

    def test_second_stage_win_ends_chain_and_returns_to_10u(self):
        ledger = self.ledger()
        self.win_first_stage(ledger)
        ledger.assign(2, 602_000)

        created = ledger.settle(2, 602_000, 2, "WIN", 1_202_000)
        terms, consumed = ledger.assign(3, 1_203_000)

        self.assertIsNone(created)
        self.assertIsNone(consumed)
        self.assertEqual((terms.stake, terms.win_return, terms.step), (10.0, 18.0, 1))

    def test_second_stage_loss_ends_chain_and_returns_to_10u(self):
        ledger = self.ledger()
        self.win_first_stage(ledger)
        ledger.assign(2, 602_000)

        created = ledger.settle(2, 602_000, 2, "LOSS", 1_202_000)
        terms, consumed = ledger.assign(3, 1_203_000)

        self.assertIsNone(created)
        self.assertIsNone(consumed)
        self.assertEqual((terms.stake, terms.win_return, terms.step), (10.0, 18.0, 1))

    def test_capacity_full_creates_cancelled_audit_credit(self):
        ledger = self.ledger()

        first = self.win_first_stage(ledger, order_id=1, settled_at=601_000)
        second = self.win_first_stage(ledger, order_id=2, settled_at=602_000)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(second.status, "CANCELLED")
        self.assertEqual(ledger.status()["pending_credits"], 1)
        self.assertEqual(len(ledger.credits), 2)

    def test_cancelled_capacity_credit_stays_cancelled_after_restart_and_replay(self):
        ledger = self.ledger()
        self.win_first_stage(ledger, order_id=1, settled_at=601_000)
        cancelled = self.win_first_stage(ledger, order_id=2, settled_at=602_000)
        restored = self.ledger(credits=[cancelled])

        replayed = self.win_first_stage(restored, order_id=2, settled_at=999_000)

        self.assertIs(replayed, cancelled)
        self.assertEqual(replayed.status, "CANCELLED")
        self.assertEqual(restored.status()["pending_credits"], 0)

    def test_capacity_two_accepts_two_simultaneous_first_stage_wins(self):
        ledger = self.ledger(max_active=2)

        first = self.win_first_stage(ledger, order_id=1, settled_at=601_000)
        second = self.win_first_stage(ledger, order_id=2, settled_at=602_000)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(ledger.status()["pending_credits"], 2)

    def test_assign_consumes_oldest_credit_by_created_at_then_credit_id(self):
        credits = [
            StakeProgressionCredit(
                source_order_id=3,
                created_at=602_000,
                credit_id="credit-c",
            ),
            StakeProgressionCredit(
                source_order_id=2,
                created_at=601_000,
                credit_id="credit-b",
            ),
            StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
                credit_id="credit-a",
            ),
        ]
        ledger = self.ledger(max_active=3, credits=credits)

        terms, consumed = ledger.assign(10, 700_000)

        self.assertEqual(consumed.credit_id, "credit-a")
        self.assertEqual(terms.source_order_id, 1)

    def test_assign_does_not_consume_credit_created_after_order_opened(self):
        future = StakeProgressionCredit(source_order_id=1, created_at=700_000)
        ledger = self.ledger(credits=[future])

        base_terms, not_consumed = ledger.assign(2, 699_999)
        second_terms, consumed = ledger.assign(3, 700_000)

        self.assertIsNone(not_consumed)
        self.assertEqual((base_terms.stake, base_terms.step), (10.0, 1))
        self.assertEqual(future.status, "CONSUMED")
        self.assertIs(consumed, future)
        self.assertEqual((second_terms.stake, second_terms.step), (18.0, 2))

    def test_activation_time_is_inclusive(self):
        ledger = self.ledger(activated_at=1_000)

        before = self.win_first_stage(ledger, order_id=1, opened_at=999)
        at_boundary = self.win_first_stage(ledger, order_id=2, opened_at=1_000)

        self.assertIsNone(before)
        self.assertIsNotNone(at_boundary)

    def test_duplicate_first_stage_settlement_is_idempotent_even_when_capacity_full(self):
        ledger = self.ledger(max_active=1)
        first = self.win_first_stage(ledger, order_id=1)

        duplicate = ledger.settle(1, 1_000, 1, "WIN", 999_000)

        self.assertIs(duplicate, first)
        self.assertEqual(len(ledger.credits), 1)
        self.assertEqual(ledger.status()["pending_credits"], 1)

    def test_disabled_ledger_neither_creates_nor_consumes_credits(self):
        pending = StakeProgressionCredit(source_order_id=1, created_at=601_000)
        ledger = self.ledger(enabled=False, credits=[pending])

        created = self.win_first_stage(ledger, order_id=2)
        terms, consumed = ledger.assign(3, 602_000)

        self.assertIsNone(created)
        self.assertIsNone(consumed)
        self.assertEqual(pending.status, "CANCELLED")
        self.assertEqual((terms.stake, terms.win_return, terms.step), (10.0, 18.0, 1))

    def test_cancel_pending_preserves_consumed_history(self):
        pending = StakeProgressionCredit(source_order_id=1, created_at=601_000)
        consumed = StakeProgressionCredit(
            source_order_id=2,
            created_at=602_000,
            status="CONSUMED",
            consumed_order_id=20,
            consumed_at=603_000,
        )
        ledger = self.ledger(
            max_active=2,
            credits=[pending, consumed],
            active_second_order_ids=[20],
        )

        cancelled = ledger.cancel_pending()

        self.assertEqual(cancelled, [pending])
        self.assertEqual(pending.status, "CANCELLED")
        self.assertEqual(consumed.status, "CONSUMED")
        self.assertEqual(consumed.consumed_order_id, 20)

    def test_restore_discards_newest_pending_credits_above_capacity(self):
        oldest = StakeProgressionCredit(source_order_id=1, created_at=601_000)
        middle = StakeProgressionCredit(source_order_id=2, created_at=602_000)
        newest = StakeProgressionCredit(source_order_id=3, created_at=603_000)
        active = StakeProgressionCredit(
            source_order_id=4,
            created_at=600_000,
            status="CONSUMED",
            consumed_order_id=20,
            consumed_at=600_500,
        )

        ledger = self.ledger(
            max_active=2,
            credits=[newest, oldest, middle, active],
            active_second_order_ids=[20],
        )

        self.assertEqual(oldest.status, "PENDING")
        self.assertEqual(middle.status, "CANCELLED")
        self.assertEqual(newest.status, "CANCELLED")
        self.assertEqual(ledger.status()["pending_credits"], 1)
        self.assertEqual(ledger.status()["active_second_stage"], 1)

    def test_restore_keeps_active_orders_when_the_limit_is_reduced(self):
        credits = [
            StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
                status="CONSUMED",
                consumed_order_id=20,
                consumed_at=602_000,
            ),
            StakeProgressionCredit(
                source_order_id=2,
                created_at=603_000,
                status="CONSUMED",
                consumed_order_id=21,
                consumed_at=604_000,
            ),
            StakeProgressionCredit(source_order_id=3, created_at=605_000),
        ]

        ledger = self.ledger(
            max_active=1,
            credits=credits,
            active_second_order_ids=[20, 21],
        )

        self.assertEqual(ledger.active_second_order_ids, frozenset({20, 21}))
        self.assertEqual(credits[2].status, "CANCELLED")
        self.assertEqual(ledger.status()["pending_credits"], 0)
        self.assertEqual(ledger.status()["active_second_stage"], 2)

        ledger.settle(20, 602_000, 2, "WIN", 1_202_000)
        self.assertEqual(ledger.status()["active_second_stage"], 1)
        blocked = ledger.settle(4, 606_000, 1, "WIN", 1_206_000)
        self.assertEqual(blocked.status, "CANCELLED")

    def test_reconcile_restores_active_ids_and_releases_matching_order(self):
        consumed = StakeProgressionCredit(
            source_order_id=1,
            created_at=601_000,
            status="CONSUMED",
            consumed_order_id=2,
            consumed_at=602_000,
        )
        ledger = self.ledger(max_active=2, credits=[consumed])

        ledger.reconcile(active_second_order_ids=[2])
        full = ledger.status()
        ledger.settle(2, 602_000, 2, "LOSS", 1_202_000)
        released = ledger.status()

        self.assertEqual(full["active_second_stage"], 1)
        self.assertEqual(released["active_second_stage"], 0)

    def test_active_second_stage_settlement_releases_matching_slot_only_once(self):
        credits = [
            StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=602_000,
            ),
            StakeProgressionCredit(
                source_order_id=4,
                created_at=603_000,
                status="CONSUMED",
                consumed_order_id=3,
                consumed_at=604_000,
            ),
        ]
        ledger = self.ledger(
            max_active=2,
            credits=credits,
            active_second_order_ids=[2, 3],
        )

        ledger.settle(2, 602_000, 2, "WIN", 1_202_000)
        ledger.settle(2, 602_000, 2, "WIN", 1_202_000)

        self.assertEqual(ledger.status()["active_second_stage"], 1)
        self.assertEqual(ledger.active_second_order_ids, frozenset({3}))

    def test_historical_consumed_order_settlement_does_not_release_current_active(self):
        historical = StakeProgressionCredit(
            source_order_id=1,
            created_at=601_000,
            status="CONSUMED",
            consumed_order_id=10,
            consumed_at=602_000,
        )
        current = StakeProgressionCredit(
            source_order_id=2,
            created_at=603_000,
            status="CONSUMED",
            consumed_order_id=20,
            consumed_at=604_000,
        )
        ledger = self.ledger(
            max_active=2,
            credits=[historical, current],
            active_second_order_ids=[20],
        )

        ledger.settle(10, 602_000, 2, "WIN", 1_202_000)

        self.assertEqual(ledger.status()["active_second_stage"], 1)
        self.assertEqual(ledger.active_second_order_ids, frozenset({20}))

    def test_unknown_second_stage_order_does_not_release_active_slot(self):
        active = StakeProgressionCredit(
            source_order_id=1,
            created_at=601_000,
            status="CONSUMED",
            consumed_order_id=20,
            consumed_at=602_000,
        )
        ledger = self.ledger(
            max_active=2,
            credits=[active],
            active_second_order_ids=[20],
        )

        ledger.settle(999, 602_000, 2, "LOSS", 1_202_000)

        self.assertEqual(ledger.status()["active_second_stage"], 1)
        self.assertEqual(ledger.active_second_order_ids, frozenset({20}))

    def test_assign_adds_consuming_order_to_active_ids(self):
        ledger = self.ledger()
        self.win_first_stage(ledger)

        ledger.assign(20, 602_000)

        self.assertEqual(ledger.active_second_order_ids, frozenset({20}))
        self.assertEqual(ledger.status()["active_second_stage"], 1)

    def test_rollback_assignment_restores_current_active_credit_to_pending(self):
        ledger = self.ledger()
        pending = self.win_first_stage(ledger)
        _, consumed = ledger.assign(20, 602_000)

        rolled_back = ledger.rollback_assignment(20)

        self.assertIs(rolled_back, consumed)
        self.assertIs(rolled_back, pending)
        self.assertEqual(rolled_back.status, "PENDING")
        self.assertIsNone(rolled_back.consumed_order_id)
        self.assertIsNone(rolled_back.consumed_at)
        self.assertEqual(ledger.active_second_order_ids, frozenset())

    def test_rollback_assignment_ignores_historical_and_unknown_order_ids(self):
        historical = StakeProgressionCredit(
            source_order_id=1,
            created_at=601_000,
            status="CONSUMED",
            consumed_order_id=10,
            consumed_at=602_000,
        )
        current = StakeProgressionCredit(
            source_order_id=2,
            created_at=603_000,
            status="CONSUMED",
            consumed_order_id=20,
            consumed_at=604_000,
        )
        ledger = self.ledger(
            max_active=2,
            credits=[historical, current],
            active_second_order_ids=[20],
        )

        historical_result = ledger.rollback_assignment(10)
        unknown_result = ledger.rollback_assignment(999)

        self.assertIsNone(historical_result)
        self.assertIsNone(unknown_result)
        self.assertEqual(historical.status, "CONSUMED")
        self.assertEqual(current.status, "CONSUMED")
        self.assertEqual(ledger.active_second_order_ids, frozenset({20}))

    def test_assign_rejects_negative_consumed_time_without_mutating_credit(self):
        ledger = self.ledger()
        pending = self.win_first_stage(ledger)

        with self.assertRaisesRegex(ValueError, "opened_at"):
            ledger.assign(20, -1)

        self.assertEqual(pending.status, "PENDING")
        self.assertIsNone(pending.consumed_order_id)
        self.assertIsNone(pending.consumed_at)
        self.assertEqual(ledger.active_second_order_ids, frozenset())

    def test_positive_active_count_without_real_ids_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "active_second_order_ids"):
            self.ledger(max_active=2, active_second_orders=1)

        ledger = self.ledger(max_active=2)
        with self.assertRaisesRegex(ValueError, "active_second_order_ids"):
            ledger.reconcile(active_second_orders=1)

        empty = self.ledger(max_active=2, active_second_orders=0)
        self.assertEqual(empty.active_second_order_ids, frozenset())
        self.assertNotIn("legacy_active_placeholders", empty.status())

    def test_runtime_order_ids_must_be_positive(self):
        active = StakeProgressionCredit(
            source_order_id=1,
            created_at=601_000,
            status="CONSUMED",
            consumed_order_id=20,
            consumed_at=602_000,
        )
        ledger = self.ledger(
            max_active=2,
            credits=[active],
            active_second_order_ids=[20],
        )

        with self.assertRaisesRegex(ValueError, "order_id"):
            ledger.settle(-1, 602_000, 2, "LOSS", 1_202_000)
        self.assertEqual(ledger.status()["active_second_stage"], 1)

        pending_ledger = self.ledger()
        self.win_first_stage(pending_ledger)
        with self.assertRaisesRegex(ValueError, "order_id"):
            pending_ledger.assign(0, 602_000)
        self.assertEqual(pending_ledger.status()["pending_credits"], 1)

    def test_active_count_and_ids_must_agree_when_both_are_restored(self):
        with self.assertRaisesRegex(ValueError, "active_second_orders"):
            self.ledger(
                max_active=3,
                active_second_orders=1,
                active_second_order_ids=[20, 21],
            )

        ledger = self.ledger(max_active=3)
        with self.assertRaisesRegex(ValueError, "active_second_orders"):
            ledger.reconcile(
                active_second_orders=1,
                active_second_order_ids=[20, 21],
            )

    def test_matching_active_count_and_ids_are_accepted(self):
        active = StakeProgressionCredit(
            source_order_id=1,
            created_at=601_000,
            status="CONSUMED",
            consumed_order_id=20,
            consumed_at=602_000,
        )

        ledger = self.ledger(
            max_active=2,
            credits=[active],
            active_second_orders=1,
            active_second_order_ids=[20],
        )

        self.assertEqual(ledger.active_second_order_ids, frozenset({20}))

    def test_active_ids_must_reference_current_consumed_credits(self):
        pending = StakeProgressionCredit(source_order_id=1, created_at=601_000)

        with self.assertRaisesRegex(ValueError, "CONSUMED credit"):
            self.ledger(
                max_active=2,
                credits=[pending],
                active_second_order_ids=[20],
            )

    def test_restore_rejects_duplicate_source_for_same_version(self):
        credits = [
            StakeProgressionCredit(source_order_id=1, created_at=601_000),
            StakeProgressionCredit(
                source_order_id=1,
                created_at=602_000,
                credit_id="duplicate-source",
            ),
        ]

        with self.assertRaisesRegex(ValueError, "source_order_id"):
            self.ledger(max_active=2, credits=credits)

    def test_restore_rejects_duplicate_consumed_order_id(self):
        credits = [
            StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
                status="CONSUMED",
                consumed_order_id=20,
                consumed_at=602_000,
            ),
            StakeProgressionCredit(
                source_order_id=2,
                created_at=603_000,
                status="CONSUMED",
                consumed_order_id=20,
                consumed_at=604_000,
            ),
        ]

        with self.assertRaisesRegex(ValueError, "consumed_order_id"):
            self.ledger(max_active=2, credits=credits, active_second_order_ids=[20])

    def test_restore_rejects_credit_from_another_version(self):
        old = StakeProgressionCredit(
            source_order_id=1,
            created_at=601_000,
            version="TWO_STAGE_V0",
        )

        with self.assertRaisesRegex(ValueError, "version"):
            self.ledger(credits=[old])

    def test_credit_rejects_invalid_status_field_combinations(self):
        invalid_arguments = [
            {"source_order_id": 0, "created_at": 0},
            {"source_order_id": 1, "created_at": -1},
            {
                "source_order_id": 1,
                "created_at": 0,
                "status": "PENDING",
                "consumed_order_id": 2,
            },
            {
                "source_order_id": 1,
                "created_at": 0,
                "status": "CANCELLED",
                "consumed_at": 1,
            },
            {"source_order_id": 1, "created_at": 0, "status": "CONSUMED"},
            {
                "source_order_id": 1,
                "created_at": 0,
                "status": "CONSUMED",
                "consumed_order_id": 2,
            },
            {
                "source_order_id": 1,
                "created_at": 0,
                "status": "CONSUMED",
                "consumed_order_id": 0,
                "consumed_at": 0,
            },
            {
                "source_order_id": 1,
                "created_at": 0,
                "status": "CONSUMED",
                "consumed_order_id": 2,
                "consumed_at": -1,
            },
            {
                "source_order_id": 1,
                "created_at": 2,
                "status": "CONSUMED",
                "consumed_order_id": 2,
                "consumed_at": 1,
            },
        ]

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    StakeProgressionCredit(**arguments)

        valid = StakeProgressionCredit(
            source_order_id=1,
            created_at=0,
            status="CONSUMED",
            consumed_order_id=2,
            consumed_at=0,
        )
        self.assertEqual(valid.consumed_at, 0)

    def test_settle_rejects_time_before_open_and_accepts_zero_boundary(self):
        ledger = self.ledger(activated_at=0)

        with self.assertRaisesRegex(ValueError, "settled_at"):
            ledger.settle(1, 1, 1, "WIN", 0)

        credit = ledger.settle(2, 0, 1, "WIN", 0)
        terms, consumed = ledger.assign(3, 0)

        self.assertEqual(credit.created_at, 0)
        self.assertIs(consumed, credit)
        self.assertEqual(consumed.consumed_at, 0)
        self.assertEqual((terms.stake, terms.win_return), (18.0, 32.4))

    def test_runtime_disable_cancels_pending_and_reenable_does_not_restore_it(self):
        ledger = self.ledger()
        pending = self.win_first_stage(ledger)

        cancelled = ledger.set_enabled(False)
        ledger.set_enabled(True)
        terms, consumed = ledger.assign(2, 602_000)

        self.assertEqual(cancelled, [pending])
        self.assertEqual(pending.status, "CANCELLED")
        self.assertTrue(ledger.status()["enabled"])
        self.assertIsNone(consumed)
        self.assertEqual((terms.stake, terms.win_return, terms.step), (10.0, 18.0, 1))

    def test_disabled_ledger_preserves_terms_for_an_already_consumed_order(self):
        ledger = self.ledger()
        self.win_first_stage(ledger)
        original_terms, original_credit = ledger.assign(2, 602_000)
        ledger.set_enabled(False)

        retried_terms, retried_credit = ledger.assign(2, 602_000)

        self.assertEqual(retried_terms, original_terms)
        self.assertIs(retried_credit, original_credit)
        self.assertEqual((retried_terms.stake, retried_terms.step), (18.0, 2))

    def test_status_contains_api_fields_and_gross_returns(self):
        ledger = self.ledger(max_active=2)
        self.win_first_stage(ledger)

        status = ledger.status()

        self.assertEqual(status["enabled"], True)
        self.assertEqual(status["version"], TWO_STAGE_VERSION)
        self.assertEqual(status["pending_credits"], 1)
        self.assertEqual(status["active_second_stage"], 0)
        self.assertEqual(status["max_active_second_stage"], 2)
        self.assertEqual(status["base_stake"], 10.0)
        self.assertEqual(status["base_win_return"], 18.0)
        self.assertEqual(status["second_stake"], 18.0)
        self.assertEqual(status["second_win_return"], 32.4)
        self.assertEqual(status["next_stake"], 18.0)
        self.assertEqual(status["next_win_return"], 32.4)

    def test_credit_and_terms_are_serializable(self):
        ledger = self.ledger()
        credit = self.win_first_stage(ledger)
        terms, _ = ledger.assign(2, 602_000)

        self.assertEqual(credit.to_dict()["credit_id"], f"{TWO_STAGE_VERSION}:1")
        self.assertEqual(credit.to_dict()["version"], TWO_STAGE_VERSION)
        self.assertEqual(terms.to_dict()["win_return"], 32.4)

    def test_compatibility_parameter_names_use_gross_return_semantics(self):
        ledger = TwoStageStakeProgression(
            enabled=True,
            base_stake=10,
            second_stake=18,
            payout_ratio=0.8,
            max_active_second_stage=1,
            activation_time=1_000,
        )
        self.win_first_stage(ledger)

        terms, _ = ledger.assign(2, 602_000)

        self.assertEqual((terms.stake, terms.win_return), (18.0, 32.4))

    def test_max_active_is_clamped_to_open_order_limit(self):
        ledger = self.ledger(max_active=99)

        self.assertEqual(ledger.status()["max_active_second_stage"], 5)

    def test_invalid_constructor_parameters_raise_value_error(self):
        invalid_arguments = [
            {"base_stake": 0},
            {"base_win_return": 0},
            {"max_active": 0},
            {"max_open_orders": 0},
        ]
        for overrides in invalid_arguments:
            arguments = {
                "enabled": True,
                "base_stake": 10,
                "base_win_return": 18,
                "max_active": 1,
                "max_open_orders": 5,
                "activated_at": 1_000,
            }
            arguments.update(overrides)
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    TwoStageStakeProgression(**arguments)

        with self.assertRaises(ValueError):
            TwoStageStakeProgression(
                enabled=True,
                base_stake=10,
                second_stake=18,
                payout_ratio=0,
                max_active_second_stage=1,
                activation_time=1_000,
            )

    def test_non_finite_amounts_and_payout_ratio_are_rejected(self):
        for field in (
            "base_stake",
            "base_win_return",
            "second_stake",
            "payout_ratio",
        ):
            for value in (float("nan"), float("inf"), float("-inf")):
                arguments = {
                    "enabled": True,
                    "base_stake": 10,
                    "base_win_return": 18,
                    "max_active": 1,
                    "max_open_orders": 5,
                    "activated_at": 1_000,
                }
                if field == "payout_ratio":
                    arguments.pop("base_win_return")
                arguments[field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        TwoStageStakeProgression(**arguments)

    def test_non_finite_derived_financial_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "base_win_return"):
            TwoStageStakeProgression(
                enabled=True,
                base_stake=sys.float_info.max,
                payout_ratio=0.8,
            )

        with self.assertRaisesRegex(ValueError, "payout_ratio"):
            TwoStageStakeProgression(
                enabled=True,
                base_stake=sys.float_info.min,
                base_win_return=sys.float_info.max,
            )

        with self.assertRaisesRegex(ValueError, "second_win_return"):
            TwoStageStakeProgression(
                enabled=True,
                base_stake=10,
                base_win_return=18,
                second_stake=sys.float_info.max,
            )

    def test_unknown_result_and_step_raise_value_error(self):
        ledger = self.ledger()

        with self.assertRaisesRegex(ValueError, "result"):
            ledger.settle(1, 1_000, 1, "DRAW", 601_000)
        with self.assertRaisesRegex(ValueError, "step"):
            ledger.settle(1, 1_000, 3, "WIN", 601_000)
        with self.assertRaisesRegex(ValueError, "step"):
            ledger.settle(1, 1_000, "third", "WIN", 601_000)


if __name__ == "__main__":
    unittest.main()
