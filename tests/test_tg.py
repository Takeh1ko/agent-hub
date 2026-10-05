"""TG v2: bot logic (no network) and launching Claude when there is no live session."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ahub import comms, events, transitions
from ahub.model import Ev, State
from ahub.scope import Scope
from ahub.store import Store
from ahub.tg import core, launcher
from ahub.time import now_ms
from tests.conftest import wait_until
from tests.enginekit import make_project


@pytest.fixture
def store() -> Store:
    return Store()


def test_text_goes_to_claude(store):
    rep = core.on_text(store, 42, "как там оплата?", projects=["webapp", "agent-hub"])
    assert "поднимаю" in rep.text
    assert events.lines(store, events.unacked(store)) == ["OWNER «как там оплата?»"]
    assert core.chats(store) == [42]
    events.touch(store)
    assert "на связи" in core.on_text(store, 42, "ещё", projects=[]).text


def test_project_prefix(store):
    assert core.split_project("по agent-hub: почини бота", ["agent-hub", "webapp"]) == ("agent-hub", "почини бота")
    assert core.split_project("webapp: статус", ["agent-hub", "webapp"]) == ("webapp", "статус")
    assert core.split_project("просто текст", ["agent-hub"]) == (None, "просто текст")
    core.on_text(store, 1, "по agent-hub: x", projects=["agent-hub"])
    with store.read() as c:
        assert c.execute("SELECT project FROM message").fetchone()[0] == "agent-hub"


def test_tasks_view(store):
    a = store.create_task(project="P", kind="scout", title="разведка")
    b = store.create_task(project="P", kind="code", title="кнопка оплаты")
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, b, st)
    rep = core.tasks_reply(store)
    labels = [row[0].label for row in rep.buttons]
    assert any(lbl.startswith(f"T{b} · готово") for lbl in labels)
    d = core.task_detail(store, b)
    assert "кнопка оплаты" in d.text and d.buttons[0][0].data == "tasks"
    assert "нет задачи" in core.task_detail(store, 999).text
    assert a


def test_question_buttons_and_reply(store):
    qid = comms.ask(store, "сливать T12?", ["да", "нет"])
    q = core.pending_questions(store)[0]
    rep = core.question_reply(q)
    assert [b.data for row in rep.buttons for b in row] == [f"ans:{qid}:0", f"ans:{qid}:1"]
    assert "→ да" in core.on_answer_button(store, f"ans:{qid}:0")
    assert "уже отвечено" in core.on_answer_button(store, f"ans:{qid}:1")
    q2 = comms.ask(store, "почему?")
    assert "передал" in core.on_reply_to_question(store, q2, "потому")
    assert [e.payload["answer"] for e in events.unacked(store)] == ["да", "потому"]


def test_alarm_text():
    class E:
        critical = True
        payload = {"text": "opencode лёг"}
    assert core.alarm_text(E()).startswith("🚨 Хаб: opencode лёг")


# --- launching Claude ---

def _forget_presence(store):
    """No live session: both presence tables (presence_project and the pre-006 one)."""
    with store.tx() as c:
        c.execute("DELETE FROM presence")
        c.execute("DELETE FROM presence_project")


def _stale_presence(store, who: str = "claude"):
    """A session that was there long ago — not "present" any more."""
    with store.tx() as c:
        c.execute("UPDATE presence SET last_seen=0 WHERE who=?", (who,))
        c.execute("UPDATE presence_project SET last_seen=0 WHERE who=?", (who,))


class Spawner:
    def __init__(self, session="sess-1"):
        self.calls = []
        self.session = session
        self.procs = []

    def __call__(self, cmd, cwd, log):
        self.calls.append((cmd, cwd))
        with open(log, "w") as f:
            f.write(json.dumps({"type": "system", "subtype": "init", "session_id": self.session}) + "\n")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.procs.append(p)
        return p.pid


def test_launcher_flow(store, tmp_path):
    project = make_project(tmp_path)
    sp = Spawner()
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude") == "idle"
    core.on_text(store, 1, "привет", projects=["P"])
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude") == "launched"
    cmd, cwd = sp.calls[0]
    assert cwd == project.root and "--dangerously-skip-permissions" in cmd and "--resume" not in cmd
    assert "привет" in cmd[cmd.index("-p") + 1] and "ahub say" in cmd[cmd.index("-p") + 1]
    core.on_text(store, 1, "ещё вопрос", projects=["P"])
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude") == "running"  # no second launch
    sp.procs[0].kill()
    sp.procs[0].wait()
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude") == "finished"
    assert json.loads(store.meta_get(launcher.session_key("")))["id"] == "sess-1"
    assert [m["text"] for m in comms.inbox(store, mark=False)] == ["ещё вопрос"]  # the first was passed on
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude") == "launched"  # the backlog
    assert sp.calls[1][0][sp.calls[1][0].index("--resume") + 1] == "sess-1"  # the same TG session
    for p in sp.procs:
        p.kill()


def test_launcher_respects_presence_and_limit(store, tmp_path):
    project = make_project(tmp_path)
    sp = Spawner()
    core.on_text(store, 1, "x", projects=[])
    events.touch(store)
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude") == "idle"  # a live Claude exists
    _forget_presence(store)
    with store.tx() as c:
        for _ in range(launcher.MAX_PER_HOUR):
            c.execute("INSERT INTO claude_launch(ts, project, status) VALUES(?,?, 'ok')", (now_ms(), "P"))
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude") == "limit"


def test_launcher_timeout_kills(store, tmp_path):
    project = make_project(tmp_path)
    sp = Spawner()
    core.on_text(store, 1, "x", projects=[])
    t0 = now_ms()
    launcher.tick(store, projects=[project], spawn=sp, binary="claude", now=t0)
    assert launcher.tick(store, projects=[project], now=t0 + launcher.TIMEOUT_MS + 1) == "killed"
    assert wait_until(lambda: sp.procs[0].poll() is not None), "процесс лаунчера пережил таймаут"


def test_project_choice(store, tmp_path):
    a = make_project(tmp_path / "a")
    import dataclasses
    b = dataclasses.replace(make_project(tmp_path / "b"), name="B")
    events.touch(store, project="B")
    _stale_presence(store)  # was there, but long ago — not "present"
    core.on_text(store, 1, "x", projects=["P", "B"])
    sp = Spawner()
    launcher.tick(store, projects=[a, b], spawn=sp, binary="claude")
    assert sp.calls[0][1] == b.root  # the last project of Claude
    sp.procs[0].kill()


# --- presence and the launcher per project ---

def two_projects(tmp_path):
    import dataclasses

    a = make_project(tmp_path / "a")
    b = dataclasses.replace(make_project(tmp_path / "b"), name="B")
    return a, b


def test_live_session_in_a_does_not_block_a_launch_for_b(store, tmp_path):
    a, b = two_projects(tmp_path)
    core.on_text(store, 1, "по B: почини кнопку", projects=["P", "B"])
    events.touch_scope(store, Scope(("P",)), via="watch")  # a live Claude in P — it is not B's
    sp = Spawner()
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    cmd, cwd = sp.calls[0]
    assert cwd == b.root and "почини кнопку" in cmd[cmd.index("-p") + 1]
    assert "Project: B" in cmd[cmd.index("-p") + 1]
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "running"
    for p in sp.procs:
        p.kill()


def test_prompt_of_b_carries_no_text_of_a(store, tmp_path):
    a, b = two_projects(tmp_path)
    core.on_text(store, 1, "по P: дело A", projects=["P", "B"])
    core.on_text(store, 1, "по B: дело B", projects=["P", "B"])
    events.touch_scope(store, Scope(("P",)), via="watch")  # A is read by a live session, B is not
    sp = Spawner()
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    prompt = sp.calls[0][0][sp.calls[0][0].index("-p") + 1]
    assert _messages_block(prompt).strip() == "- дело B"
    assert "Project: B" in prompt
    for p in sp.procs:
        p.kill()

    # a launch for B passes on its own messages only — A's stay for its Claude
    sp.procs[-1].kill()
    sp.procs[-1].wait()
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "finished"
    assert [m["text"] for m in comms.inbox(store, mark=False)] == ["дело A"]


def test_every_project_without_a_live_session_gets_its_own_claude(store, tmp_path):
    a, b = two_projects(tmp_path)
    core.on_text(store, 1, "по P: дело A", projects=["P", "B"])
    core.on_text(store, 1, "по B: дело B", projects=["P", "B"])
    sp = Spawner()
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"  # the other project
    assert [call[1] for call in sp.calls] == [a.root, b.root]
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "running"  # both are busy
    with store.read() as c:
        assert [r[0] for r in c.execute("SELECT project FROM claude_launch WHERE status='running'")] == ["P", "B"]
    for p in sp.procs:
        p.kill()


def test_each_project_resumes_its_own_tg_session(store, tmp_path):
    """The per-project session key: a launch in B must not resume A's conversation."""
    a, b = two_projects(tmp_path)

    class TwoSessions(Spawner):
        """Each launch writes its own session id — P gets sess-A, B gets sess-B."""

        def __call__(self, cmd, cwd, log):
            self.session = "sess-" + {str(a.root): "A", str(b.root): "B"}[str(cwd)]
            return super().__call__(cmd, cwd, log)

    sp = TwoSessions()
    core.on_text(store, 1, "по P: дело A", projects=["P", "B"])
    core.on_text(store, 1, "по B: дело B", projects=["P", "B"])
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    sp.calls.clear()
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    sp.calls.clear()
    for p in sp.procs:
        p.kill()
        p.wait()
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "finished"  # both ended

    core.on_text(store, 1, "по P: ещё A", projects=["P", "B"])
    core.on_text(store, 1, "по B: ещё B", projects=["P", "B"])
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    resumed = [c[0][c[0].index("--resume") + 1] if "--resume" in c[0] else None for c in sp.calls]
    assert resumed == ["sess-A", "sess-B"], resumed  # each resumes its own session
    assert store.meta_get(launcher.session_key("P")) != store.meta_get(launcher.session_key("B"))
    assert len({launcher.session_key("P"), launcher.session_key("B"), launcher.session_key("")}) == 3
    for p in sp.procs:
        p.kill()


def test_a_group_without_a_directory_is_reported_once(store, tmp_path, caplog):
    a, _b = two_projects(tmp_path)
    comms.owner_message(store, "по Z: в никуда", project="Z")  # Z is not a project of this hub
    sp = Spawner()
    said: dict[str, int] = {}  # the memory of what was reported — the caller's, not a module global
    with caplog.at_level("WARNING", logger="launcher"):
        assert launcher.tick(store, projects=[a], spawn=sp, binary="claude", no_dir=said) == "nodir:Z"
        assert launcher.tick(store, projects=[a], spawn=sp, binary="claude", no_dir=said) == "idle"  # said once
        assert list(said) == ["Z"] and said["Z"] > 0  # the memory of it is the caller's
    assert [r.message for r in caplog.records] == [
        "cannot launch Claude: no directory for project Z — no such project in the hub config"]
    assert sp.calls == []


def test_the_owner_group_without_any_project_is_reported_once(store, caplog):
    """'' is a target too: with no project configured the hub-wide group has no directory — say it, never idle."""
    comms.owner_message(store, "всем сразу", project="")
    sp = Spawner()
    said: dict[str, int] = {}
    with caplog.at_level("WARNING", logger="launcher"):
        assert launcher.tick(store, projects=[], spawn=sp, binary="claude", no_dir=said) == "nodir:"
        assert launcher.tick(store, projects=[], spawn=sp, binary="claude", no_dir=said) == "idle"  # said once
    assert [r.message for r in caplog.records] == [
        "cannot launch Claude: no directory for the hub-wide group — no projects in the hub config"]
    assert sp.calls == []


def test_every_group_without_a_directory_is_reported(store, caplog):
    """'' and a project that left the hub both have nowhere to run: each is reported once, not only the first."""
    comms.owner_message(store, "всем сразу", project="")
    comms.owner_message(store, "по Z: в никуда", project="Z")  # Z is not a project of this hub
    sp = Spawner()
    said: dict[str, int] = {}
    with caplog.at_level("WARNING", logger="launcher"):
        first = launcher.tick(store, projects=[], spawn=sp, binary="claude", no_dir=said)
        second = launcher.tick(store, projects=[], spawn=sp, binary="claude", no_dir=said)
        third = launcher.tick(store, projects=[], spawn=sp, binary="claude", no_dir=said)
    assert [first, second] == ["nodir:", "nodir:Z"]  # one per tick — the older group first
    assert third == "idle"  # both said once
    assert [r.message for r in caplog.records] == [
        "cannot launch Claude: no directory for the hub-wide group — no projects in the hub config",
        "cannot launch Claude: no directory for project Z — no such project in the hub config"]
    assert sp.calls == []


def _messages_block(prompt: str) -> str:
    """The owner messages of a launch prompt — the part that must not leak between projects."""
    return prompt.split("Owner messages:\n")[1].split("\n\nHub summary")[0]


def test_hub_wide_messages_go_to_the_owner_only(store, tmp_path):
    a, b = two_projects(tmp_path)
    core.on_text(store, 1, "всем сразу", projects=["P", "B"])
    core.on_text(store, 1, "по B: только B", projects=["P", "B"])
    sp = Spawner()
    launcher.tick(store, projects=[a, b], spawn=sp, binary="claude")  # the owner group is the older one
    prompt = sp.calls[0][0][sp.calls[0][0].index("-p") + 1]
    assert _messages_block(prompt).strip() == "- всем сразу"
    assert "--all" in prompt  # the owner's Claude reads every project
    assert launcher.tick(store, projects=[a, b], spawn=sp, binary="claude") == "launched"
    prompt = sp.calls[1][0][sp.calls[1][0].index("-p") + 1]
    assert _messages_block(prompt).strip() == "- только B"
    assert "Project: B" in prompt
    for p in sp.procs:
        p.kill()


def test_the_bot_picks_a_project(store):
    names = ["P", "B"]
    assert "все проекты" in core.on_text(store, 1, "без префикса", projects=names).text  # nothing picked yet
    core.on_text(store, 1, "по B: первое", projects=names)
    assert core.current_project(store, 1) == "B"  # the prefix is a pick
    rep = core.on_text(store, 1, "второе", projects=names)
    assert "проект: B" in rep.text
    assert [m["project"] for m in comms.inbox(store, mark=False)] == ["", "B", "B"]

    listed = core.project_reply(store, names, chat_id=1)
    assert "сейчас проект: B" in listed.text
    assert [b.data for row in listed.buttons for b in row] == ["proj:P", "proj:B", "proj:all"]
    assert core.project_reply(store, names, chat_id=1).buttons[1][0].label == "✓ B"

    assert "проект: P" in core.project_reply(store, names, "P", chat_id=1).text  # /project P
    assert core.current_project(store, 1) == "P"
    assert "нет" in core.project_reply(store, names, "нет такого", chat_id=1).text
    assert core.current_project(store, 1) == "P"  # an unknown project changes nothing
    assert "проектов здесь вообще нет" in core.project_reply(store, [], "что угодно", chat_id=1).text


def test_the_pick_is_per_chat(store):
    """A pick in one chat does not re-target the messages of another one."""
    names = ["P", "B"]
    core.project_reply(store, names, "B", chat_id=1)
    assert core.current_project(store, 1) == "B" and core.current_project(store, 2) == ""
    assert "все проекты" in core.on_text(store, 2, "из второго чата", projects=names).text
    assert "проект: B" in core.on_text(store, 1, "из первого", projects=names).text
    with store.read() as c:
        rows = c.execute("SELECT chat_id, project FROM message WHERE direction='in' ORDER BY id").fetchall()
    assert [(r["chat_id"], r["project"]) for r in rows] == [(2, ""), (1, "B")]


def test_a_pick_of_a_project_that_left_the_hub_is_dropped(store):
    """B is gone from the hub: the pick is dropped with a notice and the message goes to the hub."""
    core.project_reply(store, ["P", "B"], "B", chat_id=1)
    assert core.current_project(store, 1) == "B"
    rep = core.on_text(store, 1, "почини", projects=["P"])  # B is no longer in the hub
    assert "проекта B в хабе больше нет" in rep.text and "все проекты" in rep.text
    assert core.current_project(store, 1) == ""
    assert [m["project"] for m in comms.inbox(store, mark=False)] == [""]
    assert "не выбран" in core.project_reply(store, ["P"], chat_id=1).text  # the list agrees with the routing


def test_the_gone_notice_says_where_the_message_went(store):
    """With a valid prefix the message went to that project — the notice must not claim it went everywhere."""
    core.project_reply(store, ["P", "B"], "B", chat_id=1)  # B is picked…
    rep = core.on_text(store, 1, "по P: почини", projects=["P"])  # …but gone from the hub; the prefix says P
    assert "проекта B в хабе больше нет — сообщение ушло в проект P" in rep.text
    assert "по всем проектам" not in rep.text and "проект: P" in rep.text
    assert [m["project"] for m in comms.inbox(store, mark=False)] == ["P"]


def test_a_pick_can_be_cleared(store):
    """`/project all` — back to the hub, so a plain message goes to the owner again."""
    names = ["P", "B"]
    core.on_text(store, 1, "по B: первое", projects=names)
    assert core.current_project(store, 1) == "B"
    assert "проект: B" in core.on_text(store, 1, "второе", projects=names).text

    assert "не выбран" in core.project_reply(store, names, "all", chat_id=1).text
    assert core.current_project(store, 1) == ""
    assert [b.data for row in core.project_reply(store, names, chat_id=1).buttons for b in row] == ["proj:P", "proj:B"]
    rep = core.on_text(store, 1, "всем сразу", projects=names)
    assert "все проекты" in rep.text
    assert [m["project"] for m in comms.inbox(store, mark=False)] == ["B", "B", ""]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, reply_markup=None):
        self.sent.append((chat, text, reply_markup))

        class M:
            message_id = len(self.sent)
        return M()


async def test_background_one_pass(store, monkeypatch):
    import asyncio

    from ahub.tg import run as tgrun
    core.remember_chat(store, 7)
    comms.say(store, "T12 готова")
    comms.ask(store, "сливать?", ["да", "нет"])
    comms.raise_alarm(store, "opencode лёг", critical=True)
    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "idle")

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", stop)
    bot = FakeBot()
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(bot, store)
    texts = [t for _, t, _ in bot.sent]
    assert texts[0] == "T12 готова" and "сливать?" in texts[1] and texts[2].startswith("🚨")
    assert bot.sent[1][2] is not None  # the answer buttons
    assert comms.outbox(store) == [] and core.pending_questions(store) == [] and comms.alarms_for_tg(store) == []


async def test_background_tells_the_owner_about_a_project_without_a_directory(store, monkeypatch):
    """`nodir:B` — the launcher says it once, so the owner hears it once."""
    import asyncio

    from ahub.i18n import t
    from ahub.tg import run as tgrun

    core.remember_chat(store, 7)
    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "nodir:B")

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", stop)
    bot = FakeBot()
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(bot, store)
    assert [text for _, text, _ in bot.sent] == [t("tg.launch_no_dir", name="B")]


async def test_background_tells_the_owner_when_the_hub_has_no_projects(store, monkeypatch):
    """`nodir:` with an empty name — the hub-wide group with nothing configured: its own line, not one
    with a blank project name."""
    import asyncio

    from ahub.i18n import t
    from ahub.tg import run as tgrun

    core.remember_chat(store, 7)
    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "nodir:")

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", stop)
    bot = FakeBot()
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(bot, store)
    text = bot.sent[0][1]
    assert text == t("tg.launch_no_dir_hub") and text != t("tg.launch_no_dir", name="")


async def test_the_bot_re_exec_itself_when_the_code_changed(store, monkeypatch):
    """The bot, like the service, runs the code that was just merged — but only after the pass it is in."""
    import asyncio
    import os

    from ahub import selfupdate
    from ahub.tg import run as tgrun

    core.remember_chat(store, 7)
    comms.say(store, "T12 готова")
    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "idle")
    prints = iter(["v1", "v2"])
    monkeypatch.setattr(selfupdate, "code_fingerprint", lambda: next(prints, "v2"))
    monkeypatch.setattr(selfupdate, "new_code_healthy", lambda: (True, ""))
    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 0.0)
    execs = []

    def execve(exe, argv, env):
        execs.append((exe, argv, env))
        raise asyncio.CancelledError  # a real exec never returns

    monkeypatch.setattr(os, "execve", execve)

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", stop)
    bot = FakeBot()
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(bot, store)
    assert [text for _, text, _ in bot.sent] == ["T12 готова"]  # the message went before the restart
    assert [exe for exe, _, _ in execs] == [sys.executable]
    assert comms.outbox(store) == []


async def test_the_bot_restarts_onto_new_code_while_its_pass_fails(store, monkeypatch):
    """Tonight's incident: old code, a new DB column, the pass crash-loops — the check must run after a failed
    pass too, or the bot stays on the code that cannot read its own schema."""
    import asyncio
    import os

    from ahub import selfupdate
    from ahub.tg import run as tgrun

    def boom(*_a, **_kw):
        raise TypeError("no such column: task.new_column")

    monkeypatch.setattr(tgrun.comms, "outbox", boom)
    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 0.0)
    prints = iter(["v1", "v2"])
    monkeypatch.setattr(selfupdate, "code_fingerprint", lambda: next(prints, "v2"))
    checks = []

    def healthy():
        checks.append(1)
        return True, ""

    monkeypatch.setattr(selfupdate, "new_code_healthy", healthy)
    execs = []

    def execve(_exe, argv, _env):
        execs.append(argv)
        raise asyncio.CancelledError  # a real exec never returns

    monkeypatch.setattr(os, "execve", execve)

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(FakeBot(), store)
    assert checks == [1] and len(execs) == 1


async def test_the_bot_code_check_does_not_block_the_event_loop(store, monkeypatch):
    """new_code_healthy() runs a subprocess with a 60 s timeout: on the event loop it would freeze the bot for
    that whole time, so it goes to a thread and the loop keeps running the polling meanwhile."""
    import asyncio
    import os
    import time

    from ahub import selfupdate
    from ahub.tg import run as tgrun

    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "idle")
    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 0.0)
    prints = iter(["v1", "v2"])
    monkeypatch.setattr(selfupdate, "code_fingerprint", lambda: next(prints, "v2"))

    def slow_health():
        time.sleep(0.3)  # a subprocess that takes its time
        return True, ""

    monkeypatch.setattr(selfupdate, "new_code_healthy", slow_health)

    def execve(_exe, _argv, _env):
        raise asyncio.CancelledError

    monkeypatch.setattr(os, "execve", execve)
    ticks = []

    async def tick():
        await asyncio.sleep(0.05)
        ticks.append(1)

    asyncio.create_task(tick())
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(FakeBot(), store)
    assert ticks == [1], "the loop was busy inside the health check"


async def test_the_bot_stays_on_old_code_that_fails_the_check(store, monkeypatch):
    """New code that does not answer — the bot keeps the old one and is not checked again every CODE_CHECK_S."""
    import asyncio
    import os

    from ahub import selfupdate
    from ahub.tg import run as tgrun

    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "idle")
    prints = iter(["v1", "v2"])
    monkeypatch.setattr(selfupdate, "code_fingerprint", lambda: next(prints, "v2"))
    checks = []

    def unhealthy():
        checks.append(1)
        return False, "SyntaxError"

    monkeypatch.setattr(selfupdate, "new_code_healthy", unhealthy)
    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 0.0)
    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("restart onto broken code"))
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("restart onto broken code"))
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(FakeBot(), store)
    # three passes, and the new code was checked once: `code0 = code` remembered it as the code we stay on
    assert len(sleeps) == 3 and checks == [1] and sleeps == [tgrun.LOOP_S] * 3


async def test_the_bot_loop_crash_loop_is_reported_once_and_backs_off(store, monkeypatch, caplog):
    """The bot's loop failed for hours in silence — the failures are told once, in the log and to the owner
    (by hand: the pass that delivers alarms is the one that keeps failing), then retried slowly."""
    import asyncio
    import logging

    from ahub import selfupdate
    from ahub.i18n import t
    from ahub.model import Ev
    from ahub.tg import run as tgrun

    # the code check is not what this test is about
    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 3600.0)
    passes = []
    kinds = [TypeError, ValueError]  # a loop that alternates two kinds is still a failing loop

    def boom(*_a, **_kw):
        passes.append(1)
        raise kinds[(len(passes) - 1) % len(kinds)]("no such column: task.new_column")

    monkeypatch.setattr(tgrun.comms, "outbox", boom)
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) > tgrun.FAIL_MAX + 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", sleep)
    core.remember_chat(store, 7)
    bot = FakeBot()
    with caplog.at_level(logging.ERROR, logger="ahub.tg"), pytest.raises(asyncio.CancelledError):
        await tgrun.background(bot, store)
    assert passes == [1] * (tgrun.FAIL_MAX + 2)
    assert sleeps == [tgrun.LOOP_S] * (tgrun.FAIL_MAX - 1) + [tgrun.FAIL_BACKOFF_S] * 3
    alarms = [e for e in store.events() if e.kind == Ev.ALARM.value]
    assert len(alarms) == 1 and alarms[0].critical
    assert alarms[0].payload["text"] == t("tg.alarm_loop", err="TypeError: no such column: task.new_column")
    assert [text for _, text, _ in bot.sent] == [alarms[0].payload["text"]]  # one message, straight to the chat
    assert comms.alarms_for_tg(store) == []  # and the outbox must not send it a second time
    once = "failed %d times in a row" % tgrun.FAIL_MAX  # told once in the log — and once to the owner
    assert len([r for r in caplog.records if once in r.getMessage()]) == 1


async def test_a_restart_that_cannot_start_keeps_the_loop(store, monkeypatch):
    """An exec that cannot start (a fork limit, a busy binary) must not take the loop down: the next pass retries."""
    import asyncio
    import os

    from ahub import selfupdate
    from ahub.tg import run as tgrun

    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 0.0)
    prints = iter(["v1", "v2"])
    monkeypatch.setattr(selfupdate, "code_fingerprint", lambda: next(prints, "v2"))
    monkeypatch.setattr(selfupdate, "new_code_healthy", lambda: (True, ""))
    starts = []

    def busy(*_a):
        starts.append(1)
        raise OSError("text file busy")

    monkeypatch.setattr(os, "execve", busy)
    passes = []

    def boom(*_a, **_kw):
        passes.append(1)
        raise TypeError("no such column: task.new_column")

    monkeypatch.setattr(tgrun.comms, "outbox", boom)
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(FakeBot(), store)
    assert len(passes) == 3 and len(starts) == 3  # every pass tried, and every pass failed


async def test_a_refused_alarm_does_not_kill_the_loop(store, monkeypatch):
    """Even the alarm write can fail (a store that is broken in a new way): the loop must keep going, and the
    owner still gets the message in Telegram — the chats are read, not written."""
    import asyncio

    from ahub import selfupdate
    from ahub.i18n import t
    from ahub.tg import run as tgrun

    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 3600.0)
    passes = []

    def boom(*_a, **_kw):
        passes.append(1)
        raise TypeError("no such column: task.new_column")

    monkeypatch.setattr(tgrun.comms, "outbox", boom)

    def refuse(*_a, **_kw):
        raise RuntimeError("no such table: event")

    monkeypatch.setattr(tgrun.comms, "raise_alarm", refuse)
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) > tgrun.FAIL_MAX:
            raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", sleep)
    core.remember_chat(store, 7)
    bot = FakeBot()
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(bot, store)
    assert len(passes) == tgrun.FAIL_MAX + 1  # it kept looping
    assert [text for _, text, _ in bot.sent] == [t("tg.alarm_loop",
                                                 err="TypeError: no such column: task.new_column")]


def test_dispatcher_builds(store):
    from ahub.tg import run as tgrun
    assert tgrun.build_dispatcher(store) is not None


async def test_a_reply_to_a_question_forgets_its_key(store, monkeypatch):
    """The bot runs for weeks: the map of question messages must not grow one entry per question asked."""
    from types import SimpleNamespace

    from ahub.tg import run as tgrun

    dp = tgrun.build_dispatcher(store)
    handler = next(h.callback for h in dp.sub_routers[0].message.handlers if h.callback.__name__ == "_text")

    class FakeMsg:  # only what the handler reads: the chat, the replied-to message, the text
        def __init__(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
            self.chat = SimpleNamespace(id=chat_id)
            self.text = text
            self.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
            self.answers: list[str] = []

        async def answer(self, text: str, **_kw) -> None:
            self.answers.append(text)

    qid = comms.ask(store, "сливать?", ["да", "нет"])
    core.remember_question_message(store, 7, 1, qid)
    msg = FakeMsg(7, "да", reply_to=1)
    await handler(msg)
    assert msg.answers and comms.question(store, qid)["status"] == "answered"
    assert core.take_question_message(store, 7, 1) is None  # the entry is gone with the answer

    # a plain message in the same chat is not an answer — nothing is remembered
    plain = FakeMsg(7, "просто текст")
    await handler(plain)
    assert core.take_question_message(store, 7, 1) is None


async def test_the_bot_writes_no_heartbeat_of_its_own(store, monkeypatch):
    """`tg_heartbeat` was written with a monotonic clock and read by nobody — it is gone."""
    import asyncio

    from ahub.tg import run as tgrun

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", stop)
    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "idle")
    with pytest.raises(asyncio.CancelledError):
        await tgrun.background(FakeBot(), store)
    assert store.meta_get("tg_heartbeat") is None
    assert not hasattr(tgrun, "HEARTBEAT_KEY")


def test_chats_empty_and_fallback(store, tmp_path, monkeypatch):
    from ahub import config, paths
    from tests.conftest import write

    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    assert core.chats(store) == []
    write(paths.global_config_path(), "[telegram]\nchat_id = 77\n")
    assert core.chats(store) == [77]
    assert core.chats(store, hub=config.HubConfig()) == []
    assert core.chats(store, hub=config.HubConfig(tg_chat_id=78)) == [78]
    core.remember_chat(store, 42)
    assert core.chats(store) == [42]  # live chats beat the fallback


def test_bot_main_no_token(tmp_path, monkeypatch, capsys):
    from ahub.tg import run as tgrun

    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    rc = tgrun.main()
    assert rc == 2
    assert "токен" in capsys.readouterr().err.lower()


def test_build_session_proxy_pref(tmp_path, monkeypatch):
    from ahub import config
    from ahub.tg import run as tgrun

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    assert tgrun.build_session(config.HubConfig()) is None
    monkeypatch.setenv("HTTPS_PROXY", "http://sys:8080")
    s = tgrun.build_session(config.HubConfig())
    assert s is not None and s.proxy_url == "http://sys:8080"
    hub = config.HubConfig(tg_proxy="http://127.0.0.1:8080")
    assert tgrun.build_session(hub).proxy_url == "http://127.0.0.1:8080"
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    assert tgrun.build_session(hub).proxy_url == "http://127.0.0.1:8080"


def test_launcher_fast_death_keeps_messages(store, tmp_path):
    project = make_project(tmp_path)

    class Dead(Spawner):
        def __call__(self, cmd, cwd, log):
            open(log, "w").close()  # neither a session nor output
            p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
            p.wait()
            self.procs.append(p)
            return p.pid

    core.on_text(store, 1, "срочно", projects=[])
    sp = Dead()
    t0 = now_ms()
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude", now=t0) == "launched"
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude", now=t0 + 1000) == "finished"
    assert [m["text"] for m in comms.inbox(store, mark=False)] == ["срочно"]  # nothing lost
    assert launcher.tick(store, projects=[project], spawn=sp, binary="claude", now=t0 + 2000) == "launched"


def test_help_and_card_en(store, monkeypatch):
    """Step 4: the bot's /help and task card in English (AHUB_LANG=en)."""
    import re as _re

    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    help_en = core.help_text()
    assert "/tasks" in help_en and "/status" in help_en
    assert not _re.search(r"[а-яА-ЯёЁ]", help_en)
    tid = store.create_task(project="P", kind="code", title="pay button")
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, tid, st)
    rep = core.tasks_reply(store)
    assert "working and waiting" in rep.text and "recent" in rep.text
    card = core.task_detail(store, tid)
    assert "pay button" in card.text and "created" in card.text
    assert "State  done" in card.text
    assert not _re.search(r"[а-яА-ЯёЁ]", rep.text + card.text)
    assert card.buttons[0][0].data == "tasks"  # button codes are not translated
    _reset()


def test_alarms_for_tg_filters_non_alarms_and_sent_in_sql(store):
    """alarms_for_tg must only fetch and return unsent alarms, ignoring other events in SQL (Finding 3)."""
    t0 = 1000
    store.add_event(Ev.DONE, task_id=1, now=t0)
    store.add_event(Ev.ANSWER, payload={"text": "ok"}, now=t0)
    store.add_event(Ev.OWNER_MESSAGE, payload={"text": "hello"}, now=t0)

    # Plain alarm, fresh (not escalated yet)
    comms.raise_alarm(store, "disk warming", critical=False, now=t0)
    # Critical alarm, fresh (escalated at once)
    comms.raise_alarm(store, "database corrupt", critical=True, now=t0)
    # Already sent alarm
    sent_id = comms.raise_alarm(store, "network down", critical=True, now=t0)
    comms.mark_tg_sent(store, [sent_id], now=t0)

    # Right after creation (t0): only the unsent critical alarm is returned
    due = comms.alarms_for_tg(store, now=t0)
    assert len(due) == 1
    assert due[0].payload["text"] == "database corrupt"

    # After escalate_ms: the plain alarm is returned too
    due_later = comms.alarms_for_tg(store, now=t0 + comms.ESCALATE_MS + 1)
    assert len(due_later) == 2
    assert {e.payload["text"] for e in due_later} == {"database corrupt", "disk warming"}

    # Mark them sent: nothing due
    comms.mark_tg_sent(store, [e.id for e in due_later], now=t0 + comms.ESCALATE_MS + 1)
    assert comms.alarms_for_tg(store, now=t0 + comms.ESCALATE_MS + 1) == []


async def test_startup_fingerprint_failure_keeps_loop(store, monkeypatch, caplog):
    """An OSError in the startup fingerprint must not kill background(): log once, retry on the next tick."""
    import asyncio
    import logging

    from ahub import selfupdate
    from ahub.tg import run as tgrun

    core.remember_chat(store, 7)
    comms.say(store, "hello")
    monkeypatch.setattr(tgrun.launcher, "tick", lambda *a, **kw: "idle")
    monkeypatch.setattr(selfupdate, "CODE_CHECK_S", 3600.0)
    calls = []

    def fp():
        calls.append(1)
        raise OSError("disk busy")

    monkeypatch.setattr(selfupdate, "code_fingerprint", fp)
    sleeps = []

    async def sleep(_s):
        sleeps.append(1)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(tgrun.asyncio, "sleep", sleep)
    bot = FakeBot()
    with caplog.at_level(logging.ERROR, logger="ahub.tg"):
        with pytest.raises(asyncio.CancelledError):
            await tgrun.background(bot, store)
    assert [t for _, t, _ in bot.sent] == ["hello"]  # the pass ran despite the fingerprint failure
    assert len(calls) == 3  # retried on every tick
    assert len([r for r in caplog.records if "code fingerprint failed" in r.getMessage()]) == 1


def test_nodir_report_survives_reexec(store, tmp_path, caplog):
    """No in-memory dict: the second tick (a re-exec on the same store) does not re-announce."""
    a, _b = two_projects(tmp_path)
    comms.owner_message(store, "по Z: в никуда", project="Z")
    sp = Spawner()
    try:
        with caplog.at_level("WARNING", logger="ahub.launcher"):
            assert launcher.tick(store, projects=[a], spawn=sp, binary="claude") == "nodir:Z"
            # a new process, same store — the announcement is not repeated
            assert launcher.tick(store, projects=[a], spawn=sp, binary="claude") == "idle"
        assert [r.message for r in caplog.records] == [
            "cannot launch Claude: no directory for project Z — no such project in the hub config"]
    finally:
        for p in sp.procs:
            try:
                p.kill()
            except Exception:
                pass


async def test_question_reply_survives_reexec(store):
    """The (chat, bot_msg_id) → qid mapping lives in meta: a reply after a re-exec still answers."""
    from types import SimpleNamespace

    from ahub.tg import run as tgrun

    core.remember_chat(store, 7)
    qid = comms.ask(store, "сливать?", ["да", "нет"])
    # what background() does when it sends the question: remember the bot message id in the store
    core.remember_question_message(store, 7, 41, qid)
    core.mark_question_sent(store, qid)

    # a re-exec: no process memory survives, only the store — a new dispatcher on the same store
    dp = tgrun.build_dispatcher(store)
    handler = next(h.callback for h in dp.sub_routers[0].message.handlers if h.callback.__name__ == "_text")

    class FakeMsg:
        def __init__(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
            self.chat = SimpleNamespace(id=chat_id)
            self.text = text
            self.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
            self.answers: list[str] = []

        async def answer(self, text: str, **_kw) -> None:
            self.answers.append(text)

    msg = FakeMsg(7, "да", reply_to=41)
    await handler(msg)
    assert msg.answers and comms.question(store, qid)["status"] == "answered"
    assert core.take_question_message(store, 7, 41) is None


def test_answered_question_forgets_other_chats_mappings(store):
    """One answer closes the question for every chat: no stale mapping survives a re-exec."""
    qid = comms.ask(store, "сливать?", ["да", "нет"])
    core.remember_question_message(store, 7, 1, qid)
    core.remember_question_message(store, 8, 2, qid)
    assert "передал" in core.on_answer_button(store, f"ans:{qid}:1")
    assert core.take_question_message(store, 7, 1) is None
    assert core.take_question_message(store, 8, 2) is None

