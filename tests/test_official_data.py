"""交易所官方日历/结算价解析与缓存测试（不访问网络）。"""

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.support import submodule

official = submodule("official_data")
calendar = submodule("market_calendar")


class OfficialParseTest(unittest.TestCase):
    def test_parse_shfe_calendar(self):
        payload = [
            {"tradeDate": "20261104", "exchangeGroupId": "shfe"},
            {"tradeDate": "20261001", "exchangeGroupId": "shfe"},
            {"tradeDate": "", "exchangeGroupId": "shfe"},
        ]
        days = official.parse_shfe_calendar(payload)
        self.assertIn(date(2026, 11, 4), days)
        self.assertIn(date(2026, 10, 1), days)

    def test_parse_shfe_settlements(self):
        payload = {
            "Settlement": [
                {"INSTRUMENTID": "cu2612", "SETTLEMENTPRICE": "106230"},
                {"INSTRUMENTID": "rb2701", "SETTLEMENTPRICE": "3017"},
                {"INSTRUMENTID": "x", "SETTLEMENTPRICE": "0"},
            ]
        }
        prices = official.parse_shfe_settlements(payload)
        self.assertEqual(prices["CU2612"], 106230.0)
        self.assertEqual(prices["RB2701"], 3017.0)
        self.assertNotIn("X", prices)

    def test_parse_czce_settlements(self):
        text = "\n".join(
            [
                "合约代码|昨结算    |今开盘    |最高价    |最低价    |今收盘    |今结算    |涨跌1",
                "CF701 |15,135.00 |15,185.00 |15,320.00 |15,120.00 |15,235.00 |15,205.00 |100.00",
                "SR701 |5,220.00  |5,206.00  |5,244.00  |5,200.00  |5,242.00  |5,228.00  |22.00",
            ]
        )
        prices = official.parse_czce_settlements(text)
        self.assertEqual(prices["CF701"], 15205.0)
        self.assertEqual(prices["SR701"], 5228.0)

    def test_parse_shfe_holiday_page(self):
        html = """
        <div class="article_title">2026年休市安排</div>
        <div>一、元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。2025年12月31日（星期三）晚上不进行夜盘交易。</div>
        <div>二、春节：2月15日（星期日）至2月23日（星期一）休市，2月24日（星期二）起照常开市。2月13日（星期五）晚上不进行夜盘交易。</div>
        <div>三、国庆节：10月1日（星期四）至10月7日（星期三）休市，10月8日（星期四）起照常开市。9月30日（星期三）晚上不进行夜盘交易。</div>
        """
        year, holidays, no_night = official.parse_shfe_holiday_page(html)
        self.assertEqual(year, 2026)
        self.assertIn(date(2026, 1, 1), holidays)
        self.assertIn(date(2026, 2, 23), holidays)
        self.assertIn(date(2026, 10, 7), holidays)
        self.assertIn(date(2025, 12, 31), no_night)
        self.assertIn(date(2026, 2, 13), no_night)
        self.assertIn(date(2026, 9, 30), no_night)


class OfficialCacheTest(unittest.TestCase):
    def tearDown(self):
        calendar.clear_official_calendar("cn")

    def test_cache_loads_official_holidays(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = {
                "calendar": {
                    "2026": {
                        "holidays": ["2026-10-01", "2026-10-02"],
                        "no_night": ["2026-09-30"],
                    }
                },
                "settlements": {"2026-11-04": {"RB2701": 3017.0}},
            }
            Path(tmp, "official_cache.json").write_text(
                json.dumps(cache), encoding="utf-8"
            )
            service = official.OfficialDataService(Path(tmp), logger=_FakeLogger())
            self.assertEqual(calendar.calendar_source("cn"), "official")
            self.assertTrue(calendar.is_trading_day("cn", date(2026, 11, 4)))
            self.assertFalse(calendar.is_trading_day("cn", date(2026, 10, 1)))
            self.assertFalse(calendar.night_session_allowed("cn", date(2026, 9, 30)))
            prices = asyncio.run(
                service.settlements("cn", "2026-11-04", ["RB2701", "M2701"])
            )
            self.assertEqual(prices, {"RB2701": 3017.0})


class _FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass
