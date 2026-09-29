"""H15: владелец ставит задачи сам — черновик модели, запуск по кнопке.

Фейковые раннеры пишут карточку без сети и моделей.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from hub.bot import core as bc
from hub.cli import main
from hub.config import ProjectConfig, Defaults
from hub.pipeline import draft as d
from hub.store import Store

NOW = 1_789_000_000_000

GOOD_CARD = """# O1 — сделать икс

**Цель.** Сделать икс, чтобы игреку стало хорошо.

**Прочитать.** docs/spec.md

**Можно менять.** `sub/**`

**Интерфейс.** `f()` делает икс.

**Приёмка.** `pytest -q sub/test_ok.py`

**Нельзя.** Трогать боевую базу; сеть.

**Сеть.** нет.

**Исполнитель.** muse.

**Уровень.** hard.

**Коммит.** `feat: икс`
"""

BAD_CARD = """# O1 — сделать икс

**Цель.** Сделать икс.

**Прочитать.** docs/spec.md

**Можно менять.** `sub/**`

**Интерфейс.** `f()` делает икс.

**Нельзя.** сеть.

**Сеть.** нет.

**Исполнитель.** muse.

**Уровень.** hard.

**Коммит.** `feat: икс`
"""


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _mk_repo(tmp_path) -> tuple[Path, ProjectConfig]:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "sub").mkdir(exist_ok=True)
    (repo / "sub" / "test_ok.py").write_text(
        "def test_ok():\n    pass\n", encoding="utf-8")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "rules.md").write_text("# правила\n", encoding="utf-8")
    (repo / "docs" / "spec.md").write_text("спека\n", encoding="utf-8")
    (repo / ".hub.toml").write_text(
        f"schema_version = 1\nname = \"T\"\nroot = \"{repo}\"\n"
        f"worktrees = \"{tmp_path / 'wt-dir'}\"\n"
        "rules = \"docs/rules.md\"\n"
        f"python = \"{sys.executable}\"\ntest_lock = \"\"\n"
        "work_branch = \"main\"\npush = \"\"\n"
        "allowed_paths = [\"sub/**\", \"docs/**\", \"tests/**\"]\n"
        "[defaults]\nexecutor = \"muse\"\nreviewers = [\"muse\"]\n",
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    proj = ProjectConfig(
        name="T", root=str(repo), worktrees=str(tmp_path / "wt-dir"),
        rules="docs/rules.md", python=sys.executable, test_lock="",
        work_branch="main", push="",
        allowed_paths=["sub/**", "docs/**", "tests/**"],
        defaults=Defaults(executor="muse", reviewers=["muse"], budget_go=0.5),
    )
    return repo, proj


class GoodFake:
    tool = "fake"

    def __init__(self, card: str = GOOD_CARD):
        self.card = card
        self.n = 0

    def start(self, prompt, cwd, log=None):
        self.n += 1
        return self.card


class SeqFake:
    """Первый вызов — первая карточка, дальше — вторая."""

    tool = "fake"

    def __init__(self, first: str, second: str):
        self.first = first
        self.second = second
        self.n = 0

    def start(self, prompt, cwd, log=None):
        self.n += 1
        return self.first if self.n == 1 else self.second


class TransientThenGood:
    tool = "fake"

    def __init__(self, fails: int = 2):
        self.fails = fails
        self.n = 0

    def start(self, prompt, cwd, log=None):
        self.n += 1
        if self.n <= self.fails:
            raise RuntimeError("opencode: Unexpected server error, попробуй позже")
        return GOOD_CARD


def _inbox_texts(store: Store) -> list[str]:
    import sqlite3

    con = sqlite3.connect(str(store.path))
    try:
        return [r[0] for r in con.execute("SELECT text FROM inbox ORDER BY id")]
    finally:
        con.close()


def test_make_draft_ready(tmp_path):
    repo, proj = _mk_repo(tmp_path)
    s = Store()
    fake = GoodFake()
    did = d.make_draft(s, proj, "сделать икс для игрека", "cli", fake)
    row = s.get_draft(did)
    assert row is not None and row["status"] == "ready"
    assert fake.n == 1
    card_path = Path(str(row["card_path"]))
    assert str(card_path).startswith(str(repo / "docs" / "tasks" / "_drafts"))
    assert card_path.is_file()
    from hub.gate.lint import lint_card

    assert lint_card(card_path, proj).ok
    prev = d.draft_preview(str(row["card_text"]))
    assert len(prev) <= 1500 and "икс" in prev


def test_make_draft_retry_once_bad_then_good(tmp_path):
    repo, proj = _mk_repo(tmp_path)
    s = Store()
    fake = SeqFake(BAD_CARD, GOOD_CARD)
    did = d.make_draft(s, proj, "сделать икс", "tg", fake, chat_id=7)
    row = s.get_draft(did)
    assert row is not None and row["status"] == "ready", row
    assert fake.n == 2, "ровно один повтор при ошибках линта"


def test_make_draft_failed_twice_bad(tmp_path):
    s = Store()
    _, proj = _mk_repo(tmp_path)
    fake = SeqFake(BAD_CARD, BAD_CARD)
    did = d.make_draft(s, proj, "сделать икс", "top", fake)
    row = s.get_draft(did)
    assert row is not None and row["status"] == "failed"
    assert fake.n == 2
    assert str(row["lint_errors"]).strip(), "текст ошибок линта сохранён"


def test_transient_retries_then_ready(tmp_path, monkeypatch):
    s = Store()
    _, proj = _mk_repo(tmp_path)
    monkeypatch.setattr(d, "_retry_sleep", lambda s_: None)
    fake = TransientThenGood(fails=2)
    did = d.make_draft(s, proj, "сделать икс", "cli", fake)
    row = s.get_draft(did)
    assert row is not None and row["status"] == "ready"
    assert fake.n == 3


def test_start_draft_moves_commits_only_card(tmp_path):
    repo, proj = _mk_repo(tmp_path)
    s = Store()
    fake = GoodFake()
    did = d.make_draft(s, proj, "сделать икс для игрека", "tg", fake, chat_id=5)
    # Грязные файлы рабочего дерева: неотслеженный и изменённый.
    (repo / "sub" / "dirty.txt").write_text("мусор\n", encoding="utf-8")
    (repo / "sub" / "test_ok.py").write_text(
        "def test_ok():\n    pass\n# грязная правка\n", encoding="utf-8")
    task_id = d.start_draft(s, did)
    row = s.get_draft(did)
    assert row is not None and row["status"] == "started"
    assert row["task_id"] == task_id
    dest = Path(str(row["card_path"]))
    assert dest.parent == repo / "docs" / "tasks"
    assert dest.is_file()
    assert not (repo / "docs" / "tasks" / "_drafts" / dest.name).exists()
    # Коммит содержит ТОЛЬКО карточку.
    names = _git(repo, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
    names = [n for n in (x.strip() for x in names) if n]
    assert names == [str(dest.relative_to(repo))], names
    # Грязь не попала в коммит и осталась на диске.
    assert (repo / "sub" / "dirty.txt").is_file()
    out = _git(repo, "status", "--porcelain")
    assert "dirty.txt" in out
    # Задача в очереди, событие во входящих Claude.
    task = s.get_task(task_id)
    assert task is not None and task["stage"] == "queued"
    assert any("владелец поставил задачу" in t and task_id in t
               for t in _inbox_texts(s))


def test_cancel_draft_removes_file(tmp_path):
    s = Store()
    _, proj = _mk_repo(tmp_path)
    did = d.make_draft(s, proj, "сделать икс", "cli", GoodFake())
    path = str(s.get_draft(did)["card_path"])
    assert Path(path).is_file()
    assert d.cancel_draft(s, did) is True
    row = s.get_draft(did)
    assert row is not None and row["status"] == "cancelled"
    assert not Path(path).exists()


def test_cli_new_project_text_list_start_cancel(tmp_path, capsys, monkeypatch):
    repo, proj = _mk_repo(tmp_path)
    monkeypatch.setattr("hub.pipeline.draft.make_runner_for_project",
                        lambda p: GoodFake())
    assert main(["new", "--project", str(repo), "сделать икс"]) == 0
    out = capsys.readouterr().out
    m = re.search(r"черновик (\d+) готов", out)
    assert m, out
    did = m.group(1)
    assert main(["new", "--list"]) == 0
    assert did in capsys.readouterr().out
    assert main(["new", "--start", did]) == 0
    assert "OK" in capsys.readouterr().out
    s = Store()
    assert s.get_draft(int(did))["status"] == "started"
    # Второй черновик — отмена через CLI.
    assert main(["new", "--project", str(repo), "ещё икс"]) == 0
    m2 = re.search(r"черновик (\d+) готов", capsys.readouterr().out)
    assert m2
    assert main(["new", "--cancel", m2.group(1)]) == 0
    assert "отмен" in capsys.readouterr().out.lower()
    assert s.get_draft(int(m2.group(1)))["status"] == "cancelled"


def test_bot_new_preview_two_buttons_and_start(tmp_path):
    import asyncio

    from hub.bot import run as br

    repo, proj = _mk_repo(tmp_path)
    s = Store()
    did = d.make_draft(s, proj, "сделать икс для игрека", "tg", GoodFake(),
                       chat_id=42)
    row = s.get_draft(did)
    assert row is not None and row["status"] == "ready"
    # /new PlayerUP текст: разбор аргументов.
    p1 = ProjectConfig(name="PlayerUP", root="/x", worktrees="/w", rules="",
                       python=sys.executable, test_lock="", work_branch="",
                       push="", allowed_paths=[],
                       defaults=Defaults())
    p2 = ProjectConfig(name="T", root=str(repo), worktrees="", rules="",
                       python=sys.executable, test_lock="", work_branch="",
                       push="", allowed_paths=[],
                       defaults=Defaults())
    want, rest = bc.parse_new_args("T сделать икс", [p1, p2])
    assert (want, rest) == ("T", "сделать икс")
    assert bc.parse_new_args("", [p1, p2]) == (None, "")
    # Предпросмотр ≤1500 с двумя кнопками.
    preview = bc.format_draft_preview(row, str(row["card_text"]))
    assert len(preview) <= 1500 and "икс" in preview
    buttons = bc.draft_buttons(did)
    assert [b.data for b in buttons] == [
        f"draft:start:{did}", f"draft:cancel:{did}"]
    assert bc.parse_draft_callback(f"draft:start:{did}") == ("start", str(did))
    # «Запустить» ставит задачу (фейк-бота сети не касается).
    (repo / "sub" / "dirty.txt").write_text("мусор\n", encoding="utf-8")

    class _FakeBot:
        pass

    text = asyncio.run(br.handle_draft_callback(_FakeBot(), 42,
                                                f"draft:start:{did}", NOW))
    assert text is not None and "OK" in text and "очеред" in text
    fresh = s.get_draft(did)
    assert fresh is not None and fresh["status"] == "started"
    task = s.get_task(str(fresh["task_id"]))
    assert task is not None and task["stage"] == "queued"


def test_bot_new_help_and_project_buttons():
    assert "/new" in bc.HELP_TEXT
    assert bc.NEW_ASK_TEXT and bc.NEW_WRITING
    p = ProjectConfig(name="T", root="/x", worktrees="", rules="",
                      python=sys.executable, test_lock="", work_branch="",
                      push="", allowed_paths=[], defaults=Defaults())
    btns = bc.project_buttons([p])
    assert len(btns) == 1 and btns[0].data == "draft:project:T"
    assert bc.parse_draft_callback("draft:project:T") == ("project", "T")
    assert bc.parse_draft_callback("мусор") is None


async def test_tui_n_opens_and_creates_draft(tmp_path, monkeypatch):
    from textual.widgets import Input

    from hub.tui.app import HubApp, NewDraftScreen

    repo, proj = _mk_repo(tmp_path)
    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    app = HubApp(store=Store(), opencode_db=None, proc_root=str(proc))
    app._schedule_refresh = lambda: None
    monkeypatch.setattr(app, "_draft_projects", lambda: [proj])
    monkeypatch.setattr(app, "_draft_runner", lambda p: GoodFake())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, NewDraftScreen):
                break
        assert isinstance(app.screen, NewDraftScreen)
        inp = app.screen.query_one("#new-text", Input)
        inp.focus()
        inp.value = "сделать икс для игрека"
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        rows = []
        for _ in range(500):
            await pilot.pause()
            rows = Store().list_drafts()
            if rows and str(rows[0].get("status") or "") in ("ready", "failed"):
                break
        rows = Store().list_drafts()
        assert rows and rows[0]["status"] == "ready", rows
        assert Path(str(rows[0]["card_path"])).is_file()


def test_dispatcher_has_new_command():
    from hub.bot.run import build_dispatcher

    dp = build_dispatcher()
    assert dp is not None
