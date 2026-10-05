"""ahub models — model catalog: every alias with provider, real name, reasoning, plan and prices.

One table, grouped by provider; `--role R` shows that role's menu with the same columns.
`check` probes the aliases one by one with a live line. Never lifts project denies.
"""

from __future__ import annotations

from ahub import catalog as _catalog
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


def _provider_order(entries) -> list[str]:
    """Hub providers in registration order, then any others alphabetically."""
    return _catalog.provider_order([e.provider for e in entries])


def cmd_list(args) -> int:
    from ahub.i18n import t

    store = Store()
    project = _project_or_none(args)
    refresh = bool(getattr(args, "refresh", False))
    role_refs: list[tuple[str, str, bool]] | None = None
    if getattr(args, "role", None):
        try:
            role_refs = registry.menu_efforts(store, Role(args.role))
        except (registry.RegistryError, ValueError):
            role_refs = []
        wanted = {alias for alias, _eff, _d in role_refs}
        entries = [e for e in registry.models(store) if e.alias in wanted]
    else:
        entries = registry.models(store)
    entries = _catalog.visible_entries(entries)
    rows, extra = _catalog.build_rows(store, entries, refresh=refresh)
    by_alias = {r.entry.alias: r for r in rows}
    # --json: every field raw (registry + catalog + derived); roles kept for old readers
    data: dict = {"models": [], "roles": {}}
    for e in entries:
        r = by_alias.get(e.alias)
        info = r.info if r is not None else None
        avail = _catalog.available_levels(e, info, extra["index"]) if r is not None else []
        data["models"].append({
            "alias": e.alias, "provider": e.provider, "model_id": e.model_id, "variant": e.variant,
            "enabled": e.enabled, "note": e.note,
            "display_name": info.display_name if info else "",
            "vendor": info.vendor if info else "",
            "plan": (r.plan.value if r is not None else registry.plan_kind(e).value),
            "price_in": info.price_in if info else None,
            "price_out": info.price_out if info else None,
            "price_cache": info.price_cache if info else None,
            "context": info.context if info else None,
            "reasoning": list(info.reasoning) if info else [],
            "alias_level": _catalog.alias_level(e),
            "available": avail,
            "price": r.price if r is not None else "",
            "context_text": r.context if r is not None else "",
            "roles": r.roles if r is not None else [],
            "quota_pct": r.quota_pct if r is not None else None,
            "go_pct": r.go_pct if r is not None else None,
        })
    for role in ([Role(args.role)] if getattr(args, "role", None) else list(Role)):
        try:
            refs = registry.menu_efforts(store, role)
        except (OSError, ValueError, RuntimeError):
            continue
        data["roles"][role.value] = [{"alias": a, "effort": e, "default": d,
                                       "ref": registry.model_ref(a, e)} for a, e, d in refs]
    w = ui.width()
    with_vendor, with_context, maxw = _catalog.table_columns(w)
    lines: list[str] = []
    if role_refs is not None:
        # --role: that role's menu with the same columns, effort in the alias cell (spark:high)
        menu_rows: list = []
        index = extra.get("index", {})
        for alias, stored, _is_def in role_refs:
            base_row = by_alias.get(alias)
            if base_row is None:
                continue
            try:
                base_entry = registry.get(store, alias)
            except registry.RegistryError:
                continue
            eff_entry = registry.effective_entry(base_entry, stored, index)
            info = base_row.info
            try:
                reasoning = _catalog.reasoning_text(eff_entry, info, index)
            except (OSError, ValueError, RuntimeError, AttributeError):
                reasoning = base_row.reasoning
            menu_rows.append((eff_entry, stored, info, base_row, reasoning))
        by_prov: dict[str, list] = {}
        for eff_entry, stored, info, base_row, reasoning in menu_rows:
            by_prov.setdefault(eff_entry.provider, []).append((eff_entry, stored, info, base_row, reasoning))
        for prov in _catalog.provider_order(list(by_prov)):
            lines.append(ui.section(_catalog.group_title(prov)))
            head = [t("models.col_alias"), t("models.col_model"), t("models.col_reasoning"),
                    t("models.col_plan"), t("models.col_price")]
            if with_context:
                head.append(t("models.col_context"))
            head.append(t("models.col_roles"))
            body = []
            for eff_entry, stored, info, base_row, reasoning in by_prov[prov]:
                alias_cell = registry.model_ref(eff_entry.alias, stored) + _tags(eff_entry, project)
                model_cell = _catalog.model_text(info, with_vendor=with_vendor,
                                                 fallback=eff_entry.model_id)
                reasoning_cell = reasoning or t("models.no_reasoning")
                plan_cell = _catalog.plan_label(base_row.plan)
                roles_cell = ", ".join(base_row.roles) if base_row.roles else t("models.no_roles")
                row = [alias_cell, model_cell, reasoning_cell, plan_cell, base_row.price]
                if with_context:
                    row.append(base_row.context)
                row.append(roles_cell)
                body.append(row)
            lines.append(ui.table(head, body, max_width=maxw, indent=2))
        if not lines:
            lines.append(ui.table(None, [], indent=2))
    else:
        for prov in _provider_order(entries):
            group = [by_alias[e.alias] for e in entries if e.provider == prov and e.alias in by_alias]
            if not group:
                continue
            lines.append(ui.section(_catalog.group_title(prov)))
            head = [t("models.col_alias"), t("models.col_model"), t("models.col_reasoning"),
                    t("models.col_plan"), t("models.col_price")]
            if with_context:
                head.append(t("models.col_context"))
            head.append(t("models.col_roles"))
            body = []
            for r in group:
                alias_cell = r.entry.alias + _tags(r.entry, project)
                model_cell = _catalog.model_text(r.info, with_vendor=with_vendor, fallback=r.entry.model_id)
                reasoning_cell = r.reasoning or t("models.no_reasoning")
                plan_cell = _catalog.plan_label(r.plan)
                roles_cell = ", ".join(r.roles) if r.roles else t("models.no_roles")
                row = [alias_cell, model_cell, reasoning_cell, plan_cell, r.price]
                if with_context:
                    row.append(r.context)
                row.append(roles_cell)
                body.append(row)
            lines.append(ui.table(head, body, max_width=maxw, indent=2))
        if not lines:
            lines.append(ui.table(None, [], indent=2))
    if not lines:
        lines.append(ui.table(None, [], indent=2))
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
    notices: list[str] = []
    for ref in [args.add, args.remove, args.default_to]:
        if ref:
            note = registry.legacy_notice(ref)
            if note and note not in notices:
                notices.append(note)
    try:
        if args.add:
            alias_part, effort_part = registry.split_ref(args.add)
            _base, stored, _notice = registry.map_legacy(alias_part, effort_part)
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
    try:
        refs = registry.menu_efforts(store, args.role)
    except (OSError, ValueError, RuntimeError):
        refs = []
    default = next((registry.model_ref(a, e) for a, e, d in refs if d), "")
    others = ", ".join(registry.model_ref(a, e) for a, e, d in refs if not d)
    menu_json = [{"alias": a, "effort": e, "ref": registry.model_ref(a, e), "default": d}
                 for a, e, d in refs]
    text = ui.kv([(args.role, [default or t("models.no_default"), others])], indent=2)
    if notices and not getattr(args, "json", False):
        text = "\n".join(notices) + "\n" + text
    data: dict = {"role": args.role, "menu": menu_json}
    if notices:
        data["notice"] = "; ".join(notices)
    emit(args, data, text)
    return 0


def _role_defaults(store) -> list[str]:
    """Default refs (ALIAS[:EFFORT]) of every role menu, in role order, without repeats."""
    out: list[str] = []
    for role in Role:
        try:
            refs = registry.menu_efforts(store, role)
        except Exception:
            continue
        for alias, effort, is_def in refs:
            if not is_def:
                continue
            ref = registry.model_ref(alias, effort)
            if ref not in out:
                out.append(ref)
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
    p.add_argument("--refresh", action="store_true", help=t("help.models_refresh"))
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
