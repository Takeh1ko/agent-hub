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
import os
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
DEFAULT_IDLE_S = 900  # сторож тишины бесплатного Spark: нет JSON-событий N c
_IDLE_POLL_S = 0.2  # шаг опроса процесса сторожем

AGENT_DIR = ".agent"
HUBHOME_DIR = "hubhome"


class TransientError(RuntimeError):
    """Сбой сети/сервера opencode: повтор, а не ответ модели.

    `session_id` — sid, уже увиденный в stdout до сбоя (если есть):
    исполнитель повторяет ту же сессию (`--session`), ревьюер — новой.
    """

    def __init__(self, msg: str = "", session_id: str | None = None) -> None:
        super().__init__(msg)
        try:
            sid = str(session_id).strip() if session_id else None
        except (AttributeError, ValueError, TypeError):
            sid = None
        self.session_id: str | None = sid or None


# Подстроки транзиентного сбоя (п.1 H13, регистронезависимо).
TRANSIENT_MARKERS = (
    "unexpected server error",
    "cannot connect to api",
    "unable to connect",
    "econnrefused",
    "etimedout",
    "socket hang up",
    "status 5",
    "429",
)


def is_transient_text(text: str) -> bool:
    """Текст похож на сбой сети/сервера opencode (п.1 H13)."""
    try:
        low = str(text or "").lower()
    except Exception:
        return False
    return any(m in low for m in TRANSIENT_MARKERS)


def _error_field_texts(data: dict) -> list[str]:
    """Строковые тексты ошибки из JSON-события (только поля ошибки).

    Проверяем только текст ошибки, а не всю JSON-строку: постороннее
    «429» в других полях (например, `took 429ms`) — не транзиент.
    """
    out: list[str] = []
    try:
        keys = ("error", "message", "text", "details")
    except Exception:
        return out
    for key in keys:
        try:
            val = data.get(key)
        except (AttributeError, ValueError):
            continue
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
        elif isinstance(val, dict):
            try:
                for sub in val.values():
                    if isinstance(sub, str) and sub.strip():
                        out.append(sub.strip())
            except (AttributeError, ValueError):
                continue
        elif isinstance(val, list):
            try:
                for sub in val:
                    if isinstance(sub, str) and sub.strip():
                        out.append(sub.strip())
            except (TypeError, ValueError):
                continue
    return out


def transient_error_of_line(line: str) -> str | None:
    """Текст транзиентной ошибки из JSON-события stdout, иначе None.

    Только событие {"type": "error"}, чей текст ошибки (поля
    error/message/text/details) содержит маркеры п.1, — любая другая
    ошибка ведёт себя как раньше.
    """
    try:
        s = str(line or "")
    except Exception:
        return None
    stripped = s.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        if str(data.get("type") or "") != "error":
            return None
    except (AttributeError, ValueError):
        return None
    try:
        candidates = _error_field_texts(data)
    except (ValueError, AttributeError):
        return None
    for cand in candidates:
        try:
            if is_transient_text(cand):
                return cand[:2000]
        except (ValueError, AttributeError):
            continue
    return None


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


def agent_hubhome(cwd: str) -> Path:
    """Каталог изоляции hub.db агента: <worktree>/.agent/hubhome."""
    p = Path(cwd) / AGENT_DIR / HUBHOME_DIR
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def agent_env(cwd: str) -> dict[str, str]:
    """Env для дочерней сессии агента: копия окружения + AGENT_HUB_HOME.

    Процесс самого hub окружение не меняет — только дочерний opencode/agy.
    `Store()` внутри агента пишет в hubhome, не в боевую базу.
    """
    hubhome = agent_hubhome(cwd)
    env = dict(os.environ)
    env["AGENT_HUB_HOME"] = str(hubhome)
    return env


def _is_json_event(line: str) -> bool:
    """Строка stdout — JSON-событие opencode (сбрасывает сторож тишины)."""
    s = line.strip()
    if not s.startswith("{"):
        return False
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict)


def _has_children(pid: int | None) -> bool:
    """Есть ли живые дочерние процессы у pid (дешёво через /proc).

    Нет pid или нет /proc — детей нет (сторож вправе прервать).
    """
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        taskdir = Path(f"/proc/{pid_int}/task")
        if not taskdir.is_dir():
            return False
        for tid in taskdir.iterdir():
            try:
                txt = (tid / "children").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if txt:
                return True
        return False
    except OSError:
        return False


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
    Сторож тишины: нет JSON-событий `idle_s` c и нет дочерних процессов —
    процесс прерывается, `RuntimeError("opencode: тишина N c")`.
    Сбой сети/сервера ({"type": "error"} с текстом п.1 H13) —
    `TransientError`; исключение — rc=0 с полученным sessionID (шаг
    успешен, событие было промежуточным): возвращается sid.
    Другая ошибка — как раньше.
    Каждая сессия получает env `AGENT_HUB_HOME=<worktree>/.agent/hubhome`.
    """

    tool = "opencode"

    def __init__(
        self,
        model: str = MUSE_MODEL,
        variant: str | None = MUSE_VARIANT,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        idle_s: int = DEFAULT_IDLE_S,
    ) -> None:
        self.model = model
        self.variant = variant
        self.timeout_s = timeout_s
        try:
            self.idle_s = int(idle_s)
        except (TypeError, ValueError):
            self.idle_s = DEFAULT_IDLE_S

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
        Сторож тишины: нет JSON-событий `idle_s` c и нет дочерних процессов —
        процесс прерывается, `RuntimeError("opencode: тишина N c [sid=...]")`
        (sid — если успел появиться в stdout до тишины).
        Сбой сети/сервера (событие {"type": "error"} с текстом п.1 H13) —
        `TransientError` с этим текстом, даже если sid уже виден.
        Исключение: процесс завершился rc=0 и sessionID получен — шаг
        сделал работу (событие было промежуточным), возвращается sid,
        как для других error-событий.
        Любая другая ошибка — как раньше.
        """
        notify = on_session
        log_path = log or _default_log(cwd, "opencode.log")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            idle_env = agent_env(cwd)
        except OSError:
            idle_env = dict(os.environ)
        try:
            proc = subprocess.Popen(
                self._cmd(prompt, cwd, session_id), cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=idle_env,
            )
        except OSError as e:
            raise RuntimeError(f"opencode: не запустился (лог {log_path}): {e}") from e
        stdout_lines: list[str] = []
        stderr_box: list[str] = [""]
        sid_box: list[str | None] = [None]
        notified_box: list[bool] = [False]
        transient_box: list[str | None] = [None]
        last_event: list[float] = [time.monotonic()]

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

        def _first_transient_in(text: str) -> str | None:
            try:
                for _ln in str(text or "").splitlines():
                    try:
                        cand = transient_error_of_line(_ln)
                    except (ValueError, AttributeError):
                        continue
                    if cand:
                        return cand
            except (ValueError, AttributeError):
                return None
            return None

        def _read_stdout() -> None:
            try:
                stream = proc.stdout
                if stream is None:
                    return
                for line in stream:
                    stdout_lines.append(line)
                    try:
                        if _is_json_event(line):
                            last_event[0] = time.monotonic()
                    except Exception:
                        pass
                    if transient_box[0] is None:
                        try:
                            cand = transient_error_of_line(line)
                        except (ValueError, AttributeError):
                            cand = None
                        if cand:
                            transient_box[0] = cand
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
            idle_s = self.idle_s
            try:
                idle_s = int(idle_s)
            except (TypeError, ValueError):
                idle_s = DEFAULT_IDLE_S
            try:
                timeout_s = int(self.timeout_s)
            except (TypeError, ValueError):
                timeout_s = DEFAULT_TIMEOUT_S
            deadline = time.monotonic() + max(1, timeout_s)
            while True:
                try:
                    proc.wait(timeout=_IDLE_POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if now >= deadline:
                    try:
                        proc.kill()
                    except (OSError, ValueError):
                        pass
                    try:
                        proc.wait(timeout=10)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    t_out.join(timeout=10)
                    t_err.join(timeout=10)
                    _close_quietly(proc.stdout)
                    _close_quietly(proc.stderr)
                    partial = "".join(stdout_lines)
                    err_txt = stderr_box[0] or ""
                    combined = partial + ("\n" + err_txt if err_txt else "")
                    try:
                        with open(log_path, "w", encoding="utf-8") as f:
                            f.write(combined)
                    except OSError:
                        pass
                    # Error-событие уже в частичном stdout (потом завис) —
                    # это TransientError, а не таймаут (sid — для повтора
                    # той же сессией исполнителя).
                    transient_seen = transient_box[0] or _first_transient_in(partial)
                    if transient_seen is not None:
                        _sid = sid_box[0] or _extract_session_id(partial)
                        raise TransientError(transient_seen[:2000],
                                             session_id=_sid)
                    raise RuntimeError(
                        f"opencode: таймаут {timeout_s} c (лог {log_path})")
                if idle_s and idle_s > 0 and (now - last_event[0] >= idle_s):
                    try:
                        pid = getattr(proc, "pid", None)
                    except (AttributeError, ValueError):
                        pid = None
                    if not _has_children(pid):
                        try:
                            proc.kill()
                        except (OSError, ValueError):
                            pass
                        try:
                            proc.wait(timeout=10)
                        except (OSError, ValueError, subprocess.SubprocessError):
                            pass
                        t_out.join(timeout=10)
                        t_err.join(timeout=10)
                        secs = max(int(now - last_event[0]), int(idle_s))
                        partial = "".join(stdout_lines)
                        err_txt = stderr_box[0] or ""
                        combined = partial + ("\n" + err_txt if err_txt else "")
                        try:
                            with open(log_path, "w", encoding="utf-8") as f:
                                f.write(combined)
                        except OSError:
                            pass
                        _close_quietly(proc.stdout)
                        _close_quietly(proc.stderr)
                        # Error-событие уже в частичном stdout (потом тишина) —
                        # это TransientError, а не тишина (sid — для повтора
                        # той же сессией исполнителя).
                        transient_seen = transient_box[0] or _first_transient_in(partial)
                        if transient_seen is not None:
                            _sid = sid_box[0] or _extract_session_id(partial)
                            raise TransientError(transient_seen[:2000],
                                                 session_id=_sid)
                        # Sid из stdout до тишины — в текст ошибки: fallback
                        # в cycle продолжает ту же сессию на muse через resume.
                        known_sid = sid_box[0] or _extract_session_id(partial)
                        suffix = f" sid={known_sid}" if known_sid else ""
                        raise RuntimeError(
                            f"opencode: тишина {secs} c{suffix} (лог {log_path})")
                    # Есть дети (pytest/flock) — молчание объяснено, ждём дальше.
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
        # Транзиентный сбой сервера/сети: событие {"type": "error"} в stdout
        # с текстом п.1 — TransientError, даже если sid уже виден.
        # Но rc=0 + полученный sid — шаг сделал работу (событие было
        # промежуточным): возвращаем sid, а не отбраковываем успех
        # с повторами 120/240/480 (симметрично не-транзиентному пути ниже).
        transient_text: str | None = None
        try:
            for _line in stdout_text.splitlines():
                cand = transient_error_of_line(_line)
                if cand:
                    transient_text = cand
                    break
        except (ValueError, AttributeError):
            transient_text = None
        if transient_text is not None:
            _sid = sid_box[0] or _extract_session_id(stdout_text)
            if rc == 0 and _sid:
                return _sid
            raise TransientError(transient_text[:2000], session_id=_sid)
        sid = sid_box[0] or _extract_session_id(stdout_text)
        if sid:
            return sid
        if session_id:
            return session_id
        # Строго по контракту п.1: только событие {"type": "error"} в stdout —
        # остальное (включая голые «429»/«status 5» в хвосте) как раньше.
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
    """`agy -p …`: id — conversation_id; запускать с cwd = worktree.

    Блокирующий `subprocess.run` — потокового сторожа тишины нет
    (таймаут через `timeout_s`).
    """

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
        try:
            idle_env = agent_env(cwd)
        except OSError:
            idle_env = dict(os.environ)
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
                env=idle_env,
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


def make_runner(name: str, timeout_s: int = DEFAULT_TIMEOUT_S,
                idle_s: int = DEFAULT_IDLE_S) -> OpencodeRunner | AgyRunner:
    """Раннер по короткому имени из MODELS."""
    if name not in MODELS:
        raise ValueError(f"неизвестная модель: {name}")
    model, variant = MODELS[name]
    if name == "gemini":
        return AgyRunner(timeout_s=timeout_s, model=model)
    try:
        idle_v = int(idle_s)
    except (TypeError, ValueError):
        idle_v = DEFAULT_IDLE_S
    return OpencodeRunner(model=model, variant=variant,
                          timeout_s=timeout_s, idle_s=idle_v)
