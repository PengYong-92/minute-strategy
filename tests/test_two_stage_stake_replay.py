import json
import tempfile
import unittest
from pathlib import Path

from app.stake_progression import TWO_STAGE_VERSION
from app.models import Signal
from app.simulator import AccountSimulator
from scripts.replay_two_stage_stakes import _reprice_legacy_three_stage, main, reprice_trades


def trade(
    order_id,
    opened_at,
    settled_at,
    result,
    *,
    direction="LONG",
    profile_key="LONG|WD-08",
):
    return {
        "id": order_id,
        "opened_at": opened_at,
        "settled_at": settled_at,
        "expires_at": settled_at,
        "direction": direction,
        "result": result,
        "profile_key": profile_key,
    }


class TwoStageStakeReplayTest(unittest.TestCase):
    def test_reprice_preserves_overlapping_trades_and_limits_second_stage(self):
        rows = [
            trade(1, 0, 600_000, "WIN"),
            trade(2, 120_000, 720_000, "WIN", direction="SHORT", profile_key="SHORT|WD-23"),
            trade(3, 600_000, 1_200_000, "LOSS", profile_key="LONG|WD-12"),
            trade(4, 840_000, 1_440_000, "WIN", direction="SHORT", profile_key="SHORT|WD-18"),
        ]

        result = reprice_trades(rows, base_stake=10.0, base_win_return=18.0, max_active=1)

        repriced = result["trade_rows"]
        self.assertEqual([item["id"] for item in repriced], [1, 2, 3, 4])
        self.assertEqual([item["stake"] for item in repriced], [10.0, 10.0, 18.0, 18.0])
        self.assertEqual([item["result"] for item in repriced], ["WIN", "WIN", "LOSS", "WIN"])
        self.assertEqual([item["stake_progression_step"] for item in repriced], [1, 1, 2, 2])
        self.assertEqual(repriced[2]["stake_progression_source_order_id"], 1)
        self.assertEqual(repriced[3]["stake_progression_source_order_id"], 2)
        self.assertTrue(all(item["stake_progression_version"] == TWO_STAGE_VERSION for item in repriced))
        self.assertEqual(result["summary"]["orders"], 4)
        self.assertEqual(result["summary"]["second_stage_orders"], 2)
        self.assertEqual(result["summary"]["pnl"], 12.4)

    def test_second_stage_win_or_loss_never_creates_a_third_stage(self):
        for second_result in ("WIN", "LOSS"):
            with self.subTest(second_result=second_result):
                rows = [
                    trade(1, 0, 100, "WIN"),
                    trade(2, 100, 200, second_result),
                    trade(3, 200, 300, "WIN"),
                ]

                result = reprice_trades(rows, max_active=1)

                repriced = result["trade_rows"]
                self.assertEqual([item["stake"] for item in repriced], [10.0, 18.0, 10.0])
                self.assertEqual([item["stake_progression_step"] for item in repriced], [1, 2, 1])
                self.assertEqual(
                    [item["stake_progression_source_order_id"] for item in repriced],
                    [None, 1, None],
                )

    def test_same_millisecond_settlement_precedes_open(self):
        rows = [trade(1, 0, 100, "WIN"), trade(2, 100, 200, "LOSS")]

        result = reprice_trades(rows, max_active=1)

        self.assertEqual(result["trade_rows"][1]["stake"], 18.0)
        self.assertEqual(result["trade_rows"][1]["stake_progression_source_order_id"], 1)

    def test_same_millisecond_settlement_order_matches_live_simulator(self):
        rows = [
            trade(1, 0, 180_000, "WIN"),
            trade(2, 0, 60_000, "WIN"),
            trade(3, 60_000, 180_000, "WIN"),
            trade(4, 180_000, 240_000, "LOSS"),
        ]
        replay = reprice_trades(rows, max_active=1)

        simulator = AccountSimulator(
            enable_stake_progression=True,
            stake_progression_max_active=1,
        )

        def live_signal(timeframe):
            return Signal(
                direction="LONG", timeframe_minutes=timeframe, level="A", reason="synthetic",
                price=100.0, open_time=0, score=80, threshold=70, session_allowed=True,
            )

        simulator.open_order(live_signal(3), 100.0, 0)
        simulator.open_order(live_signal(1), 100.0, 0)
        simulator.settle_expired_orders(60_000, 101.0)
        simulator.open_order(live_signal(2), 101.0, 60_000)
        simulator.settle_expired_orders(180_000, 102.0)
        simulator.open_order(live_signal(1), 102.0, 180_000)

        self.assertEqual(
            [item["stake"] for item in replay["trade_rows"]],
            [order.stake for order in simulator.orders],
        )
        self.assertEqual([item["stake"] for item in replay["trade_rows"]], [10.0, 10.0, 18.0, 10.0])

    def test_max_active_one_through_five_is_enforced(self):
        rows = [trade(index, 0, 100, "WIN") for index in range(1, 6)]
        rows.extend(trade(index, 100, 200, "LOSS") for index in range(6, 11))

        for max_active in range(1, 6):
            with self.subTest(max_active=max_active):
                result = reprice_trades(rows, max_active=max_active)
                second = [
                    item
                    for item in result["trade_rows"]
                    if item["stake_progression_step"] == 2
                ]
                self.assertEqual(len(second), max_active)
                self.assertEqual([item["id"] for item in second], list(range(6, 6 + max_active)))
                self.assertTrue(all(item["stake"] == 18.0 for item in second))
                self.assertTrue(all(item["stake_progression_step"] <= 2 for item in result["trade_rows"]))

    def test_summary_and_direction_and_profile_breakdowns_include_exposure(self):
        rows = [
            trade(1, 0, 100, "WIN"),
            trade(2, 10, 110, "LOSS", direction="SHORT", profile_key="SHORT|WD-23"),
            trade(3, 100, 200, "WIN"),
        ]

        result = reprice_trades(rows, max_active=1)

        summary = result["summary"]
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["pnl"], 12.4)
        self.assertEqual(summary["total_staked"], 38.0)
        self.assertEqual(summary["max_drawdown"], 10.0)
        self.assertEqual(summary["peak_open_stake"], 28.0)
        self.assertEqual(summary["second_stage_orders"], 1)
        self.assertEqual(summary["second_stage_wins"], 1)
        self.assertEqual(summary["second_stage_win_rate"], 1.0)
        by_direction = {item["direction"]: item for item in result["by_direction"]}
        by_profile = {item["profile_key"]: item for item in result["by_profile"]}
        self.assertEqual(by_direction["LONG"]["orders"], 2)
        self.assertEqual(by_direction["SHORT"]["losses"], 1)
        by_session = {item["threshold_segment"]: item for item in result["by_session"]}
        self.assertEqual(by_session["UNKNOWN"]["orders"], 3)
        self.assertEqual(by_profile["LONG|WD-08"]["second_stage_orders"], 1)
        self.assertEqual(by_profile["SHORT|WD-23"]["pnl"], -10.0)

    def test_reprice_rejects_invalid_trade_rows(self):
        cases = {
            "duplicate id": [trade(1, 0, 100, "WIN"), trade(1, 100, 200, "LOSS")],
            "open status": [{**trade(1, 0, 100, "WIN"), "status": "OPEN"}],
            "open result": [trade(1, 0, 100, "OPEN")],
            "invalid result": [trade(1, 0, 100, "DRAW")],
            "settled before opened": [trade(1, 100, 99, "WIN")],
        }

        for label, rows in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    reprice_trades(rows)

    def test_empty_input_returns_complete_empty_report(self):
        result = reprice_trades([])

        self.assertEqual(result["trade_rows"], [])
        self.assertEqual(result["by_direction"], [])
        self.assertEqual(result["by_profile"], [])
        self.assertEqual(result["by_session"], [])
        self.assertEqual(
            set(result["summary"]),
            {
                "orders",
                "wins",
                "losses",
                "win_rate",
                "pnl",
                "ev",
                "total_staked",
                "roi",
                "max_drawdown",
                "peak_open_stake",
                "second_stage_orders",
                "second_stage_wins",
                "second_stage_win_rate",
            },
        )
        self.assertTrue(all(value == 0 or value == 0.0 for value in result["summary"].values()))

    def test_cli_reads_common_trade_fields_and_emits_all_comparisons(self):
        rows = [
            {key: value for key, value in trade(1, 0, 100, "WIN").items() if key != "id"},
            {key: value for key, value in trade(2, 100, 200, "LOSS").items() if key != "id"},
        ]
        expected_policies = {
            "fixed_10u",
            "legacy_three_stage",
            *(f"two_stage_max_active_{value}" for value in range(1, 6)),
        }

        for field in ("trade_rows", "traded", "orders"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.json"
                output_path = Path(temp_dir) / "output.json"
                input_path.write_text(json.dumps({field: rows}), encoding="utf-8")

                self.assertEqual(main(["--input", str(input_path), "--output", str(output_path)]), 0)

                report = json.loads(output_path.read_text(encoding="utf-8"))
                self.assertEqual(set(report["policies"]), expected_policies)
                signatures = [
                    [(item["id"], item["direction"], item["result"]) for item in policy["trade_rows"]]
                    for policy in report["policies"].values()
                ]
                self.assertTrue(all(signature == signatures[0] for signature in signatures[1:]))
                self.assertEqual(report["input"]["trade_field"], field)

    def test_legacy_three_stage_comparison_keeps_historical_amount_sequence(self):
        rows = [trade(index, (index - 1) * 100, index * 100, "WIN") for index in range(1, 5)]

        result = _reprice_legacy_three_stage(rows, base_stake=10.0, base_win_return=18.0)

        self.assertEqual([item["stake"] for item in result["trade_rows"]], [10.0, 18.0, 32.4, 10.0])
        self.assertEqual([item["stake_progression_step"] for item in result["trade_rows"]], [1, 2, 3, 1])


if __name__ == "__main__":
    unittest.main()
