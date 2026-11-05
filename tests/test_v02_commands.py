"""v0.2 管理员权限与手动操作命令测试。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.support import submodule

CST = ZoneInfo("Asia/Shanghai")
TODAY = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)


class FakeEngine:
    def __init__(self):
        self.manual_calls = []
        self.paused = False

    async def manual_order(self, *, action, symbol, lots=1, operator=""):
        self.manual_calls.append({"action": action, "symbol": symbol, "lots": lots, "operator": operator})
        return "opened", f"{symbol} long @3008"

    def set_paused(self, value):
        self.paused = bool(value)

    def reset_runtime_state(self):
        pass


class FakeLedger:
    def reset(self, *args, **kwargs):
        pass


class CommandV02Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = submodule("commands")
        self.config_module = submodule("config")
        self.config = self.config_module.MaiFuturesConfig()
        self.engine = FakeEngine()
        self.services = self.commands.CommandServices(
            ledger=FakeLedger(),
            specs={},
            quote_source=None,
            engine=self.engine,
            config=self.config,
            now=lambda: TODAY,
        )

    async def _handle(self, text, *, platform="qq", user_id="123", is_local_operator=False):
        return await self.commands.handle_command(
            text,
            stream_id="s1",
            group_id="100000001",
            platform=platform,
            user_id=user_id,
            is_local_operator=is_local_operator,
            services=self.services,
        )

    def _set_admin(self, user_id="123"):
        self.config.admin.users = [f"qq:{user_id}"]

    async def test_admin_can_buy_with_default_lots(self):
        self._set_admin()
        result = await self._handle("/期货 买 RB2701")
        self.assertTrue(result.handled)
        self.assertIn("已执行", result.text)
        self.assertEqual(self.engine.manual_calls[0]["action"], "buy")
        self.assertEqual(self.engine.manual_calls[0]["symbol"], "RB2701")
        self.assertEqual(self.engine.manual_calls[0]["lots"], 1)
        self.assertEqual(self.engine.manual_calls[0]["operator"], "qq:123")

    async def test_command_without_space_is_accepted(self):
        self._set_admin()
        result = await self._handle("/期货 买RB2701")
        self.assertIn("已执行", result.text)
        self.assertEqual(self.engine.manual_calls[0]["action"], "buy")
        self.assertEqual(self.engine.manual_calls[0]["symbol"], "RB2701")

    async def test_admin_can_sell_with_lots(self):
        self._set_admin()
        result = await self._handle("/期货 卖出 RB2701 2")
        self.assertIn("已执行", result.text)
        self.assertEqual(self.engine.manual_calls[0]["action"], "sell")
        self.assertEqual(self.engine.manual_calls[0]["lots"], 2)

    async def test_admin_can_close(self):
        self._set_admin()
        result = await self._handle("/期货 平 RB2701")
        self.assertIn("已执行", result.text)
        self.assertEqual(self.engine.manual_calls[0]["action"], "close")

    async def test_non_admin_cannot_operate(self):
        self._set_admin("999")
        result = await self._handle("/期货 买 RB2701")
        self.assertIn("只有管理员", result.text)
        self.assertEqual(self.engine.manual_calls, [])


    async def test_pause_requires_admin(self):
        result = await self._handle("/期货 暂停")
        self.assertIn("只有管理员", result.text)
        self.assertFalse(self.engine.paused)

    async def test_no_auto_trade_command(self):
        self._set_admin()
        result = await self._handle("/期货 自动 开")
        self.assertIn("未知子命令", result.text)

    async def test_help_mentions_admin_and_manual_commands(self):
        result = await self._handle("/期货 帮助")
        self.assertIn("管理员", result.text)
        self.assertIn("买", result.text)
        self.assertIn("自动交易开关在插件设置页", result.text)


if __name__ == "__main__":
    unittest.main()
