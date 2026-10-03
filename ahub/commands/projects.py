"""ahub projects — the owner's view across projects: where each one stands and what it cost.

One row per project: the path, how much is going on (active / waiting for a slot / waiting for a
decision), open owner questions, the money of this month from the hub sessions (Go and USD apart) and
when it was last touched. A project with config problems is marked `!` and its problems are printed
under the row; a project that has tasks but is not connected to the hub is listed too, as a problem.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ahub import config, cost, ui
from ahub.cliutil import emit
from ahub.model import ACTIVE, WAITING_DECISION, State
from ahub.scope import Scope
from ahub.store import Store
from ahub.time import fmt_local, to_local

EMPTY = "—"


@dataclass
class Stat:
    """What one project is busy with and what it spent this month."""

    active: int = 0
    waiting: int = 0
    decision: int = 0
    questions: int = 0
    go: float = 0.0
    usd: float = 0.0
    last: int = 0


def stat(store: Store, name: str, since: int) -> Stat:
    """Counts and money of one project: tasks by state, open questions, hub sessions since `since`."""
    out = Stat()
    for t in store.list_tasks(project=name):
        if t.state in ACTIVE:
            out.active += 1
        elif t.state is State.QUEUED:
            out.waiting += 1
        elif t.state in WAITING_DECISION:
            out.decision += 1
        out.last = max(out.last, t.updated_at)
    with store.read() as c:
        out.questions = int(c.execute("SELECT COUNT(*) FROM question WHERE status='open' AND project=?",
                                      (name,)).fetchone()[0])
    money = cost.by_project(store, scope=Scope((name,)), since=since).get(name, cost.Money())
    out.go, out.usd = money.go, money.usd
    return out


def _head() -> list[str]:
    from ahub.i18n import t

    return [t("projects.col_project"), t("projects.col_path"), t("projects.col_active"),
            t("projects.col_waiting"), t("projects.col_decide"), t("projects.col_questions"),
            t("projects.col_go"), t("projects.col_usd"), t("projects.col_last")]


def _cells(name: str, root: str, st: Stat, mark: str = " ") -> list[str]:
    """One project row: the name (with the `!` mark of a project with problems) and the rest."""
    return [f"{mark} {name}", root, str(st.active), str(st.waiting), str(st.decision), str(st.questions),
            f"{st.go:.3f}", f"{st.usd:.3f}", fmt_local(st.last) if st.last else EMPTY]


def cmd_projects(args) -> int:
    from ahub.i18n import t

    hub = config.load_hub()
    projects, errors = config.load_projects(hub)
    store = Store()
    month0 = cost.month_start()
    rows = [(p.name, p.root) for p in projects]
    problems = {p.name: config.check_project(p) for p in projects}
    known = {p.name for p in projects}
    for name in store.task_projects():  # a project that has tasks but is not connected to the hub
        if name not in known:
            rows.append((name, EMPTY))
            problems[name] = [t("projects.not_in_hub", project=name)]
    stats = {(name, root): stat(store, name, month0) for name, root in rows}
    lines = []
    if not rows and not errors:
        lines.append(t("projects.empty", source=hub.source or config.paths.global_config_path()))
    else:
        body = [_cells(name, root, stats[(name, root)], "!" if problems[name] else " ")
                for name, root in rows]
        lines.extend(ui.table(_head(), body, max_width=[16, None, None, None, None, None, 9, 9, 13],
                              indent=2).split("\n"))
        for name, _root in rows:  # the problems of a project go under its row
            lines.extend(f"    {e}" for e in problems[name])
    lines.extend(f"! {e}" for e in errors)
    emit(args, {"projects": [dict(asdict(p), **asdict(stats[(p.name, p.root)])) for p in projects],
                "unconnected": [name for name, _root in rows if name not in known],
                "problems": problems, "errors": errors, "month": to_local(month0).strftime("%Y-%m-%d")},
         "\n".join(lines))
    return 1 if errors or any(problems.values()) else 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("projects", help=t("help.projects"))
    p.set_defaults(func=cmd_projects)
