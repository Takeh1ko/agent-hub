from __future__ import annotations

import os

import pytest

from ahub import config, paths
from tests.conftest import write

V2 = """
schema_version = 2
name = "Demo"
worktrees = "$HOME/demo-wt"
work_branch = "market"
python = "/usr/bin/python3"
allowed_paths = ["core/**", "tests/**"]
max_parallel = 3
test_resource = "test_db"

[resources]
test_db = { lock = "/tmp/demo.lock" }
playerok = { capacity = 1 }
short = "/tmp/short.lock"

[hooks]
task_setup = "make db"

[models]
deny = ["deepseek"]

[budget]
go = 2.5

[secrets]
exclude = ["config/prod.toml"]

[timeouts]
idle_s = 600
retry_max = 50
"""


def test_parse_v2_full(tmp_path):
    f = write(tmp_path / "demo" / ".hub.toml", V2)
    cfg = config.load_project(tmp_path / "demo")
    assert cfg.name == "Demo"
    assert cfg.root == str(tmp_path / "demo")  # по умолчанию — каталог файла
    assert cfg.source == str(f)
    assert cfg.worktrees == os.path.expandvars("$HOME/demo-wt")
    assert cfg.work_branch == "market"
    assert cfg.branch_prefix == "ahub/"
    assert cfg.allowed_paths == ("core/**", "tests/**")
    assert cfg.max_parallel == 3
    assert cfg.resources["test_db"].lock == "/tmp/demo.lock"
    assert cfg.resources["playerok"].capacity == 1 and cfg.resources["playerok"].lock == ""
    assert cfg.resources["short"].lock == "/tmp/short.lock"
    assert cfg.test_resource == "test_db"
    assert cfg.hooks.task_setup == "make db"
    assert cfg.models_deny == ("deepseek",)
    assert cfg.budget_go == 2.5 and cfg.budget_usd == 0.0
    assert "config/prod.toml" in cfg.secret_excludes and ".env" in cfg.secret_excludes
    assert cfg.timeouts.idle_s == 600
    assert cfg.timeouts.retry_max == 10  # потолок


def test_find_upward(tmp_path):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\n')
    deep = tmp_path / "p" / "a" / "b"
    deep.mkdir(parents=True)
    assert config.load_project(deep).name == "P"
    with pytest.raises(FileNotFoundError):
        config.load_project(tmp_path)


def test_all_errors_at_once():
    data = {
        "schema_version": 2,
        "max_parallel": 0,
        "allowed_paths": "a/**, b/**",  # строка через запятую допустима
        "test_resource": "nope",
        "budget": {"go": -1},
        "resources": {"x": {"capacity": "два"}},
    }
    with pytest.raises(config.ConfigError) as ei:
        config.parse_project(data, "/tmp")
    errs = " | ".join(ei.value.errors)
    assert "name" in errs
    assert "max_parallel" in errs
    assert "test_resource" in errs
    assert "budget.go" in errs
    assert "resources.x.capacity" in errs
    assert len(ei.value.errors) == 5


def test_unknown_schema_version():
    with pytest.raises(config.ConfigError, match="schema_version"):
        config.parse_project({"schema_version": 7, "name": "x"}, "/tmp")


def test_bad_toml(tmp_path):
    write(tmp_path / ".hub.toml", "name = \n")
    with pytest.raises(config.ConfigError, match="TOML"):
        config.load_project(tmp_path)


def test_v1_file_translated(tmp_path):
    write(tmp_path / ".hub.toml", """
schema_version = 1
name = "Old"
root = "/srv/old"
test_lock = "/tmp/old.lock"
work_branch = "market"
idle_s = 300
retry_max = 2
[defaults]
executor = "muse"
budget_go = 1.5
budget_usd = 0.0
[levels]
easy = "gemini"
""")
    cfg = config.load_project(tmp_path)
    assert cfg.name == "Old" and cfg.root == "/srv/old"
    assert cfg.resources["test_lock"].lock == "/tmp/old.lock"
    assert cfg.test_resource == "test_lock"
    assert cfg.budget_go == 1.5
    assert cfg.timeouts.idle_s == 300 and cfg.timeouts.retry_max == 2


def test_check_project(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    cfg = config.parse_project({"schema_version": 2, "name": "R", "python": str(tmp_path / "nopy"),
                                "rules": "docs/rules.md", "worktrees": str(tmp_path / "x" / "y")}, root)
    problems = config.check_project(cfg)
    assert any(p.startswith("python") for p in problems)
    assert any(p.startswith("rules") for p in problems)
    assert any(p.startswith("worktrees") for p in problems)
    assert not any(p.startswith("root") for p in problems)


def test_hub_config_and_projects(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    write(a / ".hub.toml", 'schema_version = 2\nname = "A"\n')
    write(b / ".hub.toml", 'schema_version = 2\nname = "A"\n')  # дубль имени
    write(paths.global_config_path(),
          f'projects = ["{a}", "{b}", "{tmp_path / "missing"}"]\n')
    projects, errors = config.load_projects()
    assert [p.name for p in projects] == ["A"]
    assert any("уже занято" in e for e in errors)
    assert any("missing" in e for e in errors)


def test_hub_config_legacy_fallback(tmp_path):
    a = tmp_path / "a"
    write(a / ".hub.toml", 'schema_version = 2\nname = "A"\n')
    write(tmp_path / ".config" / "agent-hub" / "config.toml", f'projects = ["{a}"]\n')
    hub = config.load_hub()
    assert hub.projects == (str(a),)


def test_project_for_root_and_worktrees(tmp_path):
    outer = config.parse_project({"schema_version": 2, "name": "Outer",
                                  "worktrees": str(tmp_path / "outer-wt")}, tmp_path / "outer")
    inner = config.parse_project({"schema_version": 2, "name": "Inner"}, tmp_path / "outer" / "inner")
    for d in (tmp_path / "outer" / "inner" / "x", tmp_path / "outer-wt" / "T1"):
        d.mkdir(parents=True)
    assert config.project_for(tmp_path / "outer" / "inner" / "x", [outer, inner]).name == "Inner"
    assert config.project_for(tmp_path / "outer-wt" / "T1", [outer, inner]).name == "Outer"
    assert config.project_for(tmp_path, [outer, inner]) is None


def test_paths_follow_env(tmp_path, monkeypatch):
    assert paths.db_path() == tmp_path / "ahub-home" / "ahub.db"
    assert paths.log_dir() == tmp_path / "ahub-home" / "state" / "logs"
    monkeypatch.delenv("AHUB_HOME")
    assert paths.db_path() == tmp_path / ".local/share/ahub/ahub.db"
    assert paths.log_dir() == tmp_path / ".local/state/ahub/logs"
