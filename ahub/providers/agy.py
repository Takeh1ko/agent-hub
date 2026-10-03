"""agy provider: Google Antigravity CLI (Gemini), `agy -p … --output-format stream-json`.

Stream events (verified live 2026-10-03, agy 1.2.15; samples — tests/data/agy/):
  {"event":"init","conversation_id":<uuid>,"init":{model, cwd, tools, permission_mode}}
  {"event":"step_update","step_update":{conversation_id, step_index, state:ACTIVE|DONE,
     step_type:user_input|agent_response|tool|error_message|system_message|finish, text_delta,
     tool_name, tool_info{name, parameters}, duration_seconds,
     usage{input_tokens, output_tokens, thinking_tokens, cache_read_tokens, total_tokens}}}
  {"event":"result","result":{conversation_id, status:SUCCESS|ERROR, response, error, usage,
     structured_output, denied_actions[{action, display_name}]}}

Session id — init.conversation_id (first event, so the core links it right away); resume is
`--conversation <id>`. Tokens — per step and total in result (the total is the sum of steps, so it
wins); money is None: agy works on a quota window and reports no prices.

Errors: result.status=ERROR carries the text in `error` (exit 1 for a bad model, exit 3 for a
model/API failure; with --print-timeout agy can also exit 0 with partial output — the status decides,
not the code). denied_actions with status SUCCESS means the turn did nothing useful. No export, no
session search, no quota numbers — those capabilities are honestly absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ahub import log as hublog
from ahub.i18n import t as _t
from ahub.providers.base import Act, Activity, Cap, Health, ModelInfo, Outcome, Provider, RunSpec, Usage
from ahub.providers.opencode import extract_json, prompt_arg  # shared bits: prompt → argument, JSON out of text

_log = hublog.get("agy")

TRANSIENT_MARKERS = ("connection refused", "dial tcp", "no such host", "request failed", "timeout",
                     "timed out", "deadline exceeded", "temporarily unavailable", "unavailable",
                     "overloaded", "bad gateway", "gateway timeout", "internal error", "server error",
                     "connection reset", "connection aborted", "broken pipe", "unexpected eof",
                     "tls handshake", "try again")
QUOTA_MARKERS = ("quota", "resource_exhausted", "resource exhausted", "usage limit", "rate limit",
                 "rate_limit", "limit exceeded", "credits", "billing")
NO_ACCESS_MARKERS = ("unauthorized", "unauthenticated", "forbidden", "permission denied", "not signed in",
                     "no credentials", "invalid credentials", "api key not valid", "sign in", "login required")
PRINT_TIMEOUT_MARKER = "print timeout"  # stderr: agy hit --print-timeout, the turn did not finish
RESUME_GONE_MARKER = "not found"  # stderr: `conversation "…" not found` — agy opened a new session
_STATUS = re.compile(r"\b(?:status(?:_?code)?|http[ _]?status)\D{0,3}(\d{3})\b", re.IGNORECASE)
_AGY_ERROR = re.compile(r"AGY_ERROR:\s*(\{.*\})")
_MODEL_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.:/-]*)\t(\S.*?)\s*$")
_DELTA_LIMIT = 256  # text buffers per process (steps grow, sessions do not)


def agy_bin() -> str:
    """agy executable: which → ~/.local/bin/agy."""
    return shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")


def agy_state_file() -> Path:
    """agy CLI state: onboarding/login marker (values are never read)."""
    return Path.home() / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"


def run_capture(cmd: list[str], timeout: int = 120, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a helper command and collect its output through a file (agy drops the tail on a pipe)."""
    full_env = {**os.environ, **env} if env else None
    with tempfile.TemporaryFile("w+", encoding="utf-8") as out:
        r = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, text=True, timeout=timeout, env=full_env)
        out.seek(0)
        return r.returncode, out.read(), r.stderr or ""


def classify_text(text: str) -> dict:
    """Error text → transient/quota/no_access/status flags (no access → quota → transient)."""
    low = (text or "").lower()
    m = _STATUS.search(low)
    status = int(m.group(1)) if m else None
    flags = {"transient": False, "quota": False, "no_access": False, "status": status}
    if status in (401, 403) or any(mk in low for mk in NO_ACCESS_MARKERS):
        flags["no_access"] = True
    elif status == 429 or any(mk in low for mk in QUOTA_MARKERS):
        flags["quota"] = True
    elif (status is not None and status >= 500) or any(mk in low for mk in TRANSIENT_MARKERS):
        flags["transient"] = True
    return flags


def classify_stderr(stderr_tail: str) -> tuple[str, dict]:
    """`AGY_ERROR: {…}` from stderr → (text, flags); empty text when the line is absent."""
    m = _AGY_ERROR.search(stderr_tail or "")
    if m is None:
        return "", classify_text(stderr_tail or "")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        data = {}
    parts = [str(v) for k, v in data.items() if v and k != "retryable"] if isinstance(data, dict) else []
    text = " | ".join(parts)[:2000] or m.group(1)[:2000]
    flags = classify_text(text + " " + (stderr_tail or ""))
    if isinstance(data, dict) and data.get("retryable") and not (flags["quota"] or flags["no_access"]):
        flags["transient"] = True
    return text, flags


def usage_of(raw) -> Usage | None:
    """agy usage dict → Usage (context — the step's input tokens plus cache)."""
    if not isinstance(raw, dict):
        return None
    tin, tout, cache = raw.get("input_tokens"), raw.get("output_tokens"), raw.get("cache_read_tokens")
    thinking = raw.get("thinking_tokens")
    if not isinstance(tin, int) and not isinstance(tout, int):
        return None
    return Usage(tokens_in=tin if isinstance(tin, int) else None,
                 tokens_out=tout if isinstance(tout, int) else None,
                 tokens_reasoning=thinking if isinstance(thinking, int) else None,
                 cache_read=cache if isinstance(cache, int) else None,
                 context=(tin or 0) + (cache or 0) or None)


def _tool_input(tool_info) -> dict:
    params = tool_info.get("parameters") if isinstance(tool_info, dict) else None
    if not isinstance(params, dict):
        return {}
    return {k: (v[:200] if isinstance(v, str) else v) for k, v in params.items() if v not in ("", None)}


def parse_models(out: str) -> list[ModelInfo]:
    """`agy models` output: "id<TAB>Name" lines after the "Fetching available models…" header."""
    models: list[ModelInfo] = []
    for line in (out or "").splitlines():
        m = _MODEL_LINE.match(line.strip())
        if m:
            models.append(ModelInfo(m.group(1), counter="quota", note=m.group(2)))
    return models


def _outcome_of(flags: dict) -> Outcome:
    for key, outcome in (("no_access", Outcome.NO_ACCESS), ("quota", Outcome.QUOTA),
                         ("transient", Outcome.TRANSIENT)):
        if flags.get(key):
            return outcome
    return Outcome.MODEL_ERROR


class AgyProvider(Provider):
    name = "agy"
    # Honestly absent: export, find_session, cost_money/cost_quota (window quota, agy reports no prices).
    capabilities = frozenset({Cap.RESUME, Cap.STREAM, Cap.STRUCTURED, Cap.TOKENS, Cap.ACTIVE_TOOL,
                              Cap.CATALOG, Cap.HEALTH})

    def __init__(self, binary: str | None = None, env: dict[str, str] | None = None,
                 skip_permissions: bool = True) -> None:
        self.binary = binary
        self.extra_env = dict(env or {})  # environment for helper calls (models/--version)
        # The CLI has two permission modes and neither of them limits writes to cwd (checked live
        # 2026-10-03): accept-edits — files are written without questions, but commands are denied
        # in headless mode (auto-deny, the turn comes out empty); skip-permissions — everything is
        # allowed, otherwise the worker cannot run git and the tests. What holds agy in place is the
        # project copy, scrub_env and the gates.
        self.skip_permissions = skip_permissions
        self._deltas: dict[str, str] = {}  # (conversation_id, step_index) → accumulated text

    def _bin(self) -> str:
        return self.binary or agy_bin()

    # --- start ---

    def build_command(self, spec: RunSpec) -> list[str]:
        cmd = [self._bin(), "-p", prompt_arg(spec.prompt, spec.cwd), "--output-format", "stream-json",
               "--model", spec.model_id, "--print-timeout", f"{max(1, int(spec.timeout_s))}s"]
        if spec.session_id:
            cmd += ["--conversation", spec.session_id]
        if spec.schema is not None:
            cmd += ["--json-schema", json.dumps(spec.schema, ensure_ascii=False)]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd += ["--mode", "accept-edits"]
        return cmd

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
        kind = ev.get("event")
        if kind == "init":
            if len(self._deltas) > _DELTA_LIMIT:  # buffers of past turns (steps grow, session ids do not)
                self._deltas.clear()
            cid = str(ev.get("conversation_id") or "")
            return [Activity(Act.SESSION, now, text=cid)] if cid else []
        if kind == "step_update":
            step = ev.get("step_update")
            return self._step(step if isinstance(step, dict) else {}, now)
        if kind == "result":
            result = ev.get("result")
            return self._result(result if isinstance(result, dict) else {}, now)
        return [Activity(Act.OTHER, now, data={"event": str(kind)})] if kind else []

    def _step(self, step: dict, now: int) -> list[Activity]:
        stype = str(step.get("step_type") or "")
        state = str(step.get("state") or "")
        idx = step.get("step_index") if isinstance(step.get("step_index"), int) else 0
        key = f"{step.get('conversation_id') or ''}:{idx}"
        out: list[Activity] = []
        if stype == "tool":
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            tool = str(step.get("tool_name") or info.get("name") or "")
            if state == "ACTIVE":
                out.append(Activity(Act.TOOL_START, now, tool=tool, data={"input": _tool_input(info)}))
            elif state == "DONE":
                out.append(Activity(Act.TOOL_END, now, tool=tool,
                                    data={"status": state, "input": _tool_input(info)}))
            return out
        if stype == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                buf = self._deltas.get(key, "") + delta
                self._deltas[key] = buf
                out.append(Activity(Act.TEXT, now, text=buf))
            if state == "DONE":
                out.append(Activity(Act.STEP, now, data={"edge": "finish", "type": stype}))
                u = usage_of(step.get("usage"))
                if u is not None:
                    out.append(Activity(Act.USAGE, now, data={"usage": u, "step": idx}))
            return out
        if stype == "error_message":
            text = str(step.get("error") or step.get("text") or "")
            out.append(Activity(Act.ERROR, now, text=text or _t("agy.error_step"), data=classify_text(text)))
            return out
        if state in ("ACTIVE", "DONE"):
            out.append(Activity(Act.STEP, now, data={"edge": state.lower(), "type": stype}))
        return out

    def _result(self, result: dict, now: int) -> list[Activity]:
        out: list[Activity] = []
        response = result.get("response")
        structured = result.get("structured_output")
        data = {"structured": structured} if isinstance(structured, dict) else {}
        if isinstance(response, str) and response.strip():
            out.append(Activity(Act.TEXT, now, text=response, data=data))
        elif data:
            out.append(Activity(Act.OTHER, now, data=data))
        u = usage_of(result.get("usage"))
        if u is not None:
            out.append(Activity(Act.USAGE, now, data={"usage": u, "final": True}))
        denied = result.get("denied_actions")
        if isinstance(denied, list) and denied:
            names = ", ".join(str(d.get("display_name") or d.get("action") or d) for d in denied
                              if isinstance(d, dict))
            out.append(Activity(Act.ERROR, now, text=_t("agy.denied", actions=names or "?"),
                                data={"denied": True, "transient": False, "quota": False, "no_access": False}))
        if str(result.get("status") or "").upper() not in ("SUCCESS", ""):
            text = str(result.get("error") or "") or _t("agy.error_status", status=result.get("status"))
            out.append(Activity(Act.ERROR, now, text=text[:2000],
                                data={**classify_text(text), "terminal": True}))
        return out

    def classify(self, *, exit_code: int | None, activities: list[Activity], session_id: str | None,
                 stderr_tail: str) -> tuple[Outcome, str]:
        """The result event decides the turn's outcome, not the exit code (with --print-timeout the code is often 0)."""
        err_text, err_flags = classify_stderr(stderr_tail)
        terminal = next((a for a in activities if a.kind is Act.ERROR and a.data.get("terminal")), None)
        if terminal is not None:
            return _outcome_of({**terminal.data, **err_flags} if err_text else terminal.data), \
                (terminal.text or err_text)[:2000]
        if PRINT_TIMEOUT_MARKER in (stderr_tail or "").lower():
            line = next((ln.strip() for ln in (stderr_tail or "").splitlines()
                         if PRINT_TIMEOUT_MARKER in ln.lower()), "")
            return Outcome.TIMEOUT, (line or _t("agy.print_timeout"))[:2000]
        denied = next((a for a in activities if a.kind is Act.ERROR and a.data.get("denied")), None)
        if denied is not None:  # the turn did nothing: there was nobody to ask for permissions
            hint = next((ln.strip() for ln in (stderr_tail or "").splitlines() if "permission" in ln.lower()), "")
            return Outcome.MODEL_ERROR, "; ".join(p for p in (denied.text, hint) if p)[:2000]
        if err_text:  # AGY_ERROR without a result event (the output was cut off)
            return _outcome_of(err_flags), err_text[:2000]
        if RESUME_GONE_MARKER in (stderr_tail or "").lower():
            _log.warning("agy: conversation not found — a new session was opened instead of a resume")
        return super().classify(exit_code=exit_code, activities=activities, session_id=session_id,
                                stderr_tail=stderr_tail)

    def stream_usage(self, activities: list[Activity]) -> Usage | None:
        """The final agy total (the sum of steps) beats the sum of steps; no money comes — cost None."""
        final: Usage | None = None
        acc: Usage | None = None
        for a in activities:
            u = a.data.get("usage") if a.kind is Act.USAGE else None
            if not isinstance(u, Usage):
                continue
            if a.data.get("final"):
                final = u
            else:
                acc = u if acc is None else acc.add(u)
        return final or acc

    def structured(self, final_text: str, activities: list[Activity], schema: dict | None) -> dict | None:
        """--json-schema: agy itself puts the parsed object into result.structured_output."""
        for a in reversed(activities):
            data = a.data.get("structured")
            if isinstance(data, dict):
                return data
        return extract_json(final_text)

    # --- provider data ---

    def catalog(self) -> list[ModelInfo]:
        try:
            rc, out, _err = run_capture([self._bin(), "models"], env=self.extra_env)
        except (OSError, subprocess.SubprocessError) as e:
            _log.warning("models: %s", e)
            return []
        if rc != 0:
            _log.warning("models: code %s", rc)
            return []
        return parse_models(out)

    def health(self) -> Health:
        problems: list[str] = []
        details: dict = {}
        binary = self._bin()
        if not os.access(binary, os.X_OK):
            return Health(False, (_t("agy.no_binary", binary=binary),))
        try:
            rc, out, _err = run_capture([binary, "--version"], timeout=30, env=self.extra_env)
            details["version"] = out.strip()[:40]
            if rc != 0:
                problems.append(_t("agy.version_fail", code=rc))
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(_t("agy.no_answer", err=e))
        if not agy_state_file().is_file():
            problems.append(_t("agy.no_login"))
        models = self.catalog()
        details["models"] = len(models)
        if not models:
            problems.append(_t("agy.no_models"))
        return Health(not problems, tuple(problems), details)
