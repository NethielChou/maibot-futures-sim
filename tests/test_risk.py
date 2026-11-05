"""风控规则测试。"""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.support import submodule

CST = ZoneInfo("Asia/Shanghai")


def make_account(**overrides):
    ledger = submodule("ledger")
    base = {
        "initial_balance": 1_000_000.0,
        "cash": 1_000_000.0,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "day": "2026-11-03",
        "trades_today": 0,
        "halted": False,
        "day_start_equity": 1_000_000.0,
        "schema_version": 1,
    }
    base.update(overrides)
    return ledger.Account(**base)


def make_position(symbol="RB2701", side="long", lots=1, entry_price=3000.0):
    ledger = submodule("ledger")
    return ledger.Position(
        symbol=symbol,
        side=side,
        lots=lots,
        entry_price=entry_price,
        margin=3000.0,
        multiplier=10.0,
        margin_rate=0.1,
        opened_at="2026-11-03T09:30:00",
        stop_price=2950.0,
        take_price=3100.0,
        decision_id="d1",
    )


class RiskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.support import submodule as load_submodule

        cls.risk = load_submodule("risk")
        cls.contracts = load_submodule("contracts")
        cls.config_module = load_submodule("config")
        cls.rb = cls.contracts.get_contract("RB2701")
        cls.defaults = cls.config_module.RiskConfig()

    def context(self, **overrides):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        base = {
            "account": make_account(),
            "positions": [],
            "equity": 1_000_000.0,
            "margin_used": 0.0,
            "reference_price": 3000.0,
            "now": now,
            "session_open": True,
            "atr": 5.0,
            "last_open_ts": "",
        }
        base.update(overrides)
        return self.risk.RiskContext(**base)

    def test_open_allowed(self):
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(), config=self.defaults
        )
        self.assertTrue(verdict.allowed, verdict.reason)

    def test_open_session_closed(self):
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(session_open=False), config=self.defaults
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("时段", verdict.reason)

    def test_open_halted(self):
        account = make_account(halted=True)
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(account=account), config=self.defaults
        )
        self.assertFalse(verdict.allowed)

    def test_open_position_limit(self):
        positions = [make_position("M2701"), make_position("AU2612"), make_position("RB2705")]
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(positions=positions), config=self.defaults
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("上限", verdict.reason)

    def test_open_same_symbol_denied(self):
        positions = [make_position("RB2701")]
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(positions=positions), config=self.defaults
        )
        self.assertFalse(verdict.allowed)

    def test_open_lots_limit(self):
        verdict = self.risk.check_open(
            symbol="RB2701", lots=2, spec=self.rb, context=self.context(), config=self.defaults
        )
        self.assertFalse(verdict.allowed)

    def test_open_daily_trade_limit(self):
        account = make_account(trades_today=6)
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(account=account), config=self.defaults
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("开仓次数", verdict.reason)

    def test_open_without_atr(self):
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(atr=None), config=self.defaults
        )
        self.assertFalse(verdict.allowed)

    def test_open_margin_limit(self):
        # 价格 30000 -> 单手保证金 30000；权益 80000，上限 30% = 24000 -> 拒绝
        verdict = self.risk.check_open(
            symbol="RB2701",
            lots=1,
            spec=self.rb,
            context=self.context(equity=80_000.0, reference_price=30_000.0),
            config=self.defaults,
        )
        self.assertFalse(verdict.allowed)
        self.assertIn("保证金", verdict.reason)

    def test_open_cooldown(self):
        now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
        recent = (now - timedelta(minutes=5)).isoformat()
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(last_open_ts=recent), config=self.defaults
        )
        self.assertFalse(verdict.allowed)
        old = (now - timedelta(minutes=60)).isoformat()
        verdict = self.risk.check_open(
            symbol="RB2701", lots=1, spec=self.rb, context=self.context(last_open_ts=old), config=self.defaults
        )
        self.assertTrue(verdict.allowed, verdict.reason)

    def test_close_rules(self):
        positions = [make_position("RB2701")]
        verdict = self.risk.check_close(
            symbol="RB2701", spec=self.rb, context=self.context(positions=positions)
        )
        self.assertTrue(verdict.allowed)
        verdict = self.risk.check_close(symbol="RB2701", spec=self.rb, context=self.context())
        self.assertFalse(verdict.allowed)
        verdict = self.risk.check_close(
            symbol="RB2701", spec=self.rb, context=self.context(positions=positions, session_open=False)
        )
        self.assertFalse(verdict.allowed)

    def test_daily_loss_hit(self):
        self.assertTrue(
            self.risk.daily_loss_hit(equity=969_000.0, day_start_equity=1_000_000.0, limit_ratio=0.03)
        )
        self.assertFalse(
            self.risk.daily_loss_hit(equity=980_000.0, day_start_equity=1_000_000.0, limit_ratio=0.03)
        )
        self.assertFalse(
            self.risk.daily_loss_hit(equity=0.0, day_start_equity=0.0, limit_ratio=0.03)
        )


if __name__ == "__main__":
    unittest.main()
