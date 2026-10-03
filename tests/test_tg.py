"""TG v2: bot logic (no network) and launching Claude when there is no live session."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from ahub import comms, events, transitions
from ahub.model import State
from ahub.scope import Scope
from ahub.store import Store
from ahub.tg import core, launcher
from ahub.time import now_ms
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
    with store.tx() as c:
        c.execute("DELETE FROM presence")
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
    time.sleep(0.5)
    assert sp.procs[0].poll() is not None


def test_project_choice(store, tmp_path):
    a = make_project(tmp_path / "a")
    import dataclasses
    b = dataclasses.replace(make_project(tmp_path / "b"), name="B")
    events.touch(store, project="B")
    with store.tx() as c:
        c.execute("UPDATE presence SET last_seen=0")  # was there, but long ago — not "present"
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
    assert core.current_project(store) == "B"  # the prefix is a pick
    rep = core.on_text(store, 1, "второе", projects=names)
    assert "проект: B" in rep.text
    assert [m["project"] for m in comms.inbox(store, mark=False)] == ["", "B", "B"]

    listed = core.project_reply(store, names)
    assert "сейчас проект: B" in listed.text
    assert [b.data for row in listed.buttons for b in row] == ["proj:P", "proj:B", "proj:all"]
    assert core.project_reply(store, names).buttons[1][0].label == "✓ B"

    assert "проект: P" in core.project_reply(store, names, "P").text  # /project P
    assert core.current_project(store) == "P"
    assert "нет" in core.project_reply(store, names, "нет такого").text
    assert core.current_project(store) == "P"  # an unknown project changes nothing


def test_a_pick_can_be_cleared(store):
    """`/project all` — back to the hub, so a plain message goes to the owner again."""
    names = ["P", "B"]
    core.on_text(store, 1, "по B: первое", projects=names)
    assert core.current_project(store) == "B"
    assert "проект: B" in core.on_text(store, 1, "второе", projects=names).text

    assert "не выбран" in core.project_reply(store, names, "all").text
    assert core.current_project(store) == ""
    assert [b.data for row in core.project_reply(store, names).buttons for b in row] == ["proj:P", "proj:B"]
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
    monkeypatch.setattr(tgrun.launcher, "tick", lambda s: "idle")

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


def test_dispatcher_builds(store):
    from ahub.tg import run as tgrun
    assert tgrun.build_dispatcher(store) is not None


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
