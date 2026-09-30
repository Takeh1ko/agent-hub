"""Уровни ревью в карточке (решение владельца 2026-09-30): 1–4 и «свой»."""

from __future__ import annotations

import json

from hub.pipeline import review_levels as rl
from hub.pipeline.runners import MODELS

CARD = """# T1 — задача

**Цель.** что-то.

**Сеть.** нет. **Уровень.** hard. **Исполнитель.** muse. **Ревью.** {review}
"""


def plan(review: str):
    return rl.plan_for_card(CARD.format(review=review), MODELS)


def test_levels_1_to_4():
    assert plan("1") == (rl.ReviewPlan(("muse",), 1, "уровень 1"), "")
    assert plan("2.") == (rl.ReviewPlan(("muse",), 2, "уровень 2"), "")
    assert plan("3 — деньги") == (rl.ReviewPlan(("muse", "mimoflash"), 1, "уровень 3"), "")
    assert plan("4") == (rl.ReviewPlan(("muse", "mimoflash"), 2, "уровень 4"), "")


def test_custom_models_and_rounds():
    p, err = plan("свой: muse, mimoflash; круги 3")
    assert err == "" and p == rl.ReviewPlan(("muse", "mimoflash"), 3, "свой")
    p, err = plan("свой: `Muse`, MiMoFlash: круги 2")  # регистр/бэктики/двоеточие
    assert err == "" and p == rl.ReviewPlan(("muse", "mimoflash"), 2, "свой")
    assert plan("уровень 2")[0].rounds == 2


def test_bad_values():
    assert plan("7")[1].startswith("Ревью: уровень 7 неизвестен")
    assert "неизвестные модели" in plan("свой: gpt9; круги 2")[1]
    assert "кругов 9" in plan("свой: muse; круги 9")[1]
    assert "укажи круги" in plan("свой: mimoflash")[1]  # забытые круги — не молча 1
    assert "нужно число" in plan("свой: muse; круги много")[1]
    assert "ожидается" in plan("2abc")[1]  # опечатка с цифрой — ошибка, не умолчания
    # Свободный текст старых карточек — не уровень: раздел игнорируется, не ошибка.
    assert plan("mimo (деньги)") == (None, "")


def test_no_section_means_project_defaults():
    assert rl.plan_for_card("# T1\n\n**Цель.** x\n", MODELS) == (None, "")


def test_section_on_own_line():
    text = "# T\n\n**Ревью.** 2\n\n**Коммит.** `x`\n"
    assert rl.plan_for_card(text, MODELS)[0].rounds == 2
    # Значение на следующей строке.
    assert rl.plan_for_card("# T\n\n**Ревью.**\n3\n\n**Коммит.** x\n", MODELS)[0].label == "уровень 3"


def test_prose_mention_is_not_section():
    text = "# T\n\n**Цель.** см. **Ревью.** 9 в тексте\n\n**Коммит.** x\n"
    assert rl.plan_for_card(text, MODELS) == (None, "")


# --- hub start и lint ---

def _project(tmp_path):
    import subprocess

    root = tmp_path / "proj"
    (root / "docs" / "tasks").mkdir(parents=True)
    (root / "hub").mkdir()
    (root / "hub" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (root / ".hub.toml").write_text(
        'schema_version = 1\nname = "P"\nroot = "{r}"\nworktrees = "{w}"\n'
        'test_cmd = "pytest -q"\nwork_branch = "main"\n'
        'allowed_paths = ["hub/**", "tests/**", "docs/**"]\n'
        '[defaults]\nexecutor = "muse"\nreviewers = ["muse", "mimoflash"]\n'
        .format(r=root, w=tmp_path / "wt"), encoding="utf-8")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True)
    return root


def _card(root, review_line: str) -> str:
    card = root / "docs" / "tasks" / "T1-x.md"
    card.write_text(
        "# T1 — задача\n\n**Цель.** проверить уровни.\n\n**Прочитать.** `hub/x.py`\n\n"
        "**Можно менять.** `hub/x.py`\n\n**Интерфейс / что сделать.** поменять.\n\n"
        "**Приёмка.** `pytest -q tests/test_x.py`\n\n**Нельзя.** сеть.\n\n"
        f"**Сеть.** нет. **Уровень.** hard. **Исполнитель.** muse.{review_line}\n\n"
        "**Коммит.** `fix: x`\n", encoding="utf-8")
    return str(card)


def _task(tid="T1-x"):
    from hub.store import Store

    return Store().get_task(tid)


def test_start_takes_reviewers_and_rounds_from_card(tmp_path, capsys):
    from hub.cli import main

    root = _project(tmp_path)
    card = _card(root, " **Ревью.** 1")
    assert main(["start", card, "--project", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "OK T1-x"  # первая строка машиночитаемая, как раньше
    assert "ревью: уровень 1 — muse, кругов 1" in out
    t = _task()
    assert json.loads(t["reviewers_json"]) == ["muse"] and int(t["rounds"]) == 1


def test_start_flags_beat_card(tmp_path, capsys):
    from hub.cli import main

    root = _project(tmp_path)
    card = _card(root, " **Ревью.** 4")
    assert main(["start", card, "--project", str(root), "--reviewers", "mimoflash",
                 "--rounds", "3"]) == 0
    t = _task()
    assert json.loads(t["reviewers_json"]) == ["mimoflash"] and int(t["rounds"]) == 3


def test_start_without_section_uses_project_defaults(tmp_path, capsys):
    from hub.cli import main

    root = _project(tmp_path)
    card = _card(root, "")
    assert main(["start", card, "--project", str(root)]) == 0
    t = _task()
    assert json.loads(t["reviewers_json"]) == ["muse", "mimoflash"] and int(t["rounds"]) == 2
    assert "по умолчанию проекта" in capsys.readouterr().out


def test_lint_rejects_bad_review(tmp_path):
    from hub.config import load_project
    from hub.gate.lint import lint_card

    root = _project(tmp_path)
    card = _card(root, " **Ревью.** 9")
    res = lint_card(card, load_project(str(root)))
    assert not res.ok and any("уровень 9" in e for e in res.errors)
    ok = lint_card(_card(root, " **Ревью.** 3"), load_project(str(root)))
    assert ok.ok, ok.errors


def test_start_rejects_bad_rounds_flag(tmp_path, capsys):
    from hub.cli import main

    root = _project(tmp_path)
    card = _card(root, " **Ревью.** 2")
    for bad in ("0", "-1", "9"):
        assert main(["start", card, "--project", str(root), "--rounds", bad]) == 1
    assert "можно 1–5" in capsys.readouterr().out
