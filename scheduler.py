"""交易调度与交易编排：采样 → 决策 → 风控 → 成交 → 记账 → 播报；收盘后结算。

支持日盘 + 按品种夜盘（跨午夜）；交易日归属夜盘计入下一工作日。
节假日日历、自动换月、国际市场仍在后续版本。

Codex 评审 P1 修复：
- LLM 的 ``close`` 意图按实际持仓方向映射成 ``close_long`` / ``close_short``；
- 暂停/重置有状态版本号，模型返回后、T1 取价后、最终写账前都会复查；
- 缺行情时保留最后有效估值并标记不完整，估值不完整禁止开新仓；
- 结算价缺失/部分完成 → 持久化 pending 状态，按间隔重试，未完成禁止开新仓；
- 到期前禁开、最后交易日/过期强平；
- 决策放在独立后台任务，模型等待期间行情与止损巡检继续运行。
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from . import market_calendar, report
from .bars import SampleBook
from .config import MaiFuturesConfig
from .contracts import DEFAULT_CONTRACTS, ContractSpec, front_name
from .decision import BudgetExhausted, DecisionError, DecisionInput, DecisionText, LLMDecisionEngine
from .execution import ExecutionError, check_fill_ready, make_fill_plan
from .ledger import Ledger, LedgerError, NoPositionError
from .quotes import QuoteFetchError, SinaQuoteSource, is_quote_fresh, quote_is_usable
from .risk import (
    RiskContext,
    check_close,
    check_open,
    daily_loss_hit,
    expiry_force_close_required,
)

CST = ZoneInfo("Asia/Shanghai")
NotifyFn = Callable[[str], Awaitable[None]]


def parse_sessions(entries: list[str]) -> list[tuple[time, time]]:
    """解析 ``HH:MM-HH:MM`` 时段列表；非法配置抛 ValueError。"""

    sessions: list[tuple[time, time]] = []
    for entry in entries or []:
        parts = str(entry).split("-")
        if len(parts) != 2:
            raise ValueError(f"交易时段格式错误：{entry!r}（应为 HH:MM-HH:MM）")
        try:
            start = time.fromisoformat(parts[0].strip())
            end = time.fromisoformat(parts[1].strip())
        except ValueError as exc:
            raise ValueError(f"交易时段格式错误：{entry!r}") from exc
        if start >= end:
            raise ValueError(f"交易时段起止非法：{entry!r}")
        sessions.append((start, end))
    if not sessions:
        raise ValueError("交易时段不能为空")
    return sorted(sessions)


def in_sessions(now: datetime, sessions: list[tuple[time, time]]) -> bool:
    """当前是否在某个交易时段内（左闭右开；支持跨午夜）。"""

    current = now.time()
    for start, end in sessions:
        if start < end:
            if start <= current < end:
                return True
        else:
            # 跨午夜：21:00-02:30 表示 >=21:00 或 <02:30
            if current >= start or current < end:
                return True
    return False


# 夜盘时段（按合约 session_profile）；结束时间早于开始时间表示跨午夜
_NIGHT_RANGES: dict[str, tuple[tuple[time, time], ...]] = {
    "day": (),
    "night_2300": ((time(21, 0), time(23, 0)),),
    "night_0100": ((time(21, 0), time(1, 0)),),
    "night_0230": ((time(21, 0), time(2, 30)),),
}


def session_ranges(
    spec: ContractSpec,
    day_sessions: list[tuple[time, time]],
    *,
    night_enabled: bool,
) -> list[tuple[time, time]]:
    """日盘 + 该合约对应的夜盘时段。"""

    ranges = list(day_sessions)
    if night_enabled:
        ranges.extend(_NIGHT_RANGES.get(str(spec.session_profile or "day"), ()))
    return sorted(ranges)


def trading_day(now: datetime, market: str = "cn") -> date:
    """期货交易日归属（按市场日历）：

    - 20:00 之后的夜盘属于下一个交易日（周末/节假日顺延）；
    - 周六凌晨（周五夜盘尾段）属于下一个交易日；
    - 其余时间属于当天。
    """

    current_date = now.date()
    current_time = now.time()
    if current_time >= time(20, 0):
        return market_calendar.next_trading_day(market, current_date)
    if current_date.weekday() == 5 and current_time < time(4, 0):
        return market_calendar.next_trading_day(market, current_date)
    return current_date


def is_weekday(now: datetime) -> bool:
    return now.weekday() < 5


class TradingEngine:
    """交易编排器；``tick()`` 可被测试直接驱动。"""

    def __init__(
        self,
        *,
        config: MaiFuturesConfig,
        specs: dict[str, ContractSpec],
        ledger: Ledger,
        quote_source: SinaQuoteSource,
        sample_book: SampleBook,
        decision_engine: LLMDecisionEngine,
        notify: NotifyFn,
        logger: Any,
        clock: Callable[[], datetime] | None = None,
        persona: str = "",
        influence: Any = None,
        session_entries: list[str] | None = None,
        night_enabled: bool | None = None,
        trading_day_mode: str = "cn",
        market_label: str = "",
        currency: str = "CNY",
        calendar_market: str = "cn",
        settlement_provider: Callable[[str, list[str]], Awaitable[dict[str, float]]] | None = None,
    ) -> None:
        self._config = config
        self._specs = specs
        self._ledger = ledger
        self._source = quote_source
        self._samples = sample_book
        self._decider = decision_engine
        self._notify = notify
        self._logger = logger
        self._clock = clock or (lambda: datetime.now(CST))
        self._persona = str(persona or "")
        self._influence = influence
        self._market_label = str(market_label or "")
        self._currency = str(currency or "CNY")
        self._calendar_market = str(calendar_market or "cn")
        self._settlement_provider = settlement_provider
        self._trading_day = (
            (lambda now: trading_day(now, self._calendar_market))
            if trading_day_mode == "cn"
            else (lambda now: now.date())
        )
        self._day_sessions = parse_sessions(
            list(session_entries) if session_entries is not None else config.market.sessions
        )
        effective_night = bool(config.market.night_enabled) if night_enabled is None else bool(night_enabled)
        self._night_enabled = effective_night
        self._sessions_by_symbol = {
            symbol: session_ranges(
                spec,
                self._day_sessions,
                night_enabled=effective_night,
            )
            for symbol, spec in specs.items()
        }
        # 兼容旧测试/内部引用：默认日盘
        self._sessions = self._day_sessions
        self.paused: bool = ledger.get_meta("paused", "0") == "1"
        self.last_quotes: dict[str, Any] = {}
        self._last_poll_at: datetime | None = None
        self._next_decision_at: datetime | None = None
        self._settle_notified: set[str] = set()
        self._halt_notice_day: str = ""
        self._state_version: int = 0
        self._decision_task: asyncio.Task[None] | None = None
        self._trade_lock = asyncio.Lock()
        self._stopped: bool = False

    def _symbol_sessions(self, symbol: str) -> list[tuple[time, time]]:
        return self._sessions_by_symbol.get(symbol, self._day_sessions)

    def _symbol_session_open(self, now: datetime, symbol: str) -> bool:
        return in_sessions(now, self._symbol_sessions(symbol))

    def _any_session_open(self, now: datetime) -> bool:
        return any(in_sessions(now, ranges) for ranges in self._sessions_by_symbol.values())

    def _night_session_blocked(self, now: datetime) -> bool:
        """长假前最后交易日停夜盘；次日凌晨也属于这段夜盘。"""

        if now.time() >= time(20, 0):
            return not market_calendar.night_session_allowed(
                self._calendar_market, now.date()
            )
        if now.time() < time(4, 0):
            return not market_calendar.night_session_allowed(
                self._calendar_market, now.date() - timedelta(days=1)
            )
        return False

    async def _maybe_rollover(self, now: datetime) -> None:
        """主力合约进入换月窗口：先平旧仓，再切换合约参数。"""

        today = self._trading_day(now)
        for old_symbol, spec in list(self._specs.items()):
            target = str(spec.rollover_to or "").strip()
            if not target or target in self._specs:
                continue
            target_spec = DEFAULT_CONTRACTS.get(target)
            if target_spec is None:
                self._logger.warning("换月目标未注册：%s -> %s", old_symbol, target)
                continue
            try:
                last_day = date.fromisoformat(spec.last_trade_date)
            except ValueError:
                continue
            days_left = market_calendar.trading_days_between(
                self._calendar_market, today, last_day
            )
            if days_left > int(spec.rollover_days):
                continue

            position = self._ledger.position(old_symbol)
            if position is not None and days_left <= 1:
                # 最后交易日/已过期交给到期强平处理，避免抢在风控前面平仓
                continue
            if position is not None:
                batch = await self._fetch([old_symbol])
                quote = batch.quotes.get(old_symbol) if batch is not None else None
                if quote is None or not quote_is_usable(
                    quote, max_abs_change_ratio=self._config.risk.max_abs_price_change_ratio
                ):
                    self._logger.warning("%s 进入换月窗口但行情不可用，暂缓切换", old_symbol)
                    continue
                await self._close_from_rule(now, position, quote, "换月平仓")
                if self._ledger.position(old_symbol) is not None:
                    self._logger.warning("%s 换月平仓未完成，暂缓切换", old_symbol)
                    continue

            new_specs: dict[str, ContractSpec] = {}
            for symbol, item in self._specs.items():
                if symbol == old_symbol:
                    new_specs[target] = target_spec
                else:
                    new_specs[symbol] = item
            self._specs = new_specs
            self._sessions_by_symbol[target] = session_ranges(
                target_spec, self._day_sessions, night_enabled=self._night_enabled
            )
            self._sessions_by_symbol.pop(old_symbol, None)
            self.last_quotes.pop(old_symbol, None)
            self._next_decision_at = None
            self._logger.info("换月：%s -> %s", old_symbol, target)
            if self._config.notify.event_reports:
                await self._safe_notify(
                    f"🔄 {front_name(old_symbol)} 进入换月窗口，已平旧仓并切换到下一主力合约。"
                )

    # ---------- 状态 ----------

    @property
    def specs(self) -> dict[str, ContractSpec]:
        """当前合约池参数（只读）。"""
        return dict(self._specs)

    @property
    def quote_source(self) -> SinaQuoteSource:
        """行情源（命令层复用同一个实例）。"""
        return self._source

    @property
    def state_version(self) -> int:
        """运行状态版本号；暂停/重置/停机时递增，用于作废在途决策。"""
        return self._state_version

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self._ledger.set_meta("paused", "1" if self.paused else "0")
        self._invalidate_in_flight()

    def reset_runtime_state(self) -> None:
        """重置账户后清理内存态（行情缓存、决策节奏）并作废在途决策。"""

        self.last_quotes.clear()
        self._last_poll_at = None
        self._next_decision_at = None
        self._settle_notified.clear()
        self._halt_notice_day = ""
        self._invalidate_in_flight()

    def current_trading_day(self, now: datetime | None = None) -> str:
        """当前交易日文本（重置账户时不能用自然日）。"""

        return self._trading_day(now or self._clock()).isoformat()

    def _invalidate_in_flight(self) -> None:
        self._state_version += 1
        task = self._decision_task
        if task is not None and not task.done():
            task.cancel()

    async def stop(self) -> None:
        """停止引擎：作废在途决策并等待其退出（插件卸载/配置重建时调用）。"""

        self._stopped = True
        # tick 可能正在 await 行情；停完后它不能再创建新任务。
        self._invalidate_in_flight()
        for _ in range(3):
            task = self._decision_task
            if task is None:
                break
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停止阶段吞掉
                pass
            if self._decision_task is task:
                self._decision_task = None
            if self._decision_task is None:
                break
        self._decision_task = None

    def latest_marks(self) -> dict[str, float]:
        return {symbol: quote.last for symbol, quote in self.last_quotes.items()}

    def _marks_from(self, quotes: dict[str, Any]) -> dict[str, float]:
        return {symbol: quote.last for symbol, quote in quotes.items() if getattr(quote, "last", 0) > 0}

    def _valuation_marks(self) -> tuple[dict[str, float], bool]:
        """最后有效估值 + 是否覆盖所有持仓（缺价时不把浮亏当 0）。"""

        marks = {symbol: quote.last for symbol, quote in self.last_quotes.items() if getattr(quote, "last", 0) > 0}
        positions = self._ledger.positions()
        complete = all(position.symbol in marks for position in positions)
        return marks, complete

    # ---------- 主循环 ----------

    async def run_forever(self) -> None:
        """常驻循环：每约 5 秒 tick 一次；异常只记录，不退出。"""

        while not self._stopped:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台循环兜底
                self._logger.error("交易循环异常：%s", exc, exc_info=True)
            await asyncio.sleep(5)

    async def tick(self, now: datetime | None = None) -> None:
        """执行一次调度（可按假时钟直接调用，便于测试）。"""

        if self._stopped:
            return
        current = self._clock() if now is None else now
        today_text = self._trading_day(current).isoformat()
        account = self._ledger.account()
        if account.day != today_text:
            # 20:00 夜盘交易日会翻到 D+1，但 D 的结算仍要在翻日之前完成。
            await self._settlement_tick(current)
            await self._reconcile_before_rollover(current)
            marks, complete = self._valuation_marks()
            self._ledger.rollover(today_text, marks, complete=complete)
            self._next_decision_at = None
            self._halt_notice_day = ""
        if self._night_session_blocked(current):
            return
        if not self._any_session_open(current):
            # 周六 05:10 等非交易时段也要允许补结算上一交易日。
            await self._settlement_tick(current)
            return
        if not market_calendar.is_trading_day(self._calendar_market, current.date()):
            previous = current.date() - timedelta(days=1)
            in_early_overnight = (
                current.time() < time(4, 0)
                and market_calendar.is_trading_day(self._calendar_market, previous)
            )
            if not in_early_overnight:
                return
        await self._maybe_rollover(current)
        await self._trading_tick(current)

    # ---------- 盘中 ----------

    async def _fetch(self, symbols: list[str]) -> Any:
        try:
            return await self._source.fetch(symbols)
        except QuoteFetchError as exc:
            self._logger.warning("行情拉取失败：%s", exc)
            return None

    def _on_decision_done(self, task: asyncio.Task[None]) -> None:
        if task is self._decision_task:
            self._decision_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._logger.error("决策任务异常：%s", exc, exc_info=exc)

    async def _trading_tick(self, now: datetime) -> None:
        poll_seconds = max(10, int(self._config.market.quote_poll_seconds))
        if self._last_poll_at is not None and (now - self._last_poll_at).total_seconds() < poll_seconds:
            return
        batch = await self._fetch(list(self._specs))
        self._last_poll_at = now
        if batch is None:
            return
        if self._stopped:
            return

        fresh: dict[str, Any] = {}
        for symbol, quote in batch.quotes.items():
            spec = self._specs.get(symbol)
            if spec is None:
                continue
            if not quote_is_usable(quote, max_abs_change_ratio=self._config.risk.max_abs_price_change_ratio):
                self._logger.warning("%s 行情异常，已忽略：%s", symbol, quote)
                continue
            # 保留最后有效估值（即使过期），供权益/保证金判断使用
            self.last_quotes[symbol] = quote
            if not is_quote_fresh(quote, now, self._config.market.stale_quote_seconds):
                self._logger.warning("%s 行情过期（可能休市/断流），本轮不参与决策", symbol)
                continue
            if not self._symbol_session_open(now, symbol):
                # 该品种当前不在交易时段：不采样、不进入决策
                continue
            fresh[symbol] = quote
            self._samples.add(quote)
        if not fresh:
            self._logger.warning("本轮没有可用行情，跳过")
            return

        marks, valuation_complete = self._valuation_marks()
        equity = self._ledger.mark_equity(now.isoformat(timespec="seconds"), marks)
        if self._ledger.day_start_pending() and valuation_complete:
            self._ledger.finalize_day_start(equity.equity)

        account = self._ledger.account()
        limit = self._config.risk.daily_loss_limit_ratio
        if valuation_complete and not account.halted and daily_loss_hit(
            equity=equity.equity,
            day_start_equity=account.day_start_equity,
            limit_ratio=limit,
        ):
            self._ledger.set_halted(True)
            today_text = self._trading_day(now).isoformat()
            if self._halt_notice_day != today_text:
                self._halt_notice_day = today_text
                if self._config.notify.event_reports:
                    await self._safe_notify(
                        f"⛔ 今日亏损达到 {limit * 100:.0f}%（权益 {report.fmt_money(equity.equity)}），"
                        "已停止开新仓，持仓仍可按规则平仓。"
                    )

        # 止损/止盈优先于新决策
        for position in list(self._ledger.positions()):
            quote = fresh.get(position.symbol)
            if quote is None:
                continue
            if not is_quote_fresh(quote, now, self._config.risk.fill_max_quote_age_seconds):
                continue
            reason = self._stop_take_reason(position, quote)
            if reason:
                await self._close_from_rule(now, position, quote, reason)

        # 到期强平：最后交易日或已过期（离线跨过到期日也在这里补平）
        today = self._trading_day(now)
        for position in list(self._ledger.positions()):
            spec = self._specs.get(position.symbol)
            if spec is None:
                continue
            if not expiry_force_close_required(spec, today):
                continue
            quote = fresh.get(position.symbol)
            if quote is None:
                # 过期后新浪不再更新该合约：用最后有效行情/最近结算价离线平仓，
                # 否则持仓会永久占用保证金和持仓位。
                await self._force_close_expired(now, position, spec)
                continue
            await self._close_from_rule(now, position, quote, "到期强平")

        if not self._config.decision.auto_trade or self.paused:
            return
        account = self._ledger.account()
        if account.halted:
            return
        if not self._config.decision.llm_enabled:
            today_text = now.strftime("%Y-%m-%d")
            if self._halt_notice_day != f"llm_off:{today_text}":
                self._halt_notice_day = f"llm_off:{today_text}"
                self._logger.warning("llm_enabled=false：不发起新的决策调用")
            return
        if self._decision_task is not None and not self._decision_task.done():
            return
        if self._next_decision_at is not None and now < self._next_decision_at:
            return
        if self._settlement_pending():
            self._logger.warning("存在未完成结算对账，暂不发起新决策")
            return
        if self._stopped:
            return
        # 决策放后台任务：模型等待期间本轮循环继续负责行情/止损巡检
        self._decision_task = asyncio.create_task(
            self._decision_round(now, fresh, marks), name="mai-futures-decision"
        )
        self._decision_task.add_done_callback(self._on_decision_done)

    async def _force_close_expired(self, now: datetime, position: Any, spec: ContractSpec) -> None:
        """过期且无最新行情时，用最后有效价/最近结算价离线平仓。"""

        quote = self.last_quotes.get(position.symbol)
        price = float(getattr(quote, "last", 0) or 0) if quote is not None else 0.0
        if price <= 0:
            price = float(self._ledger.latest_settlement_price(position.symbol) or 0.0)
        if price <= 0:
            key = f"expiry_no_price:{position.symbol}"
            if self._ledger.get_meta(key) != "1":
                self._ledger.set_meta(key, "1")
                self._logger.warning("%s 已过期且没有可用的最后价格，暂不能自动平仓", position.symbol)
                if self._config.notify.event_reports:
                    await self._safe_notify(
                        f"⚠️ {spec.display_name or position.symbol} 已过期但无行情，"
                        "暂时无法自动平仓；请停止自动交易并人工检查。"
                    )
            return
        trade = self._ledger.try_close(
            order_id=f"expiry-offline:{position.symbol}:{now.date().isoformat()}",
            decision_id=f"expiry-offline:{position.symbol}",
            ts=now.isoformat(timespec="seconds"),
            spec=spec,
            price=price,
            note="过期无行情离线平仓",
        )
        self._ledger.delete_meta(f"expiry_no_price:{position.symbol}")
        self._logger.warning("%s 过期无行情，已按 %.4f 离线平仓", position.symbol, price)
        if self._config.notify.event_reports:
            equity = self._ledger.equity(self._valuation_marks()[0])
            await self._safe_notify(
                report.format_close(trade, spec, equity, reason="过期无行情，按最后价格离线平仓")
            )

    def _stop_take_reason(self, position: Any, quote: Any) -> str:
        if position.side == "long":
            if quote.last <= position.stop_price:
                return "止损"
            if quote.last >= position.take_price:
                return "止盈"
        else:
            if quote.last >= position.stop_price:
                return "止损"
            if quote.last <= position.take_price:
                return "止盈"
        return ""

    async def _close_from_rule(self, now: datetime, position: Any, quote: Any, reason: str) -> None:
        spec = self._specs[position.symbol]
        action = "close_long" if position.side == "long" else "close_short"
        decision_id = f"rule:{position.symbol}:{int(now.timestamp())}"
        try:
            plan = make_fill_plan(
                action=action,
                quote=quote,
                spec=spec,
                lots=position.lots,
                slippage_ticks=self._config.account.slippage_ticks,
            )
        except ExecutionError as exc:
            self._logger.warning("%s 规则平仓失败：%s", position.symbol, exc)
            return
        async with self._trade_lock:
            current = self._ledger.position(position.symbol)
            if current is None or current.side != position.side:
                return
            try:
                trade = self._ledger.try_close(
                    order_id=decision_id,
                    decision_id=decision_id,
                    ts=now.isoformat(timespec="seconds"),
                    spec=spec,
                    price=plan.price,
                    note=reason,
                )
            except (ExecutionError, LedgerError, NoPositionError, sqlite3.Error) as exc:
                self._logger.warning("%s 规则平仓失败：%s", position.symbol, exc)
                return
        equity = self._ledger.equity(self._valuation_marks()[0])
        if self._config.notify.event_reports:
            await self._safe_notify(report.format_close(trade, spec, equity, reason=reason))
        self._logger.info("%s 触发%s，已平仓 @%s", position.symbol, reason, trade.price)

    async def _decision_round(self, now: datetime, fresh: dict[str, Any], marks: dict[str, float]) -> None:
        self._schedule_next_decision(now)
        version = self._state_version
        influence_text = ""
        if self._influence is not None:
            try:
                influence_text = await self._influence.gather(list(self._specs))
            except Exception as exc:  # noqa: BLE001 - 影响源失败不影响交易
                self._logger.warning("影响源收集失败（本轮跳过）：%s", exc)
        decision_input = self._build_decision_input(now, fresh, influence_text=influence_text)
        decision_id = uuid.uuid4().hex
        try:
            decision = await self._decider.decide(decision_input)
        except asyncio.CancelledError:
            raise
        except BudgetExhausted as exc:
            self._logger.warning("决策跳过：%s", exc)
            return
        except DecisionError as exc:
            self._logger.warning("决策失败：%s", exc)
            return
        ts = now.isoformat(timespec="seconds")
        if version != self._state_version or self.paused:
            self._ledger.record_decision(
                decision_id=decision_id,
                ts=ts,
                symbol=decision.symbol if decision else "",
                action=decision.action if decision else "hold",
                lots=decision.lots if decision else 1,
                reason="模型返回时状态已变更（暂停/重置），旧决策作废",
                source="llm",
                outcome="stale_state",
            )
            self._logger.info("决策作废：模型返回时状态已变更")
            return
        if decision is None:
            self._ledger.record_decision(
                decision_id=decision_id,
                ts=ts,
                symbol="",
                action="hold",
                lots=1,
                reason="模型输出不可解析，按 hold 处理",
                source="llm",
                outcome="fallback_hold",
            )
            return
        if decision.action == "hold":
            self._ledger.record_decision(
                decision_id=decision_id,
                ts=ts,
                symbol="",
                action="hold",
                lots=1,
                reason=decision.reason,
                source="llm",
                outcome="hold",
            )
            return

        outcome, detail = await self._execute_decision(now, decision, decision_input, decision_id, version)
        self._ledger.record_decision(
            decision_id=decision_id,
            ts=ts,
            symbol=decision.symbol,
            action=decision.action,
            lots=decision.lots,
            reason=decision.reason,
            source="llm",
            validation=detail,
            outcome=outcome,
        )
        if outcome not in ("opened", "closed"):
            self._logger.info("决策未成交（%s）：%s", outcome, detail)

    async def _execute_decision(
        self,
        now: datetime,
        decision: Any,
        decision_input: DecisionInput,
        decision_id: str,
        version: int,
        *,
        manual: bool = False,
        source: str = "llm",
    ) -> tuple[str, str]:
        if version != self._state_version:
            return "rejected", "状态已变更（暂停/重置），旧决策作废"
        if not manual and self.paused:
            return "rejected", "已暂停，旧决策作废"
        spec = self._specs.get(decision.symbol)
        if spec is None:
            return "rejected", f"合约不在合约池：{decision.symbol}"

        # 动作映射：LLM 只输出 close，执行层按实际持仓方向转换
        action = decision.action
        if action == "close":
            position = self._ledger.position(decision.symbol)
            if position is None:
                return "rejected", f"{decision.symbol} 无持仓，无需平仓"
            action = "close_long" if position.side == "long" else "close_short"
            lots = position.lots
        elif action in ("open_long", "open_short"):
            lots = decision.lots
        else:
            return "rejected", f"未知动作：{action}"

        current = self._clock()
        if not self._symbol_session_open(current, decision.symbol):
            return "rejected", "决策完成时已不在交易时段"
        equity_before = self._ledger.equity(self._valuation_marks()[0])
        context = self._risk_context(current, decision.symbol, decision_input, equity_before, spec)
        if action.startswith("open"):
            verdict = check_open(
                symbol=decision.symbol,
                lots=lots,
                spec=spec,
                context=context,
                config=self._config.risk,
                side="long" if action == "open_long" else "short",
                allow_short=self._config.account.allow_short,
            )
        else:
            verdict = check_close(symbol=decision.symbol, spec=spec, context=context)
        if not verdict.allowed:
            return "rejected", verdict.reason

        batch = await self._fetch([decision.symbol])
        quote_t1 = None if batch is None else batch.quotes.get(decision.symbol)
        t0_quote = decision_input.quotes.get(decision.symbol)
        t0_mid = t0_quote.mid if t0_quote is not None else 0.0
        reject_reason = check_fill_ready(
            quote=quote_t1,
            t0_mid=t0_mid,
            now=self._clock(),
            spec=spec,
            max_quote_age_seconds=self._config.risk.fill_max_quote_age_seconds,
            max_drift_ticks=self._config.risk.max_drift_ticks,
        )
        if reject_reason:
            return "rejected", reject_reason
        try:
            plan = make_fill_plan(
                action=action,
                quote=quote_t1,
                spec=spec,
                lots=lots,
                slippage_ticks=self._config.account.slippage_ticks,
            )
        except ExecutionError as exc:
            return "rejected", str(exc)

        # 模型返回后/T1 取价后/写账前多次复查状态版本（管理员手动单不受暂停影响）
        if version != self._state_version:
            return "rejected", "状态已变更（暂停/重置），旧决策作废"
        if not manual and self.paused:
            return "rejected", "状态已变更（暂停/重置），旧决策作废"
        if not manual and self._ledger.account().halted:
            return "rejected", "已触发亏损停手，旧决策作废"
        equity_t1 = self._ledger.equity(self._valuation_marks()[0])
        context_t1 = self._risk_context(
            self._clock(), decision.symbol, decision_input, equity_t1, spec, reference_price=plan.price
        )
        if action.startswith("open"):
            verdict = check_open(
                symbol=decision.symbol,
                lots=lots,
                spec=spec,
                context=context_t1,
                config=self._config.risk,
                side="long" if action == "open_long" else "short",
                allow_short=self._config.account.allow_short,
            )
        else:
            verdict = check_close(symbol=decision.symbol, spec=spec, context=context_t1)
        if not verdict.allowed:
            return "rejected", verdict.reason

        ts = self._clock().isoformat(timespec="seconds")
        async with self._trade_lock:
            if version != self._state_version:
                return "rejected", "状态已变更（暂停/重置），旧决策作废"
            if not manual and self.paused:
                return "rejected", "状态已变更（暂停/重置），旧决策作废"
            if not manual and self._ledger.account().halted:
                return "rejected", "已触发亏损停手，旧决策作废"
            if not self._symbol_session_open(self._clock(), decision.symbol):
                return "rejected", "已不在交易时段"
            try:
                if action.startswith("open"):
                    atr = self._samples.atr(decision.symbol) or 0.0
                    stop_price, take_price = self._stop_take_prices(action, plan.price, atr)
                    trade = self._ledger.try_open(
                        order_id=decision_id,
                        decision_id=decision_id,
                        ts=ts,
                        spec=spec,
                        side="long" if action == "open_long" else "short",
                        lots=lots,
                        price=plan.price,
                        stop_price=stop_price,
                        take_price=take_price,
                    )
                else:
                    trade = self._ledger.try_close(
                        order_id=decision_id,
                        decision_id=decision_id,
                        ts=ts,
                        spec=spec,
                        price=plan.price,
                        note=decision.reason,
                    )
            except (ExecutionError, LedgerError, NoPositionError, sqlite3.Error) as exc:
                return "rejected", str(exc)

        equity = self._ledger.equity(self._valuation_marks()[0])
        if self._config.notify.event_reports:
            if action.startswith("open"):
                await self._safe_notify(report.format_open(trade, spec, equity, speech=decision.speech))
            else:
                await self._safe_notify(
                    report.format_close(trade, spec, equity, speech=decision.speech, reason=decision.reason)
                )
        if action.startswith("open"):
            side_text = "多" if trade.side == "long" else "空"
            return "opened", f"{front_name(trade.symbol)} {side_text} @{trade.price:.4g}"
        return "closed", f"{front_name(trade.symbol)} @{trade.price:.4g}"

    async def manual_order(self, *, action: str, symbol: str, lots: int = 1, operator: str = "") -> tuple[str, str]:
        """管理员手动单：与自主交易共用同一风控/成交/账本；不受暂停影响，但仍要求交易时段。"""

        symbol = str(symbol or "").strip().upper()
        spec = self._specs.get(symbol)
        if spec is None:
            return "rejected", f"合约不在合约池：{symbol or '（空）'}"
        normalized = {
            "buy": "open_long",
            "open_long": "open_long",
            "sell": "open_short",
            "open_short": "open_short",
            "close": "close",
        }.get(str(action or "").strip().lower())
        if normalized is None:
            return "rejected", f"未知操作：{action}"
        if normalized != "close":
            try:
                lots = int(lots)
            except (TypeError, ValueError):
                return "rejected", f"手数非法：{lots!r}"
            if lots <= 0:
                return "rejected", f"手数非法：{lots}"

        now = self._clock()
        if not self._symbol_session_open(now, symbol):
            return "rejected", "当前不在交易时段，无法执行手动操作"
        state_version = self._state_version
        batch = await self._fetch(list(self._specs))
        if batch is None:
            return "rejected", "行情取不到，无法执行手动操作"
        fresh: dict[str, Any] = {}
        for item_symbol, quote in batch.quotes.items():
            if item_symbol not in self._specs:
                continue
            if not quote_is_usable(quote, max_abs_change_ratio=self._config.risk.max_abs_price_change_ratio):
                continue
            fresh[item_symbol] = quote
        if symbol not in fresh:
            return "rejected", f"{symbol} 行情不可用，无法执行手动操作"

        decision = DecisionText(
            action=normalized,
            symbol=symbol,
            lots=lots if normalized != "close" else 1,
            reason="管理员手动操作",
            speech="",
        )
        decision_id = f"admin:{uuid.uuid4().hex}"
        source = f"admin:{operator}"[:16]
        outcome, detail = await self._execute_decision(
            now,
            decision,
            self._build_decision_input(now, fresh),
            decision_id,
            state_version,
            manual=True,
            source=source,
        )
        self._ledger.record_decision(
            decision_id=decision_id,
            ts=now.isoformat(timespec="seconds"),
            symbol=symbol,
            action=normalized,
            lots=decision.lots,
            reason=decision.reason,
            source=source,
            validation=detail,
            outcome=outcome,
        )
        if outcome not in ("opened", "closed"):
            self._logger.info("管理员手动单未成交（%s）：%s", outcome, detail)
        return outcome, detail

    def _risk_context(
        self,
        now: datetime,
        symbol: str,
        decision_input: DecisionInput,
        equity: Any,
        spec: ContractSpec,
        *,
        reference_price: float | None = None,
    ) -> RiskContext:
        price = reference_price
        if price is None:
            quote = decision_input.quotes.get(symbol)
            price = quote.last if quote is not None else 0.0
        _, valuation_ok = self._valuation_marks()
        return RiskContext(
            account=self._ledger.account(),
            positions=self._ledger.positions(),
            equity=equity.equity,
            margin_used=equity.margin_used,
            reference_price=price,
            now=now,
            session_open=self._symbol_session_open(now, symbol),
            atr=self._samples.atr(symbol),
            last_open_ts=self._ledger.last_open_ts(symbol),
            today=self._trading_day(now),
            sample_count=self._samples.count(symbol),
            required_samples=int(self._config.decision.warmup_samples),
            valuation_ok=valuation_ok,
            settlement_pending=self._settlement_pending(),
        )

    def _stop_take_prices(self, action: str, price: float, atr: float) -> tuple[float, float]:
        stop_mult = self._config.risk.stop_loss_atr_mult
        take_mult = self._config.risk.take_profit_atr_mult
        if action == "open_long":
            return round(price - atr * stop_mult, 8), round(price + atr * take_mult, 8)
        return round(price + atr * stop_mult, 8), round(price - atr * take_mult, 8)

    def _schedule_next_decision(self, now: datetime) -> None:
        interval = max(
            int(self._config.decision.min_interval_minutes),
            min(int(self._config.decision.max_interval_minutes), int(self._config.decision.interval_minutes)),
        )
        self._next_decision_at = now + timedelta(minutes=interval)

    def _build_decision_input(self, now: datetime, fresh: dict[str, Any], *, influence_text: str = "") -> DecisionInput:
        marks = {symbol: quote.last for symbol, quote in fresh.items()}
        equity = self._ledger.equity(marks)
        session_symbols = [
            symbol for symbol in self._specs if self._symbol_session_open(now, symbol)
        ]
        return DecisionInput(
            now=now,
            symbols=session_symbols,
            quotes=dict(fresh),
            positions=self._ledger.positions(),
            account=self._ledger.account(),
            equity=equity,
            atr=self._samples.atr_map(list(self._specs)),
            recent_trades=list(reversed(self._ledger.recent_trades(self._config.decision.recent_trades_in_context))),
            sample_counts={symbol: self._samples.count(symbol) for symbol in self._specs},
            max_trades_today=self._config.risk.daily_trade_limit,
            max_margin_ratio=self._config.risk.max_total_margin_ratio,
            persona=self._persona,
            influence_text=influence_text,
        )

    # ---------- 结算 ----------

    def _settle_pending_dates(self) -> list[str]:
        return [
            key[len("settle_pending:") :]
            for key in self._ledger.meta_keys("settle_pending:")
            if self._ledger.get_meta(key, "0") == "1"
        ]

    def _settlement_pending(self) -> bool:
        return bool(self._settle_pending_dates())

    def _mark_settle_pending(self, settle_date: str, now: datetime) -> bool:
        """标记待结算，返回是否为首次告警。"""

        self._ledger.set_meta(f"settle_pending:{settle_date}", "1")
        retry = max(30, int(self._config.account.settle_retry_seconds))
        self._ledger.set_meta(
            f"settle_next_try:{settle_date}",
            (now + timedelta(seconds=retry)).isoformat(timespec="seconds"),
        )
        alert_key = f"settle_alerted:{settle_date}"
        first = self._ledger.get_meta(alert_key, "0") != "1"
        if first:
            self._ledger.set_meta(alert_key, "1")
        return first

    def _clear_settle_pending(self, settle_date: str) -> None:
        for prefix in ("settle_pending:", "settle_next_try:", "settle_alerted:"):
            self._ledger.delete_meta(prefix + settle_date)

    async def _reconcile_before_rollover(self, now: datetime) -> None:
        """跨日重启：用当前行情里的昨结算（prev_settle）补最近一个缺失的结算日。

        只处理单个 pending 日期（prev_settle 只对应上一个交易日）；多日缺口无法可靠补账，
        保留 pending 并阻止开新仓，交给 owner 处理。
        """

        today_text = self._trading_day(now).isoformat()
        pending = sorted(d for d in self._settle_pending_dates() if d < today_text)
        if not pending:
            return
        positions = self._ledger.positions()
        if not positions:
            for settle_date in pending:
                self._clear_settle_pending(settle_date)
            return
        if len(pending) > 1:
            self._logger.warning("存在多个缺失结算日（%s），无法自动补账；在 owner 处理前禁止开新仓", pending)
            return
        settle_date = pending[0]
        batch = await self._fetch([position.symbol for position in positions])
        if batch is None:
            return
        prices: dict[str, float] = {}
        for position in positions:
            quote = batch.quotes.get(position.symbol)
            if quote is None:
                continue
            price = 0.0
            if quote.prev_settle > 0:
                price = quote.prev_settle
            elif quote.settle > 0:
                price = quote.settle
            elif self._config.account.settle_use_last and quote.last > 0:
                price = quote.last
            if price > 0:
                prices[position.symbol] = price
        if not prices:
            self._logger.warning("跨日补结算缺少昨结算价（%s）", settle_date)
            return
        records = self._ledger.settle(
            settle_date=settle_date,
            ts=now.isoformat(timespec="seconds"),
            settle_prices=prices,
        )
        settled_symbols = {record.symbol for record in self._ledger.settlements_for(settle_date)}
        remaining = [position for position in self._ledger.positions() if position.symbol not in settled_symbols]
        if remaining:
            self._mark_settle_pending(settle_date, now)
            self._logger.warning("跨日补结算未完成：%s 仍有 %s", settle_date, [p.symbol for p in remaining])
            return
        self._clear_settle_pending(settle_date)
        self._ledger.set_meta(f"settle_gap_audit:{settle_date}", now.isoformat(timespec="seconds"))
        self._logger.info("跨日补结算完成：%s（使用昨结算）", settle_date)
        if records and self._config.notify.close_report_enabled:
            equity = self._ledger.equity(self._valuation_marks()[0])
            await self._safe_notify(report.format_settlement(settle_date, records, equity))
            self._settle_notified.add(settle_date)

    async def _settlement_tick(self, now: datetime) -> None:
        if not self._config.account.settle_enabled:
            return
        settle_date = self._settlement_due_date(now)
        if settle_date is None:
            return
        settle_text = settle_date.isoformat()
        positions = self._ledger.positions()
        if not positions:
            for pending_date in self._settle_pending_dates():
                if pending_date <= settle_text:
                    self._clear_settle_pending(pending_date)
            return
        settled = {record.symbol for record in self._ledger.settlements_for(settle_text)}
        if all(position.symbol in settled for position in positions):
            self._clear_settle_pending(settle_text)
            return
        gate_text = self._ledger.get_meta(f"settle_next_try:{settle_text}", "")
        if gate_text:
            try:
                if now < datetime.fromisoformat(gate_text):
                    return
            except ValueError:
                pass
        symbols = [position.symbol for position in positions]
        settle_prices: dict[str, float] = {}
        if self._settlement_provider is not None:
            try:
                official = await self._settlement_provider(settle_text, symbols)
            except Exception as exc:  # noqa: BLE001 - 官方源失败回退行情
                self._logger.warning("官方结算价获取失败，回退行情：%s", exc)
                official = {}
            for symbol, price in (official or {}).items():
                if symbol in symbols and float(price) > 0:
                    settle_prices[symbol] = float(price)
        missing = [symbol for symbol in symbols if symbol not in settle_prices]
        if missing:
            batch = await self._fetch(missing)
            if batch is not None:
                for symbol, quote in batch.quotes.items():
                    if symbol not in missing:
                        continue
                    price = quote.settle if quote.settle > 0 else (
                        quote.last if self._config.account.settle_use_last else 0.0
                    )
                    if price <= 0:
                        continue
                    same_date = quote.quote_time.date() == settle_date
                    recent_overnight = (
                        self._config.account.settle_use_last
                        and (now.date() - quote.quote_time.date()).days <= 1
                    )
                    if same_date or recent_overnight:
                        settle_prices[symbol] = price
        if not settle_prices:
            first = self._mark_settle_pending(settle_text, now)
            if first:
                await self._safe_notify("⚠️ 今日结算价暂缺（结算价字段为 0），稍后自动重试；恢复前禁止开新仓。")
            self._logger.warning("本轮没有可用的当日结算价（%s）", settle_text)
            return
        records = self._ledger.settle(
            settle_date=settle_text,
            ts=now.isoformat(timespec="seconds"),
            settle_prices=settle_prices,
        )
        settled_symbols = {record.symbol for record in self._ledger.settlements_for(settle_text)}
        remaining = [position for position in self._ledger.positions() if position.symbol not in settled_symbols]
        if remaining:
            first = self._mark_settle_pending(settle_text, now)
            if first:
                await self._safe_notify(
                    "⚠️ 每日结算部分完成：部分合约缺少结算价，稍后自动重试；恢复前禁止开新仓。"
                )
            self._logger.warning("部分合约缺结算价：%s", [position.symbol for position in remaining])
            return
        self._clear_settle_pending(settle_text)
        marks = self._valuation_marks()[0]
        equity = self._ledger.mark_equity(now.isoformat(timespec="seconds"), marks)
        if records and settle_text not in self._settle_notified:
            self._settle_notified.add(settle_text)
            if self._config.notify.close_report_enabled:
                await self._safe_notify(report.format_settlement(settle_text, records, equity))
            self._logger.info("完成每日结算：%s", settle_text)

    def _settlement_due_date(self, now: datetime) -> date | None:
        """返回本轮应结算的交易日；结算日不随 20:00 夜盘交易日翻日。"""

        try:
            settle_time = time.fromisoformat(self._config.account.settle_time)
        except ValueError:
            self._logger.warning("结算时间配置非法：%s", self._config.account.settle_time)
            return None
        if now.time() < settle_time:
            return None
        if self._calendar_market == "cn":
            # 20:00 后 _trading_day 已翻到下一交易日，但 15:10 结算的是当天。
            if not market_calendar.is_trading_day("cn", now.date()):
                return None
            return now.date()
        # 国际市场 05:10 结算刚结束的上一交易日；周六/周一凌晨也能补周五。
        candidate = now.date() - timedelta(days=1)
        for _ in range(8):
            if market_calendar.is_trading_day(self._calendar_market, candidate):
                return candidate
            candidate -= timedelta(days=1)
        return None

    # ---------- 试跑/通知 ----------

    async def dry_run(self) -> str:
        """只跑一轮决策与校验，不下单、不播报、不写账本（仍消耗 LLM 预算）。"""

        if not self._config.decision.llm_enabled:
            return "试跑不可用：配置已关闭 LLM 决策（llm_enabled=false）。"
        now = self._clock()
        batch = await self._fetch(list(self._specs))
        if batch is None:
            return "试跑失败：行情取不到。"
        fresh = {
            symbol: quote
            for symbol, quote in batch.quotes.items()
            if symbol in self._specs
            and quote_is_usable(quote, max_abs_change_ratio=self._config.risk.max_abs_price_change_ratio)
        }
        if not fresh:
            return "试跑失败：本轮无可用行情。"
        influence_text = ""
        if self._influence is not None:
            try:
                influence_text = await self._influence.gather(list(self._specs))
            except Exception as exc:  # noqa: BLE001 - 影响源失败不影响试跑
                self._logger.warning("试跑收集影响源失败：%s", exc)
        decision_input = self._build_decision_input(now, fresh, influence_text=influence_text)
        try:
            decision = await self._decider.decide(decision_input)
        except BudgetExhausted as exc:
            return f"试跑未执行：{exc}"
        except DecisionError as exc:
            return f"试跑失败：{exc}"
        if decision is None:
            return "试跑：模型没有给出可解析的决策（按 hold 处理）。"
        lines = [
            f"动作：{decision.action}｜合约：{decision.symbol or '-'}｜手数：{decision.lots}",
            f"理由：{decision.reason or '（空）'}",
        ]
        if influence_text:
            lines.append("参考材料（本轮已注入，不落盘）：")
            lines.append(influence_text[:500])
        else:
            lines.append("参考材料：无（影响源关闭或本轮无相关内容）")
        if decision.action != "hold" and decision.symbol in self._specs:
            spec = self._specs[decision.symbol]
            quote = fresh.get(decision.symbol)
            if quote is not None:
                lines.append(f"参考：买一 {quote.bid1:.4g} / 卖一 {quote.ask1:.4g} / 最新 {quote.last:.4g}")
            action = decision.action
            lots = decision.lots
            if action == "close":
                position = self._ledger.position(decision.symbol)
                if position is None:
                    lines.append("风控：会拒绝（无持仓可平）")
                else:
                    action = "close_long" if position.side == "long" else "close_short"
                    lots = position.lots
            equity = self._ledger.equity(self._marks_from(fresh))
            context = self._risk_context(now, decision.symbol, decision_input, equity, spec)
            if action.startswith("open"):
                verdict = check_open(
                    symbol=decision.symbol,
                    lots=lots,
                    spec=spec,
                    context=context,
                    config=self._config.risk,
                    side="long" if action == "open_long" else "short",
                    allow_short=self._config.account.allow_short,
                )
            else:
                verdict = check_close(symbol=decision.symbol, spec=spec, context=context)
            lines.append(
                "风控：" + ("通过（实际执行时会重新取价并再次校验）" if verdict.allowed else f"会拒绝：{verdict.reason}")
            )
        return report.format_dry_run("\n".join(lines))

    async def _safe_notify(self, text: str) -> None:
        if not text:
            return
        try:
            await self._notify(text)
        except Exception as exc:  # noqa: BLE001 - 播报失败只记录
            self._logger.warning("播报失败：%s", exc)
