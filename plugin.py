"""麦麦期货模拟盘 — 主入口。

职责：
- 生命周期：on_load / on_unload / on_config_update；
- 组装服务：账本（SQLite）、行情源（新浪）、合约表、决策引擎、交易编排器；
- 后台交易循环（日盘采样、决策、止损止盈、收盘结算）；
- Command ``/期货``：查询/控制；控制类命令仅管理员；
- 播报：解析目标群/私聊聊天流后发送纯文本。

明确不做：真实交易通道、EventHandler、图片、自然语言触发。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from . import tools
from .bars import SampleBook
from .command_router import CommandRouter
from .config import CONFIG_VERSION, MaiFuturesConfig
from .contracts import MARKET_PROFILES, resolve_market_contracts
from .decision import CallBudget, LLMDecisionEngine
from .influence import InfluenceService
from .ledger import Ledger
from .official_data import OfficialDataService
from .quotes import SinaIntlQuoteSource, SinaQuoteSource
from .runtimes import MarketRuntime
from .scheduler import TradingEngine

CST = ZoneInfo("Asia/Shanghai")
PLUGIN_VERSION = "0.8.3"
_STREAM_REFRESH_INTERVAL_SECONDS = 60.0


class MaiFuturesSimPlugin(MaiBotPlugin):
    """麦麦期货模拟盘插件实例。"""

    config_model = MaiFuturesConfig

    def __init__(self) -> None:
        super().__init__()
        self._task: asyncio.Task[None] | None = None
        self._ledger: Ledger | None = None
        self._engine: TradingEngine | None = None
        self._runtimes: dict[str, MarketRuntime] = {}
        self._allowed_streams: set[str] = set()
        self._last_stream_refresh: float = 0.0
        self._setup_lock = asyncio.Lock()
        self._router = CommandRouter(self)
        self._official: OfficialDataService | None = None
        self._official_task: asyncio.Task[None] | None = None
        self._outbox: dict[str, list[str]] = {}
        self._outbox_task: asyncio.Task[None] | None = None
        self._outbox_lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        """加载：只做本地初始化与聊天流解析，不在 on_load 里拉行情。"""

        async with self._setup_lock:
            await self._stop_services()
            await self._start_services()
        self.ctx.logger.info(
            "麦麦期货模拟盘 %s 已加载（config_version=%s，市场=%s，自动交易=%s）",
            PLUGIN_VERSION,
            CONFIG_VERSION,
            ",".join(self._runtimes) or "-",
            "开启" if self.config.decision.auto_trade else "关闭",
        )

    async def on_unload(self) -> None:
        """卸载：取消后台循环、关闭账本。"""

        async with self._setup_lock:
            await self._stop_services()
        self.ctx.logger.info("麦麦期货模拟盘已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：重建服务（配置变化不需要代码热重载）。"""

        del config_data
        if scope != "self":
            return
        async with self._setup_lock:
            await self._stop_services()
            await self._start_services()
        self.ctx.logger.info("麦麦期货模拟盘配置已更新（version=%s），服务与循环已重建", version)

    # ---------- 服务装配 ----------

    def _normalized_markets(self) -> list[str]:
        aliases = {
            "cn": "cn",
            "domestic": "cn",
            "国内": "cn",
            "intl": "intl",
            "international": "intl",
            "国际": "intl",
        }
        result: list[str] = []
        for raw in self.config.market.markets or ["cn"]:
            key = aliases.get(str(raw).strip().lower(), str(raw).strip().lower())
            if key not in MARKET_PROFILES:
                raise ValueError(f"未知市场：{raw}（支持 {sorted(MARKET_PROFILES)}）")
            if key not in result:
                result.append(key)
        if not result:
            raise ValueError("未选择交易市场")
        return result

    async def _start_services(self) -> None:
        config = self.config
        today = datetime.now(CST).strftime("%Y-%m-%d")
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        self._official = OfficialDataService(
            data_dir,
            self.ctx.logger,
            enabled=config.official.enabled,
            settlement_enabled=config.official.settlement_enabled,
            timeout_seconds=config.official.timeout_seconds,
            cache_enabled=config.official.cache_enabled,
        )
        self._official_task = asyncio.create_task(
            self._official_sync_loop(), name="mai-futures-official-sync"
        )

        persona = await self._load_persona()
        influence = InfluenceService(
            rpc_call=self.ctx.call_capability,
            config_provider=lambda: self.config,
            stream_provider=lambda: sorted(self._allowed_streams),
            logger=self.ctx.logger,
        )

        runtimes: dict[str, MarketRuntime] = {}
        for market_id in self._normalized_markets():
            profile = MARKET_PROFILES[market_id]
            override_symbols = list(config.market.symbols) if market_id == "cn" else []
            specs = resolve_market_contracts([market_id], override_symbols)
            market_config = config.model_copy(deep=True)
            market_config.market.markets = [market_id]
            market_config.market.symbols = list(specs)
            market_config.market.sessions = list(
                profile.session_entries or config.market.sessions
            )
            market_config.market.night_enabled = (
                config.market.night_enabled
                if profile.night_enabled is None
                else profile.night_enabled
            )
            market_config.account.initial_balance = float(profile.initial_balance)
            market_config.account.settle_enabled = bool(profile.settle_enabled)
            market_config.account.settle_time = str(profile.settle_time)
            market_config.account.settle_use_last = bool(profile.settle_use_last)

            filename = "ledger.sqlite3" if market_id == "cn" else f"ledger_{market_id}.sqlite3"
            ledger = Ledger(data_dir / filename)
            ledger.initialize(profile.initial_balance, day=today)
            health_issues = ledger.health_check() + ledger.check_accounting()
            if health_issues:
                self.ctx.logger.error(
                    "[%s] 账本一致性检查发现异常（将暂停该市场自动交易）：%s",
                    profile.label,
                    health_issues,
                )

            budget = CallBudget(ledger, config.decision.max_llm_calls_per_day)
            decider = LLMDecisionEngine(
                generate=self._generate_llm,
                budget=budget,
                config=market_config,
                logger=self.ctx.logger,
            )
            if profile.quote_style == "hf":
                source: Any = SinaIntlQuoteSource(
                    timeout_seconds=config.market.quote_timeout_seconds
                )
            else:
                source = SinaQuoteSource(
                    timeout_seconds=config.market.quote_timeout_seconds
                )
            engine = TradingEngine(
                config=market_config,
                specs=specs,
                ledger=ledger,
                quote_source=source,
                sample_book=SampleBook(),
                decision_engine=decider,
                notify=lambda text, label=profile.label: self._send_market_text(label, text),
                logger=self.ctx.logger,
                persona=persona,
                influence=influence,
                session_entries=(
                    list(profile.session_entries)
                    if profile.session_entries is not None
                    else None
                ),
                night_enabled=profile.night_enabled,
                trading_day_mode=profile.trading_day,
                market_label=profile.label,
                currency=profile.currency,
                calendar_market=market_id,
                settlement_provider=(
                    (lambda day, symbols, mid=market_id: self._official.settlements(mid, day, symbols))
                    if self._official is not None
                    else None
                ),
            )
            if health_issues:
                engine.set_paused(True)
            runtime = MarketRuntime(
                market_id=market_id,
                profile=profile,
                config=market_config,
                ledger=ledger,
                engine=engine,
                specs=specs,
                quote_source=source,
            )
            runtime.task = asyncio.create_task(
                self._run_engine(runtime), name=f"mai-futures-{market_id}"
            )
            runtimes[market_id] = runtime

        self._runtimes = runtimes
        first = next(iter(runtimes.values()))
        self._ledger = first.ledger
        self._engine = first.engine
        self._task = first.task

        self._allowed_streams = await self._resolve_streams(config)
        self._last_stream_refresh = time.monotonic()
        if not self._allowed_streams:
            self.ctx.logger.warning(
                "未解析到任何播报聊天流（group_ids=%s），播报会静默；之后会定时重试",
                list(config.notify.group_ids),
            )
        for runtime in runtimes.values():
            if runtime.engine.paused:
                await self._send_text(
                    f"⚠️ [{runtime.profile.label}] 账本一致性检查异常，该市场自动交易已暂停，请检查日志。"
                )

    async def _load_persona(self) -> str:
        """从宿主读取麦麦人设；读不到时返回空串（prompt 使用默认称呼）。"""

        try:
            response = await self.ctx.call_capability(
                "config.get",
                key="personality.personality",
                default="",
            )
        except Exception as exc:  # noqa: BLE001 - 人设缺失不影响交易
            self.ctx.logger.warning("读取宿主人设失败：%s", exc)
            return ""
        # SDK 会把 config.get 的返回值归一化成 value 本身（字符串），也可能返回字典
        if isinstance(response, str):
            persona = response.strip()
        elif isinstance(response, dict):
            if response.get("success") is False:
                return ""
            persona = str(response.get("value") or "").strip()
        else:
            persona = ""
        if persona:
            self.ctx.logger.info("已读取宿主人设（%d 字）", len(persona))
        return persona

    async def _stop_services(self) -> None:
        await self._flush_outbox()
        official_task = self._official_task
        self._official_task = None
        if official_task is not None:
            official_task.cancel()
            try:
                await official_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("官方数据同步任务停止时异常：%s", exc)
        self._official = None
        runtimes = list(self._runtimes.values())
        self._runtimes = {}
        self._task = None
        self._engine = None
        self._ledger = None
        for runtime in runtimes:
            try:
                await runtime.engine.stop()
            except Exception as exc:  # noqa: BLE001 - 关闭阶段只记录
                self.ctx.logger.warning("[%s] 引擎停止时异常：%s", runtime.profile.label, exc)
            task = runtime.task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("[%s] 交易循环停止时异常：%s", runtime.profile.label, exc)
            runtime.ledger.close()
        self._allowed_streams = set()

    async def _run_engine(self, runtime: MarketRuntime) -> None:
        try:
            await runtime.engine.run_forever()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            self.ctx.logger.error(
                "[%s] 交易循环退出：%s", runtime.profile.label, exc, exc_info=True
            )

    async def _official_sync_loop(self) -> None:
        service = self._official
        if service is None:
            return
        try:
            await service.sync_calendar()
            while True:
                hours = max(1, int(self.config.official.calendar_sync_hours))
                await asyncio.sleep(hours * 3600)
                await service.sync_calendar()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 官方源失败不影响交易
            self.ctx.logger.warning("官方数据同步循环退出：%s", exc)

    async def _send_market_text(self, label: str, text: str) -> None:
        if text:
            await self._send_text(f"【{label}】\n{text}")

    # ---------- 聊天流 / 播报 ----------

    @staticmethod
    def _stream_id(stream: Any) -> str:
        if isinstance(stream, dict):
            return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        return str(stream or "").strip()

    async def _resolve_streams(self, config: MaiFuturesConfig) -> set[str]:
        streams: set[str] = set()
        platform = config.notify.platform
        for group_id in config.notify.group_ids:
            try:
                stream = await self.ctx.chat.get_stream_by_group_id(str(group_id), platform=platform)
            except Exception as exc:  # noqa: BLE001 - 单个聊天流失败不阻塞加载
                self.ctx.logger.warning("解析群聊流失败 group=%s：%s", group_id, exc)
                continue
            stream_id = self._stream_id(stream)
            if stream_id:
                streams.add(stream_id)
            else:
                self.ctx.logger.warning("群 %s 当前没有可用聊天流（机器人未在该群？）", group_id)
        if config.notify.private_enabled:
            for user_id in config.notify.private_user_ids:
                try:
                    stream = await self.ctx.chat.get_stream_by_user_id(str(user_id), platform=platform)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("解析私聊流失败 user=%s：%s", user_id, exc)
                    continue
                stream_id = self._stream_id(stream)
                if stream_id:
                    streams.add(stream_id)
        return streams

    async def _refresh_streams_if_due(self) -> None:
        if not self._runtimes:
            return
        now = time.monotonic()
        if now - self._last_stream_refresh < _STREAM_REFRESH_INTERVAL_SECONDS:
            return
        self._last_stream_refresh = now
        self._allowed_streams = await self._resolve_streams(self.config)

    async def _send_text(
        self,
        text: str,
        *,
        stream_id: str | None = None,
        storage_message: bool = False,
    ) -> None:
        """发送文本；默认合并为一条转发消息，且不写入宿主消息库。"""

        if not text:
            return
        if stream_id:
            targets = [stream_id]
        else:
            await self._refresh_streams_if_due()
            targets = list(self._allowed_streams)
        if not targets:
            self.ctx.logger.warning("没有可用的播报聊天流，消息未发送：%s", text[:80])
            return
        if not bool(self.config.notify.merge_messages):
            await self._send_plain(text, targets, storage_message=storage_message)
            return
        async with self._outbox_lock:
            for target in targets:
                self._outbox.setdefault(target, []).append(text)
            if self._outbox_task is None or self._outbox_task.done():
                delay = max(0.1, float(self.config.notify.merge_window_ms) / 1000.0)
                self._outbox_task = asyncio.create_task(
                    self._flush_outbox_after(delay), name="mai-futures-outbox"
                )

    async def _send_plain(
        self, text: str, targets: list[str], *, storage_message: bool = False
    ) -> None:
        for target in targets:
            try:
                sent = await self.ctx.send.text(text, target, storage_message=storage_message)
            except Exception as exc:  # noqa: BLE001 - 播报失败不重发
                self.ctx.logger.warning("播报发送异常 stream=%s：%s", target, exc)
                continue
            if sent is False:
                self.ctx.logger.warning("播报发送失败 stream=%s", target)

    async def _flush_outbox_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._flush_outbox()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("合并消息发送任务异常：%s", exc)

    async def _flush_outbox(self) -> None:
        async with self._outbox_lock:
            if not self._outbox:
                return
            batches = self._outbox
            self._outbox = {}
            self._outbox_task = None
        for target, texts in batches.items():
            if texts:
                await self._send_forward(target, texts)

    async def _send_forward(self, target: str, texts: list[str]) -> None:
        nodes = [
            {
                "nickname": "麦麦期货",
                "segments": [{"type": "text", "content": text}],
            }
            for text in texts
        ]
        try:
            result = await self.ctx.send.forward(
                nodes,
                target,
                return_details=True,
                storage_message=False,
                processed_plain_text="",
            )
            sent = bool(result.get("sent")) if isinstance(result, dict) else bool(result)
        except Exception as exc:  # noqa: BLE001 - 转发失败回退普通文本
            self.ctx.logger.warning("合并转发异常，回退普通文本 stream=%s：%s", target, exc)
            sent = False
        if sent:
            return
        self.ctx.logger.warning("合并转发未确认送达，回退普通文本 stream=%s", target)
        await self._send_plain("\n\n".join(texts), [target], storage_message=False)

    # ---------- LLM ----------

    async def _generate_llm(
        self,
        prompt: str,
        *,
        task_name: str,
        temperature: float,
        max_tokens: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        return await self.ctx.call_capability(
            "llm.generate",
            timeout_ms=int(timeout_ms),
            prompt=prompt,
            task_name=str(task_name),
            temperature=float(temperature),
            max_tokens=int(max_tokens),
        )

    # ---------- 命令 ----------

    def _command_allowed(
        self,
        *,
        group_id: str,
        platform: str,
        user_id: str,
        is_local_operator: bool,
    ) -> bool:
        config = self.config
        if is_local_operator:
            return True
        if str(platform or "").strip().lower() != str(config.notify.platform).strip().lower():
            return False
        group_text = str(group_id or "").strip()
        if group_text and group_text in {str(item).strip() for item in config.notify.group_ids}:
            return True
        user_text = str(user_id or "").strip()
        if config.notify.private_enabled and user_text in {str(item).strip() for item in config.notify.private_user_ids}:
            return True
        return False

    @Command(
        "期货",
        description="麦麦期货模拟盘：状态/行情/概览/记录/帮助；控制命令仅管理员",
        pattern=r"^/(?:期货|futures)(?:\s+.*)?$",
        timeout_ms=45000,
    )
    async def handle_futures(
        self,
        stream_id: str = "",
        text: str = "",
        group_id: str = "",
        platform: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /期货 命令；具体多市场路由在 CommandRouter。"""

        del kwargs
        result = await self._router.handle(
            text=text,
            stream_id=stream_id,
            group_id=group_id,
            platform=platform,
            user_id=user_id,
            is_local_operator=is_local_operator,
        )
        # 命令内可能同时产生“成交播报 + 操作回执”，命令结束时一起合并转发
        await self._flush_outbox()
        return result
    # ---------- 只读 Tool（从本地账本取数，不写记忆） ----------

    def _tool_context_allowed(self, kwargs: dict[str, Any]) -> bool:
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        config = self.config
        if group_id and group_id in {str(item).strip() for item in config.notify.group_ids}:
            return True
        if config.notify.private_enabled and user_id in {
            str(item).strip() for item in config.notify.private_user_ids
        }:
            return True
        return False

    @Tool(
        "futures_snapshot",
        description=(
            "只读查询麦麦的**商品期货模拟盘**（国内商品期货 + 国际期货，不是股票）："
            "账户/持仓/最近成交/市场概览；不下单、不修改任何数据。"
        ),
        parameters=[
            ToolParameterInfo(
                name="view",
                param_type=ToolParamType.STRING,
                description="查询内容：status=账户与持仓，positions=持仓，trades=最近成交，overview=两市场行情概览",
                required=False,
                enum_values=["status", "positions", "trades", "overview"],
            ),
        ],
    )
    async def tool_futures_snapshot(self, view: str = "status", **kwargs: Any) -> dict[str, Any]:
        """只读工具：账户来自本地 SQLite；overview 会实时拉行情。"""

        if not self._tool_context_allowed(kwargs):
            return {"success": False, "content": "这个聊天流没有查询期货模拟盘的权限。"}
        if not self._runtimes:
            return {"success": False, "content": "期货模拟盘尚未就绪。"}
        return await tools.snapshot(list(self._runtimes.values()), view)


def create_plugin() -> MaiFuturesSimPlugin:
    """宿主加载入口：不接受参数，返回插件实例。"""
    return MaiFuturesSimPlugin()
