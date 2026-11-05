"""市场概览输出测试（假行情源，不访问网络）。"""

import asyncio
import unittest
from types import SimpleNamespace

from tests.support import make_quote, submodule

tools = submodule("tools")
quotes = submodule("quotes")


class FakeSource:
    def __init__(self, batch):
        self.batch = batch

    async def fetch(self, symbols):
        return self.batch


class OverviewTest(unittest.TestCase):
    def test_overview_groups_categories(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        cst = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 11, 4, 22, 0, 0, tzinfo=cst)
        q1 = make_quote(symbol="RB2701", now=now, last=3000.0, prev_close=2970.0)
        q2 = make_quote(symbol="M2701", now=now, last=2800.0, prev_close=2810.0)
        batch = quotes.QuoteBatch(quotes={"RB2701": q1, "M2701": q2}, errors={}, fetched_at=now)
        runtime = SimpleNamespace(
            profile=SimpleNamespace(label="国内商品期货", currency="麦麦币"),
            engine=SimpleNamespace(
                specs={
                    "RB2701": SimpleNamespace(name="螺纹钢2701"),
                    "M2701": SimpleNamespace(name="豆粕2701"),
                }
            ),
            quote_source=FakeSource(batch),
        )
        text = asyncio.run(tools.market_overview([runtime]))
        self.assertIn("市场概览", text)
        self.assertNotIn("麦麦币", text)
        self.assertIn("国内商品期货", text)
        self.assertIn("黑色", text)
        self.assertIn("农产品", text)
        self.assertIn("+1.01%", text)
        self.assertIn("-0.36%", text)
        self.assertNotIn("RB2701", text)
        self.assertNotIn("M2701", text)
