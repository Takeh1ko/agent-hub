"""ahub observer run [--deep] [--no-model] | reports — manual observer run and its reports."""

from __future__ import annotations

from ahub import observer, ui, views
from ahub.cliutil import emit
from ahub.store import Store
from ahub.time import fmt_local


def cmd_run(args) -> int:
    from ahub.i18n import t

    store = Store()
    if args.check_only:
        sus = observer.quick_check(store)
        emit(args, {"suspicions": [s.text for s in sus]},
             ui.bullets([s.text for s in sus], indent=2) if sus else t("observer.clean"))
        return 0
    with ui.Live(t("observer.checking")) as p:
        v = observer.cycle(store, deep_due=args.deep or None, use_model=not args.no_model)
        p.step()
    r = observer.reports(store, 1)[0]
    out = [ui.styled(v, "bold" if v in ("alarm", "critical") else ""),
           ui.para(ui.fit(str(r["summary"]), views.REPORT_BYTES), indent=2),
           ui.styled(ui.kv([(t("views.lbl_next"), t("observer.next"))], indent=2), "dim")]
    emit(args, {"verdict": v, "report": r}, "\n".join(out))
    return 0


def cmd_reports(args) -> int:
    from ahub.i18n import t

    rows = observer.reports(Store(), args.n)
    if not rows:
        emit(args, {"reports": rows}, t("observer.no_reports"))
        return 0
    head = [t("observer.col_when"), t("observer.col_check"), t("observer.col_verdict"),
            t("observer.col_summary")]
    body = [[fmt_local(r["ts"]), r["kind"], r["verdict"],
             ui.clip(r["summary"], 120) + (f" (${r['cost_go']:.3f})" if r["cost_go"] else "")]
            for r in rows]
    emit(args, {"reports": rows}, ui.table(head, body, max_width=[16, 8, 12, None], indent=2))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("observer", help=t("help.observer"))
    sub = p.add_subparsers(dest="observer_cmd", required=True)
    r = sub.add_parser("run", help=t("help.observer_run"))
    r.add_argument("--deep", action="store_true", help=t("help.observer_deep"))
    r.add_argument("--no-model", action="store_true", help=t("help.observer_no_model"))
    r.add_argument("--check-only", action="store_true", help=t("help.observer_check_only"))
    r.set_defaults(func=cmd_run)
    rp = sub.add_parser("reports", help=t("help.observer_reports"))
    rp.add_argument("-n", type=int, default=10)
    rp.set_defaults(func=cmd_reports)
