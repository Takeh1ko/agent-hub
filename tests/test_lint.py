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


def test_unbackticked_can_change(tmp_path):
    # Голый core/secret.py без бэктиков — всё равно вне allowed_paths.
    text = GOOD.read_text(encoding="utf-8").replace(
        "`hub/gate/lint.py`", "core/secret.py"
    )
    card = tmp_path / "unback.md"
    card.write_text(text, encoding="utf-8")
    r = lint_card(card, _proj())
    assert not r.ok
    assert any("core/secret.py" in e for e in r.errors)


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
