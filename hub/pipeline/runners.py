"""Раннеры моделей: opencode и agy. Порт run_task.py без сети в тестах.

Контракт: каждый старт сразу линкуется в store вызывающей стороной
(`cycle` вызывает `store.link_session` следующей строкой после старта,
как только id известен) — иначе пульс/roster не видят сессию.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

MUSE_MODEL = "opencode-go/muse-spark-1.3-contributor"
MUSE_VARIANT = "xhigh"
GEMINI_MODEL = "gemini-3.8-flash-high"

# Короткое имя → (полная модель, variant). Как в run_task.py.
MODELS: dict[str, tuple[str, str | None]] = {
    "muse": (MUSE_MODEL, MUSE_VARIANT),
    "musefree": ("opencode/muse-spark-1.3-contributor-free", MUSE_VARIANT),
    "mimoflash": ("opencode-go/mimo-v2.6-flash", "high"),
    "mimo": ("opencode-go/mimo-v2.6-pro", "high"),
    "mimofree": ("opencode/mimo-v2.6-flash-free", "high"),
    "deepseek": ("opencode-go/deepseek-v4.1-flash", "high"),
    "glm": ("opencode-go/glm-5.3-flash", "high"),
    "gemini": (GEMINI_MODEL, None),
}

OPENCODE_BIN = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")
AGY_BIN = shutil.which("agy") or "agy"

PROMPT_ARG_LIMIT = 60_000  # байт; лимит одного аргумента Linux 128 КБ
DEFAULT_TIMEOUT_S = 90 * 60

AGENT_DIR = ".agent"


class Runner(Protocol):
    """Общий интерфейс: старт сессии и продолжение."""

    def start(self, prompt: str, cwd: str, log: str | None = None) -> str:
        ...

    def resume(self, session_id: str, prompt: str, cwd: str, log: str | None = None) -> str:
        ...


def _default_log(cwd: str, name: str) -> str:
    path = Path(cwd) / AGENT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def prompt_arg(prompt: str, cwd: str) -> str:
    """Короткий промпт — как есть; длинный — в файл .agent/.

    Имя файла с pid/uuid: два параллельных ревьюера в одну мс
    не перезаписывают чужой промпт.
    """
    if len(prompt.encode("utf-8")) <= PROMPT_ARG_LIMIT:
        return prompt
    import os as _os
    import uuid as _uuid

    d = Path(cwd) / AGENT_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"prompt_{int(time.time() * 1000)}_{_os.getpid()}_{_uuid.uuid4().hex[:8]}.md"
    path.write_text(prompt, encoding="utf-8")
    return (
        f"Твоё задание целиком — в файле {path}. Прочитай его полностью (он длинный, "
        f"читай по частям до конца) и выполни."
    )


def _extract_session_id(log_text: str) -> str | None:
    for line in log_text.splitlines():
        line = line.strip()
        if '"sessionID"' not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = data.get("sessionID")
        if sid:
            return str(sid)
    return None


def _agy_conversation_id(out: str) -> str | None:
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    cid = data.get("conversation_id")
    return str(cid) if cid else None


def _newest_agy_conversation() -> str | None:
    conv = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    if not conv.is_dir():
        return None
    try:
        dbs = sorted(conv.glob("*.db"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return dbs[-1].stem if dbs else None


class OpencodeRunner:
    """`opencode run --format json`: id — поле sessionID в событиях."""

    tool = "opencode"

    def __init__(
        self,
        model: str = MUSE_MODEL,
        variant: str | None = MUSE_VARIANT,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.model = model
        self.variant = variant
        self.timeout_s = timeout_s

    def _cmd(self, prompt: str, cwd: str, session_id: str | None) -> list[str]:
        cmd = [OPENCODE_BIN, "run", "--format", "json",
               "--model", self.model, "--dir", cwd]
        if self.variant:
            cmd += ["--variant", self.variant]
        if session_id:
            cmd += ["--session", session_id]
        cmd.append(prompt_arg(prompt, cwd))
        return cmd

    def _call(self, prompt: str, cwd: str, session_id: str | None,
              log: str | None) -> str:
        log_path = log or _default_log(cwd, "opencode.log")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run(
                self._cmd(prompt, cwd, session_id), cwd=cwd,
                capture_output=True, text=True, timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"opencode: таймаут {self.timeout_s} c (лог {log_path})") from e
        combined = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(combined)
        except OSError:
            pass
        sid = _extract_session_id(r.stdout or "")
        if sid:
            return sid
        if session_id:
            return session_id
        raise RuntimeError(
            f"opencode: нет sessionID в выводе (exit {r.returncode}, лог {log_path}): "
            + combined[-2000:])

    def start(self, prompt: str, cwd: str, log: str | None = None) -> str:
        return self._call(prompt, cwd, None, log)

    def resume(self, session_id: str, prompt: str, cwd: str,
               log: str | None = None) -> str:
        return self._call(prompt, cwd, session_id, log)


class AgyRunner:
    """`agy -p …`: id — conversation_id; запускать с cwd = worktree."""

    tool = "agy"

    def __init__(self, timeout_s: int = DEFAULT_TIMEOUT_S,
                 model: str = GEMINI_MODEL) -> None:
        self.timeout_s = timeout_s
        self.model = model

    def _call(self, prompt: str, cwd: str, session_id: str | None,
              log: str | None) -> str:
        log_path = log or _default_log(cwd, "agy.log")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        before = _newest_agy_conversation()
        cmd = ["-p", prompt_arg(prompt, cwd), "--model", self.model,
               "--output-format", "json",
               "--dangerously-skip-permissions"]
        if session_id:
            cmd += ["--conversation", session_id]
        try:
            r = subprocess.run(
                [AGY_BIN, *cmd], cwd=cwd,
                capture_output=True, text=True, timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"agy: таймаут {self.timeout_s} c (лог {log_path})") from e
        combined = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(combined)
        except OSError:
            pass
        cid = _agy_conversation_id(r.stdout or "")
        if cid:
            return cid
        if session_id:
            return session_id
        newest = _newest_agy_conversation()
        if newest and newest != before:
            return newest
        raise RuntimeError(
            f"agy: нет conversation_id в выводе (exit {r.returncode}, лог {log_path}): "
            + combined[-2000:])

    def start(self, prompt: str, cwd: str, log: str | None = None) -> str:
        return self._call(prompt, cwd, None, log)

    def resume(self, session_id: str, prompt: str, cwd: str,
               log: str | None = None) -> str:
        return self._call(prompt, cwd, session_id, log)


def make_runner(name: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> OpencodeRunner | AgyRunner:
    """Раннер по короткому имени из MODELS."""
    if name not in MODELS:
        raise ValueError(f"неизвестная модель: {name}")
    model, variant = MODELS[name]
    if name == "gemini":
        return AgyRunner(timeout_s=timeout_s, model=model)
    return OpencodeRunner(model=model, variant=variant, timeout_s=timeout_s)
