"""Tests for _age: minutes, hours, days, the 24 h boundary; the inbox/questions views: the head of a message
in the list, the whole one by its number."""

from __future__ import annotations

from ahub import comms
from ahub.store import Store
from ahub.views import _age, inbox_text, message_text, question_text, report_essence

LONG = ("Я тебе ставил конкретные цели на прошлой неделе, а ты сделал вид, что ничего не было, и я хочу "
        "понять почему так вышло и что ты собираешься с этим делать дальше, потому что сроки уже в четверг.")
URL = "https://example.com/a/really/long/link/that/never/breaks/at/any/word/boundary/at/all/report-2026.pdf"


def test_minuty():
    now = 1_000_000_000_000
    assert _age(now - 5 * 60000, now) == "5 мин"
    assert _age(now, now) == "0 мин"


def test_chasy():
    now = 1_000_000_000_000
    assert _age(now - 90 * 60000, now) == "1 ч 30 мин"
    assert _age(now - 59 * 60000, now) == "59 мин"


def test_granitsa_24_ch():
    now = 1_000_000_000_000
    assert _age(now - (23 * 60 + 59) * 60000, now) == "23 ч 59 мин"
    assert _age(now - 24 * 60 * 60000, now) == "1 д 0 ч"


def test_dni():
    now = 1_000_000_000_000
    assert _age(now - (26 * 60 + 15) * 60000, now) == "1 д 2 ч"
    assert _age(now - (3 * 24 * 60 + 5 * 60) * 60000, now) == "3 д 5 ч"


def test_essence_ru():
    rep = "## Суть\nутечка в core/a.py:1\n\n## Подробно\nмного текста\n"
    assert "утечка" in report_essence(rep) and "Подробно" not in report_essence(rep)


def test_essence_summary_en():
    rep = "## Summary\nleak in core/a.py:1\n\n## Details\nlots of text\n"
    assert "leak" in report_essence(rep) and "Details" not in report_essence(rep)


def test_inbox_shows_two_lines_and_where_the_rest_is():
    store = Store()
    mid = comms.owner_message(store, LONG, project="P", chat_id=42)
    comms.owner_message(store, "спасибо", project="P")
    rows = comms.inbox(store, mark=False)

    out = inbox_text(rows, w=100)
    lines = out.splitlines()
    assert lines[0].split() == ["#", "сообщение"]
    block = [ln for ln in lines if ln.startswith("      ") or ln.startswith(f"  #{mid}")]
    assert len(block) == 3  # two lines of the head + where the rest is
    assert block[-1] == f"      … остальное: ahub inbox {mid}"
    assert lines[-1].endswith("спасибо")  # a short message — the whole text, no hint
    assert sum("ahub inbox" in ln for ln in lines) == 1


def test_message_text_is_the_whole_message():
    store = Store()
    mid = comms.owner_message(store, LONG, project="P", chat_id=42, now=0)
    out = message_text(comms.message(store, mid), w=100)
    assert out.splitlines()[:2] == [f"#{mid}", "───"]
    assert "Когда" in out and "Проект  P" in out and "Чат     42" in out
    assert LONG in " ".join(out.split())  # every word of the message, nothing cut
    assert "…" not in out and "ahub inbox" not in out  # no hint — nothing is cut


def test_inbox_full_is_the_message_blocks():
    store = Store()
    comms.owner_message(store, LONG, project="P", now=0)
    rows = comms.inbox(store, mark=False)
    assert inbox_text(rows, full=True, w=100) == message_text(rows[0], w=100)
    assert inbox_text([], full=True, w=100) == "новых сообщений нет"


def test_inbox_full_keeps_the_width_of_the_caller(monkeypatch):
    """--full draws at the width it was given, not at COLUMNS (the MCP tool reads through it)."""
    monkeypatch.setenv("COLUMNS", "60")
    store = Store()
    comms.owner_message(store, LONG, project="P", now=0)
    rows = comms.inbox(store, mark=False)
    assert inbox_text(rows, full=True, w=100) == message_text(rows[0], w=100)
    assert inbox_text(rows, full=True) != inbox_text(rows, full=True, w=100)  # COLUMNS=60 is narrower


def test_a_head_never_passes_the_width_of_the_column():
    """para does not break a long word — a URL in a message would be one line past the width; it is clipped,
    and a clipped head is a cut, so the hint points at the rest."""
    store = Store()
    mid = comms.owner_message(store, f"смотри {URL}", project="P")
    rows = comms.inbox(store, mark=False)
    lines = inbox_text(rows, w=80).splitlines()

    assert len(lines) == 4  # the column head, the two lines of the message, the hint
    assert lines[1].strip() == f"#{mid}  смотри"
    assert lines[2].strip().startswith("https://example.com") and lines[2].endswith("…")
    assert len(lines[2]) == 80 and len(URL) > 80 - 7  # the URL alone is longer than the column
    assert lines[3] == f"      … остальное: ahub inbox {mid}"
    assert all(len(ln) <= 80 for ln in lines)


def test_question_text_options_and_answer():
    store = Store()
    qid = comms.ask(store, "сливать T12?", ["да", "нет"], task_id=7, project="P", now=0)
    out = question_text(comms.question(store, qid), w=100)
    assert out.splitlines()[:2] == [f"#{qid}", "───"]
    assert "сливать T12?" in out and "Варианты" in out and "• да" in out and "• нет" in out
    assert "Задача  T7" in out and "Ответ" not in out  # not answered yet

    comms.answer(store, qid, "да")
    out = question_text(comms.question(store, qid), w=100)
    assert "Ответ  да" in out


def test_a_hub_wide_row_has_no_project_line():
    store = Store()
    mid = comms.owner_message(store, "для всех", now=0)
    out = message_text(comms.message(store, mid), w=100)
    assert "Когда" in out and "Проект" not in out
    assert comms.question(store, 99) is None and comms.message(store, 99) is None
