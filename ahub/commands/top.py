"""ahub top — экран для человека (просмотр; c — управление)."""

from __future__ import annotations


def cmd_top(args) -> int:
    from ahub.tui.app import main

    return main(control=args.control)


def register(subparsers) -> None:
    p = subparsers.add_parser("top", help="экран: задачи, пульс, деньги, события")
    p.add_argument("--control", action="store_true", help="сразу в режиме управления")
    p.set_defaults(func=cmd_top)
