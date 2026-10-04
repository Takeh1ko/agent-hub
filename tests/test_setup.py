from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ahub import cli, config, doctor, paths
from tests.conftest import write
from tests.enginekit import make_repo


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
    assert cli.main(["setup", str(root), "--claude"]) == 0  # a repeat — no duplicates
    out = capsys.readouterr().out
    assert "уже v2" in out and "блок уже есть" in out
    assert (root / "CLAUDE.md").read_text().count("ahub:begin") == 1
    assert paths.global_config_path().read_text().count(str(root)) == 1


def test_setup_claude_permission(tmp_path, capsys):
    """T51: --claude puts Bash(ahub:*) into .claude/settings.json — the file, the merge, no duplicates."""
    root = tmp_path / "perm"
    make_repo(root)
    assert cli.main(["setup", str(root), "--claude"]) == 0
    out = capsys.readouterr().out
    settings = root / ".claude" / "settings.json"  # the file and the dir are created
    assert settings.is_file()
    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"] == ["Bash(ahub:*)"]
    assert '  "permissions"' in settings.read_text(encoding="utf-8")  # 2-space indent
    assert "Bash(ahub:*)" in out and "claude mcp add ahub -- ahub mcp" in out  # + the MCP hint, not run
    assert doctor.bash_allowed(root) is True
    # a repeat: the rule is already there — one copy, everything else as is
    assert cli.main(["setup", str(root), "--claude"]) == 0
    assert "уже разрешён" in capsys.readouterr().out
    assert settings.read_text(encoding="utf-8").count("Bash(ahub:*)") == 1
    # an existing file keeps its content: other rules, other keys, its order
    write(settings, json.dumps({"model": "opus", "permissions": {"allow": ["Bash(git:*)"],
                                                                 "deny": ["Bash(rm:*)"]}}, indent=2) + "\n")
    assert cli.main(["setup", str(root), "--claude"]) == 0
    capsys.readouterr()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"model": "opus",
                    "permissions": {"allow": ["Bash(git:*)", "Bash(ahub:*)"], "deny": ["Bash(rm:*)"]}}
    # a broken file is left as is and named in the output — setup does not refuse over it
    write(settings, "{ not json\n")
    assert cli.main(["setup", str(root), "--claude"]) == 0
    out = capsys.readouterr().out
    assert "оставлен как есть" in out
    assert settings.read_text(encoding="utf-8") == "{ not json\n"


def test_setup_converts_v1(tmp_path, capsys):
    root = tmp_path / "old"
    make_repo(root)
    (root / ".hub.toml").write_text("""schema_version = 1
name = "webapp"
worktrees = "/tmp/pu-wt"
python = "/usr/bin/python3"
test_lock = "/tmp/webapp_test_db.lock"
work_branch = "main"
push = "origin main:claude/x"
allowed_paths = ["core/**", "tests/**"]
[hooks]
task_setup = "python -m tools.task_db create"
task_cleanup = "python -m tools.task_db drop"
[defaults]
budget_go = 1.5
""", encoding="utf-8")
    assert cli.main(["setup", str(root), "--deny", "deepseek"]) == 0
    assert "переведён v1 → v2" in capsys.readouterr().out
    assert (root / ".agent-hub" / "hub.toml.v1").exists()
    data = tomllib.loads((root / ".hub.toml").read_text())
    assert data["schema_version"] == 2 and data["test_resource"] == "test_lock"
    cfg = config.load_project(root)
    assert cfg.resources["test_lock"].lock == "/tmp/webapp_test_db.lock" and cfg.push == "origin main:claude/x"
    assert cfg.hooks.task_setup.startswith("python -m tools.task_db") and cfg.models_deny == ("deepseek",)
    assert cfg.work_branch == "main" and cfg.budget_go == 1.5


def test_settings_json_is_never_truncated_in_place(tmp_path, monkeypatch):
    """A write that dies on the way to the user's settings leaves the file as it was (temp + os.replace)."""
    import os

    import pytest

    from ahub.commands import setup as setupecmd

    root = tmp_path / "perm2"
    settings = write(root / ".claude" / "settings.json", json.dumps({"model": "opus"}, indent=2) + "\n")

    def _boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        setupecmd.allow_bash(root)
    assert settings.read_text(encoding="utf-8") == json.dumps({"model": "opus"}, indent=2) + "\n"
    assert sorted(p.name for p in settings.parent.iterdir()) == ["settings.json"]  # no temp left behind
    monkeypatch.undo()
    assert "Bash(ahub:*)" in setupecmd.allow_bash(root)
    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"] == ["Bash(ahub:*)"]
