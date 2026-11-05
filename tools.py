"""只读工具输出：账户快照与聊天用市场概览。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import report
from .contracts import category_for, front_name

CST = ZoneInfo("Asia/Shanghai")
_CATEGORY_ORDER = ("能源", "贵金属", "有色", "黑色", "化工", "农产品", "其他")


async def snapshot(runtimes: list[Any], view: str) -> dict[str, Any]:
    """futures_snapshot 的实现：status/positions/trades/overview。"""

    normalized = str(view or "status").strip().lower()
    if normalized == "overview":
        text = await market_overview(runtimes)
        return {"success": True, "content": text}
    if normalized not in {"status", "positions", "trades"}:
        normalized = "status"
    sections: list[str] = []
    for runtime in runtimes:
        ledger = runtime.ledger
        if normalized == "trades":
            text = report.format_trades(ledger.recent_trades(10))
        else:
            marks = runtime.engine.latest_marks()
            account = ledger.account()
            recent = ledger.recent_decisions(1)
            text = report.format_state(
                account=account,
                positions=ledger.positions(),
                equity=ledger.equity(marks),
                quotes=marks,
                paused=bool(getattr(runtime.engine, "paused", False)),
                halted=account.halted,
                auto_trade=bool(runtime.config.decision.auto_trade),
                last_decision=recent[0] if recent else None,
                market_label=runtime.profile.label,
                currency=runtime.profile.currency,
            )
        sections.append(f"【{runtime.profile.label}】\n{report.strip_disclaimer(text)}")
    return {"success": True, "content": report.append_disclaimer("\n\n".join(sections))}


async def market_overview(runtimes: list[Any]) -> str:
    """拉取各市场代表合约行情，按大类汇总成聊天可读的概览。"""

    blocks: list[str] = []
    now_text = datetime.now(CST).strftime("%m-%d %H:%M")
    for runtime in runtimes:
        profile = runtime.profile
        specs = runtime.engine.specs
        symbols = list(specs)
        try:
            batch = await runtime.quote_source.fetch(symbols)
            quotes = batch.quotes
            errors = batch.errors
        except Exception as exc:  # noqa: BLE001 - 概览失败不影响账户
            blocks.append(f"【{profile.label}】\n行情暂不可用：{exc}")
            continue

        rows_by_category: dict[str, list[tuple[str, float, float]]] = {}
        for symbol in symbols:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            base = quote.prev_close if quote.prev_close > 0 else quote.prev_settle
            pct = 0.0
            if base > 0:
                pct = (quote.last - base) / base * 100.0
            spec = specs[symbol]
            display = str(getattr(spec, "display_name", "") or front_name(symbol))
            rows_by_category.setdefault(category_for(symbol), []).append(
                (display, quote.last, pct)
            )

        if not rows_by_category:
            reason = "；".join(errors.values()) if errors else "没有可用行情"
            blocks.append(f"【{profile.label}】\n行情暂不可用：{reason}")
            continue

        lines = [f"【{profile.label}】"]
        for category in _CATEGORY_ORDER:
            rows = rows_by_category.get(category)
            if not rows:
                continue
            lines.append("")
            lines.append(category)
            for name, price, pct in rows:
                lines.append(f"  {name} {price:g} ({pct:+.2f}%)")
        blocks.append("\n".join(lines))

    header = f"📊 市场概览 {now_text}"
    return header + "\n\n" + "\n\n".join(blocks)
