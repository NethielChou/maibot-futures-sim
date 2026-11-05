"""成交规则：T0 决策 → T1 重新取价 → 模拟成交（按买一/卖一价 + 滑点）。

明确不做：
- 不推测两次快照之间的限价；
- 行情过期/买卖价异常/漂移过大 → 不成交；
- 首版无挂单、无部分成交、无涨跌停推断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import ContractSpec
from .quotes import Quote

BUY_ACTIONS = {"open_long", "close_short"}
SELL_ACTIONS = {"open_short", "close_long"}
ALL_ACTIONS = BUY_ACTIONS | SELL_ACTIONS | {"hold"}


class ExecutionError(ValueError):
    """成交参数非法。"""


@dataclass(frozen=True)
class FillPlan:
    """模拟成交结果（仅价格与费用，账本负责记账）。"""

    action: str
    symbol: str
    lots: int
    price: float
    fee: float
    notional: float
    margin: float
    reason: str = ""


def action_is_buy(action: str) -> bool:
    if action not in ALL_ACTIONS:
        raise ExecutionError(f"未知动作：{action}")
    return action in BUY_ACTIONS


def check_fill_ready(
    *,
    quote: Quote | None,
    t0_mid: float,
    now: datetime,
    spec: ContractSpec,
    max_quote_age_seconds: int,
    max_drift_ticks: int,
) -> str:
    """检查 T1 行情是否可用于成交；返回拒绝原因，空字符串表示可成交。"""

    if quote is None:
        return "缺少 T1 行情"
    if quote.symbol != spec.symbol:
        return f"行情合约不匹配：{quote.symbol} != {spec.symbol}"
    age = quote.age_seconds(now)
    if age < -120:
        return f"行情时间戳在未来（{age:.1f}s），拒绝使用"
    if age > max_quote_age_seconds:
        return f"行情过期（{age:.1f}s > {max_quote_age_seconds}s）"
    if not quote.has_tradable_book():
        return "买一/卖一价缺失、倒挂或对手盘无挂单量"
    if t0_mid > 0:
        drift_ticks = abs(quote.mid - t0_mid) / spec.tick_size
        if drift_ticks > max_drift_ticks:
            return f"价格漂移过大（{drift_ticks:.1f} 跳 > {max_drift_ticks} 跳）"
    return ""


def make_fill_plan(
    *,
    action: str,
    quote: Quote,
    spec: ContractSpec,
    lots: int,
    slippage_ticks: int,
) -> FillPlan:
    """按买一/卖一价 + 滑点生成成交计划。"""

    if action not in ALL_ACTIONS or action == "hold":
        raise ExecutionError(f"动作不可成交：{action}")
    if not isinstance(lots, int) or lots <= 0:
        raise ExecutionError(f"手数非法：{lots!r}")
    if not quote.has_tradable_book():
        raise ExecutionError("买一/卖一价不可用或对手盘无挂单量")
    slip = max(0, int(slippage_ticks)) * spec.tick_size
    if action_is_buy(action):
        price = quote.ask1 + slip
    else:
        price = quote.bid1 - slip
    if price <= 0:
        raise ExecutionError(f"成交价非法：{price}")
    price = round(price, 8)
    notional = spec.notional(price, lots)
    return FillPlan(
        action=action,
        symbol=spec.symbol,
        lots=lots,
        price=price,
        fee=round(spec.fee(price, lots), 6),
        notional=round(notional, 6),
        margin=round(spec.margin(price, lots), 6),
    )
