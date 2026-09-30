"""Реестр моделей и ролей (architecture §4, contracts §9).

- Модель хаба = короткое имя → поставщик + модель + вариант (таблица model).
- Меню роли = какие модели показываются и какая по умолчанию (таблица role_model).
- Выбор модели для задачи: явная или по умолчанию роли; явная — любая включённая модель.
- Запрет проекта (`[models] deny` в .hub.toml) действует всегда: запись — алиас или часть id модели.
  Снимает его только человек правкой файла проекта — у реестра такой ручки нет по построению.
"""

from __future__ import annotations

from dataclasses import dataclass

from ahub.config import ProjectConfig
from ahub.model import Role
from ahub.store import Store

SPARK = "opencode-go/muse-spark-1.3-contributor"

# alias → (provider, model_id, variant, note)
DEFAULT_MODELS: dict[str, tuple[str, str, str, str]] = {
    "spark": ("opencode", SPARK, "xhigh", "Muse Spark 1.3, основной"),
    "spark-high": ("opencode", SPARK, "high", "Spark 1.3, размышление high"),
    "spark-medium": ("opencode", SPARK, "medium", "Spark 1.3, размышление medium"),
    "mimo-flash": ("opencode", "opencode-go/mimo-v2.6-flash", "", "MiMo 2.6 Flash"),
    "deepseek-flash": ("opencode", "opencode-go/deepseek-v4.1-flash", "high", "DeepSeek v4.1 Flash (дороже Spark)"),
    "spark-free": ("opencode", "opencode/muse-spark-1.3-contributor-free", "xhigh", "бесплатный Spark (медленнее)"),
    "gemini": ("agy", "gemini-3.8-flash-high", "", "Gemini через agy (квота окном)"),
}

# role → [(alias, is_default)] в порядке показа
DEFAULT_MENUS: dict[Role, list[tuple[str, bool]]] = {
    Role.EXECUTOR: [("spark", True), ("mimo-flash", False), ("deepseek-flash", False)],
    Role.REVIEWER: [("spark", True), ("mimo-flash", False), ("deepseek-flash", False)],
    Role.SCOUT: [("spark", True), ("deepseek-flash", False)],
    Role.ROUTINE: [("spark", True), ("mimo-flash", False)],
    Role.OBSERVER: [("spark-high", True), ("spark-medium", False)],
    Role.DRAFTER: [("spark-high", True), ("spark", False)],
}


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
    """Заполнить реестр умолчаниями, если он пуст. True — заполнили."""
    with store.tx() as c:
        if c.execute("SELECT COUNT(*) FROM model").fetchone()[0]:
            return False
        for alias, (prov, mid, var, note) in DEFAULT_MODELS.items():
            c.execute("INSERT INTO model(alias, provider, model_id, variant, note) VALUES(?,?,?,?,?)",
                      (alias, prov, mid, var, note))
        for role, items in DEFAULT_MENUS.items():
            for pos, (alias, is_def) in enumerate(items):
                c.execute("INSERT INTO role_model(role, alias, position, is_default) VALUES(?,?,?,?)",
                          (role.value, alias, pos, 1 if is_def else 0))
        return True


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
        raise RegistryError(f"нет модели {alias!r}")
    return _entry(row)


def menu(store: Store, role: Role | str) -> list[tuple[ModelEntry, bool]]:
    """Меню роли: [(модель, по умолчанию)] в порядке показа."""
    seed(store)
    with store.read() as c:
        rows = c.execute("SELECT m.*, rm.is_default FROM role_model rm JOIN model m ON m.alias=rm.alias"
                         " WHERE rm.role=? ORDER BY rm.position, m.alias", (Role(role).value,)).fetchall()
    return [(_entry(r), bool(r["is_default"])) for r in rows]


def denied_by(entry: ModelEntry, project: ProjectConfig | None) -> str | None:
    """Запись запрета проекта, которая касается модели, или None."""
    if project is None:
        return None
    for rule in project.models_deny:
        r = rule.strip().lower()
        if r and (r == entry.alias.lower() or r in entry.model_id.lower()):
            return rule
    return None


def check(store: Store, alias: str, project: ProjectConfig | None) -> ModelEntry:
    """Модель годится для задачи проекта: есть, включена, не запрещена проектом."""
    entry = get(store, alias)
    if not entry.enabled:
        raise RegistryError(f"модель {alias} выключена")
    rule = denied_by(entry, project)
    if rule is not None:
        raise RegistryError(f"модель {alias} запрещена в проекте {project.name} (правило {rule!r};"
                            f" снять может только человек в .hub.toml)")
    return entry


def pick(store: Store, role: Role | str, project: ProjectConfig | None, explicit: str | None = None) -> ModelEntry:
    """Модель для роли: явная (проверенная) или по умолчанию; если умолчание запрещено — первая разрешённая из меню."""
    if explicit:
        return check(store, explicit, project)
    items = menu(store, role)
    ordered = [e for e, d in items if d] + [e for e, d in items if not d]
    reasons = []
    for e in ordered:
        if not e.enabled:
            reasons.append(f"{e.alias}: выключена")
            continue
        rule = denied_by(e, project)
        if rule is not None:
            reasons.append(f"{e.alias}: запрещена проектом")
            continue
        return e
    raise RegistryError(f"для роли {Role(role).value} нет доступной модели" + (f" ({'; '.join(reasons)})"
                                                                              if reasons else ""))


# --- изменения (человек в терминале / оркестратор) ---

def add_model(store: Store, alias: str, provider: str, model_id: str, variant: str = "", note: str = "") -> None:
    seed(store)
    if not alias or not provider or not model_id:
        raise RegistryError("нужны alias, provider и model_id")
    with store.tx() as c:
        if c.execute("SELECT 1 FROM model WHERE alias=?", (alias,)).fetchone():
            raise RegistryError(f"модель {alias} уже есть")
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
            raise RegistryError(f"{alias} нет в меню роли {r}")
        left = c.execute("SELECT COUNT(*) FROM role_model WHERE role=?", (r,)).fetchone()[0]
        if left <= 1:
            raise RegistryError(f"в меню роли {r} должна остаться хотя бы одна модель")
        c.execute("DELETE FROM role_model WHERE role=? AND alias=?", (r, alias))
        if row["is_default"]:
            c.execute("UPDATE role_model SET is_default=1 WHERE role=? AND alias="
                      "(SELECT alias FROM role_model WHERE role=? ORDER BY position LIMIT 1)", (r, r))


def set_default(store: Store, role: Role | str, alias: str) -> None:
    r = Role(role).value
    with store.tx() as c:
        if not c.execute("SELECT 1 FROM role_model WHERE role=? AND alias=?", (r, alias)).fetchone():
            raise RegistryError(f"{alias} нет в меню роли {r} — сначала добавьте")
        c.execute("UPDATE role_model SET is_default=(alias=?) WHERE role=?", (alias, r))
