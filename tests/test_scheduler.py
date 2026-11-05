"""调度与交易编排测试（假行情源/假决策，不访问网络）。"""

import asyncio
import logging
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.support import make_quote, submodule

CST = ZoneInfo("Asia/Shanghai")
TODAY = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
TODAY_TEXT = "2026-11-03"


class FakeSource:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    async def fetch(self, symbols):
        self.calls += 1
        quotes = submodule("quotes")
        if not self.batches:
            raise quotes.QuoteFetchError("fake source exhausted")
        item = self.batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeDecider:
    def __init__(self, decision=None):
        self.decision = decision
        self.calls = 0

    async def decide(self, decision_input):
        self.calls += 1
        return self.decision


class Recorder:
    def __init__(self):
        self.messages = []

    async def __call__(self, text):
        self.messages.append(text)


def make_batch(now, quotes):
    quotes_module = submodule("quotes")
    return quotes_module.QuoteBatch(quotes=quotes, errors={}, fetched_at=now)


class EngineHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_module = submodule("ledger")
        self.contracts = submodule("contracts")
        self.config_module = submodule("config")
        self.decision_module = submodule("decision")
        self.scheduler = submodule("scheduler")
        self.bars = submodule("bars")
        self.quotes = submodule("quotes")
        self.ledger = self.ledger_module.Ledger(Path(self._tmp.name) / "ledger.sqlite3")
        self.ledger.initialize(1_000_000.0, day=TODAY_TEXT)
        self.specs = self.contracts.load_contracts(["RB2701"])
        self.config = self.config_module.MaiFuturesConfig()
        self.config.decision.auto_trade = True
        self.config.decision.interval_minutes = 30
        self.config.decision.min_interval_minutes = 10
        self.config.decision.max_interval_minutes = 90
        self.config.market.quote_poll_seconds = 10
        self.config.market.stale_quote_seconds = 180
        self.config.risk.fill_max_quote_age_seconds = 10
        self.config.decision.warmup_samples = 5

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def make_engine(self, source, decider=None, clock=None, **extra):
        self.notify = Recorder()
        engine = self.scheduler.TradingEngine(
            config=self.config,
            specs=self.specs,
            ledger=self.ledger,
            quote_source=source,
            sample_book=self.bars.SampleBook(),
            decision_engine=decider or FakeDecider(None),
            notify=self.notify,
            logger=logging.getLogger("test.scheduler"),
            clock=clock or (lambda: TODAY),
            **extra,
        )
        return engine

    def seed_samples(self, engine, prices=None):
        base = TODAY
        for index, price in enumerate(prices or [3000.0, 3002.0, 3001.0, 3003.0, 3002.0]):
            engine._samples.add(make_quote(now=base + timedelta(minutes=index), last=price, age_seconds=0))


class SessionHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scheduler = submodule("scheduler")

    def test_parse_sessions(self):
        sessions = self.scheduler.parse_sessions(["09:00-10:15", "10:30-11:30", "13:30-15:00"])
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sessions[0][0].strftime("%H:%M"), "09:00")

    def test_parse_sessions_invalid(self):
        for entry in (["bad"], ["10:00-09:00"], [""]):
            with self.assertRaises(ValueError):
                self.scheduler.parse_sessions(entry)
        with self.assertRaises(ValueError):
            self.scheduler.parse_sessions([])

    def test_in_sessions_boundaries(self):
        sessions = self.scheduler.parse_sessions(["09:00-10:15", "13:30-15:00"])

        def at(hour, minute, second=0):
            return datetime(2026, 11, 3, hour, minute, second, tzinfo=CST)

        self.assertTrue(self.scheduler.in_sessions(at(9, 0), sessions))
        self.assertTrue(self.scheduler.in_sessions(at(10, 14, 59), sessions))
        self.assertFalse(self.scheduler.in_sessions(at(10, 15), sessions))
        self.assertFalse(self.scheduler.in_sessions(at(11, 0), sessions))
        self.assertTrue(self.scheduler.in_sessions(at(13, 30), sessions))
        self.assertFalse(self.scheduler.in_sessions(at(15, 0), sessions))

    def test_overnight_sessions(self):
        overnight = [(datetime(2026, 1, 1, 21, 0).time(), datetime(2026, 1, 1, 2, 30).time())]

        def at(hour, minute, second=0):
            return datetime(2026, 11, 3, hour, minute, second, tzinfo=CST)

        self.assertTrue(self.scheduler.in_sessions(at(21, 0), overnight))
        self.assertTrue(self.scheduler.in_sessions(at(23, 59), overnight))
        self.assertTrue(self.scheduler.in_sessions(at(0, 30), overnight))
        self.assertTrue(self.scheduler.in_sessions(at(2, 29, 59), overnight))
        self.assertFalse(self.scheduler.in_sessions(at(2, 30), overnight))
        self.assertFalse(self.scheduler.in_sessions(at(9, 0), overnight))

    def test_session_profiles(self):
        contracts = submodule("contracts")
        day = self.scheduler.parse_sessions(["09:00-10:15", "10:30-11:30", "13:30-15:00"])
        rb = self.scheduler.session_ranges(contracts.get_contract("RB2701"), day, night_enabled=True)
        au = self.scheduler.session_ranges(contracts.get_contract("AU2612"), day, night_enabled=True)
        cu = self.scheduler.session_ranges(contracts.get_contract("CU2612"), day, night_enabled=True)

        def at(hour, minute):
            return datetime(2026, 11, 3, hour, minute, tzinfo=CST)

        self.assertTrue(self.scheduler.in_sessions(at(22, 0), rb))
        self.assertFalse(self.scheduler.in_sessions(at(23, 30), rb))
        self.assertTrue(self.scheduler.in_sessions(at(0, 30), au))
        self.assertTrue(self.scheduler.in_sessions(at(2, 29), au))
        self.assertTrue(self.scheduler.in_sessions(at(0, 30), cu))
        self.assertFalse(self.scheduler.in_sessions(at(1, 30), cu))

    def test_trading_day_attribution(self):
        friday_night = datetime(2026, 11, 6, 21, 30, tzinfo=CST)
        saturday_early = datetime(2026, 11, 7, 0, 30, tzinfo=CST)
        tuesday_early = datetime(2026, 11, 3, 0, 30, tzinfo=CST)
        self.assertEqual(self.scheduler.trading_day(friday_night).isoformat(), "2026-11-09")
        self.assertEqual(self.scheduler.trading_day(saturday_early).isoformat(), "2026-11-09")
        self.assertEqual(self.scheduler.trading_day(tuesday_early).isoformat(), "2026-11-03")


class TradingTickTest(EngineHarness):
    def test_auto_rollover_switches_contract(self):
        self.specs["RB2701"] = replace(
            self.specs["RB2701"], last_trade_date="2026-11-05", rollover_days=3
        )
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        self.config.decision.auto_trade = False
        engine = self.make_engine(source, clock=lambda: TODAY)
        asyncio.run(engine.tick(TODAY))
        self.assertIn("RB2705", engine.specs)
        self.assertNotIn("RB2701", engine.specs)
        self.assertTrue(any("换月" in message for message in self.notify.messages))

    def test_official_settlement_provider_used(self):
        self.ledger.try_open(
            order_id="setup-official",
            decision_id="setup-official",
            ts=TODAY.isoformat(timespec="seconds"),
            spec=self.specs["RB2701"],
            side="long",
            lots=1,
            price=3000.0,
            stop_price=2950.0,
            take_price=3100.0,
        )

        async def provider(day_text, symbols):
            return {"RB2701": 3100.0}

        engine = self.make_engine(
            FakeSource([]),
            settlement_provider=provider,
            clock=lambda: TODAY.replace(hour=15, minute=10),
        )
        asyncio.run(engine.tick(TODAY.replace(hour=15, minute=10)))
        records = self.ledger.settlements_for(TODAY_TEXT)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].settle_price, 3100.0)

    def test_night_session_tick_rolls_trading_day(self):
        night = datetime(2026, 11, 3, 21, 30, 0, tzinfo=CST)  # 周二夜盘属于周三交易日
        quote = make_quote(now=night, age_seconds=1, last=3000.0, bid1=2999.0, ask1=3000.0)
        source = FakeSource([make_batch(night, {"RB2701": quote})])
        self.config.decision.auto_trade = False
        engine = self.make_engine(source, clock=lambda: night)
        asyncio.run(engine.tick(night))
        self.assertEqual(self.ledger.account().day, "2026-11-04")
        self.assertIn("RB2701", engine.last_quotes)
        self.assertEqual(engine._samples.count("RB2701"), 1)

    def test_open_flow_with_decision(self):
        quote = make_quote(now=TODAY, age_seconds=1, last=3007.0, bid1=3006.0, ask1=3007.0)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote}), make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(
            self.decision_module.DecisionText(
                action="open_long", symbol="RB2701", lots=1, reason="测试突破", speech="冲"
            )
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)

        async def run_tick_and_decision():
            await engine.tick(TODAY)
            task = engine._decision_task
            if task is not None:
                await task

        asyncio.run(run_tick_and_decision())
        position = self.ledger.position("RB2701")
        self.assertIsNotNone(position)
        self.assertEqual(position.side, "long")
        # 卖一 3007 + 1 跳 = 3008
        self.assertAlmostEqual(position.entry_price, 3008.0)
        self.assertEqual(self.ledger.account().trades_today, 1)
        self.assertTrue(any("开多" in message for message in self.notify.messages))
        decisions = self.ledger.recent_decisions(5)
        self.assertEqual(decisions[0].outcome, "opened")

    def test_no_auto_trade_skips_decision(self):
        self.config.decision.auto_trade = False
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(None)
        engine = self.make_engine(source, decider)
        asyncio.run(engine.tick(TODAY))
        self.assertEqual(decider.calls, 0)
        self.assertIsNone(self.ledger.position("RB2701"))

    def test_paused_skips_decision(self):
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(None)
        engine = self.make_engine(source, decider)
        engine.set_paused(True)
        asyncio.run(engine.tick(TODAY))
        self.assertEqual(decider.calls, 0)
        self.assertTrue(engine.paused)

    def test_stale_quote_skips_decision(self):
        quote = make_quote(now=TODAY, age_seconds=300)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(None)
        engine = self.make_engine(source, decider)
        asyncio.run(engine.tick(TODAY))
        self.assertEqual(decider.calls, 0)

    def test_source_failure_does_not_raise(self):
        source = FakeSource([self.quotes.QuoteFetchError("boom")])
        engine = self.make_engine(source)
        asyncio.run(engine.tick(TODAY))  # 不应抛异常

    def test_stop_loss_closes_position(self):
        self.config.decision.auto_trade = False
        spec = self.specs["RB2701"]
        self.ledger.try_open(
            order_id="o1",
            decision_id="d1",
            ts="2026-11-03T09:30:00",
            spec=spec,
            side="long",
            lots=1,
            price=3005.0,
            stop_price=3000.0,
            take_price=3100.0,
        )
        quote = make_quote(now=TODAY, age_seconds=1, last=2999.0, bid1=2998.0, ask1=2999.0)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        engine = self.make_engine(source)
        asyncio.run(engine.tick(TODAY))
        self.assertIsNone(self.ledger.position("RB2701"))
        self.assertTrue(any("止损" in message for message in self.notify.messages))
        trades = self.ledger.recent_trades(5)
        self.assertEqual(trades[0].action, "close")

    def test_settlement_after_close(self):
        self.config.decision.auto_trade = False
        spec = self.specs["RB2701"]
        self.ledger.try_open(
            order_id="o1",
            decision_id="d1",
            ts="2026-11-03T09:30:00",
            spec=spec,
            side="long",
            lots=1,
            price=3005.0,
            stop_price=2900.0,
            take_price=3200.0,
        )
        settle_now = datetime(2026, 11, 3, 15, 10, 0, tzinfo=CST)
        quote = make_quote(now=settle_now, age_seconds=600, last=3008.0, settle=3010.0, bid1=3007.0, ask1=3008.0)
        source = FakeSource([make_batch(settle_now, {"RB2701": quote})])
        engine = self.make_engine(source, clock=lambda: settle_now)
        asyncio.run(engine.tick(settle_now))
        settlements = self.ledger.settlements_for(TODAY_TEXT)
        self.assertEqual(len(settlements), 1)
        self.assertAlmostEqual(settlements[0].settle_price, 3010.0)
        self.assertTrue(any("结算" in message for message in self.notify.messages))
        before = len(self.notify.messages)
        asyncio.run(engine.tick(settle_now))
        self.assertEqual(len(self.notify.messages), before)  # 不重复播报

    def test_rollover_on_new_day(self):
        self.config.decision.auto_trade = False
        self.ledger.rollover("2026-11-02", {})
        self.ledger.set_halted(True)
        self.ledger.rollover("2026-11-02", {})
        # 强制把日期改回 11-02，验证 tick 会自动 rollover 到 11-03
        self.ledger.set_meta("force_day", "2026-11-02")
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        engine = self.make_engine(source)
        asyncio.run(engine.tick(TODAY))
        self.assertEqual(self.ledger.account().day, TODAY_TEXT)

    def test_daily_loss_halts_trading(self):
        self.ledger.close()
        self.ledger = self.ledger_module.Ledger(Path(self._tmp.name) / "loss.sqlite3")
        self.ledger.initialize(100_000.0, day=TODAY_TEXT)
        self.config.decision.auto_trade = False
        spec = self.specs["RB2701"]
        self.ledger.try_open(
            order_id="o1",
            decision_id="d1",
            ts="2026-11-03T09:30:00",
            spec=spec,
            side="long",
            lots=1,
            price=3000.0,
            stop_price=1000.0,
            take_price=5000.0,
        )
        quote = make_quote(now=TODAY, age_seconds=1, last=2000.0, bid1=1999.0, ask1=2000.0, prev_settle=2000.0)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        engine = self.make_engine(source)
        asyncio.run(engine.tick(TODAY))
        self.assertTrue(self.ledger.account().halted)
        self.assertTrue(any("停止开新仓" in message for message in self.notify.messages))

    def test_dry_run(self):
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(
            self.decision_module.DecisionText(action="hold", symbol="", lots=1, reason="等", speech="")
        )
        engine = self.make_engine(source, decider)
        text = asyncio.run(engine.dry_run())
        self.assertIn("动作：hold", text)


if __name__ == "__main__":
    unittest.main()
