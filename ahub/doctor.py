"""Installation check: what is wrong and what to do.

Checks live here so the `ahub setup` wizard can reuse them.
Every user-visible string goes through t() (keys doctor.*); this module
never logs secret values (auth.json values are never read into output, a proxy URL is shown as host:port).

Also the live model probe (probe_model): one tiny request through the provider module, so setup never
makes a model that does not answer the default (the free Spark was silent for hours on 2026-10-02).
And what every provider looks like right now (provider_states): the same checks, one state per provider,
which `ahub setup` and `ahub providers` show and probe_models probes all at once.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ahub.i18n import t as _t

TIMEOUT_S = 10  # short timeout for external calls (spec: <= 15 s)
PROBE_TIMEOUT_S = 60  # one tiny live turn of a model: enough for a slow one, short enough not to hang setup
PROBE_WIZARD_S = 45  # the wizard probes several models at once — a shorter turn, so the step is quick
PROBE_PROMPT = "Reply with exactly: OK"  # to the model (not the user) — not translated
FALLBACK_FREE = "spark-free"  # when the registry knows no free alias at all
PROBE_WORKERS = 4  # how many models the wizard probes at the same time
BASH_RULE = "Bash(ahub:*)"  # the Claude Code permission rule `ahub setup --claude` writes

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class Check:
    """One check result: ok True/False, None means no data / not applicable."""

    name: str
    ok: bool | None
    detail: str = ""
    fix: str = ""


def _fail(name: str, err: Exception) -> Check:
    return Check(name, False, _t("doctor.check_error", err=f"{err.__class__.__name__}: {err}"[:200]), "")


def check_python(version: tuple[int, ...] | None = None) -> Check:
    v = version if version is not None else sys.version_info
    major, minor = v[0], v[1]
    micro = v[2] if len(v) > 2 else 0
    text = f"{major}.{minor}.{micro}"
    if (major, minor) >= (3, 11):
        return Check("python", True, _t("doctor.python_ok", version=text), "")
    return Check("python", False, _t("doctor.python_bad", version=text), _t("doctor.python_fix"))


def check_git() -> Check:
    exe = shutil.which("git")
    if not exe:
        return Check("git", False, _t("doctor.git_missing"), _t("doctor.git_fix"))
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return Check("git", False, _t("doctor.git_missing"), _t("doctor.git_fix"))
    out = (r.stdout or r.stderr or "").strip().splitlines()
    ver = out[0].strip().removeprefix("git version ")[:60] if out else "?"
    if r.returncode != 0:
        return Check("git", False, _t("doctor.git_missing"), _t("doctor.git_fix"))
    return Check("git", True, _t("doctor.git_ok", version=ver), "")


def check_config() -> Check:
    from ahub import config, paths

    try:
        hub = config.load_hub()
    except config.ConfigError as e:
        src = e.source or str(paths.global_config_path())
        return Check("config", False, _t("doctor.config_bad", err=str(e)[:500]),
                     _t("doctor.config_fix", source=src))
    if hub.source:
        return Check("config", True, _t("doctor.config_ok", source=hub.source), "")
    return Check("config", True, _t("doctor.config_default"), "")


def _service_unit() -> str:
    home = Path.home()
    sys_unit = home / ".config" / "systemd" / "user" / "ahub.service"
    if sys_unit.is_file():
        return str(sys_unit)
    plist = home / "Library" / "LaunchAgents" / "dev.ahub.service.plist"
    if plist.is_file():
        return str(plist)
    return ""


def check_service() -> Check:
    from ahub.service import HEARTBEAT_KEY
    from ahub.store import Store
    from ahub.time import now_ms

    store = Store()
    hb = store.meta_get(HEARTBEAT_KEY)
    age: int | None = None
    if hb:
        try:
            age = (now_ms() - int(hb)) // 1000
        except (ValueError, TypeError):
            age = None
    if age is not None and age < 30:
        return Check("service", True, _t("doctor.service_alive", age=age), "")
    unit = _service_unit()
    if unit:
        return Check("service", False, _t("doctor.service_dead_unit", unit=unit),
                     _t("doctor.service_fix_start"))
    return Check("service", False, _t("doctor.service_dead_no_unit"), _t("doctor.service_fix_install"))


def check_opencode() -> Check:
    from ahub.providers.opencode import opencode_bin

    binary = opencode_bin()
    if os.access(binary, os.X_OK):
        return Check("opencode", True,
                     _t("doctor.opencode_found", binary=binary) + provider_proxy_detail("opencode"), "")
    return Check("opencode", False, _t("doctor.opencode_missing", binary=binary),
                 _t("doctor.opencode_fix"))


def check_opencode_health() -> Check:
    from ahub import providers
    from ahub.providers.opencode import opencode_bin

    h = providers.get("opencode").health()
    if h.ok:
        ver = str(h.details.get("version", "")).strip()[:40]
        suffix = _t("doctor.health_version", version=ver) if ver else ""
        return Check("opencode_health", True, _t("doctor.health_ok", version=suffix), "")
    try:
        binary_missing = not os.access(opencode_bin(), os.X_OK)
    except Exception:
        binary_missing = False
    fix = _t("doctor.opencode_fix") if binary_missing else ""
    return Check("opencode_health", False,
                 _t("doctor.health_bad", problems="; ".join(h.problems)[:500]), fix)


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def auth_file_path() -> Path:
    """opencode auth.json: XDG data dir or ~/.local/share (keys only are ever read)."""
    raw = os.environ.get("XDG_DATA_HOME")
    if raw:
        return Path(raw) / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _providers_from_auth_list(binary: str) -> set[str]:
    """Provider ids from `opencode auth list` output (colors stripped)."""
    out: set[str] = set()
    try:
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        r = subprocess.run([binary, "auth", "list"], capture_output=True, text=True,
                           timeout=TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError):
        return out
    if r.returncode != 0:
        return out
    text = _strip_ansi(r.stdout or "")
    for line in text.splitlines():
        low = line.lower()
        if "opencode go" in low or "opencode-go" in low:
            out.add("opencode-go")
        elif "zen" in low:
            out.add("opencode")
    return out


def _providers_from_auth_file() -> set[str]:
    """Provider names from auth.json: keys only, values never touch output or logs."""
    try:
        raw = auth_file_path().read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    return {str(k) for k in data.keys() if str(k).strip()}


def auth_providers(binary: str | None = None) -> list[str]:
    """Authorized providers (union of `opencode auth list` and auth.json keys)."""
    found: set[str] = set()
    if binary is None:
        try:
            from ahub.providers.opencode import opencode_bin

            binary = opencode_bin()
        except Exception:
            binary = ""
    if binary and os.access(binary, os.X_OK):
        found |= _providers_from_auth_list(binary)
    found |= _providers_from_auth_file()
    return sorted(found)


def has_go_login(providers: list[str] | None = None) -> bool:
    provs = providers if providers is not None else auth_providers()
    return any(p.strip().lower() == "opencode-go" for p in provs)


def _auth_check(providers: list[str]) -> Check:
    if not providers:
        return Check("opencode_auth", False, _t("doctor.auth_none"), _t("doctor.auth_fix"))
    names = ", ".join(providers)
    if has_go_login(providers):
        return Check("opencode_auth", True, _t("doctor.auth_go", providers=names), "")
    return Check("opencode_auth", True, _t("doctor.auth_free", providers=names), "")


def check_opencode_auth() -> Check:
    return _auth_check(auth_providers())


def check_agy() -> Check:
    """agy (Gemini): found — ok True/False by the provider's health(); not found — not installed (None)."""
    from ahub import providers
    from ahub.providers.agy import agy_bin, agy_state_file

    binary = agy_bin()
    if not os.access(binary, os.X_OK):
        return Check("agy", None, _t("doctor.agy_missing"), "")
    h = providers.get("agy").health()
    if h.ok:
        ver = str(h.details.get("version", "")).strip()[:40]
        suffix = _t("doctor.health_version", version=ver) if ver else ""
        return Check("agy", True, _t("doctor.agy_found", binary=binary) + suffix
                     + provider_proxy_detail("agy"), "")
    problems = "; ".join(h.problems)[:500]
    fix = _t("doctor.agy_fix_login") if not agy_state_file().is_file() else ""
    return Check("agy", False, _t("doctor.health_bad", problems=problems), fix)


APPARMOR_USERNS_FLAG = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def apparmor_blocks_userns() -> bool:
    """AppArmor forbids unprivileged user namespaces on this host (the Ubuntu 24.04 default) — that is
    why the codex/bubblewrap sandbox cannot start. No such file — AppArmor is not in the kernel."""
    try:
        return APPARMOR_USERNS_FLAG.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def codex_sandbox_fix() -> str:
    """Fix for a codex sandbox that does not start: allow user namespaces (the admin's decision) or
    drop the OS sandbox in the hub config. The hub never changes system settings itself."""
    if apparmor_blocks_userns():
        return f"{_t('doctor.codex_fix_userns')} {_t('doctor.codex_fix_no_sandbox')}"
    return _t("doctor.codex_fix_no_sandbox")


def check_codex() -> Check:
    """codex (Codex CLI): found — ok True/False per the provider's health(); not found — None."""
    from ahub import providers
    from ahub.providers.codex import codex_bin

    binary = codex_bin()
    if not os.access(binary, os.X_OK):
        return Check("codex", None, _t("doctor.codex_missing") + ": " + _t("doctor.codex_fix"), "")
    h = providers.get("codex").health()
    if h.ok:
        ver = str(h.details.get("version", "")).strip()[:40]
        suffix = _t("doctor.health_version", version=ver) if ver else ""
        return Check("codex", True, _t("doctor.codex_found", binary=binary) + suffix
                     + provider_proxy_detail("codex"), "")
    problems = "; ".join(h.problems)[:500]
    try:
        logged_in = providers.get("codex").login()[0]
    except Exception:
        logged_in = False
    fix = "" if logged_in else _t("doctor.codex_fix_login")
    if h.details.get("sandbox_ok") is False:  # every command would fail silently — both fixes in one hint
        fix = f"{fix} {codex_sandbox_fix()}".strip()
    return Check("codex", False, _t("doctor.health_bad", problems=problems), fix)


def probing_enabled() -> bool:
    """AHUB_PROBE=0 — setup picks a free alias without a live request (an offline machine, the test suite).

    An explicit `ahub models check` always probes: that is what the user asked for.
    """
    return os.environ.get("AHUB_PROBE", "1").strip().lower() not in ("0", "no", "off", "false")


@dataclass(frozen=True)
class ProviderState:
    """What one provider looks like right now: found, logged in, a note, an install/login hint."""

    name: str
    found: bool
    logged_in: bool
    detail: str = ""  # the check detail: the binary, the version, the problems
    note: str = ""  # free / paid / plan — one line for the wizard and the table
    hint: str = ""  # install or login hint; empty — nothing to do


_PROV_HINTS: dict[str, str] = {
    "opencode": "doctor.prov_hint_opencode",
    "agy": "doctor.prov_hint_agy",
    "codex": "doctor.prov_hint_codex",
}

_PROV_NOTES: dict[str, str] = {"agy": "doctor.prov_note_agy", "codex": "doctor.prov_note_codex"}


def install_hint(name: str) -> str:
    """One-line install hint for a provider that is not found."""
    key = _PROV_HINTS.get(name)
    return _t(key) if key else ""


def provider_state(name: str, auth: list[str] | None = None) -> ProviderState:
    """One provider state, from the same checks `ahub doctor` runs."""
    provs = auth if auth is not None else auth_providers()
    if name == "opencode":
        check = check_opencode()
        found, logged, detail = bool(check.ok), bool(provs), check.detail
        fix = "" if logged else (check.fix or _t("doctor.auth_fix"))
    elif name == "agy":
        check = check_agy()
        found, logged = check.ok is not None, bool(check.ok)
        detail = check.detail
        fix = "" if logged else check.fix
    elif name == "codex":
        check = check_codex()
        found, logged = check.ok is not None, bool(check.ok)
        detail = check.detail
        fix = "" if logged else check.fix
    else:
        return ProviderState(name, False, False, _t("doctor.prov_unknown", name=name))
    note = _t(_PROV_NOTES[name]) if name in _PROV_NOTES else (
        _t("doctor.prov_note_opencode_go") if has_go_login(provs)
        else _t("doctor.prov_note_opencode_free"))
    hint = _t("doctor.prov_login_hint", name=name, cmd=fix) if (found and not logged and fix) \
        else ("" if found else install_hint(name))
    return ProviderState(name, found, logged, detail=detail, note=note, hint=hint)


def provider_states(auth: list[str] | None = None) -> list[ProviderState]:
    """State of every provider ahub knows, in registration order (opencode, agy, codex)."""
    from ahub import providers as provider_mod

    provs = auth if auth is not None else auth_providers()
    out = []
    for name in provider_mod.names():
        try:
            out.append(provider_state(name, provs))
        except Exception as e:  # one provider must not take the whole list down
            out.append(ProviderState(name, False, False,
                                    detail=_t("doctor.check_error", err=f"{e.__class__.__name__}: {e}"[:200])))
    return out


def provider_line(state: ProviderState) -> str:
    """One provider for the wizard: the mark, the state, the note, and the hint on its own line."""
    if not state.found:
        mark, word = "✗", _t("doctor.prov_missing")
    elif state.logged_in:
        mark, word = "✓", _t("doctor.prov_logged_in")
    else:
        mark, word = "!", _t("doctor.prov_no_login")
    line = f"{mark} {state.name} — {word}"
    if state.note:
        line += f" · {state.note}"
    if state.hint:
        line += f"\n  → {state.hint}"
    return line


def probe_model(entry, timeout_s: int = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """One tiny live request through the provider: does the model answer at all.

    (ok, detail). The provider module runs the turn (the shared runner + its own classification),
    so a dead model shows up with its real reason: silence, quota, no access, not started.
    """
    from ahub import providers
    from ahub.providers import runner
    from ahub.providers.base import RunSpec

    try:
        provider = providers.get(entry.provider)
    except Exception as e:
        return False, _t("doctor.probe_no_provider", alias=entry.alias, provider=entry.provider,
                         err=f"{e.__class__.__name__}: {e}"[:200])
    with tempfile.TemporaryDirectory(prefix="ahub-probe-") as tmp:
        spec = RunSpec(prompt=PROBE_PROMPT, cwd=tmp, model_id=entry.model_id, variant=entry.variant,
                       log_path=str(Path(tmp) / "probe.log"), timeout_s=timeout_s,
                       idle_s=max(5, timeout_s // 2))  # a hung model is silence before the timeout
        try:
            r = runner.run(provider, spec)
        except Exception as e:
            return False, _t("doctor.probe_error", alias=entry.alias,
                             err=f"{e.__class__.__name__}: {e}"[:200])
    reply = (r.final_text or "").strip()
    if r.ok and reply:
        return True, _t("doctor.probe_ok", alias=entry.alias, reply=reply[:60])
    return False, _t("doctor.probe_fail", alias=entry.alias, reason=(r.error or r.outcome.value)[:200])


def free_candidates(store) -> list:
    """Free model entries to try as a default, in order (empty when the registry is unreadable)."""
    from ahub import registry

    try:
        return registry.free_candidates(store)
    except Exception:
        return []


def _probe_one(entry, timeout_s: int) -> tuple[bool, str]:
    try:
        return probe_model(entry, timeout_s)
    except Exception as e:
        return False, _t("doctor.probe_error", alias=entry.alias, err=f"{e.__class__.__name__}: {e}"[:200])


def probe_models(entries, timeout_s: int = PROBE_WIZARD_S,
                 workers: int = PROBE_WORKERS) -> dict[str, tuple[bool, str]]:
    """Live probe of several models at once: alias → (answers, detail).

    Empty with probing off (AHUB_PROBE=0) or nothing to probe — the caller then keeps its own fallback.
    """
    items = list(entries)
    if not items or not probing_enabled():
        return {}
    out: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        futures = [pool.submit(_probe_one, e, timeout_s) for e in items]
        for entry, fut in zip(items, futures, strict=True):
            out[entry.alias] = fut.result()
    return out


def recommend_model(entries, results: dict[str, tuple[bool, str]]) -> str:
    """(alias) the recommended default: a paid model that answered, else the first free one that did.

    The candidate order decides which paid model wins (the registry order, by alias). "" — none answered.
    """
    from ahub import registry

    answered = [e for e in entries if results.get(e.alias, (False, ""))[0]]
    if not answered:
        return ""
    pick = [e for e in answered if not registry.is_free(e)] or answered
    return pick[0].alias


def probe_none_warning(aliases) -> str:
    """The warning when nothing answered a probe (aliases of what was tried)."""
    return _t("doctor.probe_none", tried=", ".join(aliases), fix=_t("doctor.probe_fix"))


def _free_alias(store) -> str:
    """The free alias to offer (the first candidate) — no live request: hints and questions."""
    cands = free_candidates(store)
    return cands[0].alias if cands else FALLBACK_FREE


def pick_free(store, *, timeout_s: int = PROBE_TIMEOUT_S) -> tuple[str, str]:
    """(free alias, warning): the first candidate that answers the live probe.

    The probe is one tiny free request per candidate. With none answering — the first candidate
    (as before) and a warning for the caller to print; AHUB_PROBE=0 — no probe at all.
    """
    cands = free_candidates(store)
    if not cands:
        return FALLBACK_FREE, ""
    if not probing_enabled():
        return cands[0].alias, ""
    for entry in cands:
        try:
            ok, _detail = probe_model(entry, timeout_s)
        except Exception:
            ok = False
        if ok:
            return entry.alias, ""
    return cands[0].alias, probe_none_warning([e.alias for e in cands])


def check_models(auth: list[str] | None = None) -> Check:
    from ahub import registry
    from ahub.model import Role
    from ahub.store import Store

    store = Store()
    providers = auth if auth is not None else auth_providers()
    go = has_go_login(providers)
    bad: list[str] = []
    menus: dict[str, list[str]] = {}
    for role in Role:
        try:
            menu = registry.menu(store, role)
        except Exception:
            continue
        menus[role.value] = [e.alias for e, _ in menu]
        default = next((e for e, d in menu if d), None)
        if default is None:
            continue
        if default.model_id.startswith("opencode-go/") and not go:
            bad.append(role.value)
    if not bad:
        return Check("models", True, _t("doctor.models_ok"), "")
    free = _free_alias(store)
    parts = []
    for r in bad:
        if free in menus.get(r, []):
            parts.append(f"ahub models role {r} --set-default {free}")
        else:
            parts.append(f"ahub models role {r} --add {free}"
                         f" && ahub models role {r} --set-default {free}")
    cmds = "; ".join(parts)
    return Check("models", False, _t("doctor.models_bad", roles=", ".join(bad)),
                 _t("doctor.models_fix", cmds=cmds))


def _proxy_env_url() -> str:
    env = os.environ
    return (env.get("HTTPS_PROXY") or env.get("https_proxy")
            or env.get("ALL_PROXY") or env.get("all_proxy") or "")


def _proxy_host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    u = urlparse(url if "://" in url else f"http://{url}")
    return u.hostname or "127.0.0.1", u.port or 80


def provider_proxy_detail(name: str) -> str:
    """A provider's own proxy ([providers.<name>]) as part of its line: nothing without a section,
    the proxy's state with one. Only host:port is shown (the URL may carry a password)."""
    from ahub import config
    from ahub.observer import proxy_problem

    try:
        p = config.load_hub().provider(name)
    except config.ConfigError:
        return ""
    if p.proxy is None:
        return ""
    if not p.proxy:
        return _t("doctor.provider_proxy_none")
    host, port = _proxy_host_port(p.proxy)
    key = "doctor.provider_proxy_down" if proxy_problem({"HTTPS_PROXY": p.proxy}) else "doctor.provider_proxy_ok"
    return _t(key, host=host, port=port)


def check_network() -> Check:
    from ahub.observer import proxy_problem

    problem = proxy_problem()
    if problem:
        return Check("network", False, _t("doctor.network_bad", problem=problem),
                     _t("doctor.network_fix"))
    url = _proxy_env_url()
    if not url:
        return Check("network", True, _t("doctor.network_no_proxy"), "")
    host, port = _proxy_host_port(url)
    return Check("network", True, _t("doctor.network_ok", host=host, port=port), "")


def check_claude() -> Check:
    from ahub.tg.launcher import claude_bin

    binary = claude_bin()
    if binary and os.path.isfile(binary) and os.access(binary, os.X_OK):
        return Check("claude", True, _t("doctor.claude_found", binary=binary), "")
    return Check("claude", False, _t("doctor.claude_missing"), _t("doctor.claude_fix"))


def skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / "ahub" / "SKILL.md"


def settings_path(root: Path | None = None) -> Path:
    """The project .claude/settings.json — the file the Bash(ahub:*) permission lives in."""
    return (root if root is not None else Path.cwd()) / ".claude" / "settings.json"


def bash_allowed(root: Path | None = None) -> bool:
    """Does the project allow `ahub …` without a question? An unreadable file — no."""
    f = settings_path(root)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    allow = data.get("permissions", {}).get("allow") if isinstance(data, dict) else None
    return isinstance(allow, list) and BASH_RULE in allow


def check_claude_skill(root: Path | None = None) -> Check:
    """The skill in ~/.claude/skills/ahub and the Bash(ahub:*) rule in the project settings.json.

    Both come from the same setup step: without the rule Claude Code asks for a permission on every command.
    """
    path = skill_path()
    settings = settings_path(root)
    if path.is_file() and bash_allowed(root):
        return Check("claude_skill", True,
                     _t("doctor.skill_ok", path=str(path)) + ", "
                     + _t("doctor.perm_ok", path=str(settings), rule=BASH_RULE), "")
    if path.is_file():
        return Check("claude_skill", False,
                     _t("doctor.skill_ok", path=str(path)) + ", "
                     + _t("doctor.perm_missing", path=str(settings), rule=BASH_RULE),
                     _t("doctor.skill_fix"))
    return Check("claude_skill", False, _t("doctor.skill_missing", path=str(path)),
                 _t("doctor.skill_fix"))


def check_telegram() -> Check:
    import importlib.util

    from ahub import config

    try:
        hub = config.load_hub()
    except config.ConfigError:
        return Check("telegram", None, _t("doctor.telegram_off"), "")
    if not hub.telegram_enabled:
        return Check("telegram", None, _t("doctor.telegram_off"), "")
    if importlib.util.find_spec("aiogram") is not None:
        return Check("telegram", True, _t("doctor.telegram_ok"), "")
    return Check("telegram", False, _t("doctor.telegram_no_aiogram"), _t("doctor.telegram_fix"))


def _safe(name: str, fn) -> Check:
    try:
        return fn()
    except Exception as e:  # one check must not take down the rest
        return _fail(name, e)


def run_all(root: Path | None = None) -> list[Check]:
    """All checks in display order; never raises. root — the project of the claude_skill check, cwd by default."""
    providers: list[str] = []
    try:
        providers = auth_providers()
    except Exception:
        providers = []
    checks = [
        _safe("python", check_python),
        _safe("git", check_git),
        _safe("config", check_config),
        _safe("service", check_service),
        _safe("opencode", check_opencode),
        _safe("opencode_health", check_opencode_health),
        _safe("opencode_auth", lambda: _auth_check(providers)),
        _safe("agy", check_agy),
        _safe("codex", check_codex),
        _safe("models", lambda: check_models(providers)),
        _safe("network", check_network),
        _safe("claude", check_claude),
        _safe("claude_skill", lambda: check_claude_skill(root)),
        _safe("telegram", check_telegram),
    ]
    return checks


__all__ = ["Check", "ProviderState", "TIMEOUT_S", "PROBE_TIMEOUT_S", "PROBE_WIZARD_S", "PROBE_PROMPT",
           "auth_providers", "auth_file_path", "has_go_login", "run_all", "probe_model", "probing_enabled",
           "pick_free", "free_candidates", "probe_models", "recommend_model", "probe_none_warning",
           "provider_states", "provider_state", "provider_line", "install_hint",
           "check_python", "check_git", "check_config", "check_service", "check_opencode",
           "check_opencode_health", "check_opencode_auth", "check_agy", "check_codex", "check_models",
           "check_network", "check_claude", "check_claude_skill", "check_telegram", "provider_proxy_detail",
           "skill_path", "settings_path", "bash_allowed", "apparmor_blocks_userns", "codex_sandbox_fix"]
