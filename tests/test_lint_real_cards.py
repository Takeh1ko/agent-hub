"""Линт на реальных карточках: без ложных отказов (H02b).

Для каждой карточки корпуса `tests/fixtures/real_cards/*.md` lint не даёт
структурных ошибок (разделы, scope, эталон). `allowed_paths` — как в
`docs/examples/PlayerUP.hub.toml` для T*/S* и как в `.hub.toml` для H*;
проверки существования путей и сбора pytest-нод отключены (файлов PlayerUP
в репозитории нет). Настоящие дефекты по-прежнему ловятся (негативные тесты).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from hub.config import ProjectConfig
from hub.gate.lint import _header_line_no, card_level, lint_card, strip_arbiter

REPO = Path(__file__).resolve().parents[1]
CARDS = sorted((REPO / "tests" / "fixtures" / "real_cards").glob("*.md"))

# Ожидаемый уровень по исполнителю карточки (проверка вывода без «Уровня»).
EXPECTED_LEVEL = {
    "H01-core-read.md": "hard",
    "H05b-pult-tails.md": "medium",
    "H06-pipeline.md": "hard",
    "S03-sale-link.md": "hard",
    "T14b-daily-budget.md": "hard",
    "T17-liveness-schedule.md": "hard",
    "T18b-streams-tails.md": "background",  # musefree → background (решение владельца 2026-09-29)
    "T19-geo-tier.md": "hard",
    "T20-own-coverage.md": "hard",
}


def _allowed(toml_path: Path) -> list[str]:
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    return [str(p) for p in (data.get("allowed_paths", []) or [])]


def _levels(toml_path: Path) -> dict[str, str]:
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (data.get("levels", {}) or {}).items()}


def _proj(card: Path) -> ProjectConfig:
    if card.name[0] in ("T", "S"):
        toml = REPO / "docs" / "examples" / "PlayerUP.hub.toml"
        root = str(REPO)  # файлов PlayerUP нет — read/acceptance отключены
    else:
        toml = REPO / ".hub.toml"
        root = str(REPO)
    return ProjectConfig(
        root=root,
        rules="docs/agents/rules.md",
        python=sys.executable,
        test_lock="/tmp/hub-selftest.lock",
        allowed_paths=_allowed(toml),
        levels=_levels(toml),
    )


def _structural(card: Path):
    """Линт только структурный: без путей «Прочитать» и pytest-нод."""
    return lint_card(card, _proj(card), check_read_paths=False, check_acceptance=False)


def test_corpus_present():
    assert len(CARDS) >= 9, [p.name for p in CARDS]
    assert set(EXPECTED_LEVEL) <= {p.name for p in CARDS}


def test_real_cards_no_structural_errors():
    bad: dict[str, list[str]] = {}
    for card in CARDS:
        r = _structural(card)
        if not r.ok:
            bad[card.name] = r.errors
    assert bad == {}, bad


def test_real_cards_level_derived():
    for card in CARDS:
        lines = card.read_text(encoding="utf-8").splitlines()
        assert card_level(lines, _proj(card)) == EXPECTED_LEVEL[card.name], card.name


def _arbiter_header_no(lines: list[str]) -> int | None:
    """Номер строки заголовка «Решений арбитра» (`#…` или `**…`), если есть."""
    for i, line in enumerate(lines):
        s = line.strip()
        if "Решения арбитра" in line and (s.startswith("#") or s.startswith("**")):
            return i
    return None


def test_real_cards_blind_hides_arbiter():
    # Эталон из «Решений арбитра» не течёт в blind-текст, разделы целы.
    # (Упоминание фразы в прозе — не эталон: важен именно раздел.)
    for card in CARDS:
        text = card.read_text(encoding="utf-8")
        blind = strip_arbiter(text)
        lines = text.splitlines()
        no = _arbiter_header_no(lines)
        if no is None:
            assert blind == text, card.name
        else:
            assert _arbiter_header_no(blind.splitlines()) is None, card.name
            body = next((l for l in lines[no + 1:] if l.strip()), "")
            assert body and body not in blind, (card.name, body)
        for section in ("Цель", "Приёмка", "Коммит"):
            assert _header_line_no(blind.splitlines(), section) is not None, (
                card.name,
                section,
            )


def test_real_card_scope_defect_still_caught(tmp_path):
    # Настоящий дефект scope на реальной карточке ловится и без read/acceptance.
    src = REPO / "tests" / "fixtures" / "real_cards" / "T18b-streams-tails.md"
    text = src.read_text(encoding="utf-8").replace(
        "`docs/market/spec.md` (§4.7).",
        "`docs/market/spec.md` (§4.7), `other/secret.py`.",
        1,
    )
    tmp = tmp_path / src.name
    tmp.write_text(text, encoding="utf-8")
    r = lint_card(tmp, _proj(src), check_read_paths=False, check_acceptance=False)
    assert not r.ok
    assert any("allowed_paths" in e and "other/secret.py" in e for e in r.errors)


def test_real_card_missing_section_still_caught(tmp_path):
    # Удалённый «Интерфейс» из T20 (без «Уровня»!) — отказ по разделу.
    src = REPO / "tests" / "fixtures" / "real_cards" / "T20-own-coverage.md"
    lines = [
        l for l in src.read_text(encoding="utf-8").splitlines()
        if not l.strip().startswith("**Интерфейс.")
    ]
    assert len(lines) > 0
    tmp = tmp_path / src.name
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = lint_card(tmp, _proj(src), check_read_paths=False, check_acceptance=False)
    assert not r.ok
    assert any("нет раздела «Интерфейс»" in e for e in r.errors)


def test_real_card_empty_scope_still_caught(tmp_path):
    # Пустое «Можно менять» у H05b — отказ, а не молча ok.
    src = REPO / "tests" / "fixtures" / "real_cards" / "H05b-pult-tails.md"
    lines = []
    skipped = False
    for l in src.read_text(encoding="utf-8").splitlines():
        if l.strip().startswith("**Можно менять."):
            lines.append("**Можно менять.** починить всё")
            skipped = True
            continue
        # Строка-продолжение секции (бэктики) — выкинуть.
        if skipped and l.strip().startswith("`tests/**`"):
            skipped = False
            continue
        lines.append(l)
    tmp = tmp_path / src.name
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = lint_card(tmp, _proj(src), check_read_paths=False, check_acceptance=False)
    assert not r.ok
    assert any("без путей" in e for e in r.errors)
