"""hub cost: деньги go/usd с группировкой."""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from hub import time as ht
from hub.read import opencode as oc
from hub.store import Store

DEFAULT_OPENCODB = Path.home() / ".local/share/opencode/opencode.db"


def cmd_cost(args) -> int:
    now = ht.now_ms()
    try:
        since = ht.parse_since(args.since, now) if args.since else 0
    except ValueError as e:
        print(f"непонятный --since: {e}", file=sys.stderr)
        return 2
    db = Path(args.opencode_db) if getattr(args, "opencode_db", None) else DEFAULT_OPENCODB
    sessions = oc.sessions(str(db), since) if db.exists() else []
    by = args.by
    role_of: dict[str, str] = {}
    try:
        store = Store()
        for row in store.list_sessions():
            role_of.setdefault(row["external_id"], row["role"])
    except OSError:
        pass
    groups: dict[str, list] = defaultdict(lambda: [0.0, 0.0])  # go, usd
    for s in sessions:
        if by == "task":
            key = Path(s.directory).name or s.directory
        elif by == "model":
            key = s.model or "?"
        elif by == "role":
            key = role_of.get(s.id, "-")
        else:  # day
            local = datetime.fromtimestamp(s.started_ms / 1000,
                                           tz=timezone.utc).astimezone(ht.TZ)
            key = local.strftime("%Y-%m-%d")
        if s.provider == "opencode-go":
            groups[key][0] += s.cost
        else:
            groups[key][1] += s.cost
    total_go = total_usd = 0.0
    for key in sorted(groups):
        go, usd = groups[key]
        total_go += go
        total_usd += usd
        print(f"{key}: go ${go:.3f} + usd ${usd:.3f}")
    print(f"Итого: go ${total_go:.3f} + usd ${total_usd:.3f}")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("cost", help="деньги go/usd")
    p.add_argument("--since", default=None, help="напр. «сегодня 20:00», «2ч»")
    p.add_argument("--by", default="task", choices=["task", "model", "role", "day"])
    p.set_defaults(func=cmd_cost)
