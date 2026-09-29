"""H08: коллектор agy, окно 5 ч, привязка к задачам, исполнитель по Уровню."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from hub.read import agy as ag
from hub.read import snapshot as snap
from hub.store import Store

NOW = 1_789_000_000_000


def _mk_conv(path: Path, n_steps: int = 3, n_errors: int = 1):
    con = sqlite3.connect(str(path))
    con.executescript("""
    CREATE TABLE trajectory_meta (id TEXT PRIMARY KEY, data BLOB);
    CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, status TEXT,
      has_subtrajectory INTEGER, metadata BLOB, error_details BLOB);
    CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER);
    CREATE TABLE executor_metadata (idx INTEGER PRIMARY KEY, data BLOB);
    """)
    for i in range(n_steps):
        err = b"boom" if i < n_errors else None
        con.execute(
            "INSERT INTO steps(idx, step_type, status, has_subtrajectory, metadata, error_details)"
            " VALUES (?,?,?,?,?,?)",
            (i, "tool", "done" if err is None else "error", 0, b"{}", err))
    con.commit()
    con.close()


def test_steps_and_errors(tmp_path):
    conv = tmp_path / "conv"
    conv.mkdir()
    _mk_conv(conv / "abc.db", 3, 1)
    got = ag.conversations(conv, 0)
    assert len(got) == 1
    assert got[0].steps == 3 and got[0].errors == 1
    assert got[0].pulse_ms > 0 and got[0].started_ms > 0
    # Окно: 1 запуск, 3 шага.
    runs, steps = ag.window_usage(conv, int(time.time() * 1000), hours=5)
    assert (runs, steps) == (1, 3)


def test_unknown_schema_empty(tmp_path, caplog):
    conv = tmp_path / "conv2"
    conv.mkdir()
    db = conv / "чужой.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE совсем_другое (id INTEGER)")
    con.commit()
    con.close()
    with caplog.at_level(logging.WARNING):
        assert ag.conversations(conv, 0) == []
    assert caplog.text.strip() != ""


def test_empty_root_is_empty(tmp_path):
    assert ag.conversations("", 0) == []
    assert ag.conversations(None, 0) == []
    assert ag.window_usage("", NOW, 5) == (0, 0)


def test_since_filters_by_pulse(tmp_path):
    conv = tmp_path / "conv3"
    conv.mkdir()
    _mk_conv(conv / "a.db", 1, 0)
    future = int(time.time() * 1000) + 10_000_000
    assert ag.conversations(conv, future) == []


def test_snapshot_binds_agy_and_header(tmp_path):
    conv = tmp_path / "conv4"
    conv.mkdir()
    _mk_conv(conv / "c1.db", 3, 0)
    s = Store()
    wt = tmp_path / "wt"
    wt.mkdir()
    s.upsert_task(id="T01", stage="exec r1", round=1,
                  worktree=str(wt), updated_at=NOW - 60_000)
    s.link_session("c1", "agy", "T01", "executor", 1, "gemini")
    got = snap.build(s, NOW, opencode_db=None,
                     proc_root=tmp_path / "пустой", agy_root=conv)
    assert got.agy_runs == 1 and got.agy_steps == 3
    assert "Gemini:" in got.head_text() and "за 5 ч" in got.head_text()
    assert "agy n/a" not in got.head_text()
    t = {x.id: x for x in got.tasks}["T01"]
    gem = [x for x in t.sessions if x.model == "Gemini"]
    assert gem, t.sessions
    # Привязка именно к conversations/*.db, а не к фолбэку tool==agy:
    # в активности — шаги из БД.
    assert any("3 шагов" in x.last_activity for x in gem)
    assert t.cost_go == 0.0 and t.cost_usd == 0.0
    assert "Gemini:" in got.to_text()


def test_snapshot_agy_proc_without_link(tmp_path):
    import os

    s = Store()
    wt = tmp_path / "wt-p"
    wt.mkdir()
    s.upsert_task(id="T02", stage="exec r1", round=1,
                  worktree=str(wt), updated_at=NOW - 60_000)
    root = tmp_path / "proc"
    for pid, argv in (("10", ["agy", "-p", "x"]),):
        d = root / pid
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes("\x00".join(argv).encode() + b"\x00")
        (d / "cwd").symlink_to(str(wt))
        (d / "stat").write_text(
            f"{pid} (agy) R 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n",
            encoding="utf-8")
        # Детерминированный старт процесса: свежая задача — зелёная.
        ts = NOW / 1000
        os.utime(d / "stat", (ts, ts))
    got = snap.build(s, NOW, opencode_db=None, proc_root=root,
                     agy_root=tmp_path / "нет-conv")
    t = {x.id: x for x in got.tasks}["T02"]
    assert any(x.model == "Gemini" for x in t.sessions)
    assert t.pulse == "🟢"


def test_stale_agy_with_live_proc_not_green(tmp_path):
    """Живой, но молчащий agy: mtime старый → 🟡, а не вечный 🟢."""
    import os

    conv = tmp_path / "conv-stale"
    conv.mkdir()
    db = conv / "old.db"
    _mk_conv(db, 2, 0)
    old_s = (NOW - 60 * 60_000) / 1000
    os.utime(db, (old_s, old_s))
    s = Store()
    wt = tmp_path / "wt-stale"
    wt.mkdir()
    s.upsert_task(id="TS", stage="exec r1", round=1,
                  worktree=str(wt), updated_at=NOW - 60 * 60_000)
    s.link_session("old", "agy", "TS", "executor", 1, "gemini")
    root = tmp_path / "proc-stale"
    d = root / "11"
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"agy\x00-p\x00x\x00")
    (d / "cwd").symlink_to(str(wt))
    (d / "stat").write_text(
        "11 (agy) R 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n",
        encoding="utf-8")
    os.utime(d / "stat", (old_s, old_s))
    got = snap.build(s, NOW, opencode_db=None, proc_root=root, agy_root=conv)
    t = {x.id: x for x in got.tasks}["TS"]
    assert t.pulse == "🟡"


def test_tui_header_shows_gemini_window(tmp_path):
    from hub.read.snapshot import Snapshot, TaskSnap
    from hub.tui.app import HubApp

    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    app = HubApp(store=Store(), opencode_db=None, proc_root=str(proc))
    app._schedule_refresh = lambda: None
    empty = Snapshot(tasks=[], total_go=0.0, total_usd=0.0, now_ms=NOW,
                     agy_runs=0, agy_steps=0)
    # Gemini выключен (0 запусков) — в шапке не шумит.
    assert "Gemini" not in app._header_text(empty).plain
    full = Snapshot(tasks=[], total_go=0.0, total_usd=0.0, now_ms=NOW,
                    agy_runs=2, agy_steps=7)
    text = app._header_text(full).plain
    assert "Gemini: 2 запуска / 7 шагов за 5 ч" in text
    assert "agy n/a" not in text
    _ = TaskSnap  # контракт таблицы не менялся


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_start_easy_picks_gemini(tmp_path, capsys):
    from hub.cli import main

    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path / 'wt-dir'}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\", \"tests/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n"
        "[levels]\neasy = \"gemini\"\nmedium = \"musefree\"\nhard = \"muse\"\n",
        encoding="utf-8")
    card = tmp_path / "TE-easy.md"
    card.write_text(
        "# T\n**Цель.** ц\n**Прочитать.** docs/rules.md\n"
        "**Можно менять.** `sub/**`\n**Интерфейс.** `f()`\n"
        "**Приёмка.** `pytest -q sub/test_ok.py`\n**Нельзя.** сеть\n"
        "**Сеть.** нет\n**Исполнитель.** muse\n**Уровень.** easy\n**Коммит.** `feat: x`\n",
        encoding="utf-8")
    assert main(["start", str(card), "--project", str(root)]) == 0
    capsys.readouterr()
    assert Store().get_task("TE-easy")["executor"] == "gemini"


def test_start_explicit_executor_wins(tmp_path, capsys):
    from hub.cli import main

    root = tmp_path / "proj2"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path / 'wt2'}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\", \"tests/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n"
        "[levels]\neasy = \"gemini\"\nmedium = \"musefree\"\nhard = \"muse\"\n",
        encoding="utf-8")
    card = tmp_path / "TE-exp.md"
    card.write_text(
        "# T\n**Цель.** ц\n**Прочитать.** docs/rules.md\n"
        "**Можно менять.** `sub/**`\n**Интерфейс.** `f()`\n"
        "**Приёмка.** `pytest -q sub/test_ok.py`\n**Нельзя.** сеть\n"
        "**Сеть.** нет\n**Исполнитель.** muse\n**Уровень.** easy\n**Коммит.** `feat: x`\n",
        encoding="utf-8")
    assert main(["start", str(card), "--project", str(root),
                 "--executor", "muse"]) == 0
    capsys.readouterr()
    assert Store().get_task("TE-exp")["executor"] == "muse"


def test_agy_runner_cmd_flags(tmp_path, monkeypatch):
    from hub.pipeline.runners import AgyRunner

    seen: dict = {}

    import subprocess as _sp

    class _R:
        stdout = '{"conversation_id": "c1"}\n'
        stderr = ""
        returncode = 0

    def _fake(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _R()

    monkeypatch.setattr(_sp, "run", _fake)
    log = str(tmp_path / "agy-test.log")
    AgyRunner().start("привет", str(tmp_path), log=log)
    cmd = seen["cmd"]
    assert cmd[0].endswith("agy") or cmd[0] == "agy"
    assert "-p" in cmd and "--output-format" in cmd and "json" in cmd
    assert "--model" in cmd and "gemini-3.8-flash-high" in cmd
    assert "--dangerously-skip-permissions" in cmd


def _mk_start_repo(tmp_path, name, levels_toml):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path / (name + '-wt')}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\", \"tests/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n"
        + levels_toml,
        encoding="utf-8")
    return root


def _card_text(executor="muse", level="easy"):
    lvl = f"**Уровень.** {level}\n" if level else ""
    return (
        "# T\n**Цель.** ц\n**Прочитать.** docs/rules.md\n"
        "**Можно менять.** `sub/**`\n**Интерфейс.** `f()`\n"
        "**Приёмка.** `pytest -q sub/test_ok.py`\n**Нельзя.** сеть\n"
        f"**Сеть.** нет\n**Исполнитель.** {executor}\n{lvl}**Коммит.** `feat: x`\n"
    )


def test_start_custom_levels_not_builtin(tmp_path, capsys):
    """Кастомный [levels] побеждает builtin: easy → mimoflash, а не gemini."""
    from hub.cli import main

    root = _mk_start_repo(tmp_path, "proj-custom",
                          "[levels]\neasy = \"mimoflash\"\nmedium = \"musefree\"\nhard = \"muse\"\n")
    card = tmp_path / "TC-easy.md"
    card.write_text(_card_text(executor="muse", level="easy"), encoding="utf-8")
    assert main(["start", str(card), "--project", str(root)]) == 0
    capsys.readouterr()
    assert Store().get_task("TC-easy")["executor"] == "mimoflash"


def test_start_medium_hard_levels(tmp_path, capsys):
    from hub.cli import main

    root = _mk_start_repo(tmp_path, "proj-mh",
                          "[levels]\neasy = \"gemini\"\nmedium = \"musefree\"\nhard = \"muse\"\n")
    for lvl, want in (("medium", "musefree"), ("hard", "muse")):
        card = tmp_path / f"TMH-{lvl}.md"
        card.write_text(_card_text(executor="gemini", level=lvl), encoding="utf-8")
        assert main(["start", str(card), "--project", str(root)]) == 0
        capsys.readouterr()
        assert Store().get_task(f"TMH-{lvl}")["executor"] == want


def test_start_no_level_uses_executor_section(tmp_path, capsys):
    """Без Уровня — исполнитель из раздела Исполнитель, а не дефолт."""
    from hub.cli import main

    root = _mk_start_repo(tmp_path, "proj-no-lvl",
                          "[levels]\neasy = \"gemini\"\nmedium = \"musefree\"\nhard = \"muse\"\n")
    card = tmp_path / "TNL.md"
    card.write_text(_card_text(executor="musefree", level=""), encoding="utf-8")
    assert main(["start", str(card), "--project", str(root)]) == 0
    capsys.readouterr()
    assert Store().get_task("TNL")["executor"] == "musefree"


def test_executor_section_isolation():
    """Соседние секции не протекают: уровень и исполнитель режутся по заголовкам."""
    from hub.commands.start import _executor_for_card
    from hub.config import ProjectConfig

    proj = ProjectConfig(levels={"easy": "gemini", "medium": "musefree", "hard": "muse"})
    proj.defaults.executor = "muse"
    leak_level = "**Сеть.** playerok hard. **Уровень.** easy. **Исполнитель.** muse."
    assert _executor_for_card(leak_level, proj) == "gemini"
    leak_exec = "**Сеть.** не трогать gemini-консоль. **Исполнитель.** muse"
    assert _executor_for_card(leak_exec, proj) == "muse"
