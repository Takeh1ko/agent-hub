"""ahub doctor — installation check: what is wrong and what to do.

The checks live in ahub/doctor.py; this module draws them: one section per area, the ✓/✗/– marks
in a column of their own, and the fix of a failed check indented right under it. `ahub setup` prints the
same list as its last step (_text).
"""

from __future__ import annotations

from dataclasses import asdict

from ahub import doctor, ui
from ahub.cliutil import emit

_MARKS = {True: "✓", False: "✗", None: "–"}
_STYLES = {True: "green", False: "red", None: "dim"}

# area title key → the checks of it, in display order (doctor.run_all returns all of them)
_AREAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("doctor.area_system", ("python", "git")),
    ("doctor.area_hub", ("config", "service", "models", "network")),
    ("doctor.area_providers", ("opencode", "opencode_health", "opencode_auth", "agy", "codex")),
    ("doctor.area_claude", ("claude", "claude_skill")),
    ("doctor.area_optional", ("telegram",)),
)


def _lines(checks: list[doctor.Check], w: int | None) -> list[str]:
    """The screen: a heading per area, then `mark name  detail` and the fix under it."""
    from ahub.i18n import plural, t

    by_name = {c.name: c for c in checks}
    nw = max((len(t(f"doctor.name_{c.name}")) for c in checks), default=0)
    out: list[str] = []
    shown = {name for _title, names in _AREAS for name in names}
    for title, names in _AREAS:  # the areas in order
        block = [by_name[n] for n in names if n in by_name]
        if not block:
            continue
        out.append(ui.section(t(title)))
        for c in block:
            head = f"  {ui.styled(_MARKS[c.ok], _STYLES[c.ok])} {t(f'doctor.name_{c.name}').ljust(nw)}  "
            if c.detail:
                indent = ui.plain_len(head)
                lines = ui.para(c.detail, indent=indent, w=w).split("\n")
                out.append(head + lines[0][indent:])
                out.extend(lines[1:])
            else:
                out.append(head.rstrip())
            if c.ok is False and c.fix:
                out.append(ui.para(t("doctor.fix_line", fix=c.fix), indent=4, w=w))
    rest = [c for c in checks if c.name not in shown]  # a check the areas do not know about
    if rest:
        out.append(ui.section(t("doctor.area_other")))
        for c in rest:
            out.append(f"  {_MARKS[c.ok]} {t(f'doctor.name_{c.name}')}: {c.detail}")
    bad = sum(1 for c in checks if c.ok is False)
    # one / few / many — Russian inflects the noun by the count
    last = plural(bad, "doctor.problem_one", "doctor.problems_few", "doctor.problems") if bad \
        else t("doctor.ok_all")
    out.append(ui.item(last) if ui.colour_on() else ui.styled(last, "dim"))
    return out


def _text(checks: list[doctor.Check], w: int | None = None) -> str:
    return "\n".join(_lines(checks, w))


def cmd_doctor(args) -> int:
    from ahub.i18n import t

    # the provider checks call the binaries — one live line while they run, only on a terminal
    with ui.Live(t("doctor.checking")) as p:
        checks = doctor.run_all(step=p.step)
    data = [asdict(c) for c in checks]
    emit(args, {"checks": data}, _text(checks))
    return 1 if any(c.ok is False for c in checks) else 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("doctor", help=t("help.doctor"))
    p.set_defaults(func=cmd_doctor)
