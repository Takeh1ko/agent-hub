"""Console K2: commands, confirmations, draft flow, completion, a/x/r/m keys."""

from __future__ import annotations

import pytest

from ahub import drafts
from ahub.store import Store
from ahub.time import now_ms
from ahub.tui import console as con
from ahub.tui.console import ConsoleApp, complete_input


@pytest.fixture
def store() -> Store:
    return Store()


def _task(store: Store, project: str = "P", title: str = "t"):
    return store.create_task(project=project, kind="scout", title=title)


def _proj(monkeypatch, name: str = "P", root: str = "/tmp"):
    from ahub import config

    cfg = config.ProjectConfig(name=name, root=root)
    monkeypatch.setattr(config, "load_projects", lambda hub=None: ([cfg], []))
    return cfg


def _transcript(app: ConsoleApp) -> str:
    return "\n".join(app._transcript)


async def _confirm_name(app: ConsoleApp, pilot, wait: float = 0.3) -> str:
    await pilot.pause(wait)
    return app.screen.__class__.__name__


def test_unknown_has_cross_and_help_hint(store: Store):
    app = ConsoleApp(store=store, all_projects=True)
    app.run_command("/frobnicate")
    text = _transcript(app)
    assert "✗" in text
    assert "/help" in text


def test_complete_command_names():
    assert complete_input("/ac", []) == "/accept "
    assert complete_input("/sta", []) == "/status "
    assert complete_input("/ST", []) in ("/stop ", "/status ")
    assert complete_input("plain text", []) is None
    assert complete_input("/", []) is None


def test_complete_task_ids():
    assert complete_input("/accept ", ["T12", "T13"]) == "/accept T12"
    assert complete_input("/accept T1", ["T12", "T13"]) == "/accept T12"
    assert complete_input("/accept T13", ["T12", "T13"]) == "/accept T13"
    assert complete_input("/status 2", ["T12", "T23"]) == "/status T23"
    assert complete_input("/cost ", ["T12"]) is None  # not a task command


async def test_accept_calls_function_and_confirms(store: Store, monkeypatch):
    _proj(monkeypatch)
    tid = _task(store)
    called: dict = {}

    def _fake(s, p, i, *, by=""):
        called.update(store=s, project=p, tid=i, by=by)
        return "T1 accepted"

    monkeypatch.setattr(con.accept, "accept", _fake)
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command(f"/accept T{tid}")
        assert await _confirm_name(app, pilot) == "Confirm"
        await pilot.press("y")
        await pilot.pause(0.4)
        assert called.get("tid") == tid
        assert called.get("by") == "human"
        assert "⏺" in _transcript(app)
        assert "T1 accepted" in _transcript(app)


async def test_accept_no_cancels_without_call(store: Store, monkeypatch):
    _proj(monkeypatch)
    tid = _task(store)
    called: dict = {}

    def _fake(*a, **k):
        called["yes"] = True
        return "ok"

    monkeypatch.setattr(con.accept, "accept", _fake)
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command(f"/accept T{tid}")
        assert await _confirm_name(app, pilot) == "Confirm"
        await pilot.press("n")
        await pilot.pause(0.4)
        assert "yes" not in called
        assert "cancelled" in _transcript(app).lower() or "отмен" in _transcript(app).lower()


async def test_reject_rework_stop_nudge_model_budget(store: Store, monkeypatch):
    _proj(monkeypatch)
    tid = _task(store)
    hits: dict = {}

    monkeypatch.setattr(con.accept, "reject",
                        lambda s, p, i, **k: hits.setdefault("reject", (i, k)) or "rejected")
    monkeypatch.setattr(con.accept, "rework",
                        lambda s, i, n, **k: hits.setdefault("rework", (i, n)) or "reworked")
    monkeypatch.setattr(con.transitions, "request_stop",
                        lambda s, i, **k: hits.setdefault("stop", i) or "stopped")
    monkeypatch.setattr(con.transitions, "request_nudge",
                        lambda s, i, **k: hits.setdefault("nudge", (i, k.get("text"))) or "requested")
    monkeypatch.setattr(con.accept, "change_model",
                        lambda s, p, i, a, **k: hits.setdefault("model", (i, a)) or "model changed")
    monkeypatch.setattr(con.accept, "extend_budget",
                        lambda s, i, **k: hits.setdefault("budget", (i, k.get("add"))) or "budget ok")

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        for cmd in (f"/reject T{tid} oops", f"/rework T{tid} fix it",
                    f"/stop T{tid}", f"/nudge T{tid} hello",
                    f"/model T{tid} spark", f"/budget T{tid} +0.5"):
            app.run_command(cmd)
            assert await _confirm_name(app, pilot) == "Confirm", cmd
            await pilot.press("y")
            await pilot.pause(0.4)
        assert hits["reject"][0] == tid
        assert hits["rework"] == (tid, "fix it")
        assert hits["stop"] == tid
        assert hits["nudge"] == (tid, "hello")
        assert hits["model"] == (tid, "spark")
        assert hits["budget"] == (tid, 0.5)
        text = _transcript(app)
        assert text.count("⏺") >= 6


async def test_rework_without_notes_asks(store: Store, monkeypatch):
    _proj(monkeypatch)
    tid = _task(store)
    hits: dict = {}
    monkeypatch.setattr(con.accept, "rework",
                        lambda s, i, n, **k: hits.setdefault("rework", n) or "reworked")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command(f"/rework T{tid}")
        assert await _confirm_name(app, pilot) == "Ask"


async def test_inbox_questions_alarms_models_providers_projects_cost(store: Store, monkeypatch):
    from ahub import comms

    _proj(monkeypatch)
    _task(store)
    comms.raise_alarm(store, "boom", critical=True)
    comms.ask(store, "merge?", ["yes", "no"], project="P")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command("/inbox")
        app.run_command("/questions")
        app.run_command("/alarms")
        app.run_command("/models")
        app.run_command("/providers")
        app.run_command("/projects")
        app.run_command("/cost")
        await pilot.pause(0.5)
        text = _transcript(app)
        assert len(app._transcript) >= 5
        assert "🚨" in text or "alarm" in text.lower() or "тревог" in text.lower() or "no alarms" in text.lower()


async def test_draft_plain_text_preview_start_and_cancel(store: Store, monkeypatch, tmp_path):
    import json as _json

    from ahub import config

    root = tmp_path / "P"
    root.mkdir()
    (root / ".hub.toml").write_text('schema_version = 2\nname = "P"\n', encoding="utf-8")
    proj = config.load_project(root)
    monkeypatch.setattr(config, "load_projects", lambda hub=None: ([proj], []))
    # a ready draft the fake model "made"
    task_json = _json.dumps({"project": "P", "kind": "scout", "title": "find leak",
                             "spec": "look", "result_format": ""})
    with store.tx() as c:
        did = int(c.execute(
            "INSERT INTO draft(ts, project, text, task_json, status) VALUES(?,?,?,?,?)",
            (now_ms(), "P", "find leak", task_json, drafts.READY)).lastrowid)
    monkeypatch.setattr(con.drafts, "create", lambda s, p, t, **k: did)
    app = ConsoleApp(store=store, project="P")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command("find the leak")
        await pilot.pause(1.0)
        text = _transcript(app)
        assert "✻" in text  # Drafting… status line
        assert f"/start {did}" in text  # preview + start hint
        # start it: yes → queued
        app.run_command(f"/start {did}")
        assert await _confirm_name(app, pilot) == "Confirm"
        await pilot.press("y")
        await pilot.pause(0.5)
        assert "queued" in _transcript(app).lower() or "очереди" in _transcript(app).lower()
        # cancel path: another ready draft, start → no → cancelled
        task_json2 = _json.dumps({"project": "P", "kind": "scout", "title": "other",
                                  "spec": "x", "result_format": ""})
        with store.tx() as c:
            did2 = int(c.execute(
                "INSERT INTO draft(ts, project, text, task_json, status) VALUES(?,?,?,?,?)",
                (now_ms(), "P", "other", task_json2, drafts.READY)).lastrowid)
        app.run_command(f"/start {did2}")
        assert await _confirm_name(app, pilot) == "Confirm"
        await pilot.press("n")
        await pilot.pause(0.4)
        assert drafts.status(store, did2) == "cancelled"


async def test_draft_matches_by_status_code_not_text(store: Store, monkeypatch, tmp_path):
    """Preview quoting ': failed' with READY status still offers the start hint."""
    import json as _json

    from ahub import config

    root = tmp_path / "Q"
    root.mkdir()
    (root / ".hub.toml").write_text('schema_version = 2\nname = "Q"\n', encoding="utf-8")
    proj = config.load_project(root)
    monkeypatch.setattr(config, "load_projects", lambda hub=None: ([proj], []))
    task_json = _json.dumps({"project": "Q", "kind": "scout", "title": "t",
                             "spec": "fix failed in x", "result_format": ""})
    with store.tx() as c:
        did = int(c.execute(
            "INSERT INTO draft(ts, project, text, task_json, status) VALUES(?,?,?,?,?)",
            (now_ms(), "Q", "x", task_json, drafts.READY)).lastrowid)
    app = ConsoleApp(store=store, project="Q")
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        preview = drafts.preview(store, did)
        assert "failed" in preview
        app._offer_draft(proj, did, preview, drafts.status(store, did))
        await pilot.pause(0.2)
        assert f"/start {did}" in _transcript(app)
        # a failed draft with the same words shows an error, no start hint
        with store.tx() as c:
            bad = int(c.execute(
                "INSERT INTO draft(ts, project, text, task_json, status, errors) VALUES(?,?,?,?,?,?)",
                (now_ms(), "Q", "x", "{}", "failed", "boom")).lastrowid)
        app._offer_draft(proj, bad, drafts.preview(store, bad), drafts.status(store, bad))
        await pilot.pause(0.2)
        assert f"/start {bad}" not in _transcript(app)
        assert "✗" in _transcript(app)


async def test_tasks_pane_axrm_keys(store: Store, monkeypatch):
    from ahub import transitions

    _proj(monkeypatch)
    tids = [_task(store, title=f"t{i}") for i in range(2)]
    for tid in tids:
        transitions.move(store, tid, "preparing")
    monkeypatch.setattr(con.accept, "accept", lambda s, p, i, **k: "ok")
    app = ConsoleApp(store=store, all_projects=True, control=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.6)
        assert app.focus_mode == "tasks"
        assert len(app._task_ids) >= 2
        app._selected = 0
        first = app._task_ids[0]
        # 'a' on the selected block opens the accept confirm
        from unittest.mock import Mock

        ev = Mock()
        ev.key = "a"
        app.on_key(ev)
        await pilot.pause(0.3)
        assert app.screen.__class__.__name__ == "Confirm"
        await pilot.press("n")
        await pilot.pause(0.3)
        assert first in app._task_ids
