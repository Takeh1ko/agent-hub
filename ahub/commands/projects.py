"""ahub projects — the owner's view across projects: where each one stands and what it cost.

One row per project: the path, how much is going on (active / queued / waiting for a decision), open owner
questions, the money of this month from the hub sessions (Go and USD apart) and when it was last touched.
A project with config problems is marked `!` and its problems are printed under the row; a project that
has tasks but is not connected to the hub is listed too, as a problem. In a narrow terminal the least
important columns go first — the questions, the last activity, the path — the name and its `!` mark
stay whole.

The whole hub is shown, not the scope of the current directory: this is the owner's glance at every
repository the hub serves (architecture §9), the same as `ahub cost --all`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ahub import config, cost, ui
from ahub.cliutil import emit
from ahub.model import ACTIVE, WAITING_DECISION, State
from ahub.store import Store
from ahub.time import fmt_local, to_local

EMPTY = "—"
# The columns in order: a name, the cap for ui.table (None — as wide as the content needs) and whether the
# column is FLEX (it may take any width, so it is the one that gives when the terminal is narrow).
COLUMNS = (("project", 16, False), ("path", None, True), ("active", None, False), ("queued", None, False),
           ("decision", None, False), ("questions", None, False), ("go", 9, False), ("usd", 9, False),
           ("last", 13, False))
DROP_FIRST = ("questions", "last", "path")  # least important first — the name and its `!` mark stay
FLEX_MIN = 12  # what the flexible column is assumed to need while deciding what to drop


@dataclass
class Stat:
    """What one project is busy with and what it spent this month."""

    active: int = 0
    queued: int = 0  # in the queue, waiting for a slot ('waiting' in the rest of the hub means a decision)
    decision: int = 0
    questions: int = 0
    go: float = 0.0
    usd: float = 0.0
    last: int = 0


def _bump(st: Stat, state: State) -> None:
    if state in ACTIVE:
        st.active += 1
    elif state is State.QUEUED:
        st.queued += 1
    elif state in WAITING_DECISION:
        st.decision += 1


def stats(store: Store, since: int) -> dict[str, Stat]:
    """What every project is busy with and what it spent since `since` — three queries for the whole hub.

    The money comes from the hub sessions (ahub/cost.py); a session belongs to the project of its task, so
    the counts are grouped by the project the same way.
    """
    out: dict[str, Stat] = {}

    def of(name: str) -> Stat:
        return out.setdefault(name, Stat())

    with store.read() as c:
        for r in c.execute("SELECT project, state, COUNT(*) AS n, MAX(updated_at) AS last FROM task"
                           " WHERE project!='' GROUP BY project, state"):
            st = of(str(r["project"]))
            _bump(st, State(r["state"]))
            st.last = max(st.last, int(r["last"] or 0))
        for r in c.execute("SELECT project, COUNT(*) AS n FROM question WHERE status='open' GROUP BY project"):
            of(str(r["project"])).questions = int(r["n"])
    for name, money in cost.by_project(store, since=since).items():
        st = of(name)
        st.go, st.usd = money.go, money.usd
    return out


def _row_cells(name: str, root: str, st: Stat, mark: str) -> dict[str, str]:
    """One project as the strings of the table (the name cell carries the `!` mark of a problem)."""
    return {"project": f"{mark} {name}", "path": root, "active": str(st.active), "queued": str(st.queued),
            "decision": str(st.decision), "questions": str(st.questions), "go": f"{st.go:.3f}",
            "usd": f"{st.usd:.3f}", "last": fmt_local(st.last) if st.last else EMPTY}


def _keys(rows: list[dict[str, str]], w: int) -> list[str]:
    """The columns to draw at this width: the name is never cut, the least important go first."""
    from ahub.i18n import t

    caps = {key: cap for key, cap, _flex in COLUMNS}
    flex = {key for key, _cap, is_flex in COLUMNS if is_flex}

    def natural(chosen: list[str]) -> int:
        total = 0
        for key in chosen:
            want = max([len(t(f"projects.col_{key}"))] + [len(r[key]) for r in rows])
            total += FLEX_MIN if key in flex else min(want, caps[key] or want)
        return total + 2 * (len(chosen) - 1)

    keys = [key for key, _cap, _flex in COLUMNS]
    for drop in DROP_FIRST:
        if natural(keys) <= w:
            break
        keys.remove(drop)
    return keys


def cmd_projects(args) -> int:
    from ahub.i18n import t

    hub = config.load_hub()
    projects, errors = config.load_projects(hub)
    store = Store()
    month0 = cost.month_start()
    known = {p.name: p for p in projects}
    problems = {p.name: config.check_project(p) for p in projects}
    for name in store.task_projects():  # a project that has tasks but is not connected to the hub
        problems.setdefault(name, [t("projects.not_in_hub", project=name)])
    every = stats(store, month0)
    rows = [(name, _row_cells(name, known[name].root if name in known else EMPTY,
                              every.get(name, Stat()), "!" if problems[name] else " "))
            for name in problems]
    lines: list[str] = []
    if not rows:
        if not errors:
            lines.append(t("projects.empty", source=hub.source or config.paths.global_config_path()))
    else:
        keys = _keys([cells for _name, cells in rows], ui.width())
        caps = {key: cap for key, cap, _flex in COLUMNS}
        lines.extend(ui.table([t(f"projects.col_{key}") for key in keys],
                              [[cells[key] for key in keys] for _name, cells in rows],
                              max_width=[caps[key] for key in keys], indent=2).split("\n"))
        for name, _cells in rows:  # the problems of a project go under its row
            lines.extend(f"    {e}" for e in problems[name])
    lines.extend(f"! {e}" for e in errors)
    data_: dict[str, Any] = {"unconnected": [n for n in problems if n not in known],
                             "problems": problems, "errors": errors,
                             "month": to_local(month0).strftime("%Y-%m-%d"),
                             "projects": []}
    for name, cells in rows:  # the JSON is the same set as the table, a project without a config too
        cfg = dict(asdict(known[name])) if name in known else {"name": name, "root": cells["path"]}
        data_["projects"].append(cfg | asdict(every.get(name, Stat())))
    emit(args, data_, "\n".join(lines))
    return 1 if errors or any(problems.values()) else 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("projects", help=t("help.projects"))
    p.set_defaults(func=cmd_projects)
