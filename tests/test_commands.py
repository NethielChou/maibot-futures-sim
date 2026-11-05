"""命令解析/权限/回复测试（假行情源，不访问网络）。"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.support import make_quote, submodule

CST = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)


class FakeSource:
    def __init__(self, quote):
        self.quote = quote

    async def fetch(self, symbols):
        quotes = submodule("quotes")
        quote = self.quote
        return quotes.QuoteBatch(quotes={quote.symbol: quote}, errors={}, fetched_at=NOW)


class FakeEngine:
    def __init__(self):
        self.paused = False
        self.reset_called = False
        self.marks = {}

    def latest_marks(self):
        return dict(self.marks)

    def set_paused(self, paused):
        self.paused = bool(paused)

    def reset_runtime_state(self):
        self.reset_called = True

    async def dry_run(self):
        return "🧪 试跑（未下单）：\n动作：hold"


class CommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = submodule("commands")
        cls.ledger_module = submodule("ledger")
        cls.contracts = submodule("contracts")
        cls.config_module = submodule("config")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = self.ledger_module.Ledger(Path(self._tmp.name) / "ledger.sqlite3")
        self.ledger.initialize(1_000_000.0, day="2026-11-03")
        self.specs = self.contracts.load_contracts(["RB2701"])
        self.config = self.config_module.MaiFuturesConfig()
        self.config.admin.users = ["qq:1"]
        self.engine = FakeEngine()
        self.quote = make_quote(now=NOW, age_seconds=1)
        self.services = self.commands.CommandServices(
            ledger=self.ledger,
            specs=self.specs,
            quote_source=FakeSource(self.quote),
            engine=self.engine,
            config=self.config,
            now=lambda: NOW,
        )

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def run_command(self, text, *, user_id="2", group_id="100000001", platform="qq", local=False):
        return asyncio.run(
            self.commands.handle_command(
                text,
                stream_id="stream-1",
                group_id=group_id,
                platform=platform,
                user_id=user_id,
                is_local_operator=local,
                services=self.services,
            )
        )

    def test_parse_command(self):
        self.assertEqual(self.commands.parse_command("/期货"), ("", ""))
        self.assertEqual(self.commands.parse_command("/期货 状态"), ("状态", ""))
        self.assertEqual(self.commands.parse_command("/期货 行情 RB2701"), ("行情", "RB2701"))
        self.assertEqual(self.commands.parse_command("/期货状态"), ("状态", ""))
        self.assertIsNone(self.commands.parse_command("/股 状态"))
        self.assertIsNone(self.commands.parse_command("你好"))

    def test_is_owner(self):
        self.assertTrue(self.commands.is_owner(owners=["qq:1"], platform="qq", user_id="1"))
        self.assertFalse(self.commands.is_owner(owners=["qq:1"], platform="qq", user_id="2"))
        self.assertTrue(self.commands.is_owner(owners=[], platform="", user_id="", is_local_operator=True))
        self.assertFalse(self.commands.is_owner(owners=[], platform="qq", user_id=""))

    def test_state(self):
        self.ledger.record_decision(
            decision_id="d1",
            ts="2026-11-03T10:00:00+08:00",
            symbol="RB2701",
            action="open_long",
            lots=1,
            reason="测试理由：螺纹看多",
            source="llm",
            outcome="opened",
        )
        result = self.run_command("/期货 状态")
        self.assertTrue(result.handled)
        self.assertIn("权益", result.text)
        self.assertIn("持仓", result.text)
        self.assertIn("最近决策", result.text)
        self.assertIn("测试理由：螺纹看多", result.text)

    def test_quote(self):
        result = self.run_command("/期货 行情")
        self.assertIn("行情快照", result.text)
        self.assertIn("螺纹钢", result.text)

    def test_report_outputs_have_disclaimer(self):
        report = submodule("report")
        samples = [
            report.format_help(),
            report.format_quotes([("RB2701", 3000.0, 2999.0, 3001.0, "15:00:00")]),
            report.format_trades([]),
            report.format_reject("测试拒绝"),
        ]
        for text in samples:
            self.assertIn("合约参数为近似值", text)
            self.assertIn("不构成投资建议", text)
            self.assertEqual(text.count(report.DISCLAIMER), 1)

    def test_quote_unknown_contract(self):
        result = self.run_command("/期货 行情 M9999")
        self.assertIn("不认识", result.text)

    def test_trades_empty(self):
        result = self.run_command("/期货 记录")
        self.assertIn("没有成交记录", result.text)

    def test_help(self):
        result = self.run_command("/期货 帮助")
        self.assertIn("麦麦期货模拟盘", result.text)
        self.assertIn("试跑", result.text)

    def test_non_command(self):
        result = self.run_command("你好")
        self.assertFalse(result.handled)

    def test_unknown_subcommand(self):
        result = self.run_command("/期货 不存在")
        self.assertIn("未知子命令", result.text)

    def test_pause_requires_owner(self):
        result = self.run_command("/期货 暂停", user_id="2")
        self.assertIn("只有管理员", result.text)
        self.assertFalse(self.engine.paused)
        result = self.run_command("/期货 暂停", user_id="1")
        self.assertIn("已暂停", result.text)
        self.assertTrue(self.engine.paused)
        result = self.run_command("/期货 恢复", user_id="1")
        self.assertIn("已恢复", result.text)
        self.assertFalse(self.engine.paused)

    def test_reset_requires_confirm(self):
        result = self.run_command("/期货 重置", user_id="1")
        self.assertIn("确认", result.text)
        self.assertAlmostEqual(self.ledger.account().cash, 1_000_000.0)
        result = self.run_command("/期货 重置 确认", user_id="1")
        self.assertIn("已重置", result.text)
        self.assertTrue(self.engine.reset_called)
        self.assertAlmostEqual(self.ledger.account().cash, 1_000_000.0)

    def test_dry_run_owner_only(self):
        result = self.run_command("/期货 试跑", user_id="2")
        self.assertIn("只有管理员", result.text)
        result = self.run_command("/期货 试跑", user_id="1")
        self.assertIn("试跑", result.text)

    def test_trades_after_open(self):
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
            take_price=3100.0,
        )
        result = self.run_command("/期货 记录")
        self.assertIn("最近成交", result.text)
        self.assertIn("螺纹钢", result.text)
        self.assertNotIn("RB2701", result.text)

    def test_invalid_record_limit(self):
        result = self.run_command("/期货 记录 abc")
        self.assertIn("整数", result.text)


if __name__ == "__main__":
    unittest.main()
