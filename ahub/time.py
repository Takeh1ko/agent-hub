"""Время: хранение — UTC ms, экран — системный локальный пояс (перекрытие AHUB_TZ)."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_tz() -> ZoneInfo | None:
    """Пояс экрана: ZoneInfo(AHUB_TZ) или None (системный). Неверное имя — None."""
    name = os.environ.get("AHUB_TZ", "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def now_ms() -> int:
    """Сейчас, мс UTC."""
    return int(time.time() * 1000)


def to_local(ms: int) -> datetime:
    """Мс UTC → локальное время (AHUB_TZ или системный, по правилам даты)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    tz = local_tz()
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def _local_dt(y: int, mo: int, d: int, hh: int = 0, mm: int = 0, tz: ZoneInfo | None = None) -> datetime:
    """Локальная стена → aware (AHUB_TZ или системный, по правилам даты)."""
    if tz is not None:
        return datetime(y, mo, d, hh, mm, tzinfo=tz)
    return datetime(y, mo, d, hh, mm).astimezone()


def fmt_local(ms: int, now: int | None = None) -> str:
    """«23:41» если сегодня, иначе «28.09 23:41» (локальная зона)."""
    dt = to_local(ms)
    cur = to_local(now if now is not None else now_ms())
    if dt.date() == cur.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m %H:%M")


_REL = re.compile(r"^\s*(\d+)\s*([a-zа-яё]+)?\s*$", re.IGNORECASE)
_TODAY = re.compile(r"^\s*сегодня\s*(?:(\d{1,2}):(\d{2}))?\s*$", re.IGNORECASE)
_YESTERDAY = re.compile(r"^\s*вчера\s*(?:(\d{1,2}):(\d{2}))?\s*$", re.IGNORECASE)
_FULL = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{1,2}):(\d{2}))?\s*$")


def _unit_ms(unit: str | None) -> int | None:
    if not unit:
        return None
    u = unit.lower()
    if u.startswith("ч") or u.startswith("h"):
        return 3_600_000
    if u.startswith("м") or u.startswith("m"):
        # «мин»/«м» — минуты (миллисекунды в parse_since не используются)
        return 60_000
    if u.startswith("д") or u.startswith("d"):
        return 86_400_000
    if u.startswith("с") or u.startswith("s"):
        return 1_000
    if u.startswith("н") or u.startswith("w"):
        return 7 * 86_400_000
    return None


def parse_since(text: str, now: int) -> int:
    """Строка → мс UTC. Пояс экрана (AHUB_TZ или системный).

    Понимает «2026-09-28 20:00» (локальное), «сегодня 20:00»,
    «2ч», «30м», «1д». Возвращает мс для сравнения с now_ms().
    """
    tz = local_tz()
    s = text.strip()
    m = _FULL.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) is not None else 0
        mm = int(m.group(5)) if m.group(5) is not None else 0
        return int(_local_dt(y, mo, d, hh, mm, tz).timestamp() * 1000)
    m = _TODAY.match(s)
    if m:
        day = to_local(now).date()
        hh = int(m.group(1)) if m.group(1) is not None else 0
        mm = int(m.group(2)) if m.group(2) is not None else 0
        return int(_local_dt(day.year, day.month, day.day, hh, mm, tz).timestamp() * 1000)
    m = _YESTERDAY.match(s)
    if m:
        day = to_local(now).date() - timedelta(days=1)
        hh = int(m.group(1)) if m.group(1) is not None else 0
        mm = int(m.group(2)) if m.group(2) is not None else 0
        return int(_local_dt(day.year, day.month, day.day, hh, mm, tz).timestamp() * 1000)
    m = _REL.match(s)
    if m:
        unit_ms = _unit_ms(m.group(2))
        if unit_ms is not None:
            return now - int(m.group(1)) * unit_ms
    raise ValueError(f"непонятное время: {text!r}")


_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zа-яё]*)\s*$", re.IGNORECASE)


def parse_duration(text: str) -> float:
    """«90», «30s», «30m», «4h», «2ч», «15м» → секунды."""
    m = _DUR.match(str(text))
    if not m:
        raise ValueError(f"непонятная длительность: {text!r}")
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit in ("", "s", "с", "sec", "сек"):
        return n
    if unit in ("m", "м", "min", "мин"):
        return n * 60
    if unit in ("h", "ч", "hr", "час"):
        return n * 3600
    raise ValueError(f"непонятная длительность: {text!r}")
