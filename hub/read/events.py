"""События: дельта для hub wait (без опроса со стороны Claude)."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

from hub import time as ht

if TYPE_CHECKING:
    from hub.store import Store


def next_events(store: Store, after_id: int, task_id: str | None = None) -> list[dict]:
    """Строки event с id > after_id (по task_id при заданном), порядок по id."""
    rows = store.events_since(after_id)
    if task_id is not None:
        rows = [r for r in rows if r.get("task_id") == task_id]
    return rows


def _touch_listen(store: Store) -> None:
    """Отметить, что Claude на связи (читает бот H05). Best-effort."""
    try:
        con = sqlite3.connect(str(store.path))
    except sqlite3.Error:
        return
    try:
        con.execute(
            "INSERT INTO meta(key, value) VALUES ('claude_listen_ts', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(ht.now_ms()),),
        )
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def wait(
    store: Store,
    after_id: int,
    task_id: str | None = None,
    timeout_s: float = 14400,
    poll_s: float = 0.5,
    clock=time.monotonic,
) -> list[dict]:
    """Ждать первых событий, пустой список по таймауту.

    clock — монотонные часы (параметром для тестов).
    """
    deadline = clock() + max(0.0, float(timeout_s))
    # Пол шага опроса — не молотить БД при --poll 0/малом.
    step = max(0.05, float(poll_s))
    while True:
        try:
            _touch_listen(store)
        except sqlite3.Error:
            pass
        found = next_events(store, after_id, task_id)
        if found:
            return found
        remaining = deadline - clock()
        if remaining <= 0:
            return []
        time.sleep(min(step, remaining))
