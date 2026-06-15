import unittest

from app.fear_greed import FearGreedProvider, _trend
from app.models import FearGreedContext


class FearGreedProviderTest(unittest.TestCase):
    def test_trend_compares_latest_value_with_average(self):
        self.assertEqual(_trend([43, 30, 35, 40]), "rising")
        self.assertEqual(_trend([20, 35, 40, 45]), "falling")
        self.assertEqual(_trend([40, 39, 41, 40]), "flat")

    def test_provider_uses_cache_until_ttl_expires(self):
        now = 1_000_000

        class Provider(FearGreedProvider):
            def _fetch(self, current_ms):
                self.fetches += 1
                return FearGreedContext(value=43, classification="Fear", average_30d=37.0, trend="rising", updated_at_ms=current_ms)

        provider = Provider(ttl_seconds=60, now_ms=lambda: now)
        provider.fetches = 0

        first = provider.get_context()
        second = provider.get_context()

        self.assertEqual(first, second)
        self.assertEqual(provider.fetches, 1)


if __name__ == "__main__":
    unittest.main()
