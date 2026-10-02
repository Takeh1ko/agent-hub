"""Упаковка 3.0.0: Telegram как extra, отказ на Windows, скрипт только ahub."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_bot_run_without_aiogram(monkeypatch, capsys):
    """Без extra telegram: код 2 и подсказка одной строкой."""
    import importlib.util

    from ahub import cli

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "aiogram" else real(name, *a, **k))
    assert cli.main(["bot", "run"]) == 2
    err = capsys.readouterr().err
    assert "Telegram не установлен" in err and "ahub[telegram]" in err
    assert err.strip().count("\n") == 0


def test_main_refuses_windows(monkeypatch, capsys):
    """На win32 main() отказывает до разбора команд: код 2, одна строка."""
    from ahub import cli

    monkeypatch.setattr(sys, "platform", "win32")
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert "Windows не поддерживается" in err and "WSL2" in err
    assert err.strip().count("\n") == 0


def test_tg_core_launcher_import_without_aiogram():
    """import ahub.tg.core/launcher работает без aiogram (подпроцесс с запретом импорта)."""
    code = ("import sys; sys.modules['aiogram'] = None; "
            "import ahub.tg.core, ahub.tg.launcher; "
            "print('ok')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "ok"


def test_pyproject_layout():
    """Пакет ahub 3.0.0: скрипт только ahub, aiogram только в extra."""
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["name"] == "ahub" and proj["version"] == "3.0.0"
    assert proj["requires-python"] == ">=3.11"
    assert list(proj["scripts"]) == ["ahub"]
    assert "aiogram" not in " ".join(proj["dependencies"])
    assert "aiohttp-socks" not in " ".join(proj["dependencies"])
    extra = {k.lower(): " ".join(v) for k, v in proj["optional-dependencies"].items()}
    assert "aiogram" in extra.get("telegram", "")
