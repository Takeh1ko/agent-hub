"""ahub setup — attach a project to the hub in one command (V30/V31a).

- no .hub.toml → writes the v2 template; v1 present → migrates to v2 (old copy → .hub.toml.v1);
- registers the project in ~/.config/ahub/config.toml;
- --claude: ahub skill for Claude Code (~/.claude/skills/ahub/SKILL.md) and a short block in the project CLAUDE.md.
- TTY without --yes → interactive wizard (language, project, providers, models, service, Claude, Telegram, doctor).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

from ahub import config, paths
from ahub.cliutil import CliError, emit

MARK_BEGIN = "<!-- ahub:begin -->"
MARK_END = "<!-- ahub:end -->"
CLAUDE_BLOCK = f"""{MARK_BEGIN}
## agent-hub
Tasks for worker models go through `ahub` (skill `ahub`). At session start — Monitor on `ahub watch`;
by event lines: `ahub status T<id>` → `ahub accept|rework|reject`. Summary — `ahub status`.
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
        lines.append(f"\n[models]\ndeny = {_toml_str(list(cfg.models_deny))}  # only a human can lift it")
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
    """(what was done, .hub.toml path)."""
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

        backup = archive.root(cfg) / "hub.toml.v1"  # hub archive — outside the project git
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
    """Single projects line for the global config."""
    return "projects = [" + ", ".join(_toml_str(p) for p in items) + "]"


_PROJECTS_KEY = re.compile(r"^projects\s*=\s*\[[^\]]*\][^\n]*\n?", re.MULTILINE)


def _replace_top_key(text: str, key: str, value) -> str:
    """Top-level key replace, rest (sections, comments) stays; no key — insert first."""
    from ahub.i18n import t

    if key == "projects":
        line = _render_projects(list(value)) + "\n"
        pat = _PROJECTS_KEY
    else:
        line = f"{key} = {_toml_str(value)}\n"
        pat = re.compile(rf"^{re.escape(key)}\s*=.*\n?", re.MULTILINE)
    m = pat.search(text)
    new = text[:m.start()] + line + text[m.end():] if m else line + text
    try:
        parsed = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        raise CliError(t("err.setup_global", path=paths.global_config_path())) from None
    if key == "projects":
        if tuple(parsed.get("projects", ())) != tuple(value):
            raise CliError(t("err.setup_projects", path=paths.global_config_path()))
    elif parsed.get(key) != value:
        raise CliError(t("err.setup_global", path=paths.global_config_path()))
    return new


def _replace_section_key(text: str, section: str, key: str, value) -> str:
    """Key inside [section]; section missing — append; rest (comments, other sections) stays."""
    from ahub.i18n import t

    gp = paths.global_config_path()
    rendered = f"{key} = {_toml_str(value)}\n"
    head = re.compile(rf"^\[{re.escape(section)}\][^\n]*\n?", re.MULTILINE)
    m = head.search(text)
    if m is None:
        if text and not text.endswith("\n"):
            text += "\n"
        new = text + f"[{section}]\n{rendered}"
    else:
        nxt = re.compile(r"^\[.*\][^\n]*\n?", re.MULTILINE)
        nm = nxt.search(text, m.end())
        end = nm.start() if nm else len(text)
        body = text[m.end():end]
        kpat = re.compile(rf"^{re.escape(key)}\s*=.*\n?", re.MULTILINE)
        km = kpat.search(body)
        if km:
            body = body[:km.start()] + rendered + body[km.end():]
        else:
            if body and not body.endswith("\n"):
                body += "\n"
            body = body + rendered
        new = text[:m.end()] + body + text[end:]
    try:
        parsed = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        raise CliError(t("err.setup_global", path=gp)) from None
    try:
        got = parsed.get(section, {}).get(key)
    except AttributeError:
        got = None
    if got != value:
        raise CliError(t("err.setup_global", path=gp))
    return new


def set_global(key: str, value, *, section: str | None = None) -> Path:
    """Single global-config writer: top-level key or [section] key, rest of file stays."""
    gp = paths.global_config_path()
    gp.parent.mkdir(parents=True, exist_ok=True)
    text = gp.read_text(encoding="utf-8") if gp.exists() else ""
    new = _replace_section_key(text, section, key, value) if section else _replace_top_key(text, key, value)
    gp.write_text(new, encoding="utf-8")
    return gp


def _replace_projects_line(text: str, items: list[str]) -> str:
    """Replace the projects key, keep the rest (sections, comments) as is; no key — insert first."""
    return _replace_top_key(text, "projects", list(items))


def register_project(root: Path) -> bool:
    gp = paths.global_config_path()
    hub = config.load_hub(gp) if gp.exists() else config.HubConfig()
    if not gp.exists():
        legacy = config.load_hub()  # v1 project list, if any
        hub = config.HubConfig(projects=legacy.projects)
    items = list(hub.projects)
    if any(Path(p).resolve() == root.resolve() for p in items):
        changed = not gp.exists()
    else:
        items.append(str(root))
        changed = True
    if changed:
        set_global("projects", items)
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


def ensure_free_default(store=None) -> tuple[str, list[str]]:
    """Free alias as default where default is opencode-go/*; via registry, no CLI."""
    from ahub import doctor, registry
    from ahub.model import Role
    from ahub.store import Store

    st = store if store is not None else Store()
    provs = doctor.auth_providers()
    if doctor.has_go_login(provs):
        return "", []
    try:
        alias = doctor._free_alias(st)
    except Exception:
        return "spark-free", []
    try:
        registry.get(st, alias)
    except Exception:
        return alias, []
    changed: list[str] = []
    for role in Role:
        try:
            menu = registry.menu(st, role)
        except Exception:
            continue
        default = next((e for e, d in menu if d), None)
        if default is None or not default.model_id.startswith("opencode-go/"):
            continue
        if alias not in [e.alias for e, _ in menu]:
            try:
                registry.add_to_role(st, role, alias)
            except Exception:
                continue
        try:
            registry.set_default(st, role, alias)
        except Exception:
            continue
        changed.append(role.value)
    return alias, changed


def _prompt(text: str, default: str = "") -> str:
    if default:
        raw = input(f"{text} ({default}): ")
        return raw.strip() or default
    return input(f"{text}: ").strip()


def _ask_yes_no(question: str, default: bool) -> bool:
    from ahub.i18n import t

    yes = {w.strip().lower() for w in t("setup.wizard_yes_words").split(",") if w.strip()}
    no = {w.strip().lower() for w in t("setup.wizard_no_words").split(",") if w.strip()}
    suffix = " (Y/n)" if default else " (y/N)"
    while True:
        raw = input(f"{question}{suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in yes:
            return True
        if raw in no:
            return False


def _is_interactive(args) -> bool:
    return not bool(getattr(args, "yes", False)) and sys.stdin.isatty() and sys.stdout.isatty()


def _roles_needing_free(store) -> list[str]:
    from ahub import doctor, registry
    from ahub.model import Role

    if doctor.has_go_login(doctor.auth_providers()):
        return []
    bad: list[str] = []
    for role in Role:
        try:
            menu = registry.menu(store, role)
        except Exception:
            continue
        default = next((e for e, d in menu if d), None)
        if default is not None and default.model_id.startswith("opencode-go/"):
            bad.append(role.value)
    return bad


def run_wizard(args) -> int:
    import os
    import types

    from ahub import doctor
    from ahub.i18n import lang, set_lang, t
    from ahub.store import Store

    # 1) language
    cur = lang()
    ans = _prompt(t("setup.wizard_lang"), cur).strip().lower()
    while ans not in ("en", "ru"):
        print(t("setup.wizard_lang_bad"))
        ans = _prompt(t("setup.wizard_lang"), cur).strip().lower()
    set_lang(ans)
    set_global("lang", ans)
    # 2) project
    dflt = getattr(args, "path", None) or os.getcwd()
    while True:
        raw = _prompt(t("setup.wizard_path"), dflt)
        root = Path(config.expand(raw)).resolve()
        if not (root / ".git").exists():
            print(t("setup.wizard_project_bad", root=root))
            continue
        break
    deny = [x.strip() for x in (getattr(args, "deny", None) or "").split(",") if x.strip()]
    what, f = ensure_project_file(root, name=getattr(args, "name", None), deny=deny)
    try:
        cfg = config.load_project_file(f)
    except config.ConfigError as e:
        raise CliError(str(e)) from e
    print(t("setup.done", name=cfg.name, what=what))
    if register_project(root):
        print(t("setup.registered", path=paths.global_config_path()))
    for p in config.check_project(cfg):
        print(f"! {p}")
    # 3) providers
    op_check = doctor.check_opencode()
    provs = doctor.auth_providers()
    if not op_check.ok:
        print(t("setup.wizard_opencode_missing", detail=op_check.detail))
    elif provs:
        print(t("setup.wizard_providers_ok", providers=", ".join(provs)))
    else:
        auth_c = doctor.check_opencode_auth()
        print(f"✗ {t(f'doctor.name_{auth_c.name}')} — {auth_c.detail}" + (f"\n  → {auth_c.fix}" if auth_c.fix else ""))
    # 4) models
    store = Store()
    bad = _roles_needing_free(store)
    if not bad:
        print(t("setup.wizard_models_ok"))
    elif _ask_yes_no(t("setup.wizard_models_ask", alias=doctor._free_alias(store), roles=", ".join(bad)), True):
        alias, changed = ensure_free_default(store)
        if changed:
            print(t("setup.wizard_models_done", roles=", ".join(changed), alias=alias))
        else:
            print(t("setup.wizard_models_skip"))
    else:
        print(t("setup.wizard_models_skip"))
    # 5) service
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        if _ask_yes_no(t("setup.wizard_service_ask_install"), True):
            try:
                from ahub.commands import service as svc

                _kind, _names, written, hint = svc.install_service_files()
                print(t("setup.wizard_service_done", names=", ".join(written)))
                print(hint)
            except CliError as e:
                print(e)
        elif _ask_yes_no(t("setup.wizard_service_ask_start"), False):
            try:
                from ahub.commands import service as svc

                fake = types.SimpleNamespace(json=False)
                svc.cmd_start(fake)
            except CliError as e:
                print(e)
        else:
            print(t("setup.wizard_service_skip"))
    else:
        print(t("setup.wizard_service_unsupported"))
    # 6) Claude
    want_claude = bool(getattr(args, "claude", False))
    cl_check = doctor.check_claude()
    if want_claude and not cl_check.ok:
        print(t("setup.skill", path=install_skill()))
        print(claude_md(root))
    elif cl_check.ok:
        from ahub.tg.launcher import claude_bin

        binary = claude_bin() or cl_check.detail
        if want_claude or _ask_yes_no(t("setup.wizard_claude_ask", binary=binary), True):
            print(t("setup.skill", path=install_skill()))
            print(claude_md(root))
    else:
        print(t("setup.wizard_claude_missing"))
    # 7) Telegram (default no)
    if _ask_yes_no(t("setup.wizard_tg_ask"), False):
        token = _prompt(t("setup.wizard_tg_token")).strip()
        if token:
            set_global("token", token, section="telegram")
            while True:
                raw_chat = _prompt(t("setup.wizard_tg_chat")).strip()
                if not raw_chat:
                    break
                try:
                    chat_id = int(raw_chat)
                except ValueError:
                    print(t("setup.wizard_tg_bad_chat"))
                    continue
                set_global("chat_id", chat_id, section="telegram")
                break
            print(t("setup.wizard_tg_done"))
            import importlib.util

            if importlib.util.find_spec("aiogram") is None:
                print(t("setup.wizard_tg_no_aiogram"))
        else:
            print(t("setup.wizard_tg_skip"))
    else:
        print(t("setup.wizard_tg_skip"))
    # 8) final check
    print(t("setup.wizard_doctor_head"))
    from ahub.commands.doctor import _text as _doctor_text

    print(_doctor_text(doctor.run_all()))
    return 0


def _cmd_noninteractive(args) -> int:
    from ahub.i18n import set_lang, t

    setup_lang = getattr(args, "setup_lang", None)
    if setup_lang:
        try:
            set_lang(setup_lang)
        except ValueError:
            pass
        set_global("lang", setup_lang)
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
    try:
        ensure_free_default()
    except Exception:
        pass
    problems = config.check_project(cfg)
    lines += [f"! {p}" for p in problems]
    emit(args, {"project": cfg.name, "file": str(f), "problems": problems}, "\n".join(lines))
    return 0


def cmd_setup(args) -> int:
    setup_lang = getattr(args, "setup_lang", None)
    if setup_lang:
        from ahub.i18n import set_lang

        try:
            set_lang(setup_lang)
        except ValueError:
            pass
    if _is_interactive(args):
        return run_wizard(args)
    return _cmd_noninteractive(args)


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("setup", help=t("help.setup"))
    p.add_argument("path", nargs="?", help=t("help.setup_path"))
    p.add_argument("--name")
    p.add_argument("--deny", help=t("help.setup_deny"))
    p.add_argument("--claude", action="store_true", help=t("help.setup_claude"))
    p.add_argument("--yes", action="store_true", help=t("help.setup_yes"))
    p.add_argument("--lang", dest="setup_lang", choices=("en", "ru"), default=None, help=t("help.setup_lang"))
    p.set_defaults(func=cmd_setup)
