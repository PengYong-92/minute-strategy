import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models import Kline, Signal, SimulatedOrder
from app.simulator import AccountSimulator
from app.stake_progression import TWO_STAGE_VERSION, StakeProgressionCredit


def signal(
    direction="LONG",
    timeframe_minutes=10,
    threshold_segment="WD-12",
    daily_profile_version="",
):
    return Signal(
        direction=direction,
        timeframe_minutes=timeframe_minutes,
        level="A",
        reason="test",
        price=100.0,
        open_time=0,
        threshold_segment=threshold_segment,
        session_allowed=True,
        session_sample_size=37,
        session_win_rate=0.6757,
        session_ev=2.1622,
        session_edge_min=10.0,
        daily_profile_version=daily_profile_version,
    )


class SimulatorTest(unittest.TestCase):
    def test_order_slot_is_independent_from_stake_progression_step(self):
        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_max_active=1,
            max_open_orders=2,
        )
        first = simulator.open_order(signal(), entry_price=100.0, opened_at=0)
        simulator.settle_expired_orders(first.expires_at, 101.0)

        progressed = simulator.open_order(signal(), entry_price=101.0, opened_at=601_000)
        concurrent = simulator.open_order(signal(), entry_price=101.0, opened_at=721_000)

        self.assertEqual(progressed.order_slot, "FIRST")
        self.assertEqual(progressed.stake_progression_step, 2)
        self.assertEqual(concurrent.order_slot, "SECOND")
        self.assertEqual(concurrent.stake_progression_step, 1)

    def test_simulated_order_progression_metadata_defaults_are_backward_compatible(self):
        self.assertEqual(
            [field.name for field in fields(SimulatedOrder)][-2:],
            ["profile_degradation_probe", "profile_degradation_triggered_at"],
        )
        order = SimulatedOrder(
            id=1,
            direction="LONG",
            timeframe_minutes=1,
            level="A",
            reason="restored",
            entry_price=100.0,
            opened_at=0,
            expires_at=60_000,
        )

        self.assertIsNone(order.stake_progression_source_order_id)
        self.assertEqual(order.stake_progression_version, "")
        self.assertFalse(order.profile_degradation_probe)
        self.assertEqual(order.profile_degradation_triggered_at, 0)
        self.assertIn("stake_progression_source_order_id", order.to_dict())

    def test_signal_probe_metadata_defaults_preserve_legacy_positional_arguments(self):
        self.assertEqual(
            [field.name for field in fields(Signal)][-2:],
            ["profile_degradation_probe", "profile_degradation_triggered_at"],
        )
        legacy = Signal("LONG", 1, "A", "legacy", 100.0, 0, 2.5)

        self.assertEqual(legacy.volume_ratio, 2.5)
        self.assertFalse(legacy.profile_degradation_probe)
        self.assertEqual(legacy.profile_degradation_triggered_at, 0)

    def test_simulated_order_keeps_legacy_positional_argument_order(self):
        order = SimulatedOrder(
            1,
            "LONG",
            1,
            "A",
            "legacy",
            100.0,
            0,
            60_000,
            "GLOBAL",
            0.0,
            0.0,
            False,
            0,
            0.0,
            0.0,
            0.0,
            "UNKNOWN",
            "unknown",
            "unknown",
            "",
            False,
            "",
            10.0,
            18.0,
            1,
            "SETTLED",
            "WIN",
            101.0,
            60_000,
            8.0,
        )

        self.assertEqual(order.status, "SETTLED")
        self.assertEqual(order.result, "WIN")
        self.assertIsNone(order.stake_progression_source_order_id)
        self.assertEqual(order.stake_progression_version, "")

    def test_daily_profile_selection_is_copied_from_actionable_signal_to_order(self):
        selected = Signal(
            direction="SHORT",
            timeframe_minutes=10,
            level="B",
            reason="每日画像启用",
            price=100.0,
            open_time=0,
            score=-84.0,
            threshold=80.0,
            calculated_threshold=92.0,
            strategy_family="short_observe",
            strategy_tag="generic_short_observe",
            threshold_segment="WD-02",
            daily_profile_selected=True,
            daily_profile_version="DPS-20260730-0800",
        )

        order = AccountSimulator().open_order(selected, entry_price=100.0, opened_at=0)

        self.assertTrue(selected.actionable)
        self.assertEqual(selected.score, -84.0)
        self.assertEqual(selected.threshold, 80.0)
        self.assertTrue(order.daily_profile_selected)
        self.assertEqual(order.daily_profile_version, "DPS-20260730-0800")
        self.assertEqual(order.threshold, 80.0)
        self.assertEqual(order.calculated_threshold, 92.0)

    def test_wave_metadata_is_copied_to_order(self):
        selected = Signal(
            direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="波段允许",
            price=100.0,
            open_time=0,
            score=84.0,
            threshold=79.0,
            wave_state="UP_LEG",
            wave_raw_state="UP_LEG",
            wave_window=8,
            wave_efficiency=0.8,
            wave_direction_ratio=0.86,
            wave_atr_strength=2.1,
            wave_confirmations=2,
            wave_confirmed_at=120_000,
            wave_batch_id="120000|UP_LEG|LONG|WD-00|STATIC",
            wave_guard_mode="NORMAL",
            wave_guard_status="WAVE_BATCH_NORMAL",
            wave_guard_reason="当前波段批次允许开单",
        )

        order = AccountSimulator().open_order(selected, entry_price=100.0, opened_at=180_000)

        self.assertEqual(order.wave_state, "UP_LEG")
        self.assertEqual(order.wave_window, 8)
        self.assertEqual(order.wave_batch_id, selected.wave_batch_id)
        self.assertEqual(order.wave_guard_mode, "NORMAL")
        self.assertEqual(order.wave_guard_status, "WAVE_BATCH_NORMAL")
        self.assertEqual(order.wave_guard_reason, "当前波段批次允许开单")

    def test_recovery_order_does_not_consume_pending_credit(self):
        simulator = AccountSimulator(enable_stake_progression=True, stake_progression_activated_at=0)
        first = simulator.open_order(signal(timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)
        self.assertEqual(simulator.stake_progression.credits[0].status, "PENDING")
        recovery_signal = Signal(
            "LONG",
            1,
            "A",
            "recovery",
            101.0,
            60_000,
            wave_guard_mode="RECOVERY",
        )

        recovery, consumed = simulator.open_order_with_credit(
            recovery_signal,
            101.0,
            60_000,
            allow_progression=False,
        )

        self.assertEqual(first.stake, 10.0)
        self.assertEqual(recovery.stake, 10.0)
        self.assertEqual(recovery.stake_progression_step, 1)
        self.assertIsNone(consumed)
        self.assertEqual(simulator.stake_progression.credits[0].status, "PENDING")

    def test_recovery_win_does_not_generate_pending_credit(self):
        simulator = AccountSimulator(enable_stake_progression=True, stake_progression_activated_at=0)
        recovery_signal = Signal(
            "LONG", 1, "A", "recovery", 100.0, 0, wave_guard_mode="RECOVERY"
        )
        simulator.open_order_with_credit(
            recovery_signal,
            100.0,
            0,
            allow_progression=False,
        )

        events = simulator.settle_expired_order_events(60_000, 101.0)

        self.assertEqual(events[0].order.result, "WIN")
        self.assertIsNone(events[0].progression_credit)
        self.assertEqual(simulator.stake_progression.credits, [])

    def test_profile_degradation_probe_uses_base_stake_without_consuming_pending_credit(self):
        self.assertIn("profile_degradation_probe", {field.name for field in fields(Signal)})
        simulator = AccountSimulator(
            stake=12.5,
            win_return=22.5,
            enable_stake_progression=True,
            stake_progression_activated_at=0,
        )
        simulator.open_order(signal(timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)
        pending = simulator.stake_progression.credits[0]
        self.assertEqual(simulator.stake_progression.second_stake, simulator.win_return)
        self.assertEqual(pending.status, "PENDING")
        probe_signal = Signal(
            "LONG",
            1,
            "A",
            "profile degradation probe",
            101.0,
            60_000,
            profile_degradation_probe=True,
            profile_degradation_triggered_at=55_000,
        )

        probe, consumed = simulator.open_order_with_credit(
            probe_signal,
            101.0,
            60_000,
            allow_progression=False,
        )

        self.assertTrue(probe.profile_degradation_probe)
        self.assertEqual(probe.profile_degradation_triggered_at, 55_000)
        self.assertEqual(probe.stake, simulator.stake)
        self.assertEqual(probe.win_return, simulator.win_return)
        self.assertEqual(probe.stake_progression_step, 1)
        self.assertIsNone(consumed)
        self.assertEqual(pending.status, "PENDING")

    def test_profile_degradation_probe_win_generates_pending_credit(self):
        self.assertIn("profile_degradation_probe", {field.name for field in fields(Signal)})
        simulator = AccountSimulator(enable_stake_progression=True, stake_progression_activated_at=0)
        probe_signal = Signal(
            "LONG",
            1,
            "A",
            "profile degradation probe",
            100.0,
            0,
            wave_guard_mode="NORMAL",
            profile_degradation_probe=True,
            profile_degradation_triggered_at=1,
        )
        probe, consumed = simulator.open_order_with_credit(
            probe_signal,
            100.0,
            0,
            allow_progression=False,
        )

        events = simulator.settle_expired_order_events(60_000, 101.0)

        self.assertIsNone(consumed)
        self.assertEqual(probe.wave_guard_mode, "NORMAL")
        self.assertEqual(events[0].order.result, "WIN")
        self.assertEqual(events[0].progression_credit.status, "PENDING")
        self.assertEqual(events[0].progression_credit.source_order_id, probe.id)
        self.assertEqual(simulator.stake_progression.credits[0].status, "PENDING")

    def test_profile_degradation_probe_recovery_win_generates_pending_credit(self):
        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_activated_at=0,
        )
        probe_signal = Signal(
            "LONG",
            1,
            "A",
            "profile degradation recovery probe",
            100.0,
            0,
            wave_guard_mode="RECOVERY",
            profile_degradation_probe=True,
            profile_degradation_triggered_at=1,
        )
        probe, consumed = simulator.open_order_with_credit(
            probe_signal,
            100.0,
            0,
            allow_progression=False,
        )

        events = simulator.settle_expired_order_events(60_000, 101.0)

        self.assertIsNone(consumed)
        self.assertEqual(probe.stake_progression_step, 1)
        self.assertEqual(events[0].order.result, "WIN")
        self.assertIsNotNone(events[0].progression_credit)
        self.assertEqual(events[0].progression_credit.status, "PENDING")
        self.assertEqual(events[0].progression_credit.source_order_id, probe.id)
        self.assertEqual(simulator.stake_progression.credits[0].status, "PENDING")

    def test_long_order_wins_when_expiry_price_is_higher(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=100.1)

        self.assertEqual(order.status, "SETTLED")
        self.assertEqual(order.result, "WIN")
        self.assertEqual(order.exit_price, 100.1)
        self.assertEqual(order.pnl, 8.0)
        self.assertEqual(simulator.balance, 8.0)
        self.assertEqual(order.threshold_segment, "WD-12")
        self.assertEqual(order.session_win_rate, 0.6757)

    def test_long_order_loses_when_expiry_price_is_not_higher(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=100.0)

        self.assertEqual(order.result, "LOSS")
        self.assertEqual(order.pnl, -10.0)
        self.assertEqual(simulator.balance, -10.0)

    def test_short_order_wins_when_expiry_price_is_lower(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("SHORT"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=99.9)

        self.assertEqual(order.result, "WIN")
        self.assertEqual(order.pnl, 8.0)
        self.assertEqual(simulator.balance, 8.0)

    def test_short_order_loses_when_expiry_price_is_not_lower(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("SHORT"), entry_price=100.0, opened_at=0)

        simulator.settle_expired_orders(current_time=10 * 60_000, current_price=100.0)

        self.assertEqual(order.result, "LOSS")
        self.assertEqual(order.pnl, -10.0)
        self.assertEqual(simulator.balance, -10.0)

    def test_base_win_creates_credit_consumed_by_18u_second_stage(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), entry_price=100.0, opened_at=0)
        events = simulator.settle_expired_order_events(60_000, 101.0)
        pending = events[0].progression_credit

        self.assertEqual(events[0].order, first)
        self.assertEqual(pending.status, "PENDING")

        second, consumed = simulator.open_order_with_credit(
            signal("LONG", timeframe_minutes=1),
            entry_price=101.0,
            opened_at=60_000,
        )

        self.assertEqual(consumed.status, "CONSUMED")
        self.assertIsNot(consumed, pending)
        self.assertEqual(consumed.credit_id, pending.credit_id)
        self.assertEqual(consumed.source_order_id, pending.source_order_id)
        self.assertEqual(pending.status, "PENDING")
        self.assertEqual((second.stake, second.win_return), (18.0, 32.4))
        self.assertEqual(second.stake_progression_step, 2)
        self.assertEqual(second.stake_progression_source_order_id, first.id)
        self.assertEqual(second.stake_progression_version, TWO_STAGE_VERSION)

    def test_rollback_open_order_removes_latest_base_order_and_reuses_id(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        opened = simulator.open_order(signal("LONG"), 100.0, 0)

        rolled_back = simulator.rollback_open_order(opened.id)
        replacement = simulator.open_order(signal("SHORT"), 101.0, 1_000)

        self.assertIs(rolled_back, opened)
        self.assertEqual(simulator.orders, [replacement])
        self.assertEqual(replacement.id, opened.id)
        self.assertEqual(replacement.stake_progression_step, 1)

    def test_rollback_open_order_restores_credit_consumed_by_latest_18u_order(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_order_events(60_000, 101.0)
        second, consumed = simulator.open_order_with_credit(signal("SHORT"), 101.0, 60_000)

        simulator.rollback_open_order(second.id)

        self.assertEqual(consumed.status, "PENDING")
        self.assertEqual(simulator.stake_progression.credits[0].status, "PENDING")
        self.assertEqual(simulator.stats()["active_second_orders"], 0)
        retried, retried_credit = simulator.open_order_with_credit(signal("SHORT"), 101.0, 61_000)
        self.assertEqual(retried.id, second.id)
        self.assertEqual(retried.stake, 18.0)
        self.assertEqual(retried_credit.source_order_id, first.id)

    def test_rollback_open_order_rejects_non_latest_and_settled_orders(self):
        simulator = AccountSimulator()
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        second = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)

        with self.assertRaises(ValueError):
            simulator.rollback_open_order(first.id)

        simulator.settle_expired_orders(60_000, 101.0)
        with self.assertRaises(ValueError):
            simulator.rollback_open_order(second.id)

    def test_second_stage_win_ends_chain_without_step_three(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)
        second = simulator.open_order(signal("LONG", timeframe_minutes=1), 101.0, 60_000)

        events = simulator.settle_expired_order_events(120_000, 102.0)
        third = simulator.open_order(signal("LONG", timeframe_minutes=1), 102.0, 120_000)

        self.assertIsNone(events[0].progression_credit)
        self.assertEqual([first.pnl, second.pnl], [8.0, 14.4])
        self.assertEqual(simulator.balance, 22.4)
        self.assertEqual((third.stake, third.win_return, third.stake_progression_step), (10.0, 18.0, 1))

    def test_second_stage_loss_ends_chain_at_10u(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)
        second = simulator.open_order(signal("LONG", timeframe_minutes=1), 101.0, 60_000)

        simulator.settle_expired_orders(120_000, 100.0)
        third = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 120_000)

        self.assertEqual([first.pnl, second.pnl], [8.0, -18.0])
        self.assertEqual(simulator.balance, -10.0)
        self.assertEqual((third.stake, third.stake_progression_step), (10.0, 1))

    def test_overlapping_base_wins_respect_one_active_slot_and_emit_cancelled_audit(self):
        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_max_active=1,
        )
        first = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        second = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)

        events = simulator.settle_expired_order_events(60_000, 101.0)
        progressed = simulator.open_order(signal("LONG", timeframe_minutes=1), 101.0, 60_000)
        concurrent = simulator.open_order(signal("SHORT", timeframe_minutes=1), 101.0, 60_000)

        self.assertEqual([event.order for event in events], [first, second])
        self.assertEqual(
            [event.progression_credit.status for event in events],
            ["PENDING", "CANCELLED"],
        )
        self.assertEqual(progressed.stake_progression_source_order_id, first.id)
        self.assertEqual((progressed.stake, concurrent.stake), (18.0, 10.0))
        self.assertEqual(simulator.stats()["active_second_orders"], 1)

    def test_two_active_slots_allow_two_simultaneous_second_stage_orders(self):
        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_max_active=2,
        )
        simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)

        first_second = simulator.open_order(signal("LONG", timeframe_minutes=1), 101.0, 60_000)
        second_second = simulator.open_order(signal("SHORT", timeframe_minutes=1), 101.0, 60_000)

        self.assertEqual([first_second.stake, second_second.stake], [18.0, 18.0])
        self.assertEqual(simulator.stats()["active_second_orders"], 2)

    def test_settlement_event_credit_remains_pending_after_later_assignment(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        event = simulator.settle_expired_order_events(60_000, 101.0)[0]

        _, consumed = simulator.open_order_with_credit(
            signal("SHORT", timeframe_minutes=1),
            101.0,
            60_000,
        )

        self.assertEqual(consumed.status, "CONSUMED")
        self.assertEqual(event.progression_credit.status, "PENDING")
        self.assertIsNot(event.progression_credit, consumed)
        self.assertEqual(event.progression_credit.credit_id, consumed.credit_id)

    def test_credit_crosses_segments_and_direction_even_with_legacy_base_only_config(self):
        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_base_only_segments=["WD-23"],
        )
        first = simulator.open_order(signal("LONG", timeframe_minutes=1, threshold_segment="WD-12"), 100.0, 0)
        simulator.settle_expired_orders(current_time=60_000, current_price=101.0)
        short = simulator.open_order(
            signal("SHORT", timeframe_minutes=1, threshold_segment="WD-23"),
            101.0,
            60_000,
        )

        self.assertEqual((first.stake, short.stake), (10.0, 18.0))
        self.assertEqual(short.stake_progression_source_order_id, first.id)

    def test_activation_boundary_uses_first_stage_open_time_inclusively(self):
        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_max_active=2,
            stake_progression_activated_at=1_000,
        )
        before = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 999)
        boundary = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 1_000)

        before_event = simulator.settle_expired_order_events(60_999, 101.0)[0]
        boundary_event = simulator.settle_expired_order_events(61_000, 101.0)[0]

        self.assertEqual(before_event.order, before)
        self.assertIsNone(before_event.progression_credit)
        self.assertEqual(boundary_event.order, boundary)
        self.assertEqual(boundary_event.progression_credit.status, "PENDING")

    def test_restored_consumed_credit_and_open_second_stage_restore_active_slot(self):
        initial = AccountSimulator(enable_stake_progression=True)
        initial.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        initial.settle_expired_orders(60_000, 101.0)
        second, consumed = initial.open_order_with_credit(
            signal("LONG", timeframe_minutes=1),
            101.0,
            60_000,
        )

        restored = AccountSimulator(
            orders=[second],
            enable_stake_progression=True,
            stake_progression_credits=[consumed],
        )

        self.assertEqual(restored.stats()["active_second_orders"], 1)
        restored.settle_expired_orders(120_000, 102.0)
        self.assertEqual(restored.stats()["active_second_orders"], 0)
        self.assertEqual(restored.open_order(signal(), 102.0, 120_000).stake, 10.0)

    def test_restore_open_second_stage_without_consumed_credit_is_rejected(self):
        second = SimulatedOrder(
            id=2,
            direction="LONG",
            timeframe_minutes=1,
            level="A",
            reason="restored",
            entry_price=101.0,
            opened_at=60_000,
            expires_at=120_000,
            stake=18.0,
            win_return=32.4,
            stake_progression_step=2,
            stake_progression_source_order_id=1,
            stake_progression_version=TWO_STAGE_VERSION,
        )

        with self.assertRaisesRegex(ValueError, "CONSUMED credit"):
            AccountSimulator(orders=[second], enable_stake_progression=True)

    def test_explicit_active_second_order_ids_are_forwarded_to_ledger(self):
        credit = StakeProgressionCredit(
            source_order_id=1,
            created_at=60_000,
            status="CONSUMED",
            consumed_order_id=2,
            consumed_at=60_000,
        )
        second = SimulatedOrder(
            id=2,
            direction="LONG",
            timeframe_minutes=1,
            level="A",
            reason="restored",
            entry_price=101.0,
            opened_at=60_000,
            expires_at=120_000,
            stake=18.0,
            win_return=32.4,
            stake_progression_step=2,
            stake_progression_source_order_id=1,
            stake_progression_version=TWO_STAGE_VERSION,
        )
        simulator = AccountSimulator(
            orders=[second],
            enable_stake_progression=True,
            stake_progression_max_active=2,
            stake_progression_credits=[credit],
            active_second_order_ids=[2],
        )

        self.assertEqual(simulator.stats()["active_second_orders"], 1)

    def test_explicit_active_ids_must_match_restored_open_second_stage_orders(self):
        credit = StakeProgressionCredit(
            source_order_id=1,
            created_at=60_000,
            status="CONSUMED",
            consumed_order_id=2,
            consumed_at=60_000,
        )
        second = SimulatedOrder(
            id=2,
            direction="LONG",
            timeframe_minutes=1,
            level="A",
            reason="restored",
            entry_price=101.0,
            opened_at=60_000,
            expires_at=120_000,
            stake=18.0,
            win_return=32.4,
            stake_progression_step=2,
            stake_progression_source_order_id=1,
            stake_progression_version=TWO_STAGE_VERSION,
        )

        with self.assertRaisesRegex(ValueError, "active_second_order_ids"):
            AccountSimulator(
                orders=[second],
                enable_stake_progression=True,
                stake_progression_credits=[credit],
                active_second_order_ids=[],
            )
        with self.assertRaisesRegex(ValueError, "active_second_order_ids"):
            AccountSimulator(
                enable_stake_progression=True,
                stake_progression_credits=[credit],
                active_second_order_ids=[2],
            )

    def test_open_order_rolls_back_consumed_credit_when_construction_fails(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)
        credit = simulator.stake_progression_credits[0]

        with patch("app.simulator.SimulatedOrder", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                simulator.open_order_with_credit(signal(), 101.0, 60_000)

        self.assertEqual(credit.status, "PENDING")
        self.assertIsNone(credit.consumed_order_id)
        self.assertIsNone(credit.consumed_at)
        self.assertEqual(simulator.stake_progression.active_second_order_ids, frozenset())
        self.assertEqual(len(simulator.orders), 1)
        order, consumed = simulator.open_order_with_credit(signal(), 101.0, 60_000)
        self.assertEqual(order.id, 2)
        self.assertIs(consumed, credit)

    def test_settlement_ledger_error_leaves_order_balance_and_active_credit_unchanged(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)
        second, consumed = simulator.open_order_with_credit(
            signal("LONG", timeframe_minutes=1),
            101.0,
            60_000,
        )
        second.stake_progression_step = 3

        with self.assertRaisesRegex(ValueError, "unknown step"):
            simulator.settle_expired_order_events(120_000, 102.0)

        self.assertEqual(second.status, "OPEN")
        self.assertIsNone(second.result)
        self.assertIsNone(second.exit_price)
        self.assertIsNone(second.settled_at)
        self.assertEqual(second.pnl, 0.0)
        self.assertEqual(simulator.balance, 8.0)
        self.assertEqual(consumed.status, "CONSUMED")
        self.assertEqual(simulator.stake_progression.active_second_order_ids, frozenset({2}))

    def test_disabled_progression_stays_at_base_terms_and_empty_version(self):
        simulator = AccountSimulator(enable_stake_progression=False)
        first, credit = simulator.open_order_with_credit(signal(), 100.0, 0)
        simulator.settle_expired_orders(first.expires_at, 101.0)
        second = simulator.open_order(signal(), 101.0, first.expires_at)

        self.assertIsNone(credit)
        self.assertEqual([first.stake, second.stake], [10.0, 10.0])
        self.assertEqual([first.stake_progression_version, second.stake_progression_version], ["", ""])

    def test_stats_reports_ledger_without_consuming_pending_credit(self):
        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_max_orders=9,
            stake_progression_max_active=2,
            stake_progression_base_only_segments=["WD-12"],
        )
        simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        event = simulator.settle_expired_order_events(60_000, 101.0)[0]

        stats = simulator.stats()

        self.assertEqual(event.progression_credit.status, "PENDING")
        self.assertEqual(stats["stake_progression_max_orders"], 2)
        self.assertEqual(stats["stake_progression_max_active"], 2)
        self.assertEqual(stats["active_second_orders"], 0)
        self.assertEqual(stats["pending_credits"], 1)
        self.assertEqual((stats["next_stake"], stats["next_win_return"]), (18.0, 32.4))
        self.assertEqual(stats["stake_progression_base_only_segments"], [])
        self.assertEqual(event.progression_credit.status, "PENDING")

    def test_stats_reports_orders_settled_today_in_shanghai(self):
        shanghai = timezone(timedelta(hours=8))

        def timestamp_ms(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
            value = datetime(2026, 8, day, hour, minute, second, tzinfo=shanghai)
            return int(value.timestamp() * 1000)

        simulator = AccountSimulator()

        def settle_at(settled_at: int, *, won: bool) -> None:
            order = simulator.open_order(
                signal("LONG", timeframe_minutes=1),
                entry_price=100.0,
                opened_at=settled_at - 60_000,
            )
            simulator.settle_expired_orders(
                order.expires_at,
                101.0 if won else 99.0,
            )

        settle_at(timestamp_ms(9, 23, 59, 59), won=True)
        settle_at(timestamp_ms(10), won=False)
        settle_at(timestamp_ms(10, 11, 30), won=True)
        settle_at(timestamp_ms(11), won=True)
        simulator.open_order(
            signal("LONG", timeframe_minutes=1),
            entry_price=100.0,
            opened_at=timestamp_ms(10, 12),
        )

        stats = simulator.stats(now_ms=timestamp_ms(10, 18))

        self.assertEqual(
            stats["today"],
            {
                "date": "2026-08-10",
                "pnl": -2.0,
                "settled_orders": 2,
                "wins": 1,
                "losses": 1,
                "win_rate": 0.5,
            },
        )

    def test_stats_reports_only_orders_settled_in_active_daily_profile_period(self):
        shanghai = timezone(timedelta(hours=8))

        def timestamp_ms(day: int, hour: int = 0, minute: int = 0) -> int:
            value = datetime(2026, 8, day, hour, minute, tzinfo=shanghai)
            return int(value.timestamp() * 1000)

        simulator = AccountSimulator()

        def settle_at(settled_at: int, *, version: str, won: bool) -> None:
            order = simulator.open_order(
                signal(
                    "LONG",
                    timeframe_minutes=1,
                    daily_profile_version=version,
                ),
                entry_price=100.0,
                opened_at=settled_at - 60_000,
            )
            simulator.settle_expired_orders(
                order.expires_at,
                101.0 if won else 99.0,
            )

        effective_from = timestamp_ms(12, 8)
        effective_until = timestamp_ms(13, 8)
        settle_at(timestamp_ms(12, 7, 59), version="DPS-20260811-0800", won=False)
        settle_at(timestamp_ms(12, 8), version="DPS-20260811-0800", won=True)
        settle_at(timestamp_ms(12, 8, 1), version="DPS-20260812-0800", won=True)
        settle_at(timestamp_ms(12, 9), version="DPS-20260812-0800", won=False)
        settle_at(timestamp_ms(13, 8), version="DPS-20260812-0800", won=True)

        stats = simulator.stats(
            now_ms=timestamp_ms(12, 10),
            profile_period={
                "version": "DPS-20260812-0800",
                "effective_from": effective_from,
                "effective_until": effective_until,
            },
        )

        self.assertEqual(
            stats["profile_period"],
            {
                "active": True,
                "version": "DPS-20260812-0800",
                "effective_from": effective_from,
                "effective_until": effective_until,
                "pnl": -2.0,
                "settled_orders": 2,
                "wins": 1,
                "losses": 1,
                "win_rate": 0.5,
                "by_direction_slot": [
                    {
                        "key": "LONG_FIRST",
                        "orders": 2,
                        "wins": 1,
                        "losses": 1,
                        "win_rate": 0.5,
                        "pnl": -2.0,
                        "ev": -1.0,
                    }
                ],
            },
        )

    def test_profile_period_stats_split_direction_and_concurrency_slot(self):
        version = "DPS-20260812-0800"
        simulator = AccountSimulator(
            orders=[
                SimulatedOrder(
                    id=1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="first",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    status="SETTLED",
                    result="WIN",
                    settled_at=601_000,
                    pnl=8.0,
                    daily_profile_version=version,
                    order_slot="FIRST",
                ),
                SimulatedOrder(
                    id=2,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="second",
                    entry_price=100.0,
                    opened_at=121_000,
                    expires_at=721_000,
                    status="SETTLED",
                    result="LOSS",
                    settled_at=721_000,
                    pnl=-10.0,
                    daily_profile_version=version,
                    order_slot="SECOND",
                ),
            ]
        )

        stats = simulator.stats(
            now_ms=800_000,
            profile_period={
                "version": version,
                "effective_from": 0,
                "effective_until": 1_000_000,
            },
        )

        groups = {item["key"]: item for item in stats["profile_period"]["by_direction_slot"]}
        self.assertEqual(groups["LONG_FIRST"]["wins"], 1)
        self.assertEqual(groups["LONG_SECOND"]["losses"], 1)

    def test_stats_reports_inactive_profile_period_when_no_profile_is_active(self):
        stats = AccountSimulator().stats(now_ms=0)

        self.assertEqual(
            stats["profile_period"],
            {
                "active": False,
                "version": "",
                "effective_from": None,
                "effective_until": None,
                "pnl": 0.0,
                "settled_orders": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "by_direction_slot": [],
            },
        )

    def test_same_market_batch_settles_each_expired_order_only_once(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        order = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)

        first = simulator.settle_expired_order_events(60_000, 101.0)
        duplicate = simulator.settle_expired_order_events(60_000, 101.0)

        self.assertEqual([event.order for event in first], [order])
        self.assertEqual(duplicate, [])
        self.assertEqual(len(simulator.stake_progression_credits), 1)

    def test_settlement_event_order_is_a_snapshot(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
        event = simulator.settle_expired_order_events(60_000, 101.0)[0]

        order.status = "OPEN"
        order.result = "LOSS"
        order.exit_price = 50.0
        order.settled_at = None
        order.pnl = -999.0

        self.assertIsNot(event.order, order)
        self.assertEqual(event.order.status, "SETTLED")
        self.assertEqual(event.order.result, "WIN")
        self.assertEqual(event.order.exit_price, 101.0)
        self.assertEqual(event.order.settled_at, 60_000)
        self.assertEqual(event.order.pnl, 8.0)

    def test_kline_settlement_uses_expiry_price_instead_of_latest_price(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=59_999)
        expiry_kline = Kline(600_000, 100.0, 100.0, 90.0, 90.0, 1.0, 659_999)
        later_kline = Kline(1_200_000, 100.0, 110.0, 100.0, 110.0, 1.0, 1_259_999)

        settled = simulator.settle_expired_orders_from_klines([expiry_kline, later_kline])

        self.assertEqual(settled, [order])
        self.assertEqual(order.result, "LOSS")
        self.assertEqual(order.exit_price, 90.0)
        self.assertEqual(order.settled_at, 659_999)

    def test_kline_settlement_event_contains_pending_progression_credit(self):
        simulator = AccountSimulator(enable_stake_progression=True)
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=59_999)
        expiry_kline = Kline(600_000, 100.0, 101.0, 100.0, 101.0, 1.0, 659_999)

        events = simulator.settle_expired_order_events_from_klines([expiry_kline])

        self.assertEqual([event.order for event in events], [order])
        self.assertEqual(events[0].progression_credit.status, "PENDING")

    def test_kline_settlement_waits_when_exact_expiry_kline_is_missing(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=59_999)
        next_minute = Kline(660_000, 100.0, 110.0, 100.0, 110.0, 1.0, 719_999)

        settled = simulator.settle_expired_orders_from_klines([next_minute])

        self.assertEqual(settled, [])
        self.assertEqual(order.status, "OPEN")

    def test_direct_settlement_does_not_use_price_after_expiry(self):
        simulator = AccountSimulator()
        order = simulator.open_order(signal("LONG"), entry_price=100.0, opened_at=59_999)

        settled = simulator.settle_expired_orders(current_time=719_999, current_price=110.0)

        self.assertEqual(settled, [])
        self.assertEqual(order.status, "OPEN")


if __name__ == "__main__":
    unittest.main()
