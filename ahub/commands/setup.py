"""ahub setup — подключить проект к хабу одной командой (V30/V31a).

- нет .hub.toml → пишет шаблон v2; есть v1 → переводит в v2 (старый — в .hub.toml.v1);
- вносит проект в ~/.config/ahub/config.toml;
- --claude: навык ahub для Claude Code (~/.claude/skills/ahub/SKILL.md) и короткий блок в CLAUDE.md проекта.
"""

from __future__ import annotations

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
    f = root / config.PROJECT_FILE
    if f.exists():
        data = tomllib.loads(f.read_text(encoding="utf-8"))
        if data.get("schema_version", 1) == config.SCHEMA_VERSION:
            return "уже v2", f
        cfg = config.parse_project(data, root, str(f))
        if deny:
            import dataclasses
            cfg = dataclasses.replace(cfg, models_deny=tuple(dict.fromkeys(list(cfg.models_deny) + deny)))
        backup = f.with_suffix(".toml.v1")
        backup.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        f.write_text(render_v2(cfg, raw_root=str(data.get("root", ""))), encoding="utf-8")
        return f"переведён v1 → v2 (старый — {backup.name})", f
    cfg = config.parse_project({"schema_version": 2, "name": name or root.name,
                                "worktrees": str(root.parent / f"{root.name}-wt"), "work_branch": _branch(root),
                                "allowed_paths": ["**"], "models": {"deny": deny or []}}, root)
    f.write_text(render_v2(cfg), encoding="utf-8")
    return "создан шаблон v2 (проверьте allowed_paths, python, тесты)", f


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
        gp.write_text("projects = [" + ", ".join(_toml_str(p) for p in items) + "]\n", encoding="utf-8")
    return changed


def install_skill() -> Path:
    dst = Path.home() / ".claude" / "skills" / "ahub" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text((Path(__file__).resolve().parents[1] / "claude" / "SKILL.md").read_text(encoding="utf-8"),
                   encoding="utf-8")
    return dst


def claude_md(root: Path) -> str:
    f = root / "CLAUDE.md"
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    if MARK_BEGIN in text:
        start, end = text.index(MARK_BEGIN), text.index(MARK_END) + len(MARK_END)
        new = text[:start] + CLAUDE_BLOCK.strip() + text[end:]
        if new == text:
            return "CLAUDE.md: блок уже есть"
        f.write_text(new, encoding="utf-8")
        return "CLAUDE.md: блок обновлён"
    f.write_text((text.rstrip() + "\n\n" if text else "") + CLAUDE_BLOCK, encoding="utf-8")
    return "CLAUDE.md: блок добавлен"


def cmd_setup(args) -> int:
    root = Path(config.expand(args.path or ".")).resolve()
    if not (root / ".git").exists():
        raise CliError(f"{root} — не git-репозиторий")
    deny = [x.strip() for x in (args.deny or "").split(",") if x.strip()]
    what, f = ensure_project_file(root, name=args.name, deny=deny)
    try:
        cfg = config.load_project_file(f)
    except config.ConfigError as e:
        raise CliError(str(e)) from e
    lines = [f"{cfg.name}: {what}"]
    if register_project(root):
        lines.append(f"внесён в {paths.global_config_path()}")
    if args.claude:
        lines.append(f"навык Claude: {install_skill()}")
        lines.append(claude_md(root))
    problems = config.check_project(cfg)
    lines += [f"! {p}" for p in problems]
    emit(args, {"project": cfg.name, "file": str(f), "problems": problems}, "\n".join(lines))
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("setup", help="подключить проект к хабу (и Claude Code)")
    p.add_argument("path", nargs="?", help="корень проекта (по умолчанию — текущий каталог)")
    p.add_argument("--name")
    p.add_argument("--deny", help="модели, запрещённые в проекте (через запятую)")
    p.add_argument("--claude", action="store_true", help="навык для Claude Code + блок в CLAUDE.md проекта")
    p.set_defaults(func=cmd_setup)
