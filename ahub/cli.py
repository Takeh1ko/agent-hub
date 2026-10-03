"""ahub entry point: subcommands are ahub/commands/*.py modules with register(subparsers).

Shared `--json` flag — machine output; default compact text (orchestrator token savings).
Config errors and expected refusals — one stderr line and exit 2, plus `  hint: …` when the way out is known.

Imports live inside functions: main() refuses on Windows before command imports
(some modules pull fcntl, which is missing there).
"""

from __future__ import annotations

import sys

# subcommand → group key; a command not listed here is shown under `cli.group_other`.
GROUP_KEYS: dict[str, str] = {
    # Tasks
    "task": "tasks", "status": "tasks", "result": "tasks", "log": "tasks", "diff": "tasks",
    "history": "tasks", "draft": "tasks", "nudge": "tasks", "stop": "tasks", "continue": "tasks",
    "accept": "tasks", "reject": "tasks", "rework": "tasks", "extend": "tasks", "budget": "tasks",
    "model": "tasks",
    # Watching
    "watch": "watch", "wait": "watch", "top": "watch", "follow": "watch", "alarms": "watch",
    "ack": "watch", "inbox": "watch", "questions": "watch", "observer": "watch",
    # Setup
    "setup": "setup", "doctor": "setup", "service": "setup", "projects": "setup", "config": "setup",
    "version": "setup",
    # Models and providers
    "models": "models", "providers": "models",
    # Integrations
    "mcp": "integrations", "bot": "integrations", "say": "integrations", "ask": "integrations",
}


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


def _formatter():
    """The help formatter: the subcommands under group headings instead of one long flat list."""
    import argparse

    from ahub.i18n import t

    order = ("tasks", "watch", "setup", "models", "integrations")

    class Grouped(argparse.RawDescriptionHelpFormatter):
        """RawDescriptionHelpFormatter that splits the subcommands by their group."""

        def add_arguments(self, actions):
            subs = [a for a in actions if isinstance(a, argparse._SubParsersAction)]
            for action in actions:
                if action not in subs:
                    super().add_arguments([action])
            if not subs:
                return
            items = subs[0]._get_subactions()
            keys = [k for k in order if any(GROUP_KEYS.get(p.metavar) == k for p in items)]
            keys += sorted({GROUP_KEYS.get(p.metavar, "") for p in items} - set(keys) - {""})
            for key in keys:
                rows = [p for p in items if GROUP_KEYS.get(p.metavar, "") == key]
                if not rows:
                    continue
                self.start_section(t(f"cli.group_{key}"))
                self.add_arguments(rows)
                self.end_section()
            rest = [p for p in items if p.metavar not in GROUP_KEYS]
            if rest:
                self.start_section(t("cli.group_other"))
                self.add_arguments(rest)
                self.end_section()

    return Grouped


def build_parser():
    import argparse

    from ahub.i18n import t

    ap = argparse.ArgumentParser(prog="ahub", description=t("cli.desc"), epilog=t("cli.epilog"),
                                 formatter_class=_formatter())
    ap.add_argument("--json", action="store_true", help=t("cli.help_json"))
    ap.add_argument("--lang", choices=("en", "ru"), default=None, help=t("cli.help_lang"))
    # the subcommands are drawn by the formatter under their own headings (Tasks, Watching, …), so the
    # default "positional arguments" heading would be an empty section
    sub = ap.add_subparsers(dest="cmd", metavar="<command>", title=argparse.SUPPRESS)
    _discover(sub)
    return ap


def command_names(ap=None) -> frozenset[str]:
    """Every subcommand name, the nested ones too (`task new`, `models role`).

    command_hint() reads a command while the words it sees are commands and stops at prose — it needs to
    know the names; the set is built once from the parser itself, so a new command needs nothing here.
    """
    names: set[str] = set()
    subs = getattr(ap or build_parser(), "_subparsers", None)
    for group in getattr(subs, "_group_actions", []):
        for name, parser in getattr(group, "choices", {}).items():
            names.add(name)
            nested = getattr(parser, "_subparsers", None)
            for sub_group in getattr(nested, "_group_actions", []):
                names |= set(getattr(sub_group, "choices", {}))
    return frozenset(names)


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        from ahub.i18n import t as _t

        print(_t("cli.err_win"), file=sys.stderr)
        return 2
    from ahub import log
    from ahub.cliutil import CliError, command_hint
    from ahub.config import ConfigError
    from ahub.i18n import set_lang, t

    ap = build_parser()
    args = ap.parse_args(argv)
    if getattr(args, "lang", None):
        set_lang(args.lang)
    func = getattr(args, "func", None)
    try:
        if func is None:
            if getattr(args, "cmd", None):
                ap.print_help()
                return 2
            from ahub.home import text

            print(text())  # the home screen reads the config, so it answers like any other command
            return 0
        return int(func(args) or 0)
    except (ConfigError, CliError) as e:
        hint = getattr(e, "hint", "") or command_hint(str(e), command_names())
        print(t("cli.error", msg=e), file=sys.stderr)
        if hint:  # the way out — one more line
            print(t("cli.hint", hint=hint), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.get("cli").exception("command %s failed", args.cmd)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
