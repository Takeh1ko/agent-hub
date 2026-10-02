"""ahub config — parsed project config and on-disk problems."""

from __future__ import annotations

from dataclasses import asdict

from ahub import config
from ahub.cliutil import add_project_arg, emit, resolve_project


def _text(cfg: config.ProjectConfig, problems: list[str]) -> str:
    from ahub.i18n import t

    res = ", ".join(f"{r.name}×{r.capacity}" + (f"({r.lock})" if r.lock else "")
                    for r in cfg.resources.values()) or "—"
    lines = [
        t("config.title", name=cfg.name, root=cfg.root),
        t("config.line_branch", branch=cfg.work_branch, prefix=cfg.branch_prefix,
          worktrees=cfg.worktrees or "—"),
        t("config.line_parallel", max=cfg.max_parallel, res=res, test=cfg.test_resource or "—"),
        t("config.line_budget", go=f"{cfg.budget_go:g}", usd=f"{cfg.budget_usd:g}",
          deny=", ".join(cfg.models_deny) or "—"),
        t("config.line_paths", paths=", ".join(cfg.allowed_paths) or "—"),
    ]
    lines.extend(f"! {p}" for p in problems)
    return "\n".join(lines)


def cmd_config(args) -> int:
    cfg = resolve_project(args)
    problems = config.check_project(cfg)
    emit(args, {"project": asdict(cfg), "problems": problems}, _text(cfg, problems))
    return 1 if problems else 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("config", help=t("help.config"))
    add_project_arg(p)
    p.set_defaults(func=cmd_config)
