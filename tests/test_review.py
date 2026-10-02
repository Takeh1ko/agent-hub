"""Вырезание решений-эталона из постановки для ревьюера (RU и EN)."""

from __future__ import annotations

from ahub.review import strip_arbiter


def test_strip_arbiter_ru():
    spec = "# Задача\nсделать кнопку\n## Решение арбитра\nслить так-то\n## Приёмка\nтесты\n"
    out = strip_arbiter(spec)
    assert "арбитра" not in out and "кнопку" in out and "Приёмка" in out


def test_strip_arbiter_decision_en():
    spec = "# Task\ndo button\n## Arbiter decision\nmerge this way\n## Acceptance\ntests\n"
    out = strip_arbiter(spec)
    assert "Arbiter" not in out and "merge" not in out and "button" in out and "Acceptance" in out


def test_strip_orchestrator_decision_en_case_insensitive():
    spec = "# Task\ndo button\n## orchestrator decision\nmerge that way\n## Acceptance\ntests\n"
    out = strip_arbiter(spec)
    assert "orchestrator" not in out.lower() and "merge" not in out
