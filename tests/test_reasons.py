"""Reason codes (ahub/reasons.py): what is stored is a code, what a reader sees is a sentence in its language."""

from __future__ import annotations

import re

import pytest

from ahub import reasons, service, transitions, views
from ahub.i18n import _reset, lang
from ahub.model import State
from ahub.store import Store

CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


@pytest.fixture(autouse=True)
def _en(monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    yield
    _reset()


def _ru(monkeypatch) -> None:
    monkeypatch.setenv("AHUB_LANG", "ru")
    _reset()
    assert lang() == "ru"


def test_round_trip():
    stored = reasons.dump("wait_accept", task="T53", state="reviewing")
    assert stored == '{"code":"wait_accept","task":"T53","state":"reviewing"}'
    assert reasons.load(stored) == {"code": "wait_accept", "task": "T53", "state": "reviewing"}


def test_an_empty_param_is_stored_and_none_is_not():
    """The template of a code must always get what it asks for, even when the value is empty."""
    assert reasons.dump("merged", branch="main", note="") == '{"code":"merged","branch":"main","note":""}'
    assert reasons.dump("accepted", note=None) == '{"code":"accepted"}'
    assert reasons.part("push_failed", err="") == {"code": "push_failed", "err": ""}
    assert reasons.dump("") == ""


def test_merged_without_a_push_note(monkeypatch):
    """The accept path with no push configured: the row renders, it does not raise."""
    assert reasons.text(reasons.dump("merged", branch="main", note="")) == "merged into main"
    # a row written before the param existed — the same sentence, no KeyError
    assert reasons.text('{"code":"merged","branch":"main"}') == "merged into main"
    _ru(monkeypatch)
    assert reasons.text('{"code":"merged","branch":"main"}') == "слита в main"


def test_a_missing_param_never_breaks_the_reader():
    for stored in ('{"code":"blocked","summary":""}', '{"code":"scout_bad","problems":[]}',
                   '{"code":"merged"}', '{"code":"gates_failed"}'):
        assert reasons.text(stored)  # a sentence (or at least the code) — never an exception


def test_plain_text_is_shown_as_it_is():
    """An old row, or a human note from `ahub reject --reason` — no code, no guessing."""
    assert reasons.text("не нужно: это не наша задача") == "не нужно: это не наша задача"
    assert reasons.text("") == ""
    assert reasons.load("{not json") == {}
    assert reasons.text("{not json") == "{not json"


def test_unknown_code_shows_the_code_itself():
    assert reasons.text('{"code":"no_such_code"}') == "no_such_code"


def test_a_state_param_is_read_as_a_word(monkeypatch):
    """A stored machine name becomes a readable word — in the language of the reader."""
    stored = reasons.dump("wait_accept", task="T53", state="reviewing")
    assert reasons.text(stored) == "waiting for T53 to be accepted (reviewing)"
    _ru(monkeypatch)
    assert reasons.text(stored) == "ждёт принятия T53 (ревью)"


def test_sub_reasons_are_rendered_too(monkeypatch):
    """A gate problem inside a reason is stored as data as well."""
    stored = reasons.dump("gates_failed", problems=[reasons.part("gate_no_commit"),
                                                    reasons.part("gate_dirty", files="core/a.py")])
    assert reasons.text(stored) == ("gates still failing after the fix: no commit from the base; "
                                    "uncommitted changes: core/a.py")
    _ru(monkeypatch)
    assert reasons.text(stored) == ("ворота не пройдены после исправления: нет коммита от базы; "
                                    "незакоммиченные изменения: core/a.py")


def test_one_row_reads_in_both_languages(monkeypatch):
    """The same DB row, written once, reads in the language the reader asked for."""
    store = Store()
    tid = store.create_task(project="P", kind="scout", title="x", now=0)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, tid, st, now=0)
    transitions.move(store, tid, State.DONE, reason=reasons.dump("review_exhausted", n=2, highs=1), now=0)
    assert store.get_task(tid).state_reason == '{"code":"review_exhausted","n":2,"highs":1}'

    out = {}
    for code in ("en", "ru"):
        monkeypatch.setenv("AHUB_LANG", code)
        _reset()
        assert lang() == code
        out[code] = views.task_text(store, store.get_task(tid), now=0, w=100)
    assert "review rounds exhausted (2 findings, high: 1)" in out["en"]
    assert "круги ревью кончились (2 замечаний, high: 1)" in out["ru"]
    assert not CYRILLIC.search(out["en"])


def test_the_queue_wait_reason_is_a_code(tmp_path):
    store = Store()
    base = store.create_task(project="P", kind="scout", title="a", now=0)
    task = store.create_task(project="P", kind="scout", title="b", after=[base], now=0)
    svc = service.Service(store, [], spawn=lambda tid: 1, proc_root=tmp_path, lock_busy=lambda p: False)
    assert svc._deps_ok(store.get_task(task)) == '{"code":"wait_accept","task":"T1","state":"queued"}'


def test_the_cascade_writes_a_code():
    store = Store()
    base = store.create_task(project="P", kind="scout", title="a", now=0)
    task = store.create_task(project="P", kind="scout", title="b", after=[base], now=0)
    transitions.move(store, base, State.STOPPED, reason=reasons.dump("stopped"), now=0)
    transitions.move(store, base, State.QUEUED, reason=reasons.dump("continue_task"), now=0)
    transitions.move(store, base, State.PREPARING, reason=reasons.dump("taken"), now=0)
    transitions.move(store, base, State.WORKING, reason=reasons.dump("worker_started"), now=0)
    transitions.move(store, base, State.DONE, reason=reasons.dump("report_ready"), now=0)
    transitions.move(store, base, State.REJECTED, reason=reasons.dump("rejected"), now=0)
    assert store.get_task(task).state == State.NEEDS_DECISION
    assert store.get_task(task).state_reason == '{"code":"dep_rejected","task":"T1"}'
    assert reasons.text(store.get_task(task).state_reason) == "dependency T1 is rejected"