"""Orchestrator handles via CLI: create → (engine) → status/task/result → accept; dependency; limits."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ahub import cli, comms, events, paths, transitions
from ahub.commands import comms as comms_cmd
from ahub.engine import Engine
from ahub.model import State
from ahub.store import Store
from tests.enginekit import install_fake, make_project, scout_ok


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (Path(project.root) / ".hub.toml").write_text(
        f'schema_version = 2\nname = "P"\nworktrees = "{tmp_path / "wt"}"\n'
        'allowed_paths = ["core/**", "tests/**", "docs/**"]\n', encoding="utf-8")
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text(f'projects = ["{project.root}"]\n', encoding="utf-8")
    monkeypatch.chdir(project.root)
    store = Store()
    return store, project


def ahub(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out.strip(), out.err.strip()


def test_scout_cycle(env, capsys):
    store, project = env
    install_fake(store, [scout_ok(report="## Суть\nутечка в core/a.py:1\n\n## Подробно\nмного текста\n")])
    rc, out, err = ahub(capsys, "task", "new", "--kind", "scout", "--title", "где утечка", "--model", "fake",
                        "--spec", "посмотри core/")
    assert out.startswith("T1 в очереди (scout, fake)") and "ahub follow T1" in out, err
    assert Engine(store, project, 1, sleep=lambda s: None).run().state is State.DONE

    rc, out, _ = ahub(capsys, "status")
    assert "Ждут" in out and "отчёт готов" in out and "непрочитано событий 1" in out
    assert 'DONE T1 scout «где утечка» — отчёт' in out
    rc, out, _ = ahub(capsys, "status", "T1")
    assert "Итог работника" in out and "нашёл" in out and "утечка в core/a.py:1" in out
    assert "Подробно" not in out and "ahub accept T1" in out
    assert events.unacked(store) == []  # the task was read — the event is acked
    rc, out, _ = ahub(capsys, "result", "T1", "--full")
    assert "## Подробно" in out and '"summary"' in out
    rc, out, _ = ahub(capsys, "accept", "T1")
    assert out.startswith("T1 принята")
    t = store.get_task(1)
    assert t.state is State.ACCEPTED and not Path(t.worktree).exists()
    arch = Path(project.root) / ".agent-hub"
    assert (arch / "tasks" / "T1" / "report.md").exists() and "T1" in (arch / "tasks.md").read_text()
    assert "принята" in (arch / "tasks" / "T1" / "task.md").read_text()


def test_new_errors_one_line(env, capsys):
    rc, out, err = ahub(capsys, "task", "new", "--kind", "code", "--title", "x", "--paths", "bot/**")
    assert rc == 2 and err.startswith("ошибка: задача не создана:") and "«bot/**» вне" in err and "приёмка" in err


def test_key_idempotent(env, capsys):
    store, _ = env
    install_fake(store, [])
    for _ in range(2):
        rc, out, _ = ahub(capsys, "task", "new", "--kind", "scout", "--title", "x", "--model", "fake", "--key", "k1")
    assert rc == 0 and out.startswith("T1 ") and len(store.list_tasks()) == 1


def test_stop_continue_reject(env, capsys):
    store, _ = env
    install_fake(store, [])
    ahub(capsys, "task", "new", "--kind", "scout", "--title", "x", "--model", "fake")
    assert ahub(capsys, "stop", "T1")[1].startswith("T1: остановлена")
    assert ahub(capsys, "continue", "T1")[1].startswith("T1 снова в очереди")
    assert ahub(capsys, "reject", "T1", "--reason", "не нужно")[1].startswith("T1 отклонена")
    assert store.get_task(1).state is State.REJECTED
    rc, _, err = ahub(capsys, "continue", "T1")
    assert rc == 2 and "продолжить можно" in err
    rc, _, err = ahub(capsys, "status", "T99")
    assert rc == 2 and "нет задачи" in err


def test_budget_lowers_the_real_money_budget(env, capsys):
    store, _ = env
    install_fake(store, [])
    ahub(capsys, "task", "new", "--kind", "scout", "--title", "x", "--model", "fake", "--budget-usd", "0.5")
    rc, out, err = ahub(capsys, "budget", "T1", "--set-usd", "0.02")
    assert rc == 0 and "реальные $0.5 → $0.02" in out, err
    assert store.get_task(1).budget_usd == 0.02


def test_nudge_refused_without_a_running_worker(env, capsys):
    store, _ = env
    install_fake(store, [])
    ahub(capsys, "task", "new", "--kind", "scout", "--title", "x", "--model", "fake")
    rc, out, err = ahub(capsys, "nudge", "T1", "хватит думать, почини")
    assert rc == 2 and err.startswith("ошибка:") and "написать ему некого" in err and "Traceback" not in err
    assert store.get_task(1).request == ""
    assert ahub(capsys, "nudge", "T99", "x")[0] == 2


def test_wait_and_ack(env, capsys):
    store, _ = env
    comms.owner_message(store, "как там оплата?", project="P")
    rc, out, _ = ahub(capsys, "wait", "--timeout", "5s")
    assert rc == 0 and out == "OWNER «как там оплата?»"
    rc, out, _ = ahub(capsys, "wait", "--timeout", "1s")
    assert rc == 3 and out == ""
    assert events.present(store)
    rc, out, _ = ahub(capsys, "inbox")
    assert out.endswith("как там оплата?") and events.unacked(store) == []
    assert ahub(capsys, "inbox")[1] == "новых сообщений нет"


def _locked_wait(*a, **kw):
    raise sqlite3.OperationalError("database is locked")


def test_wait_recovers_from_a_transient_failure(env, capsys, monkeypatch):
    """A broken poll must not kill the wait: the event that comes after it is still delivered
    (the one-shot call of the pre-T77 code raised out of the command)."""
    store, _ = env
    comms.owner_message(store, "как там оплата?", project="P")
    real, state = events.wait, {"n": 0}

    def flaky(*a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(events, "wait", flaky)
    monkeypatch.setattr(comms_cmd, "RETRY_S", 0.0)
    rc, out, err = ahub(capsys, "wait", "--timeout", "5s")
    assert rc == 0 and out == "OWNER «как там оплата?»" and err == ""
    assert state["n"] == 2  # the failure was retried, not passed on


def test_wait_gives_up_after_the_failure_cap(env, capsys, monkeypatch, caplog):
    monkeypatch.setattr(events, "wait", _locked_wait)  # the database never opens
    monkeypatch.setattr(comms_cmd, "RETRY_S", 0.0)
    with caplog.at_level("WARNING", logger="wait"):
        rc, out, err = ahub(capsys, "wait", "--timeout", "30m")
    assert rc == 4 and out == ""
    assert err.count("\n") == 0 and f"{comms_cmd.MAX_POLL_FAILURES} раз" in err and "database is locked" in err
    assert len([r for r in caplog.records if r.name == "ahub.wait"]) == 1  # once per distinct error


def test_wait_polls_once_with_a_zero_timeout(env, capsys, monkeypatch):
    """`--timeout 0` is a poll of what is there right now, not a refusal: a pending event is delivered."""
    store, _ = env
    comms.owner_message(store, "как там оплата?", project="P")
    seen, real = [], events.wait

    def once(store_, *, timeout_s, **kw):
        seen.append(timeout_s)
        return real(store_, timeout_s=timeout_s, **kw)

    monkeypatch.setattr(events, "wait", once)
    rc, out, err = ahub(capsys, "wait", "--timeout", "0")
    assert rc == 0 and out == "OWNER «как там оплата?»" and err == ""
    assert seen == [0.0]  # exactly one poll, and nothing to wait for
    assert ahub(capsys, "wait", "--timeout", "0") == (3, "", "")  # nothing left — but it did poll
    assert seen == [0.0, 0.0]


def test_wait_keeps_its_deadline(env, capsys, monkeypatch):
    """`--timeout` is the deadline: no poll starts past it — not even a zero-length one after a failure
    that ate the whole timeout."""
    calls, fake_clock = [], {"t": 1000.0}
    monkeypatch.setattr(comms_cmd, "clock", lambda: fake_clock["t"])
    monkeypatch.setattr(comms_cmd, "RETRY_S", 0.0)

    def timed_out(store, *, timeout_s, **kw):
        calls.append(timeout_s)
        fake_clock["t"] += timeout_s  # a poll takes exactly what it was given
        return []

    monkeypatch.setattr(events, "wait", timed_out)
    rc, out, err = ahub(capsys, "wait", "--timeout", "30s")
    assert rc == 3 and out == "" and err == ""
    assert calls == [30.0]  # one poll, exactly the timeout

    def failed_once(store, *, timeout_s, **kw):
        calls.append(timeout_s)
        fake_clock["t"] += timeout_s
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return []

    calls.clear()
    fake_clock["t"] = 1000.0
    monkeypatch.setattr(events, "wait", failed_once)
    rc, out, _ = ahub(capsys, "wait", "--timeout", "30s")
    assert rc == 3 and calls == [30.0]  # the retry slept out the deadline and stopped — no poll of 0


def _scripted_polls(monkeypatch, plan):
    """Drive the endless watch loop: `plan(poll)` returns None (a real poll), "raise" (a locked database)
    or "stop" (a Ctrl-C, as a Monitor's would be)."""
    real = events.ready_batch
    state = {"n": 0}

    def fake(store, *, now=None, scope=None, window_ms=events.GROUP_WINDOW_MS):
        state["n"] += 1
        step = plan(state["n"])
        if step == "raise":
            raise sqlite3.OperationalError("database is locked")
        if step == "stop":
            raise KeyboardInterrupt
        return real(store, now=now, scope=scope, window_ms=window_ms)

    monkeypatch.setattr(events, "ready_batch", fake)


def test_watch_streams_and_survives_a_transient_failure(env, capsys, monkeypatch, caplog):
    store, _ = env
    comms.owner_message(store, "как там оплата?", project="P")
    _scripted_polls(monkeypatch, lambda n: "raise" if n == 2 else ("stop" if n >= 4 else None))
    with caplog.at_level("WARNING", logger="watch"):
        rc, out, err = ahub(capsys, "watch", "--poll", "0")
    assert rc == 0 and out == "OWNER «как там оплата?»" and err == ""
    assert [r.message for r in caplog.records if r.name == "ahub.watch"] == [
        "watch: OperationalError: database is locked"]  # one line for the whole streak


def test_watch_gives_up_after_the_failure_cap(env, capsys, monkeypatch, caplog):
    _scripted_polls(monkeypatch, lambda n: "raise")  # the database never opens
    with caplog.at_level("WARNING", logger="watch"):
        rc, out, err = ahub(capsys, "watch", "--poll", "0")
    assert rc == 4 and out == ""
    assert err.count("\n") == 0 and f"{comms_cmd.MAX_POLL_FAILURES} раз" in err and "database is locked" in err
    assert len([r for r in caplog.records if r.name == "ahub.watch"]) == 1  # once per distinct error, not per poll


def test_watch_counts_failures_again_after_a_working_poll(env, capsys, monkeypatch, caplog):
    """The cap counts a streak, not the total: a poll that works starts a new one (else a Monitor with two
    short hiccups would be killed by MAX_POLL_FAILURES of them together)."""
    cap = comms_cmd.MAX_POLL_FAILURES
    real, state = events.ready_batch, {"n": 0}

    def fake(store, *, now=None, scope=None, window_ms=events.GROUP_WINDOW_MS):
        state["n"] += 1
        if state["n"] == 3:
            return real(store, now=now, scope=scope, window_ms=window_ms)  # the stream recovers
        if state["n"] > 3 + cap:
            raise KeyboardInterrupt  # the cap of the second streak must fire before this
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(events, "ready_batch", fake)
    with caplog.at_level("WARNING", logger="watch"):
        rc, out, err = ahub(capsys, "watch", "--poll", "0")
    assert rc == 4 and out == "" and state["n"] == 3 + cap  # 2 + cap failures, counted from zero
    assert err.count("\n") == 0 and f"{cap} раз" in err and "database is locked" in err
    assert [r.message for r in caplog.records if r.name == "ahub.watch"] == [
        "watch: OperationalError: database is locked"] * 2  # one line per streak, not per poll


def test_wait_and_watch_do_not_catch_programmer_defects(env, monkeypatch):
    """TypeError/AttributeError in poll is a code defect, not a transient DB error — raises immediately."""
    def fake_wait(*a, **kw):
        raise TypeError("internal bug")

    monkeypatch.setattr(events, "wait", fake_wait)
    with pytest.raises(TypeError, match="internal bug"):
        cli.main(["wait", "--timeout", "1s"])

    def fake_watch(*a, **kw):
        raise AttributeError("another bug")

    monkeypatch.setattr(events, "ready_batch", fake_watch)
    with pytest.raises(AttributeError, match="another bug"):
        cli.main(["watch", "--poll", "0"])


def test_say_ask_answer_alarms(env, capsys):
    store, _ = env
    assert ahub(capsys, "say", "T12 готова, смотрю")[1] == "отправлено владельцу"
    assert comms.outbox(store)[0]["text"] == "T12 готова, смотрю"
    rc, out, _ = ahub(capsys, "ask", "сливать T12?", "--options", "да,нет")
    assert out.startswith("вопрос #1")
    questions = ahub(capsys, "questions")[1]
    assert "#1" in questions and "сливать T12?" in questions and "да, нет" in questions
    assert comms.answer(store, 1, "да") and not comms.answer(store, 1, "нет")
    assert events.lines(store, events.unacked(store)) == ["ANSWER #1 «сливать T12?» → да"]
    comms.raise_alarm(store, "opencode недоступен 12 мин", critical=True)
    rc, out, _ = ahub(capsys, "alarms")
    assert rc == 0
    lines = out.splitlines()
    assert "#2" in out and "ALARM! opencode недоступен 12 мин" in out  # the id, so it can be acked by hand
    age_cell = lines[1].split()[1:3]
    assert len(age_cell) == 2 and age_cell[0] == "0" and age_cell[1] in ("мин", "мин.")  # the age column
    assert lines[-1] == "Дальше  ahub ack <#> · ahub alarms --ack"  # --ack marks read; --acked would list read ones

    rc, out, _ = ahub(capsys, "alarms", "--ack")
    assert out.splitlines()[-1] == "1 тревога отмечена прочитанной"  # the result, not a Next command
    assert ahub(capsys, "alarms")[1] == "тревог нет"

    # two at once — the plural branch has its own key (a missing one was a KeyError in front of a person)
    comms.raise_alarm(store, "codex отвечает медленно", critical=True)
    comms.raise_alarm(store, "telegram молчит", critical=True)
    rc, out, _ = ahub(capsys, "alarms", "--ack")
    assert rc == 0 and out.splitlines()[-1] == "2 тревоги отмечены прочитанными"
    assert ahub(capsys, "alarms")[1] == "тревог нет"
    assert "codex отвечает медленно" in ahub(capsys, "alarms", "--acked")[1]


LONG_OWNER = ("Я тебе ставил конкретные цели на прошлой неделе, а ты сделал вид, что ничего не было, и я хочу "
              "понять почему так вышло и что ты собираешься с этим делать дальше, потому что сроки уже в четверг.")


def test_inbox_reads_one_message_in_full(env, capsys, monkeypatch):
    """`ahub inbox` cuts the text to a cell and says where the rest is; `ahub inbox <id>` — the whole text."""
    monkeypatch.setenv("COLUMNS", "100")  # the list is drawn at the width of the terminal
    store, _ = env
    mid = comms.owner_message(store, LONG_OWNER, project="P", chat_id=42)
    comms.owner_message(store, "спасибо", project="P")

    out = ahub(capsys, "inbox")[1]
    assert out.splitlines()[0].split() == ["#", "сообщение"]
    assert f"… остальное: ahub inbox {mid}" in out
    assert "сроки уже в четверг." not in out  # the list cuts the message
    assert comms.inbox(store, mark=False) == []  # and read it

    unread = comms.owner_message(store, "ещё одно", project="P")
    rc, out, err = ahub(capsys, "inbox", f"#{mid}")
    assert rc == 0 and err == "", err
    assert out.splitlines()[:2] == ["#1", "───"]
    assert "Чат     42" in out and "сроки уже в четверг." in out  # the whole text, nothing cut
    assert "ahub inbox" not in out  # nothing is cut — no hint
    assert [r["id"] for r in comms.inbox(store, mark=False)] == [unread]  # reading one marks nothing

    data = json.loads(ahub(capsys, "--json", "inbox", str(mid))[1])
    assert data["message"]["text"] == LONG_OWNER and data["message"]["chat_id"] == 42


def test_inbox_full_and_its_refusals(env, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    store, _ = env
    comms.owner_message(store, LONG_OWNER, project="P")
    out = ahub(capsys, "inbox", "--full", "--peek")[1]
    assert LONG_OWNER in " ".join(out.split()) and "ahub inbox" not in out
    assert len(comms.inbox(store, mark=False)) == 1  # --peek

    rc, out, err = ahub(capsys, "inbox", "99")
    assert rc == 2 and out == "" and err.strip() == "ошибка: нет сообщения #99"
    rc, out, err = ahub(capsys, "inbox", "xx")
    assert rc == 2 and err.strip() == "ошибка: «xx» — не номер"
    assert ahub(capsys, "--lang", "en", "inbox", "99")[2].strip() == "error: no message #99"


def test_questions_reads_one_question_in_full(env, capsys):
    store, _ = env
    qid = comms.ask(store, "сливать T12?", ["да", "нет"], project="P")
    rc, out, err = ahub(capsys, "questions", str(qid))
    assert rc == 0 and err == "", err
    assert out.splitlines()[:2] == [f"#{qid}", "───"]
    assert "сливать T12?" in out and "Варианты" in out and "• нет" in out

    comms.answer(store, qid, "да")
    assert "Ответ  да" in ahub(capsys, "questions", str(qid))[1]  # an answered one is readable too
    data = json.loads(ahub(capsys, "--json", "questions", str(qid))[1])
    assert data["question"]["answer"] == "да" and data["question"]["options"] == ["да", "нет"]

    rc, out, err = ahub(capsys, "questions", "99")
    assert rc == 2 and out == "" and err.strip() == "ошибка: нет вопроса #99"
    assert ahub(capsys, "questions", "xx")[2].strip() == "ошибка: «xx» — не номер"


def test_l1_limit(env, capsys):
    store, _ = env
    for i in range(80):
        tid = store.create_task(project="P", kind="scout", title=f"задача номер {i} с длинным названием для проверки")
        for st in (State.PREPARING, State.WORKING, State.DONE):
            transitions.move(store, tid, st, reason="отчёт готов, суть: " + "подробно " * 10)
    rc, out, _ = ahub(capsys, "status")
    assert len(out.encode()) <= 1500 and "ещё" in out


def test_json_status(env, capsys):
    rc, out, _ = ahub(capsys, "--json", "status")
    data = json.loads(out)
    assert data["active"] == [] and data["waiting"] == []


def test_watch_summary_once_new_and_ack(env, capsys, monkeypatch):
    import time

    store, _ = env
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    comms.owner_message(store, "первое", project="P")  # the messages of this project (a hub-wide one
    # belongs to the owner's inbox only — `inbox` in a project scope does not consume it)
    batch = events.ready_batch(store)
    events.mark_delivered(store, [e.id for e in batch])
    rc, out, _ = ahub(capsys, "watch", "--poll", "0")
    assert rc == 0 and "НЕПРОЧИТАНО 1" in out and "первое" in out
    rc, out, _ = ahub(capsys, "watch", "--poll", "0")
    assert rc == 0 and out == ""  # restart stays silent
    assert len(events.unacked(store)) == 1  # old stays unread
    rc, out, _ = ahub(capsys, "status")
    assert "непрочитано событий 1" in out
    comms.owner_message(store, "второе", project="P")
    batch = events.ready_batch(store)
    assert len(batch) == 1 and "второе" in batch[0].payload.get("text", "")
    events.mark_delivered(store, [e.id for e in batch])
    rc, out, _ = ahub(capsys, "watch", "--poll", "0")
    assert rc == 0 and "НЕПРОЧИТАНО 1" in out and "второе" in out and "первое" not in out
    assert len(events.unacked(store)) == 2  # both stay unread
    rc, out, _ = ahub(capsys, "inbox")
    assert "второе" in out and events.unacked(store) == []  # ack still works
    rc, out, _ = ahub(capsys, "watch", "--poll", "0")
    assert rc == 0 and out == ""


def test_status_json_keeps_the_reason_and_renders_it(env, capsys, monkeypatch):
    """The overview --json shows both forms, as the task detail does: the code and the sentence."""
    import json as _json

    from ahub import reasons
    from ahub.i18n import _reset

    store, _ = env
    tid = store.create_task(project="P", kind="scout", title="later", now=0)
    store.update_task(tid, state_reason=reasons.dump("wait_accept", task="T1", state="queued"), now=0)
    raw = '{"code":"wait_accept","task":"T1","state":"queued"}'
    rc, out, err = ahub(capsys, "--json", "status")
    assert rc == 0, err
    row = [q for q in _json.loads(out)["queued"] if q["id"] == tid][0]
    assert row["reason"] == raw
    assert row["reason_text"] == "ждёт принятия T1 (в очереди)"  # the language of the reader
    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    row = [q for q in _json.loads(ahub(capsys, "--json", "status")[1])["queued"] if q["id"] == tid][0]
    assert row["reason"] == raw
    assert row["reason_text"] == "waiting for T1 to be accepted (queued)"
