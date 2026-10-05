"""opencode provider: `opencode run --format json` + data from opencode.db (usage, pulse, session search).

stdout events (verified against live logs 2026-09-30), each carries sessionID:
  step_start / step_finish (part.tokens, part.cost) / tool_use (finished only: state.status completed|error) /
  text (part.text) / reasoning / error (error: string or {name, data: {message, statusCode, isRetryable}}).
The running tool is not visible in the stream — opencode.db provides it (session_state).
Errors (ported v1 H13 rules + real forms): 5xx, isRetryable, "Unexpected server error", network ones — transient;
401/403 — no access; "quota/limit exceeded/insufficient" — quota; the rest — model error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from ahub import log as hublog
from ahub.i18n import t as _t
from ahub.providers.base import (
    Act,
    Activity,
    Cap,
    CatalogEntry,
    Health,
    Provider,
    RunSpec,
    SessionState,
    Usage,
    infer_plan,
    infer_vendor,
)

PROMPT_ARG_LIMIT = 60_000  # bytes; one Linux argument caps at 128 KB
CATALOG_TTL_S = 24 * 3600  # hub cache for `opencode models <provider> --verbose`
_log = hublog.get("opencode")

TRANSIENT_MARKERS = ("unexpected server error", "cannot connect to api", "unable to connect", "econnrefused",
                     "etimedout", "econnreset", "socket hang up", "temporarily overloaded", "service unavailable",
                     "bad gateway", "gateway timeout", "fetch failed", "network error")
QUOTA_MARKERS = ("quota", "limit exceeded", "usage limit", "insufficient", "credit", "billing")
_STATUS = re.compile(r"\bstatus(?:code)?\D{0,3}(\d{3})\b", re.IGNORECASE)


def opencode_bin() -> str:
    """opencode binary: [paths].opencode → which → ~/.opencode/bin/opencode."""
    try:
        from ahub import config

        override = config.load_hub().opencode
        if override:
            return override
    except config.ConfigError:
        pass
    return shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")


def run_capture(cmd: list[str], timeout: int = 120, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run and collect output via a file: opencode drops the tail output to a pipe on exit."""
    full_env = {**os.environ, **env} if env else None
    with tempfile.TemporaryFile("w+", encoding="utf-8") as out:
        r = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, text=True, timeout=timeout, env=full_env)
        out.seek(0)
        return r.returncode, out.read(), r.stderr or ""


def _error_texts(err) -> list[str]:
    if isinstance(err, str):
        return [err]
    out: list[str] = []
    if isinstance(err, dict):
        for k in ("name", "message", "text", "details"):
            v = err.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        data = err.get("data")
        if isinstance(data, dict):
            out.extend(_error_texts(data))
    elif isinstance(err, list):
        for v in err:
            out.extend(_error_texts(v))
    return out


def classify_error(ev: dict) -> tuple[str, dict]:
    """{"type":"error"} event → (text, transient/quota/no_access flags)."""
    err = ev.get("error", ev.get("message", ev.get("text", "")))
    texts = _error_texts(err)
    for k in ("message", "text", "details"):
        if k != "error" and isinstance(ev.get(k), str):
            texts.append(ev[k])
    text = " | ".join(dict.fromkeys(t for t in texts if t))[:2000] or json.dumps(ev, ensure_ascii=False)[:500]
    low = text.lower()
    data = err.get("data") if isinstance(err, dict) else None
    status = data.get("statusCode") if isinstance(data, dict) else None
    if status is None:
        m = _STATUS.search(text)
        status = int(m.group(1)) if m else None
    retryable = bool(data.get("isRetryable")) if isinstance(data, dict) else False
    flags = {"transient": False, "quota": False, "no_access": False, "status": status}
    if status in (401, 403) or "unauthorized" in low or "forbidden" in low:
        flags["no_access"] = True
    elif any(m in low for m in QUOTA_MARKERS):
        flags["quota"] = True
    elif retryable or (isinstance(status, int) and (status >= 500 or status == 429)) \
            or any(m in low for m in TRANSIENT_MARKERS):
        flags["transient"] = True
    return text, flags


def prompt_arg(prompt: str, cwd: str) -> str:
    """Short prompt — as is; long one — to a file (unique name: parallel sessions don't clobber)."""
    if len(prompt.encode("utf-8")) <= PROMPT_ARG_LIMIT:
        return prompt
    d = Path(cwd) / ".ahub"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"prompt_{int(time.time() * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:8]}.md"
    path.write_text(prompt, encoding="utf-8")
    return (f"Your full task is in file {path}. Read it fully (it is long, read it in parts "
            f"to the end) and execute it.")


def _usage_of_part(part: dict) -> Usage | None:
    tok = part.get("tokens")
    if not isinstance(tok, dict):
        return None
    cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
    return Usage(tokens_in=tok.get("input"), tokens_out=tok.get("output"), tokens_reasoning=tok.get("reasoning"),
                 cache_read=cache.get("read"), cache_write=cache.get("write"),
                 context=(tok.get("input") or 0) + (cache.get("read") or 0) or None)


class OpencodeProvider(Provider):
    name = "opencode"
    stamped = True  # every event carries `timestamp`
    capabilities = frozenset({Cap.RESUME, Cap.STREAM, Cap.TOKENS, Cap.COST_MONEY, Cap.ACTIVE_TOOL, Cap.EXPORT,
                              Cap.CATALOG, Cap.HEALTH, Cap.FIND_SESSION})

    def __init__(self, db_path: str | None = None, binary: str | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.db_path = db_path  # None — opencode_db.default_db() on each call
        self.binary = binary
        self.extra_env = dict(env or {})  # helper-command environment (export/models/--version)

    def _bin(self) -> str:
        return self.binary or opencode_bin()

    # --- start ---

    def build_command(self, spec: RunSpec) -> list[str]:
        cmd = [self._bin(), "run", "--format", "json", "--model", spec.model_id, "--dir", spec.cwd]
        if spec.variant:
            cmd += ["--variant", spec.variant]
        if spec.session_id:
            cmd += ["--session", spec.session_id]
        cmd.append(prompt_arg(spec.prompt, spec.cwd))
        return cmd

    def env(self, spec: RunSpec) -> dict[str, str]:
        """Isolation: the hub inside a worker session writes to its own dir, not the live storage."""
        home = Path(spec.cwd) / ".ahub" / "home"
        home.mkdir(parents=True, exist_ok=True)
        env = {"AHUB_HOME": str(home), "AGENT_HUB_HOME": str(home / "v1")}
        env.update(spec.env)
        return env

    def parse_line(self, line: str, now: int) -> list[Activity]:
        s = line.strip()
        if not s.startswith("{"):
            return []
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            return []
        if not isinstance(ev, dict):
            return []
        ts = ev.get("timestamp") if isinstance(ev.get("timestamp"), int) else now
        out: list[Activity] = []
        sid = ev.get("sessionID")
        if isinstance(sid, str) and sid:
            out.append(Activity(Act.SESSION, ts, text=sid))
        t = ev.get("type")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if t == "step_start":
            out.append(Activity(Act.STEP, ts, data={"edge": "start"}))
        elif t == "step_finish":
            out.append(Activity(Act.STEP, ts, data={"edge": "finish", "reason": part.get("reason", "")}))
            u = _usage_of_part(part)
            if u is not None:
                out.append(Activity(Act.USAGE, ts, data={"usage": u, "cost": part.get("cost")}))
        elif t == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status", ""))
            out.append(Activity(Act.TOOL_END, ts, tool=str(part.get("tool", "")),
                                data={"status": status, "input": _short_input(state.get("input")),
                                      "output": _short_output(state.get("output"))}))
        elif t == "text":
            out.append(Activity(Act.TEXT, ts, text=str(part.get("text", ""))))
        elif t == "reasoning":
            out.append(Activity(Act.REASONING, ts, text=str(part.get("text", ""))[:500]))
        elif t == "error":
            text, flags = classify_error(ev)
            out.append(Activity(Act.ERROR, ts, text=text, data=flags))
        elif t:
            out.append(Activity(Act.OTHER, ts, data={"type": str(t)}))
        return out

    def structured(self, final_text: str, activities: list[Activity], schema: dict | None) -> dict | None:
        """opencode has no schema support — look for JSON in the last reply (```json block or whole text)."""
        return extract_json(final_text)

    # --- provider data (opencode.db) ---

    def usage(self, session_id: str) -> Usage | None:
        from ahub.providers import opencode_db

        return opencode_db.session_usage(session_id, self.db_path)

    def session_state(self, session_id: str) -> SessionState | None:
        from ahub.providers import opencode_db

        return opencode_db.session_state(session_id, self.db_path)

    def find_session(self, cwd: str, started_after_ms: int) -> str | None:
        from ahub.providers import opencode_db

        return opencode_db.find_session(cwd, started_after_ms, self.db_path)

    def export(self, session_id: str) -> dict | None:
        try:
            rc, out, err = run_capture([self._bin(), "export", session_id], env=self.extra_env)
        except (OSError, subprocess.SubprocessError) as e:
            _log.warning("export %s: %s", session_id, e)
            return None
        if rc != 0:
            _log.warning("export %s: code %s: %s", session_id, rc, err[-300:])
            return None
        start = out.find("{")
        try:
            data = json.loads(out[start:]) if start >= 0 else None
        except json.JSONDecodeError:
            _log.warning("export %s: not JSON", session_id)
            return None
        return data if isinstance(data, dict) else None

    def catalog(self, refresh: bool = False) -> list[CatalogEntry]:
        """Catalog from `opencode models <provider> --verbose`: one call per provider id.

        Cached in the hub data dir for 24 h (paths.data_dir()); refresh=True re-reads.
        """
        binary = self._bin()
        try:
            provider_ids = _list_provider_ids(binary, self.extra_env)
        except (OSError, subprocess.SubprocessError) as e:
            _log.warning("models: %s", e)
            return []
        out: list[CatalogEntry] = []
        for pid in provider_ids:
            cached = None if refresh else _read_cache(pid)
            if cached is not None:
                out.extend(cached)
                continue
            try:
                rc, raw, _err = run_capture([binary, "models", pid, "--verbose"], env=self.extra_env)
            except (OSError, subprocess.SubprocessError) as e:
                _log.warning("models %s: %s", pid, e)
                continue
            if rc != 0:
                _log.warning("models %s: code %s", pid, rc)
                continue
            entries = parse_models_verbose(raw)
            _write_cache(pid, entries)
            out.extend(entries)
        # a provider without the per-id form (old binary): fall back to the whole list
        if not out and not refresh:
            pass
        return out

    def health(self) -> Health:
        problems: list[str] = []
        details: dict = {}
        binary = self._bin()
        if not os.access(binary, os.X_OK):
            return Health(False, (_t("opencode.no_binary", binary=binary),))
        try:
            rc, out, _err = run_capture([binary, "--version"], timeout=30, env=self.extra_env)
            details["version"] = out.strip()[:40]
            if rc != 0:
                problems.append(_t("opencode.version_fail", code=rc))
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(_t("opencode.no_answer", err=e))
        from ahub.providers import opencode_db

        st = opencode_db.check_schema(self.db_path)
        if not st.ok:
            problems.extend(_t("opencode.db_problem", problem=p) for p in st.problems)
        return Health(not problems, tuple(problems), details)


def _short_input(inp) -> dict:
    if not isinstance(inp, dict):
        return {}
    out = {}
    for k in ("command", "filePath", "path", "pattern", "url", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            out[k] = v[:200]
    return out


def _short_output(out) -> str:
    """First 300 characters of what a tool returned (the transcript shows them; the pulse does not)."""
    if isinstance(out, str):
        return out[:300]
    if isinstance(out, dict):
        return _short_output(out.get("output") or out.get("text") or "")
    if isinstance(out, list):
        return " ".join(x for x in (_short_output(v) for v in out[:3]) if x)
    return ""


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """Last JSON object from the model reply: ```json … ``` block or whole text."""
    cands = _FENCE.findall(text or "")
    cands = list(reversed(cands)) + [(text or "").strip()]
    for c in cands:
        try:
            data = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


_MODEL_LINE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.:/@-]+$")


def parse_models_verbose(out: str) -> list[CatalogEntry]:
    """`opencode models <provider> --verbose` output: a "provider/model" line + JSON object."""
    models: list[CatalogEntry] = []
    lines = out.splitlines()
    i = 0
    while i < len(lines):
        name = lines[i].strip()
        if not _MODEL_LINE.match(name):
            i += 1
            continue
        j = i + 1
        buf: list[str] = []
        depth = 0
        while j < len(lines):
            ln = lines[j]
            buf.append(ln)
            depth += ln.count("{") - ln.count("}")
            j += 1
            if depth <= 0 and buf and "{" in "".join(buf):
                break
        info: dict = {}
        try:
            parsed = json.loads("\n".join(buf))
            if isinstance(parsed, dict):
                info = parsed
        except json.JSONDecodeError:
            pass
        provider_id = str(info.get("providerID") or name.split("/", 1)[0])
        variants = tuple(info.get("variants", {}).keys()) if isinstance(info.get("variants"), dict) else ()
        cost = info.get("cost") if isinstance(info.get("cost"), dict) else {}
        pin, pout = cost.get("input"), cost.get("output")
        cache = cost.get("cache") if isinstance(cost.get("cache"), dict) else {}
        pcache = cache.get("read")
        limit = info.get("limit") if isinstance(info.get("limit"), dict) else {}
        ctx = limit.get("context")
        display = str(info.get("name") or name.split("/", 1)[-1])
        family = str(info.get("family") or "")
        vendor = infer_vendor(family, display, name)
        plan = infer_plan(provider_id, name,
                          pin if isinstance(pin, (int, float)) else None,
                          pout if isinstance(pout, (int, float)) else None)
        models.append(CatalogEntry(
            model_id=name,
            display_name=display,
            vendor=vendor,
            plan=plan,
            price_in=pin if isinstance(pin, (int, float)) else None,
            price_out=pout if isinstance(pout, (int, float)) else None,
            price_cache=pcache if isinstance(pcache, (int, float)) else None,
            context=ctx if isinstance(ctx, int) else None,
            reasoning=variants,
            status=str(info.get("status", "")),
        ))
        i = j if j > i + 1 else i + 1
    return models


def catalog_cache_path(provider_id: str):
    """Cache file for one provider's verbose catalog in the hub data dir."""
    from ahub import paths

    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in provider_id)
    return paths.data_dir() / f"catalog-opencode-{safe or 'unknown'}.json"


def _list_provider_ids(binary: str, env: dict[str, str] | None) -> list[str]:
    """Provider ids from `opencode models` (one "provider/model" per line)."""
    rc, out, _err = run_capture([binary, "models"], env=env)
    if rc != 0:
        return ["opencode", "opencode-go", "openrouter"]
    seen: list[str] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not _MODEL_LINE.match(line) or "/" not in line:
            continue
        pid = line.split("/", 1)[0]
        if pid and pid not in seen:
            seen.append(pid)
    return seen or ["opencode", "opencode-go", "openrouter"]


def _read_cache(provider_id: str) -> list[CatalogEntry] | None:
    """Cached catalog for the provider, None when missing or older than 24 h."""
    import time

    path = catalog_cache_path(provider_id)
    try:
        if not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > CATALOG_TTL_S:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    try:
        return [CatalogEntry.from_dict(e) for e in items if isinstance(e, dict)]
    except (TypeError, ValueError):
        return None


def _write_cache(provider_id: str, entries: list[CatalogEntry]) -> None:
    """Store the catalog for 24 h; a failure is a log line, never an error."""
    import time

    path = catalog_cache_path(provider_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "entries": [e.to_dict() for e in entries]}
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        _log.warning("catalog cache %s: %s", provider_id, e)
