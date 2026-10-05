"""The rendering layer (ahub/ui.py) and the views people read most: snapshots at width 100, plain text."""

from __future__ import annotations

import json
import re
import threading

import pytest

from ahub import transitions, ui, views
from ahub.i18n import _reset
from ahub.model import State
from ahub.store import Store

NOW = 1_700_000_000_000
HOUR = 3_600_000
W = 100


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    """The snapshots are the English reference strings."""
    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    yield
    _reset()


class _Stream:
    """A stdout that claims to be (or not to be) a terminal."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_width_explicit_env_and_fallback(monkeypatch):
    monkeypatch.setenv("COLUMNS", "72")
    assert ui.width() == 72
    assert ui.width(140) == 140
    assert ui.width(0) == ui.MIN_WIDTH  # an explicit 0 is a width, not "not given"
    monkeypatch.delenv("COLUMNS")
    assert ui.width() >= ui.MIN_WIDTH


def test_pipe_gets_no_ansi(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdout", _Stream(tty=False))
    assert ui.styled("hello", "bold") == "hello"
    monkeypatch.setattr(ui.sys, "stdout", _Stream(tty=True))
    assert ui.styled("hello", "bold") == "\033[1mhello\033[0m"
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.styled("hello", "bold") == "hello"  # NO_COLOR wins even on a terminal
    monkeypatch.setenv("NO_COLOR", "")  # presence alone is the convention
    assert ui.styled("hello", "bold") == "hello"
    monkeypatch.delenv("NO_COLOR")


def test_plain_is_per_thread(monkeypatch):
    """The observer and the TG launcher build their prompts on their own threads.

    While such a thread is inside `plain()`, the terminal output of the main thread still belongs to a
    human — a process-global flag muted it (and the whole ui suite with it, in whatever order it ran).
    """
    monkeypatch.setattr(ui.sys, "stdout", _Stream(tty=True))
    inside = threading.Event()
    release = threading.Event()

    def _worker():
        with ui.plain():
            assert ui.styled("prompt", "bold") == "prompt"  # its own thread: plain
            inside.set()
            release.wait(5)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    try:
        assert inside.wait(5)
        assert ui.styled("terminal", "bold") == "\033[1mterminal\033[0m"  # this thread: colour
    finally:
        release.set()
        th.join(timeout=5)
    with ui.plain():
        assert ui.styled("here", "bold") == "here"  # and plain() still works in this thread
    assert ui.styled("terminal", "bold") == "\033[1mterminal\033[0m"


def test_kv_aligns_a_block():
    block = ui.kv([("State", "done · merged into main"), ("Model", "bunny"), ("Age", "4 h 0 min")])
    assert block == ("State  done · merged into main\n"
                     "Model  bunny\n"
                     "Age    4 h 0 min")


def test_kv_chunks_line_up_like_columns():
    block = ui.kv([("T1", ["queued", "waiting for T2 to be accepted (queued)"]),
                   ("T2", ["done", ""])])
    assert block == ("T1  queued  waiting for T2 to be accepted (queued)\n"
                     "T2  done")


def test_kv_wraps_a_long_value_under_the_label():
    block = ui.kv([("State", "needs decision " + "very " * 30 + "long")], w=60)
    lines = block.split("\n")
    assert len(lines) > 1 and all(ln.startswith("      ") for ln in lines[1:])
    assert max(len(ln) for ln in lines) <= 60


def test_para_keeps_paragraphs_and_list_items():
    text = "first paragraph line\nsecond line\n\n- one\n- two"
    out = ui.para(text, indent=2, w=40)
    assert out.split("\n") == ["  first paragraph line second line", "", "  - one", "  - two"]


def test_bullets_one_per_line():
    out = ui.bullets(["a very long question that has to be wrapped somewhere around here"], indent=2, w=40)
    assert out.split("\n") == ["  • a very long question that has to be",
                               "    wrapped somewhere around here"]


def test_table_ellipsis_only_in_a_cell():
    head = ["task", "title"]
    rows = [["T1", "a rather long title that does not fit the width at all"]]
    out = ui.table(head, rows, max_width=[6, 12], w=30)
    assert out.split("\n") == ["task  title", "T1    a rather…"]  # cut at a word, in the cell only
    assert all(len(ln) <= 30 for ln in out.split("\n"))


def test_table_tolerates_short_head():
    """A head with fewer columns than the rows does not raise IndexError."""
    head = ["col1", "col2"]
    rows = [["a", "b", "c", "d"]]
    out = ui.table(head, rows)
    lines = out.split("\n")
    assert lines[0].startswith("col1  col2")
    assert lines[1] == "a     b     c  d"


def test_fit_cuts_at_a_sentence_and_points_to_the_rest():
    first = "The wizard now asks for every provider and probes its models once before saving anything."
    text = first + " " + "filler words here and there " * 20
    out = ui.fit(text, 200, hint="… the rest: ahub result T1")
    assert out.split("\n\n") == [first + "…", "… the rest: ahub result T1"]
    assert ui.fit("short", 200) == "short"  # fits — nothing is added


def test_fit_without_a_sentence_ends_cuts_a_word_and_marks_it():
    text = "filler words " * 100
    out = ui.fit(text, 100)
    assert out.endswith("…") and out.removesuffix("…").rstrip() in text  # a whole word, then the mark


def test_badge_and_section_are_colour_only_on_a_terminal(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdout", _Stream(tty=False))
    assert ui.badge("🟢", "working", "working") == "🟢 working"
    assert ui.section("Summary") == "Summary"
    monkeypatch.setattr(ui.sys, "stdout", _Stream(tty=True))
    assert ui.badge("🟢", "working", "working") == "\033[32m🟢 working\033[0m"
    assert ui.section("Summary") == "\033[1mSummary\033[0m"


# --- the views, as a snapshot ---

DETAIL = """\
T3  code  Setup wizard: choose providers and per-role models
────────────────────────────────────────────────────────────
State  done · gates passed, acceptance is green · process alive
Model  bunny  Review  spark ×2  Round  3
Cost   $0.046 Go of $1.50 budget
Age    4 h 0 min  After  T2
Summary
  The wizard asks for every provider and probes its models once. The answer is stored in the hub
  config.
Open points
  • Should the ru strings live in the catalog or in the setup wizard?
  Not done: the rebrand of the Telegram card.
Report 0.1 KB
  the wizard, the models and the service
Next  ahub accept T3 · ahub rework T3 --notes "…" · ahub reject T3"""

OVERVIEW = """\
all projects · 1 active · 1 waiting · 2 queued
      task  kind  title                           state    model  round  idle   cost
  🟢  T1    code  Setup wizard: choose providers  writing  bunny  3      2 min  $0.046
Waiting
  T3  done    gates passed, acceptance is green
  T2  queued
  T4  queued  waiting for T3 to be accepted (queued)
Unread
  DONE T3 code «Setup wizard: choose providers and per-role models» — report 2.1 KB; ready; $0.04
unread events 1"""

DETAIL_TTY = """\
\x1b[33m⏺\x1b[0m T3  code  Setup wizard: choose providers and per-role models
\x1b[2m──────────────────────────────────────────────────────────────\x1b[0m
\x1b[2mState\x1b[0m  done · gates passed, acceptance is green · process alive
\x1b[2mModel\x1b[0m  bunny  \x1b[2mReview\x1b[0m  spark ×2  \x1b[2mRound\x1b[0m  3
\x1b[2mCost\x1b[0m   $0.046 Go of $1.50 budget
\x1b[2mAge\x1b[0m    4 h 0 min  \x1b[2mAfter\x1b[0m  T2
\x1b[1mSummary\x1b[0m
  The wizard asks for every provider and probes its models once. The answer is stored in the hub
  config.
\x1b[1mOpen points\x1b[0m
  \x1b[2m⎿\x1b[0m \x1b[2mShould the ru strings live in the catalog or in the setup wizard?\x1b[0m
  Not done: the rebrand of the Telegram card.
\x1b[1mReport 0.1 KB\x1b[0m
  the wizard, the models and the service
  \x1b[2m⎿\x1b[0m \x1b[2mNext: ahub accept T3 · ahub rework T3 --notes "…" · ahub reject T3\x1b[0m"""

OVERVIEW_TTY = (
    "\x1b[1mall projects · 1 active · 1 waiting · 2 queued\x1b[0m\n"
    "⏺ T1  Setup wizard: choose providers\n"
    "  \x1b[2m⎿\x1b[0m \x1b[2mwriting · bunny · 2 min · $0.046\x1b[0m\n"
    "\x1b[33m⏺\x1b[0m T3  done · gates passed, acceptance is green\n"
    '  \x1b[2m⎿\x1b[0m \x1b[2mNext: ahub accept T3 · ahub rework T3 --notes "…" · ahub reject T3\x1b[0m\n'
    "\x1b[2m◦\x1b[0m T2  queued\n"
    "\x1b[2m◦\x1b[0m T4  queued · waiting for T3 to be accepted (queued)\n"
    "\x1b[1mUnread\x1b[0m\n"
    "  \x1b[2m⎿\x1b[0m \x1b[2mDONE T3 code «Setup wizard: choose providers and per-role models» — "
    "report 2.1 KB; ready; $0.04\x1b[0m\n"
    "\x1b[2munread events 1\x1b[0m"
)

HISTORY = """\
  task  kind   title                                              state     round  cost    took
  T5    scout  find the leak                                      accepted  0      $0.012  13 min
  T3    code   Setup wizard: choose providers and per-role…       accepted  3      $0.046  4 h 0 min"""


def _fill(store: Store, tmp_path) -> dict[str, int]:
    """One active task, one done task with a result, one task blocked in the queue."""
    a = store.create_task(project="P", kind="code", title="Setup wizard: choose providers",
                          executor="bunny", review={"models": ["spark"], "rounds": 2}, budget_go=1.5, now=NOW)
    transitions.move(store, a, State.PREPARING, now=NOW)
    transitions.move(store, a, State.WORKING, now=NOW)
    store.update_task(a, phase="writing", round=3, now=NOW)
    row = store.add_session(task_id=a, provider="fake", role="executor", model="bunny", now=NOW)
    store.update_session(row, status="ok", cost_go=0.046)
    base = store.create_task(project="P", kind="scout", title="probe", now=NOW)
    done = store.create_task(project="P", kind="code", title="Setup wizard: choose providers and per-role models",
                             executor="bunny", review={"models": ["spark"], "rounds": 2}, budget_go=1.5,
                             after=[base], now=NOW)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, done, st, now=NOW)
    transitions.move(store, done, State.DONE,
                     reason='{"code":"gates_passed_tests"}',
                     payload={"summary": "ready", "report_bytes": 2150, "cost_go": 0.04}, now=NOW)
    wt = tmp_path / "wt" / ".ahub"
    wt.mkdir(parents=True)
    (wt / "report.md").write_text("## Summary\nthe wizard, the models and the service\n\n## Details\nlots\n",
                                  encoding="utf-8")
    (wt / "result.json").write_text(
        '{"summary": "The wizard asks for every provider and probes its models once. '
        'The answer is stored in the hub config.", '
        '"questions": ["Should the ru strings live in the catalog or in the setup wizard?"], '
        '"notes": "Not done: the rebrand of the Telegram card."}', encoding="utf-8")
    store.update_task(done, worktree=str(tmp_path / "wt"), round=3, now=NOW)
    row = store.add_session(task_id=done, provider="fake", role="executor", model="bunny", now=NOW)
    store.update_session(row, status="ok", cost_go=0.046)
    q = store.create_task(project="P", kind="scout", title="later", executor="spark", now=NOW)
    store.update_task(q, state_reason='{"code":"wait_accept","task":"T%d","state":"queued"}' % done,
                      phase="waiting", now=NOW)
    return {"active": a, "base": base, "done": done, "queued": q}


def test_status_detail_snapshot(tmp_path):
    store = Store()
    ids = _fill(store, tmp_path)
    text = views.task_text(store, store.get_task(ids["done"]), live={ids["done"]: 1}, now=NOW + 4 * HOUR, w=W)
    assert text == DETAIL


def test_status_overview_snapshot(tmp_path):
    from ahub import pulse

    store = Store()
    ids = _fill(store, tmp_path)
    live = {ids["active"]: 42}
    pulses = {ids["active"]: pulse.Pulse(ids["active"], "working", pid=42)}
    text = views.status_text(store, live=live, now=NOW + 2 * 60_000, pulses=pulses, w=W)
    assert text == OVERVIEW


def test_status_detail_tty_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "colour_on", lambda: True)
    store = Store()
    ids = _fill(store, tmp_path)
    text = views.task_text(store, store.get_task(ids["done"]), live={ids["done"]: 1}, now=NOW + 4 * HOUR, w=W)
    assert text == DETAIL_TTY


def test_status_overview_tty_snapshot(tmp_path, monkeypatch):
    from ahub import pulse

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    store = Store()
    ids = _fill(store, tmp_path)
    live = {ids["active"]: 42}
    pulses = {ids["active"]: pulse.Pulse(ids["active"], "working", pid=42, reason="running pytest")}
    text = views.status_text(store, live=live, now=NOW + 2 * 60_000, pulses=pulses, w=W)
    assert text == OVERVIEW_TTY


def test_status_overview_non_green_pulse_tty(tmp_path, monkeypatch):
    from ahub import pulse

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    store = Store()
    ids = _fill(store, tmp_path)
    live = {ids["active"]: 42}
    pulses = {ids["active"]: pulse.Pulse(ids["active"], "waiting", pid=42,
                                         reason="child processes running (python -m pytest)")}
    text = views.status_text(store, live=live, now=NOW + 2 * 60_000, pulses=pulses, w=W)
    lines = text.splitlines()
    assert lines[1] == "⏺ T1  Setup wizard: choose providers"
    assert lines[2] == "  \x1b[2m⎿\x1b[0m \x1b[2mwriting · bunny · 2 min · $0.046\x1b[0m"
    assert lines[3] == "  \x1b[2m⎿\x1b[0m \x1b[33m🟡 child processes running (python -m pytest)\x1b[0m"


def test_status_detail_active_task_keeps_pulse_colour_tty(tmp_path, monkeypatch):
    from ahub import pulse

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    store = Store()
    ids = _fill(store, tmp_path)
    pulses = {ids["active"]: pulse.Pulse(ids["active"], "working", pid=42)}
    text = views.task_text(store, store.get_task(ids["active"]), live={ids["active"]: 42}, pulses=pulses,
                           now=NOW + 2 * 60_000, w=W)
    lines = text.splitlines()
    assert lines[0] == "⏺ T1  code  Setup wizard: choose providers"
    assert lines[2] == "\x1b[2mState\x1b[0m  \x1b[32mworking · writing · process alive\x1b[0m"
    assert lines[3] == "\x1b[2mModel\x1b[0m  bunny  \x1b[2mReview\x1b[0m  spark ×2  \x1b[2mRound\x1b[0m  3"
    assert lines[4] == "\x1b[2mCost\x1b[0m   $0.046 Go of $1.50 budget"
    assert lines[5] == "\x1b[2mAge\x1b[0m    2 min"


def test_history_snapshot(tmp_path):
    store = Store()
    ids = _fill(store, tmp_path)
    transitions.move(store, ids["done"], State.ACCEPTED, now=NOW + 4 * HOUR)
    done = store.create_task(project="P", kind="scout", title="find the leak", executor="spark", now=NOW)
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, done, st, now=NOW + 12 * 60_000)
    row = store.add_session(task_id=done, provider="fake", role="scout", model="spark", now=NOW)
    store.update_session(row, status="ok", cost_go=0.012)
    transitions.move(store, done, State.ACCEPTED, now=NOW + 13 * 60_000)
    assert views.history_text(store, w=W) == HISTORY


def test_long_summary_wraps_without_cutting_a_word(tmp_path):
    store = Store()
    tid = store.create_task(project="P", kind="scout", title="leak", now=NOW)
    summary = "The pipeline was red because the retry pause grew with every attempt. " * 40
    wt = tmp_path / "wt" / ".ahub"
    wt.mkdir(parents=True)
    (wt / "result.json").write_text(json.dumps({"summary": summary}), encoding="utf-8")
    store.update_task(tid, worktree=str(tmp_path / "wt"), now=NOW)
    out = views.task_text(store, store.get_task(tid), now=NOW, w=W)
    lines = out.split("\n")
    assert all(len(ln) <= W for ln in lines)
    hint = "  … the rest: ahub result T1"
    assert hint in lines  # the hint is its own line, under the summary
    body = " ".join(ln.strip() for ln in lines[lines.index("Summary") + 1:lines.index(hint)] if ln.strip())
    assert body.endswith("…") and body.removesuffix("…").strip() in summary  # cut at a sentence, not a word
    assert len(out.encode()) <= views.L2_LIMIT
    assert views.SUMMARY_BYTES < views.L2_LIMIT  # the L1–L3 limits are not raised


def test_pipe_output_of_the_cli_has_no_ansi(tmp_path, monkeypatch, capsys):
    from ahub import cli

    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.chdir(tmp_path)  # outside every project — the scope sees the tasks of project P
    ids = _fill(Store(), tmp_path)
    assert cli.main(["status"]) == 0
    assert cli.main(["status", f"T{ids['done']}"]) == 0
    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out
    assert "\033[" not in out

def test_a_crowded_overview_shows_the_rows_that_fit_and_counts_the_rest():
    """L1 cuts after whole rows, never after a whole block: with 40 tasks you still see the tasks."""
    store = Store()
    for i in range(20):
        tid = store.create_task(project="P", kind="code", title=f"active task number {i} with a long title",
                                executor="bunny", now=NOW)
        transitions.move(store, tid, State.PREPARING, now=NOW)
        transitions.move(store, tid, State.WORKING, now=NOW)
    for i in range(20):
        store.create_task(project="P", kind="scout", title=f"queued {i}", executor="spark", now=NOW)
    out = views.status_text(store, live={}, now=NOW, w=W)
    lines = out.split("\n")
    active = [ln for ln in lines if "active task number" in ln]
    queued = [ln for ln in lines if re.match(r"\s+T\d+\s+queued", ln)]
    more = int(re.search(r"\+(\d+) more tasks", out).group(1))
    assert len(active) > 5 and queued  # both groups got whole rows before the counter
    assert len(active) + len(queued) + more == 40  # nothing is dropped silently
    assert "ahub top" in out
    assert len(out.encode()) <= views.L1_LIMIT


class _TTY:
    """A stdout that keeps what was written and claims to be (or not to be) a terminal."""

    def __init__(self, tty: bool) -> None:
        self.buf = ""
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self.buf += text
        return len(text)

    def flush(self) -> None:
        pass


def test_live_line_only_on_a_terminal(monkeypatch):
    out = _TTY(tty=True)
    monkeypatch.setattr(ui.sys, "stdout", out)
    with ui.Live("Checking 2 models…", total=2) as p:
        p.step()
        p.step()
    s0 = ui.styled("·", "accent")
    s1 = ui.styled("✢", "accent")
    s2 = ui.styled("✳", "accent")
    assert out.buf == (ui.CLEAR_LINE + f"{s0} Checking 2 models… (0/2)"
                       + ui.CLEAR_LINE + f"{s1} Checking 2 models… (1/2)"
                       + ui.CLEAR_LINE + f"{s2} Checking 2 models… (2/2)"
                       + ui.CLEAR_LINE)  # cleared at the end, whatever happens
    pipe = _TTY(tty=False)
    monkeypatch.setattr(ui.sys, "stdout", pipe)
    with ui.Live("Checking 2 models…", total=2) as p:
        p.step()
    assert pipe.buf == ""  # a pipe (Claude) gets nothing


def test_live_spins_when_the_total_is_unknown(monkeypatch):
    out = _TTY(tty=True)
    monkeypatch.setattr(ui.sys, "stdout", out)
    with ui.Live("Checking the providers…") as p:
        p.step()
        p.total(3)
        p.step()
    assert out.buf.count("·") == 1 and "Checking the providers… (1/3)" in out.buf


def test_plain_len_ignores_the_colour_codes(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdout", _Stream(tty=True))
    coloured = ui.styled("✓", "green")
    assert len(coloured) > 3 and ui.plain_len(coloured) == 1
    assert ui.plain_len("plain") == 5


def test_command_hint_is_a_runnable_command():
    """The second line of an error must be a command a person can paste: backticks dropped, prose cut."""
    from ahub import cli
    from ahub.cliutil import command_hint

    known = cli.command_names()
    assert {"status", "models", "role", "providers", "enable", "task", "new", "setup"} <= known
    assert command_hint("model codex: provider codex is off (ahub providers enable codex)", known) == \
        "ahub providers enable codex"
    assert command_hint("the fix: run `ahub doctor` and then retry", known) == "ahub doctor"
    assert command_hint("run ahub setup . to rewrite", known) == "ahub setup"
    assert command_hint("Run `ahub setup` to get started", known) == "ahub setup"
    # a chain of commands: the last one is still a command, the prose around it is not
    assert command_hint("roles need a free model; ahub models role scout --set-default spark-free", known) == \
        "ahub models role scout --set-default spark-free"
    # nothing runnable in the message — no hint line at all
    for msg in ("no such task T99", "Telegram is not installed: pip install 'ahub[telegram]'",
                "T1: the review has already started — it cannot be changed", "the hub runs the tasks"):
        assert command_hint(msg, known) == "", msg


def test_table_measures_coloured_cells_by_what_is_seen():
    """A coloured cell is as wide as its visible text; a cut never lands inside an escape sequence."""
    mark = "\033[32m🟢\033[0m"
    out = ui.table(["", "task"], [[mark, "T1"], ["x", "T22"]], w=40).split("\n")
    assert mark in out[1]  # fits — kept whole, colour and all
    assert ui.plain_len(out[1][:out[1].index("T1")]) == ui.plain_len(out[2][:out[2].index("T22")])
    cut = ui.table(None, [["\033[1m" + "word " * 30 + "\033[0m", "end"]], w=30)
    assert "…" in cut and "\033" not in cut  # a cut cell drops its colour instead of a broken code


def test_item_details_never_move_onto_the_title_line(monkeypatch):
    """One rhythm for every item: the details are on their own ⎿ line, whatever the width leaves free."""
    monkeypatch.setattr(ui, "colour_on", lambda: True)
    head, detail = "T1  Setup wizard", "writing · bunny · 2 min · $0.046"
    for w in (40, 100, 200):
        lines = ui.item(head, [detail], w=w).split("\n")
        assert len(lines) == 2, f"w={w}: the title line keeps nothing else"
        assert lines[0] == f"⏺ {head}"  # working is white: the mark takes no colour
        assert lines[1] == f"  \x1b[2m⎿\x1b[0m \x1b[2m{detail}\x1b[0m"
    assert ui.item(head, [], w=200) == f"⏺ {head}"  # no details, no empty spine line


def test_a_green_pulse_takes_no_line_of_its_own(monkeypatch):
    """A green pulse is what the details line already says — only a pulse that is not green gets a line."""
    from ahub import pulse

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    assert views._pulse_detail(pulse.Pulse(1, "working")) == ""
    assert views._pulse_detail(pulse.Pulse(1, "working", reason="quiet for 3 min")) == ""  # green, no line
    assert views._pulse_detail(None) == ""
    waiting = views._pulse_detail(pulse.Pulse(1, "waiting", reason="child processes running (python -m pytest)"))
    assert waiting == "\033[33m🟡 child processes running (python -m pytest)\033[0m"


def test_kv_labels_are_dim_and_the_values_keep_their_weight(monkeypatch):
    monkeypatch.setattr(ui, "colour_on", lambda: True)
    block = ui.kv([("State", ui.styled("working", "green")), ("Model", ["bunny", ("Review", "spark")])], dim=True)
    assert block.split("\n") == ["\x1b[2mState\x1b[0m  \x1b[32mworking\x1b[0m",
                                  "\x1b[2mModel\x1b[0m  bunny    \x1b[2mReview\x1b[0m  spark"]
    # a coloured value is measured by what is seen, so the columns after it still line up
    rows = ui.kv([("a", [ui.styled("long value", "green"), ("b", "x")]), ("bb", "y")], dim=True)
    lines = rows.split("\n")
    assert lines[0].endswith("\x1b[2mb\x1b[0m  x")
    assert ui.plain_len(lines[0][:lines[0].index("x")]) == 4 + 10 + 2 + 3
    assert ui.kv([("State", "working")]) == "State  working"  # a pipe — nothing is dim


def test_home_screen_tty_snapshot(tmp_path, monkeypatch):
    import ahub
    from ahub import home, paths

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    monkeypatch.setattr(home, "_project_here", lambda: "demo")
    monkeypatch.setattr(paths, "global_config_path", lambda: tmp_path / "nonexistent.toml")
    text = home.text(w=W)
    lines = text.splitlines()
    assert lines[0] == "\x1b[2m╭───────────────────────────────────────╮\x1b[0m"
    assert lines[1] == f"│ \x1b[38;5;208m✻ ahub\x1b[0m \x1b[2m{ahub.__version__} · demo · service stopped\x1b[0m │"
    assert lines[2] == "\x1b[2m╰───────────────────────────────────────╯\x1b[0m"
    assert lines[3] == "⏺ the hub is not configured yet — run `ahub setup` to get started"
    assert lines[4] == "\x1b[2mahub setup · ahub doctor · ahub models\x1b[0m"


def test_action_result_and_error_tty(monkeypatch):
    monkeypatch.setattr(ui, "colour_on", lambda: True)
    res = ui.item("T12 merged into main", [views._t("views.hint_next", cmd="ahub status")],
                  status="success")
    assert res == "\x1b[32m⏺\x1b[0m T12 merged into main\n  \x1b[2m⎿\x1b[0m \x1b[2mNext: ahub status\x1b[0m"

    err = ui.failed("something broke", "hint: ahub doctor")
    assert err == "\x1b[31m✗ something broke\x1b[0m\n  \x1b[2m⎿\x1b[0m \x1b[2mhint: ahub doctor\x1b[0m"


def test_table_head_is_dim_without_separators_on_tty(monkeypatch):
    monkeypatch.setattr(ui, "colour_on", lambda: True)
    out = ui.table(["task", "state"], [["T1", "done"]], w=30)
    lines = out.splitlines()
    assert lines[0] == "\x1b[2mtask  state\x1b[0m"
    assert lines[1] == "T1    done"


def test_home_screen_configured_tty_snapshot(tmp_path, monkeypatch):
    import ahub
    from ahub import home, paths, pulse, reasons, transitions
    from ahub.model import Kind, State
    from ahub.service import HEARTBEAT_KEY

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / ".hub.toml").write_text('schema_version = 2\nname = "demo"\n', encoding="utf-8")
    monkeypatch.chdir(demo_dir)
    monkeypatch.setattr(paths, "global_config_path", lambda: tmp_path / "global.toml")
    (tmp_path / "global.toml").write_text("projects = []\n", encoding="utf-8")
    monkeypatch.setattr("ahub.home.now_ms", lambda: NOW + 30_000)

    store = Store()
    working = store.create_task(project="demo", kind=Kind.CODE, title="Setup wizard: choose providers",
                                executor="spark", now=NOW)
    transitions.move(store, working, State.PREPARING, now=NOW)
    transitions.move(store, working, State.WORKING, now=NOW)
    store.update_task(working, phase="writing", now=NOW)

    waiting = store.create_task(project="demo", kind=Kind.SCOUT, title="find the leak", now=NOW)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, waiting, st, now=NOW)
    transitions.move(store, waiting, State.DONE, reason=reasons.dump("review_exhausted", n=2, highs=1), now=NOW)

    store.meta_set(HEARTBEAT_KEY, str(NOW + 30_000))

    monkeypatch.setattr("ahub.home.live_workers", lambda: {working: 42})
    monkeypatch.setattr(pulse, "all_pulses", lambda store, live, projects, now: {
        working: pulse.Pulse(working, "working", pid=42, reason="running pytest")
    })

    text = home.text(w=W)
    expected = "\n".join([
        "\x1b[2m╭───────────────────────────────────────╮\x1b[0m",
        f"│ \x1b[38;5;208m✻ ahub\x1b[0m \x1b[2m{ahub.__version__} · demo · service running\x1b[0m │",
        "\x1b[2m╰───────────────────────────────────────╯\x1b[0m",
        "⏺ T1  Setup wizard: choose providers",
        "  \x1b[2m⎿\x1b[0m \x1b[2mwriting · spark · 0 min\x1b[0m",
        "\x1b[33m⏺\x1b[0m Waiting for you",
        '  \x1b[2m⎿\x1b[0m \x1b[2mahub accept T2 · ahub rework T2 --notes "…" · ahub reject T2\x1b[0m',
        '\x1b[2mahub status · ahub top · ahub doctor · ahub task new --kind scout --title "…"\x1b[0m',
    ])
    assert text == expected


def test_action_result_and_error_cli_tty(capsys, monkeypatch):
    from ahub import cli, transitions
    from ahub.model import Kind, State

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    store = Store()
    tid = store.create_task(project="P", kind=Kind.CODE, title="wizard")
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, tid, st)

    monkeypatch.setattr("ahub.commands.task._project_of", lambda store, task: None)
    monkeypatch.setattr("ahub.accept.accept", lambda store, project, tid, by="orchestrator": "T12 merged into main")

    rc = cli.main(["--all", "accept", f"T{tid}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == (
        "\x1b[32m⏺\x1b[0m T12 merged into main\n"
        '  \x1b[2m⎿\x1b[0m \x1b[2mNext: ahub status · ahub task new --kind scout --title "…"\x1b[0m\n'
    )

    rc = cli.main(["accept", "T999"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err == (
        "\x1b[31m✗ no such task T999\x1b[0m\n"
        "  \x1b[2m⎿\x1b[0m \x1b[2mhint: ahub status\x1b[0m\n"
    )


def test_box_with_overlong_header_aligns():
    long_line = "✻ ahub 3.0.0 · " + "p" * 80 + " · service running"
    out = ui.box([long_line], w=50)
    lines = out.splitlines()
    assert len(lines) == 3
    assert ui.plain_len(lines[0]) == 52
    assert ui.plain_len(lines[1]) == 52
    assert ui.plain_len(lines[2]) == 52
    assert lines[1].startswith("│ ")
    assert lines[1].endswith("│")
    assert "…" in lines[1]

