"""ahub entry point: subcommands are ahub/commands/*.py modules with register(subparsers).

Shared `--json` flag — machine output; default compact text (orchestrator token savings).
Config errors and expected refusals — one stderr line and exit 2, no traceback.

Imports live inside functions: main() refuses on Windows before command imports
(some modules pull fcntl, which is missing there).
"""

from __future__ import annotations

import sys


def _discover(subparsers) -> list[str]:
    import importlib
    import pkgutil

    import ahub.commands as _cmds

    found = []
    for mod in sorted(pkgutil.iter_modules(_cmds.__path__), key=lambda m: m.name):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"ahub.commands.{mod.name}")
        if hasattr(module, "register"):
            module.register(subparsers)
            found.append(mod.name)
    return found


def build_parser():
    import argparse

    from ahub.i18n import t

    ap = argparse.ArgumentParser(prog="ahub", description=t("cli.desc"))
    ap.add_argument("--json", action="store_true", help=t("cli.help_json"))
    ap.add_argument("--lang", choices=("en", "ru"), default=None, help=t("cli.help_lang"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    _discover(sub)
    return ap


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        from ahub.i18n import t as _t

        print(_t("cli.err_win"), file=sys.stderr)
        return 2
    from ahub import log
    from ahub.cliutil import CliError
    from ahub.config import ConfigError
    from ahub.i18n import set_lang, t

    ap = build_parser()
    args = ap.parse_args(argv)
    if getattr(args, "lang", None):
        set_lang(args.lang)
    func = getattr(args, "func", None)
    if func is None:
        ap.print_help()
        return 2
    try:
        return int(func(args) or 0)
    except (ConfigError, CliError) as e:
        print(t("cli.error", msg=e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.get("cli").exception("command %s failed", args.cmd)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
