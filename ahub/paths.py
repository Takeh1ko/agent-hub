"""Where the hub keeps data, logs, and config. All read from the env at call time (tests fake HOME).

Names differ from v1 (AGENT_HUB_HOME, ~/.local/share/agent-hub) so v2 never touches v1 while under construction.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_HOME = "AHUB_HOME"  # all hub state in one dir (tests, worker isolation)


def _xdg(var: str, default: str) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else Path.home() / default


def data_dir() -> Path:
    """Hub storage: database, session journals."""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw)
    return _xdg("XDG_DATA_HOME", ".local/share") / "ahub"


def state_dir() -> Path:
    """Logs and state files (wakeup stream position etc.)."""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw) / "state"
    return _xdg("XDG_STATE_HOME", ".local/state") / "ahub"


def config_dir() -> Path:
    """Global config. Under AHUB_HOME it lives there too: an isolated instance never reads the real config."""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw) / "config"
    return _xdg("XDG_CONFIG_HOME", ".config") / "ahub"


def db_path() -> Path:
    return data_dir() / "ahub.db"


def log_dir() -> Path:
    return state_dir() / "logs"


def global_config_path() -> Path:
    return config_dir() / "config.toml"


def service_pid_path() -> Path:
    """Pid file of background `service start` (launch without an OS service)."""
    return data_dir() / "service.pid"


def _safe_name(name: str) -> str:
    safe = re.sub(r"[^\w\-.]", "_", name)
    while ".." in safe:
        safe = safe.replace("..", "_")
    return safe or "_"


def accept_lock_path(project_name: str) -> Path:
    """Per-project accept lock file (serializes merges and acceptance within a project)."""
    return data_dir() / f"accept-{_safe_name(project_name)}.lock"


def global_prompts_dir() -> Path:
    """Global prompts directory: ~/.config/ahub/prompts (or AHUB_HOME/config/prompts)."""
    return config_dir() / "prompts"


def project_prompts_dir(project_root: str | Path) -> Path:
    """Project prompts directory: <repo>/.hub/prompts."""
    return Path(project_root) / ".hub" / "prompts"


def local_prompts_dir(project_name: str) -> Path:
    """Local project prompts directory: ~/.config/ahub/projects/<project-name>/prompts."""
    return config_dir() / "projects" / _safe_name(project_name) / "prompts"
