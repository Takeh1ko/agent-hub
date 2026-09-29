"""H06: start/merge/stop/clean/queue — временный git, идемпотентность, откаты."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hub.cli import main
from hub.config import ProjectConfig
from hub.pipeline.common import card_network_is_playerok
from hub.pipeline.merge import merge_task
from hub.store import Store


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _proj_root(tmp_path, name="proj"):
    root = tmp_path / name
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "spec.md").write_text("спека\n", encoding="utf-8")
    (root / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents" / "rules.md").write_text("# правила\n", encoding="utf-8")
    (root / "a.txt").write_text("1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    wt_dir = tmp_path / "wt-dir"
    wt_dir.mkdir(exist_ok=True)
    (root / ".hub.toml").write_text(
        "schema_version = 1\n"
        'name = "T"\n'
        f'root = "{root}"\n'
        f'worktrees = "{wt_dir}"\n'
        'rules = "docs/agents/rules.md"\n'
        f'python = "{sys.executable}"\n'
        'test_lock = ""\n'
        'work_branch = "main"\n'
        'push = ""\n'
        'allowed_paths = ["sub/**", "docs/**", "tests/**", "a.txt"]\n'
        '[hooks]\n[defaults]\nexecutor = "muse"\nreviewers = ["muse"]\nbudget_go = 0.5\n',
        encoding="utf-8")
    return root, wt_dir


CARD_TMPL = """# H
**Цель.** ц
**Прочитать.** docs/spec.md
**Можно менять.** `sub/**`, `a.txt`
**Интерфейс.** `f()`
**Приёмка.** `pytest -q sub/test_ok.py`
**Нельзя.** сеть
**Сеть.** {net}
**Исполнитель.** muse
**Уровень.** medium
**Коммит.** `feat: x`
"""


def _card(tmp_path, name, net="нет"):
    p = tmp_path / name
    p.write_text(CARD_TMPL.format(net=net), encoding="utf-8")
    return p


def test_start_idempotent(tmp_path, capsys):
    root, _ = _proj_root(tmp_path)
    card = _card(tmp_path, "T10-cycle.md")
    assert main(["start", str(card), "--project", str(root)]) == 0
    first = capsys.readouterr().out
    assert "OK T10-cycle" in first
    assert main(["start", str(card), "--project", str(root)]) == 0
    second = capsys.readouterr().out
    assert "уже есть" in second and "T10-cycle" in second
    tasks = [t for t in Store().list_tasks(active_only=False) if t["id"] == "T10-cycle"]
    assert len(tasks) == 1 and tasks[0]["stage"] == "queued"


def test_start_lint_refuses(tmp_path, capsys):
    root, _ = _proj_root(tmp_path)
    bad = tmp_path / "bad.md"
    bad.write_text("# битая\n\nнет разделов\n", encoding="utf-8")
    assert main(["start", str(bad), "--project", str(root)]) == 1
    assert Store().get_task("bad") is None


def _ready_task(root, wt_dir, tid, rel="sub/a.txt", text="2\n"):
    base = _git(root, "rev-parse", "HEAD")
    wt = wt_dir / tid
    _git(root, "worktree", "add", str(wt), "-b", f"agent/{tid}", base)
    p = wt / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(wt, "add", rel)
    _git(wt, "commit", "-m", rel)
    head = _git(wt, "rev-parse", "HEAD")
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "done.json").write_text(json.dumps({
        "commit": head, "files": [rel],
        "tests": {"cmd": sys.executable + " -m pytest -q", "ok": True, "tail": "ok"},
        "notes": ""}), encoding="utf-8")
    card = root / f"{tid}.md"
    card.write_text(CARD_TMPL.format(net="нет"), encoding="utf-8")
    Store().upsert_task(id=tid, project="T", card_path=str(card), card_hash="h",
                        level="medium", branch=f"agent/{tid}", worktree=str(wt),
                        base_sha=base, stage="ready", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="", budget_go=0.5)
    return base, head


def _load_project(root):
    from hub.config import load_project

    return load_project(str(root))


def test_merge_ok_and_clean(tmp_path):
    root, wt_dir = _proj_root(tmp_path)
    base, _ = _ready_task(root, wt_dir, "T20")
    proj = _load_project(root)
    ok, msg = merge_task(Store(), proj, "T20")
    assert ok, msg
    assert Store().get_task("T20")["stage"] == "merged"
    assert (root / "sub" / "a.txt").read_text(encoding="utf-8") == "2\n"
    # worktree и ветка удалены.
    assert not (wt_dir / "T20").exists()
    branches = subprocess.run(["git", "branch", "--list", "agent/T20"], cwd=str(root),
                              capture_output=True, text=True, timeout=60).stdout
    assert "agent/T20" not in branches


def test_merge_conflict_abort_keeps_stage(tmp_path):
    root, wt_dir = _proj_root(tmp_path)
    base, _ = _ready_task(root, wt_dir, "T21", rel="a.txt", text="ветка\n")
    # Увести main вперёд с конфликтом.
    (root / "a.txt").write_text("майн\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "main вперёд")
    proj = _load_project(root)
    ok, msg = merge_task(Store(), proj, "T21")
    assert not ok and "conflict" in msg
    assert Store().get_task("T21")["stage"] == "ready"
    # main остался на своём коммите (слияния нет).
    assert (root / "a.txt").read_text(encoding="utf-8") == "майн\n"


def test_merge_red_tests_rollback(tmp_path):
    root, wt_dir = _proj_root(tmp_path)
    base, _ = _ready_task(root, wt_dir, "T22")
    # Красный тест только на main после ответвления (в wt его нет → pre-зелёный).
    (root / "sub" / "test_red.py").write_text(
        "def test_red():\n    assert False\n", encoding="utf-8")
    _git(root, "add", "sub/test_red.py")
    _git(root, "commit", "-m", "красный на main")
    pre = _git(root, "rev-parse", "HEAD")
    proj = _load_project(root)
    ok, msg = merge_task(Store(), proj, "T22")
    assert not ok
    assert "tests-fail" in msg
    assert Store().get_task("T22")["stage"] == "ready"
    assert _git(root, "rev-parse", "HEAD") == pre


def test_merge_card_resolved_from_worktree(tmp_path):
    """LOW: относительный card_path, которого нет в корне, не блокирует merge.

    merge использует тот же _resolve_card, что cycle/review (root → worktree →
    как есть): иначе globs=[], allowed=[] и ворота помечают весь дифф
    forbidden уже готовой к слиянию задаче.
    """
    root, wt_dir = _proj_root(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    wt = wt_dir / "T60"
    _git(root, "worktree", "add", str(wt), "-b", "agent/T60", base)
    # Карточка лежит только в worktree: в корне такого файла нет.
    card = wt / "docs" / "T60.md"
    card.parent.mkdir(exist_ok=True)
    card.write_text(
        CARD_TMPL.replace("`sub/**`, `a.txt`", "`sub/**`, `docs/**`")
                 .format(net="нет"), encoding="utf-8")
    (wt / "sub" / "a.txt").write_text("2\n", encoding="utf-8")
    _git(wt, "add", "sub/a.txt", "docs/T60.md")
    _git(wt, "commit", "-m", "работа")
    head = _git(wt, "rev-parse", "HEAD")
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "done.json").write_text(json.dumps({
        "commit": head, "files": ["sub/a.txt"],
        "tests": {"cmd": sys.executable + " -m pytest -q", "ok": True, "tail": "ok"},
        "notes": ""}), encoding="utf-8")
    Store().upsert_task(id="T60", project="T", card_path="docs/T60.md",
                        card_hash="h", level="medium", branch="agent/T60",
                        worktree=str(wt), base_sha=base, stage="ready", round=1,
                        executor="muse", reviewers_json='["muse"]',
                        stage_reason="", budget_go=0.5)
    assert not (root / "docs" / "T60.md").exists()
    ok, msg = merge_task(Store(), _load_project(root), "T60")
    assert ok, msg
    assert Store().get_task("T60")["stage"] == "merged"
    assert not (wt_dir / "T60").exists()


def test_merge_only_from_ready(tmp_path, capsys):
    root, wt_dir = _proj_root(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    Store().upsert_task(id="T23", project="T", card_path="x", branch="agent/T23",
                        worktree=str(root), base_sha=base, stage="queued")
    assert main(["merge", "T23", "--project", str(root)]) == 1
    assert "not-ready" in capsys.readouterr().out


def test_stop_and_clean(tmp_path, capsys):
    root, wt_dir = _proj_root(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    Store().upsert_task(id="T30", project="T", card_path="x", branch="agent/T30",
                        worktree=str(root), base_sha=base, stage="exec r1")
    assert main(["stop", "T30"]) == 0
    assert Store().get_task("T30")["stage"] == "stopped"
    assert (root / ".agent" / "stop_requested").exists()
    # Осиротевший worktree: создать без задачи.
    orph = wt_dir / "orph"
    _git(root, "worktree", "add", str(orph), "-b", "agent/orph", base)
    assert main(["clean", "--project", str(root)]) == 0
    out = capsys.readouterr().out
    assert "orph" in out
    assert main(["clean", "--project", str(root), "--yes"]) == 0
    assert not orph.exists()
    # Ветка из worktree (префикс '+' в branch --list) тоже удалена.
    branches = subprocess.run(["git", "branch", "--list", "agent/orph"], cwd=str(root),
                              capture_output=True, text=True, timeout=60).stdout
    assert "agent/orph" not in branches


def test_queue_paused_and_after(tmp_path, capsys):
    import sqlite3 as _sq

    root, _ = _proj_root(tmp_path)
    c1 = _card(tmp_path, "T40-a.md")
    assert main(["start", str(c1), "--project", str(root)]) == 0
    capsys.readouterr()
    con = _sq.connect(str(Store().path))
    try:
        con.execute("INSERT INTO meta(key,value) VALUES('queue_paused','1')"
                    " ON CONFLICT(key) DO UPDATE SET value='1'")
        con.commit()
    finally:
        con.close()
    assert main(["queue", "run", "--project", str(root), "--once"]) == 0
    assert "пауза" in capsys.readouterr().out


def test_playerok_not_parallel_flag():
    assert card_network_is_playerok(CARD_TMPL.format(net="playerok, локально"))
    assert not card_network_is_playerok(CARD_TMPL.format(net="нет"))


def test_continue_moves_base_and_agent(tmp_path, capsys):
    root, wt_dir = _proj_root(tmp_path)
    base, _ = _ready_task(root, wt_dir, "T50")
    (wt_dir / "T50" / ".agent" / "review_r1.json").write_text("{}", encoding="utf-8")
    assert main(["continue", "T50", "--project", str(root)]) == 0
    assert "OK T50" in capsys.readouterr().out
    task = Store().get_task("T50")
    assert task["stage"] == "queued"
    leftovers = list((wt_dir / "T50").glob(".agent.prev_*"))
    assert leftovers and not (wt_dir / "T50" / ".agent" / "review_r1.json").exists()
