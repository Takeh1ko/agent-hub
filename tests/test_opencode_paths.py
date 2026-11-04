"""Where the opencode binaries and database are looked up: config [paths] → which/XDG → known location.

The config is read on every call (conftest fakes HOME); a broken config does not break the search.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ahub import paths
from ahub.providers import opencode as opmod
from ahub.providers import opencode_db as odb
from ahub.tg import launcher
from tests.conftest import write


def test_opencode_config_wins(tmp_path, monkeypatch):
    fake = tmp_path / "my-opencode"
    fake.write_text("#!/bin/sh\n")
    write(paths.global_config_path(), f'[paths]\nopencode = "{fake}"\n')
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode" if name == "opencode" else None)
    assert opmod.opencode_bin() == str(fake)


def test_opencode_config_expands_home(tmp_path, monkeypatch):
    write(paths.global_config_path(), '[paths]\nopencode = "$HOME/bin/opencode"\n')
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode" if name == "opencode" else None)
    assert opmod.opencode_bin() == str(tmp_path / "bin" / "opencode")


def test_opencode_which_then_known(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode" if name == "opencode" else None)
    assert opmod.opencode_bin() == "/usr/bin/opencode"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert opmod.opencode_bin() == str(Path.home() / ".opencode" / "bin" / "opencode")


def test_opencode_broken_config_falls_back(tmp_path, monkeypatch):
    write(paths.global_config_path(), "[paths]\nopencode = 123\n")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode" if name == "opencode" else None)
    assert opmod.opencode_bin() == "/usr/bin/opencode"


def test_claude_config_wins(tmp_path, monkeypatch):
    fake = tmp_path / "my-claude"
    fake.write_text("#!/bin/sh\n")
    write(paths.global_config_path(), f'[paths]\nclaude = "{fake}"\n')
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert launcher.claude_bin() == str(fake)


def test_claude_which_then_known_then_none(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert launcher.claude_bin() == "/usr/bin/claude"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    known = Path.home() / ".claude" / "local" / "claude"
    assert launcher.claude_bin() is None  # no known location
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text("#!/bin/sh\n")
    assert launcher.claude_bin() == str(known)


def test_claude_broken_config_falls_back(tmp_path, monkeypatch):
    write(paths.global_config_path(), "[paths]\nclaude = 123\n")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert launcher.claude_bin() == "/usr/bin/claude"


def test_db_config_wins(tmp_path, monkeypatch):
    fake = tmp_path / "custom.db"
    write(paths.global_config_path(), f'[paths]\nopencode_db = "{fake}"\n')
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert odb.default_db() == fake


def test_db_config_expands_home(tmp_path, monkeypatch):
    write(paths.global_config_path(), '[paths]\nopencode_db = "$HOME/data/opencode.db"\n')
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert odb.default_db() == tmp_path / "data" / "opencode.db"


def test_db_xdg_then_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert odb.default_db() == tmp_path / "data" / "opencode" / "opencode.db"
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert odb.default_db() == Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def test_db_broken_config_falls_back(tmp_path, monkeypatch):
    write(paths.global_config_path(), "[paths]\nopencode_db = 123\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert odb.default_db() == tmp_path / "data" / "opencode" / "opencode.db"


def test_explicit_db_beats_config_and_env(tmp_path, monkeypatch):
    """Explicit db_path is the single-DB contract; OPENCODE_DB never leaks into the hub env."""
    import os

    from ahub.providers.opencode import OpencodeProvider

    default_db = tmp_path / "default.db"
    explicit_db = tmp_path / "explicit.db"
    default_db.write_bytes(b"")
    explicit_db.write_bytes(b"")
    write(paths.global_config_path(), f'[paths]\nopencode_db = "{default_db}"\n')
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    before = dict(os.environ)
    assert odb.default_db() == default_db  # config wins over XDG
    assert OpencodeProvider(db_path=str(explicit_db)).db_path == str(explicit_db)
    assert dict(os.environ) == before and "OPENCODE_DB" not in os.environ


def test_private_shard_root_validated_names_and_modes(tmp_path):
    """Shard root is 0700 under hub data dir; metadata 0600; sha256 names; no traversal/secrets."""
    import json
    import stat as _stat

    from ahub.providers import opencode_storage as _storage
    from ahub.providers.base import RunSpec as _Spec

    def _spec(cwd: str, route: str) -> _Spec:
        return _Spec(prompt="x", cwd=cwd, model_id="m",
                     env={"AHUB_OPENCODE_ROUTE": route, "AHUB_OPENCODE_ISOLATE": "1"})

    db = _storage.register(_spec(str(tmp_path / "wt"), "task:1:role:executor:log:exec"), 1_700_000_000_000)
    root = paths.data_dir() / "opencode-shards"
    assert _stat.S_IMODE(root.stat().st_mode) == 0o700
    assert str(db).startswith(str(root)) and ".." not in str(db)
    assert len(db.stem) == 64 and all(c in "0123456789abcdef" for c in db.stem)
    meta = root / f"{db.stem}.json"
    assert _stat.S_IMODE(meta.stat().st_mode) == 0o600
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert set(data) >= {"version", "key", "cwd", "db", "sessions", "started_after_ms"}
    assert "sk-" not in json.dumps(data)
