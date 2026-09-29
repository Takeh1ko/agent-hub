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


def test_owner_empty_text_no_write():
    s = Store()
    before_inbox = len(_inbox_rows(s))
    before_ev = len(_events(s))
    reply = bc.handle_owner_text(s, 999, "   ", NOW)
    assert "Пустое" in reply or "пустое" in reply
    assert len(_inbox_rows(s)) == before_inbox
    assert len(_events(s)) == before_ev
    # Чат запомнился даже для пустого.
    assert 999 in bc.list_chats(s)


def test_all_chats_includes_owner():
    from hub.tg_send import OWNER_CHAT_ID

    s = Store()
    bc.remember_chat(s, 555, NOW)
    bc.remember_chat(s, 555, NOW + 1000)  # дубль не плодится
    chats = bc.all_chats(s)
    assert 555 in chats and int(OWNER_CHAT_ID) in chats
    assert chats.count(555) == 1


def test_outbox_primitives_order_and_mark():
    """Примитивы outbox: забор по id, пометка; доставка — через outbox_once.

    Прод шлёт рассылкой по чатам через run.outbox_once (per-chat ретрай,
    тесты в test_bot_poll.py); здесь — порядок и пометки без сети.
    """
    s = Store()
    _add_outbox(s, "раз")
    _add_outbox(s, "два")
    rows = bc.fetch_outbox(s)
    assert [r["text"] for r in rows] == ["раз", "два"]  # порядок — по id
    bc.mark_outbox_sent(s, int(rows[0]["id"]), NOW)
    left = [r["text"] for r in bc.fetch_outbox(s)]
    assert left == ["два"], "повтор забирает только непомеченную"


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
        assert g.add({**e, "id": 100 + len(g.buf)}, NOW)
    assert not g.ready(NOW + 60_000)
    assert g.ready(NOW + 5 * 60_000)
    # Дедуп: повторный забор тех же id не дублирует буфер.
    assert not g.add({"id": 100, "kind": "ready",
                      "task_id": "H01", "payload_json": "{}"}, NOW)
    assert len(g.buf) == 3
    # peek не очищает (отправка может упасть), clear — после успеха.
    assert "H01" in g.peek() and len(g.buf) == 3
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
    # Формат для владельца: роль по-русски, этап словами (hub/read/snapshot.roster_text).
    for needle in ("Spark Go пишет код", "MiMo Flash проверяет код", "H01",
                   "Пишет код (круг 1", "🟢"):
        assert needle in text
    assert "из лимита $60/мес" in text
    assert len(text) <= bc.MSG_LIMIT


def test_merge_confirm_two_steps():
    s = Store()
    s.upsert_task(id="H01", stage="ready")
    # Без подтверждения ничего не пишет.
    before = len(_events(s))
    text, buttons = bc.confirm_text("merge", "H01"), bc.confirm_buttons("merge", "H01")
    assert "H01" in text and len(buttons) == 2
    assert [b.data for b in buttons] == [
        "confirm:merge:H01:yes", "confirm:merge:H01:no"]
    for act in ("stop", "merge"):
        btns = bc.confirm_buttons(act, "H09")
        for b in btns:
            parsed = bc.parse_confirm(b.data)
            assert parsed is not None and parsed[0] == act and parsed[1] == "H09"
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


def test_merge_only_ready_stop_not_final():
    s = Store()
    s.upsert_task(id="HX", stage="exec r1")
    before = len(_events(s))
    assert "только для ready" in bc.apply_confirm(s, "merge", "HX", True, NOW)
    assert len(_events(s)) == before  # ничего не записало
    s.upsert_task(id="HF", stage="merged")
    assert "финаль" in bc.apply_confirm(s, "stop", "HF", True, NOW)
    assert "передано Claude" in bc.apply_confirm(s, "stop", "HX", True, NOW)


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
    s.upsert_task(id="H09", stage="exec r1", round=1, worktree=str(wt),
                  budget_go=0.5, budget_usd=1.0)
    s.link_session("sx", "opencode", "H09", "executor", 1, "muse")
    text = bc.format_task(s, "H09", snapshot=_fake_snapshot_for("H09"))
    # Для владельца — словами, как на экране top: этап, кто работает, $, замечания круга.
    assert "H09" in text and "Сейчас: Пишет код" in text and "Spark Go" in text
    assert "Замечания проверки (круг 1):" in text and "a.py" in text
    assert "Потрачено: $" in text and "Бюджет задачи: Go $0.50" in text
    assert "exec r1" not in text and "executor" not in text
    assert len(text) <= 3500 and text.endswith("</pre>")
    assert bc.format_task(s, "НЕТ") == "нет задачи НЕТ"


def _fake_snapshot_for(tid: str) -> Snapshot:
    ss = [SessionSnap("sx", "executor", "muse", "opencode-go",
                      NOW, "🟢", 0.11, True, 5000, "bash: pytest")]
    tasks = [TaskSnap(tid, "PlayerUP", "exec r1", 1, "🟢",
                      0.11, 0.02, 5000, "bash: pytest", ss)]
    return Snapshot(tasks=tasks, total_go=0.11, total_usd=0.02, now_ms=NOW)


def test_task_wrap_esc_limit():
    """Раздув esc не ломает лимит 3500 и </pre>."""
    text = bc._wrap_pre("<" * 3400, 3500)
    assert len(text) <= 3500 and text.endswith("</pre>")
    assert "&lt;" in text


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


def test_answer_race_single_event():
    """Параллельные ответы — один True и одно событие answer."""
    import threading

    s = Store()
    qid = _add_question(s, "гонка?", ["да", "нет"])
    results = []
    lock = threading.Lock()

    def _try(i):
        ok = bc.answer_question(s, qid, f"вариант{i}", now_ms=NOW)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=_try, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1
    assert len([e for e in _events(s) if e["kind"] == "answer"]) == 1


def test_fetch_sql_filters_kinds():
    """Ненотифицируемые висят в БД, но забор отдаёт только NOTIFY_KINDS."""
    s = Store()
    s.add_event("T1", "owner_message", {"text": "x"})
    s.add_event("T1", "answer", {})
    s.add_event("T1", "owner_command", {"action": "stop"})
    eid = s.add_event("T9", "ready", {})
    got = bc.fetch_unsent_notifiable(s)
    assert [e["id"] for e in got] == [eid]


def test_snapshot_events_and_budget():
    """Дифф этапов/пульса + эскалация бюджета только вверх."""
    prev = _fake_snapshot()
    cur = _fake_snapshot()
    assert bc.snapshot_events(None, cur) == []  # baseline молчит
    assert bc.snapshot_events(prev, cur) == []
    cur.tasks[0].stage = "ready"
    kinds = [e["kind"] for e in bc.snapshot_events(prev, cur)]
    assert kinds == ["ready"]
    cur.tasks[0].stage = "exec r1"
    cur.tasks[0].pulse = "🔴"
    kinds = [e["kind"] for e in bc.snapshot_events(prev, cur)]
    assert kinds == ["stuck"]
    # Бюджет: hard один раз, повтор и soft после hard — тишина.
    s = Store()
    s.upsert_task(id="H01", stage="exec r1", budget_go=0.5, budget_usd=0.0)
    snap = _fake_snapshot()
    snap.tasks[0].cost_go = 0.45
    assert [e["kind"] for e in bc.pop_budget_events(s, snap)] == ["budget_soft"]
    assert bc.pop_budget_events(s, snap) == []
    snap.tasks[0].cost_go = 0.6
    assert [e["kind"] for e in bc.pop_budget_events(s, snap)] == ["budget_hard"]
    assert bc.pop_budget_events(s, snap) == []


def test_snapshot_key_ignores_now():
    a = _fake_snapshot()
    b = _fake_snapshot()
    b.now_ms = a.now_ms + 3600_000
    assert bc.snapshot_key(a) == bc.snapshot_key(b)


def test_parse_question_ref():
    assert bc.parse_question_ref("❓ Вопрос #12 [T1]:\nтекст") == 12
    assert bc.parse_question_ref("реплай без номера") is None


def _snap_stage(stage: str, pulse: str, sessions: object = "one") -> Snapshot:
    ss = [] if sessions is None else [SessionSnap(
        "s1", "executor", "muse", "opencode-go",
        NOW, pulse, 0.11, True, 5000, "bash: pytest")]
    return Snapshot(
        tasks=[TaskSnap("H01", "P", stage, 1, pulse, 0.0, 0.0, 0, "-", ss)],
        total_go=0.0, total_usd=0.0, now_ms=NOW)


def test_snapshot_no_pulse_events_on_quiet_stages():
    """HIGH: exec🟢→ready⚫ — ровно ready; тихие этапы пульс-сигналов не дают."""
    kinds = [e["kind"] for e in bc.snapshot_events(
        _snap_stage("exec r1", "🟢"), _snap_stage("ready", "⚫"))]
    assert kinds == ["ready"]
    assert bc.snapshot_events(
        _snap_stage("ready", "🟡"), _snap_stage("ready", "🔴")) == []
    assert bc.snapshot_events(
        _snap_stage("ready", "🟢"), _snap_stage("ready", "⚫")) == []
    assert bc.snapshot_events(
        _snap_stage("stopped", "🟢"), _snap_stage("stopped", "⚫")) == []
    assert bc.snapshot_events(
        _snap_stage("failed", "🟢"), _snap_stage("failed", "⚫")) == []
    assert bc.snapshot_events(
        _snap_stage("arbiter", "🟡"), _snap_stage("arbiter", "🔴")) == []
    # Живая задача по-прежнему сигналит о падении/зависании.
    kinds = [e["kind"] for e in bc.snapshot_events(
        _snap_stage("exec r1", "🟢"), _snap_stage("exec r1", "⚫"))]
    assert kinds == ["crashed"]
    kinds = [e["kind"] for e in bc.snapshot_events(
        _snap_stage("exec r1", "🟢"), _snap_stage("exec r1", "🔴"))]
    assert kinds == ["stuck"]
    # _FINAL_STAGES не расширена: /stop у ready разрешён.
    assert bc._FINAL_STAGES == ("merged", "dropped")
    s = Store()
    s.upsert_task(id="HR", stage="ready")
    assert "передано Claude" in bc.apply_confirm(s, "stop", "HR", True, NOW)


def test_snapshot_quiet_queued_and_sessionless():
    """LOW: queued/preflight и задачи без сессий пульс-сигналов не дают."""
    assert bc.snapshot_events(
        _snap_stage("queued", "⚫"), _snap_stage("queued", "🔴")) == []
    assert bc.snapshot_events(
        _snap_stage("queued", "🔴"), _snap_stage("queued", "⚫")) == []
    assert bc.snapshot_events(
        _snap_stage("preflight", "🟢"), _snap_stage("preflight", "⚫")) == []
    # Без сессий пульс считается из updated_at — сигнала нет и в exec.
    assert bc.snapshot_events(
        _snap_stage("exec r1", "🟢", sessions=None),
        _snap_stage("exec r1", "⚫", sessions=None)) == []
    assert bc.snapshot_events(
        _snap_stage("exec r1", "🟢", sessions=None),
        _snap_stage("exec r1", "🔴", sessions=None)) == []


def test_snapshot_pulse_hysteresis():
    """LOW: crashed/stuck — только со второго подряд снимка; сброс при ready."""
    pending: dict = {}
    # Тик 1: процесс ушёл раньше смены этапа — молчим, сигнал отложен.
    assert bc.snapshot_events(
        _snap_stage("exec r1", "🟢"), _snap_stage("exec r1", "⚫"),
        pending) == []
    assert set(pending) == {"H01"}
    # Тик 2: этап стал ready — только ready, отложенное сброшено.
    kinds = [e["kind"] for e in bc.snapshot_events(
        _snap_stage("exec r1", "⚫"), _snap_stage("ready", "⚫"), pending)]
    assert kinds == ["ready"]
    assert pending == {}
    # Подтверждённыйстой: два тика exec⚫ подряд — crashed на втором.
    assert bc.snapshot_events(
        _snap_stage("exec r1", "🟢"), _snap_stage("exec r1", "⚫"),
        pending) == []
    kinds = [e["kind"] for e in bc.snapshot_events(
        _snap_stage("exec r1", "⚫"), _snap_stage("exec r1", "⚫"), pending)]
    assert kinds == ["crashed"]
    assert pending == {}
    # Пульс вернулся — отложенное сброшено без события.
    assert bc.snapshot_events(
        _snap_stage("exec r1", "🟢"), _snap_stage("exec r1", "🔴"),
        pending) == []
    assert bc.snapshot_events(
        _snap_stage("exec r1", "🔴"), _snap_stage("exec r1", "🟢"),
        pending) == []
    assert pending == {}


def test_record_events_payload_json_matches_db():
    """MEDIUM: строка из record_events форматируется как строка из БД."""
    s = Store()
    added = bc.record_events(
        s, [{"task_id": "H01", "kind": "ready",
             "payload": {"stage": "ready"}}], NOW)
    assert len(added) == 1
    from_db = bc.fetch_unsent_notifiable(s)
    assert len(from_db) == 1
    assert bc.format_event_line(added[0]) == "ready H01: ready"
    assert bc.format_event_line(added[0]) == bc.format_event_line(from_db[0])
    # budget: суммы видны сразу, а не только после перечитывания из БД.
    added2 = bc.record_events(
        s, [{"task_id": "H02", "kind": "budget_soft",
             "payload": {"text": "go $0.45/$0.50 usd $0.00/$0.00"}}], NOW)
    line = bc.format_event_line(added2[0])
    assert "0.45" in line and "0.50" in line


def test_select_summary_batch_cuts_by_events():
    """MEDIUM: длинная сводка режется по событиям, суммарно без потерь."""
    # Без stage в payload: строка ~90 симв., 120 строк > 4000 — разрез обязан.
    evs = [{"id": i + 1, "kind": "ready", "task_id": f"H{i:03d}",
            "payload_json": '{"text": "' + "x" * 90 + '"}'}
           for i in range(120)]
    text0, batch0 = bc.select_summary_batch(evs)
    assert 0 < len(batch0) < 120, "первый проход — частичный батч"
    assert len(text0) <= bc.MSG_LIMIT
    rest = list(evs)
    total = 0
    rounds = 0
    while rest:
        text, batch = bc.select_summary_batch(rest)
        assert text and batch
        assert len(text) <= bc.MSG_LIMIT
        total += len(batch)
        rest = rest[len(batch):]
        rounds += 1
        assert rounds < 10, "очередь встала"
    assert rounds > 1, "бэклог ушёл больше чем одним сообщением"
    assert total == 120


def test_answer_reason_not_found_empty_already():
    """LOW: answer различает отсутствие/пустоту/повтор вопроса."""
    s = Store()
    assert bc.answer_question_reason(
        s, 999999, "да", now_ms=NOW) == bc.ANSWER_NOT_FOUND
    assert bc.answer_question(s, 999999, "да", now_ms=NOW) is False
    qid = _add_question(s, "ещё?", ["да", "нет"])
    assert bc.answer_question_reason(
        s, qid, "   ", now_ms=NOW) == bc.ANSWER_EMPTY
    assert bc.answer_question_reason(
        s, qid, "да", now_ms=NOW) == bc.ANSWER_OK
    assert bc.answer_question_reason(
        s, qid, "нет", now_ms=NOW) == bc.ANSWER_ALREADY


def test_question_button_label_limit():
    """LOW: вариант длиннее 64 симв. — подпись кнопки режется, вопрос уходит."""
    s = Store()
    qid = _add_question(s, "выбирай?", ["y" * 100], task_id="T9")
    row = bc.get_question(s, qid)
    assert row is not None
    text, buttons = bc.format_question(row)
    assert len(buttons) == 1
    assert len(buttons[0].label) == bc.BUTTON_LABEL_LIMIT == 64
    assert buttons[0].data == f"qans:{qid}:0", "callback — по индексу, цел"
    assert "выбирай?" in text


def test_task_unknown_id_html_safe():
    """LOW: /task на несуществующий id с '<' безопасен при parse_mode=HTML."""
    s = Store()
    out = bc.format_task(s, "a<b")
    assert "<" not in out and "&lt;" in out


def test_flush_drops_seen():
    """LOW: после flush id не застревают в seen — событие можно добрать."""
    g = bc.Grouper()
    ev = {"id": 7, "kind": "ready", "task_id": "H01", "payload_json": "{}"}
    assert g.add(ev, NOW)
    g.flush()
    assert g.add(dict(ev), NOW), "flush не должен вечно блокировать id"


def test_dry_run_builds_dispatcher(capsys):
    assert main(["bot", "--dry-run"]) == 0
    assert "dry-run ok" in capsys.readouterr().out


def test_build_dispatcher_no_network():
    from hub.bot.run import build_dispatcher

    dp = build_dispatcher()
    from aiogram import Dispatcher

    assert isinstance(dp, Dispatcher)
