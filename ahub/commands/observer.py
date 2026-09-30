"""ahub observer run [--deep] [--no-model] | reports — наблюдатель вручную и его отчёты."""

from __future__ import annotations

from ahub import observer
from ahub.cliutil import emit
from ahub.store import Store
from ahub.time import fmt_local


def cmd_run(args) -> int:
    store = Store()
    if args.check_only:
        sus = observer.quick_check(store)
        emit(args, {"suspicions": [s.text for s in sus]}, "\n".join(s.text for s in sus) or "чисто")
        return 0
    v = observer.cycle(store, deep_due=args.deep or None, use_model=not args.no_model)
    r = observer.reports(store, 1)[0]
    emit(args, {"verdict": v, "report": r}, f"{v}: {r['summary']}")
    return 0


def cmd_reports(args) -> int:
    rows = observer.reports(Store(), args.n)
    text = "\n".join(f"{fmt_local(r['ts'])} {r['kind']} {r['verdict']}: {r['summary'][:120]}"
                     + (f" (${r['cost_go']:.3f})" if r['cost_go'] else "") for r in rows) or "отчётов нет"
    emit(args, {"reports": rows}, text)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("observer", help="наблюдатель за хабом")
    sub = p.add_subparsers(dest="observer_cmd", required=True)
    r = sub.add_parser("run", help="проход наблюдателя сейчас")
    r.add_argument("--deep", action="store_true", help="плановая проверка моделью по чек-листу")
    r.add_argument("--no-model", action="store_true", help="без модели (только код)")
    r.add_argument("--check-only", action="store_true", help="только подозрения кода, без записи тревог")
    r.set_defaults(func=cmd_run)
    rp = sub.add_parser("reports", help="последние отчёты")
    rp.add_argument("-n", type=int, default=10)
    rp.set_defaults(func=cmd_reports)
