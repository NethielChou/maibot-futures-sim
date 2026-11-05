"""决策解析、预算与 LLM 引擎测试（不访问网络/模型）。"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.support import make_quote, submodule

CST = ZoneInfo("Asia/Shanghai")


def make_decision_input():
    ledger = submodule("ledger")
    config = submodule("config").MaiFuturesConfig()
    now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
    quotes = {"RB2701": make_quote(now=now)}
    account = ledger.Account(
        initial_balance=1_000_000,
        cash=1_000_000,
        realized_pnl=0,
        fees_paid=0,
        day="2026-11-03",
        trades_today=0,
        halted=False,
        day_start_equity=1_000_000,
        schema_version=1,
    )
    equity = ledger.Equity(equity=1_000_000, cash=1_000_000, margin_used=0, floating=0)
    decision = submodule("decision")
    return decision.DecisionInput(
        now=now,
        symbols=["RB2701"],
        quotes=quotes,
        positions=[],
        account=account,
        equity=equity,
        atr={"RB2701": 5.0},
        recent_trades=[],
        sample_counts={"RB2701": 10},
        max_trades_today=config.risk.daily_trade_limit,
        max_margin_ratio=config.risk.max_total_margin_ratio,
    )


class DecisionParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decision = submodule("decision")

    def test_parse_hold(self):
        parsed = self.decision.parse_decision('{"action":"hold","symbol":"","lots":1,"reason":"观望","speech":""}')
        self.assertEqual(parsed.action, "hold")
        self.assertEqual(parsed.symbol, "")
        self.assertEqual(parsed.reason, "观望")

    def test_parse_open(self):
        parsed = self.decision.parse_decision(
            '{"action":"open_long","symbol":"rb2701","lots":1,"reason":"突破","speech":"冲了"}'
        )
        self.assertEqual(parsed.action, "open_long")
        self.assertEqual(parsed.symbol, "RB2701")
        self.assertEqual(parsed.speech, "冲了")

    def test_parse_code_fence(self):
        raw = '```json\n{"action":"close","symbol":"RB2701","lots":1,"reason":"止盈","speech":""}\n```'
        parsed = self.decision.parse_decision(raw)
        self.assertEqual(parsed.action, "close")

    def test_invalid_action(self):
        with self.assertRaises(self.decision.DecisionError):
            self.decision.parse_decision('{"action":"buy","symbol":"RB2701","lots":1}')

    def test_invalid_lots(self):
        import json as json_module

        for lots in ("1", 0, 1.5, True):
            with self.assertRaises(self.decision.DecisionError):
                self.decision.parse_decision(
                    '{"action":"open_long","symbol":"RB2701","lots":'
                    + json_module.dumps(lots)
                    + "}"
                )

    def test_invalid_symbol_for_open(self):
        with self.assertRaises(self.decision.DecisionError):
            self.decision.parse_decision('{"action":"open_long","symbol":"","lots":1}')

    def test_missing_json(self):
        with self.assertRaises(self.decision.DecisionError):
            self.decision.parse_decision("我觉得可以买")

    def test_empty(self):
        with self.assertRaises(self.decision.DecisionError):
            self.decision.parse_decision("")

    def test_speech_newlines_stripped(self):
        parsed = self.decision.parse_decision(
            '{"action":"hold","symbol":"","lots":1,"reason":"x","speech":"a\\nb"}'
        )
        self.assertNotIn("\n", parsed.speech)

    def test_build_prompt_contains_schema_and_market(self):
        config = submodule("config").MaiFuturesConfig()
        prompt = self.decision.build_prompt(make_decision_input(), config)
        self.assertIn("open_long", prompt)
        self.assertIn("RB2701", prompt)
        self.assertIn("JSON", prompt)
        self.assertLessEqual(len(prompt), config.decision.prompt_char_limit)


class BudgetTest(unittest.TestCase):
    def test_budget_counts_and_limits(self):
        ledger_module = submodule("ledger")
        decision = submodule("decision")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ledger_module.Ledger(Path(tmp) / "l.sqlite3")
            ledger.initialize(100_000, day="2026-11-03")
            budget = decision.CallBudget(ledger, 2, clock=lambda: datetime(2026, 11, 3, 10, 0, tzinfo=CST))
            self.assertEqual(budget.remaining(), 2)
            self.assertTrue(budget.try_consume())
            self.assertTrue(budget.try_consume())
            self.assertFalse(budget.try_consume())
            self.assertEqual(budget.used(), 2)
            ledger.close()


class FakeGenerate:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []

    async def __call__(self, prompt, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return {"success": True, "response": response}


class EngineTest(unittest.TestCase):
    def setUp(self):
        ledger_module = submodule("ledger")
        decision = submodule("decision")
        config = submodule("config")
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = ledger_module.Ledger(Path(self._tmp.name) / "l.sqlite3")
        self.ledger.initialize(1_000_000, day="2026-11-03")
        self.decision = decision
        self.config = config.MaiFuturesConfig()
        self.budget = decision.CallBudget(
            self.ledger, 5, clock=lambda: datetime(2026, 11, 3, 10, 0, tzinfo=CST)
        )

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def test_valid_first_call(self):
        generate = FakeGenerate(['{"action":"hold","symbol":"","lots":1,"reason":"等","speech":""}'])
        engine = self.decision.LLMDecisionEngine(generate=generate, budget=self.budget, config=self.config)
        result = asyncio.run(engine.decide(make_decision_input()))
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "hold")
        self.assertEqual(generate.calls, 1)

    def test_retry_then_success(self):
        generate = FakeGenerate(["not json", '{"action":"hold","symbol":"","lots":1,"reason":"等","speech":""}'])
        engine = self.decision.LLMDecisionEngine(generate=generate, budget=self.budget, config=self.config)
        result = asyncio.run(engine.decide(make_decision_input()))
        self.assertIsNotNone(result)
        self.assertEqual(generate.calls, 2)
        self.assertIn("上一次输出无法", generate.prompts[1])

    def test_retry_then_fail_returns_none(self):
        generate = FakeGenerate(["not json", "still not json"])
        engine = self.decision.LLMDecisionEngine(generate=generate, budget=self.budget, config=self.config)
        result = asyncio.run(engine.decide(make_decision_input()))
        self.assertIsNone(result)
        self.assertEqual(generate.calls, 2)

    def test_budget_exhausted_on_retry(self):
        self.budget = self.decision.CallBudget(
            self.ledger, 1, clock=lambda: datetime(2026, 11, 3, 10, 0, tzinfo=CST)
        )
        generate = FakeGenerate(["not json", '{"action":"hold","symbol":"","lots":1,"reason":"等"}'])
        engine = self.decision.LLMDecisionEngine(generate=generate, budget=self.budget, config=self.config)
        with self.assertRaises(self.decision.BudgetExhausted):
            asyncio.run(engine.decide(make_decision_input()))
        self.assertEqual(generate.calls, 1)

    def test_llm_failure_raises(self):
        generate = FakeGenerate([RuntimeError("timeout")])
        engine = self.decision.LLMDecisionEngine(generate=generate, budget=self.budget, config=self.config)
        with self.assertRaises(RuntimeError):
            asyncio.run(engine.decide(make_decision_input()))

    def test_empty_response_rejected(self):
        generate = FakeGenerate(["", '{"action":"hold","symbol":"","lots":1,"reason":"等"}'])
        engine = self.decision.LLMDecisionEngine(generate=generate, budget=self.budget, config=self.config)
        result = asyncio.run(engine.decide(make_decision_input()))
        self.assertIsNotNone(result)
        self.assertEqual(generate.calls, 2)


if __name__ == "__main__":
    unittest.main()
