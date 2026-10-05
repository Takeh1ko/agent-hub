"""Packaging: Telegram as an extra, refusal on Windows, only the ahub script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ahub import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_bot_run_without_aiogram(monkeypatch, capsys):
    """Without the telegram extra: exit code 2 and a one-line hint."""
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
    """On win32 main() refuses before parsing commands: code 2, one line."""
    from ahub import cli

    monkeypatch.setattr(sys, "platform", "win32")
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert "Windows не поддерживается" in err and "WSL2" in err
    assert err.strip().count("\n") == 0


def test_tg_core_launcher_import_without_aiogram():
    """import ahub.tg.core/launcher works without aiogram (subprocess that blocks the import)."""
    code = ("import sys; sys.modules['aiogram'] = None; "
            "import ahub.tg.core, ahub.tg.launcher; "
            "print('ok')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "ok"


def test_pyproject_layout():
    """ahub package: only the ahub script, aiogram only in an extra, version matches the code."""
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["name"] == "ahub" and proj["version"] == __version__
    assert proj["requires-python"] == ">=3.11"
    assert list(proj["scripts"]) == ["ahub"]
    assert "aiogram" not in " ".join(proj["dependencies"])
    assert "aiohttp-socks" not in " ".join(proj["dependencies"])
    extra = {k.lower(): " ".join(v) for k, v in proj["optional-dependencies"].items()}
    assert "aiogram" in extra.get("telegram", "")
