"""codex provider: OpenAI Codex CLI — `codex exec --json`, OS sandbox `workspace-write`.

Events (verified live 2026-10-03, codex-cli 0.153.4, ChatGPT login; samples — tests/data/codex/):
  {"type":"thread.started","thread_id":"<uuid>"}
  {"type":"turn.started"}
  {"type":"item.started"|"item.completed","item":{"id":"item_0",
     "type":"agent_message"|"reasoning"|"command_execution"|"file_change"|"mcp_tool_call"|"error",
     "text":…, "command":"/bin/bash -lc '…'", "aggregated_output":…, "exit_code":0|null,
     "status":"in_progress|completed|failed", "changes":[{path,kind}], "message":…}}
  {"type":"turn.completed","usage":{input_tokens, cached_input_tokens, cache_write_input_tokens,
     output_tokens, reasoning_output_tokens}}
  {"type":"error","message":"Reconnecting... 2/5 (stream disconnected before completion: Connection refused …)"}
  {"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: …"}}

Session id — thread.started.thread_id (the first event, so the core links it right away); resume is
`codex exec resume <thread_id>`. Tokens — one total per turn in turn.completed (input_tokens already
include the prompt cache, so the context is input_tokens); money is None: a ChatGPT subscription
reports no prices and no quota numbers.

Non-interactive: `-c approval_policy="never"` (the flag that keeps exec from waiting for a human) plus
stdin=DEVNULL, which the shared runner already gives the process. `-s workspace-write` is the OS sandbox
(Landlock on Linux, Seatbelt on macOS — in 0.153.4 it is bubblewrap around it): the tool may read
everything but write only the working copy, and it never asks. It is worth checking that the sandbox
works on the host (`codex sandbox <mode> -- true`): if it cannot initialize, codex silently fails every
command and the turn comes out empty — health() reports it as a problem.

Errors (real forms): 400 with an unsupported model for a ChatGPT account, 401 without a login
(`turn.failed`), reconnects on a dead network (`error` events, then it keeps retrying — the silence
watchdog and the timeout end the step, the runner turns it into TRANSIENT). No export and no session
search — the rollout files under ~/.codex/sessions are not read; those capabilities are honestly absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ahub import log as hublog
from ahub.i18n import t as _t
from ahub.providers.base import (Act, Activity, Cap, Health, ModelInfo, Outcome, Provider, RunSpec, Usage,
                                  clip)
from ahub.providers.opencode import extract_json, prompt_arg, run_capture  # shared bits

_log = hublog.get("codex")

TRANSIENT_MARKERS = ("stream disconnected", "connection refused", "connection reset", "connection closed",
                     "error sending request", "failed to connect", "waiting for network", "reconnecting",
                     "timed out", "timeout", "temporarily unavailable", "service unavailable", "overloaded",
                     "internal server error", "bad gateway", "gateway timeout", "dns", "econnreset",
                     "econnrefused", "network")
QUOTA_MARKERS = ("usage limit", "usage_limit", "rate limit", "rate_limit", "too many requests", "429",
                 "quota", "credits exhausted", "credit balance", "limit reached", "usage_limited",
                 "spend_control")
NO_ACCESS_MARKERS = ("401", "unauthorized", "403", "forbidden", "not logged in", "please log in",
                     "please sign in", "run codex login", "missing bearer", "no api key", "invalid api key",
                     "authentication", "permission denied", "not supported when using codex with a chatgpt account")
RESUME_GONE_MARKER = "no rollout found"  # stderr: `thread/resume failed: no rollout found for thread id …`
_STATUS = re.compile(r"\b(?:status|code|http|error)\W{0,8}(\d{3})\b", re.IGNORECASE)
_TOOL_TYPES = {"command_execution", "file_change", "mcp_tool_call", "web_search", "patch_apply",
               "tool_call", "dynamic_tool"}
_SANDBOX_TIMEOUT_S = 30


def codex_bin() -> str:
    """codex executable: which → ~/.local/bin/codex."""
    return shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")


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


def error_text(raw) -> tuple[str, dict]:
    """`{"message": …}` / a plain string, with a JSON error body inside → (text, flags)."""
    if isinstance(raw, dict):
        parts = [str(raw[k]) for k in ("message", "error", "text", "detail") if isinstance(raw.get(k), str)]
        raw = " | ".join(p for p in parts if p.strip())
    text = str(raw or "")
    inner = text.strip()
    if inner.startswith("{"):  # `{"type":"error","status":400,"error":{…,"message":"…"}}`
        try:
            data = json.loads(inner)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            msg = str(err.get("message") or data.get("message") or "")
            status = data.get("status")
            parts = [f"status {status}" if isinstance(status, int) else "", msg]
            text = ": ".join(p for p in parts if p) or text
    return clip(text, 2000), classify_text(text)


def usage_of(raw) -> Usage | None:
    """codex turn usage dict → Usage (context — input_tokens, the prompt cache is already inside)."""
    if not isinstance(raw, dict):
        return None
    tin, tout = raw.get("input_tokens"), raw.get("output_tokens")
    if not isinstance(tin, int) and not isinstance(tout, int):
        return None

    def num(v):
        return v if isinstance(v, int) else None

    return Usage(tokens_in=num(tin), tokens_out=num(tout), tokens_reasoning=num(raw.get("reasoning_output_tokens")),
                 cache_read=num(raw.get("cached_input_tokens")), cache_write=num(raw.get("cache_write_input_tokens")),
                 context=tin or None)


def parse_models(out: str) -> list[ModelInfo]:
    """`codex debug models` output: {"models":[{slug, display_name, visibility, supported_reasoning_levels…}]}.

    Hidden models (visibility "hide") are skipped — they are not offered for a turn.
    """
    try:
        data = json.loads(out or "")
    except json.JSONDecodeError:
        return []
    models = data.get("models") if isinstance(data, dict) else None
    out_models: list[ModelInfo] = []
    if not isinstance(models, list):
        return out_models
    for m in models:
        if not isinstance(m, dict):
            continue
        slug = str(m.get("slug") or "").strip()
        if not slug or str(m.get("visibility") or "list") == "hide":
            continue
        levels = m.get("supported_reasoning_levels")
        variants = tuple(str(r["effort"]) for r in levels if isinstance(r, dict) and r.get("effort")) \
            if isinstance(levels, list) else ()
        # a ChatGPT subscription: no per-token prices and no quota numbers in the stream
        out_models.append(ModelInfo(slug, variants=variants, counter="quota",
                                    note=str(m.get("display_name") or "")))
    return out_models


def _tool_input(item: dict) -> dict:
    """Short tool arguments for the log/phase (full command, first changed paths)."""
    data: dict = {}
    cmd = item.get("command")
    if isinstance(cmd, str) and cmd:
        data["command"] = clip(cmd, 500)
    changes = item.get("changes")
    if isinstance(changes, list) and changes:
        data["paths"] = [str(c.get("path"))[:200] for c in changes if isinstance(c, dict) and c.get("path")][:20]
    for key in ("server", "tool", "name", "query"):
        v = item.get(key)
        if isinstance(v, str) and v:
            data[key if key != "name" else "tool"] = v[:200]
    return data


def _schema_file(schema: dict, cwd: str) -> str:
    """Schema for `--output-schema` — a file (unique name: parallel sessions don't clobber)."""
    d = Path(cwd) / ".ahub"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"schema_{int(time.time() * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _in_git_repo(cwd: str) -> bool:
    """Is cwd inside a git work tree? `.git` is a directory in a clone and a file in a worktree."""
    return (Path(cwd) / ".git").exists()


def _outcome_of(flags: dict) -> Outcome:
    for key, outcome in (("no_access", Outcome.NO_ACCESS), ("quota", Outcome.QUOTA),
                         ("transient", Outcome.TRANSIENT)):
        if flags.get(key):
            return outcome
    return Outcome.MODEL_ERROR


class CodexProvider(Provider):
    name = "codex"
    # Honestly absent: export, find_session, cost_money/cost_quota (a subscription, codex reports no
    # prices), so usage is tokens only.
    capabilities = frozenset({Cap.RESUME, Cap.STREAM, Cap.STRUCTURED, Cap.TOKENS, Cap.ACTIVE_TOOL,
                              Cap.CATALOG, Cap.HEALTH})

    def __init__(self, binary: str | None = None, env: dict[str, str] | None = None,
                 sandbox: str = "workspace-write", approvals: str = "never") -> None:
        self.binary = binary
        self.extra_env = dict(env or {})  # environment for helper calls (models/--version/login status)
        self.sandbox = sandbox  # "" — no -s (the user's config decides); this is the isolation, keep it
        self.approvals = approvals  # "never" — exec must not wait for a human

    def _bin(self) -> str:
        return self.binary or codex_bin()

    # --- start ---

    def build_command(self, spec: RunSpec) -> list[str]:
        cmd = [self._bin(), "exec"]
        if spec.session_id:
            cmd.append("resume")
        cmd += ["--json", "-m", spec.model_id]
        if spec.variant:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(spec.variant)}"]
        if self.approvals:
            cmd += ["-c", f"approval_policy={json.dumps(self.approvals)}"]
        if spec.session_id:
            # `exec resume` has no -s/-C: the sandbox comes from -c, the cwd from the process
            if self.sandbox:
                cmd += ["-c", f"sandbox_mode={json.dumps(self.sandbox)}"]
            cmd += [spec.session_id]
        else:
            if self.sandbox:
                cmd += ["-s", self.sandbox]
            cmd += ["-C", spec.cwd]
            if not _in_git_repo(spec.cwd):
                cmd.append("--skip-git-repo-check")
        if spec.schema is not None:
            cmd += ["--output-schema", _schema_file(spec.schema, spec.cwd)]
        cmd.append(prompt_arg(spec.prompt, spec.cwd))
        return cmd

    def env(self, spec: RunSpec) -> dict[str, str]:
        """Isolation: the hub inside a worker session writes to its own dir, not the live storage.

        HOME stays real — codex reads the login from ~/.codex there (same as for opencode).
        """
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
        kind = ev.get("type")
        if kind == "thread.started":
            tid = str(ev.get("thread_id") or "")
            return [Activity(Act.SESSION, now, text=tid)] if tid else []
        if kind == "turn.started":
            return [Activity(Act.STEP, now, data={"edge": "start"})]
        if kind == "turn.completed":
            out = [Activity(Act.STEP, now, data={"edge": "finish"})]
            u = usage_of(ev.get("usage"))
            if u is not None:
                out.append(Activity(Act.USAGE, now, data={"usage": u, "final": True}))
            return out
        if kind == "turn.failed":
            text, flags = error_text(ev.get("error"))
            return [Activity(Act.ERROR, now, text=text or _t("codex.turn_failed"),
                             data={**flags, "terminal": True})]
        if kind == "error":
            text, flags = error_text(ev.get("message"))
            return [Activity(Act.ERROR, now, text=text, data=flags)] if text else []
        if kind in ("item.started", "item.completed"):
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            return self._item(item, now, started=kind == "item.started")
        return [Activity(Act.OTHER, now, data={"type": str(kind)})] if kind else []

    def _item(self, item: dict, now: int, *, started: bool) -> list[Activity]:
        itype = str(item.get("type") or "")
        if itype == "agent_message":
            text = str(item.get("text") or "")
            return [Activity(Act.TEXT, now, text=text)] if text and not started else []
        if itype == "reasoning":
            text = str(item.get("text") or "")
            return [Activity(Act.REASONING, now, text=clip(text, 500))] if text and not started else []
        if itype == "error":
            text, flags = error_text(item.get("message"))
            return [Activity(Act.ERROR, now, text=text or _t("codex.error_item"), data=flags)]
        if itype in _TOOL_TYPES:
            data = {"input": _tool_input(item), "status": str(item.get("status") or "")}
            if item.get("exit_code") is not None:
                data["exit_code"] = item.get("exit_code")
            kind = Act.TOOL_START if started else Act.TOOL_END
            return [Activity(kind, now, tool=itype, data=data)]
        if itype:
            return [Activity(Act.OTHER, now, data={"type": itype})]
        return []

    def classify(self, *, exit_code: int | None, activities: list[Activity], session_id: str | None,
                 stderr_tail: str) -> tuple[Outcome, str]:
        """turn.failed decides the turn (codex exits 1 for it); reconnect spam stays transient."""
        terminal = next((a for a in activities if a.kind is Act.ERROR and a.data.get("terminal")), None)
        if terminal is not None:
            return _outcome_of(terminal.data), terminal.text[:2000]
        if RESUME_GONE_MARKER in (stderr_tail or "").lower():
            _log.warning("codex: session not found — a new session was opened instead of a resume")
        return super().classify(exit_code=exit_code, activities=activities, session_id=session_id,
                                stderr_tail=stderr_tail)

    def stream_usage(self, activities: list[Activity]) -> Usage | None:
        """One exec = one turn: turn.completed usage is the total of the turn, not a step sum."""
        for a in reversed(activities):
            u = a.data.get("usage") if a.kind is Act.USAGE else None
            if isinstance(u, Usage):
                return u
        return None

    def structured(self, final_text: str, activities: list[Activity], schema: dict | None) -> dict | None:
        """--output-schema: codex itself puts the parsed object into the last agent_message."""
        return extract_json(final_text)

    # --- provider data ---

    def catalog(self) -> list[ModelInfo]:
        try:
            rc, out, _err = run_capture([self._bin(), "debug", "models"], timeout=60, env=self.extra_env)
        except (OSError, subprocess.SubprocessError) as e:
            _log.warning("models: %s", e)
            return []
        if rc != 0:
            _log.warning("models: code %s", rc)
            return []
        return parse_models(out)

    def login(self) -> tuple[bool, str]:
        """`codex login status` → (logged in, what it said). Empty output without rc means unknown."""
        try:
            rc, out, err = run_capture([self._bin(), "login", "status"], timeout=30, env=self.extra_env)
        except (OSError, subprocess.SubprocessError) as e:
            _log.warning("login status: %s", e)
            return False, str(e)
        text = (out or err or "").strip().splitlines()
        said = text[-1][:80] if text else ""
        return rc == 0, said

    def sandbox_ok(self) -> tuple[bool, str]:
        """Can the OS sandbox start at all? codex fails commands silently when it cannot (checked
        live 2026-10-03 inside a container: bubblewrap has no uid map → every command fails)."""
        if not self.sandbox:
            return True, ""
        try:
            rc, _out, err = run_capture([self._bin(), "sandbox", self.sandbox, "--", "true"],
                                        timeout=_SANDBOX_TIMEOUT_S, env=self.extra_env)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        if rc == 0:
            return True, ""
        first = next((ln.strip() for ln in (err or "").splitlines() if ln.strip()), "")
        return False, clip(first or f"exit {rc}", 200)

    def health(self) -> Health:
        problems: list[str] = []
        details: dict = {}
        binary = self._bin()
        if not os.access(binary, os.X_OK):
            return Health(False, (_t("codex.no_binary", binary=binary),))
        try:
            rc, out, _err = run_capture([binary, "--version"], timeout=30, env=self.extra_env)
            details["version"] = out.strip()[:40]
            if rc != 0:
                problems.append(_t("codex.version_fail", code=rc))
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(_t("codex.no_answer", err=e))
        logged_in, said = self.login()
        details["login"] = said
        if not logged_in:
            problems.append(_t("codex.no_login"))
        models = self.catalog()
        details["models"] = len(models)
        if not models:
            problems.append(_t("codex.no_models"))
        ok, why = self.sandbox_ok()
        details["sandbox"] = self.sandbox if ok else f"{self.sandbox}: {why}"
        if not ok:  # commands would fail silently — the worker would do nothing
            problems.append(_t("codex.sandbox_broken", mode=self.sandbox, err=why))
        return Health(not problems, tuple(problems), details)
