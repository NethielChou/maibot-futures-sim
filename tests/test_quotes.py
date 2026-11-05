"""行情解析/新鲜度/行情源测试（不访问网络）。"""

import asyncio
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.support import make_commodity_line, make_quote, submodule

quotes = submodule("quotes")
CST = ZoneInfo("Asia/Shanghai")


class QuoteParseTest(unittest.TestCase):
    def test_parse_commodity_line(self):
        line = make_commodity_line(symbol="RB2701", last="3007.000", bid1="3006.000", ask1="3007.000")
        quote = quotes.parse_quote_line(line, received_at=datetime(2026, 11, 3, 10, 0, 5, tzinfo=CST))
        self.assertEqual(quote.symbol, "RB2701")
        self.assertEqual(quote.name, "螺纹钢2701")
        self.assertAlmostEqual(quote.last, 3007.0)
        self.assertAlmostEqual(quote.bid1, 3006.0)
        self.assertAlmostEqual(quote.ask1, 3007.0)
        self.assertAlmostEqual(quote.prev_settle, 3039.0)
        self.assertEqual(quote.quote_time.strftime("%Y-%m-%d %H:%M:%S"), "2026-11-03 10:00:00")
        self.assertEqual(quote.bid1_volume, 1844)
        self.assertEqual(quote.ask1_volume, 86)

    def test_parse_batch_with_one_bad_line(self):
        good = make_commodity_line(symbol="RB2701")
        bad = 'var hq_str_nf_M2701="1,2,3";'
        parsed, errors = quotes.parse_quote_text(good + "\n" + bad)
        self.assertIn("RB2701", parsed)
        self.assertIn("nf_M2701", errors)

    def test_parse_intl_line(self):
        line = (
            'var hq_str_hf_OIL="102.395,,102.510,102.520,104.960,101.550,'
            '19:12:00,103.600,104.230,0,2,2,2026-11-04,布伦特原油,132677";'
        )
        quote = quotes.parse_intl_quote_line(
            line, received_at=datetime(2026, 11, 4, 19, 12, 30, tzinfo=CST)
        )
        self.assertEqual(quote.symbol, "OIL")
        self.assertEqual(quote.name, "布伦特原油")
        self.assertAlmostEqual(quote.last, 102.395)
        self.assertAlmostEqual(quote.bid1, 102.51)
        self.assertAlmostEqual(quote.ask1, 102.52)
        self.assertAlmostEqual(quote.prev_close, 103.6)
        self.assertEqual(quote.quote_time.strftime("%H:%M:%S"), "19:12:00")
        self.assertEqual(quote.source, "sina_hf")

    def test_parse_intl_batch(self):
        oil = (
            'var hq_str_hf_OIL="102.395,,102.510,102.520,104.960,101.550,'
            '19:12:00,103.600,104.230,0,2,2,2026-11-04,布伦特原油,132677";'
        )
        gc = (
            'var hq_str_hf_GC="4052.456,,4051.700,4052.000,4060.000,3928.200,'
            '19:11:54,3940.400,3940.800,0,2,1,2026-11-04,COMEX黄金,0";'
        )
        parsed, errors = quotes.parse_intl_quote_text(oil + "\n" + gc)
        self.assertEqual(errors, {})
        self.assertEqual(set(parsed), {"OIL", "GC"})

    def test_wrong_field_count_rejected(self):
        line = 'var hq_str_nf_RB2701="1,2,3";'
        with self.assertRaises(quotes.QuoteParseError):
            quotes.parse_quote_line(line)

    def test_zero_price_line_rejected(self):
        line = make_commodity_line(last="0.000", settle="0.000")
        with self.assertRaises(quotes.QuoteParseError):
            quotes.parse_quote_line(line)

    def test_mid_and_book(self):
        quote = make_quote(bid1=3006.0, ask1=3008.0)
        self.assertAlmostEqual(quote.mid, 3007.0)
        self.assertTrue(quote.has_valid_book())
        invalid = make_quote(bid1=0.0, ask1=0.0)
        self.assertFalse(invalid.has_valid_book())

    def test_freshness(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        fresh = make_quote(now=now, age_seconds=5)
        stale = make_quote(now=now, age_seconds=300)
        self.assertTrue(quotes.is_quote_fresh(fresh, now, 10))
        self.assertFalse(quotes.is_quote_fresh(stale, now, 10))

    def test_abnormal_change_rejected(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        quote = make_quote(now=now, last=4000.0, prev_settle=3000.0)
        self.assertFalse(quotes.quote_is_usable(quote, max_abs_change_ratio=0.10))


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def __call__(self, url, timeout):
        self.calls += 1
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SinaSourceTest(unittest.TestCase):
    def test_fetch_parses_gb18030(self):
        text = make_commodity_line(symbol="RB2701") + "\n" + make_commodity_line(symbol="M2701", name="豆粕2701")
        raw = text.encode("gb18030")
        source = quotes.SinaQuoteSource(http_get=FakeHttp([raw]), retry_delay_seconds=0)
        batch = asyncio.run(source.fetch(["RB2701", "M2701"]))
        self.assertEqual(set(batch.quotes), {"RB2701", "M2701"})
        self.assertEqual(batch.errors, {})

    def test_fetch_retries_once(self):
        text = make_commodity_line(symbol="RB2701")
        source = quotes.SinaQuoteSource(
            http_get=FakeHttp([OSError("boom"), text.encode("gb18030")]),
            retry_delay_seconds=0,
        )
        batch = asyncio.run(source.fetch(["RB2701"]))
        self.assertIn("RB2701", batch.quotes)

    def test_fetch_reports_missing_symbol(self):
        text = make_commodity_line(symbol="RB2701")
        source = quotes.SinaQuoteSource(http_get=FakeHttp([text.encode("gb18030")]), retry_delay_seconds=0)
        batch = asyncio.run(source.fetch(["RB2701", "AU2612"]))
        self.assertIn("AU2612", batch.errors)

    def test_fetch_failure_raises(self):
        source = quotes.SinaQuoteSource(
            http_get=FakeHttp([OSError("boom"), OSError("boom")]),
            retry_delay_seconds=0,
        )
        with self.assertRaises(quotes.QuoteFetchError):
            asyncio.run(source.fetch(["RB2701"]))


if __name__ == "__main__":
    unittest.main()
