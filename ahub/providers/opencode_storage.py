"""Durable, private routing for opt-in OpenCode SQLite shards."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from ahub import paths

_DIR = "opencode-shards"
_VERSION = 1
_LOCK_WAIT_S = 30.0


def _root() -> Path:
    root = paths.data_dir() / _DIR
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _metadata_path(key: str) -> Path:
    return paths.data_dir() / _DIR / f"{_safe_key(key)}.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid OpenCode routing metadata: {path}") from exc
    if not isinstance(data, dict) or type(data.get("version")) is not int or data.get("version") != _VERSION:
        raise ValueError(f"Invalid OpenCode routing metadata: {path}")
    if path.parent.name != "tasks":
        expected = path.with_suffix(".db").absolute()
        db = data.get("db")
        if (not isinstance(db, str) or not Path(db).is_absolute()
                or Path(db).resolve() != expected or path.is_symlink()
                or expected.resolve().parent != (paths.data_dir() / _DIR).resolve()
                or not isinstance(data.get("cwd"), str)
                or not isinstance(data.get("sessions"), list)
                or any(not isinstance(s, str) for s in data["sessions"])
                or not isinstance(data.get("key"), str)
                or _metadata_path(data["key"]).name != path.name
                or ("started_after_ms" in data and type(data["started_after_ms"]) is not int)):
            raise ValueError(f"Invalid OpenCode routing metadata: {path}")
    return data


def _write(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


@contextmanager
def _file_lock(path: Path, *, deadline=None, should_stop=None) -> Iterator[None]:
    """A bounded wait; unrelated route and database locks remain independent."""
    import fcntl

    limit = min(deadline, time.monotonic() + _LOCK_WAIT_S) if deadline is not None else time.monotonic() + _LOCK_WAIT_S
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        while True:
            if should_stop is not None and should_stop():
                raise InterruptedError("OpenCode database lock wait cancelled")
            if time.monotonic() >= limit:
                raise TimeoutError("OpenCode database lock wait timed out")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, max(0, limit - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _task_path(key: str) -> Path:
    return paths.data_dir() / _DIR / "tasks" / f"{_safe_key(key)}.json"


def task_enrolled(key: str) -> bool:
    if not key:
        return False
    data = _read(_task_path(key))
    if data is not None and data.get("task") != key:
        raise ValueError("Invalid OpenCode task enrollment")
    return data is not None


def route_key(spec) -> str | None:
    """Enrollment belongs to the task, including its future review routes."""
    key = spec.env.get("AHUB_OPENCODE_ROUTE", "").strip()
    if not key:
        return None
    # Validate an existing route even when the opt-in flag is present.
    existing = _read(_metadata_path(key))
    enrolled = task_enrolled(spec.env.get("AHUB_OPENCODE_TASK", "").strip())
    if spec.env.get("AHUB_OPENCODE_ISOLATE") == "1" or existing is not None or enrolled:
        return key
    return None


def register(spec, started_ms: int, *, deadline=None, should_stop=None) -> Path | None:
    key = route_key(spec)
    if key is None:
        return None
    root = _root()
    task = spec.env.get("AHUB_OPENCODE_TASK", "").strip()
    if task:
        directory = root / "tasks"
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        task_path = _task_path(task)
        with _file_lock(task_path.with_suffix(".lock"), deadline=deadline, should_stop=should_stop):
            current_task = _read(task_path)
            if current_task is not None and current_task.get("task") != task:
                raise ValueError("Invalid OpenCode task enrollment")
            if current_task is None:
                _write(task_path, {"version": _VERSION, "task": task})
    path = _metadata_path(key)
    db = path.with_suffix(".db")
    if db.is_symlink() or db.resolve().parent != root.resolve():
        raise ValueError("Invalid OpenCode shard database path")
    with _file_lock(path.with_suffix(".metadata.lock"), deadline=deadline, should_stop=should_stop):
        current = _read(path) or {"version": _VERSION, "key": key,
            "cwd": str(Path(spec.cwd).resolve()), "db": str(path.with_suffix(".db").resolve()), "sessions": []}
        current["started_after_ms"] = min(int(current.get("started_after_ms", started_ms)), int(started_ms))
        _write(path, current)
        return Path(current["db"])


def has_route(key: str) -> bool:
    return bool(key and _read(_metadata_path(key)))


def bind_session(sid: str, db_path: str | Path, spec=None) -> None:
    if not sid:
        return
    db = Path(db_path).resolve()
    # Only managed shards need metadata. Explicit external DBs remain single-DB callers.
    root = paths.data_dir() / _DIR
    if db.parent != root.resolve():
        return
    path = db.with_suffix(".json")
    with _file_lock(path.with_suffix(".metadata.lock")):
        data = _read(path)
        if data is None:
            raise ValueError("OpenCode shard has no routing metadata")
        if sid not in data["sessions"]:
            data["sessions"].append(sid)
            _write(path, data)


def routes() -> list[dict]:
    root = paths.data_dir() / _DIR
    if not root.is_dir():
        return []
    return [data for path in sorted(root.glob("*.json")) if (data := _read(path)) is not None]


def session_db(sid: str, *, strict: bool = False) -> Path | None:
    found = {r["db"] for r in routes() if sid in r["sessions"]}
    if len(found) > 1 and strict:
        raise ValueError("Ambiguous OpenCode session routing")
    return Path(next(iter(found))) if len(found) == 1 else None


def db_for_spec(spec) -> Path | None:
    key = route_key(spec)
    data = _read(_metadata_path(key)) if key is not None else None
    return Path(data["db"]) if data is not None else None


def _managed_db(db: Path) -> bool:
    root = (paths.data_dir() / _DIR).resolve()
    return db.parent == root and len(db.stem) == 64 and all(c in "0123456789abcdef" for c in db.stem)


@contextmanager
def db_lock(db_path: str | Path, namespace: str = "", *, deadline=None, should_stop=None) -> Iterator[None]:
    """All routes/resumes of one shard share a lock; legacy sessions lock separately."""
    db = Path(db_path).resolve()
    if _managed_db(db):
        path = db.with_suffix(".lock")
    else:
        root = paths.data_dir() / "opencode-locks"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = _safe_key(str(db) + "\0" + namespace)
        path = root / f"{name}.lock"
    with _file_lock(path, deadline=deadline, should_stop=should_stop):
        yield


@contextmanager
def lock(spec, *, deadline=None, should_stop=None) -> Iterator[None]:
    db = register(spec, int(time.time() * 1000), deadline=deadline, should_stop=should_stop)
    if db is None:
        yield
    else:
        with db_lock(db, deadline=deadline, should_stop=should_stop):
            yield


def no_lock():
    return nullcontext()
