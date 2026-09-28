"""Фейковый /proc: opencode/pytest/flock, держатель замка, дети."""

from __future__ import annotations

import os

from hub.read.procs import agent_procs, lock_holder


def _proc(root, pid: int, argv: list[str], cwd: str, ppid: int = 1) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes("\x00".join(argv).encode() + b"\x00")
    try:
        (d / "cwd").symlink_to(cwd)
    except FileExistsError:
        pass
    (d / "stat").write_text(f"{pid} (proc) R {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n",
                            encoding="utf-8")


def _fake(tmp_path):
    root = tmp_path / "proc"
    wt = tmp_path / "wt-T01"
    wt.mkdir()
    _proc(root, 100, ["/usr/bin/opencode", "run", "--dir", str(wt)], str(wt))
    _proc(root, 101, ["flock", "/tmp/playerup_test_db.lock", "pytest", "-q"], str(wt), ppid=100)
    _proc(root, 102, ["/usr/bin/python", "-m", "pytest", "-q", "tests/"], str(wt), ppid=101)
    _proc(root, 200, ["/usr/bin/python", "-m", "http.server"], str(tmp_path))
    _proc(root, 300, ["sleep", "100"], str(tmp_path))
    return root, wt


def test_kinds_and_children(tmp_path):
    root, wt = _fake(tmp_path)
    got = {p.pid: p for p in agent_procs(root)}
    assert got[100].kind == "opencode" and got[100].cwd == str(wt)
    assert got[101].kind == "flock"
    assert got[102].kind == "pytest"
    assert 200 not in got and 300 not in got  # чужие процессы не берём
    assert 101 in got[100].children  # ребёнок opencode — flock/pytest
    assert got[102].args[0].endswith("python")


def test_lock_holder(tmp_path):
    root, _ = _fake(tmp_path)
    holder = lock_holder("/tmp/playerup_test_db.lock", root)
    assert holder is not None and holder.pid == 101
    assert lock_holder("/tmp/нет-такого.lock", root) is None


def test_gone_pids_skipped(tmp_path):
    root = tmp_path / "proc"
    (root / "999").mkdir(parents=True)  # без cmdline — пропустить, не упасть
    (root / "не-pid").mkdir(parents=True)
    assert agent_procs(root) == []
    assert agent_procs(tmp_path / "нет-каталога") == []
