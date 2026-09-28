"""Конфиг: $HOME, поиск вверх, список проектов, примеры из репозитория."""

from __future__ import annotations

from pathlib import Path

from hub.config import ProjectConfig, load_project, load_projects

TOML = """\
schema_version = 1
name = "Demo"
root = "$HOME/Projects/Demo"
worktrees = "~/Projects/Demo-wt"
rules = "docs/agents/rules.md"
python = "$HOME/Projects/Demo/venv/bin/python"
test_lock = "/tmp/demo.lock"
work_branch = "market"
push = ""
allowed_paths = ["core/**", "tests/**"]

[hooks]
task_setup = "make setup"
task_cleanup = ""

[defaults]
executor = "muse"
reviewers = ["muse", "mimoflash"]
budget_go = 0.5

[levels]
easy = "gemini"
medium = "musefree"
hard = "muse"
"""


def test_home_expands_and_upward_search(tmp_path, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    proj = tmp_path / "P"
    (proj / "sub" / "deep").mkdir(parents=True)
    (proj / ".hub.toml").write_text(TOML, encoding="utf-8")
    cfg = load_project(proj / "sub" / "deep")
    assert isinstance(cfg, ProjectConfig)
    assert cfg.name == "Demo"
    assert cfg.root == f"{home}/Projects/Demo"
    assert cfg.worktrees == f"{home}/Projects/Demo-wt"
    assert cfg.python == f"{home}/Projects/Demo/venv/bin/python"
    assert cfg.defaults.executor == "muse"
    assert cfg.defaults.reviewers == ["muse", "mimoflash"]
    assert cfg.defaults.budget_go == 0.5
    assert cfg.levels == {"easy": "gemini", "medium": "musefree", "hard": "muse"}
    assert cfg.hooks.task_setup == "make setup"


def test_load_project_from_file_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "P"
    proj.mkdir()
    (proj / ".hub.toml").write_text(TOML, encoding="utf-8")
    f = proj / "card.md"
    f.write_text("x", encoding="utf-8")
    assert load_project(f).name == "Demo"


def test_load_project_missing(tmp_path):
    try:
        load_project(tmp_path)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("ожидался FileNotFoundError")


def test_load_projects_missing_file(tmp_path):
    assert load_projects(tmp_path / "нет-такого.toml") == []


def test_load_projects_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("A", "B"):
        d = tmp_path / "Projects" / name
        d.mkdir(parents=True)
        (d / ".hub.toml").write_text(TOML.replace('name = "Demo"', f'name = "{name}"'),
                                     encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text('projects = ["~/Projects/A", "~/Projects/B", "~/Projects/Нет"]\n',
                   encoding="utf-8")
    got = load_projects(cfg)
    assert [c.name for c in got] == ["A", "B"]


def test_repo_examples_exist():
    root = Path(__file__).resolve().parents[1]
    assert (root / ".hub.toml").is_file()
    assert (root / "docs" / "examples" / "PlayerUP.hub.toml").is_file()
    assert load_project(root).name == "agent-hub"
