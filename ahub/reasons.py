"""State reasons as data, not as text: the hub stores a code + params, every reader renders it in its own
language.

`{"code": "wait_accept", "task": "T53", "state": "reviewing"}` in the DB becomes "waiting for T53 to be
accepted (reviewing)" in English and "ждёт принятия T53 (ревью)" in Russian. A reason that is not a code blob —
an old row, or a human note from `ahub reject --reason` — is shown as it is.
"""

from __future__ import annotations

import json
import string
from typing import Any

from ahub.i18n import template

PARAM_LIMIT = 400  # a param longer than this is detail, not a reason: the blob stays readable


def dump(code: str, **params: Any) -> str:
    """The stored form of a reason: a code + params (JSON). Empty code — no reason.

    An empty param is kept (only None is dropped): the template of the code always gets what it asks for.
    A param a worker filled in (an error tail, a summary) is capped, so one task cannot bloat the column.
    """
    if not code:
        return ""
    body: dict[str, Any] = {"code": code}
    body.update({k: _cap(v) for k, v in params.items() if v is not None})
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)


def part(code: str, **params: Any) -> dict:
    """A sub-reason (one gate problem inside another reason) — rendered by text()."""
    return {k: v for k, v in (("code", code), *params.items()) if v is not None}


def _cap(value: Any) -> Any:
    """A param as it is stored: strings clipped, a sub-reason or a list of them capped the same way."""
    if isinstance(value, str):
        return value[:PARAM_LIMIT]
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap(v) for v in value]
    return value


def load(stored: str) -> dict:
    """The stored reason as a dict; {} — plain text (an old row, a human note)."""
    s = (stored or "").strip()
    if not s.startswith("{"):
        return {}
    try:
        data = json.loads(s)
    except ValueError:
        return {}
    return data if isinstance(data, dict) and data.get("code") else {}


def text(stored: str) -> str:
    """The reason in the current language. Unknown code or plain text — shown as it is."""
    data = load(stored)
    if not data:
        return stored or ""
    return _render(data) or stored


def _render(data: dict) -> str:
    """The sentence for one stored reason. A param the row does not carry is empty, never an error:
    reading a task must not depend on which params the writer happened to pass."""
    code = str(data.get("code") or "")
    tpl = template("reason." + code) if code else None
    if tpl is None:
        return code
    fields = {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(tpl) if f}
    try:
        return tpl.format(**{f: _value(f, data.get(f, "")) for f in fields})
    except (IndexError, KeyError, ValueError):  # a broken template or an odd param type — show the code
        return code


def _words(key: str) -> Any:
    """A param that holds a machine name is rendered as a readable word (state → its word in the language)."""
    if key == "state":
        from ahub.archive import STATE_WORDS  # imported here: archive writes reasons, not the other way round

        return STATE_WORDS
    return None


def _value(key: str, value: Any) -> Any:
    if isinstance(value, dict):  # a single sub-reason
        return _render(value)
    if isinstance(value, list):
        return "; ".join(_render(v) if isinstance(v, dict) else str(v) for v in value)
    words = _words(key)
    if words is not None and isinstance(value, str):
        return words.get(value, value)
    return value
