"""交易日历（简化版，2026–2027）。

说明：
- 中国节假日按公开安排近似整理；周末一律非交易日；
- 美国/CME 节假日按主要假日近似；周末非交易日；
- 未覆盖临时休市/调休，后续可改为外部日历数据。
"""

from __future__ import annotations

from datetime import date, timedelta

CN_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 2),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20),
    date(2026, 4, 6),
    date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
    date(2026, 10, 6), date(2026, 10, 7),
    # 2027
    date(2027, 1, 1),
    date(2027, 2, 5), date(2027, 2, 8), date(2027, 2, 9), date(2027, 2, 10), date(2027, 2, 11),
    date(2027, 4, 5),
    date(2027, 5, 3), date(2027, 5, 4), date(2027, 5, 5),
    date(2027, 6, 9),
    date(2027, 9, 15),
    date(2027, 10, 1), date(2027, 10, 4), date(2027, 10, 5),
    date(2027, 10, 6), date(2027, 10, 7),
}

US_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    # 2027
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),
    date(2027, 7, 5),
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),
}

_HOLIDAYS = {"cn": CN_HOLIDAYS, "intl": US_HOLIDAYS}

# 交易所官方日历覆盖：market -> year -> 交易日集合（保留给交易日接口）
_OFFICIAL_CALENDARS: dict[str, dict[int, set[date]]] = {}
# 交易所官方休市安排 / 节前停夜盘：market -> year -> 日期集合
_OFFICIAL_HOLIDAYS: dict[str, dict[int, set[date]]] = {}
_OFFICIAL_NO_NIGHT: dict[str, dict[int, set[date]]] = {}


def set_official_holidays(
    market: str,
    holidays_by_year: dict[int, set[date]],
    no_night_by_year: dict[int, set[date]] | None = None,
) -> None:
    """写入交易所官方休市安排（优先于静态节假日表）。"""

    key = str(market or "cn")
    if holidays_by_year:
        _OFFICIAL_HOLIDAYS[key] = {year: set(days) for year, days in holidays_by_year.items()}
    if no_night_by_year:
        _OFFICIAL_NO_NIGHT[key] = {year: set(days) for year, days in no_night_by_year.items()}


def official_holiday_years(market: str) -> set[int]:
    return set(_OFFICIAL_HOLIDAYS.get(str(market or "cn"), {}))


def set_official_calendar(market: str, dates: set[date]) -> None:
    """写入官方日历（按年份分组）；数据为空时回退静态日历。"""

    grouped: dict[int, set[date]] = {}
    for day in dates:
        grouped.setdefault(day.year, set()).add(day)
    if grouped:
        _OFFICIAL_CALENDARS[str(market or "cn")] = grouped


def clear_official_calendar(market: str | None = None) -> None:
    if market is None:
        _OFFICIAL_CALENDARS.clear()
        _OFFICIAL_HOLIDAYS.clear()
        _OFFICIAL_NO_NIGHT.clear()
    else:
        key = str(market)
        _OFFICIAL_CALENDARS.pop(key, None)
        _OFFICIAL_HOLIDAYS.pop(key, None)
        _OFFICIAL_NO_NIGHT.pop(key, None)


def calendar_source(market: str) -> str:
    key = str(market or "cn")
    if key in _OFFICIAL_HOLIDAYS or key in _OFFICIAL_CALENDARS:
        return "official"
    return "static"

# 中国期货：长假前最后一个交易日通常不设夜盘（按交易所公告近似整理）
NO_NIGHT_SESSION_DATES: set[date] = {
    date(2026, 2, 13),
    date(2026, 4, 3),
    date(2026, 4, 30),
    date(2026, 6, 18),
    date(2026, 9, 24),
    date(2026, 9, 30),
    date(2027, 2, 4),
    date(2027, 4, 2),
    date(2027, 4, 30),
    date(2027, 6, 8),
    date(2027, 9, 14),
    date(2027, 9, 30),
}


def night_session_allowed(market: str, day: date) -> bool:
    """该自然日是否允许开夜盘（优先官方节前停夜盘安排）。"""

    key = str(market or "cn")
    if key != "cn":
        return True
    official = _OFFICIAL_NO_NIGHT.get(key)
    if official is not None and day.year in official:
        return day not in official[day.year]
    return day not in NO_NIGHT_SESSION_DATES


def is_trading_day(market: str, day: date) -> bool:
    """是否交易日：优先交易所官方休市安排，否则工作日 + 静态节假日。"""

    if day.weekday() >= 5:
        return False
    key = str(market or "cn")
    official = _OFFICIAL_HOLIDAYS.get(key)
    if official is not None and day.year in official:
        return day not in official[day.year]
    official_days = _OFFICIAL_CALENDARS.get(key)
    if official_days is not None and day.year in official_days:
        return day in official_days[day.year]
    return day not in _HOLIDAYS.get(key, CN_HOLIDAYS)


def next_trading_day(market: str, day: date) -> date:
    """严格晚于 day 的第一个交易日。"""

    current = day + timedelta(days=1)
    for _ in range(400):
        if is_trading_day(market, current):
            return current
        current += timedelta(days=1)
    raise ValueError("找不到下一个交易日（日历异常）")


def previous_trading_day(market: str, day: date) -> date:
    """严格早于 day 的最近交易日。"""

    current = day - timedelta(days=1)
    for _ in range(400):
        if is_trading_day(market, current):
            return current
        current -= timedelta(days=1)
    raise ValueError("找不到上一个交易日（日历异常）")


def trading_days_between(market: str, start: date, end: date) -> int:
    """[start, end] 之间的交易日数量（含两端，按日历）。"""

    if end < start:
        return 0
    days = 0
    current = start
    while current <= end:
        if is_trading_day(market, current):
            days += 1
        current += timedelta(days=1)
    return days
