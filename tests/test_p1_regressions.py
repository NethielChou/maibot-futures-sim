"""Codex 评审 P1 回归测试：每个用例对应一个已知缺陷，先暴露旧问题再修复。

覆盖：
1. LLM 输出 close 时按实际持仓方向映射（平多/平空）；
2. 模型等待期间暂停/重置，旧决策不得成交；
3. 模型等待不阻塞行情与止损巡检；
4. 持仓缺行情时估值不完整、禁止开新仓，且不把浮亏当 0；
5. 配置开关（llm_enabled / allow_short / event_reports / close_report_enabled / warmup）；
6. 结算价缺失 → 持久化 pending、告警、禁止开新仓、按间隔重试；
7. 到期保护（到期前禁开、最后交易日强平）；
8. 行情未来时间戳/非法数值/零挂单量；
9. 账本：原始开仓价与结算成本基准分离、总盈亏含结算、现金恒等式；
10. schema v1 → v2 迁移。
"""

import asyncio
import logging
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.support import make_quote, submodule

CST = ZoneInfo("Asia/Shanghai")
TODAY = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)  # 周二


def make_batch(now, quotes):
    quotes_module = submodule("quotes")
    return quotes_module.QuoteBatch(quotes=quotes, errors={}, fetched_at=now)


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


class SlowDecider:
    """decide() 阻塞到 release 被 set；用于模拟模型思考中的竞态/巡检。"""

    def __init__(self, decision=None):
        self.decision = decision
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(self, decision_input):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self.decision


class Recorder:
    def __init__(self):
        self.messages = []

    async def __call__(self, text):
        self.messages.append(text)


class BaseHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_module = submodule("ledger")
        self.contracts = submodule("contracts")
        self.config_module = submodule("config")
        self.decision_module = submodule("decision")
        self.scheduler = submodule("scheduler")
        self.bars = submodule("bars")
        self.quotes = submodule("quotes")
        self.risk = submodule("risk")
        self.ledger = self.ledger_module.Ledger(Path(self._tmp.name) / "ledger.sqlite3")
        self.ledger.initialize(1_000_000.0, day="2026-11-03")
        self.specs = self.contracts.load_contracts(["RB2701", "M2701", "AU2612"])
        self.config = self.config_module.MaiFuturesConfig()
        self.config.decision.auto_trade = True
        self.config.decision.interval_minutes = 30
        self.config.decision.min_interval_minutes = 10
        self.config.decision.max_interval_minutes = 90
        self.config.market.quote_poll_seconds = 10
        self.config.decision.warmup_samples = 5
        self.notify = Recorder()
        self._engines = []

    async def asyncTearDown(self):
        for engine in self._engines:
            try:
                await engine.stop()
            except Exception:  # noqa: BLE001
                pass
        self.ledger.close()
        self._tmp.cleanup()

    def make_engine(self, source, decider=None, clock=None, symbols=None):
        engine = self.scheduler.TradingEngine(
            config=self.config,
            specs={k: v for k, v in self.specs.items() if symbols is None or k in symbols},
            ledger=self.ledger,
            quote_source=source,
            sample_book=self.bars.SampleBook(),
            decision_engine=decider or FakeDecider(None),
            notify=self.notify,
            logger=logging.getLogger("test.p1"),
            clock=clock or (lambda: TODAY),
        )
        self._engines.append(engine)
        return engine

    def seed_samples(self, engine, symbol="RB2701", prices=None):
        for index, price in enumerate(prices or [3000.0, 3002.0, 3001.0, 3003.0, 3002.0, 3004.0]):
            engine._samples.add(
                make_quote(symbol=symbol, now=TODAY + timedelta(minutes=index), last=price, age_seconds=0)
            )

    def open_position(self, side="long", price=3000.0, lots=1, symbol="RB2701", stop=2950.0, take=3100.0):
        spec = self.specs[symbol]
        return self.ledger.try_open(
            order_id=f"setup-{symbol}-{side}",
            decision_id=f"setup-{symbol}-{side}",
            ts=TODAY.isoformat(timespec="seconds"),
            spec=spec,
            side=side,
            lots=lots,
            price=price,
            stop_price=stop,
            take_price=take,
        )


class CloseActionMappingTest(BaseHarness):
    async def test_llm_close_long_is_executed(self):
        self.open_position(side="long")
        quote = make_quote(now=TODAY, age_seconds=1, last=3010.0, bid1=3009.0, ask1=3010.0)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote}), make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(
            self.decision_module.DecisionText(action="close", symbol="RB2701", lots=1, reason="止盈", speech="")
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)
        await engine.tick(TODAY)
        task = engine._decision_task
        self.assertIsNotNone(task)
        await task
        self.assertIsNone(self.ledger.position("RB2701"))
        trades = self.ledger.recent_trades(5)
        closes = [trade for trade in trades if trade.action == "close"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].side, "long")
        self.assertEqual(self.ledger.recent_decisions(5)[0].outcome, "closed")

    async def test_llm_close_short_is_executed(self):
        self.open_position(side="short")
        quote = make_quote(now=TODAY, age_seconds=1, last=2990.0, bid1=2989.0, ask1=2990.0)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote}), make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(
            self.decision_module.DecisionText(action="close", symbol="RB2701", lots=1, reason="回补", speech="")
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)
        await engine.tick(TODAY)
        await engine._decision_task
        self.assertIsNone(self.ledger.position("RB2701"))
        closes = [trade for trade in self.ledger.recent_trades(5) if trade.action == "close"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].side, "short")


class PauseResetRaceTest(BaseHarness):
    async def test_pause_during_model_wait_prevents_trade(self):
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource(
            [
                make_batch(TODAY, {"RB2701": quote}),
                make_batch(TODAY, {"RB2701": quote}),
            ]
        )
        decider = SlowDecider(
            self.decision_module.DecisionText(action="open_long", symbol="RB2701", lots=1, reason="追", speech="")
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)
        await engine.tick(TODAY)  # 启动后台决策任务后立即返回
        await asyncio.wait_for(decider.started.wait(), timeout=2)
        engine.set_paused(True)  # 模型返回前暂停：必须作废在途决策
        decider.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        self.assertIsNone(engine._decision_task)
        self.assertIsNone(self.ledger.position("RB2701"))
        self.assertEqual(self.ledger.account().trades_today, 0)

    async def test_reset_during_model_wait_prevents_trade(self):
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote}), make_batch(TODAY, {"RB2701": quote})])
        decider = SlowDecider(
            self.decision_module.DecisionText(action="open_long", symbol="RB2701", lots=1, reason="追", speech="")
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)
        await engine.tick(TODAY)
        await asyncio.wait_for(decider.started.wait(), timeout=2)
        self.ledger.reset(1_000_000.0, day="2026-11-03")
        engine.reset_runtime_state()
        decider.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        self.assertIsNone(self.ledger.position("RB2701"))
        self.assertEqual(self.ledger.account().trades_today, 0)


class PollingNotBlockedTest(BaseHarness):
    async def test_stop_take_still_runs_while_model_thinks(self):
        self.open_position(side="long", price=3000.0, stop=2950.0)
        quote = make_quote(now=TODAY, age_seconds=1, last=3000.0, bid1=2999.0, ask1=3000.0)
        crash = make_quote(now=TODAY + timedelta(seconds=11), age_seconds=1, last=2940.0, bid1=2939.0, ask1=2940.0)
        source = FakeSource(
            [
                make_batch(TODAY, {"RB2701": quote}),
                make_batch(TODAY + timedelta(seconds=11), {"RB2701": crash}),
                make_batch(TODAY + timedelta(seconds=11), {"RB2701": crash}),
            ]
        )
        decider = SlowDecider(
            self.decision_module.DecisionText(action="hold", symbol="", lots=1, reason="观望", speech="")
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)
        await engine.tick(TODAY)  # 决策任务开始并等待模型
        await asyncio.wait_for(decider.started.wait(), timeout=2)
        self.assertIsNotNone(self.ledger.position("RB2701"))
        await engine.tick(TODAY + timedelta(seconds=11))  # 模型仍在等待，止损巡检必须生效
        self.assertIsNone(self.ledger.position("RB2701"))
        self.assertTrue(any("止损" in message for message in self.notify.messages))
        decider.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0.1)


class ValuationTest(BaseHarness):
    async def test_missing_quote_blocks_new_risk_and_keeps_last_valuation(self):
        self.open_position(side="long", price=3000.0)
        engine = self.make_engine(FakeSource([]))
        # 从未收到该合约行情：估值不完整
        risk_context = engine._risk_context(
            TODAY,
            "RB2701",
            self.decision_module.DecisionInput(
                now=TODAY,
                symbols=["RB2701"],
                quotes={},
                positions=self.ledger.positions(),
                account=self.ledger.account(),
                equity=self.ledger.equity({}),
                atr={},
                recent_trades=[],
                sample_counts={"RB2701": 4},
                max_trades_today=6,
                max_margin_ratio=0.3,
            ),
            self.ledger.equity({}),
            self.specs["RB2701"],
        )
        self.assertFalse(risk_context.valuation_ok)
        verdict = self.risk.check_open(
            symbol="RB2701",
            lots=1,
            spec=self.specs["RB2701"],
            context=risk_context,
            config=self.config.risk,
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("估值不完整", verdict.reason)
        # 有最后有效价时按该价估值，不把浮亏当 0
        engine.last_quotes["RB2701"] = make_quote(now=TODAY, age_seconds=1, last=2900.0)
        equity = self.ledger.equity(engine._valuation_marks()[0])
        self.assertAlmostEqual(equity.floating, -1000.0)
        self.assertTrue(engine._valuation_marks()[1])


class ConfigSwitchTest(BaseHarness):
    async def test_llm_disabled_skips_decisions(self):
        self.config.decision.llm_enabled = False
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(
            self.decision_module.DecisionText(action="open_long", symbol="RB2701", lots=1, reason="", speech="")
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)
        await engine.tick(TODAY)
        self.assertEqual(decider.calls, 0)
        self.assertIsNone(engine._decision_task)

    def test_allow_short_false_denies_short(self):
        context = self.risk.RiskContext(
            account=self.ledger.account(),
            positions=[],
            equity=1_000_000.0,
            margin_used=0.0,
            reference_price=3000.0,
            now=TODAY,
            session_open=True,
            atr=10.0,
            today=TODAY.date(),
            valuation_ok=True,
            settlement_pending=False,
        )
        verdict = self.risk.check_open(
            symbol="RB2701",
            lots=1,
            spec=self.specs["RB2701"],
            context=context,
            config=self.config.risk,
            side="short",
            allow_short=False,
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("禁止做空", verdict.reason)

    def test_warmup_samples_enforced(self):
        context = self.risk.RiskContext(
            account=self.ledger.account(),
            positions=[],
            equity=1_000_000.0,
            margin_used=0.0,
            reference_price=3000.0,
            now=TODAY,
            session_open=True,
            atr=10.0,
            today=TODAY.date(),
            sample_count=2,
            required_samples=20,
            valuation_ok=True,
            settlement_pending=False,
        )
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.specs["RB2701"], context=context, config=self.config.risk
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("预热样本不足", verdict.reason)

    async def test_event_reports_false_silences_open_report(self):
        self.config.notify.event_reports = False
        quote = make_quote(now=TODAY, age_seconds=1, last=3007.0, bid1=3006.0, ask1=3007.0)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote}), make_batch(TODAY, {"RB2701": quote})])
        decider = FakeDecider(
            self.decision_module.DecisionText(action="open_long", symbol="RB2701", lots=1, reason="", speech="")
        )
        engine = self.make_engine(source, decider)
        self.seed_samples(engine)
        await engine.tick(TODAY)
        await engine._decision_task
        self.assertIsNotNone(self.ledger.position("RB2701"))
        self.assertEqual(self.notify.messages, [])


class SettlementPendingTest(BaseHarness):
    async def test_missing_settle_price_marks_pending_blocks_open_and_retries(self):
        self.open_position(side="long", price=3000.0)
        after_close = TODAY.replace(hour=15, minute=11)
        missing = make_quote(now=after_close, age_seconds=5, last=3050.0, settle=0.0)
        engine = self.make_engine(FakeSource([make_batch(after_close, {"RB2701": missing})]), clock=lambda: after_close)
        await engine._settlement_tick(after_close)
        self.assertTrue(engine._settlement_pending())
        self.assertTrue(any("结算价" in message for message in self.notify.messages))
        # 待结算期间：开仓被拒（用交易时段内的上下文，优先命中结算 pending 规则）
        context = self.risk.RiskContext(
            account=self.ledger.account(),
            positions=self.ledger.positions(),
            equity=self.ledger.equity({}),
            margin_used=0.0,
            reference_price=3050.0,
            now=TODAY,
            session_open=True,
            atr=10.0,
            today=TODAY.date(),
            sample_count=10,
            valuation_ok=True,
            settlement_pending=True,
        )
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.specs["RB2701"], context=context, config=self.config.risk
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("未完成的每日结算", verdict.reason)
        # 重试成功：持仓成本基准更新为结算价、pending 清除
        retry_time = after_close + timedelta(seconds=301)
        with_settle = make_quote(now=retry_time, age_seconds=5, last=3050.0, settle=3050.0)
        engine._source = FakeSource([make_batch(retry_time, {"RB2701": with_settle})])
        engine._last_poll_at = None
        await engine._settlement_tick(retry_time)
        self.assertFalse(engine._settlement_pending())
        self.assertAlmostEqual(self.ledger.position("RB2701").entry_price, 3050.0)
        self.assertEqual(len(self.ledger.settlements_for("2026-11-03")), 1)


class ExpiryTest(BaseHarness):
    def test_expiry_block_days_denies_open(self):
        spec = replace(self.specs["RB2701"], last_trade_date="2026-11-05")  # 周四；周二剩 3 个交易日
        context = self.risk.RiskContext(
            account=self.ledger.account(),
            positions=[],
            equity=1_000_000.0,
            margin_used=0.0,
            reference_price=3000.0,
            now=TODAY,
            session_open=True,
            atr=10.0,
            today=date(2026, 11, 3),
            valuation_ok=True,
            settlement_pending=False,
        )
        verdict = self.risk.check_open(symbol="RB2701", lots=1, spec=spec, context=context, config=self.config.risk)
        self.assertFalse(verdict.allowed)
        self.assertIn("距最后交易日", verdict.reason)

    async def test_last_trading_day_forces_close(self):
        spec = replace(self.specs["RB2701"], last_trade_date="2026-11-03")
        self.specs["RB2701"] = spec
        self.open_position(side="long", price=3000.0)
        quote = make_quote(now=TODAY, age_seconds=1, last=3005.0, bid1=3004.0, ask1=3005.0)
        source = FakeSource([make_batch(TODAY, {"RB2701": quote})])
        engine = self.make_engine(source, FakeDecider(None))
        await engine.tick(TODAY)
        self.assertIsNone(self.ledger.position("RB2701"))
        self.assertTrue(any("到期强平" in message for message in self.notify.messages))


class QuoteSafetyTest(BaseHarness):
    def test_future_quote_is_not_fresh(self):
        future = make_quote(now=TODAY, age_seconds=-300)
        self.assertLess(future.age_seconds(TODAY), 0)
        self.assertFalse(self.quotes.is_quote_fresh(future, TODAY, 180))

    def test_nan_price_is_rejected(self):
        line = __import__("tests.support", fromlist=["make_commodity_line"]).make_commodity_line(last="nan")
        with self.assertRaises(self.quotes.QuoteParseError):
            self.quotes.parse_quote_line(line)

    def test_zero_volume_book_is_not_tradable(self):
        quote = replace(make_quote(now=TODAY, age_seconds=1), bid1_volume=0)
        spec = self.specs["RB2701"]
        reason = submodule("execution").check_fill_ready(
            quote=quote,
            t0_mid=quote.mid,
            now=TODAY,
            spec=spec,
            max_quote_age_seconds=10,
            max_drift_ticks=5,
        )
        self.assertIn("无挂单量", reason)


class LedgerPrecisionTest(BaseHarness):
    async def test_settlement_cost_basis_and_total_pnl(self):
        spec = self.specs["RB2701"]
        trade = self.ledger.try_open(
            order_id="o1",
            decision_id="d1",
            ts=TODAY.isoformat(timespec="seconds"),
            spec=spec,
            side="long",
            lots=1,
            price=3000.0,
            stop_price=2950.0,
            take_price=3100.0,
        )
        open_fee = trade.fee
        self.ledger.settle(
            settle_date="2026-11-03",
            ts=TODAY.replace(hour=15, minute=10).isoformat(timespec="seconds"),
            settle_prices={"RB2701": 3050.0},
        )
        position = self.ledger.position("RB2701")
        self.assertEqual(position.open_price, 3000.0)
        self.assertEqual(position.entry_price, 3050.0)
        close = self.ledger.try_close(
            order_id="o2",
            decision_id="d2",
            ts=TODAY.replace(hour=15, minute=20).isoformat(timespec="seconds"),
            spec=spec,
            price=3100.0,
        )
        expected_total = (3100.0 - 3000.0) * spec.multiplier - open_fee - close.fee
        self.assertAlmostEqual(close.total_pnl, expected_total)
        account = self.ledger.account()
        self.assertAlmostEqual(account.cash, account.initial_balance + account.realized_pnl - account.fees_paid)
        self.assertEqual(self.ledger.check_accounting(), [])

    async def test_schema_v1_migrates_to_v2(self):
        # 模拟旧库：删掉新增列并把 schema_version 置回 1
        self.ledger._conn.execute("ALTER TABLE positions DROP COLUMN open_price")
        self.ledger._conn.execute("ALTER TABLE positions DROP COLUMN open_fee")
        self.ledger._conn.execute("ALTER TABLE trades DROP COLUMN open_price")
        self.ledger._conn.execute("ALTER TABLE trades DROP COLUMN total_pnl")
        self.ledger._conn.execute("UPDATE account SET schema_version = 1")
        self.ledger._conn.commit()
        self.ledger.close()
        reopened = self.ledger_module.Ledger(Path(self._tmp.name) / "ledger.sqlite3")
        try:
            columns = {row["name"] for row in reopened._conn.execute("PRAGMA table_info(positions)")}
            self.assertIn("open_price", columns)
            self.assertIn("open_fee", columns)
            self.assertEqual(reopened.account().schema_version, 2)
        finally:
            reopened.close()
        self.ledger = reopened
