"""采样与 ATR 代理测试。"""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.support import make_quote, submodule


class SampleBookTest(unittest.TestCase):
    def setUp(self):
        self.bars = submodule("bars")
        self.book = self.bars.SampleBook()

    def _add_prices(self, prices, symbol="RB2701"):
        base = datetime(2026, 11, 3, 9, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        for index, price in enumerate(prices):
            quote = make_quote(symbol=symbol, last=price, now=base + timedelta(minutes=index), age_seconds=0)
            self.book.add(quote)

    def test_empty(self):
        self.assertEqual(self.book.count("RB2701"), 0)
        self.assertIsNone(self.book.atr("RB2701"))
        self.assertIsNone(self.book.last_price("RB2701"))

    def test_atr_proxy(self):
        self._add_prices([100.0, 102.0, 101.0])
        # |102-100|=2, |101-102|=1 -> 1.5
        self.assertAlmostEqual(self.book.atr("RB2701", window=20), 1.5, places=6)
        self.assertEqual(self.book.count("RB2701"), 3)
        self.assertAlmostEqual(self.book.last_price("RB2701"), 101.0)

    def test_atr_window(self):
        self._add_prices([100.0, 110.0, 120.0, 130.0])
        # 只取最近 3 个点：110,120,130 -> diffs 10,10 -> 10.0
        self.assertAlmostEqual(self.book.atr("RB2701", window=2), 10.0, places=6)

    def test_ignore_invalid_price(self):
        self.book.add(make_quote(last=0.0))
        self.assertEqual(self.book.count("RB2701"), 0)

    def test_clear(self):
        self._add_prices([100.0, 101.0])
        self.book.clear()
        self.assertIsNone(self.book.atr("RB2701"))


if __name__ == "__main__":
    unittest.main()
