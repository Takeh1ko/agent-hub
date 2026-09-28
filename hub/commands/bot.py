"""hub bot: TG-пульт владельца (aiogram 3, long polling)."""

from __future__ import annotations


def cmd_bot(args) -> int:
    from hub.bot.run import build_dispatcher

    if getattr(args, "dry_run", False):
        build_dispatcher()
        print("dry-run ok")
        return 0
    from hub.bot.run import main as run_main

    return int(run_main())


def register(subparsers) -> None:
    p = subparsers.add_parser("bot", help="TG-пульт владельца")
    p.add_argument("--dry-run", action="store_true",
                   help="собрать Dispatcher без сети и выйти 0")
    p.set_defaults(func=cmd_bot)
