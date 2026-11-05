"""行情采样与简单波动率（ATR 代理）计算。

首版只做日盘 1 分钟级采样；不做 K 线合成。ATR 用最近 N 个样本的收盘价绝对差均值近似。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from .quotes import Quote


@dataclass(frozen=True)
class Sample:
    """单次采样。"""

    ts: datetime
    last: float
    high: float
    low: float


class SampleBook:
    """按合约保存最近采样，供 ATR/止损止盈使用。"""

    def __init__(self, maxlen: int = 240) -> None:
        self._maxlen = max(10, int(maxlen))
        self._samples: dict[str, deque[Sample]] = defaultdict(lambda: deque(maxlen=self._maxlen))

    def add(self, quote: Quote) -> None:
        """追加一次采样（忽略无效价格）。"""
        if quote.last <= 0:
            return
        self._samples[quote.symbol].append(
            Sample(ts=quote.quote_time, last=quote.last, high=quote.high or quote.last, low=quote.low or quote.last)
        )

    def count(self, symbol: str) -> int:
        return len(self._samples.get(symbol, ()))

    def last_price(self, symbol: str) -> float | None:
        samples = self._samples.get(symbol)
        if not samples:
            return None
        return samples[-1].last

    def atr(self, symbol: str, window: int = 20) -> float | None:
        """ATR 代理：最近 window 个收盘价绝对差的均值；样本不足返回 None。"""

        samples = self._samples.get(symbol)
        if not samples or len(samples) < 2:
            return None
        prices = [sample.last for sample in samples][-(int(window) + 1):]
        diffs = [abs(prices[index] - prices[index - 1]) for index in range(1, len(prices))]
        if not diffs:
            return None
        return sum(diffs) / len(diffs)

    def atr_map(self, symbols: list[str], window: int = 20) -> dict[str, float | None]:
        return {symbol: self.atr(symbol, window) for symbol in symbols}

    def clear(self) -> None:
        self._samples.clear()
