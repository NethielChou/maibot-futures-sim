"""插件级集成测试：fake PluginContext 驱动加载/命令/配置热更新/卸载。

Codex 评审要求：整个用例的加载、并发操作、断言和卸载必须处于**同一个事件循环**
（原实现多次 ``asyncio.run()``，每次退出都会取消剩余任务，卸载断言无法证明逻辑有效）。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import load_package, submodule


class PluginIntegrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_package()
        cls.scheduler = submodule("scheduler")

    def setUp(self):
        from maibot_sdk.context import PluginContext, PluginPaths

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.sent = []
        self.forward_args = []
        self.llm_calls = []
        self.rpc_calls = []
        self.run_forever_calls = 0
        self.loaded = False

        async def fake_rpc(method, plugin_id, payload, timeout_ms=None):
            capability = payload.get("capability", "")
            args = payload.get("args", {})
            self.rpc_calls.append(capability)
            if capability == "config.get":
                return {"success": True, "value": "测试人设：你是一个有主见的麦麦"}
            if capability == "chat.get_stream_by_group_id":
                return {
                    "success": True,
                    "stream": {"stream_id": "stream-1", "session_id": "stream-1", "group_id": args.get("group_id")},
                }
            if capability == "send.text":
                self.sent.append((args.get("stream_id"), args.get("text"), bool(args.get("storage_message", True))))
                return {"success": True, "sent": True, "message_id": "m1"}
            if capability == "send.forward":
                self.forward_args.append(args)
                texts = []
                for message in args.get("messages") or []:
                    for segment in message.get("segments") or []:
                        if segment.get("type") == "text":
                            texts.append(str(segment.get("content") or ""))
                self.sent.append(
                    (
                        args.get("stream_id"),
                        "\n".join(texts),
                        bool(args.get("storage_message", True)),
                    )
                )
                return {"success": True, "sent": True, "message_id": "f1"}
            if capability == "llm.generate":
                self.llm_calls.append(args)
                return {"success": True, "response": '{"action":"hold","symbol":"","lots":1,"reason":"x","speech":""}'}
            return {"success": True}

        self.plugin = self.module.create_plugin()
        defaults = type(self.plugin).build_default_config()
        defaults["notify"]["group_ids"] = ["100000001"]
        defaults["decision"]["auto_trade"] = False
        self.plugin.set_plugin_config(defaults)
        self.ctx = PluginContext(
            plugin_id="github.nethielchou.mai-futures-sim",
            rpc_call=fake_rpc,
            paths=PluginPaths(data_dir=base / "data", runtime_dir=base / "temp"),
        )
        self.plugin._set_context(self.ctx)

        async def _noop_run_forever(engine_self):  # noqa: ANN001 - 测试替身
            self.run_forever_calls += 1
            await asyncio.sleep(3600)

        self._patch = mock.patch.object(self.scheduler.TradingEngine, "run_forever", _noop_run_forever)
        self._patch.start()

    async def asyncTearDown(self):
        try:
            if self.loaded:
                await self.plugin.on_unload()
        finally:
            self._patch.stop()
            self._tmp.cleanup()

    async def _load(self):
        await self.plugin.on_load()
        self.loaded = True

    async def test_load_and_command_in_same_loop(self):
        await self._load()
        self.assertEqual(self.plugin._allowed_streams, {"stream-1"})
        self.assertIsNotNone(self.plugin._engine)
        self.assertIsNotNone(self.plugin._task)
        result = await self.plugin.handle_futures(
            stream_id="stream-1",
            text="/期货 状态",
            group_id="100000001",
            platform="qq",
            user_id="u1",
            is_local_operator=False,
        )
        self.assertTrue(result[0])
        self.assertTrue(any("权益" in text for _, text, _ in self.sent))
        self.assertTrue(any("合约参数为近似值" in text for _, text, _ in self.sent))
        # 插件消息必须不写入宿主消息库（避免长期记忆写回）
        self.assertTrue(all(storage is False for _, _, storage in self.sent))

    async def test_dual_market_status(self):
        defaults = type(self.plugin).build_default_config()
        defaults["notify"]["group_ids"] = ["100000001"]
        defaults["decision"]["auto_trade"] = False
        defaults["market"]["markets"] = ["cn", "intl"]
        self.plugin.set_plugin_config(defaults)
        await self._load()
        self.assertEqual(set(self.plugin._runtimes), {"cn", "intl"})
        self.assertEqual(self.plugin._runtimes["cn"].profile.currency, "麦麦币")
        self.assertEqual(self.plugin._runtimes["intl"].profile.currency, "麦麦币")
        self.sent.clear()
        await self.plugin.handle_futures(
            stream_id="stream-1",
            text="/期货 状态",
            group_id="100000001",
            platform="qq",
            user_id="u1",
            is_local_operator=False,
        )
        text = "\n".join(item[1] for item in self.sent)
        self.assertIn("国内商品期货", text)
        self.assertIn("国际市场", text)
        self.assertIn("麦麦币", text)

    async def test_messages_are_coalesced_into_one_forward(self):
        await self._load()
        self.sent.clear()
        await self.plugin._send_text("第一段", stream_id="stream-1")
        await self.plugin._send_text("第二段", stream_id="stream-1")
        await self.plugin._flush_outbox()
        self.assertEqual(len(self.sent), 1)
        stream_id, text, storage = self.sent[0]
        self.assertEqual(stream_id, "stream-1")
        self.assertIn("第一段", text)
        self.assertIn("第二段", text)
        self.assertFalse(storage)
        self.assertEqual(self.forward_args[-1].get("processed_plain_text"), "")

    async def test_readonly_tool_returns_local_ledger(self):
        await self._load()
        result = await self.plugin.tool_futures_snapshot(view="status", group_id="100000001", user_id="u1")
        self.assertTrue(result["success"])
        self.assertIn("权益", result["content"])
        denied = await self.plugin.tool_futures_snapshot(view="status", group_id="999999999", user_id="u1")
        self.assertFalse(denied["success"])

    async def test_persona_loaded_from_host(self):
        await self._load()
        self.assertIn("config.get", self.rpc_calls)
        self.assertEqual(self.plugin._engine._persona, "测试人设：你是一个有主见的麦麦")

    async def test_command_ignored_outside_allowed_group(self):
        await self._load()
        self.sent.clear()
        await self.plugin.handle_futures(
            stream_id="stream-1",
            text="/期货 状态",
            group_id="999999999",
            platform="qq",
            user_id="u1",
            is_local_operator=False,
        )
        self.assertEqual(self.sent, [])

    async def test_unload_cancels_loop_in_same_loop(self):
        await self._load()
        task = self.plugin._task
        engine = self.plugin._engine
        self.assertIsNotNone(task)
        await self.plugin.on_unload()
        self.loaded = False
        self.assertIsNone(self.plugin._task)
        self.assertIsNone(self.plugin._engine)
        self.assertTrue(task.cancelled())
        # 卸载后引擎不能再写账本/决策
        self.assertTrue(engine._stopped)

    async def test_config_update_keeps_exactly_one_live_executor(self):
        await self._load()
        await asyncio.sleep(0)  # 让后台循环任务真正开始执行
        first_task = self.plugin._task
        first_engine = self.plugin._engine
        self.assertEqual(self.run_forever_calls, 1)
        await self.plugin.on_config_update("self", {}, "0.1.1")
        await asyncio.sleep(0)
        second_task = self.plugin._task
        second_engine = self.plugin._engine
        self.assertIsNotNone(second_task)
        self.assertIsNot(first_engine, second_engine)
        self.assertTrue(first_task.cancelled())
        self.assertFalse(second_task.cancelled())
        # 只有一个有效的后台循环任务
        active = [task for task in (first_task, second_task) if not task.done()]
        self.assertEqual(len(active), 1)
        self.assertIs(active[0], second_task)
        # 旧引擎已停止，不能继续产生决策副作用
        self.assertTrue(first_engine._stopped)


if __name__ == "__main__":
    unittest.main()
