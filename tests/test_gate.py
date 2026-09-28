"""Ворота, done.json, repair-промпт, hub gate — на фейковом git-репо."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hub.cli import main
from hub.gate.donefile import load_done
from hub.gate.gate import check_gate
from hub.gate.repair import REPAIR_PROMPT, repair_prompt
from hub.store import Store

PASS = [sys.executable, "-c", "pass"]
FAIL = [sys.executable, "-c", "import sys; print('БАХ-хвост'); sys.exit(1)"]
PASS_STR = sys.executable + " -c pass"


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _commit(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", rel)
    return _git(repo, "rev-parse", "HEAD")


def _write_done(repo, commit, files, cmd=PASS_STR, ok=True, tail="ok", notes=""):
    d = repo / ".agent"
    d.mkdir(exist_ok=True)
    payload = {"commit": commit, "files": files,
               "tests": {"cmd": cmd, "ok": ok, "tail": tail}, "notes": notes}
    (d / "done.json").write_text(json.dumps(payload), encoding="utf-8")


def _card(tmp_path, globs, name="CARD.md"):
    card = tmp_path / name
    card.write_text("# Тест\n\n**Можно менять.** "
                    + ", ".join(f"`{g}`" for g in globs)
                    + "\n\n**Нельзя.** ничего\n", encoding="utf-8")
    return card


# --- check_gate ---

def test_empty_diff(tmp_path):
    repo, base = _repo(tmp_path)
    res = check_gate(repo, base, "HEAD", ["**"], PASS)
    assert not res.ok
    assert res.errors == ["empty-diff"]  # приёмка не запускалась — нет tests-fail


def test_forbidden(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(repo, "other/x.txt", "x\n")
    res = check_gate(repo, base, "HEAD", ["hub/**"], PASS)
    assert not res.ok
    assert any(e.startswith("forbidden:") for e in res.errors)
    assert any("other/x.txt" in e for e in res.errors)
    assert "other/x.txt" in res.diff_stat or "x.txt" in res.diff_stat


def test_tests_fail_tail(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    res = check_gate(repo, base, "HEAD", ["**"], FAIL)
    assert not res.ok
    assert any(e.startswith("tests-fail:") for e in res.errors)
    assert "БАХ-хвост" in res.tests_tail
    assert any("БАХ-хвост" in e for e in res.errors)


def test_tests_fail_tail_capped(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    big = [sys.executable, "-c", "import sys; print('y'*5000); sys.exit(1)"]
    res = check_gate(repo, base, "HEAD", ["**"], big)
    assert not res.ok
    err = next(e for e in res.errors if e.startswith("tests-fail:"))
    assert len(err) - len("tests-fail: ") <= 2000
    assert len(res.tests_tail) <= 2000


def test_locked_skips_command(tmp_path):
    flock_bin = shutil.which("flock")
    if flock_bin is None:
        pytest.skip("нет flock")
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    lock = tmp_path / "t.lock"
    lock.touch()
    marker = tmp_path / "marker"
    cmd = [sys.executable, "-c", "open(%r,'w').write('x')" % str(marker)]
    holder = subprocess.Popen([flock_bin, str(lock), "sleep", "15"])
    try:
        time.sleep(1.0)
        res = check_gate(repo, base, "HEAD", ["**"], cmd,
                         lock_path=str(lock), timeout_s=30)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    assert not res.ok
    assert any(e.startswith("locked:") for e in res.errors)
    assert "pid" in next(e for e in res.errors if e.startswith("locked:"))
    assert not marker.exists()  # команда не запускалась


# --- load_done ---

def test_load_done_missing(tmp_path):
    repo, _ = _repo(tmp_path)
    with pytest.raises(FileNotFoundError, match="done.json:"):
        load_done(repo)


def test_load_done_no_tests_ok(tmp_path):
    repo, base = _repo(tmp_path)
    d = repo / ".agent"
    d.mkdir(exist_ok=True)
    (d / "done.json").write_text(json.dumps({
        "commit": base, "files": [], "tests": {"cmd": "x", "tail": "t"}, "notes": "",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match=r"done\.json:.*tests\.ok"):
        load_done(repo)


def test_load_done_bad_json_and_fields(tmp_path):
    repo, base = _repo(tmp_path)
    d = repo / ".agent"
    d.mkdir(exist_ok=True)
    (d / "done.json").write_text("{битый", encoding="utf-8")
    with pytest.raises(ValueError, match="done.json:"):
        load_done(repo)
    (d / "done.json").write_text(json.dumps({
        "commit": base, "files": "не-список",
        "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": "",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match=r"done\.json:.*files"):
        load_done(repo)
    (d / "done.json").write_text(json.dumps({
        "commit": base, "files": [], "tests": {"cmd": "x", "ok": 1, "tail": "t"},
        "notes": "",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match=r"done\.json:.*tests\.ok"):
        load_done(repo)


def test_load_done_ok(tmp_path):
    repo, base = _repo(tmp_path)
    _write_done(repo, base, ["a.txt"], cmd=PASS_STR, ok=True, tail="хвост", notes="н")
    got = load_done(repo)
    assert got.commit == base and got.files == ["a.txt"]
    assert got.cmd == PASS_STR and got.ok is True
    assert got.tail == "хвост" and got.notes == "н"


# --- repair ---

def test_repair_prompt():
    assert "git status" in REPAIR_PROMPT
    assert "-A" in REPAIR_PROMPT
    assert "done.json" in REPAIR_PROMPT
    text = repair_prompt("нет коммита")
    assert "git status" in text and "-A" in text and "done.json" in text
    assert "Причина: нет коммита" in text


# --- hub gate ---

def _task(tmp_path, tid, repo, base, globs):
    card = _card(tmp_path, globs, name=f"{tid}.md")
    Store().upsert_task(id=tid, worktree=str(repo), base_sha=base,
                        branch=f"agent/{tid}", card_path=str(card))
    return card


def test_gate_mismatch(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    _task(tmp_path, "T01", repo, base, ["a.txt"])
    _write_done(repo, "0" * 40, ["a.txt"])
    assert main(["gate", "T01"]) == 1
    out = capsys.readouterr().out
    assert "mismatch:" in out


def test_gate_unknown_file(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    head = _commit(repo, "a.txt", "1\n2\n")
    _task(tmp_path, "T02", repo, base, ["a.txt", "нет-в-диффе.txt"])
    _write_done(repo, head, ["a.txt", "нет-в-диффе.txt"])
    assert main(["gate", "T02", "--round", "1"]) == 1
    out = capsys.readouterr().out
    assert "unknown-file:" in out


def test_gate_ok(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    head = _commit(repo, "a.txt", "1\n2\n")
    _task(tmp_path, "T03", repo, base, ["a.txt"])
    _write_done(repo, head, ["a.txt"])
    assert main(["gate", "T03"]) == 0
    assert "OK T03" in capsys.readouterr().out
