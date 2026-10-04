"""ahub version — the version, and what it runs on."""

from __future__ import annotations

import platform
import sys

import ahub
from ahub import paths, ui
from ahub.cliutil import emit


def cmd_version(args) -> int:
    from ahub.i18n import t

    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    where = {"python": py, "os": f"{platform.system().lower()} {platform.release()}",
             "data": str(paths.data_dir())}
    out = [ui.styled(f"ahub {ahub.__version__}", "bold"),
           ui.styled(t("version.where", **where), "dim")]
    emit(args, {"version": ahub.__version__, **where}, "\n".join(out))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("version", help=t("help.version"))
    p.set_defaults(func=cmd_version)
