"""SQLite 虚拟账本：账户、持仓、成交、决策、每日结算、权益快照。

设计要点（对应 G1 v0.2 §5 / §7）：
- 单文件 SQLite（stdlib），WAL 模式；每个写操作在一个事务内完成；
- ``order_id`` / ``decision_id`` / ``(settle_date, symbol)`` 唯一约束，防止重复执行；
- 期货开仓不扣名义价值，只扣手续费并占用保证金；平仓按价差结算已实现盈亏；
- 每日结算把浮动盈亏转为已实现并重置开仓价，**结算前后权益不变**；
- 不依赖合约表：持仓行内冗余 multiplier / margin_rate，保证账本自洽。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


class LedgerError(RuntimeError):
    """账本错误基类。"""


class DuplicateOrderError(LedgerError):
    """order_id 已存在（幂等拒绝）。"""


class PositionExistsError(LedgerError):
    """同一合约已有持仓（首版不加仓）。"""


class NoPositionError(LedgerError):
    """无持仓可平。"""


@dataclass(frozen=True)
class Account:
    initial_balance: float
    cash: float
    realized_pnl: float
    fees_paid: float
    day: str
    trades_today: int
    halted: bool
    day_start_equity: float
    schema_version: int


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str
    lots: int
    entry_price: float
    margin: float
    multiplier: float
    margin_rate: float
    opened_at: str
    stop_price: float
    take_price: float
    decision_id: str
    open_price: float = 0.0
    open_fee: float = 0.0


@dataclass(frozen=True)
class Trade:
    order_id: str
    decision_id: str
    ts: str
    symbol: str
    action: str
    side: str
    lots: int
    price: float
    fee: float
    realized_pnl: float
    note: str
    open_price: float = 0.0
    total_pnl: float = 0.0


@dataclass(frozen=True)
class Settlement:
    settle_date: str
    symbol: str
    settle_price: float
    pnl: float
    entry_before: float
    entry_after: float
    ts: str


@dataclass(frozen=True)
class Equity:
    equity: float
    cash: float
    margin_used: float
    floating: float


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    ts: str
    symbol: str
    action: str
    lots: int
    reason: str
    source: str
    validation: str
    outcome: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees_paid REAL NOT NULL DEFAULT 0,
    day TEXT NOT NULL,
    trades_today INTEGER NOT NULL DEFAULT 0,
    halted INTEGER NOT NULL DEFAULT 0,
    day_start_equity REAL NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    side TEXT NOT NULL CHECK (side IN ('long','short')),
    lots INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    margin REAL NOT NULL,
    multiplier REAL NOT NULL,
    margin_rate REAL NOT NULL,
    opened_at TEXT NOT NULL,
    stop_price REAL NOT NULL,
    take_price REAL NOT NULL,
    decision_id TEXT NOT NULL,
    open_price REAL NOT NULL DEFAULT 0,
    open_fee REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    order_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT NOT NULL,
    lots INTEGER NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    open_price REAL NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    lots INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'llm',
    validation TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settlements (
    settle_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    settle_price REAL NOT NULL,
    pnl REAL NOT NULL,
    entry_before REAL NOT NULL,
    entry_after REAL NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (settle_date, symbol)
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    margin_used REAL NOT NULL,
    floating REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _direction(side: str) -> int:
    return 1 if side == "long" else -1


def _round_money(value: float) -> float:
    """金额统一按 2 位小数（分）四舍五入；落库仍为 REAL，但边界值一致。"""

    quantized = Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(quantized)


class Ledger:
    """虚拟账户账本。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """把 schema_version=1 的旧账本升级到 2（新增原始开仓价/总盈亏列）。

        旧库可能为空（当前部署即空库）；仍按“先补列、再回填”的可重入方式处理。
        """

        with self._conn:
            position_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(positions)")}
            if "open_price" not in position_columns:
                self._conn.execute("ALTER TABLE positions ADD COLUMN open_price REAL NOT NULL DEFAULT 0")
                self._conn.execute("UPDATE positions SET open_price = entry_price WHERE open_price = 0")
            if "open_fee" not in position_columns:
                self._conn.execute("ALTER TABLE positions ADD COLUMN open_fee REAL NOT NULL DEFAULT 0")
            trade_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(trades)")}
            if "open_price" not in trade_columns:
                self._conn.execute("ALTER TABLE trades ADD COLUMN open_price REAL NOT NULL DEFAULT 0")
            if "total_pnl" not in trade_columns:
                self._conn.execute("ALTER TABLE trades ADD COLUMN total_pnl REAL NOT NULL DEFAULT 0")
            self._conn.execute(
                "UPDATE account SET schema_version = ? WHERE schema_version < ?",
                (SCHEMA_VERSION, SCHEMA_VERSION),
            )

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._conn.close()

    # ---------- 账户 ----------

    def initialize(self, initial_balance: float, *, day: str) -> None:
        """首次使用时写入账户；已存在则不动。"""

        if self._conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone():
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO account (id, initial_balance, cash, realized_pnl, fees_paid, day, trades_today, halted, day_start_equity, schema_version)"
                " VALUES (1, ?, ?, 0, 0, ?, 0, 0, ?, ?)",
                (float(initial_balance), float(initial_balance), day, float(initial_balance), SCHEMA_VERSION),
            )

    def account(self) -> Account:
        row = self._conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise LedgerError("账户未初始化")
        return Account(
            initial_balance=row["initial_balance"],
            cash=row["cash"],
            realized_pnl=row["realized_pnl"],
            fees_paid=row["fees_paid"],
            day=row["day"],
            trades_today=row["trades_today"],
            halted=bool(row["halted"]),
            day_start_equity=row["day_start_equity"],
            schema_version=row["schema_version"],
        )

    def positions(self) -> list[Position]:
        rows = self._conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [self._position_from_row(row) for row in rows]

    def position(self, symbol: str) -> Position | None:
        row = self._conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        return None if row is None else self._position_from_row(row)

    @staticmethod
    def _position_from_row(row: sqlite3.Row) -> Position:
        return Position(
            symbol=row["symbol"],
            side=row["side"],
            lots=row["lots"],
            entry_price=row["entry_price"],
            margin=row["margin"],
            multiplier=row["multiplier"],
            margin_rate=row["margin_rate"],
            opened_at=row["opened_at"],
            stop_price=row["stop_price"],
            take_price=row["take_price"],
            decision_id=row["decision_id"],
            open_price=row["open_price"] if "open_price" in row.keys() else row["entry_price"],
            open_fee=row["open_fee"] if "open_fee" in row.keys() else 0.0,
        )

    # ---------- 估值 ----------

    def equity(self, marks: dict[str, float] | None = None) -> Equity:
        """按最新标记价计算权益（现金 + 浮动盈亏）。"""

        marks = marks or {}
        account = self.account()
        floating = 0.0
        margin_used = 0.0
        for position in self.positions():
            mark = float(marks.get(position.symbol) or position.entry_price)
            floating += (mark - position.entry_price) * position.multiplier * position.lots * _direction(position.side)
            margin_used += position.margin
        return Equity(
            equity=_round_money(account.cash + floating),
            cash=account.cash,
            margin_used=_round_money(margin_used),
            floating=_round_money(floating),
        )

    def mark_equity(self, ts: str, marks: dict[str, float] | None = None) -> Equity:
        snapshot = self.equity(marks)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity (ts, equity, cash, margin_used, floating) VALUES (?, ?, ?, ?, ?)",
                (ts, snapshot.equity, snapshot.cash, snapshot.margin_used, snapshot.floating),
            )
        return snapshot

    def rollover(self, day: str, marks: dict[str, float] | None = None, *, complete: bool = True) -> bool:
        """跨交易日：重置当日计数/停手状态，记录当日起始权益。

        ``complete=False`` 表示行情/估值不完整（如重启后还没有有效报价），
        此时先按可用估值开盘，并在 meta 标记 ``day_start_pending``，待首次完整估值再校准。
        """

        account = self.account()
        if account.day == day:
            return False
        snapshot = self.equity(marks)
        with self._conn:
            self._conn.execute(
                "UPDATE account SET day = ?, trades_today = 0, halted = 0, day_start_equity = ? WHERE id = 1",
                (day, snapshot.equity),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("day_start_pending", "0" if complete else "1"),
            )
        return True

    def finalize_day_start(self, equity: float) -> None:
        """首次完整估值后校准当日起始权益，并清除 pending 标记。"""

        with self._conn:
            self._conn.execute("UPDATE account SET day_start_equity = ? WHERE id = 1", (_round_money(equity),))
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('day_start_pending', '0')"
            )

    def day_start_pending(self) -> bool:
        return self.get_meta("day_start_pending", "0") == "1"

    def set_halted(self, halted: bool) -> None:
        with self._conn:
            self._conn.execute("UPDATE account SET halted = ? WHERE id = 1", (1 if halted else 0,))

    def reset(self, initial_balance: float, *, day: str) -> None:
        """清空账本并重新开户（owner 命令，需二次确认）。"""

        with self._conn:
            for table in ("positions", "trades", "decisions", "settlements", "equity", "meta"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("DELETE FROM account")
            self._conn.execute(
                "INSERT INTO account (id, initial_balance, cash, realized_pnl, fees_paid, day, trades_today, halted, day_start_equity, schema_version)"
                " VALUES (1, ?, ?, 0, 0, ?, 0, 0, ?, ?)",
                (float(initial_balance), float(initial_balance), day, float(initial_balance), SCHEMA_VERSION),
            )

    # ---------- 交易 ----------

    def try_open(
        self,
        *,
        order_id: str,
        decision_id: str,
        ts: str,
        spec: Any,
        side: str,
        lots: int,
        price: float,
        stop_price: float,
        take_price: float,
    ) -> Trade:
        """开仓：扣手续费、占用保证金、写持仓与成交。"""

        if side not in ("long", "short"):
            raise LedgerError(f"方向非法：{side}")
        if not isinstance(lots, int) or lots <= 0:
            raise LedgerError(f"手数非法：{lots!r}")
        fee = _round_money(spec.fee(price, lots))
        margin = _round_money(spec.margin(price, lots))
        with self._conn:
            if self._conn.execute("SELECT 1 FROM trades WHERE order_id = ?", (order_id,)).fetchone():
                raise DuplicateOrderError(f"重复订单：{order_id}")
            if self._conn.execute("SELECT 1 FROM positions WHERE symbol = ?", (spec.symbol,)).fetchone():
                raise PositionExistsError(f"{spec.symbol} 已有持仓")
            account = self.account()
            self._conn.execute(
                "UPDATE account SET cash = ?, fees_paid = fees_paid + ?, trades_today = trades_today + 1 WHERE id = 1",
                (_round_money(account.cash - fee), fee),
            )
            self._conn.execute(
                "INSERT INTO positions (symbol, side, lots, entry_price, margin, multiplier, margin_rate, opened_at, stop_price, take_price, decision_id, open_price, open_fee)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec.symbol,
                    side,
                    lots,
                    _round_money(price),
                    margin,
                    spec.multiplier,
                    spec.margin_rate,
                    ts,
                    _round_money(stop_price),
                    _round_money(take_price),
                    decision_id,
                    _round_money(price),
                    fee,
                ),
            )
            self._conn.execute(
                "INSERT INTO trades (order_id, decision_id, ts, symbol, action, side, lots, price, fee, realized_pnl, note, open_price, total_pnl)"
                " VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, 0, '', ?, 0)",
                (order_id, decision_id, ts, spec.symbol, side, lots, _round_money(price), fee, _round_money(price)),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (f"last_open:{spec.symbol}", ts),
            )
        return Trade(
            order_id=order_id,
            decision_id=decision_id,
            ts=ts,
            symbol=spec.symbol,
            action="open",
            side=side,
            lots=lots,
            price=_round_money(price),
            fee=fee,
            realized_pnl=0.0,
            note="",
            open_price=_round_money(price),
            total_pnl=0.0,
        )

    def try_close(
        self,
        *,
        order_id: str,
        decision_id: str,
        ts: str,
        spec: Any,
        price: float,
        note: str = "",
    ) -> Trade:
        """平仓：按开仓方向结算价差，扣手续费，删除持仓。"""

        with self._conn:
            position = self.position(spec.symbol)
            if position is None:
                raise NoPositionError(f"{spec.symbol} 无持仓")
            if self._conn.execute("SELECT 1 FROM trades WHERE order_id = ?", (order_id,)).fetchone():
                raise DuplicateOrderError(f"重复订单：{order_id}")
            fee = _round_money(spec.fee(price, position.lots))
            pnl = _round_money(
                (price - position.entry_price)
                * position.multiplier
                * position.lots
                * _direction(position.side)
            )
            open_price = position.open_price or position.entry_price
            open_fee = position.open_fee or 0.0
            total_pnl = _round_money(
                (price - open_price) * position.multiplier * position.lots * _direction(position.side)
                - open_fee
                - fee
            )
            account = self.account()
            self._conn.execute(
                "UPDATE account SET cash = ?, realized_pnl = realized_pnl + ?, fees_paid = fees_paid + ? WHERE id = 1",
                (_round_money(account.cash + pnl - fee), pnl, fee),
            )
            self._conn.execute("DELETE FROM positions WHERE symbol = ?", (spec.symbol,))
            self._conn.execute(
                "INSERT INTO trades (order_id, decision_id, ts, symbol, action, side, lots, price, fee, realized_pnl, note, open_price, total_pnl)"
                " VALUES (?, ?, ?, ?, 'close', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    decision_id,
                    ts,
                    spec.symbol,
                    position.side,
                    position.lots,
                    _round_money(price),
                    fee,
                    pnl,
                    note[:200],
                    _round_money(open_price),
                    total_pnl,
                ),
            )
        return Trade(
            order_id=order_id,
            decision_id=decision_id,
            ts=ts,
            symbol=spec.symbol,
            action="close",
            side=position.side,
            lots=position.lots,
            price=_round_money(price),
            fee=fee,
            realized_pnl=pnl,
            note=note[:200],
            open_price=_round_money(open_price),
            total_pnl=total_pnl,
        )

    # ---------- 每日结算 ----------

    def is_settled(self, settle_date: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM settlements WHERE settle_date = ? LIMIT 1", (settle_date,)
        ).fetchone()
        return row is not None

    def settle(self, *, settle_date: str, ts: str, settle_prices: dict[str, float]) -> list[Settlement]:
        """按当日结算价结算全部持仓（幂等：已结算的合约跳过）。"""

        records: list[Settlement] = []
        with self._conn:
            account = self.account()
            cash = account.cash
            realized = account.realized_pnl
            for position in self.positions():
                price = float(settle_prices.get(position.symbol) or 0.0)
                if price <= 0:
                    continue
                existing = self._conn.execute(
                    "SELECT 1 FROM settlements WHERE settle_date = ? AND symbol = ?",
                    (settle_date, position.symbol),
                ).fetchone()
                if existing:
                    continue
                pnl = _round_money(
                    (price - position.entry_price)
                    * position.multiplier
                    * position.lots
                    * _direction(position.side)
                )
                new_margin = _round_money(
                    price * position.multiplier * position.lots * position.margin_rate
                )
                cash = _round_money(cash + pnl)
                realized = _round_money(realized + pnl)
                self._conn.execute(
                    "UPDATE positions SET entry_price = ?, margin = ? WHERE symbol = ?",
                    (_round_money(price), new_margin, position.symbol),
                )
                self._conn.execute(
                    "INSERT INTO settlements (settle_date, symbol, settle_price, pnl, entry_before, entry_after, ts)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        settle_date,
                        position.symbol,
                        _round_money(price),
                        pnl,
                        position.entry_price,
                        _round_money(price),
                        ts,
                    ),
                )
                records.append(
                    Settlement(
                        settle_date=settle_date,
                        symbol=position.symbol,
                        settle_price=_round_money(price),
                        pnl=pnl,
                        entry_before=position.entry_price,
                        entry_after=_round_money(price),
                        ts=ts,
                    )
                )
            self._conn.execute(
                "UPDATE account SET cash = ?, realized_pnl = ? WHERE id = 1",
                (cash, realized),
            )
        return records

    # ---------- 决策/查询/元数据 ----------

    def record_decision(
        self,
        *,
        decision_id: str,
        ts: str,
        symbol: str,
        action: str,
        lots: int,
        reason: str,
        source: str,
        validation: str = "",
        outcome: str = "",
    ) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO decisions (decision_id, ts, symbol, action, lots, reason, source, validation, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, ts, symbol[:32], action[:32], int(lots), reason[:500], source[:16], validation[:200], outcome[:200]),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def recent_trades(self, limit: int = 5) -> list[Trade]:
        rows = self._conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        return [
            Trade(
                order_id=row["order_id"],
                decision_id=row["decision_id"],
                ts=row["ts"],
                symbol=row["symbol"],
                action=row["action"],
                side=row["side"],
                lots=row["lots"],
                price=row["price"],
                fee=row["fee"],
                realized_pnl=row["realized_pnl"],
                note=row["note"],
                open_price=row["open_price"] if "open_price" in row.keys() else 0.0,
                total_pnl=row["total_pnl"] if "total_pnl" in row.keys() else 0.0,
            )
            for row in rows
        ]

    def recent_decisions(self, limit: int = 5) -> list[DecisionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [DecisionRecord(**dict(row)) for row in rows]

    def settlements_for(self, settle_date: str) -> list[Settlement]:
        rows = self._conn.execute(
            "SELECT * FROM settlements WHERE settle_date = ? ORDER BY symbol", (settle_date,)
        ).fetchall()
        return [
            Settlement(
                settle_date=row["settle_date"],
                symbol=row["symbol"],
                settle_price=row["settle_price"],
                pnl=row["pnl"],
                entry_before=row["entry_before"],
                entry_after=row["entry_after"],
                ts=row["ts"],
            )
            for row in rows
        ]

    def latest_settlement_price(self, symbol: str) -> float | None:
        """最近一次结算价；用于过期且无行情持仓的离线平仓参考。"""

        row = self._conn.execute(
            "SELECT settle_price FROM settlements WHERE symbol = ? ORDER BY settle_date DESC LIMIT 1",
            (str(symbol),),
        ).fetchone()
        return None if row is None else float(row["settle_price"])

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def delete_meta(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM meta WHERE key = ?", (str(key),))

    def last_open_ts(self, symbol: str) -> str:
        return self.get_meta(f"last_open:{symbol}", "") or ""

    def meta_keys(self, prefix: str) -> list[str]:
        rows = self._conn.execute("SELECT key FROM meta WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return [str(row["key"]) for row in rows]

    def check_accounting(self) -> list[str]:
        """账本恒等式与汇总一致性检查（空=通过）。

        恒等式：现金 = 初始资金 + 累计已实现盈亏 − 累计手续费
        汇总：trades.realized_pnl + settlements.pnl = account.realized_pnl；trades.fee = account.fees_paid
        """

        issues: list[str] = []
        account = self.account()
        expected_cash = _round_money(account.initial_balance + account.realized_pnl - account.fees_paid)
        if abs(expected_cash - _round_money(account.cash)) > 0.01:
            issues.append(
                f"现金恒等式不成立：cash={account.cash} != 初始 {account.initial_balance} + 已实现 {account.realized_pnl} − 手续费 {account.fees_paid}"
            )
        trade_row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl, COALESCE(SUM(fee), 0) AS fee FROM trades"
        ).fetchone()
        settle_row = self._conn.execute("SELECT COALESCE(SUM(pnl), 0) AS pnl FROM settlements").fetchone()
        total_pnl = _round_money(float(trade_row["pnl"]) + float(settle_row["pnl"]))
        if abs(total_pnl - _round_money(account.realized_pnl)) > 0.01:
            issues.append(f"成交+结算盈亏（{total_pnl}）与账户已实现（{account.realized_pnl}）不一致")
        if abs(_round_money(float(trade_row["fee"])) - _round_money(account.fees_paid)) > 0.01:
            issues.append(f"成交手续费合计（{trade_row['fee']}）与账户手续费（{account.fees_paid}）不一致")
        for position in self.positions():
            if position.open_price <= 0:
                issues.append(f"{position.symbol} 原始开仓价非法：{position.open_price}")
            if position.entry_price <= 0:
                issues.append(f"{position.symbol} 成本基准非法：{position.entry_price}")
            if position.open_fee < 0:
                issues.append(f"{position.symbol} 开仓手续费非法：{position.open_fee}")
        return issues

    def health_check(self) -> list[str]:
        """启动/恢复时的一致性检查，返回问题列表（空=正常）。"""

        issues: list[str] = []
        try:
            account = self.account()
        except LedgerError as exc:
            return [str(exc)]
        for field_name, value in (
            ("cash", account.cash),
            ("realized_pnl", account.realized_pnl),
            ("fees_paid", account.fees_paid),
            ("day_start_equity", account.day_start_equity),
        ):
            if not math.isfinite(float(value)):
                issues.append(f"账户字段 {field_name} 非法：{value}")
        position_count = len(self.positions())
        if position_count > 10:
            issues.append(f"持仓数量异常：{position_count}")
        for position in self.positions():
            if position.lots <= 0:
                issues.append(f"{position.symbol} 手数非法：{position.lots}")
            if position.margin < 0:
                issues.append(f"{position.symbol} 保证金非法：{position.margin}")
            if position.side not in ("long", "short"):
                issues.append(f"{position.symbol} 方向非法：{position.side}")
        return issues
