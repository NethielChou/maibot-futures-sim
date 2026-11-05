"""多市场运行时：一个市场一套账本、行情源、引擎与任务。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .config import MaiFuturesConfig
from .ledger import Ledger
from .scheduler import TradingEngine


@dataclass
class MarketRuntime:
    """一个市场的独立运行时：账本、币种、行情源、引擎。"""

    market_id: str
    profile: Any
    config: MaiFuturesConfig
    ledger: Ledger
    engine: TradingEngine
    specs: dict[str, Any]
    quote_source: Any
    task: asyncio.Task[None] | None = None
