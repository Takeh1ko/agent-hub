"""ahub providers — the providers ahub knows: found, logged in, enabled, models; and the on/off switch.

The switch lives in one place — [providers.<name>] enabled in the hub config — so the setup wizard and this
command change the same thing, and a provider that is off offers no models (a task naming one is refused).
"""

from __future__ import annotations

from ahub import doctor, registry, ui
from ahub.cliutil import CliError, emit
from ahub.store import Store

_MARKS = {True: "✓", False: "✗", None: "–"}


def _cells(state, enabled: bool) -> list[str]:
    """One provider row: the columns in the order of providers.head. The models are not a column — a
    provider has many of them, and a table cell is clipped: they go under the row, wrapped."""
    from ahub.i18n import t

    return [state.name, _MARKS[state.found], _MARKS[state.logged_in] if state.found else _MARKS[None],
            t("providers.enabled_on") if enabled else t("providers.enabled_off")]


def cmd_providers(args) -> int:
    from ahub.i18n import t

    store = Store()
    off = registry.disabled_providers()
    rows = []
    for state in doctor.provider_states():
        aliases = [e.alias for e in registry.models(store) if e.provider == state.name]
        rows.append((_cells(state, state.name not in off), state, aliases))
    head = [t(f"providers.col_{c}") for c in ("name", "found", "login", "enabled")]
    table = ui.table(head, [cells for cells, _s, _a in rows], max_width=None).split("\n")
    # the table is a table; the models of a provider, its note and its install/login hint are text under
    # its row — all of them, wrapped (a cell would be clipped at the end of a long list)
    out: list[str] = [table[0]]
    for i, (_row, state, aliases) in enumerate(rows, start=1):
        out.append(table[i])
        out.append(ui.kv([(t("providers.lbl_models"),
                           ", ".join(aliases) if aliases else t("providers.no_models"))], indent=2))
        if state.note:
            out.append(ui.para(f"· {state.note}", indent=2))
        if state.hint:
            out.append(ui.para(f"→ {state.hint}", indent=2))
    data = {"providers": [{"name": st.name, "found": st.found, "logged_in": st.logged_in,
                           "enabled": st.name not in off, "detail": st.detail, "note": st.note,
                           "hint": st.hint, "models": aliases} for _cells, st, aliases in rows]}
    emit(args, data, "\n".join(out))
    return 0


def cmd_switch(args, on: bool) -> int:
    from ahub import providers as provider_mod
    from ahub.i18n import t

    name = args.name
    known = provider_mod.names()
    if name not in known:
        raise CliError(t("err.providers_unknown", name=name, known=", ".join(known)))
    registry.set_provider_enabled(name, on)
    state = t("providers.enabled_on") if on else t("providers.enabled_off")
    nxt = t("hint.models") if on else t("hint.status")
    emit(args, {"ok": True, "name": name, "enabled": on},
         t("providers.enabled_line", name=name, state=state) + "\n"
         + ui.styled(ui.kv([(t("views.lbl_next"), nxt)]), "dim"))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("providers", help=t("help.providers"))
    p.set_defaults(func=cmd_providers)
    sub = p.add_subparsers(dest="providers_cmd")
    for name, on in (("enable", True), ("disable", False)):
        e = sub.add_parser(name, help=t(f"help.providers_{name}"))
        e.add_argument("name")
        e.set_defaults(func=lambda args, _on=on: cmd_switch(args, _on))
