"""ahub bot run — TG-бот v2 (связь с Claude, просмотр задач, тревоги, запуск Claude)."""

from __future__ import annotations


def cmd_run(args) -> int:
    from ahub.tg.run import main

    return main()


def register(subparsers) -> None:
    p = subparsers.add_parser("bot", help="TG-бот")
    sub = p.add_subparsers(dest="bot_cmd", required=True)
    r = sub.add_parser("run", help="запустить бота (передний план)")
    r.set_defaults(func=cmd_run)
