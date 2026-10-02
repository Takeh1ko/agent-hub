"""parse_since/to_local/fmt_local: все форматы, пояс через AHUB_TZ или системный."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ahub.time import fmt_local, local_tz, now_ms, parse_duration, parse_since, to_local

TOKYO = ZoneInfo("Asia/Tokyo")
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def tokyo(monkeypatch):
    monkeypatch.setenv("AHUB_TZ", "Asia/Tokyo")
    return TOKYO


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setenv("AHUB_TZ", "Europe/Berlin")
    return BERLIN


@pytest.fixture
def system_berlin(monkeypatch):
    """Системный пояс — Europe/Berlin (через TZ + tzset, с восстановлением)."""
    monkeypatch.delenv("AHUB_TZ", raising=False)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        yield
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def _ms_tokyo(y, mo, d, hh=0, mm=0) -> int:
    return int(datetime(y, mo, d, hh, mm, tzinfo=TOKYO).timestamp() * 1000)


def _ms_berlin(y, mo, d, hh=0, mm=0) -> int:
    return int(datetime(y, mo, d, hh, mm, tzinfo=BERLIN).timestamp() * 1000)


def _ms_utc(y, mo, d, hh=0, mm=0) -> int:
    return int(datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def test_now_ms_monotonic():
    a, b = now_ms(), now_ms()
    assert isinstance(a, int) and b >= a


def test_local_tz_override(tokyo):
    tz = local_tz()
    assert getattr(tz, "key", str(tz)) == "Asia/Tokyo"


def test_local_tz_none_without_env(monkeypatch):
    monkeypatch.delenv("AHUB_TZ", raising=False)
    assert local_tz() is None


def test_local_tz_none_when_invalid(monkeypatch):
    monkeypatch.setenv("AHUB_TZ", "Не_пояс/xx")
    assert local_tz() is None  # без падения


def test_to_local_override(tokyo):
    dt = to_local(0)
    assert dt.tzinfo is not None
    assert getattr(dt.tzinfo, "key", str(dt.tzinfo)) == "Asia/Tokyo"


def test_to_local_system(monkeypatch):
    monkeypatch.delenv("AHUB_TZ", raising=False)
    ms = _ms_utc(2026, 9, 28, 12, 0)
    exp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    assert to_local(ms) == exp


def test_to_local_invalid_fallback(monkeypatch):
    monkeypatch.setenv("AHUB_TZ", "bad/Name_xx")
    ms = _ms_utc(2026, 9, 28, 12, 0)
    exp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    assert to_local(ms) == exp  # неверное имя — системный пояс, без падения


def test_to_local_dst_berlin(berlin):
    jan = to_local(_ms_utc(2026, 1, 15, 12, 0))
    jul = to_local(_ms_utc(2026, 7, 15, 12, 0))
    assert jan.utcoffset() == timedelta(hours=1)
    assert jul.utcoffset() == timedelta(hours=2)
    assert (jan.hour, jan.day) == (13, 15)  # 12:00 UTC → 13:00 CET
    assert (jul.hour, jul.day) == (14, 15)  # 12:00 UTC → 14:00 CEST


def test_to_local_dst_system_berlin(system_berlin):
    jan = to_local(_ms_utc(2026, 1, 15, 12, 0))
    jul = to_local(_ms_utc(2026, 7, 15, 12, 0))
    assert jan.utcoffset() == timedelta(hours=1)
    assert jul.utcoffset() == timedelta(hours=2)


def test_fmt_local_today(tokyo):
    now = _ms_tokyo(2026, 9, 28, 23, 41)
    assert fmt_local(_ms_tokyo(2026, 9, 28, 23, 41), now) == "23:41"
    assert fmt_local(_ms_tokyo(2026, 9, 28, 0, 5), now) == "00:05"


def test_fmt_local_other_day(tokyo):
    now = _ms_tokyo(2026, 9, 28, 23, 41)
    assert fmt_local(_ms_tokyo(2026, 9, 27, 23, 41), now) == "27.09 23:41"
    assert fmt_local(_ms_tokyo(2026, 9, 29, 0, 10), now) == "29.09 00:10"


def test_parse_full_local(tokyo):
    now = _ms_tokyo(2026, 9, 28, 23, 41)
    assert parse_since("2026-09-28 20:00", now) == _ms_tokyo(2026, 9, 28, 20, 0)
    assert parse_since("2026-01-05 09:30", now) == _ms_tokyo(2026, 1, 5, 9, 30)


def test_parse_today(tokyo):
    now = _ms_tokyo(2026, 9, 28, 23, 41)
    assert parse_since("сегодня 20:00", now) == _ms_tokyo(2026, 9, 28, 20, 0)
    assert parse_since("сегодня 00:00", now) == _ms_tokyo(2026, 9, 28, 0, 0)


def test_parse_yesterday(tokyo):
    now = _ms_tokyo(2026, 9, 28, 23, 41)
    assert parse_since("вчера 20:00", now) == _ms_tokyo(2026, 9, 27, 20, 0)
    assert parse_since("вчера", now) == _ms_tokyo(2026, 9, 27, 0, 0)


def test_parse_relative(tokyo):
    now = _ms_tokyo(2026, 9, 28, 23, 41)
    assert parse_since("2ч", now) == now - 2 * 3_600_000
    assert parse_since("30м", now) == now - 30 * 60_000
    assert parse_since("1д", now) == now - 86_400_000
    assert parse_since(" 2 ч ", now) == now - 2 * 3_600_000


def test_parse_local_not_utc(tokyo):
    # Токио +9: 20:00 локального ≠ 20:00 UTC.
    now = _ms_tokyo(2026, 9, 28, 23, 41)
    got = parse_since("2026-09-28 20:00", now)
    assert got == int(datetime(2026, 9, 28, 20, 0, tzinfo=TOKYO).timestamp() * 1000)
    assert got != int(datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_parse_dst_berlin(berlin):
    now = _ms_berlin(2026, 7, 15, 23, 41)
    assert parse_since("2026-01-15 12:00", now) == _ms_berlin(2026, 1, 15, 12, 0)
    assert parse_since("2026-07-15 12:00", now) == _ms_berlin(2026, 7, 15, 12, 0)
    assert parse_since("сегодня 12:00", now) == _ms_berlin(2026, 7, 15, 12, 0)


def test_parse_dst_system_berlin(system_berlin):
    now = int(datetime(2026, 7, 15, 23, 41).astimezone().timestamp() * 1000)
    assert parse_since("2026-01-15 12:00", now) == int(datetime(2026, 1, 15, 12, 0).astimezone().timestamp() * 1000)
    assert parse_since("2026-07-15 12:00", now) == int(datetime(2026, 7, 15, 12, 0).astimezone().timestamp() * 1000)


def test_parse_system_uses_system_tz(monkeypatch):
    monkeypatch.delenv("AHUB_TZ", raising=False)
    now = int(datetime(2026, 9, 28, 23, 41).astimezone().timestamp() * 1000)
    got = parse_since("2026-09-28 20:00", now)
    assert got == int(datetime(2026, 9, 28, 20, 0).astimezone().timestamp() * 1000)


def test_parse_invalid_tz_uses_system(monkeypatch):
    monkeypatch.setenv("AHUB_TZ", "bad/Name_xx")
    now = int(datetime(2026, 9, 28, 23, 41).astimezone().timestamp() * 1000)
    got = parse_since("сегодня 20:00", now)  # без падения
    assert got == int(datetime(2026, 9, 28, 20, 0).astimezone().timestamp() * 1000)


def test_parse_duration_units():
    assert parse_duration("90") == 90
    assert parse_duration("30s") == 30
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("4h") == 4 * 3600
    assert parse_duration("2ч") == 2 * 3600
    assert parse_duration("15м") == 15 * 60
    assert parse_duration("30с") == 30


def test_parse_duration_fractional():
    assert parse_duration("1.5h") == pytest.approx(5400.0)
    assert parse_duration("0.5m") == pytest.approx(30.0)
    assert parse_duration("2.5s") == pytest.approx(2.5)


def test_parse_duration_spaces():
    assert parse_duration(" 90 ") == 90
    assert parse_duration(" 30 m ") == 30 * 60
    assert parse_duration("4 h") == 4 * 3600
    assert parse_duration(" 2 ч ") == 2 * 3600


@pytest.mark.parametrize("bad", ["", "abc", "10x", "m", "--5", "10 sm", "1.2.3h"])
def test_parse_duration_errors(bad):
    with pytest.raises(ValueError, match="непонятная длительность"):
        parse_duration(bad)
