"""hub lint: проверка карточки до запуска. Тонкая обёртка над hub.gate.lint."""

from __future__ import annotations

import tomllib
from pathlib import Path

from hub.config import load_project
from hub.gate.lint import lint_card, strip_arbiter


def cmd_lint(args) -> int:
    card = Path(args.card)
    proj_src = getattr(args, "project", None) or str(card.parent if card.parent != Path("") else ".")
    try:
        project = load_project(proj_src)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"{card}: нет проекта: {e}")
        return 1
    res = lint_card(card, project)
    if res.ok:
        print(f"OK {card.name}")
    else:
        for e in res.errors:
            print(e)
    if getattr(args, "blind", False):
        try:
            text = card.read_text(encoding="utf-8")
        except OSError as e:
            print(f"{card}: не читается: {e}")
            return 1
        print("--- blind ---")
        print(strip_arbiter(text), end="" if text.endswith("\n") else "\n")
    return 0 if res.ok else 1


def register(subparsers) -> None:
    p = subparsers.add_parser("lint", help="проверить карточку до запуска")
    p.add_argument("card", help="путь к карточке .md")
    p.add_argument("--project", default=None, help="корень проекта (поиск .hub.toml)")
    p.add_argument("--blind", action="store_true", help="показать промпт ревьюера без эталона")
    p.set_defaults(func=cmd_lint)
