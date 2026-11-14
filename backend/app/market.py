from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


CHINA_TZ = ZoneInfo("Asia/Shanghai")
AM_START = time(9, 15)
AM_END = time(11, 30)
PM_START = time(13, 0)
PM_END = time(15, 5)


def now_local() -> datetime:
    return datetime.now(CHINA_TZ)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def market_phase(now: datetime | None = None) -> str:
    current = now or now_local()
    if not is_weekday(current.date()):
        return "closed"
    clock = current.time()
    if AM_START <= clock <= AM_END or PM_START <= clock <= PM_END:
        return "open"
    if time(11, 30) < clock < PM_START:
        return "lunch"
    if clock < AM_START:
        return "preopen"
    return "closed"


def market_open(now: datetime | None = None) -> bool:
    return market_phase(now) == "open"


def market_status_text(now: datetime | None = None) -> str:
    return {
        "open": "交易中",
        "lunch": "午间休市",
        "preopen": "开盘前",
        "closed": "已休市",
    }.get(market_phase(now), "已休市")


def next_market_open(now: datetime | None = None) -> datetime:
    current = now or now_local()
    candidate = current
    for _ in range(10):
        if is_weekday(candidate.date()):
            if candidate.time() < AM_START:
                return candidate.replace(hour=9, minute=15, second=0, microsecond=0)
            if AM_START <= candidate.time() <= AM_END:
                return candidate
            if AM_END < candidate.time() < PM_START:
                return candidate.replace(hour=13, minute=0, second=0, microsecond=0)
            if PM_START <= candidate.time() <= PM_END:
                return candidate
        candidate = (candidate + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
    return candidate
