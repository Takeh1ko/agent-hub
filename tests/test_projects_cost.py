"""The owner's view across projects: `ahub projects`, `ahub cost` and the `ahub top` data layer.

One hub, two repositories, sessions with money: the projects command counts what each project is busy with
and what it spent this month; `ahub cost` breaks the hub sessions down per project and per model; the
`ahub top` table groups the rows by project and its money cell counts hub sessions of the shown scope —
never the machine-wide opencode.db.
"""

from __future__ import annotations

import json

import pytest

from ahub import cli, comms, cost, paths, transitions
from ahub.i18n import t
from ahub.model import State
from ahub.scope import OWNER, Scope
from ahub.store import Store
from ahub.time import fmt_local, now_ms
from ahub.tui import data
from tests.conftest import write


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """Projects A and B in one hub; the current directory — A. Returns the roots by name."""
    roots = {}
    for name in ("A", "B"):
        root = tmp_path / name.lower()
        write(root / ".hub.toml", f'schema_version = 2\nname = "{name}"\nmax_parallel = 2\n')
        roots[name] = root
    write(paths.global_config_path(), f'projects = ["{roots["A"]}", "{roots["B"]}"]\n')
    monkeypatch.chdir(roots["A"])
    return roots


def ahub(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out.strip(), out.err.strip()


def session(store: Store, tid: int, model: str, go: float, usd: float = 0.0, *, now: int | None = None) -> None:
    """A finished hub session with money — what the worker did through the hub."""
    sid = store.add_session(task_id=tid, provider="opencode", role="executor", model=model, round=1,
                            external_id=f"ses_{tid}_{len(store.list_sessions())}", now=now)
    store.update_session(sid, status="ok", cost_go=go, cost_usd=usd)


def working(store: Store, project: str, title: str, *, model: str = "spark", go: float = 0.0,
            usd: float = 0.0) -> int:
    tid = store.create_task(project=project, kind="code", title=title)
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)
    if go or usd:
        session(store, tid, model, go, usd)
    return tid


def filled(store: Store) -> dict[str, int]:
    """A: one working task and one waiting for a decision; B: one working and one in the queue."""
    out = {"a_working": working(store, "A", "pay button", go=0.30, usd=0.05)}
    out["a_done"] = store.create_task(project="A", kind="scout", title="find the leak")
    transitions.move(store, out["a_done"], State.PREPARING)
    transitions.move(store, out["a_done"], State.WORKING)
    transitions.move(store, out["a_done"], State.DONE, reason="report ready")
    session(store, out["a_done"], "spark", 0.12)
    out["b_working"] = working(store, "B", "migration", model="deepseek-flash", go=0.02)
    out["b_queued"] = store.create_task(project="B", kind="scout", title="later")
    comms.ask(store, "ship it?", project="A")
    comms.ask(store, "why B?", project="B")
    comms.ask(store, "hub-wide?", project="")
    return out


# --- ahub projects ---


def test_projects_one_row_per_project_with_counts_and_money(hub, store, capsys):
    filled(store)
    rc, out, err = ahub(capsys, "projects")
    assert rc == 0, err
    lines = {ln.split()[0]: ln for ln in out.splitlines()[1:]}
    assert set(lines) == {"A", "B"}
    # A: 1 working + 1 done (waits for a decision); B: 1 working + 1 in the queue
    assert lines["A"].split()[2:8] == ["1", "0", "1", "1", "0.420", "0.050"]
    assert lines["B"].split()[2:8] == ["1", "1", "0", "1", "0.020", "0.000"]
    # the path column is cut to the width when it does not fit — its head is what matters
    assert lines["A"].split()[1].startswith(str(hub["A"])[:20])
    assert lines["B"].split()[1].startswith(str(hub["B"])[:20])
    assert lines["A"].split()[-1] != "—"  # it was touched today


def test_projects_json_and_open_questions(hub, store, capsys):
    tids = filled(store)
    answered = comms.ask(store, "answered long ago?", project="A")
    comms.answer(store, answered, "yes")  # an answered question is not open
    store.update_task(tids["a_working"], phase="writing", now=now_ms())  # the newer touch of A
    rc, out, _ = ahub(capsys, "--json", "projects")
    assert rc == 0
    data_ = json.loads(out)
    by_name = {p["name"]: p for p in data_["projects"]}
    assert by_name["A"]["active"] == 1 and by_name["A"]["decision"] == 1 and by_name["A"]["queued"] == 0
    assert by_name["A"]["questions"] == 1  # the answered one and the hub-wide one are not open
    assert by_name["B"]["queued"] == 1 and by_name["B"]["questions"] == 1
    assert by_name["A"]["go"] == 0.42 and by_name["A"]["usd"] == 0.05
    assert by_name["B"]["go"] == 0.02 and by_name["B"]["usd"] == 0.0
    # `last` is the newest touch of the project, not the first task that happened to be counted
    assert by_name["A"]["last"] == store.get_task(tids["a_working"]).updated_at
    assert by_name["A"]["last"] > store.get_task(tids["a_done"]).updated_at
    assert data_["errors"] == [] and data_["problems"] == {"A": [], "B": []}
    assert data_["month"].count("-") == 2  # the first day of this month


def moved(store: Store, tid: int, *steps: tuple[State, int]) -> int:
    """Walk a task through the given states, each with its own timestamp."""
    for state, ts in steps:
        transitions.move(store, tid, state, now=ts)
    return tid


def test_projects_last_is_the_newest_touch_not_the_last_row_of_the_group_by(hub, store, capsys):
    """`last` — the newest touch of the project, whatever the group-by loop hands out.

    Two traps, both planted here: the newest touch of A is in the group SQLite sorts *first*
    ('done' before 'queued' and 'working'), so "the last group wins" is wrong; and the two working
    tasks of B are touched oldest-first, so a bare column instead of MAX inside the group is wrong too.
    """
    def work(project: str, ts: int) -> int:
        return moved(store, store.create_task(project=project, kind="code", title="work"),
                     (State.PREPARING, ts - 1), (State.WORKING, ts))

    store.create_task(project="A", kind="scout", title="later", now=1000)  # queued, the oldest touch of A
    work("A", 2000)
    moved(store, store.create_task(project="A", kind="scout", title="ready"),
          (State.PREPARING, 2999), (State.WORKING, 2999), (State.DONE, 3000))  # the newest touch of A
    work("B", 4000)
    work("B", 5000)  # the newest touch of B sits on the later id of its group
    moved(store, store.create_task(project="B", kind="scout", title="ready"),
          (State.PREPARING, 100), (State.WORKING, 100), (State.DONE, 100))

    by_name = {p["name"]: p for p in json.loads(ahub(capsys, "--json", "projects")[1])["projects"]}
    assert by_name["A"]["last"] == 3000 and by_name["B"]["last"] == 5000
    lines = {ln.split()[0]: ln for ln in ahub(capsys, "projects")[1].splitlines()[1:]}  # the same moment
    assert lines["A"].rstrip().endswith(fmt_local(3000))
    assert lines["B"].rstrip().endswith(fmt_local(5000))


def test_projects_marks_a_project_with_tasks_but_no_config(hub, store, capsys):
    """A task of a project that is not connected to the hub: the owner must see it, marked as a problem."""
    store.create_task(project="Ghost", kind="scout", title="orphan")
    rc, out, _ = ahub(capsys, "--lang", "en", "projects")
    assert rc == 1
    assert "! Ghost" in out and "not connected to the hub" in out
    data_ = json.loads(ahub(capsys, "--json", "projects")[1])
    assert data_["unconnected"] == ["Ghost"]
    # the JSON has no root for it — never the "—" that stands in for it in the table
    ghost = next(p for p in data_["projects"] if p["name"] == "Ghost")
    assert ghost["root"] is None and "—" not in json.dumps(ghost, ensure_ascii=False)


def test_projects_marks_a_project_with_a_problem_on_disk(hub, store, capsys):
    write(hub["B"] / ".hub.toml", 'schema_version = 2\nname = "B"\nrules = "nope.md"\n')
    rc, out, _ = ahub(capsys, "projects")
    assert rc == 1 and "! B" in out and "rules" in out
    assert out.splitlines()[1].split()[0] == "A"  # the healthy project is still in the table


def test_projects_reports_a_broken_project_file(hub, store, capsys):
    """A .hub.toml that does not parse: the entry is an error line, the healthy project stays in the table."""
    write(hub["B"] / ".hub.toml", 'schema_version = 2\nname = "B\nmax_parallel = 2\n')
    rc, out, _ = ahub(capsys, "projects")
    assert rc == 1 and str(hub["B"] / ".hub.toml") in out
    assert "A" in out.splitlines()[1]


# --- ahub cost ---


def test_cost_per_project_and_model(hub, store, capsys):
    filled(store)
    rc, out, err = ahub(capsys, "--lang", "en", "cost", "--all")
    assert rc == 0, err
    rows = [ln.split() for ln in (x.strip() for x in out.splitlines()) if ln.startswith(("A ", "B "))]
    assert rows == [["A", "spark", "2", "0.420", "0.050"], ["B", "deepseek-flash", "1", "0.020", "0.000"]]
    assert "go $0.440 · usd $0.050 · sessions 3" in out  # the totals of the scope

    data_ = json.loads(ahub(capsys, "--json", "cost", "--all")[1])
    assert data_["totals"] == {"go": 0.44, "usd": 0.05, "sessions": 3}
    assert [(m["project"], m["model"], m["sessions"]) for m in data_["models"]] == \
        [("A", "spark", 2), ("B", "deepseek-flash", 1)]
    assert {p["project"] for p in data_["projects"]} == {"A", "B"}


def test_cost_follows_the_scope_of_the_directory(hub, store, capsys):
    """From A's directory the command counts A; --project B opens the other one; --all everything."""
    filled(store)
    out = ahub(capsys, "cost")[1]
    assert "A" in out and "B" not in out and "go $0.420" in out
    assert "go $0.020" in ahub(capsys, "cost", "--project", "B")[1]
    assert "go $0.440" in ahub(capsys, "cost", "--all")[1]


def test_cost_since_and_bad_since(hub, store, capsys):
    """--since cuts the period; without it — this month (the default of the owner's view)."""
    tid = working(store, "A", "old task")
    session(store, tid, "spark", 0.5)
    session(store, tid, "spark", 0.7, now=0)  # a session of long ago
    assert "go $0.500" in ahub(capsys, "cost", "--all")[1]
    old = ahub(capsys, "cost", "--all", "--since", "1970-01-01")[1]
    assert "go $1.200" in old and "2" in old.splitlines()[1]
    rc, out, err = ahub(capsys, "cost", "--all", "--since", "nonsense")
    assert rc == 2 and out == "" and "nonsense" in err


def test_cost_counts_hub_sessions_only(hub, store, capsys, monkeypatch):
    """The machine-wide opencode.db is not a project's bill — its totals must not be read here."""
    from ahub.providers import opencode_db

    def _boom(*_a, **_kw):
        raise AssertionError("ahub cost must not read the machine-wide opencode.db")

    monkeypatch.setattr(opencode_db, "totals", _boom)
    working(store, "A", "pay button", go=0.30)
    assert "go $0.300" in ahub(capsys, "cost", "--all")[1]


def test_two_models_of_one_project_are_summed(hub, store, capsys):
    """The money of a project is the sum of its sessions whatever model they ran on.

    The group of the per-project query is the project alone: grouping by the model name as well binds
    it to the session column, splits the project into one group per model and leaves the cheapest one.
    """
    tid = working(store, "A", "pay button")
    session(store, tid, "spark", 0.30, 0.05)
    session(store, tid, "mimo-flash", 0.02)
    session(store, tid, "spark", 0.40)
    assert cost.by_project(store, scope=OWNER)["A"] == cost.Money(0.72, 0.05)
    assert cost.by_project(store, scope=Scope(("A",))) == {"A": cost.Money(0.72, 0.05)}
    assert cost.total(store, scope=Scope(("A",))) == cost.Money(0.72, 0.05)

    line = {ln.split()[0]: ln for ln in ahub(capsys, "projects")[1].splitlines()[1:]}["A"]
    assert line.split()[6:8] == ["0.720", "0.050"]
    row = json.loads(ahub(capsys, "--json", "projects")[1])["projects"][0]
    assert row["go"] == 0.72 and row["usd"] == 0.05
    out = ahub(capsys, "--lang", "en", "cost", "--all")[1]
    assert "go $0.720 · usd $0.050 · sessions 3" in out  # the totals too


def test_cost_data_layer_groups_and_sums(hub, store):
    """ahub/cost.py: per project, per model, the whole scope; sessions without a task are the hub's."""
    filled(store)
    hub_sid = store.add_session(task_id=None, provider="agy", role="executor", model="gemini-low")
    store.update_session(hub_sid, status="ok", cost_go=0.01)

    assert cost.by_project(store, scope=OWNER) == {"A": cost.Money(0.42, 0.05), "B": cost.Money(0.02, 0.0),
                                                    "": cost.Money(0.01, 0.0)}
    assert cost.by_project(store, scope=Scope(("A",))) == {"A": cost.Money(0.42, 0.05)}
    models = cost.by_model(store, scope=Scope(("A",)))
    assert [(m.project, m.model, m.sessions, m.go, m.usd) for m in models] == [("A", "spark", 2, 0.42, 0.05)]
    assert cost.total(store, scope=Scope(("A",))) == cost.Money(0.42, 0.05)
    assert cost.total(store) == cost.Money(0.45, 0.05)
    assert cost.total(store, scope=Scope(("NOPE",))) == cost.Money()
    assert cost.month_start(0) <= cost.month_start(now_ms())


def test_projects_table_keeps_the_name_whole_in_a_narrow_terminal(hub, store, capsys, monkeypatch):
    """A narrow terminal drops the least important columns; the name and its `!` mark are never cut."""
    filled(store)
    store.create_task(project="Ghost", kind="scout", title="orphan")  # a row with a `!` mark
    monkeypatch.setenv("COLUMNS", "60")
    out = ahub(capsys, "--lang", "en", "projects")[1]
    lines = out.splitlines()
    assert "! Ghost" in out and any(ln.strip().startswith("A ") for ln in lines)  # the whole name cells, no `!…`
    assert not any(ln.rstrip().endswith("…") and len(ln.split()) < 3 for ln in lines)
    for dropped in ("path", "questions", "last"):
        assert t(f"projects.col_{dropped}") not in lines[0]
    assert t("projects.col_project") in lines[0] and t("projects.col_go") in lines[0]
    monkeypatch.setenv("COLUMNS", "120")  # a wide terminal keeps them all
    assert t("projects.col_path") in ahub(capsys, "projects")[1].splitlines()[0]


def test_projects_never_cuts_the_name_however_wide_it_is(hub, store, capsys, monkeypatch):
    """The name cell is the widest one and it still comes out whole — the other columns give way."""
    long = "a-very-long-project-name"
    store.create_task(project=long, kind="scout", title="orphan")
    monkeypatch.setenv("COLUMNS", "40")
    out = ahub(capsys, "--lang", "en", "projects")[1]
    lines = out.splitlines()
    row = next(ln for ln in lines if ln.lstrip().startswith("!"))  # the row of the unconnected project
    assert row.lstrip().startswith(f"! {long}")
    assert "…" not in row  # no cell of it was cut, not even the widest one
    assert t("projects.col_go") in lines[0]  # the money outlasted the counts
    for dropped in ("active", "queued", "decision", "questions", "last", "path"):
        assert t(f"projects.col_{dropped}") not in lines[0]


# --- ahub top data layer ---


def test_top_group_money_is_the_true_sum_not_a_sum_of_rounded_cells(hub, store):
    """Five sessions of $0.0006: the raw sum is 0.003, a sum of the rounded cells would be 0.005."""
    for i in range(4):
        working(store, "A", f"a{i}", go=0.0006)
    working(store, "A", "a4", go=0.0006, usd=0.0004)
    working(store, "B", "b", go=0.01)
    rows = data.rows(store, {}, now_ms())
    group = {r.project: r for r in rows if r.header}["A"]
    assert group.cost == "0.003"  # 0.0030 go + 0.0004 usd, rounded once
    assert abs(group.go - 0.003) < 1e-12 and group.usd == 0.0004
    assert [r.cost for r in rows if not r.header][:5] == ["0.001"] * 4 + ["0.001"]
    whole = cost.total(store, scope=Scope(("A",)))
    assert abs(whole.go - group.go) < 1e-12 and whole.usd == group.usd  # the money `ahub cost` prints


def test_top_filtered_history_is_complete(hub, store):
    """`o` + `h`: the finished tasks of one project must not be cut by the newer tasks of another."""
    b_old = store.create_task(project="B", kind="code", title="old B")
    for st in (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED):
        transitions.move(store, b_old, st, now=0)
    for i in range(40):  # newer tasks of A — they would eat the history limit of the whole table
        tid = store.create_task(project="A", kind="scout", title=f"a{i}")
        for st in (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED):
            transitions.move(store, tid, st)
    screen, _live, _pulses = data.snapshot(store, projects=[], history=True, only="B")
    assert [r.task_id for r in screen.rows] == [b_old]
    assert screen.projects == ["A", "B"]  # the key still cycles every project of the hub


def test_top_rows_are_grouped_by_project(hub, store):
    tids = filled(store)
    rows = data.rows(store, {}, now_ms())
    groups = [r for r in rows if r.header]
    assert [r.project for r in groups] == ["A", "B"]
    assert [r.label for r in groups] == ["A", "B"]
    assert groups[0].mark == data.GROUP_MARK and groups[0].task_id == 0
    # the money of a group is the hub sessions of the rows under it (not the machine-wide opencode.db)
    assert groups[0].cost == "0.470" and groups[1].cost == "0.020"  # go + usd of the rows below
    assert [r.label for r in rows] == ["A", "T1", "T2", "B", "T3", "T4"]  # the group, then its tasks
    assert {r.task_id for r in rows if not r.header} == {tids["a_working"], tids["a_done"], tids["b_working"],
                                                         tids["b_queued"]}


def test_top_group_row_is_money_of_the_shown_tasks_only(hub, store):
    """An accepted task of A is not in the current view — its money is not in the group's cell."""
    working(store, "A", "a", go=0.10)
    done = store.create_task(project="A", kind="code", title="b")
    for st in (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED):
        transitions.move(store, done, st)
    session(store, done, "spark", 5.00)
    working(store, "B", "c", go=0.01)
    groups = {r.project: r.cost for r in data.rows(store, {}, now_ms()) if r.header}
    assert groups == {"A": "0.100", "B": "0.010"}
    old = {r.project: r.cost for r in data.rows(store, {}, now_ms(), history=True) if r.header}
    assert old["A"] == "5.100"


def test_top_one_project_needs_no_group_row(hub, store):
    """A single project in the table: no header row — the name would say nothing new."""
    working(store, "A", "only one")
    rows = data.rows(store, {}, now_ms())
    assert [r.header for r in rows] == [False]


def test_top_filters_to_one_project(hub, store):
    filled(store)
    only_a = data.rows(store, {}, now_ms(), only="A")
    assert [r.project for r in only_a] == ["A", "A"]
    assert not any(r.header for r in only_a)  # one project left — no header
    screen, _live, _pulses = data.snapshot(store, projects=[], only="B")
    assert {r.project for r in screen.rows} == {"B"}
    assert screen.projects == ["A", "B"]  # the key cycles through every project of the table
    assert data.snapshot(store, projects=[])[0].projects == ["A", "B"]


def test_top_header_counts_follow_the_project_filter(hub, store):
    """One scope per screen: the counts of the header describe the rows shown under it."""
    filled(store)
    comms.raise_alarm(store, "B is on fire", critical=True, project="B")
    comms.raise_alarm(store, "the hub is on fire", critical=True)  # hub-wide — in every scope
    whole = data.snapshot(store, projects=[])[0]
    a = data.snapshot(store, projects=[], only="A")[0]
    b = data.snapshot(store, projects=[], only="B")[0]
    assert "работают 2" in whole.header and "ждут решения 1" in whole.header
    assert "в очереди 1" in whole.header and "тревог 2" in whole.header
    assert "работают 1" in a.header and "работают 1" in b.header  # one working task in each
    assert "ждут решения 1" in a.header and "ждут решения 0" in b.header
    assert "в очереди 0" in a.header and "в очереди 1" in b.header
    assert "тревог 1" in a.header and "тревог 2" in b.header  # the hub-wide alarm is in both
    assert {r.project for r in a.rows} == {"A"} and {r.project for r in b.rows} == {"B"}


async def test_top_key_narrows_the_table_to_one_project(hub, store):
    from ahub.tui.app import TopApp

    tids = filled(store)
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert app._ids[0] == 0  # the header row of A
        assert app._ids == [0, tids["a_working"], tids["a_done"], 0, tids["b_working"], tids["b_queued"]]
        await pilot.press("o")
        await pilot.pause(0.4)
        assert app.project == "A" and app._ids == [tids["a_working"], tids["a_done"]]  # A alone
        assert "проект: A" in str(app.query_one("#mode").render())
        await pilot.press("o")
        await pilot.pause(0.4)
        assert app.project == "B" and app._ids == [tids["b_working"], tids["b_queued"]]
        await pilot.press("o")
        await pilot.pause(0.4)
        assert app.project == "" and len(app._ids) == 6  # back to every project


async def test_top_header_row_opens_nothing(hub, store):
    """The cursor may sit on a project header — no action reaches through it."""
    from ahub.tui.app import TopApp

    filled(store)
    app = TopApp(store=store, projects=[], control=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert app.query_one("#tasks").cursor_row == 0
        await pilot.press("enter")  # the transcript of... no task
        await pilot.pause(0.3)
        app.screen.query_one("#tasks")  # the table is still on top — no transcript of nothing
        await pilot.press("s")  # stop — nothing to stop
        await pilot.pause(0.3)
        assert store.get_task(1).state is State.WORKING
        await pilot.press("m")  # a message to the worker of... no task
        await pilot.pause(0.3)
        assert app.screen.__class__.__name__ != "Ask"
        assert not any("T0" in n.message for n in app._notifications._notifications)


async def test_top_project_filter_waits_for_the_refresh_in_flight(hub, store, monkeypatch):
    """`o` must not be swallowed by the refresh that is already running: the request is kept.

    The refresh in flight is a real one — a thread stuck inside the snapshot until the test opens the
    gate — and nothing but the kept request may serve the table: the 2 s tick is off, so this test
    stands or falls on the pending block of `_apply`.
    """
    import threading

    from ahub.tui import data as tdata
    from ahub.tui.app import TopApp

    tids = filled(store)
    monkeypatch.setattr(TopApp, "set_interval", lambda self, *a, **kw: None)  # no periodic rescue
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert len(app._ids) == 6
        gate, real = threading.Event(), tdata.snapshot
        monkeypatch.setattr(tdata, "snapshot",
                            lambda *a, **kw: (gate.wait(10), real(*a, **kw))[1])
        app.refresh_data()  # the refresh that is in flight
        await pilot.pause(0.2)
        assert app._busy
        await pilot.press("o")  # the request arrives while it runs
        await pilot.pause(0.2)
        assert app.project == "A" and app._pending is True and len(app._ids) == 6
        monkeypatch.setattr(tdata, "snapshot", real)  # the kept request must not wait at the gate
        gate.set()  # the refresh in flight ends...
        await pilot.pause(0.5)  # ...and the kept request is served right after it
        assert app._pending is False
        assert app._ids == [tids["a_working"], tids["a_done"]]


async def test_top_keeps_the_cursor_on_the_group_it_was_on(hub, store):
    """The screen refreshes every 2 s; a header row is not a task, so the cursor holds its place."""
    from ahub.tui.app import TopApp

    filled(store)
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.query_one("#tasks").move_cursor(row=3)  # the header row of B
        await pilot.pause(2.5)  # a refresh of the screen
        assert app.query_one("#tasks").cursor_row == 3


async def test_top_cursor_survives_a_refresh_that_empties_the_table(hub, store):
    """No rows left — the cursor goes back to the top row, never to row -1 (nothing is picked)."""
    from ahub.tui.app import TopApp

    tids = filled(store)
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.query_one("#tasks").move_cursor(row=3)  # the header row of B
        ahead = {State.QUEUED: (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED),
                 State.WORKING: (State.DONE, State.ACCEPTED), State.DONE: (State.ACCEPTED,)}
        for tid in tids.values():  # every task leaves the current view (a stopped task still waits)
            for st in ahead[store.get_task(tid).state]:
                transitions.move(store, tid, st)
        app.refresh_data()
        await pilot.pause(0.4)
        assert app._ids == [] and app.selected() is None
        assert app.query_one("#tasks").cursor_row == 0
        assert "нет задач" in str(app.query_one("#detail").render())
