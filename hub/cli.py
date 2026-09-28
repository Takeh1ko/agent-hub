"""Точка входа hub: подкоманды — модули hub/commands/*.py с register()."""

from __future__ import annotations

import argparse
import importlib
import pkgutil

import hub.commands as _cmds


def _discover(subparsers) -> dict:
    found = {}
    for mod in pkgutil.iter_modules(_cmds.__path__):
        module = importlib.import_module(f"hub.commands.{mod.name}")
        if hasattr(module, "register"):
            module.register(subparsers)
            found[mod.name] = module
    return found


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hub", description="Оркестратор ИИ-агентов")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _discover(sub)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        ap.print_help()
        return 2
    return int(func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
