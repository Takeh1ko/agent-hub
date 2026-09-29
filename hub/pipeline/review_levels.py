"""Уровни ревью (решение владельца 2026-09-30): кто проверяет задачу и сколько кругов.

Карточка задаёт раздел `**Ревью.**`:
- `1` — Spark, 1 круг;  `2` — Spark, до 2 кругов;
- `3` — Spark + MiMo Flash, 1 круг;  `4` — Spark + MiMo Flash, до 2 кругов;
- `свой: muse, mimoflash; круги 3` — вручную: модели (короткие имена) и число кругов.
Круг — «исполнитель пишет → тесты → ревью»; замечания на последнем круге — к Claude (арбитр).
Флаги `hub start --reviewers/--rounds` сильнее карточки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LEVELS: dict[int, tuple[tuple[str, ...], int]] = {
    1: (("muse",), 1),
    2: (("muse",), 2),
    3: (("muse", "mimoflash"), 1),
    4: (("muse", "mimoflash"), 2),
}

MAX_ROUNDS = 5

_SECTION_RE = re.compile(r"\*\*Ревью[^*]*\*\*\s*(.*)")
_ROUNDS_RE = re.compile(r"кру[гз]\w*\s*[:=]?\s*(\d+)", re.I)


@dataclass(frozen=True)
class ReviewPlan:
    reviewers: tuple[str, ...]
    rounds: int
    label: str  # «уровень 2» / «свой»


def section_value(card_text: str) -> str:
    """Текст после `**Ревью.**` до следующего жирного заголовка или конца строки."""
    for line in (card_text or "").splitlines():
        m = _SECTION_RE.search(line)
        if m and (line.lstrip().startswith("**Ревью") or " **Ревью" in line):
            return m.group(1).split("**")[0].strip().rstrip(".").strip()
    return ""


def parse(value: str, known_models) -> tuple[ReviewPlan | None, str]:
    """(план, ошибка). Пусто — (None, "") — раздела нет, берутся настройки проекта."""
    v = str(value or "").strip()
    if not v:
        return None, ""
    m = re.fullmatch(r"(\d+)\b.*", v)
    if m and not v.lower().startswith("свой"):
        n = int(m.group(1))
        if n not in LEVELS:
            return None, f"Ревью: уровень {n} неизвестен (1–4 или «свой: модели; круги N»)"
        revs, rounds = LEVELS[n]
        return ReviewPlan(revs, rounds, f"уровень {n}"), ""
    if v.lower().startswith("свой"):
        body = v.split(":", 1)[1] if ":" in v else v[4:]
        rm = _ROUNDS_RE.search(body)
        rounds = int(rm.group(1)) if rm else 1
        models_part = _ROUNDS_RE.sub("", body)
        names = [x for x in re.split(r"[\s,;+]+", models_part) if x]
        known = set(known_models)
        bad = [x for x in names if x not in known]
        if bad:
            return None, f"Ревью: неизвестные модели {', '.join(bad)}"
        if not names:
            return None, "Ревью «свой»: не указаны модели"
        if not 1 <= rounds <= MAX_ROUNDS:
            return None, f"Ревью «свой»: кругов {rounds} (можно 1–{MAX_ROUNDS})"
        uniq = tuple(dict.fromkeys(names))
        return ReviewPlan(uniq, rounds, "свой"), ""
    # Старые карточки писали «Ревью» свободным текстом («mimo (деньги)») — не уровень:
    # игнорируем, как до уровней (ревьюеры проекта). Строго проверяются только цифра и «свой».
    return None, ""


def plan_for_card(card_text: str, known_models) -> tuple[ReviewPlan | None, str]:
    return parse(section_value(card_text), known_models)
