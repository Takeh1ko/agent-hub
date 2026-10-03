"""ahub setup — attach a project to the hub in one command (V30/V31a).

- no .hub.toml → writes the v2 template; v1 present → migrates to v2 (old copy → .hub.toml.v1);
- registers the project in ~/.config/ahub/config.toml;
- --claude: ahub skill for Claude Code (~/.claude/skills/ahub/SKILL.md) and a short block in the project CLAUDE.md.
- TTY without --yes → interactive wizard (language, project, providers, models, service, Claude, Telegram, doctor).
- Without a Go login the roles move to a free alias, but only after a live probe of the free candidates
  (doctor.pick_free) — a free model that does not answer is never set as the default.
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
    data = {"schema_version": 2, "name": name or root.name, "worktrees": str(root.parent / f"{root.name}-wt"),
            "work_branch": _branch(root), "allowed_paths": ["**"], "models": {"deny": deny or []}}
    venv_py = config.parse_project(dict(data), root).python_bin()
    if venv_py != "python3":  # project venv found — write it down explicitly
        data["python"] = venv_py
    cfg = config.parse_project(data, root)
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


def _provider_lookup(parsed: dict, name: str):
    """`enabled` from [providers.<name>] — the nested table key, not a dotted path."""
    table = parsed.get("providers")
    spec = table.get(name) if isinstance(table, dict) else None
    return spec.get("enabled") if isinstance(spec, dict) else None


def _replace_section_key(text: str, section: str, key: str, value, lookup=None) -> str:
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
        got = lookup(parsed) if lookup else parsed.get(section, {}).get(key)
    except AttributeError:
        got = None
    if got != value:
        raise CliError(t("err.setup_global", path=gp))
    return new


def set_global(key: str, value, *, section: str | None = None, lookup=None) -> Path:
    """Single global-config writer: top-level key or [section] key, rest of file stays."""
    gp = paths.global_config_path()
    gp.parent.mkdir(parents=True, exist_ok=True)
    text = gp.read_text(encoding="utf-8") if gp.exists() else ""
    new = (_replace_section_key(text, section, key, value, lookup) if section
           else _replace_top_key(text, key, value))
    gp.write_text(new, encoding="utf-8")
    return gp


def set_provider_enabled(name: str, enabled: bool) -> Path:
    """The one writer of the provider switch: [providers.<name>] enabled (the rest of the table stays)."""
    return set_global("enabled", enabled, section=f"providers.{name}",
                      lookup=lambda parsed: _provider_lookup(parsed, name))


def _replace_projects_line(text: str, items: list[str]) -> str:
    """Replace the projects key, keep the rest (sections, comments) as is; no key — insert first."""
    return _replace_top_key(text, "projects", list(items))


def register_project(root: Path) -> bool:
    gp = paths.global_config_path()
    hub = config.load_hub(gp) if gp.exists() else config.HubConfig()
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


def ensure_free_default(store=None, *, alias: str | None = None, warn=None) -> tuple[str, list[str]]:
    """Free alias as default where default is opencode-go/*; via registry, no CLI.

    The alias is picked by a live probe of the free candidates (free, ~a second each); with none
    answering it stays as it was and warn(...) gets the text. alias=... — the probe is already done.
    """
    from ahub import doctor, registry
    from ahub.model import Role
    from ahub.store import Store

    st = store if store is not None else Store()
    provs = doctor.auth_providers()
    if doctor.has_go_login(provs):
        return "", []
    picked = alias
    if not picked:
        try:
            picked, warning = doctor.pick_free(st)
        except Exception:
            picked, warning = doctor.FALLBACK_FREE, ""
        if warning:
            (warn or print)(warning)
    try:
        registry.get(st, picked)
    except Exception:
        return picked, []
    changed: list[str] = []
    for role in Role:
        try:
            menu = registry.menu(st, role)
        except Exception:
            continue
        default = next((e for e, d in menu if d), None)
        if default is None or not default.model_id.startswith("opencode-go/"):
            continue
        if picked not in [e.alias for e, _ in menu]:
            try:
                registry.add_to_role(st, role, picked)
            except Exception:
                continue
        try:
            registry.set_default(st, role, picked)
        except Exception:
            continue
        changed.append(role.value)
    return picked, changed


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
        default = registry.role_default(store, role)
        if default is not None and default.model_id.startswith("opencode-go/"):
            bad.append(role.value)
    return bad


def _provider_step(ask: bool, log) -> dict[str, bool]:
    """Every provider ahub knows, then which to enable (default: found and logged in).

    Writes [providers.<name>] enabled for each — the single place the switch lives. Returns name → on.
    """
    from ahub import doctor
    from ahub.i18n import t

    states = doctor.provider_states()
    names = [st.name for st in states]
    log(t("setup.wizard_providers_head"))
    for st in states:
        log(doctor.provider_line(st))
    found = {st.name for st in states if st.found}
    recommended = [st.name for st in states if st.found and st.logged_in]
    picked = recommended
    if ask:
        while True:
            raw = _prompt(t("setup.wizard_providers_ask", default=", ".join(recommended) or "—")).strip().lower()
            picked = recommended if not raw else [w for w in re.split(r"[,\s]+", raw) if w]
            unknown = [w for w in picked if w not in names]
            if not unknown:
                break
            print(t("setup.wizard_providers_bad", name=", ".join(unknown), known=", ".join(names)))
    for name in picked:
        if name not in found:
            log(t("setup.wizard_providers_skipped", name=name))
    enabled = {st.name: st.name in picked and st.found for st in states}
    for name, on in enabled.items():
        try:
            set_provider_enabled(name, on)
        except CliError as e:
            log(str(e))
    log(t("setup.wizard_providers_on", names=", ".join(n for n, on in enabled.items() if on) or "—"))
    off = [n for n, on in enabled.items() if not on]
    if off:
        log(t("setup.wizard_providers_off", names=", ".join(off)))
    if not any(enabled.values()):
        log(t("setup.wizard_providers_none"))
    return enabled


def _set_role_default(store, role, alias: str) -> None:
    """Make the alias the role default via the registry (into the menu first); never raises."""
    from ahub import registry

    try:
        if alias not in [e.alias for e, _ in registry.menu(store, role)]:
            registry.add_to_role(store, role, alias)
        registry.set_default(store, role, alias)
    except Exception:
        pass


def _ask_role_model(role, good, recommended: str) -> str:
    """The role default: Enter takes the recommended one, an alias or a number another."""
    from ahub.i18n import t

    options = ", ".join(e.alias for e in good)
    while True:
        raw = _prompt(t("setup.wizard_role_ask", role=role.value, options=options), recommended)
        pick = raw.strip().lower()
        if not pick:
            return recommended
        if pick.isdigit() and 1 <= int(pick) <= len(good):
            return good[int(pick) - 1].alias
        if any(e.alias == pick for e in good):
            return pick
        print(t("setup.wizard_role_bad", alias=raw, options=options))


def _default_answers(store, role, results: dict) -> bool:
    """The role already has a working default: it is set and it answered the probe (or was not probed)."""
    from ahub import registry

    default = registry.role_default(store, role)
    return default is not None and bool(results.get(default.alias, (True, ""))[0])


def _first_free(store) -> str:
    """The free alias to fall back on (the first candidate; the registry knows the order)."""
    from ahub import doctor

    cands = doctor.free_candidates(store)
    return cands[0].alias if cands else doctor.FALLBACK_FREE


def _free_default_step(store, ask: bool, log, alias: str | None = None) -> dict[str, str]:
    """No live probe (AHUB_PROBE=0), or nothing answered it: the free alias where the default is opencode-go/*.

    alias=... — it is already known (the probe step found no answerer), so no second probe.
    """
    from ahub import doctor
    from ahub.i18n import t

    bad = _roles_needing_free(store)
    if not bad:
        log(t("setup.wizard_models_ok"))
        return {}
    if alias is None:
        alias, warning = doctor.pick_free(store)  # free probe: a dead free model is not offered
        if warning:
            log(f"! {warning}")
    if ask and not _ask_yes_no(t("setup.wizard_models_ask", alias=alias, roles=", ".join(bad)), True):
        log(t("setup.wizard_models_skip"))
        return {}
    alias, changed = ensure_free_default(store, alias=alias, warn=lambda w: log(f"! {w}"))
    if changed:
        log(t("setup.wizard_models_done", roles=", ".join(changed), alias=alias))
    else:
        log(t("setup.wizard_models_skip"))
    return {}


def _model_step(store, ask: bool, log) -> dict[str, str]:
    """Probe the models of the providers that are on, then set the default per role (executor, reviewer).

    Other roles follow the executor unless their own default answers. Returns role → alias.
    """
    from ahub import doctor, registry
    from ahub.i18n import t
    from ahub.model import Role

    off = registry.disabled_providers()
    entries = [e for e in registry.models(store) if e.enabled and e.provider not in off]
    results = doctor.probe_models(entries, timeout_s=doctor.PROBE_WIZARD_S)
    if not results:  # probing off or nothing to probe — the free-alias path knows better
        return _free_default_step(store, ask, log)
    log(t("setup.wizard_models_head"))
    for entry in entries:
        ok, detail = results.get(entry.alias, (False, ""))
        note = t(f"setup.wizard_model_{registry.cost_kind(entry)}")
        log(f"{'✓' if ok else '✗'} {note:<5} {detail}" + (f" — {entry.note}" if entry.note else ""))
    recommended = doctor.recommend_model(entries, results)
    if not recommended:
        log("! " + doctor.probe_none_warning([e.alias for e in entries]))
        return _free_default_step(store, ask, log, alias=_first_free(store))
    good = [e for e in entries if results[e.alias][0]]
    chosen: dict[Role, str] = {}
    for role in (Role.EXECUTOR, Role.REVIEWER):
        alias = _ask_role_model(role, good, recommended) if ask else recommended
        _set_role_default(store, role, alias)
        chosen[role] = alias
    for role in (Role.SCOUT, Role.ROUTINE, Role.OBSERVER, Role.DRAFTER):
        if _default_answers(store, role, results):
            continue
        _set_role_default(store, role, chosen[Role.EXECUTOR])
        chosen[role] = chosen[Role.EXECUTOR]
    log(t("setup.wizard_roles_done", roles=", ".join(f"{r.value}={a}" for r, a in chosen.items())))
    return {r.value: a for r, a in chosen.items()}


def _service_enable_lines(os_kind: str, names: list[str], written: list[str], hint: str) -> list[str]:
    """Enable an installed service and check it by heartbeat; never raises."""
    import os

    from ahub.commands import service as svc
    from ahub.i18n import t

    lines = [t("setup.wizard_service_enable_fail", cmd=" ".join(cmd), err=err)
             for cmd, err in svc.enable_service(os_kind, names, written)]
    age = svc.wait_for_heartbeat()
    lines.append(t("setup.wizard_service_alive", age=age) if age is not None
                 else t("setup.wizard_service_dead", hint=hint))
    if os_kind == "linux":
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
        lines.append(t("setup.wizard_service_linger", user=user))
    return lines


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
    # 3) providers: all of them, then which to enable
    _provider_step(ask=True, log=print)
    # 4) models: a live probe of every model of a provider that is on, then the role defaults
    store = Store()
    _model_step(store, ask=True, log=print)
    # 5) service
    want_service = bool(getattr(args, "service", False))
    if want_service:
        # --service: install and enable without questions; failures never stop setup.
        try:
            from ahub.commands import service as svc

            _kind, _names, written, hint = svc.install_service_files()
            print(t("setup.wizard_service_done", names=", ".join(written)))
            print(hint)
            for line in _service_enable_lines(_kind, _names, written, hint):
                print(line)
        except CliError as e:
            print(e)
        except Exception as e:
            print(t("setup.wizard_service_enable_fail", cmd="install", err=str(e)[:300]))
    elif sys.platform.startswith("linux") or sys.platform == "darwin":
        if _ask_yes_no(t("setup.wizard_service_ask_install"), True):
            try:
                from ahub.commands import service as svc

                _kind, _names, written, hint = svc.install_service_files()
                print(t("setup.wizard_service_done", names=", ".join(written)))
                print(hint)
                for line in _service_enable_lines(_kind, _names, written, hint):
                    print(line)
            except CliError as e:
                print(e)
            except Exception as e:
                print(t("setup.wizard_service_enable_fail", cmd="install", err=str(e)[:300]))
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
    from ahub.store import Store

    # providers and models: the same choices the wizard makes, by the recommendation rule, as a summary
    chosen: dict[str, bool] = {}
    role_models: dict[str, str] = {}
    try:
        chosen = _provider_step(ask=False, log=lines.append)
        role_models = _model_step(Store(), ask=False, log=lines.append)
    except Exception as e:
        lines.append(t("setup.wizard_models_skip"))
        print(f"! {e}")
    if args.claude:
        lines.append(t("setup.skill", path=install_skill()))
        lines.append(claude_md(root))
    want_service = bool(getattr(args, "service", False))
    want_install = bool(getattr(args, "yes", False)) or want_service
    if want_install and (sys.platform.startswith("linux") or sys.platform == "darwin" or want_service):
        try:
            from ahub.commands import service as svc

            _kind, _names, written, hint = svc.install_service_files()
            lines.append(t("setup.wizard_service_done", names=", ".join(written)))
            lines.append(hint)
            if want_service:
                lines.extend(_service_enable_lines(_kind, _names, written, hint))
        except CliError as e:
            lines.append(str(e))
        except Exception as e:
            lines.append(t("setup.wizard_service_enable_fail", cmd="install", err=str(e)[:300]))
    problems = config.check_project(cfg)
    lines += [f"! {p}" for p in problems]
    emit(args, {"project": cfg.name, "file": str(f), "problems": problems, "providers": chosen,
                "models": role_models}, "\n".join(lines))
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
    p.add_argument("--service", action="store_true", help=t("help.setup_service"))
    p.add_argument("--lang", dest="setup_lang", choices=("en", "ru"), default=None, help=t("help.setup_lang"))
    p.set_defaults(func=cmd_setup)
