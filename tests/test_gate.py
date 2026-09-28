"""Ворота, done.json, repair-промпт, hub gate — на фейковом git-репо."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
import types
from importlib import import_module
from pathlib import Path

import pytest

from hub.cli import main
from hub.commands.gate import _cmd_covers
from hub.gate.donefile import load_done
from hub.gate.gate import check_gate
from hub.gate.repair import REPAIR_PROMPT, repair_prompt
from hub.store import Store

PASS = [sys.executable, "-c", "pass"]
FAIL = [sys.executable, "-c", "import sys; print('БАХ-хвост'); sys.exit(1)"]
# Заявление исполнителя: голый pytest (покрывает все ноды карточки).
CANON_STR = sys.executable + " -m pytest -q"


@pytest.fixture(autouse=True)
def _stub_lint(monkeypatch):
    """hub.gate.lint (H02): настоящий модуль, если слит, иначе стаб с тем же контрактом.

    Реализация стаба — построчный поиск заголовка (как H02 _section_bounds),
    не подстрока по тексту (см. real-card.md: «Можно менять» в Цели).
    """
    monkeypatch.delitem(sys.modules, "hub.gate.lint", raising=False)
    try:
        return import_module("hub.gate.lint")
    except ImportError:
        pass
    mod = _make_stub_lint()
    monkeypatch.setitem(sys.modules, "hub.gate.lint", mod)
    return mod


def _make_stub_lint():
    mod = types.ModuleType("hub.gate.lint")

    def _is_header(line, name):
        s = line.strip()
        m = re.match(r"^(#{1,6}\s+|\*\*)(.+)$", s)
        return bool(m) and m.group(2).lstrip().startswith(name)

    def _section_text(lines, name):
        start = next((i for i, line in enumerate(lines) if _is_header(line, name)), None)
        if start is None:
            return ""
        end = len(lines)
        for j in range(start + 1, len(lines)):
            t = lines[j].strip()
            if re.match(r"^(#{1,6}\s+|\*\*)", t):
                end = j
                break
        return "\n".join(lines[start:end])

    def _can_change_globs(section):
        out = []
        for m in re.finditer(r"`([^`]+)`", section):
            c = m.group(1).strip()
            if not c or any(ch.isspace() for ch in c):
                continue
            if "/" not in c and c not in (".hub.toml", "pyproject.toml"):
                continue
            out.append(c)
        return out

    def _pytest_nodes(section):
        nodes = []
        for m in re.finditer(r"`([^`]*pytest[^`]*)`", section):
            frag = m.group(1)
            tail = frag[frag.find("pytest") + len("pytest"):]
            try:
                toks = shlex.split(tail)
            except ValueError:
                toks = tail.split()
            for t in toks:
                t = t.strip()
                if not t or t.startswith("-") or t == "pytest":
                    continue
                nodes.append(t)
        return nodes

    mod._section_text = _section_text
    mod._can_change_globs = _can_change_globs
    mod._pytest_nodes = _pytest_nodes
    return mod


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


def _with_test(repo, rel="sub/test_ok.py"):
    """Каноническая приёмка (pytest -q) в worktree: один зелёный тест, закоммичен
    (ворота требуют чистое дерево — незакоммиченное вне .agent/ даёт dirty)."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def test_ok():\n    pass\n", encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", rel)


def _write_done(repo, commit, files, cmd=CANON_STR, ok=True, tail="ok", notes=""):
    d = repo / ".agent"
    d.mkdir(exist_ok=True)
    payload = {"commit": commit, "files": files,
               "tests": {"cmd": cmd, "ok": ok, "tail": tail}, "notes": notes}
    (d / "done.json").write_text(json.dumps(payload), encoding="utf-8")


def _card(tmp_path, globs, name="CARD.md"):
    """Карточка с упоминанием «Можно менять» в Цели — ловит поиск подстрокой."""
    card = tmp_path / name
    card.write_text(
        "# Тест\n\n**Цель.** Сделать X диффом внутри «Можно менять» без путей.\n\n"
        "**Можно менять.** " + ", ".join(f"`{g}`" for g in globs) + "\n\n"
        "**Приёмка.** `pytest -q`\n\n**Нельзя.** ничего\n", encoding="utf-8")
    return card


# --- check_gate ---

def test_empty_diff(tmp_path):
    repo, base = _repo(tmp_path)
    res = check_gate(repo, base, "HEAD", ["**"], PASS)
    assert not res.ok
    assert res.errors == ["empty-diff"]  # приёмка не запускалась — нет tests-fail


def test_allow_empty_commit_is_empty_diff(tmp_path):
    repo, base = _repo(tmp_path)
    _git(repo, "commit", "--allow-empty", "-m", "пусто")
    res = check_gate(repo, base, "HEAD", ["**"], PASS)
    assert not res.ok
    assert res.errors == ["empty-diff"]


def test_empty_test_cmd_fails(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    res = check_gate(repo, base, "HEAD", ["**"], [])
    assert not res.ok
    assert any(e.startswith("tests-fail:") for e in res.errors)


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
    from hub.read.procs import lock_holder

    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    lock = tmp_path / "t.lock"
    lock.touch()
    ready = tmp_path / "ready"
    marker = tmp_path / "marker"
    script = ("import fcntl, sys, time; fd = open(sys.argv[1], 'w'); "
              "fcntl.flock(fd, fcntl.LOCK_EX); "
              "open(sys.argv[2], 'w').write('ready'); time.sleep(20)")
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(lock), str(ready)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Ждём готовности, а не время: держатель виден в /proc.
        deadline = time.time() + 10
        while True:
            if ready.exists() and lock_holder(str(lock)) is not None:
                break
            if holder.poll() is not None:
                pytest.fail("держатель замка упал")
            if time.time() > deadline:
                pytest.fail("держатель не взял замок")
            time.sleep(0.05)
        cmd = [sys.executable, "-c", "open(%r,'w').write('x')" % str(marker)]
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
    _write_done(repo, base, ["a.txt"], cmd=CANON_STR, ok=True, tail="хвост", notes="н")
    got = load_done(repo)
    assert got.commit == base and got.files == ["a.txt"]
    assert got.cmd == CANON_STR and got.ok is True
    assert got.tail == "хвост" and got.notes == "н"


def test_load_done_strips(tmp_path):
    repo, base = _repo(tmp_path)
    _write_done(repo, f"  {base} ", ["  a.txt "])
    got = load_done(repo)
    assert got.commit == base and got.files == ["a.txt"]
    _write_done(repo, base, ["   "])
    with pytest.raises(ValueError, match=r"done\.json:.*files"):
        load_done(repo)


# --- repair ---

def test_repair_prompt():
    assert "git status --short" in REPAIR_PROMPT
    assert "никогда -A" in REPAIR_PROMPT
    assert ".agent/done.json" in REPAIR_PROMPT
    text = repair_prompt("нет коммита")
    assert "git status --short" in text
    assert "никогда -A" in text
    assert ".agent/done.json" in text
    assert "Причина: нет коммита" in text


# --- hub gate ---

def _task(tmp_path, tid, repo, base, globs):
    card = _card(tmp_path, globs, name=f"{tid}.md")
    Store().upsert_task(id=tid, worktree=str(repo), base_sha=base,
                        branch=f"agent/{tid}", card_path=str(card))
    return card


def test_gate_no_lint(tmp_path, capsys, monkeypatch):
    import hub.commands.gate as gate_cmd

    def _boom():
        raise ImportError("нет H02")

    monkeypatch.setattr(gate_cmd, "_load_lint", _boom)
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _task(tmp_path, "T00", repo, base, ["sub/**"])
    _write_done(repo, _git(repo, "rev-parse", "HEAD"), ["sub/a.txt"])
    assert main(["gate", "T00"]) == 1
    assert "no-lint:" in capsys.readouterr().out


def test_gate_bad_config_loud(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".hub.toml").write_text("{битый toml", encoding="utf-8")
    repo, base = _repo(tmp_path, name="wtrepo")
    _task(tmp_path, "T06", repo, base, ["sub/**"])
    assert main(["gate", "T06", "--project", str(proj)]) == 1
    assert "config:" in capsys.readouterr().out


def test_gate_no_diff_no_flood(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _task(tmp_path, "T07", repo, base, ["sub/**"])
    Store().upsert_task(id="T07", base_sha="0" * 40)
    _write_done(repo, head, ["sub/a.txt", "sub/test_ok.py"])
    assert main(["gate", "T07"]) == 1
    out = capsys.readouterr().out
    assert "no-diff:" in out
    assert "unknown-file" not in out


def test_lock_open_error(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    res = check_gate(repo, base, "HEAD", ["**"], PASS, lock_path=str(tmp_path))
    assert not res.ok
    assert any(e.startswith("lock-error:") for e in res.errors)


def test_gate_mismatch(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    _task(tmp_path, "T01", repo, base, ["sub/**"])
    _write_done(repo, "0" * 40, ["sub/a.txt", "sub/test_ok.py"])
    assert main(["gate", "T01"]) == 1
    out = capsys.readouterr().out
    assert "mismatch:" in out


def test_gate_unknown_file(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _task(tmp_path, "T02", repo, base, ["sub/a.txt", "sub/test_ok.py", "нет-в-диффе.txt"])
    _write_done(repo, head, ["sub/a.txt", "sub/test_ok.py", "нет-в-диффе.txt"])
    assert main(["gate", "T02", "--round", "1"]) == 1
    out = capsys.readouterr().out
    assert "unknown-file:" in out


def test_gate_ok_false(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _task(tmp_path, "T04", repo, base, ["sub/**"])
    _write_done(repo, head, ["sub/a.txt", "sub/test_ok.py"], ok=False)
    assert main(["gate", "T04"]) == 1
    out = capsys.readouterr().out
    assert "tests-fail: done.json ok=false" in out


def test_gate_cmd_mismatch(tmp_path, capsys):
    """done.cmd=/bin/true при падающей приёмке: OK запрещён, приёмка каноническая."""
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _task(tmp_path, "T05", repo, base, ["sub/**"])
    _write_done(repo, head, ["sub/a.txt", "sub/test_ok.py"], cmd="/bin/true")
    assert main(["gate", "T05"]) == 1
    out = capsys.readouterr().out
    assert "cmd-mismatch:" in out
    assert "OK T05" not in out


def test_gate_ok(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _task(tmp_path, "T03", repo, base, ["sub/**"])
    _write_done(repo, head, ["sub/a.txt", "sub/test_ok.py"])
    assert main(["gate", "T03"]) == 0
    assert "OK T03" in capsys.readouterr().out


def test_gate_real_card(tmp_path, capsys):
    """Реальная карточка (упоминание «Можно менять» в Цели): OK на легальном диффе."""
    repo, base = _repo(tmp_path)
    _commit(repo, "hub/gate/gate.py", "x\n")
    _with_test(repo, "tests/fixtures/gate/test_ok.py")
    head = _git(repo, "rev-parse", "HEAD")
    card = Path(__file__).parent / "fixtures" / "gate" / "real-card.md"
    assert card.is_file()
    Store().upsert_task(id="TR", worktree=str(repo), base_sha=base,
                        branch="agent/TR", card_path=str(card))
    _write_done(repo, head, ["hub/gate/gate.py", "tests/fixtures/gate/test_ok.py"])
    assert main(["gate", "TR"]) == 0
    assert "OK TR" in capsys.readouterr().out


def test_stub_matches_real_lint(monkeypatch, _stub_lint):
    """Семантика стаба == настоящему hub.gate.lint на реальной карточке.

    Пока H02 не слит — skipped; после мержа станет активной и поймает расхождение.
    """
    monkeypatch.delitem(sys.modules, "hub.gate.lint", raising=False)
    real = pytest.importorskip("hub.gate.lint", reason="H02 ещё не слит")
    stub = _make_stub_lint()
    lines = (Path(__file__).parent / "fixtures" / "gate" / "real-card.md"
             ).read_text(encoding="utf-8").splitlines()
    for section in ("Можно менять", "Приёмка"):
        assert real._section_text(lines, section) == stub._section_text(lines, section)
    sec = stub._section_text(lines, "Можно менять")
    assert real._can_change_globs(sec) == stub._can_change_globs(sec)
    acc = stub._section_text(lines, "Приёмка")
    assert real._pytest_nodes(acc) == stub._pytest_nodes(acc)


def test_dirty_blocks_acceptance(tmp_path):
    """Незакоммиченный conftest.py (меняет исход приёмки мимо диффа) → dirty, без запуска."""
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    (repo / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n    items[:] = []\n", encoding="utf-8")
    marker = tmp_path / "marker"
    cmd = [sys.executable, "-c", "open(%r,'w').write('x')" % str(marker)]
    res = check_gate(repo, base, "HEAD", ["**"], cmd)
    assert not res.ok
    assert any(e.startswith("dirty:") for e in res.errors)
    assert any("conftest.py" in e for e in res.errors)
    assert not marker.exists()  # приёмка не запускалась


def test_agent_dir_ignored_in_dirty(tmp_path):
    """Неотслеженный .agent/done.json — не грязь (в живом репо .agent/ игнорируется)."""
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    _write_done(repo, _git(repo, "rev-parse", "HEAD"), ["a.txt"])
    res = check_gate(repo, base, "HEAD", ["**"], PASS)
    assert res.ok, res.errors


def test_forbidden_skips_acceptance(tmp_path):
    """forbidden-дифф: итог не-ok без захвата замка и запуска приёмки."""
    repo, base = _repo(tmp_path)
    _commit(repo, "other/x.txt", "x\n")
    marker = tmp_path / "marker"
    cmd = [sys.executable, "-c", "open(%r,'w').write('x')" % str(marker)]
    res = check_gate(repo, base, "HEAD", ["hub/**"], cmd)
    assert not res.ok
    assert any(e.startswith("forbidden:") for e in res.errors)
    assert not marker.exists()  # приёмка не запускалась


def test_git_error_not_empty_diff(tmp_path):
    """Битый base — это git-error, а не empty-diff (причину не маскируем)."""
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    res = check_gate(repo, base, "0" * 40, ["**"], PASS)
    assert not res.ok
    assert any(e.startswith("git-error:") for e in res.errors)
    assert not any(e == "empty-diff" for e in res.errors)


def test_cmd_covers_tokens_and_all_nodes():
    """_cmd_covers: токен pytest (не подстрока) + все ноды приёмки."""
    assert _cmd_covers(shlex.split("python -m pytest -q"), [])
    both = ["tests/test_gate.py", "tests/test_verdict.py"]
    full = shlex.split("python -m pytest -q tests/test_gate.py tests/test_verdict.py")
    assert _cmd_covers(full, both)
    part = shlex.split("python -m pytest -q tests/test_gate.py")
    assert not _cmd_covers(part, both)  # частичное покрытие — не покрытие
    assert not _cmd_covers(["mypytest", "-q"], [])  # подстрока в имени — не pytest
    assert not _cmd_covers(["/bin/true"], [])
    assert _cmd_covers(["/x/.venv/bin/pytest", "-q"], [])  # путь к бинарнику — pytest


def test_load_done_empty_cmd(tmp_path):
    """Пустой tests.cmd отклоняется: ok=true без команды заявить нельзя."""
    repo, base = _repo(tmp_path)
    _write_done(repo, base, [], cmd="")
    with pytest.raises(ValueError, match=r"done\.json:.*tests\.cmd"):
        load_done(repo)


def test_load_done_unreadable(tmp_path):
    """done.json-каталог (EISDIR): ValueError «не читается», а не «нет файла»."""
    repo, _ = _repo(tmp_path)
    d = repo / ".agent"
    d.mkdir(exist_ok=True)
    (d / "done.json").mkdir()
    with pytest.raises(ValueError, match=r"done\.json:"):
        load_done(repo)


def test_gate_runs_acceptance(tmp_path, capsys):
    """Проводка hub gate → check_gate: красная приёмка даёт exit 1 + tests-fail:."""
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _commit(repo, "sub/test_red.py", "def test_red():\n    assert False\n")
    _task(tmp_path, "T10", repo, base, ["sub/**"])
    _write_done(repo, _git(repo, "rev-parse", "HEAD"), ["sub/a.txt", "sub/test_red.py"])
    assert main(["gate", "T10"]) == 1
    assert "tests-fail:" in capsys.readouterr().out


def test_gate_uses_project_lock(tmp_path, capsys):
    """Проводка lock_path: занятый замок проекта → exit 1 + locked:, приёмка не гонялась."""
    from hub.read.procs import lock_holder

    proj = tmp_path / "proj"
    proj.mkdir()
    lock = proj / "t.lock"
    lock.touch()
    (proj / ".hub.toml").write_text(
        'schema_version = 1\nname = "T"\npython = "%s"\ntest_lock = "%s"\n'
        % (sys.executable, lock), encoding="utf-8")
    repo, base = _repo(tmp_path, name="wtrepo")
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    _task(tmp_path, "T11", repo, base, ["sub/**"])
    _write_done(repo, _git(repo, "rev-parse", "HEAD"), ["sub/a.txt", "sub/test_ok.py"])
    ready = tmp_path / "ready"
    script = ("import fcntl, sys, time; fd = open(sys.argv[1], 'w'); "
              "fcntl.flock(fd, fcntl.LOCK_EX); "
              "open(sys.argv[2], 'w').write('ready'); time.sleep(20)")
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(lock), str(ready)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while True:
            if ready.exists() and lock_holder(str(lock)) is not None:
                break
            if holder.poll() is not None:
                pytest.fail("держатель замка упал")
            if time.time() > deadline:
                pytest.fail("держатель не взял замок")
            time.sleep(0.05)
        assert main(["gate", "T11", "--project", str(proj)]) == 1
        out = capsys.readouterr().out
        assert "locked:" in out
        assert "pid" in out
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_gate_warns_without_hub_toml(tmp_path, capsys):
    """Нет .hub.toml — предупреждение в stderr, ворота идут без замка, но честно."""
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _with_test(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _task(tmp_path, "T12", repo, base, ["sub/**"])
    _write_done(repo, head, ["sub/a.txt", "sub/test_ok.py"])
    assert main(["gate", "T12"]) == 0
    assert "warn:" in capsys.readouterr().err


def test_harness_prev_dir_ignored_in_dirty(tmp_path):
    """Незакоммиченный .agent.prev_<ts>/ создаёт харнесс — не грязь исполнителя."""
    repo, base = _repo(tmp_path)
    _commit(repo, "a.txt", "1\n2\n")
    prev = repo / ".agent.prev_123"
    prev.mkdir()
    (prev / "review.json").write_text("{}", encoding="utf-8")
    res = check_gate(repo, base, "HEAD", ["**"], PASS)
    assert res.ok, res.errors
    assert not any(e.startswith("dirty:") for e in res.errors)


def test_rename_old_path_forbidden(tmp_path):
    """Перенос файла вне allowed: виден и старый путь (--no-renames) → forbidden."""
    repo, _ = _repo(tmp_path)
    _commit(repo, "other/x.txt", "x\n")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "hub").mkdir()
    _git(repo, "mv", "other/x.txt", "hub/x.txt")
    _git(repo, "commit", "-m", "mv")
    res = check_gate(repo, base, "HEAD", ["hub/**"], PASS)
    assert not res.ok
    assert "forbidden: other/x.txt" in res.errors


def test_gate_rename_no_unknown_file(tmp_path, capsys):
    """Перенос: честный done.json с исходным путём — без ложного unknown-file."""
    repo, _ = _repo(tmp_path)
    _commit(repo, "other/x.txt", "x\n")
    base = _git(repo, "rev-parse", "HEAD")
    _task(tmp_path, "T13", repo, base, ["hub/**", "sub/**"])
    _with_test(repo)
    (repo / "hub").mkdir()
    _git(repo, "mv", "other/x.txt", "hub/x.txt")
    _git(repo, "commit", "-m", "mv")
    head = _git(repo, "rev-parse", "HEAD")
    _write_done(repo, head, ["other/x.txt", "hub/x.txt", "sub/test_ok.py"])
    assert main(["gate", "T13"]) == 1
    out = capsys.readouterr().out
    assert "forbidden: other/x.txt" in out
    assert "unknown-file" not in out


def test_gate_done_errors_skip_acceptance(tmp_path, capsys):
    """mismatch при красной приёмке: exit 1 по mismatch, pytest не гонялся (нет tests-fail)."""
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    _commit(repo, "sub/test_red.py", "def test_red():\n    assert False\n")
    _task(tmp_path, "T14", repo, base, ["sub/**"])
    _write_done(repo, "0" * 40, ["sub/a.txt", "sub/test_red.py"])
    assert main(["gate", "T14"]) == 1
    out = capsys.readouterr().out
    assert "mismatch:" in out
    assert "tests-fail:" not in out


def test_unicode_names_not_mangled(tmp_path):
    """Не-ASCII имена без кавычек: легальный файл не forbidden, грязь читаема."""
    repo, base = _repo(tmp_path)
    _commit(repo, "hub/юникод.txt", "x\n")
    res = check_gate(repo, base, "HEAD", ["hub/**"], PASS)
    assert res.ok, res.errors
    (repo / "hub" / "юникод2.txt").write_text("y\n", encoding="utf-8")
    res2 = check_gate(repo, base, "HEAD", ["hub/**"], PASS)
    assert not res2.ok
    assert "dirty: hub/юникод2.txt" in res2.errors


def test_gate_empty_diff_cmd(tmp_path, capsys):
    """Командный уровень: пустой base..HEAD → exit 1 + empty-diff."""
    repo, base = _repo(tmp_path)
    _task(tmp_path, "T15", repo, base, ["sub/**"])
    _write_done(repo, base, [])
    assert main(["gate", "T15"]) == 1
    assert "empty-diff" in capsys.readouterr().out


def test_gate_forbidden_cmd(tmp_path, capsys):
    """Командный уровень: дифф вне allowed → exit 1 + forbidden:."""
    repo, base = _repo(tmp_path)
    _commit(repo, "other/x.txt", "x\n")
    _task(tmp_path, "T16", repo, base, ["hub/**"])
    _write_done(repo, _git(repo, "rev-parse", "HEAD"), ["other/x.txt"])
    assert main(["gate", "T16"]) == 1
    assert "forbidden:" in capsys.readouterr().out


def test_gate_no_task(capsys):
    """Командный уровень: нет задачи → exit 1 + no-task:."""
    assert main(["gate", "НЕТ-ТАКОЙ-ЗАДАЧИ"]) == 1
    assert "no-task:" in capsys.readouterr().out


def test_gate_no_card(tmp_path, capsys):
    """Командный уровень: нет карточки → exit 1 + no-card:."""
    repo, base = _repo(tmp_path)
    _commit(repo, "sub/a.txt", "1\n2\n")
    Store().upsert_task(id="T17", worktree=str(repo), base_sha=base,
                        branch="agent/T17", card_path=str(tmp_path / "нет-карточки.md"))
    _write_done(repo, _git(repo, "rev-parse", "HEAD"), ["sub/a.txt"])
    assert main(["gate", "T17"]) == 1
    assert "no-card:" in capsys.readouterr().out
