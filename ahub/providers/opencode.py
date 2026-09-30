"""Поставщик opencode: `opencode run --format json` + данные из opencode.db (учёт, пульс, поиск сессии).

События stdout (проверено на живых логах 2026-09-30), у каждого есть sessionID:
  step_start / step_finish (part.tokens, part.cost) / tool_use (только завершённые: state.status completed|error) /
  text (part.text) / reasoning / error (error: строка или {name, data: {message, statusCode, isRetryable}}).
Идущий инструмент в потоке не виден — его даёт opencode.db (session_state).
Ошибки (перенос правил v1 H13 + реальные формы): 5xx, isRetryable, «Unexpected server error», сетевые — сбой сети;
401/403 — нет доступа; «quota/limit exceeded/insufficient» — квота; остальное — ошибка модели.
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
from ahub.providers.base import (Act, Activity, Cap, Health, ModelInfo, Provider, RunSpec, SessionState, Usage)

PROMPT_ARG_LIMIT = 60_000  # байт; лимит одного аргумента Linux — 128 КБ
_log = hublog.get("opencode")

TRANSIENT_MARKERS = ("unexpected server error", "cannot connect to api", "unable to connect", "econnrefused",
                     "etimedout", "econnreset", "socket hang up", "temporarily overloaded", "service unavailable",
                     "bad gateway", "gateway timeout", "fetch failed", "network error")
QUOTA_MARKERS = ("quota", "limit exceeded", "usage limit", "insufficient", "credit", "billing")
_STATUS = re.compile(r"\bstatus(?:code)?\D{0,3}(\d{3})\b", re.IGNORECASE)


def opencode_bin() -> str:
    return shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")


def run_capture(cmd: list[str], timeout: int = 120, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Запустить и забрать вывод через файл: в пайп opencode теряет хвост вывода при выходе."""
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
    """Событие {"type":"error"} → (текст, флаги transient/quota/no_access)."""
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
    elif any(m in low for m in QUOTA_MARKERS) and status in (None, 402, 429):
        flags["quota"] = True
    elif retryable or (isinstance(status, int) and (status >= 500 or status == 429)) \
            or any(m in low for m in TRANSIENT_MARKERS):
        flags["transient"] = True
    return text, flags


def prompt_arg(prompt: str, cwd: str) -> str:
    """Короткий промпт — как есть; длинный — в файл (уникальное имя: параллельные сессии не затирают)."""
    if len(prompt.encode("utf-8")) <= PROMPT_ARG_LIMIT:
        return prompt
    d = Path(cwd) / ".ahub"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"prompt_{int(time.time() * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:8]}.md"
    path.write_text(prompt, encoding="utf-8")
    return (f"Твоё задание целиком — в файле {path}. Прочитай его полностью (он длинный, читай по частям "
            f"до конца) и выполни.")


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
    capabilities = frozenset({Cap.RESUME, Cap.STREAM, Cap.TOKENS, Cap.COST_MONEY, Cap.ACTIVE_TOOL, Cap.EXPORT,
                              Cap.CATALOG, Cap.HEALTH, Cap.FIND_SESSION})

    def __init__(self, db_path: str | None = None, binary: str | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.db_path = db_path  # None — opencode_db.default_db() при каждом вызове
        self.binary = binary
        self.extra_env = dict(env or {})  # окружение служебных команд (export/models/--version)

    def _bin(self) -> str:
        return self.binary or opencode_bin()

    # --- запуск ---

    def build_command(self, spec: RunSpec) -> list[str]:
        cmd = [self._bin(), "run", "--format", "json", "--model", spec.model_id, "--dir", spec.cwd]
        if spec.variant:
            cmd += ["--variant", spec.variant]
        if spec.session_id:
            cmd += ["--session", spec.session_id]
        cmd.append(prompt_arg(spec.prompt, spec.cwd))
        return cmd

    def env(self, spec: RunSpec) -> dict[str, str]:
        """Изоляция: хаб внутри сессии работника пишет в свой каталог, не в боевое хранилище."""
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
                                data={"status": status, "input": _short_input(state.get("input"))}))
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
        """opencode не умеет схему — ищем JSON в последнем ответе (блок ```json или весь текст)."""
        return extract_json(final_text)

    # --- данные поставщика (opencode.db) ---

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
            _log.warning("export %s: код %s: %s", session_id, rc, err[-300:])
            return None
        start = out.find("{")
        try:
            data = json.loads(out[start:]) if start >= 0 else None
        except json.JSONDecodeError:
            _log.warning("export %s: не JSON", session_id)
            return None
        return data if isinstance(data, dict) else None

    def catalog(self) -> list[ModelInfo]:
        try:
            rc, out, _err = run_capture([self._bin(), "models", "--verbose"], env=self.extra_env)
        except (OSError, subprocess.SubprocessError) as e:
            _log.warning("models: %s", e)
            return []
        if rc != 0:
            _log.warning("models: код %s", rc)
            return []
        return parse_models_verbose(out)

    def health(self) -> Health:
        problems: list[str] = []
        details: dict = {}
        binary = self._bin()
        if not os.access(binary, os.X_OK):
            return Health(False, (f"нет исполняемого opencode ({binary})",))
        try:
            rc, out, _err = run_capture([binary, "--version"], timeout=30, env=self.extra_env)
            details["version"] = out.strip()[:40]
            if rc != 0:
                problems.append(f"opencode --version: код {rc}")
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"opencode не отвечает: {e}")
        from ahub.providers import opencode_db

        st = opencode_db.check_schema(self.db_path)
        if not st.ok:
            problems.extend(f"opencode.db: {p}" for p in st.problems)
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


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """Последний JSON-объект из ответа модели: блок ```json … ``` или весь текст."""
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


def parse_models_verbose(out: str) -> list[ModelInfo]:
    """Вывод `opencode models --verbose`: строка «провайдер/модель», за ней JSON-объект."""
    models: list[ModelInfo] = []
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
        free = name.endswith("-free") or (pin == 0 and pout == 0)
        counter = "free" if free else ("go" if provider_id == "opencode-go" else "usd")
        models.append(ModelInfo(name, variants, counter=counter,
                                price_in=pin if isinstance(pin, (int, float)) else None,
                                price_out=pout if isinstance(pout, (int, float)) else None,
                                note=str(info.get("status", ""))))
        i = j if j > i + 1 else i + 1
    return models
