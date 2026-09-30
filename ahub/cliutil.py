"""Общее для подкоманд: вывод текст/JSON, выбор проекта."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ahub import config


class CliError(RuntimeError):
    """Ожидаемый отказ команды: печатается одной строкой, код 2."""


def emit(args, data: Any, text: str) -> None:
    """--json → data как JSON, иначе text."""
    if getattr(args, "json", False):
        json.dump(data, sys.stdout, ensure_ascii=False, separators=(",", ":"), default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")


def add_project_arg(parser) -> None:
    parser.add_argument("--project", "-P", default=None,
                        help="имя проекта или путь; по умолчанию — проект текущего каталога")


def resolve_project(args, cwd: str | Path | None = None) -> config.ProjectConfig:
    """Проект по --project (имя из конфига хаба или путь) или по текущему каталогу."""
    want = getattr(args, "project", None)
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    if want:
        p = Path(config.expand(want))
        if p.exists():
            return config.load_project(p)
        projects, _errors = config.load_projects()
        for cfg in projects:
            if cfg.name == want:
                return cfg
        raise CliError(f"нет проекта {want!r}")
    try:
        return config.load_project(cwd)
    except FileNotFoundError:
        projects, _errors = config.load_projects()
        cfg = config.project_for(cwd, projects)
        if cfg is None:
            raise CliError(f"каталог {cwd} не относится ни к одному проекту (нет {config.PROJECT_FILE})")
        return cfg
