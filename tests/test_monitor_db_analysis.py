import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.models import Signal
from app.order_profile import (
    evaluate_profile_guard,
    profile_guard_shadow,
    risk_hint_keys_for_sample,
    sample_from_entry_snapshot,
    sample_from_signal,
    sweep_profile_guard,
    sweep_profile_guard_key_subsets,
)
from scripts.analyze_monitor_db import analyze_samples, load_order_samples


class MonitorDbAnalysisTest(unittest.TestCase):
    def test_entry_snapshot_exposes_wave_fields_for_analysis(self):
        sample = sample_from_entry_snapshot(
            {
                "symbol": "BTCUSDT",
                "order_id": 1,
                "direction": "SHORT",
                "entry_payload": {
                    "signal": {
                        "wave_state": "DOWN_LEG",
                        "wave_raw_state": "DOWN_LEG",
                        "wave_batch_id": "123|DOWN_LEG|SHORT|WD-05|DPS-1",
                        "wave_guard_mode": "RECOVERY",
                    },
                    "wave_batch_guard": {"mode": "RECOVERY"},
                },
            }
        )

        self.assertEqual(sample["wave_state"], "DOWN_LEG")
        self.assertEqual(sample["wave_batch_id"], "123|DOWN_LEG|SHORT|WD-05|DPS-1")
        self.assertEqual(sample["wave_guard_mode"], "RECOVERY")

    def test_loads_snapshots_and_reports_risk_hints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            _create_schema(db_path)
            _insert_snapshot(
                db_path,
                order_id=1,
                segment="WD-18",
                result="LOSS",
                pnl=-10.0,
                level="A",
                price_position=0.45,
                price_change_pct=-0.0015,
                rsi=46.0,
                mtf_10m_bias=0.2,
                mtf_30m_bias=0.3,
                shadow_status="WOULD_BLOCK",
                default_shadow_status="PASS",
            )
            _insert_snapshot(
                db_path,
                order_id=2,
                segment="WD-18",
                result="LOSS",
                pnl=-10.0,
                level="A",
                price_position=0.48,
                price_change_pct=-0.0017,
                rsi=47.0,
                mtf_10m_bias=0.1,
                mtf_30m_bias=0.2,
                shadow_status="PASS",
                default_shadow_status="WOULD_BLOCK",
            )

            samples = load_order_samples(db_path)
            report = analyze_samples(samples, min_group_size=1)

        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["profile_guard_shadow_status"], "WOULD_BLOCK")
        self.assertIn("HIGH_RSI_REBOUND", samples[0]["profile_guard_shadow_hit_keys"])
        self.assertEqual(samples[0]["profile_guard_selection_policy_name"], "STABILITY_BAND")
        self.assertIn("HIGH_RSI_REBOUND", samples[0]["profile_guard_selection_policy_selected_keys"])
        self.assertEqual(report["total"]["orders"], 2)
        self.assertEqual(report["total"]["losses"], 2)
        self.assertIn("recommended_walk_forward", report["profile_guard"])
        self.assertIn("walk_forward_sweep", report["profile_guard"])
        self.assertIn("replay_upgrade", report["profile_guard"])
        hint_names = {item["key"] for item in report["risk_hints"]}
        self.assertIn("LEVEL_A_REBOUND", hint_names)
        self.assertIn("WEAK_SEGMENT_WD00_WD18_WD22", hint_names)
        self.assertIn("MID_POSITION_REBOUND", hint_names)
        self.assertIn("SHALLOW_DROP_REBOUND", hint_names)
        self.assertIn("HIGH_RSI_REBOUND", hint_names)
        self.assertIn("DUAL_UP_BIAS_REBOUND", hint_names)
        self.assertEqual(report["feature_bins"]["price_position"][0]["key"], "[0.35,0.5)")
        self.assertGreater(report["profile_guard"]["static_combined"]["delta_pnl"], 0.0)
        self.assertEqual(report["profile_guard_shadow"]["coverage"], 1.0)
        self.assertEqual(report["profile_guard_shadow"]["would_block"]["orders"], 1)
        self.assertEqual(report["profile_guard_shadow"]["pass"]["orders"], 1)
        self.assertEqual(report["profile_guard_shadow"]["upgrade"]["action"], "COLLECTING")
        self.assertEqual(report["profile_guard_policy"]["by_policy"][0]["key"], "STABILITY_BAND")
        selected_keys = {item["key"] for item in report["profile_guard_policy"]["by_selected_key"]}
        self.assertIn("HIGH_RSI_REBOUND", selected_keys)
        compare = report["profile_guard_shadow_compare"]
        self.assertEqual(compare["observed"]["orders"], 2)
        self.assertEqual(compare["recommended_block_default_pass"]["orders"], 1)
        self.assertEqual(compare["recommended_pass_default_block"]["orders"], 1)
        self.assertEqual(compare["upgrade"]["action"], "COLLECTING")

    def test_profile_guard_compare_recommends_promoting_stable_candidate(self):
        samples = []
        for order_id in range(1, 11):
            sample = _sample(
                order_id=order_id,
                segment="WD-18",
                result="LOSS",
                pnl=-10.0,
                level="A",
                rsi=46.0,
            )
            sample.update(
                {
                    "profile_guard_shadow_status": "WOULD_BLOCK",
                    "profile_guard_default_shadow_status": "PASS",
                    "profile_guard_shadow_hit_keys": ["HIGH_RSI_REBOUND"],
                    "profile_guard_default_shadow_hit_keys": [],
                }
            )
            samples.append(sample)
        for order_id in range(11, 21):
            sample = _sample(
                order_id=order_id,
                segment="WD-12",
                result="WIN",
                pnl=8.0,
                level="S",
                rsi=34.0,
            )
            sample.update(
                {
                    "profile_guard_shadow_status": "PASS",
                    "profile_guard_default_shadow_status": "WOULD_BLOCK",
                    "profile_guard_shadow_hit_keys": [],
                    "profile_guard_default_shadow_hit_keys": ["LOW_RSI_REBOUND"],
                }
            )
            samples.append(sample)
        for order_id in range(21, 26):
            sample = _sample(
                order_id=order_id,
                segment="WD-03",
                result="WIN",
                pnl=8.0,
                level="S",
                rsi=34.0,
            )
            sample.update(
                {
                    "profile_guard_shadow_status": "PASS",
                    "profile_guard_default_shadow_status": "PASS",
                }
            )
            samples.append(sample)

        report = analyze_samples(samples, min_group_size=2)

        compare = report["profile_guard_shadow_compare"]
        self.assertEqual(compare["observed"]["orders"], 25)
        self.assertEqual(compare["recommended_block_default_pass"]["orders"], 10)
        self.assertEqual(compare["recommended_pass_default_block"]["orders"], 10)
        self.assertEqual(compare["upgrade"]["action"], "PROMOTE_RECOMMENDED_GUARD")
        self.assertEqual(compare["upgrade"]["confidence"], "MEDIUM")

    def test_profile_guard_walk_forward_uses_prior_samples_only(self):
        samples = []
        for index, result in enumerate(["LOSS", "LOSS", "LOSS", "WIN"], start=1):
            samples.append(
                _sample(
                    order_id=index,
                    segment="WD-18",
                    result=result,
                    pnl=-10.0 if result == "LOSS" else 8.0,
                    level="A",
                    rsi=46.0,
                )
            )

        profile = evaluate_profile_guard(samples, min_history=2, min_group_size=2)

        self.assertIn("HIGH_RSI_REBOUND", risk_hint_keys_for_sample(samples[0]))
        self.assertEqual(profile["baseline"]["orders"], 4)
        self.assertEqual(profile["walk_forward_combined"]["blocked"]["orders"], 2)
        self.assertIn("recommended_walk_forward", profile)
        self.assertIn("recommended_key_subset", profile)
        self.assertIn("walk_forward_sweep", profile)
        self.assertIn("key_subset_sweep", profile)
        self.assertIn("replay_upgrade", profile)
        self.assertEqual(
            [item["order_id"] for item in profile["walk_forward_combined"]["blocked_records"]],
            [3, 4],
        )
        contribution = profile["walk_forward_combined"]["blocked_key_contribution"]
        self.assertIn("HIGH_RSI_REBOUND", [item["key"] for item in contribution])
        high_rsi = next(item for item in contribution if item["key"] == "HIGH_RSI_REBOUND")
        self.assertEqual(high_rsi["orders"], 2)
        self.assertEqual(high_rsi["wins"], 1)
        self.assertEqual(high_rsi["losses"], 1)
        self.assertGreaterEqual(profile["key_subset_sweep"]["tested"], 1)
        self.assertIn("candidate_risk_keys", profile["recommended_key_subset"])
        self.assertIn("final_active_keys", profile["recommended_key_subset"])
        self.assertIn("training", profile["recommended_key_subset"])
        self.assertIn("validation", profile["recommended_key_subset"])
        self.assertIn("selection_policy", profile["recommended_key_subset"])
        self.assertIn("stable_best", profile["key_subset_sweep"])

    def test_profile_guard_sweep_ranks_walk_forward_parameters(self):
        samples = []
        for index, result in enumerate(["LOSS", "LOSS", "WIN", "LOSS", "WIN", "LOSS"], start=1):
            samples.append(
                _sample(
                    order_id=index,
                    segment="WD-18",
                    result=result,
                    pnl=-10.0 if result == "LOSS" else 8.0,
                    level="A",
                    rsi=46.0,
                )
            )

        sweep = sweep_profile_guard(
            samples,
            min_history_values=(2, 3, 4),
            min_group_size_values=(2, 3),
            top=3,
        )

        self.assertEqual(sweep["tested"], 5)
        self.assertEqual(len(sweep["top"]), 3)
        self.assertEqual(sweep["best"], sweep["top"][0])
        self.assertIn("score", sweep["best"])
        self.assertGreaterEqual(sweep["best"]["blocked"]["orders"], 1)

    def test_profile_guard_key_subset_sweep_enumerates_candidate_keys(self):
        samples = []
        for index, result in enumerate(["LOSS", "LOSS", "LOSS", "WIN"], start=1):
            samples.append(
                _sample(
                    order_id=index,
                    segment="WD-18",
                    result=result,
                    pnl=-10.0 if result == "LOSS" else 8.0,
                    level="A",
                    rsi=46.0,
                )
            )

        candidate_keys = ["HIGH_RSI_REBOUND", "LEVEL_A_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"]
        sweep = sweep_profile_guard_key_subsets(
            samples,
            candidate_keys=candidate_keys,
            min_history=2,
            min_group_size=2,
            top=4,
        )

        self.assertEqual(sweep["tested"], 7)
        self.assertEqual(len(sweep["top"]), 4)
        self.assertEqual(sweep["best"], sweep["top"][0])
        self.assertEqual(sweep["best"]["name"], "walk_forward_key_subset")
        self.assertIn("candidate_risk_keys", sweep["best"])
        self.assertIn("stability_score", sweep["best"])
        self.assertIn("training", sweep["best"])
        self.assertIn("validation", sweep["best"])
        self.assertIn("stable_best", sweep)
        self.assertIn("stable_top", sweep)
        self.assertIn("selection_policy", sweep)
        if sweep["stable_best"]:
            self.assertIn("selection_policy", sweep["stable_best"])
        self.assertTrue(set(sweep["best"]["candidate_risk_keys"]).issubset(candidate_keys))

    def test_profile_guard_shadow_reports_would_block_current_signal(self):
        signal = Signal(
            direction="WAIT",
            observe_direction="LONG",
            timeframe_minutes=10,
            level="A",
            reason="放量急跌反抽：synthetic",
            price=100.0,
            open_time=1_780_000_000_000,
            threshold_segment="WD-18",
            score=85.0,
            threshold=70.0,
            price_change_pct=-0.0015,
            price_position=0.45,
            rsi=46.0,
            mtf_10m_bias=0.1,
            mtf_30m_bias=0.2,
        )
        profile = {
            "profile_guard": {
                "recommended_key_subset": {
                    "selection_policy": {
                        "name": "STABILITY_BAND",
                        "reason": "最高稳定分组合已满足稳定带",
                        "selected_keys": ["HIGH_RSI_REBOUND"],
                        "score_best_keys": ["HIGH_RSI_REBOUND"],
                    },
                    "final_active_keys": ["HIGH_RSI_REBOUND"],
                    "risk_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                    "min_history": 15,
                    "min_group_size": 2,
                    "traded": {"orders": 29, "win_rate": 0.7241, "ev": 6.06, "pnl": 175.68},
                    "blocked": {"orders": 20},
                    "delta_pnl": 216.24,
                },
                "recommended_walk_forward": {
                    "risk_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                    "min_history": 15,
                    "min_group_size": 2,
                    "traded": {"orders": 28, "win_rate": 0.7143, "ev": 5.35, "pnl": 149.76},
                    "blocked": {"orders": 21},
                    "delta_pnl": 190.32,
                }
            }
        }

        sample = sample_from_signal(signal)
        shadow = profile_guard_shadow(signal, profile)

        self.assertIn("HIGH_RSI_REBOUND", risk_hint_keys_for_sample(sample))
        self.assertEqual(shadow["status"], "WOULD_BLOCK")
        self.assertEqual(shadow["min_history"], 15)
        self.assertEqual(shadow["hit_keys"], ["HIGH_RSI_REBOUND"])
        self.assertEqual(shadow["recommended"]["blocked_orders"], 20)
        self.assertEqual(shadow["variant"], "recommended_key_subset")
        self.assertEqual(shadow["selection_policy"]["name"], "STABILITY_BAND")

    def test_profile_guard_shadow_upgrade_recommends_block_after_enough_bad_hits(self):
        samples = []
        for order_id in range(1, 10):
            sample = _sample(
                order_id=order_id,
                segment="WD-18",
                result="LOSS",
                pnl=-10.0,
                level="A",
                rsi=46.0,
            )
            sample.update(
                {
                    "profile_guard_shadow_status": "WOULD_BLOCK",
                    "profile_guard_shadow_hit_keys": ["HIGH_RSI_REBOUND"],
                }
            )
            samples.append(sample)
        for order_id in range(10, 31):
            sample = _sample(
                order_id=order_id,
                segment="WD-12",
                result="WIN",
                pnl=8.0,
                level="S",
                rsi=34.0,
            )
            sample.update(
                {
                    "profile_guard_shadow_status": "PASS",
                    "profile_guard_shadow_hit_keys": [],
                }
            )
            samples.append(sample)

        report = analyze_samples(samples, min_group_size=2)

        self.assertEqual(report["profile_guard_shadow"]["would_block"]["orders"], 9)
        self.assertEqual(report["profile_guard_shadow"]["upgrade"]["action"], "READY_TO_BLOCK")
        self.assertEqual(report["profile_guard_shadow"]["upgrade"]["confidence"], "MEDIUM")


def _create_schema(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            """
            create table order_entry_snapshots (
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
                primary key(symbol, order_id)
            )
            """
        )
        connection.commit()


def _insert_snapshot(
    db_path: Path,
    *,
    order_id: int,
    segment: str,
    result: str,
    pnl: float,
    level: str,
    price_position: float,
    price_change_pct: float,
    rsi: float,
    mtf_10m_bias: float,
    mtf_30m_bias: float,
    shadow_status: str = "",
    default_shadow_status: str = "",
) -> None:
    signal = {
        "direction": "LONG",
        "timeframe_minutes": 10,
        "level": level,
        "reason": "放量急跌反抽：synthetic",
        "score": 85.0,
        "threshold": 70.0,
        "volume_ratio": 2.1,
        "volume_threshold": 1.5,
        "price_change_pct": price_change_pct,
        "price_position": price_position,
        "rsi": rsi,
        "bollinger_position": 0.2,
        "mtf_10m_bias": mtf_10m_bias,
        "mtf_30m_bias": mtf_30m_bias,
        "regime": "FEAR_FALLING",
    }
    payload = {
        "signal": signal,
        "fear_greed": {"value": 12, "trend": "falling"},
    }
    if shadow_status:
        payload["profile_guard_shadow"] = {
            "status": shadow_status,
            "hit_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"] if shadow_status == "WOULD_BLOCK" else [],
            "active_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
            "min_history": 15,
            "min_group_size": 2,
            "variant": "recommended_key_subset",
            "selection_policy": {
                "name": "STABILITY_BAND",
                "reason": "最高稳定分组合已满足稳定带",
                "selected_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                "score_best_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
            },
        }
        payload["profile_guard_selection_policy"] = payload["profile_guard_shadow"]["selection_policy"]
    if default_shadow_status:
        payload["profile_guard_default_shadow"] = {
            "status": default_shadow_status,
            "hit_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"]
            if default_shadow_status == "WOULD_BLOCK"
            else [],
            "active_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
            "min_history": 15,
            "min_group_size": 2,
            "variant": "walk_forward_combined",
        }
    opened_at = 1_780_000_000_000 + order_id * 600_000
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            """
            insert into order_entry_snapshots(
                symbol, order_id, direction, timeframe_minutes, opened_at, expires_at,
                entry_price, stake, win_return, stake_progression_step,
                threshold_segment, regime, score, threshold, edge,
                result, settled_at, exit_price, pnl, entry_payload, settlement_payload
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "BTCUSDT",
                order_id,
                "LONG",
                10,
                opened_at,
                opened_at + 600_000,
                100.0,
                10.0,
                18.0,
                1,
                segment,
                "FEAR_FALLING",
                85.0,
                70.0,
                15.0,
                result,
                opened_at + 600_000,
                99.0,
                pnl,
                json.dumps(payload),
                None,
            ),
        )
        connection.commit()


def _sample(
    *,
    order_id: int,
    segment: str,
    result: str,
    pnl: float,
    level: str,
    rsi: float,
) -> dict:
    opened_at = 1_780_000_000_000 + order_id * 600_000
    return {
        "symbol": "BTCUSDT",
        "order_id": order_id,
        "direction": "LONG",
        "timeframe_minutes": 10,
        "threshold_segment": segment,
        "result": result,
        "pnl": pnl,
        "stake": 10.0,
        "stake_progression_step": 1,
        "opened_at": opened_at,
        "settled_at": opened_at + 600_000,
        "level": level,
        "reason": "放量急跌反抽：synthetic",
        "reason_setup": "放量急跌反抽",
        "score": 85.0,
        "threshold": 70.0,
        "edge": 15.0,
        "volume_ratio": 2.1,
        "volume_threshold": 1.5,
        "price_change_pct": -0.0015,
        "price_position": 0.45,
        "rsi": rsi,
        "bollinger_position": 0.2,
        "mtf_10m_bias": 0.1,
        "mtf_30m_bias": 0.2,
        "regime": "FEAR_FALLING",
        "risk_flags": "",
        "fear_greed_value": 12,
        "fear_greed_trend": "falling",
    }


if __name__ == "__main__":
    unittest.main()
