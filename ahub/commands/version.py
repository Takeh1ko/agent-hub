"""ahub version."""

from __future__ import annotations

import ahub
from ahub.cliutil import emit


def cmd_version(args) -> int:
    emit(args, {"version": ahub.__version__}, f"ahub {ahub.__version__}")
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("version", help=t("help.version"))
    p.set_defaults(func=cmd_version)
