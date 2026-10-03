"""ahub config — the project config as a kv block, and its on-disk problems."""

from __future__ import annotations

from dataclasses import asdict

from ahub import config, ui
from ahub.cliutil import add_project_arg, emit, resolve_project


def _text(cfg: config.ProjectConfig, problems: list[str], w: int | None = None) -> str:
    from ahub.i18n import t

    res = ", ".join(f"{r.name}×{r.capacity}" + (f" ({r.lock})" if r.lock else "")
                    for r in cfg.resources.values()) or "—"
    rows = [(t("config.lbl_branch"), t("config.branch", branch=cfg.work_branch, prefix=cfg.branch_prefix,
                                       worktrees=cfg.worktrees or "—")),
            (t("config.lbl_python"), cfg.python_bin()),
            (t("config.lbl_parallel"), t("config.parallel", max=cfg.max_parallel, res=res,
                                        test=cfg.test_resource or "—")),
            (t("config.lbl_budget"), t("config.budget_line", go=f"{cfg.budget_go:g}", usd=f"{cfg.budget_usd:g}"))]
    if cfg.models_deny:
        rows.append((t("config.lbl_denied"), t("config.denied_line", deny=", ".join(cfg.models_deny))))
    rows.append((t("config.lbl_files"), ", ".join(cfg.allowed_paths) or "—"))
    if problems:
        rows.append((t("config.lbl_problems"), "\n".join(f"! {p}" for p in problems)))
    out = [ui.para(t("config.title", name=cfg.name, root=cfg.root), indent=0, w=w),
           ui.kv(rows, indent=2, w=w),
           ui.styled(ui.kv([(t("views.lbl_next"), t("config.next", root=cfg.root))], indent=2, w=w), "dim")]
    return "\n".join(out)


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
