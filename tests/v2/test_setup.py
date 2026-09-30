from __future__ import annotations

import tomllib
from pathlib import Path

from ahub import cli, config, paths
from tests.v2.enginekit import make_repo


def test_setup_new_project(tmp_path, capsys):
    root = tmp_path / "shop"
    make_repo(root)
    assert cli.main(["setup", str(root), "--deny", "deepseek", "--claude"]) == 0
    out = capsys.readouterr().out
    assert "создан шаблон v2" in out and "навык Claude" in out and "CLAUDE.md: блок добавлен" in out
    cfg = config.load_project(root)
    assert cfg.name == "shop" and cfg.work_branch == "main" and cfg.models_deny == ("deepseek",)
    assert str(root) in paths.global_config_path().read_text()
    assert (Path.home() / ".claude/skills/ahub/SKILL.md").read_text().startswith("---\nname: ahub")
    assert cli.main(["setup", str(root), "--claude"]) == 0  # повтор — без дублей
    out = capsys.readouterr().out
    assert "уже v2" in out and "блок уже есть" in out
    assert (root / "CLAUDE.md").read_text().count("ahub:begin") == 1
    assert paths.global_config_path().read_text().count(str(root)) == 1


def test_setup_converts_v1(tmp_path, capsys):
    root = tmp_path / "old"
    make_repo(root)
    (root / ".hub.toml").write_text("""schema_version = 1
name = "PlayerUP"
worktrees = "/tmp/pu-wt"
python = "/usr/bin/python3"
test_lock = "/tmp/playerup_test_db.lock"
work_branch = "market"
push = "origin market:claude/x"
allowed_paths = ["core/**", "tests/**"]
[hooks]
task_setup = "python -m tools.task_db create"
task_cleanup = "python -m tools.task_db drop"
[defaults]
budget_go = 1.5
""", encoding="utf-8")
    assert cli.main(["setup", str(root), "--deny", "deepseek"]) == 0
    assert "переведён v1 → v2" in capsys.readouterr().out
    assert (root / ".hub.toml.v1").exists()
    data = tomllib.loads((root / ".hub.toml").read_text())
    assert data["schema_version"] == 2 and data["test_resource"] == "test_lock"
    cfg = config.load_project(root)
    assert cfg.resources["test_lock"].lock == "/tmp/playerup_test_db.lock" and cfg.push == "origin market:claude/x"
    assert cfg.hooks.task_setup.startswith("python -m tools.task_db") and cfg.models_deny == ("deepseek",)
    assert cfg.work_branch == "market" and cfg.budget_go == 1.5


def test_import_v1(tmp_path):
    import sqlite3

    from ahub.commands.import_v1 import import_v1
    from tests.v2.enginekit import make_project
    project = make_project(tmp_path)
    db = tmp_path / "hub.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task(id TEXT, project TEXT, stage TEXT, card_path TEXT, created_at INT, merged_sha TEXT,"
                " worktree TEXT, stage_reason TEXT)")
    con.execute("INSERT INTO task VALUES('T33-x','P','merged','docs/t.md',1790768812846,'89eabfc592','', '')")
    con.execute("INSERT INTO task VALUES('H01','','failed','docs/h.md',1790768812846,'',?, '')",
                (str(tmp_path / "wt" / "H01"),))
    con.execute("INSERT INTO task VALUES('Z9','Other','merged','',1,'','', '')")
    con.commit()
    con.close()
    assert import_v1(project, db) == 2
    text = (Path(project.root) / ".agent-hub" / "v1-tasks.md").read_text()
    assert "T33-x" in text and "слита" in text and "H01" in text and "Z9" not in text
