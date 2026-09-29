"""H12: сторож тишины, изоляция hub.db, wip при continue. Фейки, без сети."""

from __future__ import annotations

import json
import os
import subprocess as _sp
import sys
import threading
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
    """Фейк молчит дольше idle_s (1 c) — прерывается с 'тишина'."""
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _SilentProc())
    r = OpencodeRunner(idle_s=1, timeout_s=30)
    with pytest.raises(RuntimeError, match="тишина 1 c"):
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
        assert " c " in str(e) or " c(" in str(e)


def test_json_resets_watchdog(tmp_path, monkeypatch):
    """Говорящий процесс (JSON сразу) — сторож не срабатывает."""
    def _gen2():
        yield json.dumps({"sessionID": "ses-ok"}) + "\n"

    class _Quick:
        def __init__(self) -> None:
            self.stdout = _gen2()
            self.stderr = _FakeErr()
            self.returncode = 0
            self.pid = 2_000_000_002

        def wait(self, timeout=None) -> int:
            return 0

        def kill(self) -> None:
            pass

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _Quick())
    got = OpencodeRunner(idle_s=1, timeout_s=10).start(
        "промпт", str(tmp_path), log=str(tmp_path / "ok.log"))
    assert got == "ses-ok"


def test_children_inhibit_watchdog(tmp_path, monkeypatch):
    """Есть дочерние процессы — тишина объяснена, watchdog не убивает."""
    import hub.pipeline.runners as _rm

    def _gen():
        yield json.dumps({"sessionID": "ses-kid"}) + "\n"
        # Дальше молчим, но дети есть — процесс должен дожить до конца.
        import time as _t

        _t.sleep(1.5)
        yield json.dumps({"type": "done"}) + "\n"

    class _WithKid:
        def __init__(self) -> None:
            self.stdout = _gen()
            self.stderr = _FakeErr()
            self.returncode = 0
            self.pid = 1  # init: дети точно есть
            self.killed = False

        def wait(self, timeout=None) -> int:
            import time as _t

            _t.sleep(timeout if timeout else 0.2)
            return 0

        def kill(self) -> None:
            self.killed = True

    proc_box: list = []
    orig = _WithKid

    def _mk(*a, **k):
        p = orig()
        proc_box.append(p)
        return p

    monkeypatch.setattr(_sp, "Popen", _mk)
    # pid=1 имеет детей на Linux, но для надёжности подменяем проверку.
    monkeypatch.setattr(_rm, "_has_children", lambda pid: True)
    got = OpencodeRunner(idle_s=1, timeout_s=10).start(
        "промпт", str(tmp_path), log=str(tmp_path / "kid.log"))
    assert got == "ses-kid"
    assert proc_box and not proc_box[0].killed


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
