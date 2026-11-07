"""omp provider: oh-my-pi CLI, `omp -p --mode json` (verified 2026-11-01, 18.14.0).

JSONL events (samples in tests/data/omp/): session{id,cwd}, agent_start/end,
turn_start/end, message_start/update/end, tool_execution_start/end, auto_retry_end.
Session id comes from the first `session`; resume uses --resume with the same --session-dir.
Completed assistant messages carry content[{type:text|thinking|toolCall}], usage{input,
output,cacheRead,cacheWrite,cost{total}} and stopReason. The same message/usage is repeated
in turn_end and agent_end: only message_end is counted. Message updates are heartbeats;
complete text/reasoning blocks are emitted once at message_end (no duplicated deltas).

Errors: assistant stopReason=error, errorStatus/errorMessage; exhausted retries emit
auto_retry_end{success:false,finalError}. Go 429 includes retry-after-ms: persist a zero
quota bucket until that reset so workers and the scheduler share the hold. Recovered
auto-retries do not fail a successful turn. Exit 0 means success, 1 means error.
Go and Mistral Vibe are subscriptions; zero/absent costs mean unknown money, including
willow's zero catalog prices. omp keeps other providers' keys itself (~/.omp/agent/models.yml),
so the Go key is required only when omp lists no models without it. A 429 outside Go has no
shared bucket: it stays a plain QUOTA outcome with the core's hold backoff.
Export and session search are deliberately absent: no reliable API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from ahub import config, paths
from ahub.i18n import t as _t
from ahub.providers.base import (
    Act,
    Activity,
    Cap,
    CatalogEntry,
    Health,
    Outcome,
    PlanKind,
    Provider,
    QuotaBucket,
    RunSpec,
    Usage,
    infer_vendor,
)
from ahub.providers.opencode import prompt_arg, run_capture
from ahub.time import now_ms

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
SUBSCRIPTIONS = ("opencode-go", "mistral-vibe")  # omp providers paid by a plan, not per token
_GO_MODELS = ("opencode-go/deepseek-v4.1-flash", "opencode-go/muse-spark-1.3-contributor")
_RETRY_MS = re.compile(r"retry-after-ms[=: ]+(\d+)", re.IGNORECASE)
_STATUS = re.compile(r"\b(?:status|http|error)\W{0,8}(\d{3})\b|^\s*(\d{3})\b", re.IGNORECASE)
_TRANSIENT = ("network", "fetch failed", "connection", "econnreset", "econnrefused", "dns",
              "timed out", "timeout", "socket", "overloaded", "service unavailable", "bad gateway")


def omp_bin(configured: str = "") -> str:
    return str(Path(configured).expanduser()) if configured else (
        shutil.which("omp") or str(Path.home() / ".local/bin/omp"))


def usage_of(raw) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    values = {key: raw.get(key) if type(raw.get(key)) is int else None
              for key in ("input", "output", "cacheRead", "cacheWrite")}
    if all(v is None for v in values.values()):
        return None
    cost = raw.get("cost")
    total = cost.get("total") if isinstance(cost, dict) else None
    money = float(total) if isinstance(total, (int, float)) and total > 0 else None
    return Usage(tokens_in=values["input"], tokens_out=values["output"],
                 tokens_reasoning=raw.get("reasoningTokens") if type(raw.get("reasoningTokens")) is int else None,
                 cache_read=values["cacheRead"], cache_write=values["cacheWrite"], cost_usd=money,
                 context=sum(values[k] or 0 for k in ("input", "cacheRead", "cacheWrite")) or None)


def parse_models(out: str) -> list[CatalogEntry]:
    """`omp models --json`: chat models of every provider omp is set up for; zero prices are not free."""
    try:
        raw = json.loads(out)
    except (ValueError, TypeError):
        return []
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list):
        return []
    result = []
    for m in models:
        if not isinstance(m, dict) or not m.get("provider") or not m.get("id") or m.get("kind", "chat") != "chat":
            continue
        mid = f"{m['provider']}/{m['id']}"
        cost = m.get("cost") if isinstance(m.get("cost"), dict) else {}
        subscription = m["provider"] in SUBSCRIPTIONS

        def price(key, cost=cost, subscription=subscription):
            v = cost.get(key)
            return float(v) if isinstance(v, (int, float)) and v > 0 and not subscription else None

        result.append(CatalogEntry(
            model_id=mid, display_name=str(m.get("name") or m["id"]), vendor=infer_vendor(model_id=mid),
            plan=PlanKind.SUBSCRIPTION if subscription else PlanKind.PAYG,
            price_in=price("input"), price_out=price("output"), price_cache=price("cacheRead"),
            context=m.get("contextWindow") if type(m.get("contextWindow")) is int else None,
            reasoning=THINKING_LEVELS if m.get("reasoning") else ("off",),
        ))
    return result


class OmpProvider(Provider):
    name = "omp"
    capabilities = frozenset({Cap.RESUME, Cap.STREAM, Cap.TOKENS, Cap.COST_MONEY,
                              Cap.ACTIVE_TOOL, Cap.CATALOG, Cap.HEALTH})

    def __init__(self, binary: str | None = None, api_key_file: str | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.binary = binary
        self.api_key_file = api_key_file
        self.extra_env = dict(env or {})

    def _settings(self) -> config.ProviderSettings:
        try:
            return config.load_hub().provider(self.name)
        except config.ConfigError:
            return config.ProviderSettings()

    def _bin(self) -> str:
        return omp_bin(self.binary if self.binary is not None else self._settings().binary)

    def _key(self) -> str:
        path = self.api_key_file if self.api_key_file is not None else self._settings().api_key_file
        try:
            return Path(path or "~/.config/opencode-go/api_key").expanduser().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    def _redact(self, text: str) -> str:
        for key in (self._key(), self.extra_env.get("OPENCODE_API_KEY"), os.environ.get("OPENCODE_API_KEY")):
            if key:
                text = text.replace(key, "[redacted]")
        return text

    def build_command(self, spec: RunSpec) -> list[str]:
        # Stable across turns, unique per worktree; shared by executor/review sessions with distinct IDs.
        ident = hashlib.sha256(str(Path(spec.cwd).resolve()).encode()).hexdigest()[:24]
        sessions = paths.state_dir() / "omp" / "sessions" / ident
        sessions.mkdir(parents=True, exist_ok=True)
        cmd = [self._bin(), "-p", "--mode", "json", "--model", spec.model_id,
               "--thinking", spec.variant or "off", "--approval-mode", "yolo", "--cwd", spec.cwd,
               "--session-dir", str(sessions)]
        if spec.session_id:
            cmd += ["--resume", spec.session_id]
        cmd += ["--", prompt_arg(spec.prompt, spec.cwd)]
        return cmd

    def env(self, spec: RunSpec) -> dict[str, str]:
        home = Path(spec.cwd) / ".ahub/home"
        home.mkdir(parents=True, exist_ok=True)
        env = {"AHUB_HOME": str(home), "AGENT_HUB_HOME": str(home / "v1"), **self.extra_env, **spec.env}
        key = self._key()
        if key:
            env["OPENCODE_API_KEY"] = key
        return env

    def _error(self, text: str, now: int, status=None, **data) -> Activity:
        text = self._redact(text)
        low = text.lower()
        if type(status) is not int:
            match = _STATUS.search(text)
            status = int(match.group(1) or match.group(2)) if match else None
        no_access = status in (401, 403) or any(s in low for s in (
            "unauthorized", "forbidden", "invalid api key", "no api key", "authentication"))
        quota = not no_access and (status == 429 or any(s in low for s in (
            "usage limit", "rate limit", "quota", "credits exhausted")))
        transient = not (no_access or quota) and (
            (status is not None and status >= 500) or any(s in low for s in _TRANSIENT))
        retry = _RETRY_MS.search(text)
        reset = now + int(retry.group(1)) if quota and retry else 0
        return Activity(Act.ERROR, now, text=text[:2000] or _t("omp.turn_failed"),
                        data={**data, "status": status, "no_access": no_access, "quota": quota,
                              "transient": transient, "reset_at": reset,
                              "go": "go usage limit" in low})

    def parse_line(self, line: str, now: int) -> list[Activity]:
        try:
            ev = json.loads(self._redact(line))
        except (ValueError, TypeError):
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("type")
        if kind == "session":
            sid = ev.get("id")
            return [Activity(Act.SESSION, now, text=sid)] if isinstance(sid, str) and sid else []
        if kind in ("turn_start", "turn_end"):
            return [Activity(Act.STEP, now, data={"edge": "start" if kind == "turn_start" else "finish"})]
        if kind == "message_end":
            msg = ev.get("message")
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                return []
            out = []
            content = msg.get("content")
            if isinstance(content, list):
                text = "".join(b["text"] for b in content if isinstance(b, dict)
                               and b.get("type") == "text" and isinstance(b.get("text"), str))
                if text:
                    out.append(Activity(Act.TEXT, now, text=self._redact(text)))
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "thinking" and isinstance(b.get("thinking"), str):
                        out.append(Activity(Act.REASONING, now, text=self._redact(b["thinking"])))
            usage = usage_of(msg.get("usage"))
            if usage is not None:
                out.append(Activity(Act.USAGE, now, data={"usage": usage}))
            if msg.get("stopReason") in ("error", "aborted"):
                out.append(self._error(str(msg.get("errorMessage") or ""), now, msg.get("errorStatus")))
            else:
                out.append(Activity(Act.OTHER, now, data={"assistant_ok": True}))
            return out
        if kind in ("tool_execution_start", "tool_execution_end"):
            tool = str(ev.get("toolName") or "")
            if kind == "tool_execution_start":
                args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
                return [Activity(Act.TOOL_START, now, tool=tool, data={"input": args})]
            result = ev.get("result")
            content = result.get("content") if isinstance(result, dict) else None
            text = "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)) \
                if isinstance(content, list) else ""
            return [Activity(Act.TOOL_END, now, tool=tool,
                             data={"status": "error" if ev.get("isError") else "completed",
                                   "output": self._redact(text)[:300]})]
        if kind == "auto_retry_end":
            if ev.get("success") is False:
                return [self._error(str(ev.get("finalError") or ""), now, terminal=True)]
            return [Activity(Act.OTHER, now, data={"retry_ok": True})]
        return [Activity(Act.OTHER, now, data={"type": str(kind)})] if kind else []

    def classify(self, *, exit_code: int | None, activities: list[Activity], session_id: str | None,
                 stderr_tail: str) -> tuple[Outcome, str]:
        # Only errors since the last successful assistant message/retry matter.
        pending = []
        for a in activities:
            if a.data.get("assistant_ok") or a.data.get("retry_ok"):
                pending.clear()
            elif a.kind is Act.ERROR:
                pending.append(a)
        error = next((a for a in reversed(pending) if a.data.get("terminal")), None)
        error = error or (pending[-1] if pending else None)
        if error is None and exit_code not in (0, None) and stderr_tail:
            error = self._error(stderr_tail, now_ms())
        if error is not None:
            for flag, outcome in (("no_access", Outcome.NO_ACCESS), ("quota", Outcome.QUOTA),
                                  ("transient", Outcome.TRANSIENT)):
                if error.data.get(flag):
                    if outcome is Outcome.QUOTA and error.data.get("go") and error.data.get("reset_at"):
                        self._save_reset(error.data["reset_at"])
                    return outcome, error.text
            return Outcome.MODEL_ERROR, error.text
        return super().classify(exit_code=exit_code, activities=[], session_id=session_id,
                                stderr_tail=self._redact(stderr_tail))

    def _save_reset(self, reset: int) -> None:
        path = paths.state_dir() / "omp" / "go-quota.json"
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"reset_at": reset}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # still return QUOTA: the core can use its normal hold backoff
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def quota(self, force: bool = False) -> list[QuotaBucket]:
        del force  # no quota endpoint; only a real 429 gives us a reset
        try:
            raw = json.loads((paths.state_dir() / "omp/go-quota.json").read_text(encoding="utf-8"))
            reset = raw.get("reset_at") if isinstance(raw, dict) else None
        except (OSError, ValueError):
            return []
        if type(reset) is not int or reset <= now_ms():
            return []
        # The scheduler supports 5h/weekly buckets; use its short-window slot with
        # the actual retry-after reset, without inferring the Go window's duration.
        return [QuotaBucket("OpenCode Go (omp)", "5h", 0, reset,
                            models=lambda mid: mid.startswith("opencode-go/"))]

    def _helper_env(self) -> dict[str, str]:
        from ahub.prepare import apply_proxy, scrub_env
        from ahub.providers.runner import hub_proxy

        env = apply_proxy(scrub_env(dict(os.environ)), *hub_proxy(self.name))
        env.update(self.extra_env)
        key = self._key()
        if key:
            env["OPENCODE_API_KEY"] = key
        return env

    def _listed(self) -> list[CatalogEntry]:
        try:
            rc, out, _err = run_capture([self._bin(), "models", "--json"], timeout=30, env=self._helper_env())
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_models(out) if rc == 0 else []

    def catalog(self, refresh: bool = False) -> list[CatalogEntry]:
        del refresh  # `models refresh` can make network requests; the local CLI catalog suffices
        models = self._listed()
        if models and not self._key():
            return models  # omp runs on its own providers: Go models would fail without the key
        known = {m.model_id for m in models}
        return models + [CatalogEntry(mid, vendor=infer_vendor(model_id=mid), plan=PlanKind.SUBSCRIPTION,
                                      reasoning=THINKING_LEVELS) for mid in _GO_MODELS if mid not in known]

    def health(self) -> Health:
        binary = self._bin()
        if not (shutil.which(binary) or os.access(binary, os.X_OK)):
            return Health(False, (_t("omp.no_binary", binary=binary),))
        problems = []
        details = {"binary": binary, "key_present": bool(self._key())}
        try:
            rc, out, _err = run_capture([binary, "--version"], timeout=30, env=self._helper_env())
            details["version"] = self._redact(out.strip())[:80]
            if rc != 0 or not out.strip():
                problems.append(_t("omp.version_fail"))
        except (OSError, subprocess.SubprocessError):
            problems.append(_t("omp.version_fail"))
        if not details["key_present"] and not self._listed():
            problems.append(_t("omp.no_key"))
        return Health(not problems, tuple(problems), details)
