"""Конвейер H06: круги, repair, вердикт, бюджет, сессии. Фейковые раннеры, без сети."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from hub.config import ProjectConfig
from hub.pipeline import cycle as cyc
from hub.pipeline import prompts
from hub.pipeline.runners import MODELS
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


def _mk_project(repo: Path) -> ProjectConfig:
    return ProjectConfig(
        name="T", root=str(repo), worktrees=str(repo.parent / "wt-dir"),
        rules="docs/rules.md", python=sys.executable, test_lock="",
        work_branch="main", push="", allowed_paths=["sub/**", "docs/**", "tests/**"],
        defaults=__import__("hub.config", fromlist=["Defaults"]).Defaults(
            executor="muse", reviewers=["muse"], budget_go=0.5),
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


def _mk_task(tmp_path, repo, base, tid="T01", card_text=CARD):
    card = tmp_path / f"{tid}.md"
    card.write_text(card_text, encoding="utf-8")
    Store().upsert_task(id=tid, project="T", card_path=str(card),
                        card_hash="h", level="hard", branch=f"agent/{tid}",
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


def _round_of(prompt: str, log: str | None) -> int:
    m = re.search(r"review_r(\d+)\.json", prompt or "")
    if m:
        return int(m.group(1))
    m2 = re.search(r"reviewer_r(\d+)_", log or "")
    return int(m2.group(1)) if m2 else 1


class ExecOk:
    tool = "opencode"
    model = "muse"

    def start(self, prompt, cwd, log=None):
        assert ".agent/done.json" in prompt  # шаблон в конце промпта исполнителя
        assert "без коммита" in prompt.lower() or "Без коммита" in prompt
        _commit_ok(Path(cwd))
        return "exec-1"

    def resume(self, sid, prompt, cwd, log=None):
        _commit_ok(Path(cwd), text="3\n")
        return sid or "exec-1"


class RevApprove:
    def __init__(self, name="muse"):
        self.name = name
        self.tool = "opencode"
        self.model = name

    def start(self, prompt, cwd, log=None):
        rnd = _round_of(prompt, log)
        p = Path(cwd) / ".agent" / f"review_r{rnd}_{self.name}.json"
        p.write_text(json.dumps({"verdict": "approve", "findings": []}),
                     encoding="utf-8")
        return f"rev-{self.name}-{rnd}"

    def resume(self, sid, prompt, cwd, log=None):
        return sid


class RevScript:
    """Сценарий вердиктов по кругам: {1: (verdict, findings), 2: ...}."""

    def __init__(self, name, script):
        self.name = name
        self.script = script
        self.tool = "opencode"
        self.model = name

    def start(self, prompt, cwd, log=None):
        rnd = _round_of(prompt, log)
        v, findings = self.script.get(rnd, ("approve", []))
        p = Path(cwd) / ".agent" / f"review_r{rnd}_{self.name}.json"
        p.write_text(json.dumps({"verdict": v, "findings": findings}),
                     encoding="utf-8")
        return f"rev-{self.name}-{rnd}"

    def resume(self, sid, prompt, cwd, log=None):
        return sid


def test_models_table():
    for m in ("muse", "musefree", "mimoflash", "mimo", "mimofree",
              "deepseek", "glm", "gemini"):
        assert m in MODELS, m


def test_executor_prompt_has_template():
    text = prompts.executor_prompt("правила", CARD)
    assert ".agent/done.json" in text
    assert "без коммита" in text.lower()
    # Шаблон ≤ 15 строк.
    tmpl_lines = prompts.DONE_TEMPLATE.strip().splitlines()
    assert len(tmpl_lines) <= 15


def test_review_blind_strips_arbiter():
    card = CARD + "\n## Решения арбитра\n\nЭТАЛОН-СЕКРЕТ\n"
    full = prompts.review_prompt("r", card, "d", {}, 1, blind=False)
    assert "ЭТАЛОН-СЕКРЕТ" in full
    blinded = prompts.review_prompt("r", card, "d", {}, 1, blind=True)
    assert "ЭТАЛОН-СЕКРЕТ" not in blinded


def test_ready_one_round(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    runners = {"executor": ExecOk(), "reviewers": {"muse": RevApprove("muse")}}
    got = cyc.run_task(Store(), proj, "T01", runners, rounds=2)
    assert got == "ready"
    task = Store().get_task("T01")
    assert task["stage"] == "ready" and task["round"] == 1
    # Сессии связаны сразу, не в конце.
    roles = {r["external_id"]: r["role"] for r in Store().list_sessions("T01")}
    assert roles.get("exec-1") == "executor"
    assert any(v == "reviewer" for v in roles.values())


def test_changes_then_ready(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    ch = {"file": "sub/a.txt", "line": 1, "issue": "баг", "severity": "high"}
    rev = RevScript("muse", {1: ("changes", [ch]), 2: ("approve", [])})
    runners = {"executor": ExecOk(), "reviewers": {"muse": rev}}
    got = cyc.run_task(Store(), proj, "T01", runners, rounds=2)
    assert got == "ready"
    assert Store().get_task("T01")["round"] == 2


def test_no_commit_repair_ok(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)

    class Lazy:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            (Path(cwd) / "sub" / "a.txt").write_text("грязь\n", encoding="utf-8")
            return "exec-1"  # без коммита и done.json

        def resume(self, sid, prompt, cwd, log=None):
            assert "git status" in prompt  # REPAIR_PROMPT
            _commit_ok(Path(cwd))
            return sid

    runners = {"executor": Lazy(), "reviewers": {"muse": RevApprove("muse")}}
    assert cyc.run_task(Store(), proj, "T01", runners, rounds=2) == "ready"


def test_repair_twice_failed(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)

    class Never:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            return "exec-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid  # repair тоже без коммита

    runners = {"executor": Never(), "reviewers": {"muse": RevApprove("muse")}}
    assert cyc.run_task(Store(), proj, "T01", runners, rounds=2) == "failed"
    assert Store().get_task("T01")["stage"] == "failed"


def test_dispute_without_file_is_changes(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    long_body = "о" * 60
    # dispute без file:line оба круга → changes,changes → круги кончились → arbiter на 2 круге.
    rev = RevScript("muse", {1: ("dispute", [{"issue": long_body}]),
                             2: ("dispute", [{"issue": long_body}])})
    runners = {"executor": ExecOk(), "reviewers": {"muse": rev}}
    got = cyc.run_task(Store(), proj, "T01", runners, rounds=2)
    assert got == "arbiter"
    assert Store().get_task("T01")["round"] == 2  # дошёл до 2-го, не arbiter сразу


def test_valid_dispute_arbiter_at_once(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    rev = RevScript("muse", {1: ("dispute", [{"file": "sub/a.txt", "line": 5,
                                             "issue": "о" * 60}])})
    runners = {"executor": ExecOk(), "reviewers": {"muse": rev}}
    assert cyc.run_task(Store(), proj, "T01", runners, rounds=2) == "arbiter"
    assert Store().get_task("T01")["round"] == 1


def test_nobody_answered_arbiter(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    runners = {"executor": ExecOk(), "reviewers": {}}
    assert cyc.run_task(Store(), proj, "T01", runners, rounds=2) == "arbiter"


def test_forbidden_project_failed(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)

    class Bad:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            p = Path(cwd) / "other" / "x.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n", encoding="utf-8")
            _git(cwd, "add", "other/x.txt")
            _git(cwd, "commit", "-m", "bad")
            head = _git(cwd, "rev-parse", "HEAD")
            (Path(cwd) / ".agent").mkdir(exist_ok=True)
            (Path(cwd) / ".agent" / "done.json").write_text(json.dumps({
                "commit": head, "files": ["other/x.txt"],
                "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
                encoding="utf-8")
            return "exec-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    runners = {"executor": Bad(), "reviewers": {"muse": RevApprove("muse")}}
    assert cyc.run_task(Store(), proj, "T01", runners, rounds=2) == "failed"


def test_budget_stopped_and_question(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    runners = {"executor": ExecOk(), "reviewers": {"muse": RevApprove("muse")}}
    got = cyc.run_task(Store(), proj, "T01", runners, rounds=2,
                       cost_fn=lambda s, t: (10.0, 0.0))
    assert got == "stopped"
    assert Store().get_task("T01")["stage"] == "stopped"
    import sqlite3 as _sq

    con = _sq.connect(str(Store().path))
    try:
        rows = con.execute("SELECT text,status FROM question WHERE task_id='T01'").fetchall()
    finally:
        con.close()
    assert rows and any("родлить" in r[0] or "Продлить" in r[0] for r in rows)


def test_session_visible_during_step(tmp_path):
    """Во время фейкового шага ревью snapshot.build видит сессию исполнителя."""
    from hub.read import snapshot as snap
    from hub import time as ht

    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    store = Store()
    seen = {}

    class CheckingRev:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            s = snap.build(store, ht.now_ms(), opencode_db=None, proc_root="/proc")
            ids = [x.external_id for t in s.tasks if t.id == "T01" for x in t.sessions]
            seen["ids"] = ids
            rnd = _round_of(prompt, log)
            p = Path(cwd) / ".agent" / f"review_r{rnd}_muse.json"
            p.write_text(json.dumps({"verdict": "approve", "findings": []}),
                         encoding="utf-8")
            return "rev-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    runners = {"executor": ExecOk(), "reviewers": {"muse": CheckingRev()}}
    assert cyc.run_task(store, proj, "T01", runners, rounds=2) == "ready"
    assert "exec-1" in seen.get("ids", [])


def test_stage_events_on_transitions(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    runners = {"executor": ExecOk(), "reviewers": {"muse": RevApprove("muse")}}
    cyc.run_task(Store(), proj, "T01", runners, rounds=2)
    kinds = [e["kind"] for e in Store().events_since(0) if e["task_id"] == "T01"]
    assert "stage" in kinds
    assert kinds.count("stage") >= 3  # каждый переход пишется
