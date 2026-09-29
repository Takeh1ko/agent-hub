"""H06-фикс: ловушки ревью — approve при красных воротах, brace-глобы,
work_branch, continue, owner_command once, модели громко, замок, проект."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from hub.cli import main
from hub.config import ProjectConfig, Defaults
from hub.pipeline import cycle as cyc
from hub.pipeline.common import card_globs, card_network_is_playerok, parse_level
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
    return repo, _git(repo, "rev-parse", "HEAD")


def _mk_project(repo: Path, allowed=None) -> ProjectConfig:
    return ProjectConfig(
        name="T", root=str(repo), worktrees=str(repo.parent / "wt-dir"),
        rules="docs/rules.md", python=sys.executable, test_lock="",
        work_branch="main", push="",
        allowed_paths=allowed or ["sub/**", "docs/**", "tests/**"],
        defaults=Defaults(executor="muse", reviewers=["muse"], budget_go=0.5),
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


def _round_of(prompt, log) -> int:
    m = re.search(r"review_r(\d+)\.json", prompt or "")
    if m:
        return int(m.group(1))
    m2 = re.search(r"reviewer_r(\d+)_", log or "")
    return int(m2.group(1)) if m2 else 1


class CountingExec:
    """Коммитит новый файл при каждом вызове (start и resume)."""

    tool = "opencode"
    model = "muse"

    def __init__(self):
        self.n = 0
        self.tag = abs(id(self)) % 1000000

    def _do(self, cwd):
        self.n += 1
        rel = f"sub/w{self.tag}_{self.n}.txt"
        p = Path(cwd) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{self.n}\n", encoding="utf-8")
        _git(cwd, "add", rel)
        _git(cwd, "commit", "-m", rel)
        head = _git(cwd, "rev-parse", "HEAD")
        (Path(cwd) / ".agent").mkdir(exist_ok=True)
        (Path(cwd) / ".agent" / "done.json").write_text(json.dumps({
            "commit": head, "files": [rel],
            "tests": {"cmd": sys.executable + " -m pytest -q",
                      "ok": True, "tail": "ok"},
            "notes": ""}), encoding="utf-8")
        return f"exec-{self.n}"

    def start(self, prompt, cwd, log=None):
        return self._do(cwd)

    def resume(self, sid, prompt, cwd, log=None):
        self._do(cwd)
        return sid or "exec-1"


class RevApprove:
    def __init__(self, name="muse"):
        self.name = name
        self.tool = "opencode"
        self.model = name

    def start(self, prompt, cwd, log=None):
        rnd = _round_of(prompt, log)
        (Path(cwd) / ".agent" / f"review_r{rnd}_{self.name}.json").write_text(
            json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8")
        return f"rev-{self.name}-{rnd}"

    def resume(self, sid, prompt, cwd, log=None):
        return sid


# --- high: approve при красных воротах ---

def test_approve_red_tests_not_ready(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)

    class RedExec:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            p = Path(cwd) / "sub" / "test_red.py"
            p.write_text("def test_red():\n    assert False\n", encoding="utf-8")
            _git(cwd, "add", "sub/test_red.py")
            _git(cwd, "commit", "-m", "red")
            head = _git(cwd, "rev-parse", "HEAD")
            (Path(cwd) / ".agent").mkdir(exist_ok=True)
            (Path(cwd) / ".agent" / "done.json").write_text(json.dumps({
                "commit": head, "files": ["sub/test_red.py"],
                "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
                encoding="utf-8")
            return "exec-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": RedExec(), "reviewers": {"muse": RevApprove()}},
                       rounds=2)
    assert got != "ready"
    assert got == "arbiter"


def test_approve_card_only_forbidden_not_ready(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    n = {"i": 0}

    class CardBreaker:
        tool = "opencode"
        model = "muse"

        def _do(self, cwd):
            n["i"] += 1
            (Path(cwd) / "docs").mkdir(exist_ok=True)
            (Path(cwd) / "docs" / "extra.txt").write_text("x\n", encoding="utf-8")
            rel = f"sub/f{n['i']}.txt"
            (Path(cwd) / rel).write_text("y\n", encoding="utf-8")
            _git(cwd, "add", "docs/extra.txt", rel)
            _git(cwd, "commit", "-m", "m")
            head = _git(cwd, "rev-parse", "HEAD")
            (Path(cwd) / ".agent").mkdir(exist_ok=True)
            (Path(cwd) / ".agent" / "done.json").write_text(json.dumps({
                "commit": head, "files": ["docs/extra.txt", rel],
                "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
                encoding="utf-8")
            return "exec-1"

        def start(self, prompt, cwd, log=None):
            return self._do(cwd)

        def resume(self, sid, prompt, cwd, log=None):
            self._do(cwd)
            return sid

    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": CardBreaker(), "reviewers": {"muse": RevApprove()}},
                       rounds=2)
    assert got != "ready"
    assert Store().get_task("T01")["round"] == 2


# --- high: brace-глобы ---

def test_brace_globs_expand():
    card = ("**Можно менять.** `hub/pipeline/**`, "
            "`hub/commands/{start,stop}.py`\n")
    globs = card_globs(card)
    assert "hub/commands/start.py" in globs
    assert "hub/commands/stop.py" in globs
    assert not any("{" in g for g in globs)


def test_brace_globs_cycle_ready(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    card = CARD.replace("`sub/**`", "`sub/{a,b}.txt`")
    _mk_task(tmp_path, repo, base, card_text=card)

    class Both:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            for rel in ("sub/a.txt", "sub/b.txt"):
                (Path(cwd) / rel).write_text("x\n", encoding="utf-8")
                _git(cwd, "add", rel)
            _git(cwd, "commit", "-m", "both")
            head = _git(cwd, "rev-parse", "HEAD")
            (Path(cwd) / ".agent").mkdir(exist_ok=True)
            (Path(cwd) / ".agent" / "done.json").write_text(json.dumps({
                "commit": head, "files": ["sub/a.txt", "sub/b.txt"],
                "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
                encoding="utf-8")
            return "exec-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": Both(), "reviewers": {"muse": RevApprove()}},
                       rounds=2)
    assert got == "ready"


# --- high: merge только на work_branch ---

def test_merge_refuses_not_on_work_branch(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "branch", "market", base)
    wt = tmp_path / "wt"
    _git(root, "worktree", "add", str(wt), "-b", "agent/TM", base)
    (wt / "sub" / "a.txt").write_text("2\n", encoding="utf-8")
    _git(wt, "add", "sub/a.txt")
    _git(wt, "commit", "-m", "a")
    head = _git(wt, "rev-parse", "HEAD")
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "done.json").write_text(json.dumps({
        "commit": head, "files": ["sub/a.txt"],
        "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
        encoding="utf-8")
    card = tmp_path / "TM.md"
    card.write_text(CARD, encoding="utf-8")
    Store().upsert_task(id="TM", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TM", worktree=str(wt),
                        base_sha=base, stage="ready", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="")
    proj = ProjectConfig(name="T", root=str(root), rules="docs/rules.md",
                         python=sys.executable, test_lock="", work_branch="market",
                         push="", allowed_paths=["sub/**"],
                         defaults=Defaults(executor="muse", reviewers=["muse"]))
    from hub.pipeline.merge import merge_task

    assert _git(root, "branch", "--show-current") == "main"
    ok, msg = merge_task(Store(), proj, "TM")
    assert not ok and "not-on-work-branch" in msg
    assert Store().get_task("TM")["stage"] == "ready"
    _git(root, "checkout", "market")
    ok2, msg2 = merge_task(Store(), proj, "TM")
    assert ok2, msg2


def test_merge_unknown_file_refused(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    base = _git(root, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(root, "worktree", "add", str(wt), "-b", "agent/TU", base)
    (wt / "sub" / "a.txt").write_text("2\n", encoding="utf-8")
    _git(wt, "add", "sub/a.txt")
    _git(wt, "commit", "-m", "a")
    head = _git(wt, "rev-parse", "HEAD")
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "done.json").write_text(json.dumps({
        "commit": head, "files": ["sub/a.txt", "ghost.txt"],
        "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
        encoding="utf-8")
    card = tmp_path / "TU.md"
    card.write_text(CARD, encoding="utf-8")
    Store().upsert_task(id="TU", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TU", worktree=str(wt),
                        base_sha=base, stage="ready", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="")
    proj = ProjectConfig(name="T", root=str(root), rules="docs/rules.md",
                         python=sys.executable, test_lock="", work_branch="main",
                         push="", allowed_paths=["sub/**"],
                         defaults=Defaults(executor="muse", reviewers=["muse"]))
    from hub.pipeline.merge import merge_task

    ok, msg = merge_task(Store(), proj, "TU")
    assert not ok and "unknown-file" in msg


# --- high: continue + relaxed preflight ---

def test_continue_then_run_ok(tmp_path, capsys):
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
    wt = tmp_path / "wt-TC"
    _git(root, "worktree", "add", str(wt), "-b", "agent/TC", base)
    card = tmp_path / "TC.md"
    card.write_text(CARD, encoding="utf-8")
    Store().upsert_task(id="TC", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TC", worktree=str(wt),
                        base_sha=base, stage="queued", round=0, executor="muse",
                        reviewers_json='["muse"]', stage_reason="")
    proj = ProjectConfig(name="T", root=str(root), rules="docs/rules.md",
                         python=sys.executable, test_lock="", work_branch="main",
                         push="", allowed_paths=["sub/**"],
                         defaults=Defaults(executor="muse", reviewers=["muse"]))
    runners = {"executor": CountingExec(), "reviewers": {"muse": RevApprove()}}
    assert cyc.run_task(Store(), proj, "TC", runners, rounds=2) == "ready"
    assert main(["continue", "TC", "--project", str(root)]) == 0
    capsys.readouterr()
    assert Store().get_task("TC")["stage"] == "queued"
    runners2 = {"executor": CountingExec(), "reviewers": {"muse": RevApprove()}}
    got = cyc.run_task(Store(), proj, "TC", runners2, rounds=2)
    assert got == "ready", Store().get_task("TC")


# --- high: owner_command ровно один раз (формат H05: id в event.task_id) ---

def test_owner_command_once(tmp_path):
    from hub.commands.queue import _owner_commands

    repo, base = _mk_repo(tmp_path)
    Store().upsert_task(id="TQ", project="T", card_path="x", branch="agent/TQ",
                        worktree=str(repo), base_sha=base, stage="exec r1")
    (repo / ".agent").mkdir(exist_ok=True)
    # Реальный payload H05: id только в колонке event.task_id.
    Store().add_event("TQ", "owner_command", {"action": "stop"})
    _owner_commands(Store())
    assert Store().get_task("TQ")["stage"] == "stopped"
    # Как после continue: снова queued — повтор не должен останавливать.
    Store().upsert_task(id="TQ", stage="queued", stage_reason="continue")
    _owner_commands(Store())
    assert Store().get_task("TQ")["stage"] == "queued"
    # Старый формат (id в payload) тоже работает.
    Store().upsert_task(id="TQ", stage="exec r1")
    Store().add_event("OTHER", "owner_command", {"cmd": "stop", "task_id": "TQ"})
    _owner_commands(Store())
    assert Store().get_task("TQ")["stage"] == "stopped"


# --- medium: модели громко, stop финал, rtool, бюджет ---

def test_start_rejects_unknown_model(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "spec.md").write_text("с\n", encoding="utf-8")
    (root / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/spec.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n",
        encoding="utf-8")
    card = tmp_path / "TZ.md"
    card.write_text(CARD, encoding="utf-8")
    assert main(["start", str(card), "--project", str(root),
                 "--reviewers", "bogus"]) == 1
    assert "неизвестная модель" in capsys.readouterr().out
    assert Store().get_task("TZ") is None


def test_queue_unknown_executor_failed(tmp_path, capsys):
    from hub.commands.queue import _run_one

    repo, base = _mk_repo(tmp_path)
    (repo / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{repo}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n",
        encoding="utf-8")
    _mk_task(tmp_path, repo, base, tid="TE")
    Store().upsert_task(id="TE", executor="bogus-model")
    assert _run_one("TE", None) == "failed"
    assert "bogus-model" in (Store().get_task("TE") or {}).get("stage_reason", "")


def test_stop_merged_keeps(tmp_path, capsys):
    repo, base = _mk_repo(tmp_path)
    Store().upsert_task(id="TMG", project="T", card_path="x", branch="agent/TMG",
                        worktree=str(repo), base_sha=base, stage="merged",
                        merged_sha="abc")
    assert main(["stop", "TMG"]) == 1
    got = Store().get_task("TMG")
    assert got["stage"] == "merged" and got["merged_sha"] == "abc"


def _review_wt(tmp_path, tid, done_files, extra_commit=None):
    """Корень + отдельный worktree: toml/rules в корне не пачкают ворота wt."""
    root = tmp_path / f"proj-{tid}"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "rules.md").write_text("# п\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    base = _git(root, "rev-parse", "HEAD")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"gemini\"]\nbudget_go = 0.5\n",
        encoding="utf-8")
    wt = tmp_path / f"wt-{tid}"
    _git(root, "worktree", "add", str(wt), "-b", f"agent/{tid}", base)
    (wt / "sub" / "a.txt").write_text("2\n", encoding="utf-8")
    _git(wt, "add", "sub/a.txt")
    if extra_commit:
        extra_commit(wt)
    _git(wt, "commit", "-m", "a")
    head = _git(wt, "rev-parse", "HEAD")
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "done.json").write_text(json.dumps({
        "commit": head, "files": done_files,
        "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
        encoding="utf-8")
    return root, wt, base, head


def test_review_links_agy_tool(tmp_path, monkeypatch):
    card = tmp_path / "TR.md"
    card.write_text(CARD, encoding="utf-8")
    root, wt, base, _ = _review_wt(tmp_path, "TR", ["sub/a.txt"])
    Store().upsert_task(id="TR", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TR", worktree=str(wt),
                        base_sha=base, stage="review r1", round=1, executor="muse",
                        reviewers_json='["gemini"]', stage_reason="")

    class FakeAgy:
        tool = "agy"
        model = "gemini-3.8-flash-high"

        def start(self, prompt, cwd, log=None):
            m = re.search(r"review_r(\d+)\.json", prompt or "")
            rnd = m.group(1) if m else "1"
            (Path(cwd) / ".agent" / f"review_r{rnd}_gemini.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8")
            return "agy-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    import hub.pipeline.runners as _rn

    monkeypatch.setattr(_rn, "make_runner", lambda name: FakeAgy())
    assert main(["review", "TR", "--project", str(root)]) == 0
    sess = {r["external_id"]: r for r in Store().list_sessions("TR")}
    assert sess["agy-1"]["tool"] == "agy"


def test_review_panel_per_reviewer_file(tmp_path, monkeypatch):
    """HIGH: `hub review` подменяет имя файла каждому ревьюеру (как PanelReviewer).

    Честный фейк пишет файл, названный в промпте, и считает вызовы: без подмены
    оба ревьюера пишут в общий review_r1.json (гонка) и каждый получает
    обязательный resume.
    """
    card = tmp_path / "TP.md"
    card.write_text(CARD, encoding="utf-8")
    root, wt, base, _ = _review_wt(tmp_path, "TP", ["sub/a.txt"])
    Store().upsert_task(id="TP", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TP", worktree=str(wt),
                        base_sha=base, stage="review r1", round=1, executor="muse",
                        reviewers_json='["muse", "mimoflash"]', stage_reason="")

    class HonestRev:
        def __init__(self, name: str) -> None:
            self.name = name
            self.tool = "opencode"
            self.model = name
            self.starts = 0
            self.resumes = 0
            self.prompts: list[str] = []

        def start(self, prompt, cwd, log=None):
            self.starts += 1
            self.prompts.append(prompt)
            m = re.search(r"\.agent/(review_r\d+_[A-Za-z0-9_-]+\.json)", prompt or "")
            assert m, f"в промпте нет per-reviewer файла: {(prompt or '')[-200:]}"
            (Path(cwd) / ".agent" / m.group(1)).write_text(
                json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8")
            return f"rev-{self.name}"

        def resume(self, sid, prompt, cwd, log=None):
            self.resumes += 1
            return sid

    made: dict[str, HonestRev] = {}

    def _mk(name: str) -> HonestRev:
        made[name] = HonestRev(name)
        return made[name]

    import hub.pipeline.runners as _rn

    monkeypatch.setattr(_rn, "make_runner", _mk)
    assert main(["review", "TP", "--project", str(root)]) == 0
    assert set(made) == {"muse", "mimoflash"}, made
    for name, rev in made.items():
        assert rev.starts == 1, (name, rev.starts)
        assert rev.resumes == 0, (name, rev.resumes)
        assert f"review_r1_{name}.json" in rev.prompts[0]
    assert not (wt / ".agent" / "review_r1.json").exists()
    assert Store().get_task("TP")["stage"] == "ready"


def test_review_unknown_file(tmp_path, capsys):
    card = tmp_path / "TU2.md"
    card.write_text(CARD, encoding="utf-8")
    root, wt, base, _ = _review_wt(tmp_path, "TU2", ["sub/a.txt", "ghost.txt"])
    Store().upsert_task(id="TU2", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TU2", worktree=str(wt),
                        base_sha=base, stage="review r1", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="")
    assert main(["review", "TU2", "--project", str(root)]) == 1
    assert "unknown-file" in capsys.readouterr().out


def test_review_link_session_db_error_survives(tmp_path, monkeypatch):
    """LOW: sqlite3.Error в link_session не валит `hub review` (как в cycle)."""
    import sqlite3 as _sq

    card = tmp_path / "TSQ.md"
    card.write_text(CARD, encoding="utf-8")
    root, wt, base, _ = _review_wt(tmp_path, "TSQ", ["sub/a.txt"])
    Store().upsert_task(id="TSQ", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TSQ", worktree=str(wt),
                        base_sha=base, stage="review r1", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="")

    class FakeRev:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            (Path(cwd) / ".agent" / "review_r1_muse.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8")
            return "rev-1"

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    import hub.commands.review as _rv
    import hub.pipeline.runners as _rn

    class BoomStore(Store):
        def link_session(self, *a, **k):
            raise _sq.Error("база занята")

    monkeypatch.setattr(_rv, "Store", BoomStore)
    monkeypatch.setattr(_rn, "make_runner", lambda name: FakeRev())
    assert main(["review", "TSQ", "--project", str(root)]) == 0
    assert Store().get_task("TSQ")["stage"] == "ready"


def test_budget_stop_keeps_round(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": CountingExec(), "reviewers": {"muse": RevApprove()}},
                       rounds=2, cost_fn=lambda s, t: (10.0, 0.0))
    assert got == "stopped"
    assert Store().get_task("T01")["round"] == 1


def test_budget_soft_once(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    Store().upsert_task(id="T01", budget_go=1.0)
    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": CountingExec(), "reviewers": {"muse": RevApprove()}},
                       rounds=2, cost_fn=lambda s, t: (0.85, 0.0))
    assert got == "ready"
    soft = [e for e in Store().events_since(0)
            if e["task_id"] == "T01" and e["kind"] == "budget_soft"]
    assert len(soft) == 1


# --- after/extra persist, секции, partition, очередь ---

def test_after_and_extra_persist(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "spec.md").write_text("с\n", encoding="utf-8")
    (root / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/spec.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n",
        encoding="utf-8")
    Store().upsert_task(id="DEP", project="T", card_path="x", branch="agent/DEP",
                        worktree=str(root), base_sha="b", stage="queued")
    card = tmp_path / "TB.md"
    card.write_text(CARD, encoding="utf-8")
    assert main(["start", str(card), "--project", str(root), "--rounds", "3",
                 "--blind", "--after", "DEP"]) == 0
    capsys.readouterr()
    from hub.pipeline.common import read_extra

    assert read_extra(Store().get_task("TB")) == (3, "DEP", True)
    from hub.commands.queue import _eligible
    from hub.config import load_project

    proj = load_project(str(root))
    ok, _ = _eligible(Store(), Store().get_task("TB"), proj)
    assert not ok  # DEP ещё queued
    Store().upsert_task(id="DEP", stage="merged")
    ok2, _ = _eligible(Store(), Store().get_task("TB"), proj)
    assert ok2


def test_playerok_section_only():
    net_card = CARD.replace("**Сеть.** нет", "**Сеть.** playerok, локально")
    assert card_network_is_playerok(net_card)
    other = CARD.replace("**Нельзя.** сеть", "**Нельзя.** сеть, playerok упомянут")
    assert not card_network_is_playerok(other)
    assert parse_level(CARD) == "hard"
    prose = CARD.replace("**Цель.** ц", "**Цель.** ц уровня уровня сложности")
    assert parse_level(prose) == "hard"


def test_queue_partition_lock():
    from hub.commands.queue import _partition

    ts = [{"id": "A", "l": "L"}, {"id": "B", "l": "L"}, {"id": "C", "l": ""}]
    free, serial = _partition(ts, lambda t: t["l"])
    assert [t["id"] for t in free] == ["C"]
    assert [[t["id"] for t in g] for g in serial] == [["A", "B"]]


def test_queue_no_project_exit1(tmp_path, capsys):
    empty = tmp_path / "empty-wt"
    empty.mkdir()
    Store().upsert_task(id="TN", project="NOPE", card_path="x", branch="agent/TN",
                        worktree=str(empty), base_sha="b", stage="queued")
    assert main(["queue", "run", "--once"]) == 1
    assert "NO-PROJECT" in capsys.readouterr().out
    # Не висит в queued вечно: помечена failed.
    assert Store().get_task("TN")["stage"] == "failed"


# --- low: stale, retry, раннеры ---

def test_review_stale_file_not_counted(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    (repo / ".agent").mkdir(exist_ok=True)
    (repo / ".agent" / "review_r1_muse.json").write_text(
        json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8")

    class Crash:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            raise RuntimeError("упал")

        def resume(self, sid, prompt, cwd, log=None):
            return sid

    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": CountingExec(), "reviewers": {"muse": Crash()}},
                       rounds=1)
    assert got == "arbiter"  # stale approve не засчитан


def test_reviewer_retry_after_invalid(tmp_path):
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)

    class Flaky:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            (Path(cwd) / ".agent" / "review_r1_muse.json").write_text(
                "{не json", encoding="utf-8")
            return "rev-1"

        def resume(self, sid, prompt, cwd, log=None):
            (Path(cwd) / ".agent" / "review_r1_muse.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8")
            return sid

    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": CountingExec(), "reviewers": {"muse": Flaky()}},
                       rounds=2)
    assert got == "ready"


def test_runner_units(tmp_path):
    from hub.pipeline.runners import (
        _extract_session_id,
        make_runner,
        prompt_arg,
        AgyRunner,
        OpencodeRunner,
    )

    assert prompt_arg("коротко", str(tmp_path)) == "коротко"
    long_prompt = "ы" * 70000
    ref = prompt_arg(long_prompt, str(tmp_path))
    assert long_prompt not in ref and ".agent/prompt_" in ref
    assert _extract_session_id('{"sessionID": "abc-1"}\nмусор\n') == "abc-1"
    assert _extract_session_id("нет id\n") is None
    assert isinstance(make_runner("muse"), OpencodeRunner)
    assert isinstance(make_runner("gemini"), AgyRunner)
    try:
        make_runner("bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("bogus принят")
