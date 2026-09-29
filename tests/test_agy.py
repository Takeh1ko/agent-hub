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
    assert any(x.model == "Gemini" for x in t.sessions)
    assert t.cost_go == 0.0 and t.cost_usd == 0.0
    assert "Gemini:" in got.to_text()


def test_snapshot_agy_proc_without_link(tmp_path):
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
    got = snap.build(s, NOW, opencode_db=None, proc_root=root,
                     agy_root=tmp_path / "нет-conv")
    t = {x.id: x for x in got.tasks}["T02"]
    assert any(x.model == "Gemini" for x in t.sessions)
    assert t.pulse == "🟢"


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


def test_agy_runner_cmd_flags():
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

    orig = _sp.run
    _sp.run = _fake  # type: ignore
    try:
        AgyRunner().start("привет", "/tmp", log="/tmp/agy-test.log")
    finally:
        _sp.run = orig  # type: ignore
    cmd = seen["cmd"]
    assert cmd[0].endswith("agy") or cmd[0] == "agy"
    assert "-p" in cmd and "--output-format" in cmd and "json" in cmd
    assert "--model" in cmd and "gemini-3.8-flash-high" in cmd
    assert "--dangerously-skip-permissions" in cmd
