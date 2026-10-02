"""ahub doctor — installation check: what is wrong and what to do."""

from __future__ import annotations

from dataclasses import asdict

from ahub import doctor
from ahub.cliutil import emit

_MARKS = {True: "\u2713", False: "\u2717", None: "\u2013"}


def _text(checks: list[doctor.Check]) -> str:
    from ahub.i18n import t

    lines = []
    for c in checks:
        name = t(f"doctor.name_{c.name}")
        lines.append(t("doctor.line", mark=_MARKS[c.ok], name=name, detail=c.detail))
        if c.ok is False and c.fix:
            lines.append(t("doctor.fix_line", fix=c.fix))
    return "\n".join(lines)


def cmd_doctor(args) -> int:
    checks = doctor.run_all()
    data = [asdict(c) for c in checks]
    emit(args, {"checks": data}, _text(checks))
    return 1 if any(c.ok is False for c in checks) else 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("doctor", help=t("help.doctor"))
    p.set_defaults(func=cmd_doctor)
