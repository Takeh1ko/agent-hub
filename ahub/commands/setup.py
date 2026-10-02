"""ahub setup — подключить проект к хабу одной командой (V30/V31a).

- нет .hub.toml → пишет шаблон v2; есть v1 → переводит в v2 (старый — в .hub.toml.v1);
- вносит проект в ~/.config/ahub/config.toml;
- --claude: навык ahub для Claude Code (~/.claude/skills/ahub/SKILL.md) и короткий блок в CLAUDE.md проекта.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from ahub import config, paths
from ahub.cliutil import CliError, emit

MARK_BEGIN = "<!-- ahub:begin -->"
MARK_END = "<!-- ahub:end -->"
CLAUDE_BLOCK = f"""{MARK_BEGIN}
## agent-hub
Задачи для моделей-работников — через `ahub` (навык `ahub`). В начале сессии — Monitor на `ahub watch`;
по строкам событий: `ahub status T<id>` → `ahub accept|rework|reject`. Сводка — `ahub status`.
{MARK_END}
"""


def _toml_str(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_str(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_v2(cfg: config.ProjectConfig, *, raw_root: str = "") -> str:
    lines = ["schema_version = 2", f"name = {_toml_str(cfg.name)}"]
    if raw_root:
        lines.append(f"root = {_toml_str(raw_root)}")
    for key in ("worktrees", "work_branch", "push", "python", "rules"):
        val = getattr(cfg, key)
        if val:
            lines.append(f"{key} = {_toml_str(val)}")
    lines.append(f"allowed_paths = {_toml_str(list(cfg.allowed_paths))}")
    lines.append(f"max_parallel = {cfg.max_parallel}")
    if cfg.test_resource:
        lines.append(f"test_resource = {_toml_str(cfg.test_resource)}")
    if cfg.resources:
        lines.append("\n[resources]")
        for r in cfg.resources.values():
            spec = {"capacity": r.capacity} if not r.lock else ({"lock": r.lock} if r.capacity == 1
                                                                 else {"lock": r.lock, "capacity": r.capacity})
            lines.append(f"{r.name} = {{ " + ", ".join(f"{k} = {_toml_str(v)}" for k, v in spec.items()) + " }")
    if cfg.hooks.task_setup or cfg.hooks.task_cleanup:
        lines.append("\n[hooks]")
        lines.append(f"task_setup = {_toml_str(cfg.hooks.task_setup)}")
        lines.append(f"task_cleanup = {_toml_str(cfg.hooks.task_cleanup)}")
    if cfg.models_deny:
        lines.append(f"\n[models]\ndeny = {_toml_str(list(cfg.models_deny))}  # снимает только человек")
    lines.append(f"\n[budget]\ngo = {cfg.budget_go}\nusd = {cfg.budget_usd}")
    extra = [x for x in cfg.secret_excludes if x not in config.DEFAULT_SECRET_EXCLUDES]
    if extra:
        lines.append(f"\n[secrets]\nexclude = {_toml_str(extra)}")
    t = cfg.timeouts
    lines.append(f"\n[timeouts]\nidle_s = {t.idle_s}\nretry_max = {t.retry_max}\nretry_pause_s = {t.retry_pause_s}")
    return "\n".join(lines) + "\n"


def _branch(root: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "main"


def ensure_project_file(root: Path, *, name: str | None = None, deny: list[str] | None = None) -> tuple[str, Path]:
    """(что сделали, путь .hub.toml)."""
    from ahub.i18n import t

    f = root / config.PROJECT_FILE
    if f.exists():
        data = tomllib.loads(f.read_text(encoding="utf-8"))
        if data.get("schema_version", 1) == config.SCHEMA_VERSION:
            return t("setup.already_v2"), f
        cfg = config.parse_project(data, root, str(f))
        if deny:
            import dataclasses
            cfg = dataclasses.replace(cfg, models_deny=tuple(dict.fromkeys(list(cfg.models_deny) + deny)))
        from ahub import archive

        backup = archive.root(cfg) / "hub.toml.v1"  # архив хаба — вне git проекта
        backup.parent.mkdir(parents=True, exist_ok=True)
        archive._exclude(cfg)
        backup.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        f.write_text(render_v2(cfg, raw_root=str(data.get("root", ""))), encoding="utf-8")
        return t("setup.converted", dir=archive.DIR, name=backup.name), f
    cfg = config.parse_project({"schema_version": 2, "name": name or root.name,
                                "worktrees": str(root.parent / f"{root.name}-wt"), "work_branch": _branch(root),
                                "allowed_paths": ["**"], "models": {"deny": deny or []}}, root)
    f.write_text(render_v2(cfg), encoding="utf-8")
    return t("setup.created"), f


def _render_projects(items: list[str]) -> str:
    """Одна строка projects для глобального конфига."""
    return "projects = [" + ", ".join(_toml_str(p) for p in items) + "]"


_PROJECTS_KEY = re.compile(r"^projects\s*=\s*\[[^\]]*\][^\n]*\n?", re.MULTILINE)


def _replace_projects_line(text: str, items: list[str]) -> str:
    """Заменить ключ projects, остальное (секции, комментарии) оставить как было; нет ключа — вставить первым."""
    line = _render_projects(items) + "\n"
    m = _PROJECTS_KEY.search(text)
    new = text[:m.start()] + line + text[m.end():] if m else line + text
    if tuple(tomllib.loads(new).get("projects", ())) != tuple(items):
        from ahub.i18n import t

        raise CliError(t("err.setup_projects", path=paths.global_config_path()))
    return new


def register_project(root: Path) -> bool:
    gp = paths.global_config_path()
    hub = config.load_hub(gp) if gp.exists() else config.HubConfig()
    if not gp.exists():
        legacy = config.load_hub()  # список проектов v1, если был
        hub = config.HubConfig(projects=legacy.projects)
    items = list(hub.projects)
    if any(Path(p).resolve() == root.resolve() for p in items):
        changed = not gp.exists()
    else:
        items.append(str(root))
        changed = True
    if changed:
        gp.parent.mkdir(parents=True, exist_ok=True)
        if gp.exists():
            gp.write_text(_replace_projects_line(gp.read_text(encoding="utf-8"), items), encoding="utf-8")
        else:
            gp.write_text(_render_projects(items) + "\n", encoding="utf-8")
    return changed


def install_skill() -> Path:
    dst = Path.home() / ".claude" / "skills" / "ahub" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text((Path(__file__).resolve().parents[1] / "claude" / "SKILL.md").read_text(encoding="utf-8"),
                   encoding="utf-8")
    return dst


def claude_md(root: Path) -> str:
    from ahub.i18n import t

    f = root / "CLAUDE.md"
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    if MARK_BEGIN in text:
        start, end = text.index(MARK_BEGIN), text.index(MARK_END) + len(MARK_END)
        new = text[:start] + CLAUDE_BLOCK.strip() + text[end:]
        if new == text:
            return t("setup.claude_exists")
        f.write_text(new, encoding="utf-8")
        return t("setup.claude_updated")
    f.write_text((text.rstrip() + "\n\n" if text else "") + CLAUDE_BLOCK, encoding="utf-8")
    return t("setup.claude_added")


def cmd_setup(args) -> int:
    from ahub.i18n import t

    root = Path(config.expand(args.path or ".")).resolve()
    if not (root / ".git").exists():
        raise CliError(t("err.setup_not_git", root=root))
    deny = [x.strip() for x in (args.deny or "").split(",") if x.strip()]
    what, f = ensure_project_file(root, name=args.name, deny=deny)
    try:
        cfg = config.load_project_file(f)
    except config.ConfigError as e:
        raise CliError(str(e)) from e
    lines = [t("setup.done", name=cfg.name, what=what)]
    if register_project(root):
        lines.append(t("setup.registered", path=paths.global_config_path()))
    if args.claude:
        lines.append(t("setup.skill", path=install_skill()))
        lines.append(claude_md(root))
    problems = config.check_project(cfg)
    lines += [f"! {p}" for p in problems]
    emit(args, {"project": cfg.name, "file": str(f), "problems": problems}, "\n".join(lines))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("setup", help=t("help.setup"))
    p.add_argument("path", nargs="?", help=t("help.setup_path"))
    p.add_argument("--name")
    p.add_argument("--deny", help=t("help.setup_deny"))
    p.add_argument("--claude", action="store_true", help=t("help.setup_claude"))
    p.set_defaults(func=cmd_setup)
