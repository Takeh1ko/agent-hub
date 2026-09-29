"""H13: повтор при сбое сети/сервера, новая сессия при смене карточки, честный этап."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from hub.config import ProjectConfig, Defaults
from hub.pipeline import cycle as cyc
from hub.pipeline.runners import (
    TransientError,
    is_transient_text,
    transient_error_of_line,
)
from hub.store import Store


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _mk_repo(tmp_path, name="wt"):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "sub").mkdir(exist_ok=True)
    (repo / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")
    return repo, base


def _mk_project(repo: Path, retry_max=3, retry_pause=0.01) -> ProjectConfig:
    return ProjectConfig(
        name="T", root=str(repo), worktrees=str(repo.parent / "wt-dir"),
        rules="docs/rules.md", python=sys.executable, test_lock="",
        work_branch="main", push="", allowed_paths=["sub/**", "docs/**", "tests/**"],
        defaults=Defaults(executor="muse", reviewers=["muse"], budget_go=0.5),
        retry_max=retry_max, retry_pause_s=retry_pause,
    )


CARD = """# T
**Цель.** ц
**Прочитать.** docs/rules.md
**Можно менять.** `sub/**`
**Интерфейс.** `f()`
**Приёмка.** `pytest -q sub/test_ok.py`
**Нельзя.** сеть
**Сеть.** нет
**Исполнитель.** muse
**Уровень.** hard
**Коммит.** `feat: x`
"""


def _mk_task(tmp_path, repo, base, tid="T13", card_text=CARD):
    card = tmp_path / f"{tid}.md"
    card.write_text(card_text, encoding="utf-8")
    h = hashlib.sha256(card.read_bytes()).hexdigest()
    Store().upsert_task(id=tid, project="T", card_path=str(card),
                        card_hash=h, level="hard", branch=f"agent/{tid}",
                        worktree=str(repo), base_sha=base, stage="queued",
                        round=0, executor="muse", reviewers_json='["muse"]',
                        stage_reason="", budget_go=0.5, budget_usd=0.0)
    return card


def _commit_ok(repo: Path, rel="sub/a.txt", text="2\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", rel)
    head = _git(repo, "rev-parse", "HEAD")
    d = repo / ".agent"
    d.mkdir(exist_ok=True)
    (d / "done.json").write_text(json.dumps({
        "commit": head, "files": [rel],
        "tests": {"cmd": sys.executable + " -m pytest -q", "ok": True, "tail": "ok"},
        "notes": ""}), encoding="utf-8")
    return head


# --- п.1: детект ---

def test_transient_markers():
    assert is_transient_text("UnknownError: Unexpected server error")
    assert is_transient_text("Cannot connect to API foo")
    assert is_transient_text("Unable to connect bar")
    assert is_transient_text("ECONNREFUSED baz")
    assert is_transient_text("ETIMEDOUT")
    assert is_transient_text("socket hang up")
    assert is_transient_text("HTTP 429 too many")
    assert is_transient_text("status 500 internal")
    assert not is_transient_text("invalid tool call: нет такого")
    assert not is_transient_text("обычный ответ модели")


def test_transient_error_line_only_type_error():
    line = json.dumps({"type": "error", "error": "UnknownError: Unexpected server error"})
    assert transient_error_of_line(line) is not None
    ok_line = json.dumps({"sessionID": "s-1", "type": "message"})
    assert transient_error_of_line(ok_line) is None
    other = json.dumps({"type": "error", "error": "invalid tool call"})
    assert transient_error_of_line(other) is None
    assert transient_error_of_line("не json") is None
    # Постороннее 429 вне текста ошибки — не транзиент.
    assert transient_error_of_line(json.dumps({
        "type": "error", "error": "invalid tool call",
        "elapsed_ms": 429})) is None
    assert transient_error_of_line(json.dumps({
        "type": "error", "error": "invalid tool call",
        "note": "took 429ms"})) is None
    # А в самом тексте ошибки 429 — транзиент.
    assert transient_error_of_line(json.dumps({
        "type": "error", "error": "HTTP 429 too many requests"})) is not None


def test_opencode_runner_raises_transient(tmp_path, monkeypatch):
    import subprocess as _sp

    from hub.pipeline.runners import OpencodeRunner

    class _FakeErr:
        def read(self):
            return ""

    class _Proc:
        def __init__(self):
            self.stdout = iter([
                json.dumps({"sessionID": "s-9"}) + "\n",
                json.dumps({"type": "error",
                            "error": "UnknownError: Unexpected server error"}) + "\n",
            ])
            self.stderr = _FakeErr()
            self.returncode = 1
            self.pid = 2_000_000_021

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _Proc())
    r = OpencodeRunner(idle_s=0, timeout_s=30)
    try:
        r.start("промпт", str(tmp_path), log=str(tmp_path / "t.log"))
    except TransientError as e:
        assert "Unexpected server error" in str(e)
    else:
        raise AssertionError("ожидался TransientError")


def test_opencode_runner_plain_429_without_event_not_transient(tmp_path, monkeypatch):
    """Голые «429» без {"type":"error"} — как раньше (RuntimeError, не повтор)."""
    import subprocess as _sp

    from hub.pipeline.runners import OpencodeRunner

    class _FakeErr:
        def read(self):
            return ""

    class _Proc:
        def __init__(self):
            self.stdout = iter(["какая-то строка 429 без json\n"])
            self.stderr = _FakeErr()
            self.returncode = 1
            self.pid = 2_000_000_032

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _Proc())
    try:
        OpencodeRunner(idle_s=0, timeout_s=30).start(
            "промпт", str(tmp_path), log=str(tmp_path / "p.log"))
    except TransientError:
        raise AssertionError("голые 429 без error-события — не TransientError")
    except RuntimeError as e:
        assert "нет sessionID" in str(e)
    else:
        raise AssertionError("ожидался RuntimeError")


def test_opencode_runner_error_then_hang_is_transient(tmp_path, monkeypatch):
    """Error-событие п.1, потом завис: приоритет TransientError, а не тишина."""
    import subprocess as _sp
    import threading

    from hub.pipeline.runners import OpencodeRunner

    class _FakeErr:
        def read(self):
            return ""

    class _HangAfterError:
        def __init__(self):
            self.stdout = self._gen()
            self.stderr = _FakeErr()
            self.returncode = None
            self.pid = 2_000_000_031
            self._killed = threading.Event()

        def _gen(self):
            yield json.dumps({"sessionID": "s-hang"}) + "\n"
            yield json.dumps({"type": "error",
                              "error": "Unexpected server error"}) + "\n"
            # Дальше тишина: генератор висит до kill.
            while not self._killed.is_set():
                import time as _t
                _t.sleep(0.05)
                yield ""

        def wait(self, timeout=None):
            if self._killed.is_set():
                self.returncode = 1
                return 1
            import time as _t
            _t.sleep(timeout if timeout else 0.2)
            if self._killed.is_set():
                self.returncode = 1
                return 1
            raise _sp.TimeoutExpired(cmd="opencode", timeout=timeout)

        def kill(self):
            self._killed.set()

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _HangAfterError())
    try:
        OpencodeRunner(idle_s=1, timeout_s=30).start(
            "промпт", str(tmp_path), log=str(tmp_path / "hang.log"))
    except TransientError as e:
        assert "Unexpected server error" in str(e)
    else:
        raise AssertionError("ожидался TransientError, а не тишина")


def test_opencode_runner_other_error_not_transient(tmp_path, monkeypatch):
    import subprocess as _sp

    from hub.pipeline.runners import OpencodeRunner

    class _FakeErr:
        def read(self):
            return ""

    class _Proc:
        def __init__(self):
            self.stdout = iter([
                json.dumps({"sessionID": "s-10"}) + "\n",
                json.dumps({"type": "error",
                            "error": "invalid tool call: нет такого"}) + "\n",
            ])
            self.stderr = _FakeErr()
            self.returncode = 1
            self.pid = 2_000_000_022

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _Proc())
    got = OpencodeRunner(idle_s=0, timeout_s=30).start(
        "промпт", str(tmp_path), log=str(tmp_path / "o.log"))
    assert got == "s-10"


# --- п.2: повторы ревьюера ---

class ExecOk:
    tool = "opencode"
    model = "muse"

    def start(self, prompt, cwd, log=None):
        _commit_ok(Path(cwd))
        return "exec-1"

    def resume(self, sid, prompt, cwd, log=None):
        _commit_ok(Path(cwd), text="3\n")
        return sid or "exec-1"


class FlakyRevTwice:
    """2 раза TransientError, на третий — approve."""

    def __init__(self, name="muse"):
        self.name = name
        self.tool = "opencode"
        self.model = name
        self.calls = 0
        self.resumes = 0

    def start(self, prompt, cwd, log=None):
        import re as _re

        self.calls += 1
        if self.calls <= 2:
            raise TransientError("UnknownError: Unexpected server error (прокси)")
        m = _re.search(r"review_r(\d+)\.json", prompt or "")
        rnd = int(m.group(1)) if m else 1
        p = Path(cwd) / ".agent" / f"review_r{rnd}_{self.name}.json"
        p.write_text(json.dumps({"verdict": "approve", "findings": []}),
                     encoding="utf-8")
        return f"rev-{self.name}-{rnd}"

    def resume(self, sid, prompt, cwd, log=None):
        self.resumes += 1
        return sid


def test_reviewer_two_transients_then_ready(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TR2")
    rev = FlakyRevTwice()
    got = cyc.run_task(Store(), proj, "TR2",
                       {"executor": ExecOk(), "reviewers": {"muse": rev}},
                       rounds=2)
    assert got == "ready", Store().get_task("TR2")
    assert rev.calls == 3
    # Ревьюер — новой сессией: resume для повтора не зовём.
    assert rev.resumes == 0
    events = [e for e in Store().events_since(0) if e["task_id"] == "TR2"]
    retry = [e for e in events if "сбой opencode" in (e.get("payload_json") or "")
             and "повтор" in (e.get("payload_json") or "")]
    assert len(retry) == 2, [e.get("payload_json") for e in events]
    assert any("повтор 1/3" in (e.get("payload_json") or "") for e in retry)
    assert any("повтор 2/3" in (e.get("payload_json") or "") for e in retry)


class AlwaysTransientRev:
    def __init__(self, name="muse"):
        self.name = name
        self.tool = "opencode"
        self.model = name
        self.calls = 0

    def start(self, prompt, cwd, log=None):
        self.calls += 1
        raise TransientError("Cannot connect to API (прокси выключен)")

    def resume(self, sid, prompt, cwd, log=None):
        return sid


def test_reviewer_all_transients_arbiter_network(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TRF")
    rev = AlwaysTransientRev()
    got = cyc.run_task(Store(), proj, "TRF",
                       {"executor": ExecOk(), "reviewers": {"muse": rev}},
                       rounds=1)
    assert got == "arbiter"
    reason = (Store().get_task("TRF") or {}).get("stage_reason", "")
    assert "сбой сети/сервера opencode" in reason, reason
    assert "панель молчит" not in reason
    # 1 + 3 повтора = 4 вызова при retry_max=3.
    assert rev.calls == 4, rev.calls


class NonTransientRev:
    tool = "opencode"
    model = "muse"

    def start(self, prompt, cwd, log=None):
        raise RuntimeError("invalid tool call: нет такого инструмента")

    def resume(self, sid, prompt, cwd, log=None):
        return sid


def test_non_transient_no_retry_panel_silent(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TNT")
    got = cyc.run_task(Store(), proj, "TNT",
                       {"executor": ExecOk(),
                        "reviewers": {"muse": NonTransientRev()}},
                       rounds=1)
    assert got == "arbiter"
    reason = (Store().get_task("TNT") or {}).get("stage_reason", "")
    assert "панель молчит" in reason, reason
    assert "сбой сети/сервера" not in reason
    events = [e for e in Store().events_since(0) if e["task_id"] == "TNT"]
    assert not any("сбой opencode" in (e.get("payload_json") or "") for e in events)


def test_exec_transient_retry_then_ready(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TE1")

    class FlakyExec:
        tool = "opencode"
        model = "muse"

        def __init__(self):
            self.n = 0

        def start(self, prompt, cwd, log=None):
            self.n += 1
            if self.n == 1:
                raise TransientError("socket hang up")
            _commit_ok(Path(cwd))
            return "exec-1"

        def resume(self, sid, prompt, cwd, log=None):
            _commit_ok(Path(cwd), text="3\n")
            return sid or "exec-1"

    class RevApprove:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            import re as _re

            m = _re.search(r"review_r(\d+)\.json", prompt or "")
            rnd = int(m.group(1)) if m else 1
            (Path(cwd) / ".agent" / f"review_r{rnd}_muse.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}),
                encoding="utf-8")
            return "rev-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    exe = FlakyExec()
    got = cyc.run_task(Store(), proj, "TE1",
                       {"executor": exe, "reviewers": {"muse": RevApprove()}},
                       rounds=1)
    assert got == "ready"
    assert exe.n == 2
    events = [e for e in Store().events_since(0) if e["task_id"] == "TE1"]
    assert any("сбой opencode" in (e.get("payload_json") or "") for e in events)


def test_exec_all_transients_arbiter(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TEF")

    class DeadExec:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            raise TransientError("ETIMEDOUT")

        def resume(self, sid, prompt, cwd, log=None):
            raise TransientError("ETIMEDOUT")

    class RevApprove:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            return "rev-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    got = cyc.run_task(Store(), proj, "TEF",
                       {"executor": DeadExec(),
                        "reviewers": {"muse": RevApprove()}},
                       rounds=1)
    assert got == "arbiter"
    reason = (Store().get_task("TEF") or {}).get("stage_reason", "")
    assert "сбой сети/сервера opencode" in reason, reason


def test_retry_pause_grows_x2(monkeypatch, tmp_path):
    """Паузы растут ×2: при base=120 → 120/240/480."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo, retry_max=3, retry_pause=120.0)
    _mk_task(tmp_path, repo, base, tid="TPZ")
    sleeps: list[float] = []
    monkeypatch.setattr(cyc.time, "sleep", lambda s: sleeps.append(float(s)))
    rev = AlwaysTransientRev()
    got = cyc.run_task(Store(), proj, "TPZ",
                       {"executor": ExecOk(), "reviewers": {"muse": rev}},
                       rounds=1)
    assert got == "arbiter"
    assert sleeps == [120.0, 240.0, 480.0], sleeps
    assert cyc._fmt_pause(120.0) == "120"
    assert cyc._fmt_pause(0.01) == "0.01"


def test_retry_max_zero_no_retry(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo, retry_max=0, retry_pause=0.01)
    _mk_task(tmp_path, repo, base, tid="TR0")
    rev = AlwaysTransientRev()
    got = cyc.run_task(Store(), proj, "TR0",
                       {"executor": ExecOk(), "reviewers": {"muse": rev}},
                       rounds=1)
    assert got == "arbiter"
    assert rev.calls == 1, rev.calls
    events = [e for e in Store().events_since(0) if e["task_id"] == "TR0"]
    assert not any("повтор" in (e.get("payload_json") or "") for e in events)


def test_retry_cfg_defaults():
    proj = ProjectConfig(name="T", root="r", worktrees="w", rules="",
                         python=sys.executable, test_lock="",
                         work_branch="main", push="", allowed_paths=[],
                         defaults=Defaults(executor="muse", reviewers=["muse"]))
    max_r, base = cyc._retry_cfg(proj)
    assert (max_r, base) == (3, 120.0)
    assert cyc._retry_cfg(type("P", (), {"retry_max": "мусор",
                                         "retry_pause_s": "мусор"})()) == (3, 120.0)


def test_exec_round2_same_session_retry(tmp_path):
    """Круг ≥2: повтор исполнителя — тот же --session (resume того же sid)."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TR2S")
    calls: list = []

    class ExecRound2:
        tool = "opencode"
        model = "muse"

        def __init__(self):
            self.resume_n = 0

        def start(self, prompt, cwd, log=None):
            calls.append(("start", None))
            _commit_ok(Path(cwd))
            return "exec-r1"

        def resume(self, sid, prompt, cwd, log=None):
            calls.append(("resume", sid))
            # Первый resume круга 2 — транзиент, повтор — успех.
            if "fix" in (prompt or "").lower() or True:
                self.resume_n += 1
                if self.resume_n == 1:
                    raise TransientError("socket hang up")
            _commit_ok(Path(cwd), text="3\n")
            return sid or "exec-r1"

    class RevChangesOnce:
        tool = "opencode"
        model = "muse"

        def __init__(self):
            self.n = 0

        def start(self, prompt, cwd, log=None):
            import re as _re

            self.n += 1
            # Круг — по логу (reviewer_rN_), промпт содержит и историю r1.
            m2 = _re.search(r"reviewer_r(\d+)_", log or "")
            if m2:
                rnd = int(m2.group(1))
            else:
                m = _re.search(r"review_r(\d+)\.json", prompt or "")
                rnd = int(m.group(1)) if m else 1
            if rnd == 1:
                (Path(cwd) / ".agent" / f"review_r{rnd}_muse.json").write_text(
                    json.dumps({"verdict": "changes",
                                "findings": [{"file": "sub/a.txt", "line": 1,
                                              "issue": "поправь",
                                              "severity": "high"}]}),
                    encoding="utf-8")
            else:
                (Path(cwd) / ".agent" / f"review_r{rnd}_muse.json").write_text(
                    json.dumps({"verdict": "approve", "findings": []}),
                    encoding="utf-8")
            return f"rev-{rnd}"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    exe = ExecRound2()
    got = cyc.run_task(Store(), proj, "TR2S",
                       {"executor": exe, "reviewers": {"muse": RevChangesOnce()}},
                       rounds=2)
    assert got == "ready", Store().get_task("TR2S")
    resumes = [c for c in calls if c[0] == "resume"]
    assert len(resumes) >= 2, calls
    assert all(sid == "exec-r1" for _, sid in resumes), calls


def test_mixed_reviewers_transient_goes_arbiter(tmp_path):
    """Один ревьюер всегда TransientError, второй approve → arbiter, не ready."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TMIX")
    Store().upsert_task(id="TMIX", reviewers_json='["muse", "mimoflash"]')

    class RevApprove2:
        def __init__(self, name):
            self.name = name
            self.tool = "opencode"
            self.model = name

        def start(self, prompt, cwd, log=None):
            import re as _re

            m = _re.search(r"review_r(\d+)\.json", prompt or "")
            rnd = int(m.group(1)) if m else 1
            (Path(cwd) / ".agent" / f"review_r{rnd}_{self.name}.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}),
                encoding="utf-8")
            return f"rev-{self.name}"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    revs = {"muse": AlwaysTransientRev("muse"),
            "mimoflash": RevApprove2("mimoflash")}
    got = cyc.run_task(Store(), proj, "TMIX",
                       {"executor": ExecOk(), "reviewers": revs}, rounds=1)
    assert got == "arbiter", Store().get_task("TMIX")
    reason = (Store().get_task("TMIX") or {}).get("stage_reason", "")
    assert "сбой сети/сервера opencode" in reason, reason
    # Упавшему ревьюеру stub-approve не пишем.
    assert not (repo / ".agent" / "review_r1_muse.json").is_file()


# --- п.3: continue новая/старая сессия ---

def _mk_continue_proj(tmp_path, root_name="root"):
    root = tmp_path / root_name
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n",
        encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    base = _git(root, "rev-parse", "HEAD")
    return root, base


def test_continue_changed_card_new_session(tmp_path, capsys):
    from hub.cli import main

    root, base = _mk_continue_proj(tmp_path, "root1")
    tid = "TC1"
    wt = tmp_path / f"wt-{tid}"
    _git(root, "worktree", "add", str(wt), "-b", f"agent/{tid}", base)
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    card = tmp_path / f"{tid}.md"
    card.write_text(CARD, encoding="utf-8")
    h1 = hashlib.sha256(card.read_bytes()).hexdigest()
    Store().upsert_task(id=tid, project="T", card_path=str(card), card_hash=h1,
                        level="hard", branch=f"agent/{tid}", worktree=str(wt),
                        base_sha=base, stage="failed", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="x")
    Store().link_session("old-sid-1", "opencode", tid, "executor", 1, "muse")
    # Меняем карточку.
    card.write_text(CARD + "\nНовое решение арбитра.\n", encoding="utf-8")
    h2 = hashlib.sha256(card.read_bytes()).hexdigest()
    assert h2 != h1
    capsys.readouterr()
    assert main(["continue", tid, "--project", str(root)]) == 0
    capsys.readouterr()
    got = Store().get_task(tid)
    assert got["card_hash"] == h2
    events = [e for e in Store().events_since(0) if e["task_id"] == tid]
    assert any("карточка изменена" in (e.get("payload_json") or "") for e in events), \
        [e.get("payload_json") for e in events]
    # Следующий прогон — новая сессия (start, не resume старой).
    proj = ProjectConfig(name="T", root=str(root), rules="docs/rules.md",
                         python=sys.executable, test_lock="", work_branch="main",
                         push="", allowed_paths=["sub/**"],
                         defaults=Defaults(executor="muse", reviewers=["muse"]),
                         retry_max=3, retry_pause_s=0.01)

    class RecExec:
        tool = "opencode"
        model = "muse"

        def __init__(self):
            self.calls: list = []

        def start(self, prompt, cwd, log=None):
            self.calls.append(("start", None))
            _commit_ok(Path(cwd))
            return "new-sid"

        def resume(self, sid, prompt, cwd, log=None):
            self.calls.append(("resume", sid))
            _commit_ok(Path(cwd))
            return sid or "new-sid"

    class RevApprove:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            import re as _re

            m = _re.search(r"review_r(\d+)\.json", prompt or "")
            rnd = int(m.group(1)) if m else 1
            (Path(cwd) / ".agent" / f"review_r{rnd}_muse.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}),
                encoding="utf-8")
            return "rev-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    rec = RecExec()
    out = cyc.run_task(Store(), proj, tid,
                       {"executor": rec, "reviewers": {"muse": RevApprove()}},
                       rounds=1)
    assert out == "ready", Store().get_task(tid)
    assert rec.calls and rec.calls[0][0] == "start", rec.calls
    assert all(sid != "old-sid-1" for _, sid in rec.calls)


def test_continue_same_card_old_session(tmp_path, capsys):
    from hub.cli import main

    root, base = _mk_continue_proj(tmp_path, "root2")
    tid = "TC2"
    wt = tmp_path / f"wt-{tid}"
    _git(root, "worktree", "add", str(wt), "-b", f"agent/{tid}", base)
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    card = tmp_path / f"{tid}.md"
    card.write_text(CARD, encoding="utf-8")
    h1 = hashlib.sha256(card.read_bytes()).hexdigest()
    Store().upsert_task(id=tid, project="T", card_path=str(card), card_hash=h1,
                        level="hard", branch=f"agent/{tid}", worktree=str(wt),
                        base_sha=base, stage="failed", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="x")
    Store().link_session("old-sid-2", "opencode", tid, "executor", 1, "muse")
    capsys.readouterr()
    assert main(["continue", tid, "--project", str(root)]) == 0
    capsys.readouterr()
    got = Store().get_task(tid)
    assert got["card_hash"] == h1
    events = [e for e in Store().events_since(0) if e["task_id"] == tid]
    assert not any("карточка изменена" in (e.get("payload_json") or "")
                   for e in events)
    proj = ProjectConfig(name="T", root=str(root), rules="docs/rules.md",
                         python=sys.executable, test_lock="", work_branch="main",
                         push="", allowed_paths=["sub/**"],
                         defaults=Defaults(executor="muse", reviewers=["muse"]),
                         retry_max=3, retry_pause_s=0.01)

    class RecExec:
        tool = "opencode"
        model = "muse"

        def __init__(self):
            self.calls: list = []

        def start(self, prompt, cwd, log=None):
            self.calls.append(("start", None))
            _commit_ok(Path(cwd))
            return "new-sid"

        def resume(self, sid, prompt, cwd, log=None):
            self.calls.append(("resume", sid))
            _commit_ok(Path(cwd))
            return sid or "new-sid"

    class RevApprove:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            import re as _re

            m = _re.search(r"review_r(\d+)\.json", prompt or "")
            rnd = int(m.group(1)) if m else 1
            (Path(cwd) / ".agent" / f"review_r{rnd}_muse.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}),
                encoding="utf-8")
            return "rev-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    rec = RecExec()
    out = cyc.run_task(Store(), proj, tid,
                       {"executor": rec, "reviewers": {"muse": RevApprove()}},
                       rounds=1)
    assert out == "ready", Store().get_task(tid)
    assert rec.calls and rec.calls[0] == ("resume", "old-sid-2"), rec.calls


# --- п.4: честный этап очереди ---

def test_queue_taken_marks_exec(tmp_path):
    import hub.commands.queue as q

    proj_wt = tmp_path / "wt-P"
    proj_wt.mkdir(parents=True, exist_ok=True)
    proj = ProjectConfig(name="P", root=str(tmp_path),
                         worktrees=str(proj_wt), rules="",
                         python=sys.executable, test_lock="",
                         work_branch="main", push="", allowed_paths=[],
                         defaults=Defaults(executor="muse", reviewers=["muse"]))
    Store().upsert_task(id="Q1", project="P", card_path="", card_hash="h",
                        level="hard", branch="agent/Q1", worktree="",
                        base_sha="b", stage="queued", round=0, executor="muse",
                        reviewers_json="[]", stage_reason="",
                        budget_go=0.5, budget_usd=0.0, created_at=1000)
    assert q._mark_taken(Store(), "Q1") is True
    got = Store().get_task("Q1")
    assert got["stage"] == "exec r1", got
    assert int(got["round"] or 0) == 1
    # Повторная метка и чужой этап — не перетираем (атомарно).
    assert q._mark_taken(Store(), "Q1") is False
    Store().upsert_task(id="Q1", stage="gate r1", round=1)
    assert q._mark_taken(Store(), "Q1") is False
    assert Store().get_task("Q1")["stage"] == "gate r1"


def _unified_fake(stage_sink: dict, tid: str):
    """Один фейк на executor+reviewer (оба muse): различает по промпту."""

    class UnifiedFake:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            import re as _re

            if "review_r" in (prompt or ""):
                m0 = _re.search(r"reviewer_r(\d+)_", log or "")
                if m0:
                    rnd = int(m0.group(1))
                else:
                    m = _re.search(r"review_r(\d+)\.json", prompt or "")
                    rnd = int(m.group(1)) if m else 1
                m2 = _re.search(r"review_r\d+_([A-Za-z0-9_-]+)\.json", prompt or "")
                nm = m2.group(1) if m2 else "muse"
                (Path(cwd) / ".agent" / f"review_r{rnd}_{nm}.json").write_text(
                    json.dumps({"verdict": "approve", "findings": []}),
                    encoding="utf-8")
                return f"rev-{nm}-{rnd}"
            stage_sink["stage"] = (Store().get_task(tid) or {}).get("stage")
            _commit_ok(Path(cwd))
            return "exec-1"

        def resume(self, sid, prompt, cwd, log=None):
            import re as _re

            if "review_r" in (prompt or ""):
                return sid
            stage_sink["stage"] = (Store().get_task(tid) or {}).get("stage")
            _commit_ok(Path(cwd), text="3\n")
            return sid or "exec-1"

    return UnifiedFake()


def test_queue_exec_stage_during_work(tmp_path, monkeypatch):
    import hub.commands.queue as q

    repo, base = _mk_repo(tmp_path, name="wtq")
    card = tmp_path / "TQ1.md"
    card.write_text(CARD, encoding="utf-8")
    h = hashlib.sha256(card.read_bytes()).hexdigest()
    Store().upsert_task(id="TQ1", project="T", card_path=str(card), card_hash=h,
                        level="hard", branch="agent/TQ1", worktree=str(repo),
                        base_sha=base, stage="queued", round=0, executor="muse",
                        reviewers_json='["muse"]', stage_reason="",
                        budget_go=0.5, budget_usd=0.0)
    seen: dict = {}
    exe = _unified_fake(seen, "TQ1")
    monkeypatch.setattr("hub.pipeline.runners.make_runner", lambda name: exe)
    monkeypatch.setattr(q, "_resolve_project",
                        lambda task, src: _mk_project(repo))
    got = q._run_one("TQ1", None)
    assert got == "ready", Store().get_task("TQ1")
    assert seen.get("stage") == "exec r1", seen


def test_queue_stage_exec_during_preflight(tmp_path, monkeypatch):
    """Метка видна весь предполёт: без _mark_taken тут preflight, не exec.

    Мутация «return False» в начале _mark_taken валит этот тест
    (а слабый scenarный — нет: run_task сам ставит exec перед стартом).
    """
    import hub.commands.queue as q
    from hub.gate.preflight import PreflightResult

    repo, base = _mk_repo(tmp_path, name="wtqp")
    card = tmp_path / "TQP.md"
    card.write_text(CARD, encoding="utf-8")
    h = hashlib.sha256(card.read_bytes()).hexdigest()
    Store().upsert_task(id="TQP", project="T", card_path=str(card), card_hash=h,
                        level="hard", branch="agent/TQP", worktree=str(repo),
                        base_sha=base, stage="queued", round=0, executor="muse",
                        reviewers_json='["muse"]', stage_reason="",
                        budget_go=0.5, budget_usd=0.0)
    entry: dict = {}

    def _slow(store, task_id, project):
        try:
            entry["stage"] = (Store().get_task("TQP") or {}).get("stage")
        except Exception:
            entry["stage"] = None
        time.sleep(0.3)
        return PreflightResult(ok=True, reason="")

    monkeypatch.setattr(cyc, "preflight", _slow)
    seen: dict = {}
    exe = _unified_fake(seen, "TQP")
    monkeypatch.setattr("hub.pipeline.runners.make_runner", lambda name: exe)
    monkeypatch.setattr(q, "_resolve_project",
                        lambda task, src: _mk_project(repo))
    got = q._run_one("TQP", None)
    assert got == "ready", Store().get_task("TQP")
    # На входе в предполёт метка уже exec (очередь взяла), а не queued/preflight.
    assert entry.get("stage") == "exec r1", entry


def test_config_retry_defaults():
    from hub.config import _from_dict

    assert _from_dict({}).retry_max == 3
    assert _from_dict({}).retry_pause_s == 120.0
    assert _from_dict({"retry_max": 1, "retry_pause_s": 0.5}).retry_max == 1
    assert _from_dict({"retry_max": "мусор"}).retry_max == 3


def _observer_stubs(tmp_path: Path, mode: str) -> dict:
    """Фейки bin/{opencode,sleep,hub} + H/P/R/D для одного прогона observer.sh."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake_d = tmp_path / "faked"
    fake_d.mkdir(parents=True, exist_ok=True)
    hub = tmp_path / "hub"
    (hub / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (hub / "docs").mkdir(parents=True, exist_ok=True)
    (hub / "docs" / "observer_prompt.md").write_text(
        "snap {SNAP} out {OUT} prev {PREV}\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (bindir / "opencode").write_text(
        "#!/bin/bash\n"
        "n=$(cat \"$FAKE_D/count\" 2>/dev/null || echo 0)\n"
        "n=$((n+1)); echo \"$n\" > \"$FAKE_D/count\"\n"
        "if [ \"$FAKE_MODE\" = \"transient-always\" ]; then\n"
        "  echo '{\"type\":\"error\",\"error\":\"unexpected server error (proxy)\"}'\n"
        "elif [ \"$FAKE_MODE\" = \"transient-then-clean\" ]; then\n"
        "  if [ \"$n\" = \"1\" ]; then\n"
        "    echo '{\"type\":\"error\",\"error\":\"unexpected server error\"}'\n"
        "  else\n"
        "    echo '{\"sessionID\":\"ok-1\"}'\n"
        "  fi\n"
        "else\n"
        "  echo '{\"sessionID\":\"ok-1\"}'\n"
        "fi\n"
        "exit 0\n", encoding="utf-8")
    (bindir / "sleep").write_text(
        "#!/bin/bash\necho \"$*\" >> \"$FAKE_D/sleep.log\"\nexit 0\n",
        encoding="utf-8")
    (bindir / "hub").write_text(
        "#!/bin/bash\necho \"$*\" >> \"$FAKE_D/hub.log\"\n"
        "if [ \"$1\" = \"status\" ]; then echo ok; fi\n"
        "if [ \"$1\" = \"roster\" ]; then echo ok; fi\n"
        "exit 0\n", encoding="utf-8")
    import os as _os

    for f in ("opencode", "sleep", "hub"):
        _os.chmod(bindir / f, 0o755)
    (hub / ".venv" / "bin" / "hub").write_text(
        (bindir / "hub").read_text(encoding="utf-8"), encoding="utf-8")
    import os as _os2

    _os2.chmod(hub / ".venv" / "bin" / "hub", 0o755)
    try:
        import sys as _sys

        (hub / ".venv" / "bin" / "python").symlink_to(_sys.executable)
    except (OSError, FileExistsError):
        pass
    return {"bindir": bindir, "fake_d": fake_d, "hub": hub, "proj": proj,
            "obs": tmp_path / "obs"}


def _run_observer_once(env: dict, timeout_s: int = 60) -> None:
    import subprocess as _sp

    script = (Path(__file__).resolve().parents[1] / "tools" / "observer.sh")
    _sp.run(["bash", str(script)], env=env, capture_output=True, text=True,
            timeout=timeout_s, check=False)


def _observer_env(tmp_path: Path, stubs: dict, mode: str) -> dict:
    import os as _os

    env = dict(_os.environ)
    env["H"] = str(stubs["hub"])
    env["P"] = str(stubs["proj"])
    env["R"] = str(tmp_path / "run")
    env["D"] = str(stubs["obs"])
    env["EVERY"] = "0.05"
    env["OBSERVER_ONCE"] = "1"
    env["OBSERVER_MODEL"] = "dummy"
    env["FAKE_MODE"] = mode
    env["FAKE_D"] = str(stubs["fake_d"])
    env["PATH"] = str(stubs["bindir"]) + ":" + env.get("PATH", "")
    return env


def test_observer_retry_then_clean(tmp_path):
    """Первый лог транзиентный (другой регистр) → повтор; второй чистый."""
    stubs = _observer_stubs(tmp_path / "a", "transient-then-clean")
    _run_observer_once(_observer_env(tmp_path / "a", stubs,
                                      "transient-then-clean"))
    fake_d = stubs["fake_d"]
    try:
        count = int((fake_d / "count").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        count = 0
    assert count == 2, count
    sleep_log = ""
    try:
        sleep_log = (fake_d / "sleep.log").read_text(encoding="utf-8")
    except OSError:
        pass
    assert "120" in sleep_log.split(), sleep_log
    logs = sorted((stubs["obs"]).glob("agy_*.log"))
    assert len(logs) == 2, [p.name for p in logs]
    first = logs[0].read_text(encoding="utf-8")
    second = logs[1].read_text(encoding="utf-8")
    assert "unexpected server error" in first.lower()
    assert "unexpected server error" not in second.lower()


def test_observer_double_transient_network_problem(tmp_path):
    """Оба прогона с ошибкой сети → hub say про сбой сети/сервера."""
    stubs = _observer_stubs(tmp_path / "b", "transient-always")
    _run_observer_once(_observer_env(tmp_path / "b", stubs,
                                      "transient-always"))
    fake_d = stubs["fake_d"]
    try:
        count = int((fake_d / "count").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        count = 0
    assert count == 2, count
    hub_log = ""
    try:
        hub_log = (fake_d / "hub.log").read_text(encoding="utf-8")
    except OSError:
        pass
    assert "сбой сети/сервера opencode" in hub_log, hub_log


def test_observer_no_transient_no_retry(tmp_path):
    """Чистый первый прогон → повтора нет."""
    stubs = _observer_stubs(tmp_path / "c", "clean")
    _run_observer_once(_observer_env(tmp_path / "c", stubs, "clean"))
    fake_d = stubs["fake_d"]
    try:
        count = int((fake_d / "count").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        count = 0
    assert count == 1, count
    assert len(list((stubs["obs"]).glob("agy_*.log"))) == 1
    sleep_log = ""
    try:
        sleep_log = (fake_d / "sleep.log").read_text(encoding="utf-8")
    except OSError:
        pass
    assert "120" not in sleep_log.split(), sleep_log
