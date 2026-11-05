"""交易所官方数据：交易日历与结算价。

已接入：
- 上期所：官方“休市安排”页面（含节前停夜盘）、官方结算价 JSON\n- 上期能源：官方结算价 JSON
- 郑商所：官方每日行情表（结算价列）

失败时由调用方回退到静态日历 / Sina 行情，不阻塞交易。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import market_calendar
from .quotes import NO_PROXY_OPENER

CST = ZoneInfo("Asia/Shanghai")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://www.shfe.com.cn/",
}


class OfficialDataError(RuntimeError):
    """官方数据获取/解析失败。"""


def _http_get(url: str, timeout: int = 15) -> bytes:
    request = urllib.request.Request(url, headers=_HEADERS)
    with NO_PROXY_OPENER.open(request, timeout=timeout) as response:  # noqa: S310 - 固定交易所域名
        return response.read()


def _json_get(url: str, timeout: int = 15) -> Any:
    return json.loads(_http_get(url, timeout).decode("utf-8-sig", errors="replace"))


def parse_shfe_holiday_page(html: str, default_year: int | None = None) -> tuple[int, set[date], set[date]]:
    """解析上期所“休市安排”页面，返回 (年份, 休市日期, 节前停夜盘日期)。"""

    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)
    title = re.search(r"(20\d{2})年休市安排", text)
    year = int(title.group(1)) if title else int(default_year or datetime.now(CST).year)

    holidays: set[date] = set()
    range_pattern = re.compile(
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[^。；]{0,30}?至"
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[^。；]{0,20}?休市"
    )
    for match in range_pattern.finditer(text):
        start_year = int(match.group(1)) if match.group(1) else year
        end_year = int(match.group(4)) if match.group(4) else start_year
        start_month, start_day = int(match.group(2)), int(match.group(3))
        end_month, end_day = int(match.group(5)), int(match.group(6))
        try:
            start = datetime(start_year, start_month, start_day).date()
            end = datetime(end_year, end_month, end_day).date()
        except ValueError:
            continue
        if end < start:
            end = datetime(end_year + 1, end_month, end_day).date()
        current = start
        for _ in range(60):
            if current > end:
                break
            holidays.add(current)
            current = datetime.fromordinal(current.toordinal() + 1).date()

    no_night: set[date] = set()
    no_night_pattern = re.compile(
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[^。；]{0,20}?晚上不进行夜盘交易"
    )
    for match in no_night_pattern.finditer(text):
        target_year = int(match.group(1)) if match.group(1) else year
        try:
            no_night.add(
                datetime(target_year, int(match.group(2)), int(match.group(3))).date()
            )
        except ValueError:
            continue
    return year, holidays, no_night


def fetch_cn_holiday_page(timeout: int = 15) -> str:
    url = "https://www.shfe.com.cn/services/calenderandholidays/holiday/"
    return _http_get(url, timeout).decode("utf-8", errors="replace")


def parse_shfe_calendar(payload: Any) -> set[date]:
    """解析上期所 ``calendar_YYYY.dat``：只保留交易日。"""

    if not isinstance(payload, list):
        raise OfficialDataError("交易日历格式不是 JSON 数组")
    days: set[date] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        text = str(item.get("tradeDate") or "").strip()
        if len(text) == 8 and text.isdigit():
            try:
                days.add(datetime.strptime(text, "%Y%m%d").date())
            except ValueError:
                continue
    return days


def parse_shfe_settlements(payload: Any) -> dict[str, float]:
    """解析上期所/上期能源 ``SettlementYYYYMMDD.dat``。"""

    if not isinstance(payload, dict):
        raise OfficialDataError("结算价格式不是 JSON 对象")
    rows = payload.get("Settlement")
    if not isinstance(rows, list):
        return {}
    result: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("INSTRUMENTID") or "").strip().upper()
        try:
            price = float(row.get("SETTLEMENTPRICE") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and price > 0:
            result[symbol] = price
    return result


def _normalize_czce_symbol(symbol: str, reference_year: int) -> str:
    """郑商所文件用 3 位合约码（AP701），插件用 4 位（AP2701）。"""

    text = str(symbol or "").strip().upper()
    letters = "".join(ch for ch in text if ch.isalpha())
    digits = "".join(ch for ch in text if ch.isdigit())
    if not letters or len(digits) != 3:
        return text
    decade = (int(reference_year) // 10) * 10
    year = decade + int(digits[0])
    if year < int(reference_year) - 5:
        year += 10
    return f"{letters}{year % 100:02d}{digits[1:]}"


def parse_czce_settlements(text: str, *, reference_year: int | None = None) -> dict[str, float]:
    """解析郑商所 ``FutureDataDaily.txt`` 的“今结算”列，并规范为 4 位合约码。"""

    ref_year = int(reference_year or datetime.now().year)
    result: dict[str, float] = {}
    for line in text.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 7:
            continue
        symbol = _normalize_czce_symbol(fields[0], ref_year)
        if not symbol or not symbol[0].isalpha():
            continue
        try:
            price = float(fields[6].replace(",", ""))
        except ValueError:
            continue
        if price > 0:
            result[symbol] = price
    return result


def fetch_cn_calendar(year: int, timeout: int = 15) -> set[date]:
    errors: list[str] = []
    for url in (
        f"https://www.shfe.com.cn/data/calendar/calendar_{year}.dat",
        f"https://www.ine.cn/data/calendar/calendar_{year}.dat",
    ):
        try:
            return parse_shfe_calendar(_json_get(url, timeout))
        except Exception as exc:  # noqa: BLE001 - 逐个源尝试
            errors.append(f"{url}: {exc}")
    raise OfficialDataError("官方交易日历获取失败：" + "；".join(errors))


def fetch_cn_settlements(day: datetime.date, timeout: int = 15) -> dict[str, float]:
    """拉取上期所/上期能源/郑商所官方结算价，合并返回。"""

    compact = day.strftime("%Y%m%d")
    result: dict[str, float] = {}
    errors: list[str] = []
    for url in (
        f"https://www.shfe.com.cn/data/busiparamdata/future/Settlement{compact}.dat",
        f"https://www.ine.cn/data/busiparamdata/future/Settlement{compact}.dat",
    ):
        try:
            result.update(parse_shfe_settlements(_json_get(url, timeout)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    czce_url = (
        f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day.year}/"
        f"{compact}/FutureDataDaily.txt"
    )
    try:
        result.update(
            parse_czce_settlements(
                _http_get(czce_url, timeout).decode("utf-8", errors="replace"),
                reference_year=day.year,
            )
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{czce_url}: {exc}")
    if not result:
        raise OfficialDataError("官方结算价获取失败：" + "；".join(errors))
    return result


class OfficialDataService:
    """插件本地的官方数据缓存与同步服务。"""

    def __init__(
        self,
        data_dir: Path,
        logger: Any,
        *,
        enabled: bool = True,
        settlement_enabled: bool = True,
        timeout_seconds: int = 15,
        cache_enabled: bool = True,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._logger = logger
        self._enabled = bool(enabled)
        self._settlement_enabled = bool(settlement_enabled)
        self._timeout = max(3, int(timeout_seconds))
        self._cache_enabled = bool(cache_enabled)
        self._cache_path = self._data_dir / "official_cache.json"
        self._cache: dict[str, Any] = {"calendar": {}, "settlements": {}}
        self._load_cache()

    def _load_cache(self) -> None:
        if not self._cache_enabled or not self._cache_path.exists():
            return
        try:
            self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("官方数据缓存读取失败：%s", exc)
            return
        holidays: dict[int, set[date]] = {}
        no_night: dict[int, set[date]] = {}
        for year_text, group in (self._cache.get("calendar") or {}).items():
            try:
                year = int(year_text)
            except ValueError:
                continue
            if not isinstance(group, dict):
                continue
            for item in group.get("holidays") or []:
                try:
                    holidays.setdefault(year, set()).add(
                        datetime.strptime(str(item), "%Y-%m-%d").date()
                    )
                except ValueError:
                    continue
            for item in group.get("no_night") or []:
                try:
                    no_night.setdefault(year, set()).add(
                        datetime.strptime(str(item), "%Y-%m-%d").date()
                    )
                except ValueError:
                    continue
        if holidays:
            market_calendar.set_official_holidays("cn", holidays, no_night)
            self._logger.info("官方休市安排缓存已加载：%s 年", sorted(holidays))

    def _save_cache(self) -> None:
        if not self._cache_enabled:
            return
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self._cache_path)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("官方数据缓存写入失败：%s", exc)

    async def sync_calendar(self) -> None:
        if not self._enabled:
            return
        try:
            page = await asyncio.to_thread(fetch_cn_holiday_page, self._timeout)
            year, holidays, no_night = parse_shfe_holiday_page(page)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("官方休市安排同步失败：%s", exc)
            return
        if not holidays:
            self._logger.warning("官方休市安排没有解析出日期，继续使用静态日历")
            return
        no_night_by_year: dict[int, set[date]] = {}
        for day in no_night:
            no_night_by_year.setdefault(day.year, set()).add(day)
        market_calendar.set_official_holidays("cn", {year: holidays}, no_night_by_year)
        self._cache.setdefault("calendar", {})[str(year)] = {
            "holidays": [day.isoformat() for day in sorted(holidays)],
            "no_night": [
                day.isoformat()
                for day in sorted(no_night_by_year.get(year, set()))
            ],
        }
        # 跨年日期（如 2025-12-31 停夜盘）单独放进对应年份，避免归错年
        for extra_year, days in no_night_by_year.items():
            if extra_year == year:
                continue
            self._cache.setdefault("calendar", {}).setdefault(
                str(extra_year), {"holidays": [], "no_night": []}
            )["no_night"] = [day.isoformat() for day in sorted(days)]
        self._save_cache()
        self._logger.info(
            "官方休市安排已同步：%s 年，休市 %s 天，节前停夜盘 %s 天",
            year,
            len(holidays),
            len(no_night),
        )

    async def settlements(self, market_id: str, day_text: str, symbols: list[str]) -> dict[str, float]:
        if not self._enabled or not self._settlement_enabled or market_id != "cn":
            return {}
        try:
            day = datetime.strptime(day_text, "%Y-%m-%d").date()
        except ValueError:
            return {}
        cached = (self._cache.get("settlements") or {}).get(day_text)
        if isinstance(cached, dict):
            return {symbol: float(cached[symbol]) for symbol in symbols if symbol in cached}
        try:
            prices = await asyncio.to_thread(fetch_cn_settlements, day, self._timeout)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("官方结算价同步失败（%s）：%s", day_text, exc)
            return {}
        self._cache.setdefault("settlements", {})[day_text] = prices
        self._save_cache()
        return {symbol: prices[symbol] for symbol in symbols if symbol in prices}
