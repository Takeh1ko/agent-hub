"""Installation check: what is wrong and what to do (plan docs/v3/plan.md section 5).

Checks live here so the `ahub setup` wizard (next task) can reuse them.
Every user-visible string goes through t() (keys doctor.*); this module
never logs secret values (auth.json values are never read into output).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ahub.i18n import t as _t

TIMEOUT_S = 10  # short timeout for external calls (spec: <= 15 s)

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
        return Check("opencode", True, _t("doctor.opencode_found", binary=binary), "")
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
        return Check("agy", True, _t("doctor.agy_found", binary=binary) + suffix, "")
    problems = "; ".join(h.problems)[:500]
    fix = _t("doctor.agy_fix_login") if not agy_state_file().is_file() else ""
    return Check("agy", False, _t("doctor.health_bad", problems=problems), fix)


def _free_alias(store) -> str:
    try:
        from ahub import registry

        models = registry.models(store)
    except Exception:
        return "spark-free"
    for m in models:
        if m.alias == "spark-free" and m.enabled:
            return m.alias
    for m in models:
        if m.enabled and ("free" in m.model_id.lower() or "free" in m.alias.lower()):
            return m.alias
    return "spark-free"


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


def check_network() -> Check:
    from urllib.parse import urlparse

    from ahub.observer import proxy_problem

    problem = proxy_problem()
    if problem:
        return Check("network", False, _t("doctor.network_bad", problem=problem),
                     _t("doctor.network_fix"))
    url = _proxy_env_url()
    if not url:
        return Check("network", True, _t("doctor.network_no_proxy"), "")
    u = urlparse(url if "://" in url else f"http://{url}")
    return Check("network", True,
                 _t("doctor.network_ok", host=u.hostname or "127.0.0.1", port=u.port or 80), "")


def check_claude() -> Check:
    from ahub.tg.launcher import claude_bin

    binary = claude_bin()
    if binary and os.path.isfile(binary) and os.access(binary, os.X_OK):
        return Check("claude", True, _t("doctor.claude_found", binary=binary), "")
    return Check("claude", False, _t("doctor.claude_missing"), _t("doctor.claude_fix"))


def skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / "ahub" / "SKILL.md"


def check_claude_skill() -> Check:
    path = skill_path()
    if path.is_file():
        return Check("claude_skill", True, _t("doctor.skill_ok", path=str(path)), "")
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


def run_all() -> list[Check]:
    """All checks in display order; never raises."""
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
        _safe("models", lambda: check_models(providers)),
        _safe("network", check_network),
        _safe("claude", check_claude),
        _safe("claude_skill", check_claude_skill),
        _safe("telegram", check_telegram),
    ]
    return checks


__all__ = ["Check", "TIMEOUT_S", "auth_providers", "auth_file_path", "has_go_login", "run_all",
           "check_python", "check_git", "check_config", "check_service", "check_opencode",
           "check_opencode_health", "check_opencode_auth", "check_agy", "check_models",
           "check_network", "check_claude", "check_claude_skill", "check_telegram", "skill_path"]
