"""ahub config — разобранный конфиг проекта и проблемы на диске."""

from __future__ import annotations

from dataclasses import asdict

from ahub import config
from ahub.cliutil import add_project_arg, emit, resolve_project


def _text(cfg: config.ProjectConfig, problems: list[str]) -> str:
    res = ", ".join(f"{r.name}×{r.capacity}" + (f"({r.lock})" if r.lock else "")
                    for r in cfg.resources.values()) or "—"
    lines = [
        f"{cfg.name}  {cfg.root}",
        f"  ветка {cfg.work_branch}, задачи {cfg.branch_prefix}<ID> в {cfg.worktrees or '—'}",
        f"  параллельно {cfg.max_parallel}; ресурсы {res}; тесты под {cfg.test_resource or '—'}",
        f"  бюджет go ${cfg.budget_go:g} usd ${cfg.budget_usd:g}; запрещены модели: {', '.join(cfg.models_deny) or '—'}",
        f"  разрешённые пути: {', '.join(cfg.allowed_paths) or '—'}",
    ]
    lines.extend(f"! {p}" for p in problems)
    return "\n".join(lines)


def cmd_config(args) -> int:
    cfg = resolve_project(args)
    problems = config.check_project(cfg)
    emit(args, {"project": asdict(cfg), "problems": problems}, _text(cfg, problems))
    return 1 if problems else 0


def register(subparsers) -> None:
    p = subparsers.add_parser("config", help="конфиг проекта")
    add_project_arg(p)
    p.set_defaults(func=cmd_config)
