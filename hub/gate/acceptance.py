"""Команда приёмки из карточки: pytest-пути из раздела «Приёмка» → один запуск.

Весь набор тестов проекта под общим замком занимает десятки минут (PlayerUP ~1300 тестов) и выстраивает все
задачи в очередь; старый run_task гонял только приёмку карточки — так и здесь. Нет pytest-команд — весь набор.
"""

from __future__ import annotations

import re
import shlex

_OPT_WITH_VALUE = {"-k", "-m", "-p", "--maxfail", "--deselect", "-c", "--rootdir"}


def acceptance_paths(card_text: str) -> list[str]:
    """Пути/ноды pytest из «Приёмки» (порядок сохранён, без дублей)."""
    from hub.gate.lint import _section_text

    sec = _section_text(str(card_text or "").splitlines(), "Приёмка")
    out: list[str] = []
    for m in re.finditer(r"`([^`]*\bpytest\b[^`]*)`", sec):
        try:
            toks = shlex.split(m.group(1))
        except ValueError:
            continue
        if "pytest" not in toks:
            continue
        toks = toks[toks.index("pytest") + 1:]
        skip = False
        for t in toks:
            if skip:
                skip = False
                continue
            if t in _OPT_WITH_VALUE:
                skip = True
                continue
            if t.startswith("-"):
                continue
            if t not in out:
                out.append(t)
    return out


def acceptance_cmd(card_text: str, py: str) -> list[str]:
    return [py, "-m", "pytest", "-q", *acceptance_paths(card_text)]
