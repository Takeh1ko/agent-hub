"""Раннеры моделей: opencode и agy. Порт run_task.py без сети в тестах.

Контракт: каждый старт линкуется в store СРАЗУ при появлении sessionID
в stdout (колбэк `on_session`), а не только после конца шага — иначе
пульс/roster не видят сессию во время долгого шага (живой запуск H08).
Вызывающая сторона (cycle) передаёт `on_session=lambda sid:
store.link_session(...)`; без колбэка поведение прежнее (id возвращается
в конце).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
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
    """Общий интерфейс: старт сессии и продолжение.

    `on_session` — колбэк линка в store: opencode зовёт его сразу при
    появлении sessionID в stdout (во время шага), agy — когда id известен.
    Ошибки колбэка глотаются.
    """

    def start(self, prompt: str, cwd: str, log: str | None = None, *,
              on_session: Callable[[str], None] | None = None) -> str:
        ...

    def resume(self, session_id: str, prompt: str, cwd: str,
               log: str | None = None, *,
               on_session: Callable[[str], None] | None = None) -> str:
        ...


def _default_log(cwd: str, name: str) -> str:
    path = Path(cwd) / AGENT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _close_quietly(stream) -> None:
    """Закрыть пайп процесса; best effort (фейки в тестах без close — мимо)."""
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


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
    """`opencode run --format json`: id — поле sessionID в событиях.

    stdout читается потоково (Popen): как только в очередной JSON-строке
    появляется sessionID, он сразу отдаётся в `on_session` — вызывающая
    сторона линкует его в store во время шага, не дожидаясь конца.
    """

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
              log: str | None, *,
              on_session: Callable[[str], None] | None = None) -> str:
        """Запустить opencode, вернуть sessionID.

        `on_session(sid)` вызывается СРАЗУ, как только sid виден в stdout
        (ещё до конца процесса) — для линка в store во время шага.
        Ошибки колбэка глотаются: линк — best effort, id всё равно вернётся.
        """
        notify = on_session
        log_path = log or _default_log(cwd, "opencode.log")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(
                self._cmd(prompt, cwd, session_id), cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except OSError as e:
            raise RuntimeError(f"opencode: не запустился (лог {log_path}): {e}") from e
        stdout_lines: list[str] = []
        stderr_box: list[str] = [""]
        sid_box: list[str | None] = [None]
        notified_box: list[bool] = [False]

        def _maybe_notify(cand: str | None) -> None:
            if cand and not notified_box[0]:
                notified_box[0] = True
                sid_box[0] = cand
                if notify is not None:
                    try:
                        notify(cand)
                    except Exception:
                        pass

        def _sid_of_line(line: str) -> str | None:
            if '"sessionID"' not in line:
                return None
            try:
                data = json.loads(line.strip())
            except json.JSONDecodeError:
                return _extract_session_id(line)
            sid = data.get("sessionID") if isinstance(data, dict) else None
            if sid:
                return str(sid)
            return _extract_session_id(line)

        def _read_stdout() -> None:
            try:
                stream = proc.stdout
                if stream is None:
                    return
                for line in stream:
                    stdout_lines.append(line)
                    if sid_box[0] is None:
                        _maybe_notify(_sid_of_line(line))
            except Exception:
                pass

        def _read_stderr() -> None:
            try:
                stream = proc.stderr
                if stream is None:
                    return
                stderr_box[0] = stream.read() or ""
            except Exception:
                pass

        import threading as _th

        t_out = _th.Thread(target=_read_stdout, daemon=True)
        t_err = _th.Thread(target=_read_stderr, daemon=True)
        t_out.start()
        t_err.start()
        try:
            try:
                proc.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired as e:
                try:
                    proc.kill()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=10)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                raise RuntimeError(f"opencode: таймаут {self.timeout_s} c (лог {log_path})") from e
            t_out.join(timeout=10)
            t_err.join(timeout=10)
        finally:
            # Пайпы закрыть явно: в долгоживущем воркере иначе висят fd до GC.
            _close_quietly(proc.stdout)
            _close_quietly(proc.stderr)
        stdout_text = "".join(stdout_lines)
        stderr_text = stderr_box[0]
        try:
            rc = proc.returncode if proc.returncode is not None else 0
        except (AttributeError, ValueError):
            rc = 0
        combined = stdout_text + ("\n" + stderr_text if stderr_text else "")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(combined)
        except OSError:
            pass
        sid = sid_box[0] or _extract_session_id(stdout_text)
        if sid:
            return sid
        if session_id:
            return session_id
        raise RuntimeError(
            f"opencode: нет sessionID в выводе (exit {rc}, лог {log_path}): "
            + combined[-2000:])

    def start(self, prompt: str, cwd: str, log: str | None = None, *,
              on_session: Callable[[str], None] | None = None) -> str:
        return self._call(prompt, cwd, None, log, on_session=on_session)

    def resume(self, session_id: str, prompt: str, cwd: str,
               log: str | None = None, *,
               on_session: Callable[[str], None] | None = None) -> str:
        return self._call(prompt, cwd, session_id, log, on_session=on_session)


class AgyRunner:
    """`agy -p …`: id — conversation_id; запускать с cwd = worktree."""

    tool = "agy"

    def __init__(self, timeout_s: int = DEFAULT_TIMEOUT_S,
                 model: str = GEMINI_MODEL) -> None:
        self.timeout_s = timeout_s
        self.model = model

    def _call(self, prompt: str, cwd: str, session_id: str | None,
              log: str | None, *,
              on_session: Callable[[str], None] | None = None) -> str:
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
            result = cid
        elif session_id:
            result = session_id
        else:
            newest = _newest_agy_conversation()
            if not newest or newest == before:
                raise RuntimeError(
                    f"agy: нет conversation_id в выводе (exit {r.returncode}, лог {log_path}): "
                    + combined[-2000:])
            result = newest
        if on_session is not None:
            try:
                on_session(result)
            except Exception:
                pass
        return result

    def start(self, prompt: str, cwd: str, log: str | None = None, *,
              on_session: Callable[[str], None] | None = None) -> str:
        return self._call(prompt, cwd, None, log, on_session=on_session)

    def resume(self, session_id: str, prompt: str, cwd: str,
               log: str | None = None, *,
               on_session: Callable[[str], None] | None = None) -> str:
        return self._call(prompt, cwd, session_id, log, on_session=on_session)


def make_runner(name: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> OpencodeRunner | AgyRunner:
    """Раннер по короткому имени из MODELS."""
    if name not in MODELS:
        raise ValueError(f"неизвестная модель: {name}")
    model, variant = MODELS[name]
    if name == "gemini":
        return AgyRunner(timeout_s=timeout_s, model=model)
    return OpencodeRunner(model=model, variant=variant, timeout_s=timeout_s)
