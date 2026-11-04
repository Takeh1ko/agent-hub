"""Durable routing for opt-in OpenCode SQLite shards.

The routing index is hub-owned data.  It contains only routing facts (task,
role, work directory and database path), never credentials or copied
OpenCode data.  The feature is deliberately driven by immutable RunSpec
environment values so the cached provider instance remains stateless.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from ahub import paths

_DIR = "opencode-shards"
_VERSION = 1


def _root() -> Path:
    root = paths.data_dir() / _DIR
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _metadata_path(key: str) -> Path:
    return paths.data_dir() / _DIR / f"{_safe_key(key)}.json"


def _db_path(meta_path: Path) -> Path:
    return meta_path.with_suffix(".db")


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == _VERSION else None


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def route_key(spec) -> str | None:
    """Return the caller-provided stable route identity, if enrolled."""
    key = spec.env.get("AHUB_OPENCODE_ROUTE", "").strip()
    if not key:
        return None
    if spec.env.get("AHUB_OPENCODE_ISOLATE") == "1" or _metadata_path(key).is_file():
        return key
    return None


def register(spec, started_ms: int) -> Path | None:
    """Create/update route metadata before OpenCode is spawned."""
    key = route_key(spec)
    if key is None:
        return None
    _root()
    path = _metadata_path(key)
    current = _read(path) or {
        "version": _VERSION,
        "key": key,
        "cwd": str(Path(spec.cwd).resolve()),
        "db": str(_db_path(path).resolve()),
        "sessions": [],
    }
    current["started_after_ms"] = min(int(current.get("started_after_ms", started_ms)), int(started_ms))
    _write(path, current)
    return Path(current["db"])


def has_route(key: str) -> bool:
    return bool(key and _read(_metadata_path(key)))


def bind_session(sid: str, db_path: str | Path, spec=None) -> None:
    """Durably associate a SESSION id with its shard after the event arrives."""
    if not sid:
        return
    root = _root()
    db = str(Path(db_path).resolve())
    for path in sorted(root.glob("*.json")):
        data = _read(path)
        if not data or str(data.get("db", "")) != db:
            continue
        ids = [str(x) for x in data.get("sessions", []) if str(x)]
        if sid not in ids:
            data["sessions"] = ids + [sid]
            _write(path, data)
        return


def routes() -> list[dict]:
    out: list[dict] = []
    root = paths.data_dir() / _DIR
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        data = _read(path)
        if data and isinstance(data.get("db"), str) and isinstance(data.get("cwd"), str):
            out.append(data)
    return out


def session_db(sid: str) -> Path | None:
    """Find a mapped shard for a session, returning None when absent/ambiguous."""
    found = {str(r["db"]) for r in routes() if sid in {str(x) for x in r.get("sessions", [])}}
    if len(found) != 1:
        return Path(next(iter(found))) if len(found) == 1 else None
    return Path(next(iter(found)))


def db_for_spec(spec) -> Path | None:
    """Resolve a run's route without changing process-global provider state."""
    key = route_key(spec)
    if key is None:
        return None
    path = _metadata_path(key)
    data = _read(path)
    if data is None:
        return None
    db = data.get("db")
    return Path(db) if isinstance(db, str) and Path(db).is_absolute() else None


@contextmanager
def lock(spec) -> Iterator[None]:
    """Serialize only runs sharing one shard namespace."""
    import time

    db = register(spec, int(time.time() * 1000))
    if db is None:
        yield
        return
    lock_path = db.with_suffix(".lock")
    lock_path.touch(mode=0o600, exist_ok=True)
    try:
        lock_path.chmod(0o600)
    except OSError:
        pass
    try:
        import fcntl
    except ImportError:
        yield
        return
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def no_lock():
    return nullcontext()


@contextmanager
def db_lock(db_path: str | Path, namespace: str) -> Iterator[None]:
    """Lock one legacy session namespace without serializing the whole database."""
    root = paths.data_dir() / "opencode-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(f"{Path(db_path).resolve()}\0{namespace}".encode()).hexdigest()
    path = root / f"{name}.lock"
    path.touch(mode=0o600, exist_ok=True)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    try:
        import fcntl
    except ImportError:
        yield
        return
    with path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
