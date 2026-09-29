"""Линт карточки: фикстуры tests/fixtures/cards/, collect-only, blind."""

from __future__ import annotations

import sys
from pathlib import Path

from hub.cli import main
from hub.config import ProjectConfig
from hub.gate.lint import lint_card, strip_arbiter

REPO = Path(__file__).resolve().parents[1]
GOOD = REPO / "tests" / "fixtures" / "cards" / "good.md"
SECRET = "ЭТАЛОН-СЕКРЕТ-12345"


def _proj(root: Path | str = REPO) -> ProjectConfig:
    return ProjectConfig(
        root=str(root),
        rules="docs/agents/rules.md",
        python=sys.executable,
        test_lock="/tmp/hub-selftest.lock",
        allowed_paths=["hub/**", "tests/**", "docs/**"],
    )


def test_good_ok():
    r = lint_card(GOOD, _proj())
    assert r.ok, r.errors
    assert r.errors == []


def test_missing_acceptance_tmp(tmp_path):
    text = GOOD.read_text(encoding="utf-8")
    text = "\n".join(l for l in text.splitlines() if "Приёмка" not in l)
    card = tmp_path / "no-acc.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("Приёмка" in e for e in r.errors)


def test_bad_glob(tmp_path):
    text = GOOD.read_text(encoding="utf-8").replace("hub/gate/lint.py", "core/secret.py")
    card = tmp_path / "bad-glob.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("allowed_paths" in e and "core/secret.py" in e for e in r.errors)


def test_tests_glob_needs_allowed(tmp_path):
    # tests/** только если в allowed_paths: уберём tests/** — карточка с tests/*.py не ok.
    proj = ProjectConfig(
        root=str(REPO),
        rules="docs/agents/rules.md",
        python=sys.executable,
        allowed_paths=["hub/**", "docs/**"],
    )
    r = lint_card(GOOD, proj)
    assert not r.ok
    assert any("tests/test_lint.py" in e for e in r.errors)


def test_missing_read_path(tmp_path):
    text = GOOD.read_text(encoding="utf-8").replace("hub/config.py", "hub/нет-такого.py")
    card = tmp_path / "bad-read.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("нет пути" in e and "нет-такого" in e for e in r.errors)


def test_too_big(tmp_path):
    text = GOOD.read_text(encoding="utf-8")
    pad = "\n" + "x" * (13 * 1024)
    card = tmp_path / "big.md"
    card.write_text(text + pad, encoding="utf-8")
    assert card.stat().st_size > 12 * 1024
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("12" in e or "размер" in e for e in r.errors)


def test_strip_arbiter_removes():
    text = GOOD.read_text(encoding="utf-8")
    assert SECRET in text
    stripped = strip_arbiter(text)
    assert SECRET not in stripped
    assert "Цель" in stripped
    # Заголовок арбитра любого уровня: середина файла.
    sample = "# T\n\n## Решения арбитра (круг 1)\n\nсекрет\n\n### детали\n\nещё\n\n## Приёмка\n\nтест\n"
    got = strip_arbiter(sample)
    assert "секрет" not in got and "детали" not in got
    assert "## Приёмка" in got
    # Более глубокий старт: ### арбитр, следующий ## заканчивает.
    sample2 = "# T\n\n### Решения арбитра\n\nсекрет\n\n## Дальше\n\nок\n"
    got2 = strip_arbiter(sample2)
    assert "секрет" not in got2 and "## Дальше" in got2


def test_collect_fail_fake_worktree(tmp_path):
    # Фейковый корень: Прочитать и Можно менять — свои, Приёмка — битая нода.
    proj_root = tmp_path / "proj"
    (proj_root / "docs").mkdir(parents=True)
    (proj_root / "docs" / "spec.md").write_text("спека\n", encoding="utf-8")
    (proj_root / "tests").mkdir(parents=True)
    # Битый тест: collect-only упадёт без сети.
    (proj_root / "tests" / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    card = tmp_path / "card.md"
    card.write_text(
        "# H\n\n"
        "**Цель.** ц\n\n"
        "**Прочитать.** docs/spec.md\n\n"
        "**Можно менять.** `tests/test_broken.py`\n\n"
        "**Интерфейс.** `f()`\n\n"
        "**Приёмка.** `pytest -q tests/test_broken.py`\n\n"
        "**Нельзя.** сеть\n\n"
        "**Сеть.** нет\n\n"
        "**Исполнитель.** musefree\n\n"
        "**Уровень.** medium\n\n"
        "**Коммит.** `feat: x`\n",
        encoding="utf-8",
    )
    proj = ProjectConfig(
        root=str(proj_root),
        rules="docs/spec.md",
        python=sys.executable,
        allowed_paths=["tests/**", "docs/**"],
    )
    r = lint_card(card, proj)
    assert not r.ok
    assert any("collect-only" in e for e in r.errors)


def test_no_pytest_nodes(tmp_path):
    text = GOOD.read_text(encoding="utf-8").replace(
        "`pytest -q tests/test_time.py`", "всё руками"
    )
    card = tmp_path / "no-nodes.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("pytest" in e for e in r.errors)


def _cli_proj(tmp_path: Path) -> Path:
    """Каталог с .hub.toml, чей root — настоящий REPO (HOME в тестах подменён)."""
    d = tmp_path / "cli-proj"
    d.mkdir(exist_ok=True)
    (d / ".hub.toml").write_text(
        "schema_version = 1\n"
        f'name = "T"\nroot = "{REPO}"\n'
        'rules = "docs/agents/rules.md"\n'
        f'python = "{sys.executable}"\n'
        'test_lock = "/tmp/hub-selftest.lock"\n'
        'allowed_paths = ["hub/**", "tests/**", "docs/**"]\n',
        encoding="utf-8",
    )
    return d


def test_cli_ok_and_fail(tmp_path, capsys):
    proj = _cli_proj(tmp_path)
    assert main(["lint", str(GOOD), "--project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK ")
    # Плохая карточка → exit 1 и строка `path:line:` или `path:`.
    text = GOOD.read_text(encoding="utf-8").replace("hub/gate/lint.py", "core/secret.py")
    bad = tmp_path / "bad.md"
    bad.write_text(text, encoding="utf-8")
    assert main(["lint", str(bad), "--project", str(proj)]) == 1
    err_out = capsys.readouterr().out
    assert str(bad) in err_out
    assert "allowed_paths" in err_out


def test_cli_blind_hides_etalon(tmp_path, capsys):
    proj = _cli_proj(tmp_path)
    assert main(["lint", str(GOOD), "--project", str(proj), "--blind"]) == 0
    out = capsys.readouterr().out
    assert "--- blind ---" in out
    blind_part = out.split("--- blind ---", 1)[1]
    assert SECRET not in blind_part
    assert "Цель" in blind_part


def test_cli_broken_toml_no_traceback(tmp_path, capsys):
    # LOW (ревью): битый .hub.toml — аккуратная ошибка, а не traceback.
    d = tmp_path / "bad-proj"
    d.mkdir()
    (d / ".hub.toml").write_text("это не toml [[[\n", encoding="utf-8")
    assert main(["lint", str(GOOD), "--project", str(d)]) == 1
    out = capsys.readouterr().out
    assert "нет проекта" in out


def test_unbackticked_can_change(tmp_path):
    # Пути в «Можно менять» — только из бэктиков: голый core/secret.py
    # без бэктиков путём не считается, карточка ok.
    text = GOOD.read_text(encoding="utf-8").replace(
        "`hub/gate/lint.py`", "core/secret.py"
    )
    card = tmp_path / "unback.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors
    # Тот же путь в бэктиках — вне allowed_paths, не ok.
    text2 = GOOD.read_text(encoding="utf-8").replace(
        "`hub/gate/lint.py`", "`core/secret.py`"
    )
    card2 = tmp_path / "back.md"
    card2.write_text(text2, encoding="utf-8")
    r2 = lint_card(card2, _proj())
    assert not r2.ok
    assert any("core/secret.py" in e for e in r2.errors)


def test_star_glob_rejected(tmp_path):
    text = GOOD.read_text(encoding="utf-8").replace(
        "`hub/gate/lint.py`", "`*.py`"
    )
    card = tmp_path / "star.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("*.py" in e for e in r.errors)


def test_dotdot_glob_rejected(tmp_path):
    text = GOOD.read_text(encoding="utf-8").replace(
        "hub/gate/lint.py", "../hub/gate/lint.py"
    )
    card = tmp_path / "dotdot.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("allowed_paths" in e for e in r.errors)


def test_read_dot_hub_toml_and_prose_ok(tmp_path):
    # `.hub.toml` существует, проза со слэшами — не пути.
    import subprocess as _sp

    proj_root = tmp_path / "proj"
    (proj_root / "docs").mkdir(parents=True)
    (proj_root / "docs" / "spec.md").write_text("с\n", encoding="utf-8")
    (proj_root / ".hub.toml").write_text("x\n", encoding="utf-8")
    (proj_root / "tests").mkdir()
    (proj_root / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")
    _sp.run(["git", "init", "-b", "main"], cwd=str(proj_root),
            capture_output=True, timeout=60)
    card = proj_root / "card.md"
    card.write_text(
        "# H\n\n**Цель.** ц\n\n"
        "**Прочитать.** `.hub.toml`, docs/spec.md, событие/вопрос/inbox, "
        "подпроцессы/кнопки\n\n"
        "**Можно менять.** `docs/spec.md`\n\n"
        "**Интерфейс.** `f()`\n\n"
        "**Приёмка.** `pytest -q tests/test_a.py`\n\n"
        "**Нельзя.** сеть\n\n**Сеть.** нет\n\n"
        "**Исполнитель.** m\n\n**Уровень.** medium\n\n**Коммит.** `feat: x`\n",
        encoding="utf-8",
    )
    proj = ProjectConfig(
        root=str(proj_root),
        rules="docs/spec.md",
        python=sys.executable,
        allowed_paths=["docs/**", "tests/**", ".hub.toml"],
    )
    r = lint_card(card, proj)
    assert r.ok, r.errors
    # А отсутствующий docs/нет.md — не ok.
    card2 = proj_root / "card2.md"
    card2.write_text(
        card.read_text(encoding="utf-8").replace("docs/spec.md", "docs/нет.md"),
        encoding="utf-8",
    )
    # docs/нет.md нет, но docs/ есть → кандидат остаётся, проверка падает.
    r2 = lint_card(card2, proj)
    assert not r2.ok
    assert any("нет.md" in e for e in r2.errors)


def test_pytest_nodes_ignore_prose(tmp_path):
    # Проза «(подмена PATH/worktree фейком)» не должна стать нодой.
    from hub.gate.lint import _pytest_nodes

    sec = ("`pytest -q tests/test_lint.py tests/test_preflight.py` "
           "(подмена PATH/worktree фейком, сеть не нужна)")
    nodes = _pytest_nodes(sec)
    assert "PATH/worktree" not in nodes
    assert "tests/test_lint.py" in nodes and "tests/test_preflight.py" in nodes
    sec2 = "`pytest -q tests/` пример `/flock` замок"
    nodes2 = _pytest_nodes(sec2)
    assert "/flock" not in nodes2
    assert nodes2 == ["tests/"]


def test_bare_tests_without_pytest_is_error(tmp_path):
    text = GOOD.read_text(encoding="utf-8").replace(
        "`pytest -q tests/test_time.py`", "tests/test_time.py руками"
    )
    card = tmp_path / "bare.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("pytest" in e for e in r.errors)


def test_strip_bold_arbiter():
    sample = ("# T\n\n**Цель.** ц\n\n**Решения арбитра.**\n\nЭТАЛОН-БОЛД\n\n"
              "**Приёмка.** тест\n")
    got = strip_arbiter(sample)
    assert "ЭТАЛОН-БОЛД" not in got
    assert "**Приёмка.**" in got
    sample2 = "#T\n\n#Решения арбитра\n\nсекрет\n\n## Дальше\n\nок\n"
    assert "секрет" not in strip_arbiter(sample2)


def test_strip_bold_arbiter_swallows_deep_headers():
    # LOW (ревью): `###` внутри жирного раздела арбитра — часть раздела,
    # иначе `секрет2` течёт в blind-промпт.
    sample = (
        "**Решения арбитра.**\n\nсекрет\n\n### детали\n\nсекрет2\n\n"
        "**Приёмка.**\nтест\n"
    )
    got = strip_arbiter(sample)
    assert "секрет" not in got and "секрет2" not in got
    assert "детали" not in got
    assert "**Приёмка.**" in got


def test_collect_cwd_git_root(tmp_path):
    # Карточка во временном git-worktree, project.root указывает в другое место.
    import subprocess as _sp

    wt = tmp_path / "wt"
    wt.mkdir()
    _sp.run(["git", "init", "-b", "main"], cwd=str(wt),
            capture_output=True, timeout=60, check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=str(wt),
            capture_output=True, timeout=60, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(wt),
            capture_output=True, timeout=60, check=True)
    (wt / "docs").mkdir()
    (wt / "docs" / "spec.md").write_text("с\n", encoding="utf-8")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")
    _sp.run(["git", "add", "."], cwd=str(wt), capture_output=True, timeout=60)
    _sp.run(["git", "commit", "-m", "init"], cwd=str(wt),
            capture_output=True, timeout=60, check=True)
    other = tmp_path / "other"
    other.mkdir()
    # «Прочитать» резолвится относительно project.root — создать там же,
    # чтобы проверка читала git-корень только для collect.
    (other / "docs").mkdir(parents=True, exist_ok=True)
    (other / "docs" / "spec.md").write_text("с\n", encoding="utf-8")
    card = wt / "card.md"
    card.write_text(
        "# H\n\n**Цель.** ц\n\n**Прочитать.** docs/spec.md\n\n"
        "**Можно менять.** `tests/test_a.py`\n\n**Интерфейс.** `f()`\n\n"
        "**Приёмка.** `pytest -q tests/test_a.py`\n\n"
        "**Нельзя.** сеть\n\n**Сеть.** нет\n\n"
        "**Исполнитель.** m\n\n**Уровень.** medium\n\n**Коммит.** `feat: x`\n",
        encoding="utf-8",
    )
    proj = ProjectConfig(
        root=str(other),
        rules="docs/spec.md",
        python=sys.executable,
        allowed_paths=["tests/**", "docs/**"],
    )
    r = lint_card(card, proj)
    assert r.ok, r.errors


def test_collect_python_missing(tmp_path):
    card = tmp_path / "card.md"
    card.write_text(GOOD.read_text(encoding="utf-8"), encoding="utf-8")
    proj = ProjectConfig(
        root=str(REPO),
        rules="docs/agents/rules.md",
        python="/nonexistent/bin/python",
        allowed_paths=["hub/**", "tests/**", "docs/**"],
    )
    r = lint_card(card, proj)
    assert not r.ok
    assert any("питон" in e for e in r.errors)


def test_header_mention_is_not_section(tmp_path):
    # HIGH: упоминание `см. **Цель** выше` в другом разделе — не заголовок.
    base = GOOD.read_text(encoding="utf-8")
    # Убрать настоящий `**Цель.**`, добавить прозу в «Прочитать».
    lines = [l for l in base.splitlines() if not l.strip().startswith("**Цель.")]
    assert any("**Цель" in l for l in base.splitlines())
    text = "\n".join(lines).replace(
        "**Прочитать.**", "**Прочитать.** см. **Цель** выше,"
    )
    card = tmp_path / "no-goal.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("нет раздела «Цель»" in e for e in r.errors)


def test_header_mention_acceptance_not_section(tmp_path):
    # HIGH (mimoflash): `см. **Приёмка** ...` в «Интерфейс» — не раздел.
    base = GOOD.read_text(encoding="utf-8")
    lines = [l for l in base.splitlines() if "Приёмка" not in l]
    text = "\n".join(lines).replace(
        "**Интерфейс.**",
        "**Интерфейс.** см. **Приёмка** `pytest -q tests/test_time.py`,",
    )
    card = tmp_path / "no-acc2.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("нет раздела «Приёмка»" in e for e in r.errors)


def test_header_mention_with_dot_in_interface_not_section(tmp_path):
    # HIGH (ревью): упоминание с точкой в чужой строке — `см. **Приёмка.**`
    # внутри `**Интерфейс.**` — разделом не считается, карточка битая.
    base = GOOD.read_text(encoding="utf-8")
    lines = [l for l in base.splitlines() if "Приёмка" not in l]
    out = []
    for l in lines:
        if l.strip().startswith("**Интерфейс."):
            l = l + " см. **Приёмка.** `pytest -q tests/test_time.py`"
        out.append(l)
    card = tmp_path / "no-acc-dot.md"
    card.write_text("\n".join(out) + "\n", encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("нет раздела «Приёмка»" in e for e in r.errors)


def test_header_mention_level_not_section(tmp_path):
    # «Уровень» необязателен: `**Уровень** см. в карточке` в чужой строке —
    # не раздел, но карточка ok (уровень выводится из «Исполнитель»).
    from hub.gate.lint import _header_line_no, card_level

    base = GOOD.read_text(encoding="utf-8")
    lines = [l for l in base.splitlines() if not l.strip().startswith("**Уровень.")]
    text = "\n".join(lines).replace(
        "**Сеть.** нет.", "**Сеть.** нет; **Уровень** см. в карточке."
    )
    card = tmp_path / "no-level.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors
    # Проза разделом не стала — уровень именно выведен из исполнителя.
    assert _header_line_no(text.splitlines(), "Уровень") is None
    assert card_level(text.splitlines()) == "medium"  # musefree из GOOD


def test_header_prose_mention_with_dot_not_section(tmp_path):
    # arbiter №1: строка не начинается с `**Уровень` — упоминание в прозе
    # с точкой (`см. **Уровень.** medium`) разделом не считается,
    # но «Уровень» необязателен — карточка ok.
    from hub.gate.lint import _header_line_no, card_level

    base = GOOD.read_text(encoding="utf-8")
    lines = [l for l in base.splitlines() if not l.strip().startswith("**Уровень.")]
    text = "\n".join(lines) + "\n\nОб этом сказано: см. **Уровень.** medium.\n"
    card = tmp_path / "no-level2.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors
    # Проза разделом не стала — уровень именно выведен из исполнителя.
    assert _header_line_no(text.splitlines(), "Уровень") is None
    assert card_level(text.splitlines()) == "medium"  # musefree, не проза


def test_combined_sections_line_is_section(tmp_path):
    # Формат карточек docs/tasks/*.md: `**Сеть.** нет. **Уровень.** …` в одну
    # строку — разделы находятся (иначе линт не пропускает живые карточки).
    base = GOOD.read_text(encoding="utf-8")
    drop = ("**Сеть.", "**Уровень.", "**Исполнитель.")
    out: list[str] = []
    inserted = False
    for line in base.splitlines():
        if line.strip().startswith(drop):
            if not inserted:
                out.append(
                    "**Сеть.** нет. **Уровень.** medium. **Исполнитель.** musefree."
                )
                inserted = True
            continue
        out.append(line)
    assert inserted
    card = tmp_path / "combined.md"
    card.write_text("\n".join(out) + "\n", encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors


def test_strip_arbiter_bold_mention_inside_markdown_no_leak():
    # Утечка эталона: жирное упоминание раздела в середине строки внутри
    # markdown-раздела арбитра не кончает раздел — секреты вырезаются целиком,
    # следующий настоящий заголовок раздела остаётся.
    sample = (
        "# T\n\n## Решения арбитра\n\nсекрет-1\n\n"
        "Об этом сказано: см. **Приёмка.** ниже\n\nсекрет-2\n\n"
        "**Приёмка.**\npytest -q tests/x.py\n"
    )
    got = strip_arbiter(sample)
    # Решение арбитра H02 №3: markdown-раздел кончается только заголовком того же/высшего уровня —
    # жирная строка после него считается частью раздела (утечка эталона опаснее).
    assert "секрет-1" not in got and "секрет-2" not in got
    assert got.startswith("# T")


def test_read_missing_first_segment_is_error(tmp_path):
    # HIGH: `core/secret.py` (нет каталога core) — «нет пути», не молча ok.
    text = GOOD.read_text(encoding="utf-8").replace("docs/spec.md", "core/secret.py")
    card = tmp_path / "bad-first.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("нет пути" in e and "core/secret.py" in e for e in r.errors)
    # Тот же класс: `docs2/nope.md` при живом docs/.
    text2 = GOOD.read_text(encoding="utf-8").replace("docs/spec.md", "docs2/nope.md")
    card2 = tmp_path / "bad-first2.md"
    card2.write_text(text2, encoding="utf-8")
    r2 = lint_card(card2, _proj())
    assert not r2.ok
    assert any("нет пути" in e and "docs2/nope.md" in e for e in r2.errors)


def test_read_prose_with_slash_ok(tmp_path):
    # HIGH: проза `событие/вопрос/inbox` без расширения — не путь, ok.
    base = GOOD.read_text(encoding="utf-8")
    text = base.replace("**Прочитать.**", "**Прочитать.** событие/вопрос/inbox,")
    card = tmp_path / "prose.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors


def test_read_two_paths_in_one_backtick(tmp_path):
    # LOW (ревью): второй путь в том же бэктике тоже проверяется.
    base = GOOD.read_text(encoding="utf-8")
    text = base.replace(
        "`hub/config.py`", "`docs/spec.md hub/нет-такого.py`", 1
    )
    card = tmp_path / "two-in-tick.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("нет пути" in e and "нет-такого" in e for e in r.errors)


def test_strip_arbiter_bold_inside_markdown():
    # MEDIUM: `**`-строка внутри markdown-арбитра — часть раздела (утечка эталона).
    sample = "## Решения арбитра\n\nсекрет1\n\n**жирный** текст\n\nсекрет2\n\n## Дальше\n\nок\n"
    got = strip_arbiter(sample)
    assert "секрет1" not in got and "секрет2" not in got
    assert "жирный" not in got
    assert "## Дальше" in got
    sample2 = (
        "# T\n\n## Решения арбитра (круг 1)\n\nсекрет-1\n\n"
        "**Вывод:** эталон-2\n\nещё-секрет-3\n\n## Приёмка\n\nтест\n"
    )
    got2 = strip_arbiter(sample2)
    assert "секрет-1" not in got2
    assert "эталон-2" not in got2
    assert "секрет-3" not in got2
    assert "## Приёмка" in got2


def test_can_change_split_backtick(tmp_path):
    # MEDIUM: два пути в одном бэктике, второй вне allowed_paths → не ok.
    text = GOOD.read_text(encoding="utf-8").replace(
        "`hub/gate/lint.py`, `tests/test_lint.py`",
        "`hub/gate/lint.py`, `core/secret.py tests/test_lint.py`",
    )
    card = tmp_path / "split.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("allowed_paths" in e and "core/secret.py" in e for e in r.errors)


def test_can_change_bare_filenames(tmp_path):
    # MEDIUM: голые `Makefile`, `conftest.py`, `setup.cfg` — тоже пути.
    text = GOOD.read_text(encoding="utf-8").replace(
        "`hub/gate/lint.py`, `tests/test_lint.py`",
        "`Makefile`, `conftest.py`, `setup.cfg`",
    )
    card = tmp_path / "bare-names.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("Makefile" in e for e in r.errors)


def test_acceptance_new_file_allowed_and_missing_rejected(tmp_path):
    # MEDIUM (арбитр №5): нода на новый файл из «Можно менять» — допустима.
    import subprocess as _sp

    proj_root = tmp_path / "proj"
    (proj_root / "docs").mkdir(parents=True)
    (proj_root / "docs" / "spec.md").write_text("с\n", encoding="utf-8")
    (proj_root / "tests").mkdir(parents=True)
    (proj_root / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8"
    )
    _sp.run(["git", "init", "-b", "main"], cwd=str(proj_root),
            capture_output=True, timeout=60)
    proj = ProjectConfig(
        root=str(proj_root),
        rules="docs/spec.md",
        python=sys.executable,
        allowed_paths=["tests/**", "docs/**"],
    )
    card_ok = proj_root / "card-ok.md"
    card_ok.write_text(
        "# H\n\n**Цель.** ц\n\n**Прочитать.** docs/spec.md\n\n"
        "**Можно менять.** `tests/test_a.py`, `tests/test_new.py`\n\n"
        "**Интерфейс.** `f()`\n\n"
        "**Приёмка.** `pytest -q tests/test_a.py tests/test_new.py`\n\n"
        "**Нельзя.** сеть\n\n**Сеть.** нет\n\n"
        "**Исполнитель.** m\n\n**Уровень.** medium\n\n**Коммит.** `feat: x`\n",
        encoding="utf-8",
    )
    r = lint_card(card_ok, proj)
    assert r.ok, r.errors
    # Нет файла и не в «Можно менять» — ошибка.
    card_bad = proj_root / "card-bad.md"
    card_bad.write_text(
        "# H\n\n**Цель.** ц\n\n**Прочитать.** docs/spec.md\n\n"
        "**Можно менять.** `tests/test_a.py`\n\n"
        "**Интерфейс.** `f()`\n\n"
        "**Приёмка.** `pytest -q tests/test_missing2.py`\n\n"
        "**Нельзя.** сеть\n\n**Сеть.** нет\n\n"
        "**Исполнитель.** m\n\n**Уровень.** medium\n\n**Коммит.** `feat: x`\n",
        encoding="utf-8",
    )
    r2 = lint_card(card_bad, proj)
    assert not r2.ok
    assert any("нет файла" in e and "test_missing2" in e for e in r2.errors)


def test_level_missing_ok_and_derived(tmp_path):
    # «Уровень» необязателен; выводится из «Исполнитель».
    from hub.gate.lint import card_level

    base = GOOD.read_text(encoding="utf-8")
    text = "\n".join(
        l for l in base.splitlines() if not l.strip().startswith("**Уровень.")
    ) + "\n"
    card = tmp_path / "no-level-ok.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors
    assert card_level(text.splitlines()) == "medium"  # musefree
    assert card_level(["**Исполнитель.** gemini."]) == "easy"
    assert card_level(["**Исполнитель.** muse."]) == "hard"
    assert card_level(["**Исполнитель.** musefree."]) == "medium"
    # Явный «Уровень» побеждает исполнителя.
    assert card_level(["**Уровень.** easy.", "**Исполнитель.** muse."]) == "easy"
    # Неизвестный исполнитель — medium.
    assert card_level(["**Исполнитель.** кто-то."]) == "medium"
    # Ветка project.levels: отображение берётся из конфига проекта.
    custom = ProjectConfig(
        root=str(REPO),
        rules="docs/agents/rules.md",
        python=sys.executable,
        allowed_paths=[],
        levels={"easy": "muse", "medium": "gemini", "hard": "deepseek"},
    )
    assert card_level(["**Исполнитель.** gemini."], custom) == "medium"
    assert card_level(["**Исполнитель.** muse."], custom) == "easy"
    assert card_level(["**Исполнитель.** deepseek."], custom) == "hard"


def test_interface_header_with_tail(tmp_path):
    # Заголовок с произвольным хвостом после имени — раздел находится,
    # следующий раздел не заглатывается.
    for header in (
        "**Интерфейс / что сделать.**",
        "**Интерфейс (контракт для следующих задач — не переименовывать).**",
    ):
        text = GOOD.read_text(encoding="utf-8").replace("**Интерфейс.**", header)
        card = tmp_path / "tail.md"
        card.write_text(text, encoding="utf-8")
        r = lint_card(card, _proj())
        assert r.ok, (header, r.errors)
    # А без «Интерфейса» вообще — отказ (настоящий дефект ловится).
    text = "\n".join(
        l for l in GOOD.read_text(encoding="utf-8").splitlines()
        if not l.strip().startswith("**Интерфейс")
    ) + "\n"
    card = tmp_path / "no-iface.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("нет раздела «Интерфейс»" in e for e in r.errors)


def test_can_change_ignores_markup_and_flags(tmp_path):
    # Разметка, флаги CLI и идентификаторы в «Можно менять» — не пути.
    text = GOOD.read_text(encoding="utf-8").replace(
        "`hub/gate/lint.py`, `tests/test_lint.py`",
        "`hub/gate/lint.py`, `tests/test_lint.py`, `**MEDIUM.**`, "
        "`--proxy/--proxies`, `market.*`, `import_legacy`",
    )
    card = tmp_path / "junk.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors
    # А настоящий glob вне scope в бэктиках — ловится.
    from hub.gate.lint import _can_change_globs

    assert _can_change_globs("`*.py`") == ["*.py"]
    assert _can_change_globs("`**MEDIUM.**`") == []
    assert _can_change_globs("`--proxy/--proxies`") == []
    assert _can_change_globs("`market.*`") == []
    assert _can_change_globs("`import_legacy`") == []


def test_bold_prose_is_not_header(tmp_path):
    # Жирная строка-проза (`**Сеть не нужна …**`, `**Интерфейс чик**`) —
    # не заголовок раздела.
    from hub.gate.lint import _header_line_no

    for line in (
        "**Сеть не нужна для этой задачи**",
        "**Сеть не нужна**",
        "**Интерфейс чик**",
        "**Интерфейсчик**",
    ):
        assert _header_line_no([line], "Сеть") is None, line
        assert _header_line_no([line], "Интерфейс") is None, line
    # А закрытые хвосты на `.`/`:` — заголовки.
    for line, name in (
        ("**Интерфейс / что сделать.**", "Интерфейс"),
        ("**Приёмка (без сети).**", "Приёмка"),
        ("**Сеть.** нет.", "Сеть"),
        ("**Приёмка:**", "Приёмка"),
    ):
        assert _header_line_no([line], name) == 1, line


def test_bold_prose_inside_arbiter_no_leak():
    # Жирная проза внутри «Решений арбитра» не обрывает вырезание эталона
    # (spec §7/Н11: эталон вырезается всегда, иначе утечка в промпт ревьюера).
    sample = (
        "**Решения арбитра.**\n\nсекрет-1\n\n"
        "**Сеть не нужна для этой задачи**\n\nсекрет-2\n\n"
        "**Приёмка.**\nтест\n"
    )
    got = strip_arbiter(sample)
    assert "секрет-1" not in got and "секрет-2" not in got
    assert "**Приёмка.**" in got


def test_bold_prose_does_not_cut_section(tmp_path):
    # Та же жирная строка внутри «Приёмки» не режет границы секции:
    # pytest-ноды ниже неё по-прежнему собираются, карточка ok.
    text = GOOD.read_text(encoding="utf-8").replace(
        "**Приёмка.** `pytest -q tests/test_time.py`",
        "**Приёмка.**\n\n**Сеть не нужна для этой задачи**\n\n"
        "`pytest -q tests/test_time.py`",
    )
    card = tmp_path / "prose-in-acc.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert r.ok, r.errors
