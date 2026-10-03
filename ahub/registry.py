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

from ahub.config import ConfigError, HubConfig, ProjectConfig, load_hub
from ahub.i18n import t as _t
from ahub.model import Role
from ahub.providers.fake import selectable_from_env
from ahub.store import Store

SPARK = "opencode-go/muse-spark-1.3-contributor"

# alias → (provider, model_id, variant, note)
DEFAULT_MODELS: dict[str, tuple[str, str, str, str]] = {
    "spark": ("opencode", SPARK, "xhigh", "Muse Spark 1.3, main"),
    "spark-high": ("opencode", SPARK, "high", "Spark 1.3, high reasoning"),
    "spark-medium": ("opencode", SPARK, "medium", "Spark 1.3, medium reasoning"),
    "mimo-flash": ("opencode", "opencode-go/mimo-v2.6-flash", "", "MiMo 2.6 Flash"),
    "deepseek-flash": ("opencode", "opencode-go/deepseek-v4.1-flash", "high", "DeepSeek v4.1 Flash (pricier)"),
    "spark-free": ("opencode", "opencode/muse-spark-1.3-contributor-free", "xhigh", "free Spark (slower)"),
    "bunny": ("opencode", "opencode/space-bunny-free", "", "Space Bunny free (opencode)"),
    "gemini": ("agy", "gemini-3.8-flash-high", "", "Gemini via agy (window quota)"),
    "gemini-low": ("agy", "gemini-3.8-flash-low", "", "Gemini via agy, fast (window quota)"),
    # Codex CLI: a ChatGPT subscription, no prices; the sandbox limits writes to the copy.
    # The ids come from the login catalog (`codex debug models`, `ahub models --all`).
    "codex": ("codex", "gpt-5.6-terra", "", "Codex via codex CLI (subscription, OS sandbox)"),
    "codex-fast": ("codex", "gpt-5.6-luna", "", "Codex via codex CLI, cheaper/faster (subscription)"),
}

# role → [(alias, is_default)] in display order
DEFAULT_MENUS: dict[Role, list[tuple[str, bool]]] = {
    Role.EXECUTOR: [("spark", True), ("mimo-flash", False), ("deepseek-flash", False)],
    Role.REVIEWER: [("spark", True), ("mimo-flash", False), ("deepseek-flash", False)],
    Role.SCOUT: [("spark", True), ("deepseek-flash", False)],
    Role.ROUTINE: [("spark", True), ("mimo-flash", False)],
    Role.OBSERVER: [("spark-high", True), ("spark-medium", False)],
    Role.DRAFTER: [("spark-high", True), ("spark", False)],
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


def seed(store: Store) -> bool:
    """Seed the registry with defaults if empty. True — seeded.

    A default model missing in an older hub (a new alias in the code) is added too: it is enabled,
    but in no role menu — nothing changes until a human picks it.

    AHUB_FAKE_PROVIDER=1 (tests, tools/smoke.sh): the fake provider becomes a normal entry — the model
    "fake" in every role menu and the default of every role, so a task runs with no network.
    """
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
            for pos, (alias, is_def) in enumerate(items):
                c.execute("INSERT INTO role_model(role, alias, position, is_default) VALUES(?,?,?,?)",
                          (role.value, alias, pos, 1 if is_def else 0))
        return True


def _seed_fake(c) -> None:
    """The fake provider as a registry entry (env: AHUB_FAKE_PROVIDER=1). Idempotent."""
    from ahub.providers.fake import ALIAS, MODEL_ID

    if not c.execute("SELECT 1 FROM model WHERE alias=?", (ALIAS,)).fetchone():
        c.execute("INSERT INTO model(alias, provider, model_id, variant, note) VALUES(?,?,?,?,?)",
                  (ALIAS, ALIAS, MODEL_ID, "", "fake provider (AHUB_FAKE_PROVIDER)"))
    for role in Role:
        c.execute("INSERT OR IGNORE INTO role_model(role, alias, position, is_default) VALUES(?,?,999,0)",
                  (role.value, ALIAS))
        c.execute("UPDATE role_model SET is_default=(alias=?) WHERE role=?", (ALIAS, role.value))


def _entry(row) -> ModelEntry:
    return ModelEntry(row["alias"], row["provider"], row["model_id"], row["variant"], bool(row["enabled"]),
                      row["note"])


def models(store: Store) -> list[ModelEntry]:
    seed(store)
    with store.read() as c:
        return [_entry(r) for r in c.execute("SELECT * FROM model ORDER BY alias")]


def get(store: Store, alias: str) -> ModelEntry:
    seed(store)
    with store.read() as c:
        row = c.execute("SELECT * FROM model WHERE alias=?", (alias,)).fetchone()
    if row is None:
        raise RegistryError(_t("registry.no_model", alias=alias))
    return _entry(row)


def disabled_providers(hub: HubConfig | None = None) -> frozenset[str]:
    """Providers switched off in the hub config ([providers.<name>] enabled = false).

    A broken global config must not hide every model: doctor/check_config reports it, here everything stays on.
    """
    if hub is None:
        try:
            hub = load_hub()
        except ConfigError:
            return frozenset()
    return frozenset(name.strip().lower() for name in hub.providers_off)


def provider_enabled(name: str, hub: HubConfig | None = None) -> bool:
    """Is the provider on (the single place the switch lives: the hub config)."""
    return name.strip().lower() not in disabled_providers(hub)


def _raw_menu(store: Store, role: Role | str) -> list[tuple[ModelEntry, bool]]:
    """Role menu rows as stored, a switched-off provider included (for the reasons in a refusal)."""
    seed(store)
    with store.read() as c:
        rows = c.execute("SELECT m.*, rm.is_default FROM role_model rm JOIN model m ON m.alias=rm.alias"
                         " WHERE rm.role=? ORDER BY rm.position, m.alias", (Role(role).value,)).fetchall()
    return [(_entry(r), bool(r["is_default"])) for r in rows]


def menu(store: Store, role: Role | str) -> list[tuple[ModelEntry, bool]]:
    """Role menu: [(model, default)] in display order; models of a switched-off provider are hidden."""
    off = disabled_providers()
    return [(e, d) for e, d in _raw_menu(store, role) if e.provider not in off]


def role_default(store: Store, role: Role | str) -> ModelEntry | None:
    """The default model of a role menu as stored, None — no default (a switched-off provider included)."""
    try:
        items = _raw_menu(store, role)
    except Exception:
        return None
    return next((e for e, d in items if d), None)


def is_free(entry: ModelEntry) -> bool:
    """A model that answers without an opencode-go login: a known free alias or a free model id."""
    if entry.alias in FREE_ALIASES:
        return True
    return "free" in entry.model_id.lower().rsplit("/", 1)[-1] or "free" in entry.alias.lower()


def cost_kind(entry: ModelEntry) -> str:
    """free | paid | plan — what the user pays for the model (a note in the setup wizard)."""
    if is_free(entry):
        return "free"
    return "plan" if entry.provider in PLAN_PROVIDERS else "paid"


def free_candidates(store: Store) -> list[ModelEntry]:
    """Enabled free aliases to try, in order: FREE_ALIASES first, then any other free model."""
    entries = models(store)  # ordered by alias
    off = disabled_providers()
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


def check(store: Store, alias: str, project: ProjectConfig | None) -> ModelEntry:
    """Model fit for a project task: exists, enabled, provider on, not denied by the project."""
    entry = get(store, alias)
    if not entry.enabled:
        raise RegistryError(_t("registry.disabled", alias=alias))
    if not provider_enabled(entry.provider):
        raise RegistryError(_t("registry.provider_off", alias=alias, provider=entry.provider))
    rule = denied_by(entry, project)
    if rule is not None:
        raise RegistryError(_t("registry.denied", alias=alias, project=project.name, rule=rule))
    return entry


def pick(store: Store, role: Role | str, project: ProjectConfig | None, explicit: str | None = None) -> ModelEntry:
    """Model for a role: explicit (checked) or default; if the default is denied — first allowed menu entry."""
    if explicit:
        return check(store, explicit, project)
    off = disabled_providers()
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
    get(store, alias)
    with store.tx() as c:
        c.execute("UPDATE model SET enabled=? WHERE alias=?", (1 if enabled else 0, alias))


def add_to_role(store: Store, role: Role | str, alias: str, default: bool = False) -> None:
    get(store, alias)
    r = Role(role).value
    with store.tx() as c:
        pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM role_model WHERE role=?", (r,)).fetchone()[0]
        c.execute("INSERT OR IGNORE INTO role_model(role, alias, position) VALUES(?,?,?)", (r, alias, pos))
        if default:
            c.execute("UPDATE role_model SET is_default=(alias=?) WHERE role=?", (alias, r))


def remove_from_role(store: Store, role: Role | str, alias: str) -> None:
    r = Role(role).value
    with store.tx() as c:
        row = c.execute("SELECT is_default FROM role_model WHERE role=? AND alias=?", (r, alias)).fetchone()
        if row is None:
            raise RegistryError(_t("registry.no_menu", alias=alias, role=r))
        left = c.execute("SELECT COUNT(*) FROM role_model WHERE role=?", (r,)).fetchone()[0]
        if left <= 1:
            raise RegistryError(_t("registry.menu_last", role=r))
        c.execute("DELETE FROM role_model WHERE role=? AND alias=?", (r, alias))
        if row["is_default"]:
            c.execute("UPDATE role_model SET is_default=1 WHERE role=? AND alias="
                      "(SELECT alias FROM role_model WHERE role=? ORDER BY position LIMIT 1)", (r, r))


def set_default(store: Store, role: Role | str, alias: str) -> None:
    r = Role(role).value
    with store.tx() as c:
        if not c.execute("SELECT 1 FROM role_model WHERE role=? AND alias=?", (r, alias)).fetchone():
            raise RegistryError(_t("registry.menu_add_first", alias=alias, role=r))
        c.execute("UPDATE role_model SET is_default=(alias=?) WHERE role=?", (alias, r))
