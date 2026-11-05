"""合约参数表测试。"""

import unittest
from datetime import date

from tests.support import submodule

contracts = submodule("contracts")


class ContractTest(unittest.TestCase):
    def test_default_contracts(self):
        specs = contracts.load_contracts(["RB2701", "M2701", "AU2612"])
        self.assertEqual(set(specs), {"RB2701", "M2701", "AU2612"})
        self.assertEqual(specs["RB2701"].multiplier, 10.0)
        self.assertEqual(specs["AU2612"].multiplier, 1000.0)
        self.assertEqual(specs["M2701"].exchange, "DCE")

    def test_cn_market_profile(self):
        specs = contracts.resolve_market_contracts(["cn"])
        self.assertEqual(
            set(specs),
            {
                "SC2701",
                "AU2612",
                "AG2612",
                "CU2612",
                "AL2612",
                "RB2701",
                "I2701",
                "M2701",
                "SR2701",
                "CF2701",
                "MA2701",
                "LH2701",
                "FG2701",
                "SA2701",
                "UR2701",
                "RU2701",
                "Y2701",
                "P2701",
                "JD2701",
                "AP2701",
            },
        )
        self.assertEqual(specs["AG2612"].exchange, "SHFE")
        self.assertEqual(specs["CU2612"].multiplier, 5.0)
        self.assertEqual(specs["I2701"].exchange, "DCE")
        self.assertEqual(specs["SC2701"].exchange, "INE")
        self.assertEqual(specs["SR2701"].exchange, "CZCE")
        self.assertEqual(specs["RB2701"].session_profile, "night_2300")
        self.assertEqual(specs["AU2612"].session_profile, "night_0230")
        self.assertEqual(specs["CU2612"].session_profile, "night_0100")
        self.assertEqual(specs["MA2701"].display_name, "甲醇")
        self.assertEqual(specs["LH2701"].display_name, "生猪")
        self.assertEqual(specs["RB2701"].display_name, "螺纹钢")

    def test_market_alias_and_advanced_override(self):
        self.assertIn("RB2701", contracts.resolve_market_contracts(["国内"]))
        self.assertEqual(
            set(contracts.resolve_market_contracts([], ["RB2701"])),
            {"RB2701"},
        )

    def test_unknown_market_raises(self):
        with self.assertRaises(contracts.ContractError):
            contracts.resolve_market_contracts(["us"])

    def test_intl_profile(self):
        profile = contracts.MARKET_PROFILES["intl"]
        self.assertEqual(profile.currency, "麦麦币")
        self.assertEqual(profile.initial_balance, 100_000.0)
        self.assertEqual(profile.quote_style, "hf")
        self.assertTrue(profile.settle_enabled)
        self.assertTrue(profile.settle_use_last)
        self.assertEqual(profile.settle_time, "05:10")
        specs = contracts.resolve_market_contracts(["intl"])
        self.assertEqual(
            set(specs),
            {"OIL", "GC", "SI", "CAD", "S", "C", "W", "CT", "KC"},
        )
        self.assertEqual(specs["GC"].multiplier, 100.0)
        self.assertEqual(specs["OIL"].exchange, "ICE")

    def test_all_cn_products_have_rollover_chain(self):
        specs = contracts.resolve_market_contracts(["cn"])
        for symbol, spec in specs.items():
            self.assertTrue(spec.rollover_to, f"{symbol} 缺少下一主力")
            self.assertIn(spec.rollover_to, contracts.DEFAULT_CONTRACTS)
            self.assertTrue(
                contracts.DEFAULT_CONTRACTS[spec.rollover_to].rollover_to,
                f"{symbol} 的下一级缺少再下一级",
            )

    def test_multi_level_rollover_chain(self):
        self.assertEqual(contracts.DEFAULT_CONTRACTS["RB2701"].rollover_to, "RB2705")
        self.assertEqual(contracts.DEFAULT_CONTRACTS["RB2705"].rollover_to, "RB2710")
        self.assertIn("RB2710", contracts.DEFAULT_CONTRACTS)
        self.assertEqual(contracts.DEFAULT_CONTRACTS["M2701"].rollover_to, "M2705")
        self.assertEqual(contracts.DEFAULT_CONTRACTS["M2705"].rollover_to, "M2709")
        self.assertIn("M2709", contracts.DEFAULT_CONTRACTS)

    def test_lowercase_and_spaces_normalized(self):
        specs = contracts.load_contracts([" rb2701 "])
        self.assertIn("RB2701", specs)

    def test_unknown_contract_raises(self):
        with self.assertRaises(contracts.ContractError):
            contracts.load_contracts(["RB9999"])

    def test_empty_pool_raises(self):
        with self.assertRaises(contracts.ContractError):
            contracts.load_contracts([])

    def test_fee_and_margin_math(self):
        spec = contracts.get_contract("RB2701")
        self.assertAlmostEqual(spec.notional(3000.0, 1), 30000.0, places=6)
        self.assertAlmostEqual(spec.margin(3000.0, 1), 3000.0, places=6)
        self.assertAlmostEqual(spec.fee(3000.0, 1), 3.0, places=6)  # 30000 * 0.0001

    def test_fixed_fee_contract(self):
        spec = contracts.get_contract("AU2612")
        self.assertAlmostEqual(spec.fee(850.0, 2), 20.0, places=6)

    def test_days_to_expiry(self):
        rb = contracts.get_contract("RB2701")
        self.assertEqual(rb.days_to_expiry(date(2027, 1, 10)), 5)
        self.assertLess(rb.days_to_expiry(date(2027, 1, 16)), 0)


if __name__ == "__main__":
    unittest.main()
