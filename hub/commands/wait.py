"""hub wait: блокировка до события, печать дельты."""

from __future__ import annotations

import re
import sys

from hub import time as ht
from hub.read import events as ev
from hub.store import Store

DEFAULT_TIMEOUT = "4ч"
_REL_LIKE = re.compile(r"^\s*\d+\s*[a-zа-яё]+\s*$", re.IGNORECASE)


def timeout_s(timeout_text: str, now_ms: int) -> float:
    """Длительность (--timeout) в секундах через parse_since.

    Относительные («4ч», «1м») — now минус parsed; абсолютные будущие —
    parsed минус now (прошлое — 0).
    """
    parsed = ht.parse_since(timeout_text, now_ms)
    if _REL_LIKE.match(timeout_text):
        return max(0.0, (now_ms - parsed) / 1000.0)
    return max(0.0, (parsed - now_ms) / 1000.0)


def cmd_wait(args) -> int:
    now = ht.now_ms()
    try:
        t_s = timeout_s(args.timeout, now)
    except ValueError as e:
        print(f"непонятный --timeout: {e}", file=sys.stderr)
        return 2
    store = Store()
    task_id = getattr(args, "task", None)
    # Старт — с текущего максимума, ждём только новые.
    known = store.events_since(0)
    after = max((r["id"] for r in known), default=0)
    if task_id is not None:
        known_t = [r for r in known if r.get("task_id") == task_id]
        after = max((r["id"] for r in known_t), default=after)
        # Если задача указана, а событий по ней нет — ждать от общего максимума
        # нельзя (чужие события разбудят раньше); берём общий максимум.
    found = ev.wait(store, after, task_id, t_s, float(args.poll))
    if not found:
        return 2
    for e in found:
        print(f"{e['id']} {e['kind']} {e['task_id']} {e.get('payload_json', '{}')}")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("wait", help="ждать события (для run_in_background)")
    p.add_argument("--task", default=None, help="только события задачи")
    p.add_argument("--timeout", default=DEFAULT_TIMEOUT, help="«4ч», «1м» или время")
    p.add_argument("--poll", type=float, default=0.5, help="опрос store, сек")
    p.set_defaults(func=cmd_wait)
