"""成交规则测试。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.support import make_quote

CST = ZoneInfo("Asia/Shanghai")


class ExecutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.support import submodule

        cls.execution = submodule("execution")
        cls.contracts = submodule("contracts")
        cls.rb = cls.contracts.get_contract("RB2701")
        cls.au = cls.contracts.get_contract("AU2612")

    def test_buy_uses_ask_plus_slippage(self):
        quote = make_quote(bid1=3006.0, ask1=3007.0)
        plan = self.execution.make_fill_plan(
            action="open_long", quote=quote, spec=self.rb, lots=1, slippage_ticks=1
        )
        self.assertAlmostEqual(plan.price, 3008.0)
        self.assertAlmostEqual(plan.notional, 30080.0)
        self.assertAlmostEqual(plan.fee, 30080.0 * 0.0001)
        self.assertAlmostEqual(plan.margin, 3008.0)

    def test_sell_uses_bid_minus_slippage(self):
        quote = make_quote(bid1=3006.0, ask1=3007.0)
        plan = self.execution.make_fill_plan(
            action="close_long", quote=quote, spec=self.rb, lots=1, slippage_ticks=1
        )
        self.assertAlmostEqual(plan.price, 3005.0)

    def test_tick_slippage_on_gold(self):
        quote = make_quote(symbol="AU2612", bid1=849.06, ask1=849.08)
        plan = self.execution.make_fill_plan(
            action="open_long", quote=quote, spec=self.au, lots=1, slippage_ticks=1
        )
        self.assertAlmostEqual(plan.price, 849.10, places=6)

    def test_zero_slippage(self):
        quote = make_quote(bid1=3006.0, ask1=3007.0)
        plan = self.execution.make_fill_plan(
            action="open_short", quote=quote, spec=self.rb, lots=1, slippage_ticks=0
        )
        self.assertAlmostEqual(plan.price, 3006.0)

    def test_hold_not_executable(self):
        quote = make_quote()
        with self.assertRaises(self.execution.ExecutionError):
            self.execution.make_fill_plan(action="hold", quote=quote, spec=self.rb, lots=1, slippage_ticks=1)

    def test_invalid_lots(self):
        quote = make_quote()
        for lots in (0, -1, 1.5):
            with self.assertRaises(self.execution.ExecutionError):
                self.execution.make_fill_plan(
                    action="open_long", quote=quote, spec=self.rb, lots=lots, slippage_ticks=1
                )

    def test_fill_ready_ok(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        quote = make_quote(now=now, age_seconds=1, last=3007.0, bid1=3006.0, ask1=3007.0)
        reason = self.execution.check_fill_ready(
            quote=quote,
            t0_mid=3006.5,
            now=now,
            spec=self.rb,
            max_quote_age_seconds=10,
            max_drift_ticks=5,
        )
        self.assertEqual(reason, "")

    def test_fill_ready_stale(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        quote = make_quote(now=now, age_seconds=30)
        reason = self.execution.check_fill_ready(
            quote=quote,
            t0_mid=quote.mid,
            now=now,
            spec=self.rb,
            max_quote_age_seconds=10,
            max_drift_ticks=5,
        )
        self.assertIn("过期", reason)

    def test_fill_ready_drift(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        quote = make_quote(now=now, age_seconds=1, last=3010.0, bid1=3009.0, ask1=3010.0)
        reason = self.execution.check_fill_ready(
            quote=quote,
            t0_mid=3000.0,
            now=now,
            spec=self.rb,
            max_quote_age_seconds=10,
            max_drift_ticks=5,
        )
        self.assertIn("漂移", reason)

    def test_fill_ready_invalid_book(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        quote = make_quote(now=now, bid1=0.0, ask1=0.0)
        reason = self.execution.check_fill_ready(
            quote=quote,
            t0_mid=3007.0,
            now=now,
            spec=self.rb,
            max_quote_age_seconds=10,
            max_drift_ticks=5,
        )
        self.assertIn("买一", reason)

    def test_fill_ready_missing_quote(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        reason = self.execution.check_fill_ready(
            quote=None,
            t0_mid=3007.0,
            now=now,
            spec=self.rb,
            max_quote_age_seconds=10,
            max_drift_ticks=5,
        )
        self.assertIn("缺少", reason)


if __name__ == "__main__":
    unittest.main()
