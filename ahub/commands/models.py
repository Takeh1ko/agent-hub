"""ahub models — model registry and role menus (view / edit), plus `check`: a live probe of the aliases.
Never lifts project denies."""

from __future__ import annotations

from dataclasses import asdict

from ahub import registry
from ahub.cliutil import CliError, add_project_arg, emit
from ahub.model import Role
from ahub.store import Store

_MARKS = {True: "\u2713", False: "\u2717"}


def _project_or_none(args):
    from ahub import config
    from ahub.cliutil import resolve_project

    try:
        return resolve_project(args)
    except (CliError, config.ConfigError, FileNotFoundError):
        return None


def cmd_list(args) -> int:
    from ahub.i18n import t

    store = Store()
    project = _project_or_none(args)
    roles = [Role(args.role)] if args.role else list(Role)
    data = {"roles": {}, "models": [asdict(m) for m in registry.models(store)]}
    lines = []
    for role in roles:
        items = registry.menu(store, role)
        data["roles"][role.value] = [{"alias": e.alias, "default": d} for e, d in items]
        cells = []
        for e, d in items:
            mark = "★" if d else ""
            if not e.enabled:
                mark += t("models.tag_off")
            if registry.denied_by(e, project):
                mark += t("models.tag_denied")
            cells.append(f"{e.alias}{mark}")
        lines.append(f"{role.value:<9} {', '.join(cells)}")
    if args.all:
        lines.append("")
        for m in registry.models(store):
            flag = "" if m.enabled else t("models.flag_off")
            deny = t("models.flag_denied") if registry.denied_by(m, project) else ""
            var = f" [{m.variant}]" if m.variant else ""
            lines.append(f"{m.alias:<15} {m.provider}: {m.model_id}{var}{flag}{deny}")
    emit(args, data, "\n".join(lines))
    return 0


def cmd_add(args) -> int:
    from ahub.i18n import t

    try:
        registry.add_model(Store(), args.alias, args.provider, args.model_id, args.variant or "", args.note or "")
    except registry.RegistryError as e:
        raise CliError(str(e)) from e
    emit(args, {"ok": True}, t("models.added", alias=args.alias))
    return 0


def cmd_role(args) -> int:
    store = Store()
    try:
        if args.add:
            registry.add_to_role(store, args.role, args.add, default=args.default)
        elif args.remove:
            registry.remove_from_role(store, args.role, args.remove)
        elif args.default_to:
            registry.set_default(store, args.role, args.default_to)
        else:
            from ahub.i18n import t

            raise CliError(t("err.models_need_opt"))
    except registry.RegistryError as e:
        raise CliError(str(e)) from e
    items = registry.menu(store, args.role)
    emit(args, {"role": args.role, "menu": [{"alias": e.alias, "default": d} for e, d in items]},
         f"{args.role}: " + ", ".join(e.alias + ("★" if d else "") for e, d in items))
    return 0


def _role_defaults(store) -> list[str]:
    """The default alias of every role menu, in role order, without repeats."""
    out: list[str] = []
    for role in Role:
        try:
            items = registry.menu(store, role)
        except Exception:
            continue
        out += [e.alias for e, d in items if d and e.alias not in out]
    return out


def cmd_check(args) -> int:
    """Live probe of the aliases (default: the role defaults): does the model answer at all."""
    from ahub import doctor
    from ahub.i18n import t

    store = Store()
    aliases = list(args.aliases or []) or _role_defaults(store)
    if not aliases:
        raise CliError(t("err.models_no_defaults"))
    results, lines = [], []
    for alias in aliases:
        try:
            entry = registry.get(store, alias)
        except registry.RegistryError as e:
            raise CliError(str(e)) from e
        ok, detail = doctor.probe_model(entry)
        results.append({"alias": alias, "ok": ok, "detail": detail})
        lines.append(f"{_MARKS[ok]} {detail}")
    emit(args, {"checked": results}, "\n".join(lines))
    return 1 if any(not r["ok"] for r in results) else 0


def cmd_enable(args, on: bool) -> int:
    from ahub.i18n import t

    try:
        registry.set_enabled(Store(), args.alias, on)
    except registry.RegistryError as e:
        raise CliError(str(e)) from e
    state = t("models.enabled_on") if on else t("models.enabled_off")
    emit(args, {"ok": True}, t("models.enabled_line", alias=args.alias, state=state))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("models", help=t("help.models"))
    add_project_arg(p)
    p.add_argument("--role", choices=[r.value for r in Role])
    p.add_argument("--all", action="store_true", help=t("help.models_all"))
    p.set_defaults(func=cmd_list)
    sub = p.add_subparsers(dest="models_cmd")
    a = sub.add_parser("add", help=t("help.models_add"))
    a.add_argument("alias")
    a.add_argument("--provider", required=True)
    a.add_argument("--model-id", required=True)
    a.add_argument("--variant")
    a.add_argument("--note")
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("role", help=t("help.models_role"))
    r.add_argument("role", choices=[x.value for x in Role])
    g = r.add_mutually_exclusive_group()
    g.add_argument("--add")
    g.add_argument("--remove")
    g.add_argument("--set-default", dest="default_to")
    r.add_argument("--default", action="store_true", help=t("help.models_default"))
    r.set_defaults(func=cmd_role)
    c = sub.add_parser("check", help=t("help.models_check"))
    c.add_argument("aliases", nargs="*", help=t("help.models_check"))
    c.set_defaults(func=cmd_check)
    for name, on in (("enable", True), ("disable", False)):
        e = sub.add_parser(name)
        e.add_argument("alias")
        e.set_defaults(func=lambda args, _on=on: cmd_enable(args, _on))
