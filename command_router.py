"""多市场命令路由：汇总/按市场/按合约分发，避免全部堆在 plugin.py。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import commands as command_module, report, tools
from .commands import CommandServices

CST = ZoneInfo("Asia/Shanghai")


class CommandRouter:
    """把 ``/期货`` 命令分发到市场运行时。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def _services_for(self, runtime: Any) -> CommandServices:
        def resolve_symbol(token: str) -> str | None:
            text = str(token or "").strip()
            upper = text.upper()
            specs = runtime.engine.specs
            if upper in specs:
                return upper
            for symbol, spec in specs.items():
                if (spec.display_name or "") == text:
                    return symbol
            return None

        return CommandServices(
            ledger=runtime.ledger,
            specs=runtime.engine.specs,
            quote_source=runtime.quote_source,
            engine=runtime.engine,
            config=runtime.config,
            now=lambda: datetime.now(CST),
            market_label=runtime.profile.label,
            currency=runtime.profile.currency,
            resolve_symbol=resolve_symbol,
        )

    def _find_runtime_symbol(self, token: str) -> tuple[Any, str] | None:
        text = str(token or "").strip()
        if not text:
            return None
        upper = text.upper()
        for runtime in self.plugin._runtimes.values():
            if upper in runtime.engine.specs:
                return runtime, upper
        for runtime in self.plugin._runtimes.values():
            for symbol, spec in runtime.engine.specs.items():
                if (spec.display_name or "") == text:
                    return runtime, symbol
        return None

    async def _command_on(
        self,
        runtime: Any,
        text: str,
        *,
        stream_id: str,
        group_id: str,
        platform: str,
        user_id: str,
        is_local_operator: bool,
    ) -> str | None:
        result = await command_module.handle_command(
            text,
            stream_id=stream_id,
            group_id=group_id,
            platform=platform,
            user_id=user_id,
            is_local_operator=is_local_operator,
            services=self._services_for(runtime),
        )
        return result.text

    async def _send_combined(self, parts: list[str], *, stream_id: str) -> None:
        body = "\n\n".join(part for part in parts if part and part.strip())
        if body and stream_id:
            await self.plugin._send_text(report.append_disclaimer(body), stream_id=stream_id)

    async def handle(
        self,
        *,
        text: str,
        stream_id: str = "",
        group_id: str = "",
        platform: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
    ) -> tuple[bool, str, bool]:
        plugin = self.plugin
        if not plugin._command_allowed(
            group_id=group_id,
            platform=platform,
            user_id=user_id,
            is_local_operator=is_local_operator,
        ):
            plugin.ctx.logger.info(
                "忽略非授权聊天流的命令：group=%s user=%s", group_id, user_id
            )
            return False, "非授权聊天流", True
        if not plugin._runtimes:
            if stream_id:
                await plugin._send_text(
                    "插件尚未就绪（服务未初始化），请查看日志。", stream_id=stream_id
                )
            return False, "服务未就绪", True

        parsed = command_module.parse_command(text)
        if parsed is None:
            return False, "", True
        raw_sub, arg = parsed
        sub = command_module.resolve_sub(raw_sub)
        runtimes = list(plugin._runtimes.values())
        first = runtimes[0]
        arg_text = str(arg or "").strip()
        call_args = {
            "stream_id": stream_id,
            "group_id": group_id,
            "platform": platform,
            "user_id": user_id,
            "is_local_operator": is_local_operator,
        }

        if sub == "overview":
            content = await tools.market_overview(runtimes)
            if stream_id:
                await plugin._send_text(report.append_disclaimer(content), stream_id=stream_id)
            return True, "", True

        if sub in (None, "help"):
            reply = await self._command_on(first, text, **call_args)
            if reply and stream_id:
                await plugin._send_text(report.append_disclaimer(reply), stream_id=stream_id)
            return True, "", True

        if sub in {"pause", "resume", "reset"}:
            parts: list[str] = []
            targets = runtimes
            control_text = text
            if sub == "reset" and arg_text:
                tokens = arg_text.split()
                if tokens and tokens[0].lower() in plugin._runtimes:
                    targets = [plugin._runtimes[tokens[0].lower()]]
                    rest = " ".join(tokens[1:]).strip()
                    control_text = "/期货 重置" + (f" {rest}" if rest else "")
            for runtime in targets:
                reply = await self._command_on(runtime, control_text, **call_args)
                if not reply:
                    continue
                cleaned = report.strip_disclaimer(reply)
                # 每个市场的控制结果都要显示；不能按回复文本去重，否则只看到第一个市场。
                parts.append(f"【{runtime.profile.label}】\n{cleaned}")
            await self._send_combined(parts, stream_id=stream_id)
            return True, "", True

        if sub in {"buy", "sell", "close"}:
            found = self._find_runtime_symbol(
                arg_text.split()[0] if arg_text else ""
            )
            if found is None:
                available = "、".join(
                    f"{runtime.profile.label}:"
                    + ",".join(spec.display_name or symbol for symbol, spec in runtime.engine.specs.items())
                    for runtime in runtimes
                )
                reply = f"不认识的品种：{arg_text or '（空）'}。可用品种：{available}"
                if stream_id:
                    await plugin._send_text(report.append_disclaimer(reply), stream_id=stream_id)
                return True, "", True
            target, _ = found
            reply = await self._command_on(target, text, **call_args)
            if reply and stream_id:
                await plugin._send_text(report.append_disclaimer(reply), stream_id=stream_id)
            return True, "", True

        market_selector = arg_text.lower() if arg_text.lower() in plugin._runtimes else ""
        selected = runtimes
        text_for = text
        if sub == "quote" and arg_text and not market_selector:
            found = self._find_runtime_symbol(arg_text.split()[0])
            if found is not None:
                selected = [found[0]]
        if market_selector:
            selected = [plugin._runtimes[market_selector]]
            text_for = "/期货 " + (
                {"state": "状态", "quote": "行情", "trades": "记录", "dry": "试跑"}.get(
                    sub, raw_sub
                )
            )

        parts = []
        for runtime in selected:
            reply = await self._command_on(runtime, text_for, **call_args)
            if reply:
                parts.append(
                    f"【{runtime.profile.label}】\n{report.strip_disclaimer(reply)}"
                )
        await self._send_combined(parts, stream_id=stream_id)
        return True, "", True
