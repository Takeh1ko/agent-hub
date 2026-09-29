"""H11 — хвосты hub: background, on_session при старте, stale в findings, _run_one в status."""

from __future__ import annotations

import json
import os
import subprocess as _sp
import threading
from pathlib import Path

from hub.cli import main
from hub.commands import findings as fcmd
from hub.config import ProjectConfig
from hub.gate import lint as lint_mod
from hub.gate.lint import card_level, lint_card
from hub.pipeline.runners import AgyRunner, OpencodeRunner
from hub.read import procs as pr
from hub.read import snapshot as snap
from hub.store import Store

NOW = 1_789_000_000_000


def _card(level_line: str) -> list[str]:
    return [
        "# H11t",
        "",
        "**Цель.** закрыть хвосты",
        "",
        "**Прочитать.** `hub/x.py`",
        "",
        "**Можно менять.** `hub/x.py`",
        "",
        "**Интерфейс.** `f()`",
        "",
        "**Приёмка.** `pytest -q tests/test_h11_tails.py`",
        "",
        "**Нельзя.** сеть",
        "",
        "**Сеть.** нет",
        "",
        level_line,
        "",
        "**Исполнитель.** musefree",
        "",
        "**Коммит.** `fix: x`",
    ]


def test_level_background_wins():
    assert card_level(_card("**Уровень.** background")) == "background"


def test_level_medium_explicit():
    assert card_level(_card("**Уровень.** medium")) == "medium"


def test_level_default_still_medium():
    assert card_level([l for l in _card("**Уровень.** нет") if "Уровень" not in l]) == "medium"


def test_network_prose_not_header():
    assert "Сеть" not in lint_mod._bold_header_names("**Сеть** — не нужна")
    assert not lint_mod._is_header_line("**Сеть** — не нужна", "Сеть")
    # Настоящий заголовок узнаётся как раньше.
    assert lint_mod._is_header_line("**Сеть.** нет", "Сеть")
    assert "Сеть" in lint_mod._bold_header_names("**Сеть.** нет. **Уровень.** medium")


def test_lint_missing_network_with_prose(tmp_path):
    """Проза `**Сеть** — не нужна` не закрывает отсутствие раздела «Сеть»."""
    lines = []
    for line in _card("**Уровень.** background"):
        if line.startswith("**Сеть.**"):
            continue
        if line.startswith("**Цель.**"):
            line += " (**Сеть** — не нужна)"
        lines.append(line)
    assert card_level(lines) == "background"
    card = tmp_path / "card.md"
    card.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proj = ProjectConfig(root=str(tmp_path), allowed_paths=["hub/**", "tests/**"])
    res = lint_card(card, proj, check_read_paths=False, check_acceptance=False)
    assert not res.ok
    assert any("«Сеть»" in e for e in res.errors)


class _FakeErr:
    def read(self) -> str:
        return ""


class _FakeProc:
    def __init__(self, stdout_iter) -> None:
        self.stdout = stdout_iter
        self.stderr = _FakeErr()
        self.returncode = 0

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        pass


def test_on_session_during_stream(tmp_path, monkeypatch):
    """Колбэк fired ещё до конца stdout — линк в store возможен во время шага."""
    fired = threading.Event()
    seen: list[str] = []

    def _gen():
        yield '{"type":"session","sessionID":"ses-live"}\n'
        # Вторая строка не отдаётся, пока колбэк не сработал: при линке
        # только в конце шага тест завис бы здесь до join-таймаута и упал.
        assert fired.wait(timeout=15), "on_session не вызван во время стрима"
        yield '{"type":"done"}\n'

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _FakeProc(_gen()))

    def _cb(sid: str) -> None:
        seen.append(sid)
        fired.set()

    log = tmp_path / "op.log"
    got = OpencodeRunner().start("короткий промпт", str(tmp_path),
                                 log=str(log), on_session=_cb)
    assert got == "ses-live"
    assert seen == ["ses-live"]
    assert "ses-live" in log.read_text(encoding="utf-8")


def test_on_session_error_swallowed(tmp_path, monkeypatch):
    def _gen():
        yield '{"sessionID": "ses-err"}\n'

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _FakeProc(_gen()))

    def _bad(sid: str) -> None:
        raise RuntimeError("линк упал")

    got = OpencodeRunner().start("промпт", str(tmp_path),
                                 log=str(tmp_path / "o.log"), on_session=_bad)
    assert got == "ses-err"


def test_no_callback_same_sid(tmp_path, monkeypatch):
    def _gen():
        yield '{"sessionID": "ses-x"}\n'
        yield '{"type":"done"}\n'

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _FakeProc(_gen()))
    got = OpencodeRunner().start("промпт", str(tmp_path), log=str(tmp_path / "o.log"))
    assert got == "ses-x"


def _git(cwd: Path, *args: str) -> None:
    _sp.run(["git", *args], cwd=str(cwd), check=True,
            capture_output=True, text=True)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _head(repo: Path) -> str:
    r = _sp.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                capture_output=True, text=True, check=True)
    return r.stdout.strip()


def test_findings_review_commit_and_stale(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    a = repo / "a.py"
    a.write_text("".join(f"строка {i}\n" for i in range(1, 11)), encoding="utf-8")
    (repo / "b.py").write_text("x = 1\n", encoding="utf-8")
    _write(repo / ".agent" / "review_r1.json", {
        "verdict": "changes",
        "findings": [
            {"file": "a.py", "line": 3, "issue": "баг в третьей", "severity": "high"},
            {"file": "a.py", "line": 9, "issue": "далёкий баг", "severity": "medium"},
            {"file": "b.py", "line": 1, "issue": "мелочь", "severity": "low"},
        ],
    })
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ревью")
    review = _head(repo)
    (repo / ".agent" / "done.json").write_text(
        json.dumps({"commit": review}), encoding="utf-8")
    s = Store()
    s.upsert_task(id="TS", stage="review r1", worktree=str(repo))
    # Нового коммита нет: шапка без HEAD, пометок нет.
    assert main(["findings", "TS"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"ревью по коммиту {review[:8]}"
    assert fcmd.STALE_MARK not in out
    # Правим строку 3 в a.py: замечание stale; b.py не трогаем.
    body = a.read_text(encoding="utf-8").splitlines()
    body[2] = "строка 3 ИСПРАВЛЕНА"
    a.write_text("\n".join(body) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "починка")
    head = _head(repo)
    assert head != review
    assert main(["findings", "TS"]) == 0
    out = capsys.readouterr().out
    assert review[:8] in out.splitlines()[0] and head[:8] in out.splitlines()[0]
    line_a = next(l for l in out.splitlines()[1:] if l.startswith("a.py:3"))
    line_b = next(l for l in out.splitlines()[1:] if l.startswith("b.py:1"))
    line_far = next(l for l in out.splitlines()[1:] if l.startswith("a.py:9"))
    assert fcmd.STALE_MARK in line_a
    assert fcmd.STALE_MARK not in line_b
    # Тот же файл, но строка вне изменённого ханка — не stale
    # (ловит грубую реализацию «весь изменённый файл = stale»).
    assert fcmd.STALE_MARK not in line_far


def test_findings_stale_deleted_lines(tmp_path, capsys):
    """Удалённые строки тоже помечаются: old-диапазон ханка с new count 0."""
    repo = tmp_path / "repo-del"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    a = repo / "a.py"
    a.write_text("".join(f"строка {i}\n" for i in range(1, 11)), encoding="utf-8")
    _write(repo / ".agent" / "review_r1.json", {
        "verdict": "changes",
        "findings": [
            {"file": "a.py", "line": 3, "issue": "баг в третьей", "severity": "high"},
            {"file": "a.py", "line": 9, "issue": "далёкий баг", "severity": "medium"},
        ],
    })
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ревью")
    review = _head(repo)
    (repo / ".agent" / "done.json").write_text(
        json.dumps({"commit": review}), encoding="utf-8")
    s = Store()
    s.upsert_task(id="TD", stage="review r1", worktree=str(repo))
    body = a.read_text(encoding="utf-8").splitlines()
    del body[2:4]
    a.write_text("\n".join(body) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "удалил строки")
    assert main(["findings", "TD"]) == 0
    out = capsys.readouterr().out
    line_del = next(l for l in out.splitlines()[1:] if l.startswith("a.py:3"))
    line_far = next(l for l in out.splitlines()[1:] if l.startswith("a.py:9"))
    assert fcmd.STALE_MARK in line_del
    assert fcmd.STALE_MARK not in line_far


def test_findings_empty_no_output(tmp_path, capsys):
    """Без замечаний — пустой вывод, без одинокой шапки (как до H11)."""
    repo = tmp_path / "repo-empty"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "код")
    s = Store()
    s.upsert_task(id="TE", stage="review r1", worktree=str(repo))
    assert main(["findings", "TE"]) == 0
    assert capsys.readouterr().out == ""


def _proc(root: Path, pid: int, argv: list[str]) -> Path:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes("\x00".join(argv).encode() + b"\x00")
    (d / "stat").write_text(
        f"{pid} (x) R 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n",
        encoding="utf-8")
    return d


def test_run_one_manual_alive(tmp_path):
    """Ручной _run_one без worktree: задача жива (🟡), без ложных ⚫/🔴."""
    s = Store()
    s.upsert_task(id="TM", stage="exec r1", round=1, worktree="",
                  updated_at=NOW - 21 * 60_000)
    root = tmp_path / "proc"
    pdir = _proc(root, 50, ["python", "-m", "hub.commands.queue", "--run-one", "TM"])
    os.utime(pdir / "stat", (NOW / 1000 - 300, NOW / 1000 - 300))
    got = snap.build(s, NOW, proc_root=root).tasks[0]
    assert got.pulse == "🟡", got.pulse


def test_run_one_exact_id_no_false_alive(tmp_path):
    """Чужой T10 не даёт живости задаче T1."""
    root = tmp_path / "proc"
    _proc(root, 51, ["python", "-m", "hub.commands.queue", "--run-one", "T10"])
    assert snap._run_one_pids(root, "T1") == []
    assert snap._hub_task_procs([], root, "T1", NOW) == []
    s = Store()
    s.upsert_task(id="T1", stage="exec r1", round=1, worktree="",
                  updated_at=NOW - 30_000)
    got = snap.build(s, NOW, proc_root=root).tasks[0]
    assert got.pulse == "⚫", got.pulse


def test_hub_task_exact_id(tmp_path):
    """hub_task чужой задачи (подстрока T1 в T10) — не живой; свой — живой."""
    live = [
        pr.Proc(pid=60, kind="hub_task", cwd="", args=["hub", "review", "T10"],
                started_ms=NOW - 300_000, children=[]),
        pr.Proc(pid=61, kind="hub_task", cwd="", args=["hub", "review", "T1"],
                started_ms=NOW - 300_000, children=[]),
    ]
    root = tmp_path / "proc"
    root.mkdir()
    assert [p.pid for p in snap._hub_task_procs(live, root, "T1", NOW)] == [61]
    assert snap._task_arg_hit(["hub", "review", "T10"], "T1") is False
    assert snap._task_arg_hit(["--run-one=T10"], "T1") is False
    assert snap._task_arg_hit(["--run-one=T1"], "T1") is True
    assert snap._task_arg_hit(["--id", "T1"], "T1") is True
    assert snap._task_arg_hit([], "T1") is False
    assert snap._task_arg_hit(["hub", "review", "T1"], "") is False


def test_hub_task_foreign_no_alive(tmp_path):
    """Задача с worktree: чужой hub_task T10 не делает T1 живой."""
    s = Store()
    wt = tmp_path / "wt-f"
    wt.mkdir()
    s.upsert_task(id="T1", stage="exec r1", round=1, worktree=str(wt),
                  updated_at=NOW - 30_000)
    root = tmp_path / "proc"
    _proc(root, 60, ["hub", "review", "T10"])
    got = snap.build(s, NOW, proc_root=root).tasks[0]
    assert got.pulse == "⚫", got.pulse


def test_hub_task_own_alive(tmp_path):
    """Свой hub_task-процесс — живость без регрессии (🟢 на свежем пульсе)."""
    s = Store()
    wt = tmp_path / "wt-o"
    wt.mkdir()
    s.upsert_task(id="T1", stage="exec r1", round=1, worktree=str(wt),
                  updated_at=NOW - 30_000)
    root = tmp_path / "proc"
    _proc(root, 61, ["hub", "review", "T1"])
    got = snap.build(s, NOW, proc_root=root).tasks[0]
    assert got.pulse == "🟢", got.pulse


def test_agy_on_session(tmp_path, monkeypatch):
    """AgyRunner зовёт on_session, когда id известен (контракт Runner)."""
    import types

    seen: list[str] = []

    def _fake_run(*a, **k):
        return types.SimpleNamespace(
            stdout='{"conversation_id": "c-1"}\n', stderr="", returncode=0)

    monkeypatch.setattr(_sp, "run", _fake_run)
    got = AgyRunner().start("промпт", str(tmp_path),
                            log=str(tmp_path / "agy.log"),
                            on_session=seen.append)
    assert got == "c-1"
    assert seen == ["c-1"]
