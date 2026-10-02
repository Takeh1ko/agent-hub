"""Конфиг хаба (~/.config/ahub/config.toml) и проектов (.hub.toml, schema_version = 2).

Чтение — чистые функции. Ошибки не глотаются: разбор копит все проблемы и бросает ConfigError со списком,
проверка файловой системы — отдельно (`check_project`), чтобы конфиг можно было разобрать без диска.
Файлы v1 (schema_version = 1) читаются с переводом полей — нужно для переключения (V31a).

Пример ~/.config/ahub/config.toml:

    projects = ["$HOME/Projects/webapp"]

    [telegram]                          # всё необязательно; бот включён, если есть token
    token = "..."
    chat_id = 123
    proxy = "http://127.0.0.1:8080"

    [usage]
    go_month_limit = 60.0               # нет — лимит не показывается

    [paths]                             # всё необязательно; переопределение путей
    opencode = "$HOME/bin/opencode"
    claude = "~/.claude/local/claude"
    opencode_db = "$HOME/.local/share/opencode/opencode.db"

Пример .hub.toml v2:

    schema_version = 2
    name = "webapp"
    root = "$HOME/Projects/webapp"        # по умолчанию — каталог файла
    worktrees = "$HOME/Projects/webapp-wt"
    work_branch = "main"
    python = "$HOME/Projects/webapp/venv/bin/python"
    rules = "docs/agents/rules.md"
    allowed_paths = ["core/**", "tests/**"]
    max_parallel = 2
    test_resource = "test_db"                      # приёмка идёт под этим ресурсом

    [resources]                                    # общие ресурсы: не больше capacity задач одновременно
    test_db = { lock = "/tmp/webapp_test_db.lock" }   # lock — внешний flock-файл, общий с другими инструментами
    gpu = { capacity = 1 }

    [hooks]                                        # shell; env: AHUB_TASK_ID, AHUB_WORKTREE, AHUB_PROJECT_ROOT
    task_setup = "venv/bin/python -m tools.task_db create"
    task_cleanup = "venv/bin/python -m tools.task_db drop"

    [models]
    deny = ["slow-model"]                          # снимает только человек

    [budget]
    go = 1.5
    usd = 0.0

    [secrets]
    exclude = [".env", "*.pem"]                    # не попадают в копию проекта для работника

    [timeouts]
    idle_s = 900
    retry_max = 3
    retry_pause_s = 120
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ahub import paths
from ahub.i18n import t as _t

PROJECT_FILE = ".hub.toml"
SCHEMA_VERSION = 2
DEFAULT_SECRET_EXCLUDES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*")


class ConfigError(ValueError):
    """Конфиг не разобран; `errors` — все найденные проблемы."""

    def __init__(self, source: str, errors: list[str]) -> None:
        self.source = source
        self.errors = list(errors)
        super().__init__(f"{source}: " + "; ".join(self.errors))


def expand(value: str) -> str:
    """Развернуть $VAR и ~ в пути."""
    return os.path.expandvars(os.path.expanduser(value))


@dataclass(frozen=True)
class Resource:
    name: str
    capacity: int = 1
    lock: str = ""  # внешний flock-файл (общий с инструментами вне хаба); пусто — только счётчик хаба


@dataclass(frozen=True)
class Hooks:
    task_setup: str = ""
    task_cleanup: str = ""


@dataclass(frozen=True)
class Timeouts:
    idle_s: int = 900  # тишина работника: нет событий N c
    retry_max: int = 3  # повторов шага при сбое сети/сервера
    retry_pause_s: float = 120.0  # пауза перед повтором, растёт ×2


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    root: str
    source: str = ""  # путь к .hub.toml
    schema_version: int = SCHEMA_VERSION
    worktrees: str = ""
    work_branch: str = "main"
    branch_prefix: str = "ahub/"
    push: str = ""
    python: str = ""
    rules: str = ""
    allowed_paths: tuple[str, ...] = ()
    max_parallel: int = 2
    resources: dict[str, Resource] = field(default_factory=dict)
    test_resource: str = ""
    hooks: Hooks = field(default_factory=Hooks)
    models_deny: tuple[str, ...] = ()
    budget_go: float = 1.5
    budget_usd: float = 0.0
    secret_excludes: tuple[str, ...] = DEFAULT_SECRET_EXCLUDES
    timeouts: Timeouts = field(default_factory=Timeouts)

    def rules_path(self) -> Path | None:
        if not self.rules:
            return None
        p = Path(self.rules)
        return p if p.is_absolute() else Path(self.root) / p


@dataclass(frozen=True)
class HubConfig:
    projects: tuple[str, ...] = ()  # пути к корням проектов (или к их .hub.toml)
    source: str = ""
    lang: str = ""  # lang = "en" | "ru"; пусто — по LANG/AHUB_LANG
    tg_token: str = ""  # [telegram] token; пусто — бот выключен
    tg_chat_id: int | None = None  # [telegram] chat_id; запасной чат для рассылки
    tg_proxy: str = ""  # [telegram] proxy; пусто — системный HTTPS_PROXY
    go_month_limit: float | None = None  # [usage] go_month_limit; None — не показывать
    opencode: str = ""  # [paths] opencode; пусто — which/известное место
    claude: str = ""  # [paths] claude; пусто — which/известное место
    opencode_db: str = ""  # [paths] opencode_db; пусто — XDG/известное место

    @property
    def telegram_enabled(self) -> bool:
        """Включён ли бот: есть токен."""
        return bool(self.tg_token)


class _Reader:
    """Типизированное чтение полей с накоплением ошибок."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def str_(self, data: dict, key: str, default: str = "", where: str = "") -> str:
        v = data.get(key, default)
        if v is None:
            return default
        if not isinstance(v, str):
            self.errors.append(_t("config.expect_str", where=where, field=key, got=type(v).__name__))
            return default
        return v

    def int_(self, data: dict, key: str, default: int, where: str = "", minimum: int | None = None) -> int:
        v = data.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int):
            self.errors.append(_t("config.expect_int", where=where, field=key, got=v))
            return default
        if minimum is not None and v < minimum:
            self.errors.append(_t("config.expect_min", where=where, field=key, minimum=minimum, got=v))
            return default
        return v

    def float_(self, data: dict, key: str, default: float, where: str = "") -> float:
        v = data.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.errors.append(_t("config.expect_num", where=where, field=key, got=v))
            return default
        if v < 0:
            self.errors.append(_t("config.expect_nonneg", where=where, field=key, got=v))
            return default
        return float(v)

    def strs(self, data: dict, key: str, default: tuple[str, ...] = (), where: str = "") -> tuple[str, ...]:
        v = data.get(key)
        if v is None:
            return default
        if isinstance(v, str):
            return tuple(s.strip() for s in v.split(",") if s.strip())
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            self.errors.append(_t("config.expect_strs", where=where, field=key))
            return default
        return tuple(v)

    def table(self, data: dict, key: str) -> dict:
        v = data.get(key)
        if v is None:
            return {}
        if not isinstance(v, dict):
            self.errors.append(_t("config.expect_table", field=key))
            return {}
        return v


def _resources(r: _Reader, raw: dict) -> dict[str, Resource]:
    out: dict[str, Resource] = {}
    for name, spec in raw.items():
        where = f"resources.{name}."
        if isinstance(spec, str):  # короткая форма: name = "/путь/к/lock"
            spec = {"lock": spec}
        if not isinstance(spec, dict):
            r.errors.append(_t("config.expect_resource", name=name))
            continue
        out[name] = Resource(
            name=name,
            capacity=r.int_(spec, "capacity", 1, where, minimum=1),
            lock=expand(r.str_(spec, "lock", "", where)),
        )
    return out


def _from_v1(data: dict) -> dict:
    """Поля .hub.toml v1 → форма v2 (без потерь того, что v2 понимает)."""
    out = {k: data[k] for k in ("name", "root", "worktrees", "work_branch", "push", "python", "rules",
                                 "allowed_paths", "hooks")
           if k in data}
    lock = data.get("test_lock")
    if isinstance(lock, str) and lock.strip():
        out["resources"] = {"test_lock": {"lock": lock}}
        out["test_resource"] = "test_lock"
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    budget = {}
    if "budget_go" in defaults:
        budget["go"] = defaults["budget_go"]
    if "budget_usd" in defaults:
        budget["usd"] = defaults["budget_usd"]
    if budget:
        out["budget"] = budget
    timeouts = {k: data[k] for k in ("idle_s", "retry_max", "retry_pause_s") if k in data}
    if timeouts:
        out["timeouts"] = timeouts
    return out


def parse_project(data: dict, base_dir: str | Path, source: str = "") -> ProjectConfig:
    """dict из TOML → ProjectConfig. Все проблемы разом — в ConfigError."""
    r = _Reader()
    version = data.get("schema_version", 1)
    if version == 1:
        data = _from_v1(data)
    elif version != SCHEMA_VERSION:
        raise ConfigError(source or "<dict>", [_t("config.bad_version", version=version)])

    name = r.str_(data, "name").strip()
    if not name:
        r.errors.append(_t("config.need_name"))
    root = expand(r.str_(data, "root").strip()) or str(Path(base_dir))
    resources = _resources(r, r.table(data, "resources"))
    test_resource = r.str_(data, "test_resource").strip()
    if test_resource and test_resource not in resources:
        r.errors.append(_t("config.no_test_resource", name=test_resource))
    hooks = r.table(data, "hooks")
    models = r.table(data, "models")
    budget = r.table(data, "budget")
    secrets = r.table(data, "secrets")
    timeouts = r.table(data, "timeouts")
    retry_max = r.int_(timeouts, "retry_max", 3, "timeouts.", minimum=0)
    extra_excl = r.strs(secrets, "exclude", (), "secrets.")
    branch_prefix = r.str_(data, "branch_prefix", "ahub/")
    if branch_prefix and not branch_prefix.endswith("/"):
        branch_prefix += "/"

    cfg = ProjectConfig(
        name=name,
        root=root,
        source=source,
        worktrees=expand(r.str_(data, "worktrees").strip()),
        work_branch=r.str_(data, "work_branch", "main").strip() or "main",
        branch_prefix=branch_prefix,
        push=r.str_(data, "push").strip(),
        python=expand(r.str_(data, "python").strip()),
        rules=expand(r.str_(data, "rules").strip()),
        allowed_paths=r.strs(data, "allowed_paths"),
        max_parallel=r.int_(data, "max_parallel", 2, minimum=1),
        resources=resources,
        test_resource=test_resource,
        hooks=Hooks(
            task_setup=r.str_(hooks, "task_setup", "", "hooks."),
            task_cleanup=r.str_(hooks, "task_cleanup", "", "hooks."),
        ),
        models_deny=r.strs(models, "deny", (), "models."),
        budget_go=r.float_(budget, "go", 1.5, "budget."),
        budget_usd=r.float_(budget, "usd", 0.0, "budget."),
        secret_excludes=tuple(dict.fromkeys(DEFAULT_SECRET_EXCLUDES + extra_excl)),
        timeouts=Timeouts(
            idle_s=r.int_(timeouts, "idle_s", 900, "timeouts.", minimum=0),
            retry_max=min(retry_max, 10),
            retry_pause_s=r.float_(timeouts, "retry_pause_s", 120.0, "timeouts."),
        ),
    )
    if r.errors:
        raise ConfigError(source or "<dict>", r.errors)
    return cfg


def find_project_file(start: str | Path) -> Path | None:
    """Ближайший .hub.toml от start вверх."""
    cur = Path(start).resolve()
    if cur.is_file():
        cur = cur.parent
    for cand in (cur, *cur.parents):
        f = cand / PROJECT_FILE
        if f.is_file():
            return f
    return None


def load_project_file(path: str | Path) -> ProjectConfig:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(p), [_t("config.bad_toml", err=e)]) from e
    return parse_project(data, p.parent, str(p))


def load_project(start: str | Path) -> ProjectConfig:
    """Проект по каталогу (ищет .hub.toml вверх). Нет файла — FileNotFoundError."""
    f = find_project_file(start)
    if f is None:
        raise FileNotFoundError(_t("config.no_project_file", file=PROJECT_FILE, start=start))
    return load_project_file(f)


def check_project(cfg: ProjectConfig) -> list[str]:
    """Проблемы, видимые только на диске: нет корня, python, rules, каталога копий."""
    out: list[str] = []
    root = Path(cfg.root)
    if not root.is_dir():
        out.append(_t("config.bad_root", root=cfg.root))
    if cfg.python and not os.access(cfg.python, os.X_OK):
        out.append(_t("config.bad_python", path=cfg.python))
    rp = cfg.rules_path()
    if rp is not None and not rp.is_file():
        out.append(_t("config.bad_rules", path=rp))
    if cfg.worktrees:
        wt = Path(cfg.worktrees)
        if not wt.is_dir() and not wt.parent.is_dir():
            out.append(_t("config.bad_worktrees", path=cfg.worktrees))
    return out


def _legacy_global_path() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    base = Path(raw) if raw else Path.home() / ".config"
    return base / "agent-hub" / "config.toml"


def _parse_hub_data(data: dict, source: str) -> HubConfig:
    """dict из TOML → HubConfig. Все проблемы разом — в ConfigError."""
    r = _Reader()
    projects = r.strs(data, "projects")
    tg = r.table(data, "telegram")
    token = r.str_(tg, "token", "", "telegram.")
    chat_id: int | None = None
    if "chat_id" in tg:
        v = tg["chat_id"]
        if isinstance(v, bool) or not isinstance(v, int):
            r.errors.append(_t("config.bad_chat_id", got=v))
        else:
            chat_id = v
    proxy = r.str_(tg, "proxy", "", "telegram.").strip()
    if proxy and not proxy.startswith(("http://", "https://")):
        r.errors.append(_t("config.bad_proxy", got=proxy))
    usage = r.table(data, "usage")
    go_limit: float | None = None
    if "go_month_limit" in usage:
        v = usage["go_month_limit"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            r.errors.append(_t("config.bad_go_type", got=v))
        elif v <= 0:
            r.errors.append(_t("config.bad_go_value", got=v))
        else:
            go_limit = float(v)
    env_token = os.environ.get("AHUB_TG_TOKEN")
    if env_token is not None and env_token.strip():
        token = env_token.strip()
    env_chat = os.environ.get("AHUB_TG_CHAT")
    if env_chat is not None and env_chat.strip():
        try:
            chat_id = int(env_chat.strip())
        except ValueError:
            r.errors.append(_t("config.bad_env_chat", got=env_chat))
    pth = r.table(data, "paths")
    opencode = expand(r.str_(pth, "opencode", "", "paths.").strip())
    claude = expand(r.str_(pth, "claude", "", "paths.").strip())
    opencode_db = expand(r.str_(pth, "opencode_db", "", "paths.").strip())
    raw = data.get("lang", "")
    norm = raw.strip().lower() if isinstance(raw, str) else ""
    if isinstance(raw, str) and norm not in ("", "en", "ru"):
        r.errors.append(_t("config.bad_lang_value", got=raw))
        norm = ""
    elif not isinstance(raw, str) and "lang" in data:
        r.errors.append(_t("config.bad_lang_type", got=type(raw).__name__))
        norm = ""
    raw_lang = norm
    if r.errors:
        raise ConfigError(source or "<dict>", r.errors)
    return HubConfig(
        projects=tuple(expand(x) for x in projects),
        source=source,
        lang=raw_lang,
        tg_token=token.strip(),
        tg_chat_id=chat_id,
        tg_proxy=proxy.strip(),
        go_month_limit=go_limit,
        opencode=opencode,
        claude=claude,
        opencode_db=opencode_db,
    )


def load_hub(path: str | Path | None = None) -> HubConfig:
    """Глобальный конфиг. Нет своего — список проектов из конфига v1. Нет ничего — пустой.

    AHUB_TG_TOKEN и AHUB_TG_CHAT перекрывают файл (и работают без файла).
    """
    cands = [Path(path)] if path is not None else [paths.global_config_path(), _legacy_global_path()]
    for p in cands:
        if not p.is_file():
            continue
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(str(p), [_t("config.bad_toml", err=e)]) from e
        return _parse_hub_data(data, str(p))
    return _parse_hub_data({}, "")


def load_projects(hub: HubConfig | None = None) -> tuple[list[ProjectConfig], list[str]]:
    """Все проекты хаба и проблемы загрузки (не глотаются — их покажет вызывающий)."""
    hub = hub if hub is not None else load_hub()
    out: list[ProjectConfig] = []
    errors: list[str] = []
    for entry in hub.projects:
        try:
            p = Path(entry)
            cfg = load_project_file(p) if p.is_file() else load_project_file(p / PROJECT_FILE)
        except FileNotFoundError:
            errors.append(_t("config.entry_no_file", entry=entry, file=PROJECT_FILE))
            continue
        except ConfigError as e:
            errors.append(str(e))
            continue
        except OSError as e:
            errors.append(f"{entry}: {e}")
            continue
        if any(c.name == cfg.name for c in out):
            errors.append(_t("config.entry_dup", entry=entry, name=cfg.name))
            continue
        out.append(cfg)
    return out, errors


def _inside(path: Path, base: str) -> bool:
    if not base:
        return False
    try:
        path.relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def project_for(path: str | Path, projects: list[ProjectConfig]) -> ProjectConfig | None:
    """Проект, к которому относится каталог: внутри корня или внутри каталога копий задач.

    Самый глубокий корень выигрывает (проект внутри проекта).
    """
    p = Path(path).resolve()
    best: tuple[int, ProjectConfig] | None = None
    for cfg in projects:
        for base in (cfg.root, cfg.worktrees):
            if _inside(p, base):
                depth = len(Path(base).resolve().parts)
                if best is None or depth > best[0]:
                    best = (depth, cfg)
    return best[1] if best else None
