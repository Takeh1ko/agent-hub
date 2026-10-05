"""Model and role registry (architecture §4, contracts §9).

- Hub model = short name → provider + model + variant (model table).
- Role menu = which models are shown and which is default (role_model table).
- Task model pick: explicit or role default; explicit — any enabled model.
- Project deny (`[models] deny` in .hub.toml) always applies: an entry is an alias or part of the model id.
  Only a human editing the project file lifts it — the registry has no such knob by design.
- A provider switched off in the hub config (`[providers.<name>] enabled = false`, `ahub providers disable`)
  hides all of its models: they leave the role menus, and naming one in a task is a refusal with a clear reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ahub import paths
from ahub.config import ConfigError, HubConfig, ProjectConfig, load_hub, provider_lookup, set_global
from ahub.i18n import t as _t
from ahub.model import Role
from ahub.providers.base import PlanKind
from ahub.providers.fake import selectable_from_env
from ahub.store import Store

SPARK = "opencode-go/muse-spark-1.3-contributor"

# alias → (provider, model_id, variant, note). One alias per model+plan: <model> for the paid
# default route, <model>-<plan> otherwise. spark-high/spark-medium/gemini-low are legacy names
# (LEGACY_ALIASES): still accepted, mapped to base + effort, hidden from menus.
DEFAULT_MODELS: dict[str, tuple[str, str, str, str]] = {
    "spark": ("opencode", SPARK, "xhigh", "Muse Spark 1.3, main"),
    "mimo-flash": ("opencode", "opencode-go/mimo-v2.6-flash", "", "MiMo 2.6 Flash"),
    "deepseek-flash": ("opencode", "opencode-go/deepseek-v4.1-flash", "high", "DeepSeek v4.1 Flash (pricier)"),
    "spark-free": ("opencode", "opencode/muse-spark-1.3-contributor-free", "xhigh", "free Spark (slower)"),
    "bunny": ("opencode", "opencode/space-bunny-free", "", "Space Bunny free (opencode)"),
    "gemini": ("agy", "gemini-3.8-flash-high", "", "Gemini via agy (window quota)"),
    # Codex CLI: a ChatGPT subscription, no prices; the sandbox limits writes to the copy.
    # The ids come from the login catalog (`codex debug models`, `ahub models --all`).
    "codex": ("codex", "gpt-5.6-terra", "", "Codex via codex CLI (subscription, OS sandbox)"),
    "codex-fast": ("codex", "gpt-5.6-luna", "", "Codex via codex CLI, cheaper/faster (subscription)"),
}

# Legacy names: alias → (base alias, implied effort). Accepted everywhere, mapped to base + effort
# with one line "<legacy> is <base>:<effort>" on use, hidden from menus and tables.
LEGACY_ALIASES: dict[str, tuple[str, str]] = {
    "spark-high": ("spark", "high"),
    "spark-medium": ("spark", "medium"),
    "gemini-low": ("gemini", "low"),
}
HIDDEN_ALIASES: frozenset[str] = frozenset(LEGACY_ALIASES)

# Reasoning levels for the --effort flag and ALIAS[:EFFORT] refs (validated per model against the catalog).
EFFORT_CHOICES: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
_AGY_FALLBACK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")
_CODEX_FALLBACK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")

# role → [(ref, is_default)] in display order; ref is ALIAS[:EFFORT] ("" effort — the alias default).
DEFAULT_MENUS: dict[Role, list[tuple[str, bool]]] = {
    Role.EXECUTOR: [("spark", True), ("mimo-flash", False), ("deepseek-flash", False)],
    Role.REVIEWER: [("spark", True), ("mimo-flash", False), ("deepseek-flash", False)],
    Role.SCOUT: [("spark", True), ("deepseek-flash", False)],
    Role.ROUTINE: [("spark", True), ("mimo-flash", False)],
    Role.OBSERVER: [("spark:high", True), ("spark:medium", False)],
    Role.DRAFTER: [("spark:high", True), ("spark", False)],
}


# Free aliases in the order the probe tries them: the first that answers becomes the default
# (doctor.pick_free). A free model needs no opencode-go login.
FREE_ALIASES: tuple[str, ...] = ("spark-free", "bunny")

# Providers that are paid for by a subscription or a window quota, not per token — the note in the wizard.
PLAN_PROVIDERS: tuple[str, ...] = ("agy", "codex")


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ModelEntry:
    alias: str
    provider: str
    model_id: str
    variant: str = ""
    enabled: bool = True
    note: str = ""


def split_ref(ref: str) -> tuple[str, str]:
    """ALIAS[:EFFORT] → (alias, effort); effort lowercased, "" when absent."""
    s = (ref or "").strip()
    if ":" in s:
        alias, _, effort = s.rpartition(":")
        return alias.strip(), effort.strip().lower()
    return s.strip(), ""


def map_legacy(alias: str, effort: str) -> tuple[str, str, str]:
    """Legacy alias → (base alias, stored effort, notice). Notice "" when not legacy."""
    if alias in LEGACY_ALIASES:
        base, implied = LEGACY_ALIASES[alias]
        eff = effort or implied
        if effort:
            notice = _t("registry.legacy_mapped_full", ref=f"{alias}:{effort}", base=base, effort=eff)
        else:
            notice = _t("registry.legacy_mapped", alias=alias, base=base, effort=eff)
        return base, eff, notice
    return alias, effort, ""


def legacy_notice(ref: str) -> str:
    """One line "<legacy> is <base>:<effort>" when the ref uses a legacy name, else ""."""
    alias, effort = split_ref(ref)
    _base, _eff, notice = map_legacy(alias, effort)
    return notice


def base_alias(ref: str) -> str:
    """Base alias of a ref: legacy mapped, :effort stripped."""
    alias, effort = split_ref(ref)
    base, _eff, _notice = map_legacy(alias, effort)
    return base


def stored_effort(ref: str) -> str:
    """Stored effort of a ref: explicit or legacy-implied, "" when the alias default applies."""
    alias, effort = split_ref(ref)
    _base, eff, _notice = map_legacy(alias, effort)
    return eff


def default_effort(entry: ModelEntry) -> str:
    """Alias default reasoning level (the variant; agy encodes it in the model id)."""
    if entry.variant:
        return entry.variant
    if entry.provider == "agy":
        tail = (entry.model_id or "").lower().rsplit("-", 1)[-1]
        if tail in _AGY_FALLBACK_LEVELS:
            return tail
    return ""


def effective_effort(entry: ModelEntry, stored: str) -> str:
    """Effort to run/display: stored override or the alias default."""
    return stored or default_effort(entry)


def model_ref(alias: str, stored: str) -> str:
    """Canonical ref for storage/display: alias or alias:effort."""
    return f"{alias}:{stored}" if stored else alias


def valid_levels(entry: ModelEntry, info=None, index=None) -> list[str]:
    """Reasoning levels the alias accepts, weakest first (catalog when known, else a fallback)."""
    from ahub.catalog import LEVEL_ORDER as _order

    rank = {lvl: i for i, lvl in enumerate(_order)}
    # catalog first (strict when the provider knows the model)
    avail: list[str] = []
    try:
        if info is None and index is None:
            from ahub import catalog as _catalog

            try:
                catalogs = _catalog.get_catalogs()
            except (OSError, ValueError, RuntimeError, AttributeError):
                catalogs = {}
            try:
                index = _catalog.index_by_id(catalogs)
            except (OSError, ValueError, RuntimeError, AttributeError):
                index = {}
            info = _catalog.match_entry(entry, index) if index else None
        if info is not None or index is not None:
            from ahub import catalog as _catalog

            try:
                avail = _catalog.available_levels(entry, info, index)
            except (OSError, ValueError, RuntimeError, AttributeError):
                avail = []
    except (ImportError, AttributeError):
        avail = []
    if avail:
        seen: list[str] = []
        for lvl in avail:
            low = (lvl or "").lower()
            if low and low not in seen:
                seen.append(low)
        return sorted(seen, key=lambda lv: (rank.get(lv, len(rank)), lv))
    # fallback when the catalog is silent: agy/codex levels are known, otherwise the CLI choices
    # for a model with a default level, nothing for one without reasoning
    if entry.provider == "agy":
        return list(_AGY_FALLBACK_LEVELS)
    if entry.provider == "codex":
        return list(_CODEX_FALLBACK_LEVELS)
    if entry.provider == "fake":
        return ["low", "high"]
    if entry.variant:
        return list(EFFORT_CHOICES)
    return []


def check_effort(entry: ModelEntry, effort: str, info=None, index=None) -> None:
    """Refuse an unknown reasoning level, listing the valid ones."""
    eff = (effort or "").strip().lower()
    if not eff:
        return
    valid = valid_levels(entry, info, index)
    if not valid:
        raise RegistryError(_t("registry.no_effort", alias=entry.alias))
    if eff not in valid:
        raise RegistryError(_t("registry.bad_effort", effort=effort, alias=entry.alias,
                               valid=", ".join(valid)))


def _agy_sibling_id(model_id: str, effort: str) -> str:
    """agy model id for the effort: base without the level suffix + the new suffix."""
    base = (model_id or "")
    low = base.lower()
    for lvl in _AGY_FALLBACK_LEVELS:
        suffix = f"-{lvl}"
        if low.endswith(suffix):
            return base[: -len(suffix)] + f"-{effort}" if effort else base
    return f"{base}-{effort}" if effort and not base.lower().endswith(f"-{effort}") else base


def effective_entry(entry: ModelEntry, stored: str, index=None) -> ModelEntry:
    """Entry to run/display: stored effort applied (agy — the sibling model id)."""
    eff = (stored or "").strip().lower()
    if not eff:
        return entry
    if entry.provider == "agy":
        return ModelEntry(entry.alias, entry.provider, _agy_sibling_id(entry.model_id, eff),
                          "", entry.enabled, entry.note)
    return ModelEntry(entry.alias, entry.provider, entry.model_id, eff, entry.enabled, entry.note)


def resolve_ref(store: Store, ref: str, info=None, index=None) -> tuple[ModelEntry, str, str]:
    """Ref → (base entry, stored effort, legacy notice). Validates an explicit effort."""
    alias, effort = split_ref(ref)
    base, stored, notice = map_legacy(alias, effort)
    entry = get(store, base)
    if stored:
        check_effort(entry, stored, info, index)
    return entry, stored, notice


def seed(store: Store) -> bool:
    """Seed the registry with defaults if empty. True — seeded.

    A default model missing in an older hub (a new alias in the code) is added too: it is enabled,
    but in no role menu — nothing changes until a human picks it.

    AHUB_FAKE_PROVIDER=1 (tests, tools/smoke.sh): the fake provider becomes a normal entry — the model
    "fake" in every role menu and the default of every role, so a task runs with no network.

    Reads first without the write lock: an already seeded hub never takes BEGIN IMMEDIATE here.
    """
    from ahub.providers.fake import ALIAS as _fake_alias

    with store.read() as c:
        have = {r[0] for r in c.execute("SELECT alias FROM model")}
        if have:
            missing = [a for a in DEFAULT_MODELS if a not in have]
            if selectable_from_env() and _fake_alias not in have:
                missing.append(_fake_alias)
            if not missing:
                return False
    with store.tx() as c:
        seeded = not c.execute("SELECT COUNT(*) FROM model").fetchone()[0]
        for alias, (prov, mid, var, note) in DEFAULT_MODELS.items():
            if not c.execute("SELECT 1 FROM model WHERE alias=?", (alias,)).fetchone():
                c.execute("INSERT INTO model(alias, provider, model_id, variant, note) VALUES(?,?,?,?,?)",
                          (alias, prov, mid, var, note))
        if selectable_from_env():
            _seed_fake(c)
        if not seeded:
            return False
        for role, items in DEFAULT_MENUS.items():
            for pos, (ref, is_def) in enumerate(items):
                alias, effort = split_ref(ref)
                _base, stored, _notice = map_legacy(alias, effort)
                c.execute("INSERT INTO role_model(role, alias, position, is_default, effort) VALUES(?,?,?,?,?)",
                          (role.value, _base, pos, 1 if is_def else 0, stored))
        return True


def _seed_fake(c) -> None:
    """The fake provider as a registry entry (env: AHUB_FAKE_PROVIDER=1). Idempotent."""
    from ahub.providers.fake import ALIAS, MODEL_ID

    if c.execute("SELECT 1 FROM model WHERE alias=?", (ALIAS,)).fetchone():
        return
    c.execute("INSERT INTO model(alias, provider, model_id, variant, note) VALUES(?,?,?,?,?)",
              (ALIAS, ALIAS, MODEL_ID, "", "fake provider (AHUB_FAKE_PROVIDER)"))
    for role in Role:
        try:
            c.execute("INSERT OR IGNORE INTO role_model(role, alias, position, is_default, effort)"
                      " VALUES(?,?,999,0,'')", (role.value, ALIAS))
        except Exception:
            c.execute("INSERT OR IGNORE INTO role_model(role, alias, position, is_default) VALUES(?,?,999,0)",
                      (role.value, ALIAS))


def _entry(row) -> ModelEntry:
    return ModelEntry(row["alias"], row["provider"], row["model_id"], row["variant"], bool(row["enabled"]),
                      row["note"])


def models(store: Store) -> list[ModelEntry]:
    seed(store)
    with store.read() as c:
        return [_entry(r) for r in c.execute("SELECT * FROM model ORDER BY alias")]


def get(store: Store, alias: str, info=None, index=None) -> ModelEntry:
    """Base entry by alias; accepts ALIAS[:EFFORT] and legacy names (mapped, validated).

    Returns the effective entry (stored effort applied) so runners get the right variant/model id.
    """
    alias_part, effort_part = split_ref(alias)
    base, stored, _notice = map_legacy(alias_part, effort_part)
    seed(store)
    with store.read() as c:
        row = c.execute("SELECT * FROM model WHERE alias=?", (base,)).fetchone()
    if row is None:
        raise RegistryError(_t("registry.no_model", alias=alias))
    entry = _entry(row)
    if stored:
        check_effort(entry, stored, info, index)
        return effective_entry(entry, stored, index)
    return entry


_cached_disabled: tuple[int, frozenset[str]] | None = None


def disabled_providers(hub: HubConfig | None = None) -> frozenset[str]:
    """Providers switched off in the hub config ([providers.<name>] enabled = false).

    A broken global config must not hide every model: doctor/check_config reports it, here everything stays on.
    """
    if hub is not None:
        return frozenset(name.strip().lower() for name in hub.providers_off)
    p = paths.global_config_path()
    try:
        mtime = p.stat().st_mtime_ns if p.is_file() else -1
    except OSError:
        mtime = -1
    global _cached_disabled
    if _cached_disabled is not None and _cached_disabled[0] == mtime:
        return _cached_disabled[1]
    try:
        hub = load_hub()
    except ConfigError:
        return frozenset()
    result = frozenset(name.strip().lower() for name in hub.providers_off)
    _cached_disabled = (mtime, result)
    return result


def provider_enabled(name: str, hub: HubConfig | None = None) -> bool:
    """Is the provider on (the single place the switch lives: the hub config)."""
    if hub is None:
        try:
            hub = load_hub()
        except ConfigError:
            return True
    return hub.provider_enabled(name)


def set_provider_enabled(name: str, enabled: bool) -> Path:
    """The one writer of the provider switch: [providers.<name>] enabled (the rest of the table stays).

    The read side is provider_enabled() above — `ahub providers enable|disable` and the setup wizard both
    come through here, so nothing imports another command module to change the switch.
    """
    return set_global("enabled", enabled, section=f"providers.{name}",
                      lookup=lambda parsed: provider_lookup(parsed, name))


def _raw_menu(store: Store, role: Role | str) -> list[tuple[ModelEntry, bool]]:
    """Role menu rows as stored, a switched-off provider included (for the reasons in a refusal).

    The entry carries the stored effort (role effort or the alias default): legacy names never appear
    here (the migration maps them), hidden ones are filtered by the caller.
    """
    seed(store)
    with store.read() as c:
        try:
            rows = c.execute("SELECT m.*, rm.is_default, rm.effort AS rm_effort FROM role_model rm"
                             " JOIN model m ON m.alias=rm.alias"
                             " WHERE rm.role=? ORDER BY rm.position, m.alias",
                             (Role(role).value,)).fetchall()
        except Exception:
            rows = c.execute("SELECT m.*, rm.is_default FROM role_model rm JOIN model m ON m.alias=rm.alias"
                             " WHERE rm.role=? ORDER BY rm.position, m.alias", (Role(role).value,)).fetchall()
    out: list[tuple[ModelEntry, bool]] = []
    for r in rows:
        try:
            stored = str(r["rm_effort"] or "")
        except (KeyError, IndexError, TypeError):
            stored = ""
        base = _entry(r)
        if base.alias in HIDDEN_ALIASES:
            continue
        out.append((effective_entry(base, stored), bool(r["is_default"])))
    return out


def menu(store: Store, role: Role | str, hub: HubConfig | None = None) -> list[tuple[ModelEntry, bool]]:
    """Role menu: [(model, default)] in display order; models of a switched-off provider are hidden."""
    off = disabled_providers(hub)
    return [(e, d) for e, d in _raw_menu(store, role) if e.provider not in off]


def role_default(store: Store, role: Role | str) -> ModelEntry | None:
    """The default model of a role menu as stored, None — no default (a switched-off provider included)."""
    items = _raw_menu(store, role)
    return next((e for e, d in items if d), None)


def menu_efforts(store: Store, role: Role | str) -> list[tuple[str, str, bool]]:
    """Menu refs as stored: [(alias, stored effort, default)] in display order (legacy never appears)."""
    seed(store)
    with store.read() as c:
        try:
            rows = c.execute("SELECT alias, effort, is_default FROM role_model WHERE role=? ORDER BY position",
                             (Role(role).value,)).fetchall()
            return [(str(r["alias"]), str(r["effort"] or ""), bool(r["is_default"])) for r in rows
                    if str(r["alias"]) not in HIDDEN_ALIASES]
        except Exception:
            rows = c.execute("SELECT alias, is_default FROM role_model WHERE role=? ORDER BY position",
                             (Role(role).value,)).fetchall()
            return [(str(r["alias"]), "", bool(r["is_default"])) for r in rows
                    if str(r["alias"]) not in HIDDEN_ALIASES]


def role_default_ref(store: Store, role: Role | str) -> tuple[str, str] | None:
    """(alias, stored effort) of the role default, None — no default."""
    for alias, effort, is_def in menu_efforts(store, role):
        if is_def:
            return alias, effort
    return None


def is_free(entry) -> bool:
    """A model that answers without an opencode-go login: a known free alias or a free model id."""
    alias = getattr(entry, "alias", "") or ""
    model_id = getattr(entry, "model_id", "") or ""
    if alias in FREE_ALIASES:
        return True
    return "free" in model_id.lower().rsplit("/", 1)[-1] or "free" in alias.lower()


def plan_kind(entry, info=None) -> PlanKind:
    """Plan for the alias from the provider + catalog (free · go-plan · pay-as-you-go · subscription).

    info — the catalog entry for this model id, when the provider has one: its plan wins,
    except a free alias/model id is always free (a cost-0 catalog row for a paid alias stays paid).
    Without a catalog the provider name decides (agy/codex → subscription, opencode-go → go-plan,
    openrouter/opencode → pay-as-you-go).
    """
    from ahub.providers.base import PlanKind as _Plan
    from ahub.providers.base import infer_plan as _infer

    if is_free(entry):
        return _Plan.FREE
    if info is not None:
        pin = info.price_in if isinstance(getattr(info, "price_in", None), (int, float)) else None
        pout = info.price_out if isinstance(getattr(info, "price_out", None), (int, float)) else None
        if pin == 0 and pout == 0:
            return _Plan.FREE
        try:
            return info.plan
        except AttributeError:
            pass
    return _infer(getattr(entry, "provider", "") or "", getattr(entry, "model_id", "") or "", None, None)


def cost_kind(entry: ModelEntry) -> str:
    """free | paid | plan — what the user pays for the model (a note in the setup wizard).

    Kept for its callers; new code uses plan_kind() (one enum with i18n labels).
    """
    from ahub.providers.base import PlanKind as _Plan

    plan = plan_kind(entry)
    if plan is _Plan.FREE:
        return "free"
    return "plan" if plan is _Plan.SUBSCRIPTION else "paid"


def free_candidates(store: Store, hub: HubConfig | None = None) -> list[ModelEntry]:
    """Enabled free aliases to try, in order: FREE_ALIASES first, then any other free model."""
    entries = models(store)  # ordered by alias
    off = disabled_providers(hub)
    known = {e.alias: e for e in entries if e.enabled and e.alias in FREE_ALIASES and e.provider not in off}
    out = [known[a] for a in FREE_ALIASES if a in known]
    return out + [e for e in entries if e.enabled and e.provider not in off
                  and is_free(e) and e.alias not in known]


def denied_by(entry: ModelEntry, project: ProjectConfig | None) -> str | None:
    """Project deny entry matching the model, or None."""
    if project is None:
        return None
    for rule in project.models_deny:
        r = rule.strip().lower()
        if r and (r == entry.alias.lower() or r in entry.model_id.lower()):
            return rule
    return None


def check(store: Store, alias: str, project: ProjectConfig | None, hub: HubConfig | None = None,
          info=None, index=None) -> ModelEntry:
    """Model fit for a project task: exists, enabled, provider on, not denied by the project.

    Accepts ALIAS[:EFFORT] and legacy names (mapped to base + effort, validated against the catalog).
    Returns the effective entry (stored effort applied).
    """
    base, stored, _notice = map_legacy(*split_ref(alias))
    entry = get(store, base)
    if stored:
        check_effort(entry, stored, info, index)
    eff = effective_entry(entry, stored, index)
    if not eff.enabled:
        raise RegistryError(_t("registry.disabled", alias=base))
    if not provider_enabled(eff.provider, hub):
        raise RegistryError(_t("registry.provider_off", alias=base, provider=eff.provider))
    rule = denied_by(eff, project)
    if rule is not None:
        raise RegistryError(_t("registry.denied", alias=base, project=project.name, rule=rule))
    return eff


def pick(store: Store, role: Role | str, project: ProjectConfig | None, explicit: str | None = None,
         hub: HubConfig | None = None, effort: str | None = None,
         info=None, index=None) -> ModelEntry:
    """Model for a role: explicit (checked) or default; if the default is denied — first allowed menu entry.

    explicit is ALIAS[:EFFORT] (legacy mapped); effort overrides it when given (a mismatch is refused
    by the caller — here an explicit :effort and effort="" simply combine). With effort and no
    explicit alias the override applies to every menu candidate in order: a denied default falls
    back to the next allowed entry, exactly like a pick without an effort.
    """
    if explicit:
        alias_part, effort_part = split_ref(explicit)
        base, stored, _notice = map_legacy(alias_part, effort_part)
        want = (effort or "").strip().lower() or stored
        ref = f"{base}:{want}" if want else base
        return check(store, ref, project, hub, info, index)
    want_override = (effort or "").strip().lower()
    if selectable_from_env():
        try:
            return check(store, f"fake:{want_override}" if want_override else "fake",
                         project, hub, info, index)
        except RegistryError:
            pass
    off = disabled_providers(hub)
    items = _raw_menu(store, role)
    ordered = [e for e, d in items if d] + [e for e, d in items if not d]
    reasons = []
    for e in ordered:
        if not e.enabled:
            reasons.append(_t("registry.reason_disabled", alias=e.alias))
            continue
        if e.provider in off:
            reasons.append(_t("registry.reason_provider_off", alias=e.alias, provider=e.provider))
            continue
        rule = denied_by(e, project)
        if rule is not None:
            reasons.append(_t("registry.reason_denied", alias=e.alias))
            continue
        if want_override:
            try:
                return check(store, f"{e.alias}:{want_override}", project, hub, info, index)
            except RegistryError as err:
                reasons.append(str(err))
                continue
        return e
    suffix = f" ({'; '.join(reasons)})" if reasons else ""
    raise RegistryError(_t("registry.no_role", role=Role(role).value, reasons=suffix))


# --- changes (human in the terminal / orchestrator) ---

def add_model(store: Store, alias: str, provider: str, model_id: str, variant: str = "", note: str = "") -> None:
    seed(store)
    if not alias or not provider or not model_id:
        raise RegistryError(_t("registry.need_fields"))
    with store.tx() as c:
        if c.execute("SELECT 1 FROM model WHERE alias=?", (alias,)).fetchone():
            raise RegistryError(_t("registry.exists", alias=alias))
        c.execute("INSERT INTO model(alias, provider, model_id, variant, note) VALUES(?,?,?,?,?)",
                  (alias, provider, model_id, variant, note))


def set_enabled(store: Store, alias: str, enabled: bool) -> None:
    alias_part, effort_part = split_ref(alias)
    base, _stored, _notice = map_legacy(alias_part, effort_part)
    get(store, base)
    with store.tx() as c:
        c.execute("UPDATE model SET enabled=? WHERE alias=?", (1 if enabled else 0, base))


def add_to_role(store: Store, role: Role | str, alias: str, default: bool = False,
                effort: str = "") -> None:
    alias_part, effort_part = split_ref(alias)
    base, stored, _notice = map_legacy(alias_part, effort_part or effort)
    get(store, base)
    if stored:
        check_effort(get(store, base), stored)
    r = Role(role).value
    with store.tx() as c:
        pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM role_model WHERE role=?", (r,)).fetchone()[0]
        try:
            c.execute("INSERT OR IGNORE INTO role_model(role, alias, position, effort) VALUES(?,?,?,?)",
                      (r, base, pos, stored))
        except Exception:
            c.execute("INSERT OR IGNORE INTO role_model(role, alias, position) VALUES(?,?,?)", (r, base, pos))
        if default:
            try:
                c.execute("UPDATE role_model SET is_default=((alias=? AND effort=?)) WHERE role=?",
                          (base, stored, r))
            except Exception:
                c.execute("UPDATE role_model SET is_default=(alias=?) WHERE role=?", (base, r))


def remove_from_role(store: Store, role: Role | str, alias: str, effort: str | None = None) -> None:
    alias_part, effort_part = split_ref(alias)
    base, stored, _notice = map_legacy(alias_part, effort_part if effort is None else effort)
    explicit = effort is not None or bool(effort_part)
    r = Role(role).value
    with store.tx() as c:
        try:
            rows = c.execute("SELECT effort, is_default FROM role_model WHERE role=? AND alias=?"
                             " ORDER BY position", (r, base)).fetchall()
            left = c.execute("SELECT COUNT(*) FROM role_model WHERE role=?", (r,)).fetchone()[0]
            use_effort = True
        except Exception:
            rows, left, use_effort = None, 0, False
        if rows is None:
            # pre-effort schema (live reload under migration): one row per alias at most
            row = c.execute("SELECT is_default FROM role_model WHERE role=? AND alias=?", (r, base)).fetchone()
            if row is None:
                raise RegistryError(_t("registry.no_menu", alias=base, role=r))
            left = c.execute("SELECT COUNT(*) FROM role_model WHERE role=?", (r,)).fetchone()[0]
            if left <= 1:
                raise RegistryError(_t("registry.menu_last", role=r))
            c.execute("DELETE FROM role_model WHERE role=? AND alias=?", (r, base))
            if row["is_default"]:
                c.execute("UPDATE role_model SET is_default=1 WHERE role=? AND alias="
                          "(SELECT alias FROM role_model WHERE role=? ORDER BY position LIMIT 1)", (r, r))
            return
        if not rows:
            raise RegistryError(_t("registry.no_menu",
                                    alias=model_ref(base, stored) if explicit else base, role=r))
        if not explicit and len(rows) > 1:
            refs = ", ".join(model_ref(base, str(w["effort"] or "")) for w in rows)
            raise RegistryError(_t("registry.need_effort", alias=base, role=r, refs=refs))
        doomed = [w for w in rows if str(w["effort"] or "") == stored] if explicit else list(rows)
        if not doomed:
            raise RegistryError(_t("registry.no_menu", alias=model_ref(base, stored), role=r))
        if left - len(doomed) < 1:
            raise RegistryError(_t("registry.menu_last", role=r))
        if explicit and use_effort:
            c.execute("DELETE FROM role_model WHERE role=? AND alias=? AND effort=?", (r, base, stored))
        else:
            c.execute("DELETE FROM role_model WHERE role=? AND alias=?", (r, base))
        if any(w["is_default"] for w in doomed):
            try:
                c.execute("UPDATE role_model SET is_default=1 WHERE role=? AND (alias, effort)=("
                          "SELECT alias, effort FROM role_model WHERE role=? ORDER BY position LIMIT 1)",
                          (r, r))
            except Exception:
                c.execute("UPDATE role_model SET is_default=1 WHERE role=? AND alias="
                          "(SELECT alias FROM role_model WHERE role=? ORDER BY position LIMIT 1)", (r, r))


def set_default(store: Store, role: Role | str, alias: str, effort: str = "") -> None:
    alias_part, effort_part = split_ref(alias)
    base, stored, _notice = map_legacy(alias_part, effort_part or effort)
    if stored:
        check_effort(get(store, base), stored)
    r = Role(role).value
    with store.tx() as c:
        try:
            if not c.execute("SELECT 1 FROM role_model WHERE role=? AND alias=? AND effort=?",
                             (r, base, stored)).fetchone():
                raise RegistryError(_t("registry.menu_add_first", alias=model_ref(base, stored), role=r))
            c.execute("UPDATE role_model SET is_default=((alias=? AND effort=?)) WHERE role=?",
                      (base, stored, r))
        except RegistryError:
            raise
        except Exception:
            if not c.execute("SELECT 1 FROM role_model WHERE role=? AND alias=?", (r, base)).fetchone():
                raise RegistryError(_t("registry.menu_add_first", alias=base, role=r)) from None
            c.execute("UPDATE role_model SET is_default=(alias=?) WHERE role=?", (base, r))
