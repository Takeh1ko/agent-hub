"""H06 круг 3: тесты на каждое high/medium ревью архитектора.

HIGH: per-reviewer файлы панели, owner_command из event.task_id.
MEDIUM: цикл queue run, fix_prompt «зелёные», clean префиксов веток,
budget_usd == 0 — запрет, start при ушедшей базе.
LOW (попутно): первый finding с file:line, уникальные prompt-файлы,
флаг continued после упавшего preflight, link сессии до resume.
Фейковые раннеры, без сети.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

from hub.cli import main
from hub.config import ProjectConfig, Defaults
from hub.pipeline import cycle as cyc
from hub.pipeline import prompts
from hub.pipeline.common import card_hash_of, meta_get, meta_set
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
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _mk_project(repo: Path) -> ProjectConfig:
    return ProjectConfig(
        name="T", root=str(repo), worktrees=str(repo.parent / "wt-dir"),
        rules="docs/rules.md", python=sys.executable, test_lock="",
        work_branch="main", push="", allowed_paths=["sub/**", "docs/**", "tests/**"],
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


class ExecOk:
    tool = "opencode"
    model = "muse"

    def start(self, prompt, cwd, log=None):
        _commit_ok(Path(cwd))
        return "exec-1"

    def resume(self, sid, prompt, cwd, log=None):
        _commit_ok(Path(cwd), text="3\n")
        return sid or "exec-1"


class HonestRev:
    """Честный фейк: пишет файл, названный в промпте (как настоящая модель)."""

    def __init__(self, name):
        self.name = name
        self.tool = "opencode"
        self.model = name
        self.starts = 0
        self.resumes = 0
        self.prompts = []

    def start(self, prompt, cwd, log=None):
        self.starts += 1
        self.prompts.append(prompt)
        m = re.search(r"\.agent/(review_r\d+_[A-Za-z0-9_-]+\.json)", prompt or "")
        assert m, f"в промпте нет per-reviewer файла: {prompt[-200:]}"
        (Path(cwd) / ".agent" / m.group(1)).write_text(
            json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8")
        return f"rev-{self.name}"

    def resume(self, sid, prompt, cwd, log=None):
        self.resumes += 1
        return sid


def test_panel_honest_fake_single_call(tmp_path):
    """HIGH: честный ревьюер пишет файл из промпта — второй вызов не нужен."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    Store().upsert_task(id="T01", reviewers_json='["aa", "bb"]')
    revs = {"aa": HonestRev("aa"), "bb": HonestRev("bb")}
    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": ExecOk(), "reviewers": revs}, rounds=2)
    assert got == "ready"
    for name, rev in revs.items():
        assert rev.starts == 1, (name, rev.starts)
        assert rev.resumes == 0, (name, rev.resumes)
        assert f"review_r1_{name}.json" in rev.prompts[0]
    # Общего файла, за который гонялись бы оба, нет.
    assert not (repo / ".agent" / "review_r1.json").exists()


def test_reviewer_session_linked_before_resume(tmp_path):
    """LOW: link_session видна уже в resume (не только в конце шага)."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    store = Store()
    seen: dict = {}

    class LinkCheck:
        tool = "opencode"
        model = "muse"

        def start(self, prompt, cwd, log=None):
            (Path(cwd) / ".agent" / "review_r1_muse.json").write_text(
                "{не json", encoding="utf-8")
            return "rev-link-1"

        def resume(self, sid, prompt, cwd, log=None):
            ids = [r["external_id"] for r in store.list_sessions("T01")]
            seen["linked"] = sid in ids
            (Path(cwd) / ".agent" / "review_r1_muse.json").write_text(
                json.dumps({"verdict": "approve", "findings": []}),
                encoding="utf-8")
            return sid

    got = cyc.run_task(store, proj, "T01",
                       {"executor": ExecOk(), "reviewers": {"muse": LinkCheck()}},
                       rounds=2)
    assert got == "ready"
    assert seen.get("linked") is True


def test_owner_command_h05_merge(tmp_path):
    """HIGH: /merge владельца в формате H05 (id в event.task_id) исполняется."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "rules.md").write_text("# п\n", encoding="utf-8")
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
    # Worktree — соседний каталог; .hub.toml кладём выше него, чтобы
    # hub merge нашёл проект от worktree (как в реальном worktrees-каталоге).
    (tmp_path / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n",
        encoding="utf-8")
    wt = tmp_path / "wt-TM2"
    _git(root, "worktree", "add", str(wt), "-b", "agent/TM2", base)
    (wt / "sub" / "a.txt").write_text("2\n", encoding="utf-8")
    _git(wt, "add", "sub/a.txt")
    _git(wt, "commit", "-m", "a")
    head = _git(wt, "rev-parse", "HEAD")
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "done.json").write_text(json.dumps({
        "commit": head, "files": ["sub/a.txt"],
        "tests": {"cmd": "x", "ok": True, "tail": "t"}, "notes": ""}),
        encoding="utf-8")
    card = tmp_path / "TM2.md"
    card.write_text(CARD, encoding="utf-8")
    Store().upsert_task(id="TM2", project="T", card_path=str(card), card_hash="h",
                        level="hard", branch="agent/TM2", worktree=str(wt),
                        base_sha=base, stage="ready", round=1, executor="muse",
                        reviewers_json='["muse"]', stage_reason="")
    from hub.commands.queue import _owner_commands

    Store().add_event("TM2", "owner_command", {"action": "merge"})
    _owner_commands(Store())
    assert Store().get_task("TM2")["stage"] == "merged"
    # Повтор — ровно один раз: merged уже не трогаем.
    _owner_commands(Store())
    assert Store().get_task("TM2")["stage"] == "merged"


def test_queue_loop_picks_late_event(tmp_path):
    """MEDIUM: событие, добавленное после старта run, исполняется в цикле."""
    repo, base = _mk_repo(tmp_path)
    Store().upsert_task(id="TL", project="T", card_path="x", branch="agent/TL",
                        worktree=str(repo), base_sha=base, stage="exec r1")
    (repo / ".agent").mkdir(exist_ok=True)
    from hub.commands.queue import cmd_queue_run

    args = types.SimpleNamespace(project=None, max_parallel=1, once=False,
                                 poll_secs=0.05)
    th = threading.Thread(target=cmd_queue_run, args=(args,), daemon=True)
    th.start()
    try:
        time.sleep(0.3)  # цикл уже крутится, queued пуст
        assert th.is_alive()
        Store().add_event("TL", "owner_command", {"action": "stop"})
        deadline = time.time() + 10
        while Store().get_task("TL")["stage"] != "stopped" and time.time() < deadline:
            time.sleep(0.05)
        assert Store().get_task("TL")["stage"] == "stopped"
    finally:
        meta_set(Store(), "queue_stop", "1")
        th.join(timeout=10)
    assert not th.is_alive()


def test_fix_prompt_green(tmp_path):
    """MEDIUM: пустые ошибки ворот — «зелёные», не «красные»."""
    from hub.gate.gate import GateResult

    text = prompts.fix_prompt(
        [], GateResult(ok=True, errors=[], diff_stat="",
                       tests_tail="2 passed в 0.1s"))
    assert "зелёные" in text
    assert "красные" not in text


def test_clean_lists_branch_without_plus(tmp_path):
    """MEDIUM: list_orphans отдаёт ветку из worktree без префикса '+'."""
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
    orph = tmp_path / "wt-dir" / "orph"
    orph.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", str(orph), "-b", "agent/orph", base)
    proj = ProjectConfig(name="T", root=str(root), rules="",
                         python=sys.executable, test_lock="", work_branch="main",
                         push="", allowed_paths=["sub/**"],
                         defaults=Defaults())
    from hub.pipeline.merge import list_orphans

    orph_wt, orph_br = list_orphans(proj, Store())
    assert "agent/orph" in orph_br
    assert not any(b.startswith(("+", "*")) for b in orph_br)


def test_budget_usd_zero_forbids(tmp_path):
    """MEDIUM: budget_usd == 0 — стоп при любом usd > 0 (+ вопрос)."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": ExecOk(),
                        "reviewers": {"muse": HonestRev("muse")}},
                       rounds=2, cost_fn=lambda s, t: (0.0, 5.0))
    assert got == "stopped"
    assert Store().get_task("T01")["stage"] == "stopped"
    import sqlite3 as _sq

    con = _sq.connect(str(Store().path))
    try:
        rows = con.execute("SELECT text FROM question WHERE task_id='T01'").fetchall()
    finally:
        con.close()
    assert rows


def test_budget_go_zero_no_limit(tmp_path):
    """MEDIUM: budget_go == 0 — лимита нет (пара к запрету usd)."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    Store().upsert_task(id="T01", budget_go=0.0, budget_usd=100.0)
    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": ExecOk(),
                        "reviewers": {"muse": HonestRev("muse")}},
                       rounds=2, cost_fn=lambda s, t: (1000.0, 0.0))
    assert got == "ready"


def test_start_new_base_final_refuses(tmp_path, capsys):
    """MEDIUM: та же карточка при ушедшей базе не затирает merged/ready."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "rules.md").write_text("# п\n", encoding="utf-8")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n",
        encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    card = tmp_path / "TB2.md"
    card.write_text(CARD, encoding="utf-8")
    chash = card_hash_of(card)
    old_base = _git(root, "rev-parse", "HEAD")
    Store().upsert_task(id="TB2", project="T", card_path=str(card), card_hash=chash,
                        level="hard", branch="agent/TB2", worktree=str(root),
                        base_sha=old_base, stage="merged", round=2,
                        stage_reason="слито")
    # База ушла вперёд после merge.
    (root / "sub" / "next.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "sub/next.txt")
    _git(root, "commit", "-m", "вперёд")
    assert main(["start", str(card), "--project", str(root)]) == 1
    assert "уже есть TB2" in capsys.readouterr().out
    got = Store().get_task("TB2")
    assert got["stage"] == "merged" and got["base_sha"] == old_base


def test_start_budget_usd_flag(tmp_path, capsys):
    """MEDIUM: --budget-usd пишется в задачу (не хардкод 0.0)."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "sub").mkdir()
    (root / "sub" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "rules.md").write_text("# п\n", encoding="utf-8")
    (root / ".hub.toml").write_text(
        "schema_version = 1\nname = \"T\"\n"
        f"root = \"{root}\"\nworktrees = \"{tmp_path}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\nwork_branch = \"main\"\n"
        "push = \"\"\nallowed_paths = [\"sub/**\", \"docs/**\"]\n"
        "[hooks]\n[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\nbudget_go = 0.5\n"
        "budget_usd = 1.5\n",
        encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    card = tmp_path / "TU3.md"
    card.write_text(CARD, encoding="utf-8")
    assert main(["start", str(card), "--project", str(root)]) == 0
    capsys.readouterr()
    assert Store().get_task("TU3")["budget_usd"] == 1.5
    card2 = tmp_path / "TU4.md"
    card2.write_text(CARD.replace("# T", "# T4"), encoding="utf-8")
    assert main(["start", str(card2), "--project", str(root),
                 "--budget-usd", "2.5"]) == 0
    capsys.readouterr()
    assert Store().get_task("TU4")["budget_usd"] == 2.5


def test_collect_prefers_finding_with_pos(tmp_path):
    """LOW: dispute считается по finding с file:line, не по первому."""
    repo, base = _mk_repo(tmp_path)
    agent = repo / ".agent"
    agent.mkdir(exist_ok=True)
    long_body = "о" * 60
    (agent / "review_r1_a.json").write_text(json.dumps({
        "verdict": "dispute",
        "findings": [{"severity": "low", "issue": long_body},
                     {"severity": "high", "file": "sub/a.txt", "line": 5,
                      "issue": long_body, "fix": "x"}]}), encoding="utf-8")
    revs = cyc._collect_reviews(str(repo), 1)
    assert len(revs) == 1
    assert revs[0].file == "sub/a.txt" and revs[0].line == 5
    from hub.gate.verdict import verdict as _v

    assert _v(revs, 1, 2) == "arbiter"


def test_prompt_arg_unique_names(tmp_path):
    """LOW: длинные промпты не перезаписывают друг друга."""
    from hub.pipeline.runners import prompt_arg

    long_prompt = "ы" * 70000
    ref1 = prompt_arg(long_prompt, str(tmp_path))
    ref2 = prompt_arg(long_prompt, str(tmp_path))
    assert ref1 != ref2
    assert (tmp_path / ".agent").is_dir()
    assert len(list((tmp_path / ".agent").glob("prompt_*.md"))) == 2


def test_continued_flag_kept_on_prefail(tmp_path):
    """LOW: упавший preflight не съедает флаг continued."""
    repo, base = _mk_repo(tmp_path)
    proj = _mk_project(repo)
    _mk_task(tmp_path, repo, base)
    meta_set(Store(), "continued:T01", "1")
    (repo / "sub" / "dirty.txt").write_text("грязь\n", encoding="utf-8")
    got = cyc.run_task(Store(), proj, "T01",
                       {"executor": ExecOk(),
                        "reviewers": {"muse": HonestRev("muse")}}, rounds=2)
    assert got == "failed"
    assert meta_get(Store(), "continued:T01") == "1"
