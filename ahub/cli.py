"""Точка входа ahub: подкоманды — модули ahub/commands/*.py с register(subparsers).

Общий флаг `--json` — машинный вывод; по умолчанию компактный текст (экономия токенов оркестратора).
Ошибки конфига и ожидаемые отказы — одна строка в stderr и код 2, не трассировка.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys

import ahub.commands as _cmds
from ahub import log
from ahub.config import ConfigError
from ahub.cliutil import CliError


def _discover(subparsers) -> list[str]:
    found = []
    for mod in sorted(pkgutil.iter_modules(_cmds.__path__), key=lambda m: m.name):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"ahub.commands.{mod.name}")
        if hasattr(module, "register"):
            module.register(subparsers)
            found.append(mod.name)
    return found


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ahub", description="agent-hub v2: оркестратор моделей-работников")
    ap.add_argument("--json", action="store_true", help="машинный вывод (JSON)")
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
    try:
        return int(func(args) or 0)
    except (ConfigError, CliError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.get("cli").exception("команда %s упала", args.cmd)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
