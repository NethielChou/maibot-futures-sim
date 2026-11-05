"""影响源：群聊（只读宿主已有聊天记录）+ 新闻/研报标题（内存缓存）。

设计约束（v0.2，已与用户确认）：
- 不新增任何持久化存储：不建表、不写文件、不写日志、不改决策表；
- 群聊只在决策时刻通过宿主 ``message.get_recent`` 读一次，摘要仅在内存；
- 新闻/研报只取标题/元数据/链接，内存缓存，失败即降级；
- 所有参考材料都是“不可信文本”，是否采纳由麦麦人设/模型自行判断；
- 本模块不包含任何交易/权限逻辑。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
MAX_INFLUENCE_CHARS = 900
_HTTP_TIMEOUT_SECONDS = 8.0
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

# 品种/期货术语/宏观关键词（用于相关性过滤）
_SYMBOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "RB": ("螺纹", "钢材", "钢铁", "建材"),
    "M": ("豆粕", "大豆", "饲料"),
    "AU": ("黄金", "沪金", "金价"),
    "AG": ("白银", "沪银", "银价"),
    "CU": ("沪铜", "铜价", "精铜"),
    "I": ("铁矿", "铁矿石"),
    "SC": ("原油", "上海原油", "SC原油"),
    "AL": ("沪铝", "铝价", "电解铝"),
    "SR": ("白糖", "郑糖", "糖价"),
    "CF": ("棉花", "郑棉", "棉价"),
}
# 合约代码（RB2701/M2701/AU2612/AG2612/CU2612/AL2612/SR2701/CF2701 等）
_CONTRACT_CODE_RE = re.compile(
    r"(?<![A-Za-z])(?:RB|M|AU|AG|CU|AL|I|SC|SR|CF)\d{3,4}(?![A-Za-z])", re.IGNORECASE
)
# 只在群聊里单独算相关的“期货交易术语”；新闻标题必须命中品种或宏观词
_FUTURES_WORDS = (
    "期货", "开仓", "平仓", "持仓", "止损", "止盈", "做多", "做空",
    "看多", "看空", "多单", "空单", "合约", "保证金", "加仓", "减仓",
    "抄底", "追高",
)
_MACRO_WORDS = (
    "降息", "加息", "美联储", "央行", "政策", "库存", "产量", "通胀", "关税",
    "地缘", "非农", "CPI", "PMI", "GDP", "OPEC", "原油", "汇率", "房地产",
)
_NOISE_PATTERNS = re.compile(r"(^https?://)|(^\[图片\])|(^\[表情\])|(^\[语音\])|(^\[视频\])|(^\[文件\])")


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published: str = ""
    link: str = ""


def _symbol_keywords(symbols: list[str]) -> tuple[str, ...]:
    words: list[str] = []
    for symbol in symbols:
        prefix = "".join(ch for ch in str(symbol).upper() if ch.isalpha())
        for keyword in _SYMBOL_KEYWORDS.get(prefix, ()):
            if keyword not in words:
                words.append(keyword)
        if symbol:
            # 完整合约代码允许直接匹配（如 RB2701）
            code = str(symbol).upper()
            if code not in words:
                words.append(code)
    return tuple(words)


def is_relevant_text(text: str, symbols: list[str], *, kind: str = "group") -> bool:
    """相关性过滤：

    - 品种关键词（螺纹/豆粕/黄金）或完整合约代码（RB2701…）或宏观关键词 → 相关；
    - 群聊额外允许“期货交易术语”（开仓/持仓/止损…）；
    - 新闻标题不接受泛泛的“涨/跌/多/空”，避免把 A 股、比特币等无关新闻带进来；
    - 不把 Marvell/ESMO 里的单个 M 当成豆粕。
    """

    content = str(text or "").strip()
    if len(content) < 2:
        return False
    if any(word in content for word in _symbol_keywords(symbols)):
        return True
    if _CONTRACT_CODE_RE.search(content):
        return True
    if any(word in content for word in _MACRO_WORDS):
        return True
    if kind == "group" and any(word in content for word in _FUTURES_WORDS):
        return True
    return False


def message_text(message: dict[str, Any]) -> str:
    """从宿主序列化消息里提取纯文本（优先 processed_plain_text）。"""

    if not isinstance(message, dict):
        return ""
    for key in ("processed_plain_text", "plain_text", "text"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw = message.get("raw_message")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        parts: list[str] = []
        for item in raw.get("components") or raw.get("items") or []:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return " ".join(parts)
    return ""


def build_group_summary(messages: list[dict[str, Any]], symbols: list[str], *, max_items: int = 5) -> str:
    """把相关群聊消息压成 3–5 行摘要；不含昵称，不保存原文。"""

    picked: list[str] = []
    seen: set[str] = set()
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        if message.get("is_command") or message.get("is_notify"):
            continue
        text = message_text(message)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or _NOISE_PATTERNS.search(text):
            continue
        if len(text) > 120:
            text = text[:120] + "…"
        if not is_relevant_text(text, symbols):
            continue
        key = text[:40]
        if key in seen:
            continue
        seen.add(key)
        picked.append(f"- 群友：{text}")
        if len(picked) >= max(1, int(max_items)):
            break
    picked.reverse()
    return "\n".join(picked)


def _strip_jsonp(text: str) -> str:
    content = text.strip()
    if content.startswith("var "):
        _, _, content = content.partition("=")
    content = content.strip().rstrip(";").strip()
    if content.startswith("(") and content.endswith(")"):
        content = content[1:-1]
    return content


def _http_get(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://finance.eastmoney.com/", **(headers or {})})
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 - 固定 https 主机
        return response.read()


def parse_sina_news(payload: str) -> list[NewsItem]:
    data = json.loads(payload)
    result = data.get("result") if isinstance(data, dict) else None
    rows = result.get("data") if isinstance(result, dict) else None
    items: list[NewsItem] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        ctime = row.get("ctime")
        published = ""
        try:
            if ctime is not None:
                published = datetime.fromtimestamp(float(ctime), CST).strftime("%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            published = ""
        items.append(NewsItem(title=title, source="新浪财经", published=published, link=str(row.get("url") or "")))
    return items


def parse_eastmoney_flash(payload: str) -> list[NewsItem]:
    data = json.loads(_strip_jsonp(payload))
    rows = data.get("LivesList") if isinstance(data, dict) else None
    items: list[NewsItem] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("digest") or "").strip()
        if not title:
            continue
        items.append(
            NewsItem(
                title=title.split("】")[-1].strip() if title.startswith("【") else title,
                source="东方财富快讯",
                published=str(row.get("showtime") or "").strip(),
                link=str(row.get("url") or row.get("url_w") or "").strip(),
            )
        )
    return items


def parse_eastmoney_reports(payload: str) -> list[NewsItem]:
    data = json.loads(payload)
    rows = data.get("data") if isinstance(data, dict) else None
    items: list[NewsItem] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        org = str(row.get("orgSName") or "").strip()
        rating = str(row.get("emRatingName") or "").strip()
        label = "｜".join(part for part in (org, rating) if part)
        info_code = str(row.get("infoCode") or "").strip()
        items.append(
            NewsItem(
                title=title,
                source=f"研报·{label}" if label else "研报",
                published=str(row.get("publishDate") or "").strip()[:10],
                link=f"https://data.eastmoney.com/report/info/{info_code}.html" if info_code else "",
            )
        )
    return items


def format_news_text(items: list[NewsItem], *, max_items: int = 5) -> str:
    lines: list[str] = []
    for item in items[: max(1, int(max_items))]:
        meta = "｜".join(part for part in (item.source, item.published) if part)
        lines.append(f"- [{meta or '新闻'}] {item.title}")
    return "\n".join(lines)


class InfluenceService:
    """决策前收集群聊/新闻参考材料；全部内存、失败静默降级。"""

    def __init__(
        self,
        *,
        rpc_call: Callable[..., Awaitable[Any]],
        config_provider: Callable[[], Any],
        stream_provider: Callable[[], list[str]],
        logger: Any = None,
        http_get: Callable[[str], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._rpc = rpc_call
        self._config_provider = config_provider
        self._stream_provider = stream_provider
        self._logger = logger
        self._http_get = http_get or _http_get
        self._clock = clock or (lambda: datetime.now(CST))
        self._news_cache: list[NewsItem] = []
        self._news_fetched_at: float | None = None
        self._news_lock = asyncio.Lock()

    async def gather(self, symbols: list[str] | None = None) -> str:
        config = self._config_provider()
        influence = config.influence
        if not influence.enabled:
            return ""
        focus_symbols = list(symbols) if symbols is not None else list(config.market.symbols)
        parts: list[str] = []
        if influence.group_enabled:
            try:
                group_text = await self._group_context(influence, focus_symbols)
                if group_text:
                    parts.append(group_text)
            except Exception as exc:  # noqa: BLE001 - 影响源失败不影响交易
                self._log("群聊影响读取失败（本轮跳过）：%s", exc)
        if influence.news_enabled:
            try:
                news_text = await self._news_context(influence, focus_symbols)
                if news_text:
                    parts.append(news_text)
            except Exception as exc:  # noqa: BLE001
                self._log("新闻影响读取失败（本轮跳过）：%s", exc)
        text = "\n".join(parts)
        return text[:MAX_INFLUENCE_CHARS]

    async def _group_context(self, influence: Any, symbols: list[str]) -> str:
        stream_ids = [stream for stream in (self._stream_provider() or []) if stream]
        if not stream_ids:
            return ""
        response = await self._rpc(
            "message.get_recent",
            timeout_ms=5000,
            chat_id=stream_ids[0],
            hours=max(1, int(influence.group_window_minutes)) / 60.0,
            limit=int(influence.group_max_messages),
            filter_mai=True,
        )
        # SDK 会把 message.get_recent 归一化成 messages 列表；兼容字典形态
        if isinstance(response, list):
            messages = response
        elif isinstance(response, dict) and response.get("success") is not False:
            messages = response.get("messages") or []
        else:
            return ""
        if not isinstance(messages, list):
            return ""
        summary = build_group_summary(messages, list(symbols), max_items=5)
        return f"群友最近讨论（只作参考）：\n{summary}" if summary else ""

    async def _news_context(self, influence: Any, symbols: list[str]) -> str:
        items = await self._get_news_items(influence)
        if not items:
            return ""
        relevant = [
            item
            for item in items
            if is_relevant_text(item.title, list(symbols), kind="news")
        ]
        if not relevant:
            return ""
        return "新闻/研报标题（只作参考）：\n" + format_news_text(relevant, max_items=int(influence.news_max_items))

    async def _get_news_items(self, influence: Any) -> list[NewsItem]:
        interval = max(5, int(influence.news_interval_minutes)) * 60
        now = time.monotonic()
        if self._news_cache and self._news_fetched_at is not None and now - self._news_fetched_at < interval:
            return self._news_cache
        async with self._news_lock:
            now = time.monotonic()
            if self._news_cache and self._news_fetched_at is not None and now - self._news_fetched_at < interval:
                return self._news_cache
            items = await asyncio.to_thread(self._fetch_news_sync)
            self._news_cache = items
            self._news_fetched_at = time.monotonic()
            return items

    def _fetch_news_sync(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        end = self._clock()
        begin = end - timedelta(days=3)
        sources = (
            (
                "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html",
                parse_eastmoney_flash,
            ),
            (
                "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=20&page=1",
                parse_sina_news,
            ),
            (
                "https://reportapi.eastmoney.com/report/list"
                "?cb=&industryCode=*&pageSize=20&industry=*&rating=&ratingChange="
                f"&beginTime={urllib.parse.quote(begin.strftime('%Y-%m-%d'))}"
                f"&endTime={urllib.parse.quote(end.strftime('%Y-%m-%d'))}"
                "&pageNo=1&fields=&qType=0&orgCode=&code=*&rcode=&p=1&pageNum=1&pageNumber=1",
                parse_eastmoney_reports,
            ),
        )
        for url, parser in sources:
            try:
                payload = self._http_get(url).decode("utf-8", errors="replace")
                items.extend(parser(payload))
            except Exception as exc:  # noqa: BLE001 - 单个源失败不影响其它源
                self._log("新闻源失败（%s）：%s", url.split("/")[2], exc)
        # 简单去重（标题前缀）
        unique: list[NewsItem] = []
        seen: set[str] = set()
        for item in items:
            key = item.title[:30]
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique[:50]

    def _log(self, message: str, *args: Any) -> None:
        if self._logger is not None:
            try:
                self._logger.info(message, *args)
            except Exception:  # noqa: BLE001
                pass
