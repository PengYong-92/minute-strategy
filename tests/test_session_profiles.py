import unittest

from app.session_profiles import build_session_edges, stable_segments


class SessionProfilesTest(unittest.TestCase):
    def test_builds_session_edges_and_requires_multiple_profitable_months(self):
        orders = [
            {"entry_time": 1_704_067_200_000, "timeframe_minutes": 10, "threshold_segment": "WD-12", "result": "WIN", "pnl": 8.0},
            {"entry_time": 1_704_067_800_000, "timeframe_minutes": 10, "threshold_segment": "WD-12", "result": "WIN", "pnl": 8.0},
            {"entry_time": 1_704_068_400_000, "timeframe_minutes": 10, "threshold_segment": "WD-12", "result": "LOSS", "pnl": -10.0},
            {"entry_time": 1_972_512_000_000, "timeframe_minutes": 10, "threshold_segment": "WD-12", "result": "WIN", "pnl": 8.0},
            {"entry_time": 1_972_512_600_000, "timeframe_minutes": 10, "threshold_segment": "WD-12", "result": "WIN", "pnl": 8.0},
            {"entry_time": 1_972_513_200_000, "timeframe_minutes": 10, "threshold_segment": "WD-12", "result": "LOSS", "pnl": -10.0},
            {"entry_time": 1_704_067_200_000, "timeframe_minutes": 30, "threshold_segment": "WD-15", "result": "WIN", "pnl": 8.0},
            {"entry_time": 1_704_067_800_000, "timeframe_minutes": 30, "threshold_segment": "WD-15", "result": "LOSS", "pnl": -10.0},
        ]

        edges = build_session_edges(orders, min_sample_size=3)
        stable = stable_segments(orders, min_months=2, min_sample_size=3, min_win_rate=0.60, min_ev=0.0)

        self.assertEqual(edges["10|WD-12"].sample_size, 6)
        self.assertIn("10|WD-12", stable)
        self.assertNotIn("30|WD-15", stable)


if __name__ == "__main__":
    unittest.main()
