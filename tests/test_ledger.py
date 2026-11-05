"""账本测试：手算用例 U1–U4、幂等、结算、恢复。"""

import tempfile
import unittest
from pathlib import Path

from tests.support import submodule

contracts = submodule("contracts")
ledger_module = submodule("ledger")


def make_spec():
    return contracts.ContractSpec(
        symbol="RB2701",
        name="测试螺纹",
        exchange="TEST",
        multiplier=10.0,
        tick_size=1.0,
        margin_rate=0.1,
        fee_per_lot=5.0,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    )


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.sqlite3"
        self.ledger = ledger_module.Ledger(self.db_path)
        self.ledger.initialize(1_000_000.0, day="2026-11-03")
        self.spec = make_spec()

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def open_long(self, order_id="o1", decision_id="d1", price=100.0):
        return self.ledger.try_open(
            order_id=order_id,
            decision_id=decision_id,
            ts="2026-11-03T09:30:00",
            spec=self.spec,
            side="long",
            lots=1,
            price=price,
            stop_price=price - 10,
            take_price=price + 10,
        )

    # U1：100 开仓、110 平仓、每边手续费 5 -> 净收益 90
    def test_u1_round_trip_profit(self):
        self.open_long()
        trade = self.ledger.try_close(
            order_id="o2",
            decision_id="d2",
            ts="2026-11-03T14:00:00",
            spec=self.spec,
            price=110.0,
        )
        self.assertAlmostEqual(trade.realized_pnl, 100.0)
        account = self.ledger.account()
        self.assertAlmostEqual(account.cash, 1_000_090.0)
        self.assertAlmostEqual(account.realized_pnl, 100.0)
        self.assertAlmostEqual(account.fees_paid, 10.0)
        self.assertAlmostEqual(self.ledger.equity({}).equity, 1_000_090.0)

    # U2：中途按 105 结算，再 110 平仓，总收益仍为 90
    def test_u2_settlement_then_close(self):
        self.open_long()
        records = self.ledger.settle(
            settle_date="2026-11-03",
            ts="2026-11-03T15:10:00",
            settle_prices={"RB2701": 105.0},
        )
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0].pnl, 50.0)
        position = self.ledger.position("RB2701")
        self.assertAlmostEqual(position.entry_price, 105.0)
        self.assertAlmostEqual(self.ledger.equity({"RB2701": 105.0}).equity, 1_000_045.0)
        trade = self.ledger.try_close(
            order_id="o2",
            decision_id="d2",
            ts="2026-11-03T15:30:00",
            spec=self.spec,
            price=110.0,
        )
        self.assertAlmostEqual(trade.realized_pnl, 50.0)
        account = self.ledger.account()
        self.assertAlmostEqual(account.cash, 1_000_090.0)
        self.assertAlmostEqual(account.realized_pnl, 100.0)
        self.assertAlmostEqual(account.fees_paid, 10.0)

    # U3：保证金与权益
    def test_u3_margin_and_equity(self):
        self.open_long()
        equity = self.ledger.equity({"RB2701": 100.0})
        self.assertAlmostEqual(equity.margin_used, 100.0)
        self.assertAlmostEqual(equity.floating, 0.0)
        self.assertAlmostEqual(equity.equity, 999_995.0)
        self.ledger.settle(
            settle_date="2026-11-03",
            ts="2026-11-03T15:10:00",
            settle_prices={"RB2701": 105.0},
        )
        equity = self.ledger.equity({"RB2701": 105.0})
        self.assertAlmostEqual(equity.margin_used, 105.0)
        self.assertAlmostEqual(equity.equity, 1_000_045.0)

    # U4：做空对称
    def test_u4_short_profit(self):
        self.ledger.try_open(
            order_id="s1",
            decision_id="sd1",
            ts="2026-11-03T09:30:00",
            spec=self.spec,
            side="short",
            lots=1,
            price=100.0,
            stop_price=110.0,
            take_price=90.0,
        )
        trade = self.ledger.try_close(
            order_id="s2", decision_id="sd2", ts="2026-11-03T14:00:00", spec=self.spec, price=90.0
        )
        self.assertAlmostEqual(trade.realized_pnl, 100.0)
        self.assertAlmostEqual(self.ledger.account().cash, 1_000_090.0)

    def test_duplicate_order_rejected(self):
        self.open_long()
        with self.assertRaises(ledger_module.DuplicateOrderError):
            self.open_long(order_id="o1", decision_id="d9", price=100.0)

    def test_position_exists_rejected(self):
        self.open_long(order_id="o1", decision_id="d1")
        with self.assertRaises(ledger_module.PositionExistsError):
            self.open_long(order_id="o2", decision_id="d2")

    def test_close_without_position(self):
        with self.assertRaises(ledger_module.NoPositionError):
            self.ledger.try_close(
                order_id="x", decision_id="dx", ts="2026-11-03T14:00:00", spec=self.spec, price=100.0
            )

    def test_settlement_idempotent(self):
        self.open_long()
        first = self.ledger.settle(
            settle_date="2026-11-03", ts="2026-11-03T15:10:00", settle_prices={"RB2701": 105.0}
        )
        self.assertEqual(len(first), 1)
        second = self.ledger.settle(
            settle_date="2026-11-03", ts="2026-11-03T15:11:00", settle_prices={"RB2701": 105.0}
        )
        self.assertEqual(second, [])
        self.assertTrue(self.ledger.is_settled("2026-11-03"))
        self.assertAlmostEqual(self.ledger.account().realized_pnl, 50.0)

    def test_rollover_resets_day_counters(self):
        self.open_long()
        self.ledger.set_halted(True)
        account = self.ledger.account()
        self.assertTrue(account.halted)
        self.assertEqual(account.trades_today, 1)
        changed = self.ledger.rollover("2026-11-04", {"RB2701": 100.0})
        self.assertTrue(changed)
        account = self.ledger.account()
        self.assertEqual(account.day, "2026-11-04")
        self.assertFalse(account.halted)
        self.assertEqual(account.trades_today, 0)
        self.assertAlmostEqual(account.day_start_equity, 999_995.0)
        self.assertFalse(self.ledger.rollover("2026-11-04", {"RB2701": 100.0}))

    def test_reset_wipes_account(self):
        self.open_long()
        self.ledger.reset(500_000.0, day="2026-11-05")
        self.assertEqual(self.ledger.positions(), [])
        account = self.ledger.account()
        self.assertAlmostEqual(account.cash, 500_000.0)
        self.assertEqual(account.trades_today, 0)
        self.assertEqual(self.ledger.recent_trades(10), [])

    def test_persistence_reopen(self):
        self.open_long()
        self.ledger.close()
        reopened = ledger_module.Ledger(self.db_path)
        reopened.initialize(1_000_000.0, day="2026-11-03")
        self.assertIsNotNone(reopened.position("RB2701"))
        self.assertAlmostEqual(reopened.account().cash, 999_995.0)
        self.assertEqual(reopened.health_check(), [])
        reopened.close()

    def test_record_decision_idempotent(self):
        first = self.ledger.record_decision(
            decision_id="d1", ts="2026-11-03T10:00:00", symbol="RB2701", action="hold", lots=1, reason="x", source="llm"
        )
        second = self.ledger.record_decision(
            decision_id="d1", ts="2026-11-03T10:00:00", symbol="RB2701", action="hold", lots=1, reason="x", source="llm"
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.ledger.recent_decisions(10)), 1)

    def test_meta(self):
        self.assertIsNone(self.ledger.get_meta("missing"))
        self.ledger.set_meta("paused", "1")
        self.assertEqual(self.ledger.get_meta("paused"), "1")


if __name__ == "__main__":
    unittest.main()
