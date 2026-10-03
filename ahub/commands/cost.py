"""ahub cost — what the hub sessions cost: per project and per model (architecture §9).

Money comes from the hub session table only: a session is what a task did through the hub, so it belongs
to the project of that task. The machine-wide opencode.db (every opencode session on the box, Claude's own
included) is a different thing — it is the Go month limit in `ahub top`'s header, never a project's bill.
Scope: the project of the current directory by default, --project X — X, --all — every project.
"""

from __future__ import annotations

from ahub import cost, scope, ui
from ahub.cliutil import CliError, add_scope_args, emit
from ahub.store import Store
from ahub.time import now_ms, to_local
from ahub.time import parse_since as _parse_since

EMPTY_MODEL = "—"


def _since(args) -> int:
    """--since YYYY-MM-DD (ahub/time.py understands the relative words too); default — this month."""
    now = now_ms()
    if not getattr(args, "since", None):
        return cost.month_start(now)
    try:
        return _parse_since(args.since, now)
    except ValueError as e:
        raise CliError(str(e)) from e


def _project_cell(name: str) -> str:
    """The project of a row; a session without a task (or of a hub-wide one) is the hub's own."""
    from ahub.i18n import t

    return name or t("cost.hub")


def cmd_cost(args) -> int:
    from ahub.i18n import t

    sc = scope.resolve(args)
    since = _since(args)
    store = Store()
    models = cost.by_model(store, scope=sc, since=since)
    projects = cost.by_project(store, scope=sc, since=since)
    total = cost.Money()
    for m in models:
        total += cost.Money(m.go, m.usd)
    sessions = sum(m.sessions for m in models)
    head = [t("cost.col_project"), t("cost.col_model"), t("cost.col_sessions"), t("cost.col_go"),
            t("cost.col_usd")]
    body = [[_project_cell(m.project), m.model or EMPTY_MODEL, str(m.sessions),
             f"{m.go:.3f}", f"{m.usd:.3f}"] for m in models]
    lines = []
    if body:
        lines.append(ui.table(head, body, max_width=[16, None, None, 9, 9], indent=2))
    else:
        lines.append(t("cost.empty"))
    where = sc.name or t("views.all_projects")
    lines.append(ui.styled(t("cost.scope", scope=where, since=to_local(since).strftime("%Y-%m-%d")), "dim"))
    lines.append(ui.styled(t("cost.totals", go=f"{total.go:.3f}", usd=f"{total.usd:.3f}", n=sessions), "dim"))
    emit(args, {"scope": sc.name, "since": to_local(since).strftime("%Y-%m-%d"),
                "projects": [{"project": name, "go": m.go, "usd": m.usd} for name, m in projects.items()],
                "models": [{"project": m.project, "model": m.model, "sessions": m.sessions,
                            "go": m.go, "usd": m.usd} for m in models],
                "totals": {"go": total.go, "usd": total.usd, "sessions": sessions}},
         "\n".join(lines))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("cost", help=t("help.cost"))
    add_scope_args(p)
    p.add_argument("--since", default=None, help=t("help.cost_since"))
    p.set_defaults(func=cmd_cost)
