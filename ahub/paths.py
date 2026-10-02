"""Где хаб хранит данные, логи и конфиг. Всё читается из окружения при вызове (тесты подменяют HOME).

Имена отличаются от v1 (AGENT_HUB_HOME, ~/.local/share/agent-hub), чтобы v2 при стройке не задевал v1.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "AHUB_HOME"  # всё состояние хаба в одном каталоге (тесты, изоляция работников)


def _xdg(var: str, default: str) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else Path.home() / default


def data_dir() -> Path:
    """Хранилище хаба: база, журналы сессий."""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw)
    return _xdg("XDG_DATA_HOME", ".local/share") / "ahub"


def state_dir() -> Path:
    """Логи и файлы состояния (позиция потока пробуждения и т.п.)."""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw) / "state"
    return _xdg("XDG_STATE_HOME", ".local/state") / "ahub"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "ahub"


def db_path() -> Path:
    return data_dir() / "ahub.db"


def log_dir() -> Path:
    return state_dir() / "logs"


def global_config_path() -> Path:
    return config_dir() / "config.toml"


def service_pid_path() -> Path:
    """Pid-файл фонового `service start` (запуск без службы ОС)."""
    return data_dir() / "service.pid"
