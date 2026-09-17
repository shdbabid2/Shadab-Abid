import unittest
from unittest.mock import patch

import monitor
import trade_engine


class ScannerTests(unittest.TestCase):
    def test_primary_pair_uses_highest_liquidity(self):
        mint = "Mint111"
        raw_a = {
            "baseToken": {"address": mint, "name": "A", "symbol": "A"},
            "quoteToken": {"address": "Quote"},
            "liquidity": {"usd": 10000},
            "volume": {"h24": 1000},
            "txns": {},
            "priceChange": {},
        }
        raw_b = {
            "baseToken": {"address": mint, "name": "A", "symbol": "A"},
            "quoteToken": {"address": "Quote"},
            "liquidity": {"usd": 50000},
            "volume": {"h24": 500},
            "txns": {},
            "priceChange": {},
        }
        pair = monitor.select_primary_pair([raw_a, raw_b], mint)
        self.assertEqual(pair["liquidity_usd"], 50000)

    def test_active_mint_authority_creates_high_flag(self):
        market = {
            "liquidity_usd": 100000,
            "fdv": 500000,
            "change_5m_pct": 5,
            "buys_5m": 20,
            "sells_5m": 10,
            "pair_age_minutes": 15,
        }
        chain = {
            "rpc_mint_ok": True,
            "holder_data_ok": True,
            "mint_authority": "abc",
            "freeze_authority": None,
            "top1_account_pct": 5,
            "top10_accounts_pct": 30,
        }
        score, level, coverage, flags = monitor.risk_analysis(
            market, chain, monitor.DEFAULT_CONFIG
        )
        self.assertEqual(coverage, 100)
        self.assertTrue(any(x["code"] == "MINT_AUTHORITY_ACTIVE" for x in flags))
        self.assertGreater(score, 0)

    def test_trade_gate_requires_99_and_clean(self):
        report = {
            "setup_score": 99,
            "risk_level": "LOW",
            "risk_score": 0,
            "data_coverage_pct": 100,
            "chain": {"mint_authority": None, "freeze_authority": None},
        }
        ok, failures = trade_engine.report_gate(
            report, trade_engine.DEFAULT_TRADING
        )
        self.assertTrue(ok)
        self.assertEqual(failures, [])

    def test_trade_gate_rejects_98(self):
        report = {
            "setup_score": 98,
            "risk_level": "LOW",
            "risk_score": 0,
            "data_coverage_pct": 100,
            "chain": {"mint_authority": None, "freeze_authority": None},
        }
        ok, failures = trade_engine.report_gate(
            report, trade_engine.DEFAULT_TRADING
        )
        self.assertFalse(ok)
        self.assertTrue(any("setup score" in x for x in failures))

    def test_daily_loss_limit(self):
        state = trade_engine.fresh_state()
        state["realized_pnl_usdc"] = -2.70
        ok, reason = trade_engine.daily_gate(
            state, trade_engine.DEFAULT_TRADING
        )
        self.assertFalse(ok)
        self.assertIn("daily loss", reason)

    def test_stop_is_410_for_5_trade(self):
        trading = dict(trade_engine.DEFAULT_TRADING)
        self.assertEqual(trading["trade_size_usdc"], 5.0)
        self.assertEqual(trading["hard_stop_value_usdc"], 4.10)
        self.assertAlmostEqual(
            (trading["trade_size_usdc"] - trading["hard_stop_value_usdc"])
            / trading["trade_size_usdc"]
            * 100,
            18.0,
        )


if __name__ == "__main__":
    unittest.main()
