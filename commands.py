"""命令处理：解析 /期货 子命令、权限校验、生成回复文本。

权限：查询类任何人可用（但仍受聊天流白名单约束，由 plugin 层先过滤）；
控制类（试跑/暂停/恢复/重置/买/卖/平）只允许管理员白名单或宿主本地操作员。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from . import report
from .config import MaiFuturesConfig
from .contracts import ContractError, ContractSpec, get_contract
from .ledger import Ledger
from .quotes import QuoteFetchError, SinaQuoteSource

_COMMAND_RE = re.compile(r"^/(?:期货|futures)\s*(?P<rest>.*)$", re.IGNORECASE)
_ALIASES = {
    "": "state",
    "状态": "state",
    "status": "state",
    "账户": "state",
    "持仓": "state",
    "行情": "quote",
    "quote": "quote",
    "价": "quote",
    "记录": "trades",
    "history": "trades",
    "成交": "trades",
    "概览": "overview",
    "overview": "overview",
    "市场": "overview",
    "帮助": "help",
    "help": "help",
    "试跑": "dry",
    "dry": "dry",
    "演练": "dry",
    "暂停": "pause",
    "pause": "pause",
    "恢复": "resume",
    "resume": "resume",
    "重置": "reset",
    "reset": "reset",
    "买": "buy",
    "买入": "buy",
    "buy": "buy",
    "卖": "sell",
    "卖出": "sell",
    "sell": "sell",
    "平": "close",
    "平仓": "close",
    "close": "close",
}
_CONTROL_SUBS = {"dry", "pause", "resume", "reset", "buy", "sell", "close"}
_OWNER_SUBS = _CONTROL_SUBS  # 向后兼容旧名字
_MANUAL_SUBS = {"buy", "sell", "close"}


class CommandError(ValueError):
    """命令解析错误。"""


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    text: str | None = None


@dataclass
class CommandServices:
    """命令处理依赖（由 plugin 注入）。"""

    ledger: Ledger
    specs: dict[str, ContractSpec]
    quote_source: SinaQuoteSource
    engine: Any
    config: MaiFuturesConfig
    now: Callable[[], datetime]
    market_label: str = ""
    currency: str = "麦麦币"
    resolve_symbol: Callable[[str], str | None] | None = None


def resolve_sub(raw_sub: str) -> str | None:
    """把用户输入的子命令归一化（供 plugin 多市场路由复用）。"""

    return _ALIASES.get(str(raw_sub or "").lower()) or _ALIASES.get(str(raw_sub or ""))


def parse_command(text: str) -> tuple[str, str] | None:
    """解析 ``/期货 [子命令] [参数]``；不是本插件命令返回 None。"""

    if not isinstance(text, str) or not text.strip():
        return None
    match = _COMMAND_RE.match(text.strip())
    if match is None:
        return None
    rest = match.group("rest").strip()
    if not rest:
        return "", ""
    parts = rest.split(maxsplit=1)
    sub = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    # 兼容“买RB2701 / 平RB2701”这类无空格写法：按最长别名前缀拆分
    if sub not in _ALIASES:
        for alias in sorted((item for item in _ALIASES if item), key=len, reverse=True):
            if sub.startswith(alias) and len(sub) > len(alias):
                remainder = sub[len(alias) :]
                arg = (remainder + (" " + arg if arg else "")).strip()
                sub = alias
                break
    return sub, arg


def is_admin(*, users: list[str], platform: str, user_id: str, is_local_operator: bool = False) -> bool:
    """判断是否管理员：本地操作员或 ``platform:user_id`` 白名单。"""

    if is_local_operator:
        return True
    platform_text = str(platform or "").strip().lower()
    user_text = str(user_id or "").strip()
    if not platform_text or not user_text:
        return False
    target = f"{platform_text}:{user_text}"
    return target in {str(item).strip().lower() for item in users or []}


def is_owner(*, owners: list[str], platform: str, user_id: str, is_local_operator: bool = False) -> bool:
    """旧名字，等价于 is_admin（0.2 起只有管理员一种角色）。"""

    return is_admin(users=owners, platform=platform, user_id=user_id, is_local_operator=is_local_operator)


def admin_ids(config: MaiFuturesConfig) -> list[str]:
    """管理员名单（0.2 起唯一权限来源）。"""

    return [str(item).strip() for item in (config.admin.users or []) if str(item).strip()]


def _latest_marks(engine: Any) -> dict[str, float]:
    getter = getattr(engine, "latest_marks", None)
    if callable(getter):
        return getter()
    return {}


async def handle_command(
    text: str,
    *,
    stream_id: str,
    group_id: str,
    platform: str,
    user_id: str,
    is_local_operator: bool,
    services: CommandServices,
) -> CommandResult:
    """执行命令并返回要发送的文本（None 表示不回复）。"""

    del stream_id, group_id
    parsed = parse_command(text)
    if parsed is None:
        return CommandResult(False, None)
    raw_sub, arg = parsed
    sub = _ALIASES.get(raw_sub.lower(), None)
    if sub is None and raw_sub:
        sub = _ALIASES.get(raw_sub, None)
    if sub is None:
        return CommandResult(True, "未知子命令。\n" + report.format_help())
    if sub in _CONTROL_SUBS and not is_admin(
        users=admin_ids(services.config),
        platform=platform,
        user_id=user_id,
        is_local_operator=is_local_operator,
    ):
        return CommandResult(True, "这个命令只有管理员能用（可在插件设置的白名单里添加）。")

    now = services.now()
    ledger = services.ledger
    try:
        if sub == "state":
            account = ledger.account()
            positions = ledger.positions()
            equity = ledger.equity(_latest_marks(services.engine))
            paused = bool(getattr(services.engine, "paused", False))
            recent = ledger.recent_decisions(1)
            return CommandResult(
                True,
                report.format_state(
                    account=account,
                    positions=positions,
                    equity=equity,
                    quotes=_latest_marks(services.engine),
                    paused=paused,
                    halted=account.halted,
                    auto_trade=bool(services.config.decision.auto_trade),
                    last_decision=recent[0] if recent else None,
                    market_label=services.market_label,
                    currency=services.currency,
                ),
            )

        if sub == "quote":
            symbols = list(services.specs)
            if arg:
                lookup = arg
                if callable(services.resolve_symbol):
                    lookup = services.resolve_symbol(arg) or arg
                try:
                    spec = get_contract(lookup, services.specs)
                except ContractError:
                    available = "、".join(
                        services.specs[s].display_name or services.specs[s].name
                        for s in symbols
                    )
                    return CommandResult(
                        True, f"不认识的品种：{arg}。可用品种：{available}"
                    )
                symbols = [spec.symbol]
            batch = await services.quote_source.fetch(symbols)
            rows = []
            for symbol in symbols:
                quote = batch.quotes.get(symbol)
                if quote is None:
                    continue
                rows.append(
                    (
                        symbol,
                        quote.last,
                        quote.bid1,
                        quote.ask1,
                        quote.quote_time.strftime("%H:%M:%S"),
                    )
                )
            if not rows:
                return CommandResult(True, "行情暂时取不到：" + "；".join(batch.errors.values() or ["未知错误"]))
            return CommandResult(True, report.format_quotes(rows))

        if sub == "trades":
            limit = 5
            if arg:
                try:
                    limit = max(1, min(20, int(arg)))
                except ValueError:
                    return CommandResult(True, "记录条数必须是 1-20 的整数。")
            return CommandResult(True, report.format_trades(ledger.recent_trades(limit)))

        if sub == "dry":
            dry_runner = getattr(services.engine, "dry_run", None)
            if not callable(dry_runner):
                return CommandResult(True, "当前版本不支持试跑。")
            return CommandResult(True, await dry_runner())

        if sub == "pause":
            services.engine.set_paused(True)
            return CommandResult(
                True,
                "⏸ 已暂停自动交易（在途决策已作废；行情/风控/结算仍继续；恢复用 /期货 恢复）。",
            )

        if sub == "resume":
            services.engine.set_paused(False)
            return CommandResult(True, "▶️ 已恢复自动交易。")

        if sub == "reset":
            if arg.strip() not in {"确认", "確認", "confirm", "CONFIRM"}:
                return CommandResult(True, "重置会清空虚拟账户与全部成交记录。确认请发送：/期货 重置 确认")
            ledger.reset(services.config.account.initial_balance, day=now.strftime("%Y-%m-%d"))
            reset_state = getattr(services.engine, "reset_runtime_state", None)
            if callable(reset_state):
                reset_state()
            return CommandResult(True, "🔄 已重置虚拟账户并重新开户（旧的待结算/在途决策已一并作废）。")

        if sub in _MANUAL_SUBS:
            parts = arg.split()
            if not parts:
                return CommandResult(True, "用法：/期货 买 RB2701 [手数]｜/期货 卖 RB2701 [手数]｜/期货 平 RB2701")
            symbol = parts[0].strip().upper()
            if callable(services.resolve_symbol):
                symbol = services.resolve_symbol(parts[0]) or symbol
            lots = 1
            if len(parts) > 1:
                try:
                    lots = int(parts[1])
                except ValueError:
                    return CommandResult(True, f"手数必须是整数：{parts[1]!r}")
            manual_order = getattr(services.engine, "manual_order", None)
            if not callable(manual_order):
                return CommandResult(True, "当前版本不支持管理员手动操作。")
            outcome, detail = await manual_order(
                action=sub,
                symbol=symbol,
                lots=lots,
                operator=f"{platform}:{user_id}",
            )
            if outcome in ("opened", "closed"):
                return CommandResult(True, f"✅ 已执行管理员操作：{detail}")
            return CommandResult(True, f"这次没有成交：{detail}")

        return CommandResult(True, report.format_help())
    except QuoteFetchError as exc:
        return CommandResult(True, f"行情请求失败：{exc}")
    except Exception as exc:  # noqa: BLE001 - 命令层兜底，避免异常抛给宿主
        return CommandResult(True, f"命令执行出错：{type(exc).__name__}: {exc}")
