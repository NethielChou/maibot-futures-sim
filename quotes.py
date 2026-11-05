"""新浪期货行情：拉取、解析、新鲜度判断。

本机实测（2026-11-02）：
- 商品期货连续/具体合约返回 44 字段（GB18030、必须带 Referer）；
- 具体合约代码形如 ``nf_RB2701``（郑商所使用 4 位月份，如 ``nf_TA2701``）；
- 字段映射见 ``_FIELD`` 常量；G3 用真实样例锁定，G4 单测覆盖。
"""

from __future__ import annotations

import asyncio
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
}
# 宿主进程可能继承已停掉的本地代理；行情/官方数据明确绕过系统代理直连。
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 商品期货 44 字段布局（实测：nf_RB2701 / nf_M2701 / nf_AU2612）
_FIELD = {
    "name": 0,
    "time": 1,
    "open": 2,
    "high": 3,
    "low": 4,
    "prev_close": 5,
    "bid1": 6,
    "ask1": 7,
    "last": 8,
    "settle": 9,
    "prev_settle": 10,
    "bid1_volume": 11,
    "ask1_volume": 12,
    "open_interest": 13,
    "volume": 14,
    "exchange": 15,
    "product": 16,
    "date": 17,
}
COMMODITY_FIELD_COUNT = 44

# 国际期货 hf_ 15 字段布局（实测：hf_OIL / hf_GC / hf_CL）
# 0=最新 2=买一 3=卖一 4=高 5=低 6=时间 7=昨收 12=日期 13=名称 14=成交量
_INTL_FIELD = {
    "last": 0,
    "bid1": 2,
    "ask1": 3,
    "high": 4,
    "low": 5,
    "time": 6,
    "prev_close": 7,
    "date": 12,
    "name": 13,
    "volume": 14,
}


class QuoteError(RuntimeError):
    """行情错误基类。"""


class QuoteParseError(QuoteError):
    """单条行情解析失败。"""


class QuoteFetchError(QuoteError):
    """行情请求失败。"""


@dataclass(frozen=True)
class Quote:
    """一条标准化行情快照。"""

    symbol: str
    name: str
    quote_time: datetime
    received_at: datetime
    open: float
    high: float
    low: float
    prev_close: float
    bid1: float
    ask1: float
    last: float
    settle: float
    prev_settle: float
    bid1_volume: int
    ask1_volume: int
    open_interest: float
    volume: float
    source: str = "sina"

    @property
    def mid(self) -> float:
        """中间价；买卖价缺失时退回最新价。"""
        if self.bid1 > 0 and self.ask1 > 0 and self.ask1 >= self.bid1:
            return (self.bid1 + self.ask1) / 2.0
        return self.last

    def age_seconds(self, now: datetime) -> float:
        """行情时间戳距 ``now`` 的秒数（可为负：未来时间戳不压成 0）。"""
        return (now - self.quote_time).total_seconds()

    def has_valid_book(self) -> bool:
        """买一/卖一是否存在且不交叉（估值/展示用）。"""
        if not (self.bid1 > 0 and self.ask1 > 0 and self.ask1 >= self.bid1):
            return False
        return math.isfinite(self.bid1) and math.isfinite(self.ask1)

    def has_tradable_book(self) -> bool:
        """是否可成交：价格有效且对手盘有挂单量。"""
        return self.has_valid_book() and self.bid1_volume > 0 and self.ask1_volume > 0


@dataclass(frozen=True)
class QuoteBatch:
    """一次批量拉取结果。"""

    quotes: dict[str, Quote]
    errors: dict[str, str]
    fetched_at: datetime


def _to_float(raw: str) -> float:
    text = str(raw).strip()
    if not text:
        return 0.0
    return float(text)


def _to_int(raw: str) -> int:
    value = _to_float(raw)
    return int(value)


def parse_quote_line(line: str, *, received_at: datetime | None = None) -> Quote:
    """解析一行 ``var hq_str_nf_XXX="...";`` 行情。"""

    text = line.strip()
    if not text or '="' not in text:
        raise QuoteParseError(f"行情行格式不正确：{text[:80]!r}")
    code_part, payload_part = text.split('="', 1)
    code = code_part.replace("var hq_str_", "").strip()
    if not code.startswith("nf_"):
        raise QuoteParseError(f"不是国内期货代码：{code}")
    symbol = code[3:].strip().upper()
    payload = payload_part.rstrip('";')
    fields = payload.split(",")
    if len(fields) != COMMODITY_FIELD_COUNT:
        raise QuoteParseError(f"{symbol} 字段数 {len(fields)} != {COMMODITY_FIELD_COUNT}（仅支持商品期货布局）")

    name = fields[_FIELD["name"]].strip()
    date_text = fields[_FIELD["date"]].strip()
    time_text = fields[_FIELD["time"]].strip()
    if not date_text or not time_text:
        raise QuoteParseError(f"{symbol} 缺少日期/时间字段")
    try:
        quote_dt = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H%M%S").replace(tzinfo=CST)
    except ValueError as exc:
        raise QuoteParseError(f"{symbol} 时间字段无法解析：{date_text} {time_text}") from exc

    quote = Quote(
        symbol=symbol,
        name=name,
        quote_time=quote_dt,
        received_at=received_at or datetime.now(CST),
        open=_to_float(fields[_FIELD["open"]]),
        high=_to_float(fields[_FIELD["high"]]),
        low=_to_float(fields[_FIELD["low"]]),
        prev_close=_to_float(fields[_FIELD["prev_close"]]),
        bid1=_to_float(fields[_FIELD["bid1"]]),
        ask1=_to_float(fields[_FIELD["ask1"]]),
        last=_to_float(fields[_FIELD["last"]]),
        settle=_to_float(fields[_FIELD["settle"]]),
        prev_settle=_to_float(fields[_FIELD["prev_settle"]]),
        bid1_volume=_to_int(fields[_FIELD["bid1_volume"]]),
        ask1_volume=_to_int(fields[_FIELD["ask1_volume"]]),
        open_interest=_to_float(fields[_FIELD["open_interest"]]),
        volume=_to_float(fields[_FIELD["volume"]]),
    )
    if quote.last <= 0 and quote.settle <= 0:
        raise QuoteParseError(f"{symbol} 最新价与结算价均为 0，视为无效行情")
    for field_name in ("open", "high", "low", "bid1", "ask1", "last", "settle", "prev_settle"):
        value = getattr(quote, field_name)
        if not math.isfinite(value):
            raise QuoteParseError(f"{symbol} 字段 {field_name} 不是有限数值：{value!r}")
    return quote


def parse_quote_text(text: str, *, received_at: datetime | None = None) -> tuple[dict[str, Quote], dict[str, str]]:
    """解析整批返回文本，返回 (quotes, errors)。单条失败不影响其它合约。"""

    received = received_at or datetime.now(CST)
    quotes: dict[str, Quote] = {}
    errors: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            quote = parse_quote_line(line, received_at=received)
        except QuoteParseError as exc:
            code_part = line.split("=", 1)[0].replace("var hq_str_", "").strip()
            errors[code_part or line[:20]] = str(exc)
            continue
        quotes[quote.symbol] = quote
    return quotes, errors


def build_sina_url(symbols: list[str]) -> str:
    """构造新浪批量行情 URL（自动加 nf_ 前缀）。"""

    codes = ",".join(f"nf_{symbol.strip().upper()}" for symbol in symbols)
    return f"{SINA_URL}{codes}"


def build_intl_url(symbols: list[str]) -> str:
    """构造新浪国际期货 URL（自动加 hf_ 前缀）。"""

    codes = ",".join(f"hf_{symbol.strip().upper()}" for symbol in symbols)
    return f"{SINA_URL}{codes}"


def parse_intl_quote_line(line: str, *, received_at: datetime | None = None) -> Quote:
    """解析一行 ``var hq_str_hf_XXX="...";`` 国际期货行情。"""

    text = line.strip()
    if not text or '="' not in text:
        raise QuoteParseError(f"无法识别的国际行情行：{text[:80]!r}")
    prefix, payload_part = text.split('="', 1)
    code = prefix.rsplit("hq_str_", 1)[-1].strip()
    if not code.startswith("hf_"):
        raise QuoteParseError(f"不是国际期货代码：{code}")
    symbol = code[3:].strip().upper()
    fields = payload_part.rstrip('";').split(",")
    if len(fields) < 14:
        raise QuoteParseError(f"{symbol} 国际行情字段不足：{len(fields)}")

    last = _to_float(fields[_INTL_FIELD["last"]])
    bid1 = _to_float(fields[_INTL_FIELD["bid1"]])
    ask1 = _to_float(fields[_INTL_FIELD["ask1"]])
    high = _to_float(fields[_INTL_FIELD["high"]])
    low = _to_float(fields[_INTL_FIELD["low"]])
    prev_close = _to_float(fields[_INTL_FIELD["prev_close"]])
    name = fields[_INTL_FIELD["name"]].strip() or symbol
    volume = _to_float(fields[_INTL_FIELD["volume"]]) if len(fields) > _INTL_FIELD["volume"] else 0.0

    if last <= 0:
        raise QuoteParseError(f"{symbol} 最新价为 0，视为无效行情")
    for value in (last, bid1, ask1, high, low, prev_close):
        if not math.isfinite(value):
            raise QuoteParseError(f"{symbol} 字段不是有限数值")
    if symbol in _INTL_CENTS_SYMBOLS:
        scale = 0.01
        last *= scale
        bid1 *= scale
        ask1 *= scale
        high *= scale
        low *= scale
        prev_close *= scale

    date_text = fields[_INTL_FIELD["date"]].strip()
    time_text = fields[_INTL_FIELD["time"]].strip()
    received = received_at or datetime.now(CST)
    try:
        quote_time = datetime.fromisoformat(f"{date_text}T{time_text}").replace(tzinfo=CST)
    except ValueError:
        quote_time = received
    if quote_time > received + timedelta(seconds=120):
        quote_time = received

    return Quote(
        symbol=symbol,
        name=name,
        quote_time=quote_time,
        received_at=received,
        open=last,
        high=high,
        low=low,
        prev_close=prev_close if prev_close > 0 else last,
        bid1=bid1,
        ask1=ask1,
        last=last,
        settle=0.0,
        prev_settle=0.0,
        bid1_volume=1,
        ask1_volume=1,
        open_interest=0.0,
        volume=volume,
        source="sina_hf",
    )


def parse_intl_quote_text(
    text: str, *, received_at: datetime | None = None
) -> tuple[dict[str, Quote], dict[str, str]]:
    """解析整批国际行情文本，返回 (quotes, errors)。"""

    quotes: dict[str, Quote] = {}
    errors: dict[str, str] = {}
    for line in text.splitlines():
        if "hq_str_hf_" not in line:
            continue
        try:
            quote = parse_intl_quote_line(line, received_at=received_at)
        except QuoteParseError as exc:
            errors["?"] = str(exc)
            continue
        quotes[quote.symbol] = quote
    return quotes, errors


FUTURE_QUOTE_TOLERANCE_SECONDS = 120.0
# 新浪 hf_ 里这些品种以“美分/蒲式耳”或“美分/磅”报价，统一换成美元，
# 否则保证金会放大 100 倍。
_INTL_CENTS_SYMBOLS = {"S", "C", "W", "CT", "KC"}


def is_quote_fresh(quote: Quote, now: datetime, max_age_seconds: int) -> bool:
    """行情时间戳是否在允许年龄内；未来时间戳（超容差）一律视为不可信。"""

    age = quote.age_seconds(now)
    if age < -FUTURE_QUOTE_TOLERANCE_SECONDS:
        return False
    if age < 0:
        return True  # 轻微超前（时钟误差）可接受
    return age <= max_age_seconds


def quote_is_usable(quote: Quote, *, max_abs_change_ratio: float = 0.10) -> bool:
    """基础异常值检查：数值有限、价格非负、买卖价不交叉、涨跌幅不超阈值。"""

    for value in (quote.last, quote.bid1, quote.ask1, quote.prev_settle, quote.prev_close):
        if not math.isfinite(value):
            return False
    if quote.last <= 0:
        return False
    if quote.bid1 < 0 or quote.ask1 < 0:
        return False
    if quote.bid1 > 0 and quote.ask1 > 0 and quote.ask1 < quote.bid1:
        return False
    reference = quote.prev_settle if quote.prev_settle > 0 else quote.prev_close
    if reference > 0 and quote.last > 0:
        if abs(quote.last - reference) / reference > max_abs_change_ratio:
            return False
    return True


class SinaQuoteSource:
    """新浪行情源（异步入口；同步 HTTP 在线程中执行）。"""

    def __init__(
        self,
        *,
        timeout_seconds: int = 8,
        http_get: Callable[[str, float], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self._timeout = max(1, int(timeout_seconds))
        self._http_get = http_get or self._default_http_get
        self._clock = clock or (lambda: datetime.now(CST))
        self._retry_delay = max(0.0, float(retry_delay_seconds))

    def _default_http_get(self, url: str, timeout: float) -> bytes:
        request = urllib.request.Request(url, headers=SINA_HEADERS)
        with NO_PROXY_OPENER.open(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 主机
            return response.read()

    async def fetch(self, symbols: list[str]) -> QuoteBatch:
        """拉取一批合约行情；失败重试 1 次。"""

        if not symbols:
            raise QuoteFetchError("合约列表为空")
        url = build_sina_url(symbols)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = await asyncio.to_thread(self._http_get, url, float(self._timeout))
                received = self._clock()  # 以实际收到响应的时间为准
                text = raw.decode("gb18030", errors="replace")
                quotes, errors = parse_quote_text(text, received_at=received)
                missing = [symbol.strip().upper() for symbol in symbols if symbol.strip().upper() not in quotes]
                for symbol in missing:
                    errors.setdefault(symbol, "返回中缺少该合约")
                return QuoteBatch(quotes=quotes, errors=errors, fetched_at=received)
            except Exception as exc:  # noqa: BLE001 - 统一转成行情错误
                last_error = exc
                if attempt == 0 and self._retry_delay:
                    await asyncio.sleep(self._retry_delay)
        raise QuoteFetchError(f"行情拉取失败：{last_error}") from last_error


class SinaIntlQuoteSource(SinaQuoteSource):
    """新浪国际期货行情源（hf_ 前缀，15 字段）。"""

    async def fetch(self, symbols: list[str]) -> QuoteBatch:
        if not symbols:
            raise QuoteFetchError("合约列表为空")
        url = build_intl_url(symbols)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = await asyncio.to_thread(self._http_get, url, float(self._timeout))
                received = self._clock()
                text = raw.decode("gb18030", errors="replace")
                quotes, errors = parse_intl_quote_text(text, received_at=received)
                missing = [symbol.strip().upper() for symbol in symbols if symbol.strip().upper() not in quotes]
                for symbol in missing:
                    errors.setdefault(symbol, "返回中缺少该合约")
                return QuoteBatch(quotes=quotes, errors=errors, fetched_at=received)
            except Exception as exc:  # noqa: BLE001 - 统一转成行情错误
                last_error = exc
                if attempt == 0 and self._retry_delay:
                    await asyncio.sleep(self._retry_delay)
        raise QuoteFetchError(f"国际行情拉取失败：{last_error}") from last_error
