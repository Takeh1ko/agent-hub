"""ahub models — реестр моделей и меню ролей (посмотреть / изменить). Запреты проекта не снимает."""

from __future__ import annotations

from dataclasses import asdict

from ahub import registry
from ahub.cliutil import CliError, add_project_arg, emit
from ahub.model import Role
from ahub.store import Store


def _project_or_none(args):
    from ahub import config
    from ahub.cliutil import resolve_project

    try:
        return resolve_project(args)
    except (CliError, config.ConfigError, FileNotFoundError):
        return None


def cmd_list(args) -> int:
    store = Store()
    project = _project_or_none(args)
    roles = [Role(args.role)] if args.role else list(Role)
    data = {"roles": {}, "models": [asdict(m) for m in registry.models(store)]}
    lines = []
    for role in roles:
        items = registry.menu(store, role)
        data["roles"][role.value] = [{"alias": e.alias, "default": d} for e, d in items]
        cells = []
        for e, d in items:
            mark = "★" if d else ""
            if not e.enabled:
                mark += "(выкл)"
            if registry.denied_by(e, project):
                mark += "(запрет проекта)"
            cells.append(f"{e.alias}{mark}")
        lines.append(f"{role.value:<9} {', '.join(cells)}")
    if args.all:
        lines.append("")
        for m in registry.models(store):
            flag = "" if m.enabled else " (выкл)"
            deny = " (запрет проекта)" if registry.denied_by(m, project) else ""
            var = f" [{m.variant}]" if m.variant else ""
            lines.append(f"{m.alias:<15} {m.provider}: {m.model_id}{var}{flag}{deny}")
    emit(args, data, "\n".join(lines))
    return 0


def cmd_add(args) -> int:
    try:
        registry.add_model(Store(), args.alias, args.provider, args.model_id, args.variant or "", args.note or "")
    except registry.RegistryError as e:
        raise CliError(str(e)) from e
    emit(args, {"ok": True}, f"добавлена модель {args.alias}")
    return 0


def cmd_role(args) -> int:
    store = Store()
    try:
        if args.add:
            registry.add_to_role(store, args.role, args.add, default=args.default)
        elif args.remove:
            registry.remove_from_role(store, args.role, args.remove)
        elif args.default_to:
            registry.set_default(store, args.role, args.default_to)
        else:
            raise CliError("укажите --add, --remove или --set-default")
    except registry.RegistryError as e:
        raise CliError(str(e)) from e
    items = registry.menu(store, args.role)
    emit(args, {"role": args.role, "menu": [{"alias": e.alias, "default": d} for e, d in items]},
         f"{args.role}: " + ", ".join(e.alias + ("★" if d else "") for e, d in items))
    return 0


def cmd_enable(args, on: bool) -> int:
    try:
        registry.set_enabled(Store(), args.alias, on)
    except registry.RegistryError as e:
        raise CliError(str(e)) from e
    emit(args, {"ok": True}, f"{args.alias}: {'включена' if on else 'выключена'}")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("models", help="модели и роли")
    add_project_arg(p)
    p.add_argument("--role", choices=[r.value for r in Role])
    p.add_argument("--all", action="store_true", help="все модели с поставщиками")
    p.set_defaults(func=cmd_list)
    sub = p.add_subparsers(dest="models_cmd")
    a = sub.add_parser("add", help="новая модель")
    a.add_argument("alias")
    a.add_argument("--provider", required=True)
    a.add_argument("--model-id", required=True)
    a.add_argument("--variant")
    a.add_argument("--note")
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("role", help="меню роли")
    r.add_argument("role", choices=[x.value for x in Role])
    g = r.add_mutually_exclusive_group()
    g.add_argument("--add")
    g.add_argument("--remove")
    g.add_argument("--set-default", dest="default_to")
    r.add_argument("--default", action="store_true", help="с --add: сделать по умолчанию")
    r.set_defaults(func=cmd_role)
    for name, on in (("enable", True), ("disable", False)):
        e = sub.add_parser(name)
        e.add_argument("alias")
        e.set_defaults(func=lambda args, _on=on: cmd_enable(args, _on))
