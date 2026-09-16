import unittest

import monitor


class MonitorTests(unittest.TestCase):
    def test_to_float(self):
        self.assertEqual(monitor._to_float("12.5"), 12.5)
        self.assertEqual(monitor._to_float(None), 0.0)

    def test_eligible_pairs(self):
        pair = monitor.Pair(
            name="Example",
            symbol="EX",
            price_usd=1.0,
            liquidity_usd=50000,
            volume_24h_usd=20000,
            change_5m_pct=1.0,
            dex_id="test",
            url="",
            pair_address="abc",
        )
        config = {
            "min_liquidity_usd": 10000,
            "min_volume_24h_usd": 5000,
        }
        self.assertEqual(monitor.eligible_pairs([pair], config), [pair])

    def test_alert_reason(self):
        pair = monitor.Pair(
            name="Example",
            symbol="EX",
            price_usd=1.0,
            liquidity_usd=50000,
            volume_24h_usd=20000,
            change_5m_pct=-7.5,
            dex_id="test",
            url="",
            pair_address="abc",
        )
        config = {"alert_abs_price_change_5m_pct": 5}
        self.assertIn("down 7.50%", monitor.alert_reason(pair, config))


if __name__ == "__main__":
    unittest.main()
