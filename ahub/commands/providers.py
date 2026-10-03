"""ahub providers — the providers ahub knows: found, logged in, enabled, models; and the on/off switch.

The switch lives in one place — [providers.<name>] enabled in the hub config — so the setup wizard and this
command change the same thing, and a provider that is off offers no models (a task naming one is refused).
"""

from __future__ import annotations

from ahub import doctor, registry
from ahub.cliutil import CliError, emit
from ahub.store import Store

_MARKS = {True: "\u2713", False: "\u2717", None: "\u2013"}


def _cells(state, aliases: list[str], enabled: bool) -> list[str]:
    """One provider row: the columns in the order of providers.head."""
    from ahub.i18n import t

    return [state.name, _MARKS[state.found], _MARKS[state.logged_in] if state.found else _MARKS[None],
            t("providers.enabled_on") if enabled else t("providers.enabled_off"),
            ", ".join(aliases) or t("providers.no_models")]


def _line(cells: list[str], widths: list[int]) -> str:
    """Columns left-aligned, the last one (the model list) as long as it is."""
    return " ".join(c.ljust(w) for c, w in zip(cells[:-1], widths)).rstrip() + " " + cells[-1]


def cmd_providers(args) -> int:
    from ahub.i18n import t

    store = Store()
    off = registry.disabled_providers()
    rows = []
    for state in doctor.provider_states():
        aliases = [e.alias for e in registry.models(store) if e.provider == state.name]
        rows.append((_cells(state, aliases, state.name not in off), state, aliases))
    head = t("providers.head").split()
    widths = [max([9] + [len(cells[i]) for cells, _s, _a in rows]) for i in range(len(head) - 1)] + [0]
    lines = [_line(head, widths)]
    for cells, state, _aliases in rows:
        lines.append(_line(cells, widths))
        if state.note:
            lines.append(f"  · {state.note}")
        if state.hint:
            lines.append(f"  → {state.hint}")
    data = {"providers": [{"name": st.name, "found": st.found, "logged_in": st.logged_in,
                           "enabled": st.name not in off, "detail": st.detail, "note": st.note,
                           "hint": st.hint, "models": aliases} for _cells, st, aliases in rows]}
    emit(args, data, "\n".join(lines))
    return 0


def cmd_switch(args, on: bool) -> int:
    from ahub import providers as provider_mod
    from ahub.commands.setup import set_provider_enabled
    from ahub.i18n import t

    name = args.name
    known = provider_mod.names()
    if name not in known:
        raise CliError(t("err.providers_unknown", name=name, known=", ".join(known)))
    set_provider_enabled(name, on)
    state = t("providers.enabled_on") if on else t("providers.enabled_off")
    emit(args, {"ok": True, "name": name, "enabled": on}, t("providers.enabled_line", name=name, state=state))
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
