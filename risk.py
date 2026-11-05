"""风控规则（纯函数，便于单测）：所有最终否决都在代码里，LLM 不参与。

修复点（Codex 评审 P1）：
- 接入 ``allow_short`` / ``warmup_samples`` 等配置；
- 估值不完整、待结算对账未完成、合约临近/超过到期日时禁止开新仓；
- 平仓不受上述开仓限制影响（除交易时段与持仓存在性）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .config import RiskConfig
from .contracts import ContractSpec, front_name
from .ledger import Account, Position


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class RiskContext:
    """一次交易意图校验所需的账户/市场状态。"""

    account: Account
    positions: list[Position]
    equity: float
    margin_used: float
    reference_price: float
    now: datetime
    session_open: bool
    atr: float | None
    last_open_ts: str = ""
    today: date | None = None
    sample_count: int = 0
    required_samples: int = 0
    valuation_ok: bool = True
    settlement_pending: bool = False


def daily_loss_hit(*, equity: float, day_start_equity: float, limit_ratio: float) -> bool:
    """当日亏损是否达到停手阈值（按起始权益比例）。"""

    if day_start_equity <= 0 or limit_ratio <= 0:
        return False
    loss_ratio = (day_start_equity - equity) / day_start_equity
    return loss_ratio >= limit_ratio


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def check_open(
    *,
    symbol: str,
    lots: int,
    spec: ContractSpec,
    context: RiskContext,
    config: RiskConfig,
    side: str = "long",
    allow_short: bool = True,
) -> RiskVerdict:
    """开新仓校验。任一条件不满足就拒绝，并给出可读原因。"""

    if not context.session_open:
        return RiskVerdict(False, "当前不在交易时段")
    if context.account.halted:
        return RiskVerdict(False, "今日已触发亏损停手")
    if not context.valuation_ok:
        return RiskVerdict(False, "估值不完整（有持仓缺少有效行情），禁止新增风险")
    if context.settlement_pending:
        return RiskVerdict(False, "存在未完成的每日结算对账，禁止开新仓")
    if side not in ("long", "short"):
        return RiskVerdict(False, f"方向非法：{side}")
    if side == "short" and not allow_short:
        return RiskVerdict(False, "配置禁止做空（allow_short=false）")

    today = context.today or context.now.date()
    if spec.is_expired(today):
        return RiskVerdict(False, f"{front_name(spec.symbol)} 已过最后交易日，禁止开仓")
    trading_days_left = spec.trading_days_to_last(today)
    if trading_days_left <= int(config.expiry_block_days):
        return RiskVerdict(
            False,
            f"{front_name(spec.symbol)} 距最后交易日仅剩 {trading_days_left} 个交易日"
            f"（阈值 {config.expiry_block_days}），禁止开新仓",
        )

    if context.reference_price <= 0:
        return RiskVerdict(False, "参考价非法")
    for position in context.positions:
        if position.symbol == symbol:
            return RiskVerdict(False, f"{front_name(symbol)} 已有持仓，不支持加仓")
    if not isinstance(lots, int) or lots <= 0:
        return RiskVerdict(False, f"手数非法：{lots!r}")
    if lots > config.max_lots_per_symbol:
        return RiskVerdict(False, f"单合约手数 {lots} 超过上限 {config.max_lots_per_symbol}")
    if context.account.trades_today >= config.daily_trade_limit:
        return RiskVerdict(False, f"今日开仓次数已达上限 {config.daily_trade_limit}")
    if context.atr is None or context.atr <= 0:
        return RiskVerdict(False, "行情样本不足，无法计算 ATR")
    if context.required_samples > 0 and context.sample_count < context.required_samples:
        return RiskVerdict(
            False,
            f"预热样本不足（{context.sample_count}/{context.required_samples}）",
        )

    last_open = _parse_iso(context.last_open_ts)
    if last_open is not None and config.cooldown_minutes_per_symbol > 0:
        now = context.now.replace(tzinfo=None) if context.now.tzinfo is not None else context.now
        opened = last_open.replace(tzinfo=None) if last_open.tzinfo is not None else last_open
        if (now - opened).total_seconds() < config.cooldown_minutes_per_symbol * 60:
            return RiskVerdict(False, f"{symbol} 开仓冷却中（{config.cooldown_minutes_per_symbol} 分钟）")

    new_margin = context.reference_price * spec.multiplier * lots * spec.margin_rate
    if context.equity <= 0:
        return RiskVerdict(False, "权益为 0，无法开仓")
    margin_ratio = (context.margin_used + new_margin) / context.equity
    if margin_ratio > config.max_total_margin_ratio:
        return RiskVerdict(
            False,
            f"保证金占用将达 {margin_ratio:.1%}，超过上限 {config.max_total_margin_ratio:.0%}",
        )
    if len(context.positions) >= config.max_positions:
        return RiskVerdict(False, f"持仓数量已达上限 {config.max_positions}")
    return RiskVerdict(True)


def check_close(
    *,
    symbol: str,
    spec: ContractSpec,
    context: RiskContext,
) -> RiskVerdict:
    """平仓校验：只要求交易时段与存在持仓；停手/待结算/估值不完整不阻止平仓。"""

    if not context.session_open:
        return RiskVerdict(False, "当前不在交易时段")
    if not any(position.symbol == symbol for position in context.positions):
        return RiskVerdict(False, f"{symbol} 无持仓")
    if context.reference_price <= 0:
        return RiskVerdict(False, "参考价非法")
    return RiskVerdict(True)


def expiry_force_close_required(spec: ContractSpec, today: date) -> bool:
    """是否需要在今天强制平仓（最后交易日或已过期）。"""

    return spec.is_expired(today) or spec.trading_days_to_last(today) <= 1
