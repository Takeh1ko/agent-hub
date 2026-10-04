"""ahub setup — attach a project to the hub in one command (V30/V31a).

- no .hub.toml → writes the v2 template; v1 present → migrates to v2 (old copy → .hub.toml.v1);
- registers the project in ~/.config/ahub/config.toml;
- --claude: ahub skill for Claude Code (~/.claude/skills/ahub/SKILL.md), a short block in the project CLAUDE.md
  and the permission Bash(ahub:*) in .claude/settings.json — so `ahub …` runs without a question every time.
- Other agents get the same stdio server over MCP; setup only prints the one-line hint, it never runs it.
- TTY without --yes → interactive wizard (language, project, providers, models, service, Claude, Telegram, doctor).
- The providers step lists every provider ahub knows (found / logged in / a note / an install hint) and writes
  [providers.<name>] enabled — the same switch `ahub providers enable|disable` changes.
- The models step probes the models of the providers that are on (live, at once) and picks the role defaults:
  a paid model that answered, else a free one that answered — a model that does not answer is never the default.
  Without a Go login and with no probe (AHUB_PROBE=0), the roles move to a free alias (doctor.pick_free).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from ahub import config, paths, ui
from ahub.cliutil import CliError, emit
from ahub.config import set_global, toml_str as _toml_str

MARK_BEGIN = "<!-- ahub:begin -->"
MARK_END = "<!-- ahub:end -->"
BASH_RULE = "Bash(ahub:*)"  # the Claude Code permission rule for every `ahub …` command
MCP_CMD = "claude mcp add ahub -- ahub mcp"  # the one-line hint for MCP, printed and never run
CLAUDE_BLOCK = f"""{MARK_BEGIN}
## agent-hub
Tasks for worker models go through `ahub` (skill `ahub`). At session start — Monitor on `ahub watch`;
by event lines: `ahub status T<id>` → `ahub accept|rework|reject`. Summary — `ahub status`.
{MARK_END}
"""


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


def allow_bash(root: Path) -> str:
    """Put BASH_RULE into .claude/settings.json permissions.allow; the rest of the file stays.

    The file and the directory are created if missing, the rule is never duplicated, the JSON is written
    with a 2-space indent — through a temp file and os.replace, so an interrupted write cannot truncate
    the user's settings. Returns the line for the user; a settings.json that is not readable JSON is
    reported as is — setup does not refuse over it.
    """
    import os

    from ahub.i18n import t

    f = root / ".claude" / "settings.json"
    data: dict = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return t("setup.perm_bad", path=f, err=f"{e}"[:120])
        if not isinstance(data, dict):
            return t("setup.perm_bad", path=f, err=t("setup.perm_bad_json"))
    perms = data.get("permissions")
    perms = dict(perms) if isinstance(perms, dict) else {}
    allow = list(perms.get("allow")) if isinstance(perms.get("allow"), list) else []
    if BASH_RULE in allow:
        return t("setup.perm_exists", path=f, rule=BASH_RULE)
    allow.append(BASH_RULE)
    perms["allow"] = allow
    data["permissions"] = perms
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".ahub-tmp")  # same dir — os.replace is atomic only within a filesystem
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, f)  # the user's file is replaced whole, never truncated in place
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return t("setup.perm_added", path=f, rule=BASH_RULE)


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


class Steps:
    """The setup report: numbered sections with one line of result each, then a summary block.

    out — where the lines go: print for the wizard (so a question is asked right after its step),
    a list for `--yes`/`--json` (the text is emitted at the end).
    """

    def __init__(self, w: int | None = None, out=None) -> None:
        from ahub.i18n import t

        self.w = w
        self.lines: list[str] = []
        self._t = t
        self._out = out if out is not None else print
        self._n = 0
        self.summary: list[tuple[str, str]] = []

    @classmethod
    def collect(cls, w: int | None = None) -> "Steps":
        """No printing — the text is built for emit() (--json prints the data only)."""
        return cls(w, out=lambda line: None)

    def section(self, key: str) -> None:
        self._n += 1
        self.write(ui.styled(f"{self._n}. {self._t(key)}", "bold"))

    def line(self, text: str, wrap: bool = True) -> None:
        """One line of result under the section (a multi-line text keeps its lines).
        wrap=False — a command a person copies: it stays on one line, whatever its length."""
        for ln in (str(text) or "").splitlines() or [""]:
            self.write(ui.para(ln, indent=4, w=self.w) if wrap else " " * 4 + ln)

    def table(self, head, rows, max_width=None) -> None:
        self.write(ui.table(head, rows, max_width=max_width, indent=4, w=self.w))

    def note(self, label: str, value: str) -> None:
        self.summary.append((label, value))

    def finish(self, nxt: str = "") -> None:
        if self.summary:
            self.write(ui.section(self._t("setup.step_summary")))
            self.write(ui.kv(self.summary, indent=2, w=self.w))
        if nxt:
            self.write(ui.styled(ui.kv([(self._t("views.lbl_next"), nxt)], indent=2, w=self.w), "dim"))

    def write(self, text: str) -> None:
        if text:
            self.lines.append(text)
            self._out(text)

    def text(self) -> str:
        return "\n".join(self.lines)


def _prompt(text: str, default: str = "") -> str:
    if default:
        raw = input(f"{text} ({default}): ")
        return raw.strip() or default
    return input(f"{text}: ").strip()


def _ask_yes_no(question: str, default: bool) -> bool:
    """A yes/no question. --yes and a pipe cannot be asked — the default is the answer there."""
    from ahub.i18n import t

    if not sys.stdin.isatty():
        return default
    yes = {w.strip().lower() for w in t("setup.wizard_yes_words").split(",") if w.strip()}
    no = {w.strip().lower() for w in t("setup.wizard_no_words").split(",") if w.strip()}
    suffix = " (Y/n)" if default else " (y/N)"
    while True:
        try:
            raw = input(f"{question}{suffix}: ").strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        if raw in yes:
            return True
        if raw in no:
            return False


def _can_ask(args) -> bool:
    """May this run ask anything at all: a terminal and no --yes (the flag answers with the defaults)."""
    return not bool(getattr(args, "yes", False)) and sys.stdin.isatty() and sys.stdout.isatty()


def _is_interactive(args) -> bool:
    return _can_ask(args)


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


def _provider_step(states, ask: bool, out: Steps) -> dict[str, bool]:
    """Every provider ahub knows, then which to enable (default: found and logged in).

    Writes [providers.<name>] enabled for each — the single place the switch lives
    (registry.set_provider_enabled, the same one `ahub providers enable` uses). Returns name → on.
    """
    from ahub import doctor, registry
    from ahub.i18n import t

    names = [st.name for st in states]
    for st in states:
        out.line(doctor.provider_line(st))
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
            out.line(t("setup.wizard_providers_skipped", name=name))
    enabled = {st.name: st.name in picked and st.found for st in states}
    for name, on in enabled.items():
        try:
            registry.set_provider_enabled(name, on)
        except CliError as e:
            out.line(str(e))
    on_names = ", ".join(n for n, on in enabled.items() if on) or "—"
    out.line(t("setup.wizard_providers_on", names=on_names))
    off = [n for n, on in enabled.items() if not on]
    if off:
        out.line(t("setup.wizard_providers_off", names=", ".join(off)))
    if not any(enabled.values()):
        out.line(t("setup.wizard_providers_none"))
    out.note(t("setup.step_providers"), on_names)
    return enabled


def _set_role_default(store, role, alias: str) -> None:
    """Make the alias the role default via the registry (into the menu first).

    An alias the registry refuses (unknown, denied, a switched-off provider) leaves the role as it was —
    a defect in the code is not one of those and reaches the wizard console.
    """
    import sqlite3

    from ahub import registry

    try:
        if alias not in [e.alias for e, _ in registry.menu(store, role)]:
            registry.add_to_role(store, role, alias)
        registry.set_default(store, role, alias)
    except (registry.RegistryError, CliError, sqlite3.Error, OSError):
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
    """The role already has a working default: it is set and it answered the probe.

    An alias that was not probed (disabled, or its provider is off or not logged in — the models are not
    offered then) is not working: such a role follows the executor, otherwise its default stays unusable.
    """
    from ahub import registry

    default = registry.role_default(store, role)
    return default is not None and bool(results.get(default.alias, (False, ""))[0])


def _first_free(store) -> str:
    """The free alias to fall back on (the first candidate; the registry knows the order)."""
    from ahub import doctor

    cands = doctor.free_candidates(store)
    return cands[0].alias if cands else doctor.FALLBACK_FREE


def _free_default_step(store, ask: bool, out: Steps, alias: str | None = None) -> dict[str, str]:
    """No live probe (AHUB_PROBE=0), or nothing answered it: the free alias where the default is opencode-go/*.

    alias=... — it is already known (the probe step found no answerer), so no second probe.
    """
    from ahub import doctor
    from ahub.i18n import t

    bad = _roles_needing_free(store)
    if not bad:
        out.line(t("setup.wizard_models_ok"))
        out.note(t("setup.step_models"), t("setup.sum_models", roles=t("setup.wizard_models_ok")))
        return {}
    if alias is None:
        cands = doctor.free_candidates(store)  # free probe: a dead free model is not offered
        with ui.Live(t("setup.probing", n=max(1, len(cands)))) as p:
            alias, warning = doctor.pick_free(store, step=p.step)
        if warning:
            out.line(f"! {warning}")
    if ask and not _ask_yes_no(t("setup.wizard_models_ask", alias=alias, roles=", ".join(bad)), True):
        out.line(t("setup.wizard_models_skip"))
        out.note(t("setup.step_models"), t("setup.sum_models", roles=t("setup.wizard_models_skip")))
        return {}
    alias, changed = ensure_free_default(store, alias=alias, warn=lambda w: out.line(f"! {w}"))
    if changed:
        out.line(t("setup.wizard_models_done", roles=", ".join(changed), alias=alias))
        out.note(t("setup.step_models"), t("setup.sum_models", roles=", ".join(f"{r}={alias}" for r in changed)))
    else:
        out.line(t("setup.wizard_models_skip"))
        out.note(t("setup.step_models"), t("setup.sum_models", roles=t("setup.wizard_models_skip")))
    return {}


def _model_step(store, states, ask: bool, out: Steps) -> dict[str, str]:
    """Probe the models of the providers that are on, then set the default per role (executor, reviewer).

    A provider that is not found or not logged in is never probed — its models are not offered. Other roles
    follow the executor unless their own default answers. Returns role → alias.
    """
    from ahub import doctor, registry
    from ahub.i18n import t
    from ahub.model import Role

    off = registry.disabled_providers()
    live = {st.name for st in states if st.found and st.logged_in}
    entries = [e for e in registry.models(store)
               if e.enabled and e.provider not in off and e.provider in live]
    with ui.Live(t("setup.probing", n=len(entries)), total=len(entries)) as p:  # probes run at once
        results = doctor.probe_models(entries, timeout_s=doctor.PROBE_WIZARD_S)
        p.step()
    if not results:  # probing off or nothing to probe — the free-alias path knows better
        return _free_default_step(store, ask, out)
    kinds = {e.alias: t(f"setup.wizard_model_{registry.cost_kind(e)}") for e in entries}
    rows = []
    for entry in entries:
        ok, detail = results.get(entry.alias, (False, ""))
        body = doctor.probe_detail(detail, entry.alias)
        rows.append(["✓" if ok else "✗", entry.alias, kinds[entry.alias],
                     body + (f" — {entry.note}" if entry.note else "")])
    out.table(None, rows, max_width=[1, 16, 5, None])
    recommended = doctor.recommend_model(entries, results)
    if not recommended:
        out.line("! " + doctor.probe_none_warning([e.alias for e in entries]))
        return _free_default_step(store, ask, out, alias=_first_free(store))
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
    roles = ", ".join(f"{r.value}={a}" for r, a in chosen.items())
    out.line(t("setup.wizard_roles_done", roles=roles))
    out.note(t("setup.step_models"), t("setup.sum_models", roles=roles))
    return {r.value: a for r, a in chosen.items()}


def _claude_install(root: Path, *, ask: bool, out: Steps) -> None:
    """The Claude Code step: skill, CLAUDE.md block, Bash(ahub:*) — then the MCP hint for other agents.

    ask=True asks about the permission — including when --claude asked for the step: the grant is the only
    line here that lets ahub run without a question, so it is asked whenever a question is possible at all.
    """
    from ahub.i18n import t

    out.line(t("setup.skill", path=install_skill()))
    out.line(claude_md(root))
    if ask and not _ask_yes_no(t("setup.wizard_perm_ask", rule=BASH_RULE), True):
        out.line(t("setup.perm_skip", rule=BASH_RULE))
        out.note(t("setup.step_claude"), t("setup.sum_claude", state=t("setup.sum_claude_skip", rule=BASH_RULE)))
    else:
        out.line(allow_bash(root))
        out.note(t("setup.step_claude"), t("setup.sum_claude", state=t("setup.sum_claude_yes", rule=BASH_RULE)))
    out.line(t("setup.mcp_hint", cmd=MCP_CMD, server="ahub mcp"), wrap=False)


def _install_and_enable(out: Steps) -> None:
    """Write the unit/plist, enable it, wait for a tick, hint about lingering. Never raises."""
    import os

    from ahub.commands import service as svc
    from ahub.i18n import t

    kind, names, written, hint = svc.install_service_files()
    out.line(t("setup.wizard_service_done", names=", ".join(written)))
    out.line(t("service.next", cmd=hint), wrap=False)  # the same wording as the install-only path
    for cmd, err in svc.enable_service(kind, names, written):
        out.line(t("setup.wizard_service_enable_fail", cmd=" ".join(cmd), err=err))
    age = svc.wait_for_heartbeat()
    out.line(t("setup.wizard_service_alive", age=age) if age is not None
             else t("setup.wizard_service_dead", hint=hint))
    if kind == "linux":
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
        out.line(t("setup.wizard_service_linger", user=user))
    out.note(t("setup.step_service"),
             t("setup.sum_service", state=t("setup.wizard_service_alive", age=age) if age is not None
                                     else t("setup.sum_none")))


def _install_only(out: Steps) -> None:
    """--yes without --service: the unit is written, enabling it stays a human's decision."""
    from ahub.commands import service as svc
    from ahub.i18n import t

    _kind, _names, written, hint = svc.install_service_files()
    out.line(t("setup.wizard_service_done", names=", ".join(written)))
    out.line(t("service.next", cmd=hint), wrap=False)
    out.note(t("setup.step_service"), t("setup.sum_service", state=t("setup.sum_none")))


def _service_step(args, out: Steps, *, interactive: bool) -> None:
    """The service step: write the unit/plist, enable it, wait for a tick.

    Failures never stop setup — an OS the service does not exist for, a home that cannot be written to,
    an enable that fails: each one is a line of the report and the summary says the step is empty.
    """
    from ahub.i18n import t

    out.section("setup.step_service")
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):  # the guard comes first:
        out.line(t("setup.wizard_service_unsupported"))  # --service/--yes ask for the same files
        out.note(t("setup.step_service"), t("setup.sum_service", state=t("setup.sum_none")))
        return
    try:
        if bool(getattr(args, "service", False)):  # --service: install and enable, no questions
            _install_and_enable(out)
        elif bool(getattr(args, "yes", False)):  # --yes: the files are written, the enable is not run
            _install_only(out)
        elif not interactive:
            out.line(t("setup.wizard_service_skip"))
            out.note(t("setup.step_service"), t("setup.sum_service", state=t("setup.sum_none")))
        elif _ask_yes_no(t("setup.wizard_service_ask_install"), True):
            _install_and_enable(out)
        elif _ask_yes_no(t("setup.wizard_service_ask_start"), False):
            import types

            from ahub.cliutil import captured
            from ahub.commands import service as svc

            with captured() as lines:  # its lines are part of this step — not printed at indent 0
                svc.cmd_start(types.SimpleNamespace(json=False))
            for ln in "".join(ln + "\n" for ln in lines).splitlines():
                out.line(ln)
            out.note(t("setup.step_service"), t("setup.sum_service", state=t("setup.sum_none")))
        else:
            out.line(t("setup.wizard_service_skip"))
            out.note(t("setup.step_service"), t("setup.sum_service", state=t("setup.sum_none")))
    except CliError as e:  # the service refused (its own message is the one to show)
        out.line(str(e))
        out.note(t("setup.step_service"), t("setup.sum_service", state=t("setup.sum_none")))
    except OSError as e:  # an unwritable home, a missing systemctl — nothing of that stops setup
        out.line(t("setup.wizard_service_enable_fail", cmd="install", err=f"{type(e).__name__}: {e}"[:300]))
        out.note(t("setup.step_service"), t("setup.sum_service", state=t("setup.sum_none")))


def run_wizard(args) -> int:
    import os

    from ahub import doctor
    from ahub.i18n import lang, set_lang, t
    from ahub.store import Store

    out = Steps()
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
        raise CliError(str(e), hint=t("hint.setup_path", path=root)) from e
    out.section("setup.step_project")
    out.line(t("setup.done", name=cfg.name, what=what))
    out.line(f"{cfg.name}  {cfg.root}")
    if register_project(root):
        out.line(t("setup.registered", path=paths.global_config_path()))
    out.note(t("setup.step_project"), t("setup.sum_project", name=cfg.name, root=cfg.root))
    out.note(t("setup.step_config"), t("setup.sum_config", path=paths.global_config_path()))
    problems = config.check_project(cfg)
    for problem in problems:
        out.line(f"! {problem}")
    # 3) providers: all of them, then which to enable
    states = doctor.provider_states()
    out.section("setup.step_providers")
    _provider_step(states, ask=True, out=out)
    # 4) models: a live probe of every model of a provider that is on, then the role defaults
    out.section("setup.step_models")
    _model_step(Store(), states, ask=True, out=out)
    # 5) service
    _service_step(args, out, interactive=True)
    # 6) Claude
    out.section("setup.step_claude")
    want_claude = bool(getattr(args, "claude", False))
    cl_check = doctor.check_claude()
    ask = _can_ask(args)  # the permission is asked even with --claude; --yes takes the default
    if want_claude and not cl_check.ok:
        _claude_install(root, ask=ask, out=out)
    elif cl_check.ok:
        from ahub.tg.launcher import claude_bin

        binary = claude_bin() or cl_check.detail
        if want_claude or _ask_yes_no(t("setup.wizard_claude_ask", binary=binary), True):
            _claude_install(root, ask=ask, out=out)
    else:
        out.line(t("setup.wizard_claude_missing"))
        out.note(t("setup.step_claude"), t("setup.sum_claude", state=t("setup.sum_claude_no")))
    # 7) Telegram (default no)
    out.section("setup.step_telegram")
    _telegram_step(out)
    # 8) final check
    out.section("setup.step_check")
    from ahub.commands import doctor as doctorcmd

    for line in doctorcmd.text(doctor.run_all(root)).split("\n"):  # the project from step 2, not the cwd
        out.write(line)
    out.finish(t("setup.next"))
    return 0


def _telegram_step(out: Steps) -> None:
    """Telegram is optional; the token is asked for only if the owner wants the bot."""
    from ahub.i18n import t

    if not _ask_yes_no(t("setup.wizard_tg_ask"), False):
        out.line(t("setup.wizard_tg_skip"))
        out.note(t("setup.step_telegram"), t("setup.wizard_tg_skip"))
        return
    token = _prompt(t("setup.wizard_tg_token")).strip()
    if not token:
        out.line(t("setup.wizard_tg_skip"))
        out.note(t("setup.step_telegram"), t("setup.wizard_tg_skip"))
        return
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
    out.line(t("setup.wizard_tg_done"))
    import importlib.util

    if importlib.util.find_spec("aiogram") is None:
        out.line(t("setup.wizard_tg_no_aiogram"))
    out.note(t("setup.step_telegram"), t("setup.wizard_tg_done"))


def _cmd_noninteractive(args) -> int:
    import sqlite3

    from ahub.config import ConfigError
    from ahub.i18n import set_lang, t
    from ahub.registry import RegistryError

    setup_lang = getattr(args, "setup_lang", None)
    if setup_lang:
        try:
            set_lang(setup_lang)
        except ValueError:
            pass
        set_global("lang", setup_lang)
    root = Path(config.expand(args.path or ".")).resolve()
    if not (root / ".git").exists():
        raise CliError(t("err.setup_not_git", root=root), hint=t("hint.setup_path", path=root))
    deny = [x.strip() for x in (args.deny or "").split(",") if x.strip()]
    what, f = ensure_project_file(root, name=args.name, deny=deny)
    try:
        cfg = config.load_project_file(f)
    except config.ConfigError as e:
        raise CliError(str(e), hint=t("hint.setup_path", path=root)) from e
    out = Steps.collect()  # the text is emitted at the end (with --json only the data goes out)
    out.section("setup.step_project")
    out.line(t("setup.done", name=cfg.name, what=what))
    out.line(f"{cfg.name}  {cfg.root}")
    if register_project(root):
        out.line(t("setup.registered", path=paths.global_config_path()))
    problems = config.check_project(cfg)
    for problem in problems:
        out.line(f"! {problem}")  # under "1. Project", like the wizard — not under the last step
    out.note(t("setup.step_project"), t("setup.sum_project", name=cfg.name, root=cfg.root))
    out.note(t("setup.step_config"), t("setup.sum_config", path=paths.global_config_path()))
    from ahub.store import Store

    # providers and models: the same choices the wizard makes, by the recommendation rule, as a summary
    chosen: dict[str, bool] = {}
    role_models: dict[str, str] = {}
    try:
        from ahub import doctor

        states = doctor.provider_states()
        out.section("setup.step_providers")
        chosen = _provider_step(states, ask=False, out=out)
        out.section("setup.step_models")
        role_models = _model_step(Store(), states, ask=False, out=out)
    except (CliError, ConfigError, RegistryError, sqlite3.Error, OSError) as e:
        out.line(t("setup.wizard_models_skip"))
        out.line(f"! {e}")
    _service_step(args, out, interactive=False)  # the wizard's order: service, then Claude
    out.section("setup.step_claude")
    if args.claude:
        _claude_install(root, ask=_can_ask(args), out=out)
    else:
        out.line(t("setup.wizard_claude_skip"))
        out.note(t("setup.step_claude"), t("setup.sum_none"))
    out.finish(t("setup.next"))
    emit(args, {"project": cfg.name, "file": str(f), "problems": problems, "providers": chosen,
                "models": role_models}, out.text())
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
