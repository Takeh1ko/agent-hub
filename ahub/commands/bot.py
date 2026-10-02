"""ahub bot run — TG-бот v2 (связь с Claude, просмотр задач, тревоги, запуск Claude)."""

from __future__ import annotations

import importlib.util

from ahub.cliutil import CliError


def cmd_run(args) -> int:
    if importlib.util.find_spec("aiogram") is None:
        from ahub.i18n import t

        raise CliError(t("err.no_telegram"))
    from ahub.tg.run import main

    return main()


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("bot", help=t("help.bot"))
    sub = p.add_subparsers(dest="bot_cmd", required=True)
    r = sub.add_parser("run", help=t("help.bot_run"))
    r.set_defaults(func=cmd_run)
