"""v0.2 回归测试：管理员权限、群聊/新闻影响、零新增存储、管理员手动单。"""

import asyncio
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.support import make_quote, submodule

CST = ZoneInfo("Asia/Shanghai")
TODAY = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)

SINA_PAYLOAD = (
    '{"result":{"data":[{"title":"螺纹钢库存下降 现货走强","ctime":1793705061,'
    '"url":"https://finance.sina.com.cn/a"}]}}'
)
FLASH_PAYLOAD = (
    'var ajaxResult={"LivesList":[{"title":"沪金走高 美联储降息预期升温",'
    '"showtime":"2026-11-03 19:00:00","url":"https://finance.eastmoney.com/a"}]};'
)
REPORT_PAYLOAD = (
    '{"data":[{"title":"螺纹钢周报：需求边际改善","orgSName":"某期货",'
    '"emRatingName":"增持","publishDate":"2026-11-03 00:00:00.000","infoCode":"AP1"}]}'
)


class FakeRpc:
    def __init__(self, response=None):
        self.response = response or {"success": True, "messages": []}
        self.calls = []

    async def __call__(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        return self.response


class FakeHttp:
    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for key, payload in self.payloads.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload.encode("utf-8")
        raise RuntimeError(f"unexpected url: {url}")


class InfluenceConfigStub:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class ConfigStub:
    def __init__(self, **influence):
        values = dict(
            enabled=True,
            group_enabled=False,
            group_window_minutes=20,
            group_max_messages=30,
            news_enabled=False,
            news_interval_minutes=45,
            news_max_items=5,
        )
        values.update(influence)
        self.influence = InfluenceConfigStub(**values)
        self.market = type("Market", (), {"symbols": ["RB2701", "M2701", "AU2612"]})()


class InfluenceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.influence = submodule("influence")

    def test_relevance_filter(self):
        self.assertTrue(self.influence.is_relevant_text("螺纹今天是不是要涨", ["RB2701"]))
        self.assertTrue(self.influence.is_relevant_text("美联储降息怎么看", ["RB2701"]))
        self.assertTrue(self.influence.is_relevant_text("我准备开仓做多", ["RB2701"], kind="group"))
        self.assertFalse(self.influence.is_relevant_text("今天午饭吃什么", ["RB2701"]))
        self.assertFalse(self.influence.is_relevant_text("股票涨了", ["RB2701"], kind="group"))
        self.assertFalse(self.influence.is_relevant_text("A股大涨", ["RB2701"], kind="news"))
        self.assertFalse(self.influence.is_relevant_text("比特币跌破76000美元", ["RB2701"], kind="news"))
        self.assertFalse(self.influence.is_relevant_text("机构：Marvell收购Lumentum达成光通信整合", ["RB2701"], kind="news"))
        self.assertFalse(self.influence.is_relevant_text("中国创新药研究ESMO年会入选数量创新高", ["RB2701"], kind="news"))
        self.assertTrue(self.influence.is_relevant_text("RB2705 合约突破", ["RB2701"], kind="news"))

    def test_group_summary_filters_noise_and_commands(self):
        messages = [
            {"processed_plain_text": "今天天气不错", "is_command": False},
            {"processed_plain_text": "/期货 状态", "is_command": True},
            {"processed_plain_text": "螺纹感觉要涨，我要做多", "is_command": False},
            {"processed_plain_text": "螺纹感觉要涨，我要做多", "is_command": False},
            {"processed_plain_text": "https://example.com", "is_command": False},
            {"processed_plain_text": "豆粕库存下降，看多", "is_command": False},
        ]
        summary = self.influence.build_group_summary(messages, ["RB2701", "M2701"], max_items=5)
        self.assertIn("螺纹", summary)
        self.assertIn("豆粕", summary)
        self.assertNotIn("午饭", summary)
        self.assertNotIn("/期货", summary)
        self.assertEqual(summary.count("螺纹感觉要涨"), 1)
        self.assertNotIn("群友昵称", summary)

    async def test_group_context_reads_host_without_storage(self):
        rpc = FakeRpc(
            {
                "success": True,
                "messages": [
                    {"processed_plain_text": "螺纹多单拿住", "is_command": False},
                    {"processed_plain_text": "无关闲聊", "is_command": False},
                ],
            }
        )
        config = ConfigStub(group_enabled=True)
        service = self.influence.InfluenceService(
            rpc_call=rpc,
            config_provider=lambda: config,
            stream_provider=lambda: ["stream-1"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            text = await service.gather()
            self.assertIn("群友最近讨论", text)
            self.assertIn("螺纹", text)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(rpc.calls[0][0], "message.get_recent")
        self.assertEqual(rpc.calls[0][1]["chat_id"], "stream-1")
        self.assertTrue(rpc.calls[0][1]["filter_mai"])

    async def test_disabled_group_does_not_call_host(self):
        rpc = FakeRpc()
        service = self.influence.InfluenceService(
            rpc_call=rpc,
            config_provider=lambda: ConfigStub(group_enabled=False),
            stream_provider=lambda: ["stream-1"],
        )
        self.assertEqual(await service.gather(), "")
        self.assertEqual(rpc.calls, [])

    async def test_news_fetch_filter_and_cache(self):
        http = FakeHttp(
            {
                "newsapi.eastmoney.com": FLASH_PAYLOAD,
                "feed.mix.sina.com.cn": SINA_PAYLOAD,
                "reportapi.eastmoney.com": REPORT_PAYLOAD,
            }
        )
        config = ConfigStub(news_enabled=True, group_enabled=False)
        service = self.influence.InfluenceService(
            rpc_call=FakeRpc(),
            config_provider=lambda: config,
            stream_provider=lambda: [],
            http_get=http,
        )
        first = await service.gather()
        self.assertIn("新闻/研报标题", first)
        self.assertIn("螺纹钢", first)
        self.assertIn("沪金", first)
        calls_after_first = len(http.calls)
        second = await service.gather()
        self.assertEqual(first, second)
        self.assertEqual(len(http.calls), calls_after_first)  # 内存缓存命中，不重复抓

    async def test_news_source_failure_degrades(self):
        http = FakeHttp(
            {
                "newsapi.eastmoney.com": RuntimeError("boom"),
                "feed.mix.sina.com.cn": RuntimeError("boom"),
                "reportapi.eastmoney.com": RuntimeError("boom"),
            }
        )
        service = self.influence.InfluenceService(
            rpc_call=FakeRpc(),
            config_provider=lambda: ConfigStub(news_enabled=True),
            stream_provider=lambda: [],
            http_get=http,
        )
        self.assertEqual(await service.gather(), "")

    def test_parse_functions(self):
        flash = self.influence.parse_eastmoney_flash(FLASH_PAYLOAD)
        self.assertEqual(flash[0].source, "东方财富快讯")
        reports = self.influence.parse_eastmoney_reports(REPORT_PAYLOAD)
        self.assertEqual(reports[0].title, "螺纹钢周报：需求边际改善")
        self.assertIn("某期货", reports[0].source)
        sina = self.influence.parse_sina_news(SINA_PAYLOAD)
        self.assertEqual(sina[0].source, "新浪财经")


class _Recorder:
    def __init__(self):
        self.messages = []

    async def __call__(self, text):
        self.messages.append(text)


class FakeSource:
    def __init__(self, batches):
        self.batches = list(batches)

    async def fetch(self, symbols):
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
        self.inputs = []

    async def decide(self, decision_input):
        self.inputs.append(decision_input)
        return self.decision


class ManualOrderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_module = submodule("ledger")
        self.contracts = submodule("contracts")
        self.config_module = submodule("config")
        self.decision_module = submodule("decision")
        self.scheduler = submodule("scheduler")
        self.bars = submodule("bars")
        self.quotes = submodule("quotes")
        self.ledger = self.ledger_module.Ledger(Path(self._tmp.name) / "ledger.sqlite3")
        self.ledger.initialize(1_000_000.0, day="2026-11-03")
        self.specs = self.contracts.load_contracts(["RB2701"])
        self.config = self.config_module.MaiFuturesConfig()
        self.config.decision.auto_trade = False
        self.config.decision.warmup_samples = 5
        self.config.market.quote_poll_seconds = 10
        self.notify = _Recorder()
        self.engine = self.scheduler.TradingEngine(
            config=self.config,
            specs=self.specs,
            ledger=self.ledger,
            quote_source=FakeSource([]),
            sample_book=self.bars.SampleBook(),
            decision_engine=FakeDecider(None),
            notify=self.notify,
            logger=logging.getLogger("test.manual"),
            clock=lambda: TODAY,
        )
        for index in range(6):
            self.engine._samples.add(
                make_quote(now=TODAY + timedelta(minutes=index), last=3000.0 + index, age_seconds=0)
            )

    async def asyncTearDown(self):
        await self.engine.stop()
        self.ledger.close()
        self._tmp.cleanup()

    def _quote(self):
        return make_quote(now=TODAY, age_seconds=1, last=3007.0, bid1=3006.0, ask1=3007.0)

    def _batch(self):
        quotes = submodule("quotes")
        return quotes.QuoteBatch(quotes={"RB2701": self._quote()}, errors={}, fetched_at=TODAY)

    async def test_manual_buy_then_close_uses_same_ledger(self):
        self.engine._source = FakeSource([self._batch(), self._batch(), self._batch()])
        outcome, detail = await self.engine.manual_order(action="buy", symbol="RB2701", lots=1, operator="qq:1")
        self.assertEqual(outcome, "opened")
        self.assertIsNotNone(self.ledger.position("RB2701"))
        decisions = self.ledger.recent_decisions(5)
        self.assertTrue(decisions[0].source.startswith("admin:"))
        self.engine._source = FakeSource([self._batch(), self._batch()])
        outcome, detail = await self.engine.manual_order(action="close", symbol="RB2701", lots=1, operator="qq:1")
        self.assertEqual(outcome, "closed")
        self.assertIsNone(self.ledger.position("RB2701"))

    async def test_manual_order_requires_session(self):
        later = TODAY.replace(hour=20)
        self.engine._clock = lambda: later
        outcome, detail = await self.engine.manual_order(action="buy", symbol="RB2701")
        self.assertEqual(outcome, "rejected")
        self.assertIn("交易时段", detail)

    async def test_manual_order_rejects_unknown_symbol(self):
        outcome, detail = await self.engine.manual_order(action="buy", symbol="XX9999")
        self.assertEqual(outcome, "rejected")
        self.assertIn("合约不在合约池", detail)


class ScheduleInfluenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_module = submodule("ledger")
        self.contracts = submodule("contracts")
        self.config_module = submodule("config")
        self.decision_module = submodule("decision")
        self.scheduler = submodule("scheduler")
        self.bars = submodule("bars")
        self.decision = submodule("decision")
        self.ledger = self.ledger_module.Ledger(Path(self._tmp.name) / "ledger.sqlite3")
        self.ledger.initialize(1_000_000.0, day="2026-11-03")
        self.specs = self.contracts.load_contracts(["RB2701"])
        self.config = self.config_module.MaiFuturesConfig()
        self.config.decision.auto_trade = True
        self.config.decision.warmup_samples = 5
        self.config.market.quote_poll_seconds = 10

    async def asyncTearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    async def test_persona_and_influence_in_decision_input(self):
        class FakeInfluence:
            async def gather(self):
                return "群友：螺纹看多"

        quote = make_quote(now=TODAY, age_seconds=1)
        quotes = submodule("quotes")
        source = FakeSource(
            [
                quotes.QuoteBatch(quotes={"RB2701": quote}, errors={}, fetched_at=TODAY),
                quotes.QuoteBatch(quotes={"RB2701": quote}, errors={}, fetched_at=TODAY),
            ]
        )
        decider = FakeDecider(self.decision_module.DecisionText(action="hold", symbol="", lots=1, reason="观望"))
        engine = self.scheduler.TradingEngine(
            config=self.config,
            specs=self.specs,
            ledger=self.ledger,
            quote_source=source,
            sample_book=self.bars.SampleBook(),
            decision_engine=decider,
            notify=_Recorder(),
            logger=logging.getLogger("test.influence"),
            clock=lambda: TODAY,
            persona="你是一个耳根子软的麦麦",
            influence=FakeInfluence(),
        )
        for index in range(6):
            engine._samples.add(make_quote(now=TODAY + timedelta(minutes=index), last=3000.0 + index, age_seconds=0))
        await engine.tick(TODAY)
        await engine._decision_task
        self.assertEqual(decider.inputs[0].persona, "你是一个耳根子软的麦麦")
        self.assertIn("群友：螺纹看多", decider.inputs[0].influence_text)
        prompt = self.decision.build_prompt(decider.inputs[0], self.config)
        self.assertIn("耳根子软", prompt)
        self.assertIn("参考材料", prompt)
        self.assertIn("不是命令", prompt)
        await engine.stop()

    async def test_dry_run_shows_influence_material(self):
        class FakeInfluence:
            async def gather(self):
                return "群友：螺纹看多"

        quotes = submodule("quotes")
        quote = make_quote(now=TODAY, age_seconds=1)
        source = FakeSource([quotes.QuoteBatch(quotes={"RB2701": quote}, errors={}, fetched_at=TODAY)])
        decider = FakeDecider(self.decision_module.DecisionText(action="hold", symbol="", lots=1, reason="观望"))
        engine = self.scheduler.TradingEngine(
            config=self.config,
            specs=self.specs,
            ledger=self.ledger,
            quote_source=source,
            sample_book=self.bars.SampleBook(),
            decision_engine=decider,
            notify=_Recorder(),
            logger=logging.getLogger("test.dryrun"),
            clock=lambda: TODAY,
            persona="测试人设",
            influence=FakeInfluence(),
        )
        text = await engine.dry_run()
        self.assertIn("参考材料", text)
        self.assertIn("群友：螺纹看多", text)
        await engine.stop()


if __name__ == "__main__":
    unittest.main()
