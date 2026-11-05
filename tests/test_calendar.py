"""交易日历测试（不依赖外部数据）。"""

import unittest
from datetime import date

from tests.support import submodule

calendar = submodule("market_calendar")


class CalendarTest(unittest.TestCase):
    def test_cn_weekend_and_holiday(self):
        self.assertTrue(calendar.is_trading_day("cn", date(2026, 11, 3)))  # 周二
        self.assertFalse(calendar.is_trading_day("cn", date(2026, 11, 7)))  # 周六
        self.assertFalse(calendar.is_trading_day("cn", date(2026, 10, 1)))  # 国庆
        self.assertFalse(calendar.is_trading_day("cn", date(2026, 10, 5)))

    def test_next_trading_day_skips_holiday(self):
        # 2026-09-30 周三 -> 国庆后 10-08 周四
        self.assertEqual(calendar.next_trading_day("cn", date(2026, 9, 30)), date(2026, 10, 8))
        # 周五 -> 下周一
        self.assertEqual(calendar.next_trading_day("cn", date(2026, 11, 6)), date(2026, 11, 9))

    def test_us_holiday(self):
        self.assertFalse(calendar.is_trading_day("intl", date(2026, 11, 26)))  # 感恩节
        self.assertTrue(calendar.is_trading_day("intl", date(2026, 11, 27)))

    def test_trading_days_between(self):
        # 2026-09-28 周一 到 2026-10-08 周四：9/28,29,30 与 10/8 共 4 个交易日
        self.assertEqual(
            calendar.trading_days_between("cn", date(2026, 9, 28), date(2026, 10, 8)),
            4,
        )

    def test_holiday_eve_has_no_night_session(self):
        self.assertFalse(calendar.night_session_allowed("cn", date(2026, 9, 30)))
        self.assertTrue(calendar.night_session_allowed("cn", date(2026, 11, 3)))
        self.assertTrue(calendar.night_session_allowed("intl", date(2026, 9, 30)))
