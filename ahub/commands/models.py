"""ahub models — model registry and role menus (view / edit), plus `check`: a live probe of the aliases.

The menus are a table (role, default, other models); `check` probes the aliases one by one with a live
line on a terminal. Never lifts project denies.
"""

from __future__ import annotations

from dataclasses import asdict

from ahub import registry, ui
from ahub.cliutil import CliError, add_project_arg, emit
from ahub.model import Role
from ahub.store import Store

_MARKS = {True: "✓", False: "✗"}


def _project_or_none(args):
    from ahub import config
    from ahub.cliutil import resolve_project

    try:
        return resolve_project(args)
    except (CliError, config.ConfigError, FileNotFoundError):
        return None


def _tags(entry, project) -> str:
    """The (off) / (project-denied) marks after an alias."""
    from ahub.i18n import t

    out = ""
    if not entry.enabled:
        out += t("models.tag_off")
    if registry.denied_by(entry, project):
        out += t("models.tag_denied")
    return out


def _config_error() -> str:
    """Why the global config was not read, "" — it was (or there is none).

    The registry keeps every model visible when the config is broken (its provider switches are then
    unknown), so the command says itself under the table instead of failing.
    """
    from ahub import config

    try:
        config.load_hub()
    except config.ConfigError as e:
        return str(e)
    return ""


def cmd_list(args) -> int:
    from ahub.i18n import t

    store = Store()
    project = _project_or_none(args)
    roles = [Role(args.role)] if args.role else list(Role)
    data = {"roles": {}, "models": [asdict(m) for m in registry.models(store)]}
    head = [t("models.col_role"), t("models.col_default"), t("models.col_other")]
    rows = []
    for role in roles:
        items = registry.menu(store, role)
        data["roles"][role.value] = [{"alias": e.alias, "default": d} for e, d in items]
        default = next((e for e, d in items if d), None)
        others = [e.alias + _tags(e, project) for e, d in items if not d]
        rows.append([role.value,
                     (default.alias + _tags(default, project)) if default is not None else t("models.no_default"),
                     ", ".join(others) or t("models.no_other")])
    lines = [ui.table(head, rows, max_width=[10, 18, None], indent=2)]
    if args.all:
        lines.append("")
        lines.append(ui.table([t("models.col_model"), t("models.col_provider"), t("models.col_model_id")],
                              [[m.alias + ("" if m.enabled else t("models.flag_off")), m.provider,
                                m.model_id + (f" [{m.variant}]" if m.variant else "")
                                + (t("models.flag_denied") if registry.denied_by(m, project) else "")]
                               for m in registry.models(store)],
                              max_width=[20, 10, None], indent=2))
    bad = _config_error()
    if bad:
        lines.append(ui.styled(t("cli.error", msg=bad), "dim"))  # the models are shown, the config is not read
        data["config_error"] = bad
    emit(args, data, "\n".join(lines))
    return 0


def cmd_add(args) -> int:
    from ahub.i18n import t

    try:
        registry.add_model(Store(), args.alias, args.provider, args.model_id, args.variant or "", args.note or "")
    except registry.RegistryError as e:
        raise CliError(str(e), hint=t("hint.models_all")) from e
    nxt = t("hint.models_role", role=Role.EXECUTOR.value, alias=args.alias)
    emit(args, {"ok": True}, t("models.added", alias=args.alias) + "\n"
         + ui.styled(ui.kv([(t("views.lbl_next"), nxt)]), "dim"))
    return 0


def cmd_role(args) -> int:
    from ahub.i18n import t

    store = Store()
    try:
        if args.add:
            registry.add_to_role(store, args.role, args.add, default=args.default)
        elif args.remove:
            registry.remove_from_role(store, args.role, args.remove)
        elif args.default_to:
            registry.set_default(store, args.role, args.default_to)
        else:
            raise CliError(t("err.models_need_opt"))
    except registry.RegistryError as e:
        raise CliError(str(e), hint=t("hint.models_role", role=args.role, alias=args.add or args.default_to
                                                    or args.remove or "")) from e
    items = registry.menu(store, args.role)
    default = next((e.alias for e, d in items if d), "")
    emit(args, {"role": args.role, "menu": [{"alias": e.alias, "default": d} for e, d in items]},
         ui.kv([(args.role, [default or t("models.no_default"), ", ".join(e.alias for e, d in items if not d)])],
               indent=2))
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
        raise CliError(t("err.models_no_defaults"), hint=t("hint.models"))
    results = []
    head = ["", t("models.col_model"), t("models.col_probe")]
    rows = []
    with ui.Live(t("models.checking", n=len(aliases)), total=len(aliases)) as p:
        for alias in aliases:
            try:
                entry = registry.get(store, alias)
            except registry.RegistryError as e:
                raise CliError(str(e), hint=t("hint.models_all")) from e
            ok, detail = doctor.probe_model(entry)
            results.append({"alias": alias, "ok": ok, "detail": detail})
            rows.append([_MARKS[ok], alias, _body(detail, alias)])
            p.step()
    ok_n = sum(1 for r in results if r["ok"])
    key = "models.check_ok_many" if len(results) > 1 else "models.check_ok_one"  # "1 of 1 models" is not English
    last = t(key, ok=ok_n, total=len(results)) if ok_n == len(results) \
        else t("models.check_bad", ok=ok_n, total=len(results))
    emit(args, {"checked": results},
         "\n".join([ui.table(head, rows, max_width=[1, 16, None], indent=2), ui.styled(last, "dim")]))
    return 1 if any(not r["ok"] for r in results) else 0


def _body(detail: str, alias: str) -> str:
    """The probe detail without the alias it repeats in its own cell."""
    from ahub import doctor

    return doctor.probe_detail(detail, alias)


def cmd_enable(args, on: bool) -> int:
    from ahub.i18n import t

    try:
        registry.set_enabled(Store(), args.alias, on)
    except registry.RegistryError as e:
        raise CliError(str(e), hint=t("hint.models_enable", alias=args.alias)) from e
    state = t("models.enabled_on") if on else t("models.enabled_off")
    emit(args, {"ok": True}, t("models.enabled_line", alias=args.alias, state=state) + "\n"
         + ui.styled(ui.kv([(t("views.lbl_next"), t("hint.status") if on
                             else t("hint.models_enable", alias=args.alias))]), "dim"))
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
