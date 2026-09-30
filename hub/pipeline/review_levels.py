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

@dataclass(frozen=True)
class ReviewPlan:
    reviewers: tuple[str, ...]
    rounds: int
    label: str  # «уровень 2» / «свой»


_ROUNDS_RE = re.compile(r"кру[гз]\w*\s*[:=]?\s*(\S+)?", re.I)
_LEVEL_RE = re.compile(r"^(?:уровень\s*)?(\d+)(?:\s|$|[.,;—–-])", re.I)


def section_value(card_text: str) -> str:
    """Значение раздела `**Ревью.**` тем же разбором заголовков, что у lint
    (строка-цепочка разделов, значение на следующей строке; проза «см. **Ревью.**» — не заголовок)."""
    from hub.gate.lint import _section_text

    sec = _section_text((card_text or "").splitlines(), "Ревью")
    i = sec.find("**Ревью")
    if i < 0:
        return ""
    rest = sec[i + 2:]
    j = rest.find("**")
    if j < 0:
        return ""
    value = rest[j + 2:].split("**")[0]
    return " ".join(value.split()).strip().rstrip(".").strip()


def _norm_model(name: str) -> str:
    return name.strip().strip("`'\"«».:").lower()


def parse(value: str, known_models) -> tuple[ReviewPlan | None, str]:
    """(план, ошибка). Пусто — (None, "") — раздела нет, берутся настройки проекта.

    Уровень: «2», «2 — деньги», «уровень 2». «свой: модели; круги N» — круги обязательны.
    Свободный текст без цифр (старые карточки: «mimo (деньги)») — игнор, как до уровней;
    текст с цифрой, но не уровень («2abc») — ошибка, чтобы опечатка не ушла молча в умолчания.
    """
    v = str(value or "").strip()
    if not v:
        return None, ""
    if v.lower().startswith("свой"):
        body = v.split(":", 1)[1] if ":" in v else v[4:]
        rm = _ROUNDS_RE.search(body)
        if not rm:
            return None, "Ревью «свой»: укажи круги — «свой: muse, mimoflash; круги 2»"
        raw_rounds = (rm.group(1) or "").strip(".,;")
        if not raw_rounds.isdigit():
            return None, f"Ревью «свой»: круги «{raw_rounds or '?'}» — нужно число 1–{MAX_ROUNDS}"
        rounds = int(raw_rounds)
        if not 1 <= rounds <= MAX_ROUNDS:
            return None, f"Ревью «свой»: кругов {rounds} (можно 1–{MAX_ROUNDS})"
        models_part = body[:rm.start()] + body[rm.end():]
        names = [_norm_model(x) for x in re.split(r"[\s,;:+]+", models_part)]
        names = [x for x in names if x]
        known = {str(k).lower(): str(k) for k in known_models}
        bad = [x for x in names if x not in known]
        if bad:
            return None, f"Ревью: неизвестные модели {', '.join(bad)}"
        if not names:
            return None, "Ревью «свой»: не указаны модели"
        uniq = tuple(dict.fromkeys(known[x] for x in names))
        return ReviewPlan(uniq, rounds, "свой"), ""
    m = _LEVEL_RE.match(v)
    if m:
        n = int(m.group(1))
        if n not in LEVELS:
            return None, f"Ревью: уровень {n} неизвестен (1–4 или «свой: модели; круги N»)"
        revs, rounds = LEVELS[n]
        return ReviewPlan(revs, rounds, f"уровень {n}"), ""
    if re.search(r"\d", v):
        return None, f"Ревью: «{v}» — ожидается 1–4 или «свой: модели; круги N»"
    # Старые карточки писали «Ревью» свободным текстом («mimo (деньги)») — не уровень:
    # игнорируем, как до уровней (ревьюеры проекта).
    return None, ""


def plan_for_card(card_text: str, known_models) -> tuple[ReviewPlan | None, str]:
    return parse(section_value(card_text), known_models)
