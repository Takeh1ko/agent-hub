"""parse_since/to_local/fmt_local: все форматы, локальная зона."""

from __future__ import annotations

from datetime import datetime

import pytest

from ahub.time import TZ, fmt_local, now_ms, parse_duration, parse_since, to_local


def _ms(y, mo, d, hh=0, mm=0) -> int:
    return int(datetime(y, mo, d, hh, mm, tzinfo=TZ).timestamp() * 1000)


def test_now_ms_monotonic():
    a, b = now_ms(), now_ms()
    assert isinstance(a, int) and b >= a


def test_to_local_zone():
    dt = to_local(0)
    assert dt.tzinfo is not None
    assert dt.utcoffset() is not None
    assert str(dt.tzinfo) == "Asia/Yekaterinburg" or dt.tzname()


def test_fmt_local_today():
    now = _ms(2026, 9, 28, 23, 41)
    assert fmt_local(_ms(2026, 9, 28, 23, 41), now) == "23:41"
    assert fmt_local(_ms(2026, 9, 28, 0, 5), now) == "00:05"


def test_fmt_local_other_day():
    now = _ms(2026, 9, 28, 23, 41)
    assert fmt_local(_ms(2026, 9, 27, 23, 41), now) == "27.09 23:41"
    assert fmt_local(_ms(2026, 9, 29, 0, 10), now) == "29.09 00:10"


def test_parse_full_local():
    now = _ms(2026, 9, 28, 23, 41)
    assert parse_since("2026-09-28 20:00", now) == _ms(2026, 9, 28, 20, 0)
    assert parse_since("2026-01-05 09:30", now) == _ms(2026, 1, 5, 9, 30)


def test_parse_today():
    now = _ms(2026, 9, 28, 23, 41)
    assert parse_since("сегодня 20:00", now) == _ms(2026, 9, 28, 20, 0)
    assert parse_since("сегодня 00:00", now) == _ms(2026, 9, 28, 0, 0)


def test_parse_relative():
    now = _ms(2026, 9, 28, 23, 41)
    assert parse_since("2ч", now) == now - 2 * 3_600_000
    assert parse_since("30м", now) == now - 30 * 60_000
    assert parse_since("1д", now) == now - 86_400_000
    assert parse_since(" 2 ч ", now) == now - 2 * 3_600_000


def test_parse_local_not_utc():
    # Екатеринбург +5: 20:00 локального ≠ 20:00 UTC.
    from datetime import timezone

    now = _ms(2026, 9, 28, 23, 41)
    got = parse_since("2026-09-28 20:00", now)
    assert got == int(datetime(2026, 9, 28, 20, 0, tzinfo=TZ).timestamp() * 1000)
    assert got != int(datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)


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
