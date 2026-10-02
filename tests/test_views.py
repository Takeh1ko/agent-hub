"""Тесты _age: минуты, часы, дни, граница 24 ч."""

from __future__ import annotations

from ahub.views import _age, report_essence


def test_minuty():
    now = 1_000_000_000_000
    assert _age(now - 5 * 60000, now) == "5 мин"
    assert _age(now, now) == "0 мин"


def test_chasy():
    now = 1_000_000_000_000
    assert _age(now - 90 * 60000, now) == "1 ч 30 мин"
    assert _age(now - 59 * 60000, now) == "59 мин"


def test_granitsa_24_ch():
    now = 1_000_000_000_000
    assert _age(now - (23 * 60 + 59) * 60000, now) == "23 ч 59 мин"
    assert _age(now - 24 * 60 * 60000, now) == "1 д 0 ч"


def test_dni():
    now = 1_000_000_000_000
    assert _age(now - (26 * 60 + 15) * 60000, now) == "1 д 2 ч"
    assert _age(now - (3 * 24 * 60 + 5 * 60) * 60000, now) == "3 д 5 ч"


def test_essence_ru():
    rep = "## Суть\nутечка в core/a.py:1\n\n## Подробно\nмного текста\n"
    assert "утечка" in report_essence(rep) and "Подробно" not in report_essence(rep)


def test_essence_summary_en():
    rep = "## Summary\nleak in core/a.py:1\n\n## Details\nlots of text\n"
    assert "leak" in report_essence(rep) and "Details" not in report_essence(rep)
