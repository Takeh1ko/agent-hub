"""H05 TG-пульт: core без сети + обвязка dry-run."""

from __future__ import annotations

import json
import sqlite3

from hub.bot import core as bc
from hub.cli import main
from hub.read.snapshot import SessionSnap, Snapshot, TaskSnap
from hub.store import Store

NOW = 1_789_000_000_000


def _con(store: Store):
    con = sqlite3.connect(str(store.path))
    con.row_factory = sqlite3.Row
    return con


def _set_listen(store: Store, ts: int) -> None:
    bc.meta_set(store, "claude_listen_ts", str(ts))


def _inbox_rows(store: Store) -> list[dict]:
    con = _con(store)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM inbox ORDER BY id")]
    finally:
        con.close()


def _events(store: Store) -> list[dict]:
    return store.events_since(0)


def _add_question(store: Store, text="идём?", opts=None, task_id="T1") -> int:
    con = _con(store)
    try:
        cur = con.execute(
            "INSERT INTO question(task_id, asked_by, text, options_json,"
            " status, answer, answered_via, ts)"
            " VALUES (?, 'claude', ?, ?, 'open', '', '', ?)",
            (task_id, text, json.dumps(opts or [], ensure_ascii=False), NOW),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _add_outbox(store: Store, text="привет", task_id="") -> int:
    con = _con(store)
    try:
        cur = con.execute(
            "INSERT INTO outbox(ts, text, task_id, sent_ts) VALUES (?, ?, ?, NULL)",
            (NOW, text, task_id),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def test_migration_003_tg_chat(tmp_path):
    s = Store(tmp_path / "hub.db")
    con = sqlite3.connect(str(s.path))
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "tg_chat" in tables
    finally:
        con.close()
    from pathlib import Path

    mig = (Path(__file__).resolve().parent.parent
           / "hub" / "migrations" / "003_bot.sql").read_text(encoding="utf-8")
    con = sqlite3.connect(str(s.path))
    try:
        con.executescript(mig)
        con.executescript(mig)
    finally:
        con.close()
    Store(tmp_path / "hub.db")


def test_owner_text_fresh():
    s = Store()
    _set_listen(s, NOW - 2 * 60_000)
    reply = bc.handle_owner_text(s, 111, "привет, Claude", NOW)
    assert "Передал" in reply and "Claude" in reply
    rows = _inbox_rows(s)
    assert len(rows) == 1 and rows[0]["text"] == "привет, Claude"
    assert rows[0]["source"] == "tg"
    kinds = [e["kind"] for e in _events(s)]
    assert "owner_message" in kinds
    assert 111 in bc.list_chats(s)


def test_owner_text_stale():
    s = Store()
    _set_listen(s, NOW - 25 * 60_000)
    reply = bc.handle_owner_text(s, 222, "где ты?", NOW)
    assert "не слушает" in reply and "25 мин" in reply
    assert "очереди" in reply


def test_owner_text_never_listened():
    s = Store()
    reply = bc.handle_owner_text(s, 333, "ау?", NOW)
    assert "не слушает" in reply and "очереди" in reply


def test_all_chats_includes_owner():
    from hub.tg_send import OWNER_CHAT_ID

    s = Store()
    bc.remember_chat(s, 555, NOW)
    bc.remember_chat(s, 555, NOW + 1000)  # дубль не плодится
    chats = bc.all_chats(s)
    assert 555 in chats and int(OWNER_CHAT_ID) in chats
    assert chats.count(555) == 1


def test_outbox_once():
    s = Store()
    _add_outbox(s, "раз")
    _add_outbox(s, "два")
    sent: list[str] = []
    n = bc.drain_outbox(s, lambda row: sent.append(row["text"]), NOW)
    assert n == 2 and sent == ["раз", "два"]
    # Второй проход — тишина, ровно один раз.
    sent2: list[str] = []
    assert bc.drain_outbox(s, lambda row: sent2.append(row["text"]), NOW) == 0
    assert sent2 == []
    con = _con(s)
    try:
        left = con.execute(
            "SELECT COUNT(*) FROM outbox WHERE sent_ts IS NULL").fetchone()[0]
    finally:
        con.close()
    assert left == 0


def test_question_buttons_and_answer():
    s = Store()
    qid = _add_question(s, "продлить?", ["да", "нет"], task_id="T1")
    row = bc.get_question(s, qid)
    assert row is not None and row["status"] == "open"
    text, buttons = bc.format_question(row)
    assert "продлить?" in text
    assert [b.label for b in buttons] == ["да", "нет"]
    assert buttons[0].data == f"qans:{qid}:0"
    ok, edited = bc.answer_by_callback(s, f"qans:{qid}:1", now_ms=NOW)
    assert ok and "нет" in edited and "Ответ:" in edited
    fresh = bc.get_question(s, qid)
    assert fresh["status"] == "answered" and fresh["answer"] == "нет"
    assert fresh["answered_via"] == "tg"
    kinds = [e["kind"] for e in _events(s)]
    assert "answer" in kinds
    # Повтор — без дубля.
    before = len(_events(s))
    ok2, _ = bc.answer_by_callback(s, f"qans:{qid}:0", now_ms=NOW)
    assert not ok2 and len(_events(s)) == before


def test_question_no_options_reply_hint():
    s = Store()
    qid = _add_question(s, "своими словами?", [], task_id="T2")
    row = bc.get_question(s, qid)
    assert row is not None
    text, buttons = bc.format_question(row)
    assert buttons == [] and "Ответить текстом" in text
    # Ответ реплаем (прямой вызов core).
    assert bc.answer_question(s, qid, "моё решение", now_ms=NOW)
    assert bc.get_question(s, qid)["answer"] == "моё решение"


def test_grouping_three_events_one_message():
    evs = [
        {"kind": "ready", "task_id": "H01", "payload_json": "{}"},
        {"kind": "failed", "task_id": "H02", "payload_json": "{}"},
        {"kind": "stuck", "task_id": "H03", "payload_json": "{}"},
    ]
    text = bc.format_grouped(evs)
    assert text.count("\n") >= 3  # заголовок + 3 строки, но одно сообщение
    assert "H01" in text and "H02" in text and "H03" in text
    assert len(text) <= bc.MSG_LIMIT
    # Окно 5 мин: раньше не готово, после — готово.
    g = bc.Grouper()
    for e in evs:
        g.add(e, NOW)
    assert not g.ready(NOW + 60_000)
    assert g.ready(NOW + 5 * 60_000)
    one = g.flush()
    assert "H01" in one and "H03" in one and g.buf == []


def _fake_snapshot() -> Snapshot:
    ss = [
        SessionSnap("s1", "executor", "muse", "opencode-go",
                    NOW, "🟢", 0.11, True, 5000, "bash: pytest"),
        SessionSnap("s2", "reviewer", "mimoflash", "zen",
                    NOW, "🟡", 0.02, False, 1000, "думает"),
    ]
    tasks = [TaskSnap("H01", "PlayerUP", "exec r1", 1, "🟢",
                      0.11, 0.02, 5000, "bash: pytest", ss)]
    return Snapshot(tasks=tasks, total_go=0.11, total_usd=0.02, now_ms=NOW)


def test_roster_fake_snapshot():
    text = bc.format_roster(_fake_snapshot())
    assert "<pre>" in text
    for needle in ("muse", "executor", "H01", "exec r1", "🟢"):
        assert needle in text
    assert "лимит Go" in text and "$60" in text
    assert len(text) <= bc.MSG_LIMIT


def test_merge_confirm_two_steps():
    s = Store()
    s.upsert_task(id="H01", stage="ready")
    # Без подтверждения ничего не пишет.
    before = len(_events(s))
    text, buttons = bc.confirm_text("merge", "H01"), bc.confirm_buttons("H01", "H01")
    assert "H01" in text and len(buttons) == 2
    parsed = bc.parse_confirm("confirm:merge:H01:no")
    assert parsed == ("merge", "H01", False)
    assert bc.apply_confirm(s, "merge", "H01", False, NOW) == "Отменено."
    assert len(_events(s)) == before
    # С подтверждением — событие owner_command.
    parsed2 = bc.parse_confirm("confirm:merge:H01:yes")
    assert parsed2 == ("merge", "H01", True)
    reply = bc.apply_confirm(s, "merge", "H01", True, NOW)
    assert "передано Claude" in reply
    got = [e for e in _events(s) if e["kind"] == "owner_command"]
    assert len(got) == 1
    assert json.loads(got[0]["payload_json"])["action"] == "merge"


def test_stop_confirm_and_unknown_task():
    s = Store()
    assert "нет задачи" in bc.apply_confirm(s, "stop", "НЕТ", True, NOW)
    assert "Не знаю" in bc.apply_confirm(s, "drop", "H01", True, NOW)


def test_task_format_limit(tmp_path):
    s = Store()
    wt = tmp_path / "wt"
    wt.mkdir()
    agent = wt / ".agent"
    agent.mkdir()
    (agent / "review_r1_muse.json").write_text(json.dumps(
        {"findings": [{"file": "a.py", "line": 1, "issue": "баг",
                       "severity": "high", "author": "muse"}]}),
        encoding="utf-8")
    s.upsert_task(id="H09", stage="exec r1", round=1, worktree=str(wt))
    s.link_session("sx", "opencode", "H09", "executor", 1, "muse")
    text = bc.format_task(s, "H09")
    assert "H09" in text and "exec r1" in text and "executor" in text
    assert "a.py" in text
    assert len(text) <= 4000
    assert bc.format_task(s, "НЕТ") == "нет задачи НЕТ"


def test_budget_pause_resume():
    s = Store()
    assert "Бюджет" in bc.format_budget(s)
    assert not bc.is_paused(s)
    assert "паузе" in bc.set_paused(s, True)
    assert bc.is_paused(s)
    assert "пауза" in bc.format_budget(s)
    assert "запущена" in bc.set_paused(s, False)
    assert not bc.is_paused(s)


def test_clip_and_split():
    assert len(bc.clip("x" * 5000)) <= bc.MSG_LIMIT
    assert bc.split_command("/task H01") == ("task", "H01")
    assert bc.split_command("просто текст")[0] == ""
    assert bc.esc("<a>&") == "&lt;a&gt;&amp;"


def test_new_questions_dedup():
    s = Store()
    qid = _add_question(s, "раз?", ["да"])
    assert [q["id"] for q in bc.new_questions_to_send(s)] == [qid]
    bc.mark_question_sent(s, qid)
    assert bc.new_questions_to_send(s) == []


def test_dry_run_builds_dispatcher(capsys):
    assert main(["bot", "--dry-run"]) == 0
    assert "dry-run ok" in capsys.readouterr().out


def test_build_dispatcher_no_network():
    from hub.bot.run import build_dispatcher

    dp = build_dispatcher()
    from aiogram import Dispatcher

    assert isinstance(dp, Dispatcher)
