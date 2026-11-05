"""播报文本（纯文本；不依赖图片/合并转发）。

原则：
- 交易事件短、可核对（合约/方向/手数/价格/手续费）；
- 收盘总结附权益与持仓，每日最多一条；
- 明确“虚拟盘，不构成投资建议”。
"""

from __future__ import annotations

from .contracts import ContractSpec, front_name
from .ledger import Account, DecisionRecord, Equity, Position, Settlement, Trade

DISCLAIMER = "（模拟盘：行情为真实数据，合约参数为近似值，成交为模拟撮合，仅供娱乐，不构成投资建议）"
COIN = "麦麦币"
_ACTION_TEXT = {
    "open_long": "开多",
    "open_short": "开空",
    "close_long": "平多",
    "close_short": "平空",
    "close": "平仓",
    "hold": "观望",
}


def _action_text(action) -> str:
    return _ACTION_TEXT.get(str(action or ""), str(action or "-"))


def append_disclaimer(text: str) -> str:
    """给任意用户可见回复追加统一声明；已包含则不重复。"""

    body = str(text or "").rstrip()
    if not body:
        return DISCLAIMER
    if DISCLAIMER in body:
        return body
    return f"{body}\n{DISCLAIMER}"


def strip_disclaimer(text: str) -> str:
    """去掉文本中的统一声明（用于多市场拼接后再统一追加）。"""

    return str(text or "").replace(DISCLAIMER, "").rstrip()


def fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def fmt_signed(value: float) -> str:
    return f"{value:+,.2f}"


def _side_text(side: str) -> str:
    return "多" if side == "long" else "空"


def format_open(trade: Trade, spec: ContractSpec, equity: Equity, *, speech: str = "") -> str:
    """开仓播报。"""

    direction = "开多" if trade.side == "long" else "开空"
    lines = [
        f"📈 {direction} {spec.display_name or front_name(trade.symbol)} {trade.lots} 手 @ {trade.price:.4g}",
        f"手续费 {trade.fee:.2f}｜权益 {fmt_money(equity.equity)} {COIN}｜保证金占用 {fmt_money(equity.margin_used)} {COIN}",
    ]
    if speech:
        lines.append(f"麦麦：{speech}")
    return append_disclaimer("\n".join(lines))


def format_close(trade: Trade, spec: ContractSpec, equity: Equity, *, speech: str = "", reason: str = "") -> str:
    """平仓播报。"""

    direction = "平多" if trade.side == "long" else "平空"
    lines = [
        f"📉 {direction} {spec.display_name or front_name(trade.symbol)} {trade.lots} 手 @ {trade.price:.4g}",
        f"本笔盈亏 {fmt_signed(trade.total_pnl or trade.realized_pnl)} {COIN}（含结算，开仓价 {trade.open_price or trade.price:.4g}）"
        f"｜手续费 {trade.fee:.2f}｜权益 {fmt_money(equity.equity)} {COIN}",
    ]
    if reason:
        lines.append(f"原因：{reason}")
    if speech:
        lines.append(f"麦麦：{speech}")
    return append_disclaimer("\n".join(lines))


def format_settlement(settle_date: str, records: list[Settlement], equity: Equity) -> str:
    """每日结算播报。"""

    lines = [f"🧾 {settle_date} 每日结算完成（按交易所结算价）"]
    for record in records:
        lines.append(f"- {front_name(record.symbol)}：结算价 {record.settle_price:.4g}｜本日盈亏 {fmt_signed(record.pnl)} {COIN}")
    lines.append(f"结算后权益 {fmt_money(equity.equity)} {COIN}（结算只把浮动盈亏转为已实现，权益不变）")
    return append_disclaimer("\n".join(lines))


def format_state(
    *,
    account: Account,
    positions: list[Position],
    equity: Equity,
    quotes: dict[str, float],
    paused: bool,
    halted: bool,
    auto_trade: bool = True,
    last_decision: DecisionRecord | None = None,
    market_label: str = "",
    currency: str = "麦麦币",
) -> str:
    """``/期货 状态``。"""

    trading = "自动交易开启" if auto_trade else "自动交易关闭"
    title = f"📊 {market_label}" if market_label else "📊 麦麦期货模拟盘"
    lines = [
        f"{title}｜权益 {fmt_money(equity.equity)} {currency}｜现金 {fmt_money(account.cash)} {currency}",
        f"已用保证金 {fmt_money(equity.margin_used)}｜浮动盈亏 {fmt_signed(equity.floating)}"
        f"｜手续费累计 {fmt_money(account.fees_paid)}",
        f"今日开仓 {account.trades_today} 次｜状态：{'暂停' if paused else '运行中'}"
        f"（{trading}）{'（已停手）' if halted else ''}",
    ]
    if last_decision is None:
        lines.append("最近决策：暂无")
    else:
        reason = " ".join(str(last_decision.reason or "（无理由）").split())
        if len(reason) > 160:
            reason = reason[:160] + "…"
        fallback_note = "（回退）" if str(last_decision.outcome).startswith("fallback") else ""
        lines.append(
            f"最近决策：{last_decision.ts[11:19]} {_action_text(last_decision.action)}{fallback_note}｜理由：{reason}"
        )
    if positions:
        lines.append("持仓：")
        for position in positions:
            mark = quotes.get(position.symbol)
            floating = ""
            if mark:
                direction = 1 if position.side == "long" else -1
                floating = f"｜浮动 {fmt_signed((mark - position.entry_price) * position.multiplier * position.lots * direction)}"
            cost_note = ""
            if position.open_price and abs(position.open_price - position.entry_price) > 1e-9:
                cost_note = f"（原始开仓 {position.open_price:.4g}）"
            lines.append(
                f"- {front_name(position.symbol)} {_side_text(position.side)} {position.lots} 手"
                f"｜开仓 {position.entry_price:.4g}{cost_note}｜保证金 {fmt_money(position.margin)}"
                f"｜止损 {position.stop_price:.4g}｜止盈 {position.take_price:.4g}{floating}"
            )
    else:
        lines.append("持仓：无（空仓）")
    return append_disclaimer("\n".join(lines))


def format_quotes(quotes: list[tuple[str, float, float, float, str]]) -> str:
    """``/期货 行情``；参数为 (合约, 最新, 买一, 卖一, 时间)。"""

    lines = ["📈 行情快照："]
    for symbol, last, bid1, ask1, when in quotes:
        lines.append(f"- {front_name(symbol)}：最新 {last:.4g}｜买一 {bid1:.4g}｜卖一 {ask1:.4g}｜{when}")
    return append_disclaimer("\n".join(lines))


def format_trades(trades: list[Trade]) -> str:
    """``/期货 记录``。"""

    if not trades:
        return append_disclaimer("📝 最近没有成交记录。")
    lines = ["📝 最近成交："]
    for trade in trades:
        direction = "开" if trade.action == "open" else "平"
        pnl = fmt_signed(trade.total_pnl or trade.realized_pnl) if trade.action == "close" else "-"
        pnl_text = f"{pnl} {COIN}" if trade.action == "close" else "-"
        lines.append(
            f"- {trade.ts[11:19]} {direction}{_side_text(trade.side)} {front_name(trade.symbol)} {trade.lots} 手"
            f" @{trade.price:.4g}｜费 {trade.fee:.2f} {COIN}｜盈亏 {pnl_text}"
        )
    return append_disclaimer("\n".join(lines))


def format_help() -> str:
    """``/期货 帮助``。"""

    return append_disclaimer(
        "\n".join(
            [
                "麦麦期货模拟盘 · 命令",
                "/期货 状态       账户、持仓、权益（所有人）",
                "/期货 行情 [合约] 行情快照（所有人）",
                "/期货 概览       国内+国际市场行情概览（所有人）",
                "/期货 记录 [N]   最近成交（所有人）",
                "/期货 帮助       本帮助（所有人）",
                "/期货 试跑       只决策不下单（管理员）",
                "/期货 暂停 / 恢复 暂停或恢复自动交易（管理员）",
                "/期货 买 螺纹钢 [手数] / 卖 / 平（管理员，前台用品种名）",
                "/期货 重置 [cn|intl] 确认   账户重置为初始状态（管理员）",
                "管理员名单在插件设置页维护；自动交易开关在插件设置页。",
                "市场档案在插件设置页选择：cn=国内，intl=国际，可同时启用。",
                "说明：交易为模拟，不接真实账户；行情为真实数据，合约参数为近似值。",
            ]
        )
    )


def format_reject(reason: str) -> str:
    return append_disclaimer(f"这次没有成交：{reason}")


def format_dry_run(decision_text: str) -> str:
    return append_disclaimer(f"🧪 试跑（未下单）：\n{decision_text}")
