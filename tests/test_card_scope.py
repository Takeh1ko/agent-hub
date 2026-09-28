"""Карточка H01: «Можно менять» не должен ронять ворота scope.

Повтор логики ворот конвейера (run_task.py: allowed_globs/scope_violations),
чтобы противоречие «Интерфейс п.2 требует файлов, которых нет в «Можно менять»»
не вернулось: без правки карточки .hub.toml и docs/** дают ложно-красные ворота.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

CARD = Path(__file__).resolve().parents[1] / "docs" / "tasks" / "H01-core-read.md"


def _allowed() -> list[str]:
    """Маски из раздела «Можно менять» — как в run_task.allowed_globs()."""
    line = next(
        (ln for ln in CARD.read_text(encoding="utf-8").splitlines()
         if ln.startswith("**Можно менять.**")),
        "",
    )
    assert line, "в карточке нет раздела «Можно менять»"
    return re.findall(r"`([^`]+)`", line)


def _violations(paths: list[str]) -> list[str]:
    bad = []
    for path in paths:
        ok = False
        for pat in _allowed():
            if fnmatch.fnmatchcase(path, pat):
                ok = True
                break
            if pat.endswith("/**") and path == pat[:-3]:
                ok = True
                break
        if not ok:
            bad.append(path)
    return bad


def test_interface_files_pass_scope():
    """П.2 Интерфейс (.hub.toml, docs/examples/*.hub.toml) — вне scope-нарушений."""
    assert _violations([".hub.toml", "docs/examples/PlayerUP.hub.toml"]) == []


def test_card_edit_passes_scope():
    """Сама карточка в diff — тоже должна проходить scope-проверку ворот."""
    assert _violations(["docs/tasks/H01-core-read.md"]) == []


def test_out_of_scope_still_flagged():
    """Чужие пути остаются нарушением — тест не пустышка."""
    assert _violations(["tools/agents/run_task.py"]) == ["tools/agents/run_task.py"]
