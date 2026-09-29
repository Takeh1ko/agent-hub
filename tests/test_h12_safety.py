"""H12: сторож тишины, изоляция hub.db, wip при continue. Фейки, без сети."""

from __future__ import annotations

import json
import os
import signal
import subprocess as _sp
import sys
import threading
import time
from pathlib import Path

import pytest

from hub.cli import main
from hub.config import ProjectConfig
from hub.pipeline import cycle as cyc
from hub.pipeline.runners import AgyRunner, OpencodeRunner, agent_env
from hub.store import Store


def _git(cwd, *args):
    r = _sp.run(["git", *args], cwd=str(cwd),
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


def _mk_project(repo: Path) -> ProjectConfig:
    from hub.config import Defaults

    return ProjectConfig(
        name="T", root=str(repo), worktrees=str(repo.parent / "wt-dir"),
        rules="docs/rules.md", python=sys.executable, test_lock="",
        work_branch="main", push="", allowed_paths=["sub/**", "docs/**", "tests/**"],
        defaults=Defaults(executor="musefree", reviewers=["muse"], budget_go=0.5),
        idle_s=1,
    )


CARD = """# T
**Цель.** ц
**Прочитать.** docs/rules.md
**Можно менять.** `sub/**`
**Интерфейс.** `f()`
**Приёмка.** `pytest -q sub/test_ok.py`
**Нельзя.** сеть
**Сеть.** нет
**Исполнитель.** musefree
**Уровень.** background
**Коммит.** `feat: x`
"""


def _mk_task(tmp_path, repo, base, tid="TH12", executor="musefree"):
    card = tmp_path / f"{tid}.md"
    card.write_text(CARD, encoding="utf-8")
    Store().upsert_task(id=tid, project="T", card_path=str(card),
                        card_hash="h", level="background", branch=f"agent/{tid}",
                        worktree=str(repo), base_sha=base, stage="queued",
                        round=0, executor=executor, reviewers_json='["muse"]',
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


class _FakeErr:
    def read(self) -> str:
        return ""


class _SilentProc:
    """Фейковый молчащий opencode: stdout пуст, wait висит до kill."""

    def __init__(self) -> None:
        self.stdout = iter([])
        self.stderr = _FakeErr()
        self.returncode = None
        self.pid = 2_000_000_000  # нет такого pid — детей нет
        self._killed = threading.Event()

    def wait(self, timeout=None) -> int:
        if self._killed.is_set():
            self.returncode = 1
            return 1
        import time as _t

        _t.sleep(timeout if timeout else 0.2)
        if self._killed.is_set():
            self.returncode = 1
            return 1
        raise _sp.TimeoutExpired(cmd="opencode", timeout=timeout)

    def kill(self) -> None:
        self._killed.set()


def test_silence_watchdog_kills(tmp_path, monkeypatch):
    """Фейк молчит дольше idle_s (1 c) — прерывается с 'тишина N c'."""
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _SilentProc())
    r = OpencodeRunner(idle_s=1, timeout_s=30)
    with pytest.raises(RuntimeError, match=r"тишина \d+ c"):
        r.start("промпт", str(tmp_path), log=str(tmp_path / "o.log"))
    # Изоляция заодно: каталог создан даже при тишине.
    assert (tmp_path / ".agent" / "hubhome").is_dir()


def test_silence_message_has_secs(tmp_path, monkeypatch):
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _SilentProc())
    r = OpencodeRunner(idle_s=1, timeout_s=30)
    try:
        r.start("промпт", str(tmp_path), log=str(tmp_path / "o2.log"))
        assert False, "должен упасть тишиной"
    except RuntimeError as e:
        assert "opencode: тишина" in str(e)
        secs = cyc._silence_secs(e)
        assert secs is not None and secs >= 1, str(e)


def test_json_resets_watchdog(tmp_path, monkeypatch):
    """Пульс JSON-событий сбрасывает сторож: паузы < idle_s, общее время > idle_s.

    Фейк живёт ~2,4 с при idle_s=1: если сброс last_event сломан,
    сторож убьёт процесс на первой секунде (проверено мутацией).
    """
    gap, beats, idle = 0.6, 4, 1
    done = threading.Event()

    def _gen():
        yield json.dumps({"sessionID": "ses-ok"}) + "\n"
        for _ in range(beats):
            time.sleep(gap)
            yield json.dumps({"type": "beat"}) + "\n"

    class _Talkative:
        def __init__(self) -> None:
            self.stdout = self._wrap()
            self.stderr = _FakeErr()
            self.returncode = 0
            self.pid = 2_000_000_002
            self.killed = False

        def _wrap(self):
            try:
                for line in _gen():
                    yield line
            finally:
                done.set()

        def wait(self, timeout=None) -> int:
            if self.killed:
                self.returncode = 1
                return 1
            if done.is_set():
                return 0
            time.sleep(timeout if timeout else 0.2)
            if done.is_set():
                return 0
            raise _sp.TimeoutExpired(cmd="opencode", timeout=timeout)

        def kill(self) -> None:
            self.killed = True

    box: list = []
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: box.append(_Talkative()) or box[-1])
    got = OpencodeRunner(idle_s=idle, timeout_s=30).start(
        "промпт", str(tmp_path), log=str(tmp_path / "ok.log"))
    assert got == "ses-ok"
    assert box and not box[0].killed


def test_children_inhibit_watchdog(tmp_path, monkeypatch):
    """Есть дочерние процессы — тишина ~1,8 с при idle_s=1 объяснена, живём.

    Фейк висит дольше idle_s (wait бросает TimeoutExpired до конца),
    поэтому ветка `_has_children` реально достигается: мутация
    `if not _has_children(pid)` -> `if True` валит тест.
    """
    import hub.pipeline.runners as _rm

    silence = 1.8
    done = threading.Event()

    def _gen():
        yield json.dumps({"sessionID": "ses-kid"}) + "\n"
        time.sleep(silence)
        yield json.dumps({"type": "done"}) + "\n"

    class _WithKid:
        def __init__(self) -> None:
            self.stdout = self._wrap()
            self.stderr = _FakeErr()
            self.returncode = 0
            self.pid = 1
            self.killed = False

        def _wrap(self):
            try:
                for line in _gen():
                    yield line
            finally:
                done.set()

        def wait(self, timeout=None) -> int:
            if self.killed:
                self.returncode = 1
                return 1
            if done.is_set():
                return 0
            time.sleep(timeout if timeout else 0.2)
            if done.is_set():
                return 0
            raise _sp.TimeoutExpired(cmd="opencode", timeout=timeout)

        def kill(self) -> None:
            self.killed = True

    proc_box: list = []

    def _mk(*a, **k):
        p = _WithKid()
        proc_box.append(p)
        return p

    monkeypatch.setattr(_sp, "Popen", _mk)
    monkeypatch.setattr(_rm, "_has_children", lambda pid: True)
    got = OpencodeRunner(idle_s=1, timeout_s=30).start(
        "промпт", str(tmp_path), log=str(tmp_path / "kid.log"))
    assert got == "ses-kid"
    assert proc_box and not proc_box[0].killed


def test_has_children_real():
    """Настоящая проверка /proc: без моков, на живых процессах."""
    import hub.pipeline.runners as _rm

    assert _rm._has_children(None) is False
    assert _rm._has_children(2_000_000_007) is False
    assert _rm._has_children("мусор") is False
    solo = _sp.Popen(["sleep", "30"])
    try:
        assert _rm._has_children(solo.pid) is False
    finally:
        solo.kill()
        solo.wait()
    shell = _sp.Popen(["sh", "-c", "sleep 30 & wait"], start_new_session=True)
    try:
        ok = False
        for _ in range(50):
            if _rm._has_children(shell.pid):
                ok = True
                break
            time.sleep(0.1)
        assert ok, "у sh со спящим ребёнком должны быть дети"
    finally:
        try:
            os.killpg(shell.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        shell.wait()


class _FreeSilent:
    tool = "opencode"
    model = "opencode/muse-spark-1.3-contributor-free"
    timeout_s = 30
    idle_s = 1

    def start(self, prompt, cwd, log=None, on_session=None):
        raise RuntimeError("opencode: тишина 1 c (лог x)")

    def resume(self, sid, prompt, cwd, log=None, on_session=None):
        raise RuntimeError("opencode: тишина 1 c (лог x)")


class _GoOk:
    tool = "opencode"
    model = "opencode-go/muse-spark-1.3-contributor"

    def __init__(self) -> None:
        self.timeout_s = 30
        self.idle_s = 1

    def start(self, prompt, cwd, log=None, on_session=None):
        _commit_ok(Path(cwd))
        return "go-1"

    def resume(self, sid, prompt, cwd, log=None, on_session=None):
        _commit_ok(Path(cwd), text="3\n")
        return sid or "go-1"


class _RevApprove:
    tool = "opencode"
    model = "muse"

    def start(self, prompt, cwd, log=None, on_session=None):
        import re as _re

        m = _re.search(r"review_r(\d+)\.json", prompt or "")
        rnd = int(m.group(1)) if m else 1
        p = Path(cwd) / ".agent" / f"review_r{rnd}_muse.json"
        p.write_text(json.dumps({"verdict": "approve", "findings": []}),
                     encoding="utf-8")
        return "rev-1"

    def resume(self, sid, prompt, cwd, log=None, on_session=None):
        return sid


def test_musefree_fallback_to_muse(tmp_path, monkeypatch):
    """Тишина musefree → та же задача на muse, executor обновлён, событие в журнале."""
    import hub.pipeline.runners as _rm

    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TFREE", executor="musefree")
    monkeypatch.setattr(_rm, "make_runner", lambda name, timeout_s=0, idle_s=0: _GoOk())
    runners = {"executor": _FreeSilent(), "reviewers": {"muse": _RevApprove()}}
    got = cyc.run_task(Store(), proj, "TFREE", runners, rounds=1)
    assert got == "ready", Store().get_task("TFREE")
    assert Store().get_task("TFREE")["executor"] == "muse"
    events = [e for e in Store().events_since(0) if e["task_id"] == "TFREE"]
    texts = [e.get("payload_json", "") for e in events]
    assert any("бесплатный Spark молчал" in t and "Spark Go" in t for t in texts), texts


def test_no_fallback_for_muse(tmp_path):
    """Тишина платного muse — честный failed без перевода."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TGO", executor="muse")

    class _GoSilent:
        tool = "opencode"
        model = "opencode-go/muse-spark-1.3-contributor"

        def start(self, prompt, cwd, log=None, on_session=None):
            raise RuntimeError("opencode: тишина 5 c (лог x)")

        def resume(self, sid, prompt, cwd, log=None, on_session=None):
            raise RuntimeError("opencode: тишина 5 c (лог x)")

    runners = {"executor": _GoSilent(), "reviewers": {}}
    got = cyc.run_task(Store(), proj, "TGO", runners, rounds=1)
    assert got == "failed"
    assert Store().get_task("TGO")["executor"] == "muse"


def test_silence_secs_strict():
    """Слово «тишина» без счётчика N c — не сработка сторожа."""
    assert cyc._silence_secs(RuntimeError("opencode: тишина 75 c (лог x)")) == 75
    assert cyc._silence_secs(RuntimeError("промпт про тишину без счётчика")) is None
    assert cyc._silence_secs(RuntimeError("opencode: таймаут 10 c")) is None
    assert cyc._silence_secs(RuntimeError("другая ошибка")) is None
    assert cyc._silence_sid(
        RuntimeError("opencode: тишина 60 c sid=ses-1 (лог x)")) == "ses-1"
    assert cyc._silence_sid(
        RuntimeError("opencode: тишина 60 c (лог x)")) is None


def test_is_musefree_only_musefree():
    """Fallback только для musefree: mimofree не уводится на muse."""
    from hub.pipeline.runners import MODELS as _MODELS

    class _St:
        def __init__(self, exe: str) -> None:
            self._exe = exe

        def get_task(self, tid: str):
            return {"executor": self._exe}

    class _Ex:
        def __init__(self, model: str) -> None:
            self.model = model

    musefree_id = _MODELS["musefree"][0]
    assert cyc._is_musefree_task(_St("musefree"), "T", _Ex("что угодно"), {}) is True
    assert cyc._is_musefree_task(_St("muse"), "T", _Ex(musefree_id), {}) is True
    assert cyc._is_musefree_task(
        _St("muse"), "T", _Ex("opencode/mimo-v2.6-flash-free"), {}) is False
    assert cyc._is_musefree_task(
        _St("muse"), "T", _Ex("opencode-go/muse-spark-1.3-contributor"), {}) is False


def test_silence_error_carries_sid(tmp_path, monkeypatch):
    """Sid из stdout до тишины — в тексте ошибки для resume той же сессии."""
    def _gen():
        yield json.dumps({"sessionID": "ses-part"}) + "\n"

    class _PartSilent:
        def __init__(self) -> None:
            self.stdout = _gen()
            self.stderr = _FakeErr()
            self.returncode = None
            self.pid = 2_000_000_009
            self._killed = threading.Event()

        def wait(self, timeout=None) -> int:
            if self._killed.is_set():
                self.returncode = 1
                return 1
            time.sleep(timeout if timeout else 0.2)
            if self._killed.is_set():
                self.returncode = 1
                return 1
            raise _sp.TimeoutExpired(cmd="opencode", timeout=timeout)

        def kill(self) -> None:
            self._killed.set()

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _PartSilent())
    with pytest.raises(RuntimeError) as ei:
        OpencodeRunner(idle_s=1, timeout_s=30).start(
            "промпт", str(tmp_path), log=str(tmp_path / "part.log"))
    assert "тишина" in str(ei.value)
    assert "ses-part" in str(ei.value)
    assert cyc._silence_sid(ei.value) == "ses-part"


def test_musefree_fallback_resumes_same_session(tmp_path, monkeypatch):
    """Тишина в круге 1 с известным sid → resume той же сессии на muse."""
    import hub.pipeline.runners as _rm

    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base, tid="TSID", executor="musefree")
    calls: dict = {}

    class _GoSid:
        tool = "opencode"
        model = "opencode-go/muse-spark-1.3-contributor"
        timeout_s = 30
        idle_s = 1

        def start(self, prompt, cwd, log=None, on_session=None):
            calls["start"] = True
            _commit_ok(Path(cwd))
            return "go-new"

        def resume(self, sid, prompt, cwd, log=None, on_session=None):
            calls["resume_sid"] = sid
            _commit_ok(Path(cwd))
            return sid

    class _FreeSid:
        tool = "opencode"
        model = "opencode/muse-spark-1.3-contributor-free"
        timeout_s = 30
        idle_s = 1

        def start(self, prompt, cwd, log=None, on_session=None):
            raise RuntimeError("opencode: тишина 60 c sid=ses-free (лог x)")

        def resume(self, sid, prompt, cwd, log=None, on_session=None):
            raise RuntimeError("opencode: тишина 60 c (лог x)")

    monkeypatch.setattr(_rm, "make_runner",
                        lambda name, timeout_s=0, idle_s=0: _GoSid())
    runners = {"executor": _FreeSid(), "reviewers": {"muse": _RevApprove()}}
    got = cyc.run_task(Store(), proj, "TSID", runners, rounds=1)
    assert got == "ready", Store().get_task("TSID")
    assert calls.get("resume_sid") == "ses-free", calls
    assert "start" not in calls
    assert Store().get_task("TSID")["executor"] == "muse"


def test_apply_idle_from_project():
    """Порог тишины из конфига расходится по раннерам."""

    class _R:
        idle_s = 900

    class _P:
        idle_s = 5

    r = _R()
    cyc._apply_idle_from_project(_P(), [r])
    assert r.idle_s == 5
    cyc._apply_idle_from_project(_P(), [object()])


def test_timeout_writes_partial_log(tmp_path, monkeypatch):
    """Ветка таймаута пишет частичный stdout в лог, как ветка тишины."""
    def _gen():
        yield json.dumps({"sessionID": "ses-t"}) + "\n"
        yield "часть-вывода\n"

    class _Hang:
        def __init__(self) -> None:
            self.stdout = _gen()
            self.stderr = _FakeErr()
            self.returncode = None
            self.pid = 2_000_000_011
            self._killed = threading.Event()

        def wait(self, timeout=None) -> int:
            if self._killed.is_set():
                self.returncode = 1
                return 1
            time.sleep(timeout if timeout else 0.2)
            if self._killed.is_set():
                self.returncode = 1
                return 1
            raise _sp.TimeoutExpired(cmd="opencode", timeout=timeout)

        def kill(self) -> None:
            self._killed.set()

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _Hang())
    log = str(tmp_path / "hang.log")
    with pytest.raises(RuntimeError, match="таймаут"):
        OpencodeRunner(idle_s=0, timeout_s=1).start("промпт", str(tmp_path), log=log)
    assert "часть-вывода" in Path(log).read_text(encoding="utf-8")


def test_agent_env_inside_worktree(tmp_path, monkeypatch):
    """Env сессии содержит AGENT_HUB_HOME внутри worktree; hub env не меняется."""
    before = os.environ.get("AGENT_HUB_HOME")
    wt = tmp_path / "wt-env"
    wt.mkdir()
    seen: dict = {}

    class _Quick:
        def __init__(self, *a, **k) -> None:
            seen.update(k)
            self.pid = 2_000_000_003
            self.returncode = 0
            self.stderr = _FakeErr()

            def _g():
                yield json.dumps({"sessionID": "ses-env"}) + "\n"

            self.stdout = _g()

        def wait(self, timeout=None) -> int:
            return 0

        def kill(self) -> None:
            pass

    monkeypatch.setattr(_sp, "Popen", _Quick)
    got = OpencodeRunner(idle_s=60).start("промпт", str(wt),
                                          log=str(wt / ".agent" / "o.log"))
    assert got == "ses-env"
    env = seen.get("env") or {}
    hubhome = env.get("AGENT_HUB_HOME", "")
    assert hubhome == str(wt / ".agent" / "hubhome"), hubhome
    assert Path(hubhome).is_dir()
    assert os.environ.get("AGENT_HUB_HOME") == before


def test_agent_store_isolated(tmp_path, monkeypatch):
    """Store() в фейковом агенте не трогает базу теста-«боевой»."""
    wt = tmp_path / "wt-iso"
    wt.mkdir()
    env = agent_env(str(wt))
    hubhome = env["AGENT_HUB_HOME"]
    assert Path(hubhome).is_dir()
    # Боевая база теста уже есть (задача TBASE).
    Store().upsert_task(id="TBASE", stage="queued")
    # Агент пишет со своим AGENT_HUB_HOME.
    monkeypatch.setenv("AGENT_HUB_HOME", hubhome)
    from hub.store import Store as _S

    _S().upsert_task(id="AGENT-ONLY", stage="queued")
    assert _S().get_task("AGENT-ONLY") is not None
    # Боевая база теста — без фантома.
    monkeypatch.setenv("AGENT_HUB_HOME",
                       str(tmp_path / ".local/share/agent-hub"))
    assert Store().get_task("AGENT-ONLY") is None
    assert Store().get_task("TBASE") is not None
    # Файл агента — внутри worktree.
    assert str(Path(hubhome) / "hub.db") == str(_S(path=Path(hubhome) / "hub.db").path)


def test_agy_env_isolated(tmp_path, monkeypatch):
    """AgyRunner тоже получает AGENT_HUB_HOME внутри worktree."""
    wt = tmp_path / "wt-agy"
    wt.mkdir()
    seen: dict = {}

    import types as _t

    def _fake_run(*a, **k):
        seen.update(k)
        return _t.SimpleNamespace(stdout='{"conversation_id": "c-iso"}\n',
                                  stderr="", returncode=0)

    monkeypatch.setattr(_sp, "run", _fake_run)
    got = AgyRunner().start("промпт", str(wt), log=str(wt / "agy.log"))
    assert got == "c-iso"
    assert seen.get("env", {}).get("AGENT_HUB_HOME") == str(wt / ".agent" / "hubhome")


def test_idle_s_config_default_and_parse(tmp_path):
    from hub.config import _from_dict

    assert _from_dict({}).idle_s == 900
    assert _from_dict({"idle_s": 123}).idle_s == 123
    assert _from_dict({"idle_s": "7"}).idle_s == 7
    assert _from_dict({"idle_s": "мусор"}).idle_s == 900


def test_continue_dirty_wip(tmp_path, capsys):
    """Continue на грязном worktree: wip-коммит без .agent.prev_* и не падает."""
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
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
    wt = tmp_path / "wt-TW"
    _git(root, "worktree", "add", str(wt), "-b", "agent/TW", base)
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    card = tmp_path / "TW.md"
    card.write_text(CARD, encoding="utf-8")
    Store().upsert_task(id="TW", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TW", worktree=str(wt),
                        base_sha=base, stage="failed", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="x")
    # Грязная наработка + мусор, который нельзя коммитить.
    (wt / "sub" / "a.txt").write_text("наработка\n", encoding="utf-8")
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "executor_r1.log").write_text("лог\n", encoding="utf-8")
    prev_old = wt / ".agent.prev_111"
    prev_old.mkdir(exist_ok=True)
    (prev_old / "old.log").write_text("старьё\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["continue", "TW", "--project", str(root)]) == 0
    out = capsys.readouterr().out
    assert "OK TW" in out
    # Wip-коммит один, с нужным сообщением, без .agent*.
    log = _git(wt, "log", "--oneline", "-2")
    assert "wip: наработка до continue" in _git(wt, "log", "-1", "--format=%s")
    files = _git(wt, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "sub/a.txt" in files, files
    assert not any(f.startswith(".agent") for f in files), files
    # Старый .agent уехал в prev, новый пуст.
    assert (wt / ".agent").is_dir()
    assert list(wt.glob(".agent.prev_*"))
    assert Store().get_task("TW")["stage"] == "queued"
