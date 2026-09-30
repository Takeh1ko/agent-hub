"""Общий запуск сессии поставщика: процесс, поток активности, сторож тишины, таймаут, остановка, лог.

Одинаково для всех поставщиков — модуль поставщика только собирает команду и разбирает строки.

- Процесс стартует в своей группе (start_new_session): остановка убивает и его детей (тесты, замки).
- Каждая строка stdout сразу пишется в лог (сырой вывод переживает падение хаба) и разбирается
  поставщиком в Activity → on_activity; первый id сессии → on_session (вызывающий линкует его в базу сразу).
- Сторож тишины: нет строк вывода idle_s секунд и нет дочерних процессов → прервать, Outcome.SILENCE.
  Есть дети (тесты, ожидание замка) — молчание объяснено, ждём дальше.
- should_stop() — ядро просит остановиться (бюджет, команда): прервать, Outcome.KILLED.
- Если до тишины/таймаута/остановки в потоке был сбой сети — итог TRANSIENT (повтор уместнее).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ahub import log as hublog
from ahub import procs
from ahub.providers.base import Act, Activity, Cap, Outcome, Provider, RunResult, RunSpec, clip
from ahub.time import now_ms

POLL_S = 0.2
KILL_GRACE_S = 5.0
_log = hublog.get("runner")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 10.0)):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run(provider: Provider, spec: RunSpec, *,
        on_activity: Callable[[Activity], None] | None = None,
        on_session: Callable[[str], None] | None = None,
        on_start: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> RunResult:
    started = now_ms()
    log_path = spec.log_path or str(Path(spec.cwd) / ".ahub" / f"{provider.name}_{started}.log")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = provider.build_command(spec)
    env = dict(os.environ)
    env.update(provider.env(spec))
    ctx = {"provider": provider.name, "model": spec.model_id, "cwd": spec.cwd}

    log_lock = threading.Lock()
    logf = open(log_path, "a", encoding="utf-8")

    def _write(line: str) -> None:
        with log_lock:
            try:
                logf.write(line if line.endswith("\n") else line + "\n")
                logf.flush()
            except (OSError, ValueError):
                pass

    try:
        proc = subprocess.Popen(cmd, cwd=spec.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1, env=env,
                                start_new_session=True, errors="replace")
    except OSError as e:
        logf.close()
        _log.error("не запустился: %s", e, extra=ctx)
        return RunResult(Outcome.NOT_STARTED, spec.session_id, error=clip(f"{cmd[0]}: {e}"),
                         started_ms=started, ended_ms=now_ms(), log_path=log_path)
    if on_start is not None:
        try:
            on_start(proc.pid)
        except Exception:  # колбэк не должен ронять шаг
            _log.exception("on_start упал", extra=ctx)

    activities: list[Activity] = []
    acts_lock = threading.Lock()
    sid_box: list[str | None] = [None]
    last_line = [time.monotonic()]
    last_act_ms = [started]
    stderr_tail: list[str] = []

    def _emit(act: Activity) -> None:
        with acts_lock:
            activities.append(act)
        last_act_ms[0] = act.ts
        if act.kind is Act.SESSION and act.text and sid_box[0] is None:
            sid_box[0] = act.text
            if on_session is not None:
                try:
                    on_session(act.text)
                except Exception:
                    _log.exception("on_session упал", extra=ctx)
        if on_activity is not None:
            try:
                on_activity(act)
            except Exception:
                _log.exception("on_activity упал", extra=ctx)

    def _read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            last_line[0] = time.monotonic()
            _write(line)
            try:
                acts = provider.parse_line(line, now_ms())
            except Exception:
                _log.exception("parse_line упал", extra=ctx)
                acts = []
            for a in acts:
                _emit(a)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            _write("[stderr] " + line)
            stderr_tail.append(line)
            if len(stderr_tail) > 200:
                del stderr_tail[:100]

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    forced: Outcome | None = None
    silence_s = 0
    deadline = time.monotonic() + max(1, int(spec.timeout_s))
    try:
        while True:
            try:
                proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if should_stop is not None:
                try:
                    stop = bool(should_stop())
                except Exception:
                    _log.exception("should_stop упал", extra=ctx)
                    stop = False
                if stop:
                    forced = Outcome.KILLED
                    _kill_group(proc)
                    break
            if now >= deadline:
                forced = Outcome.TIMEOUT
                _kill_group(proc)
                break
            if spec.idle_s and now - last_line[0] >= spec.idle_s and not procs.has_children(proc.pid):
                silence_s = int(now - last_line[0])
                forced = Outcome.SILENCE
                _kill_group(proc)
                break
    finally:
        t_out.join(timeout=10)
        t_err.join(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    ended = now_ms()
    with acts_lock:
        acts = list(activities)
    sid = sid_box[0]
    if sid is None and provider.has(Cap.FIND_SESSION):
        try:
            sid = provider.find_session(spec.cwd, started)
        except Exception:
            _log.exception("find_session упал", extra=ctx)
    if sid is None and spec.session_id:
        sid = spec.session_id
    tail = "".join(stderr_tail)[-2000:]

    if forced is not None:
        outcome = forced
        transient = next((a for a in acts if a.kind is Act.ERROR and a.data.get("transient")), None)
        if transient is not None and forced is not Outcome.KILLED:
            outcome, error = Outcome.TRANSIENT, clip(transient.text)
        else:
            error = {Outcome.SILENCE: f"тишина {silence_s} c", Outcome.TIMEOUT: f"таймаут {spec.timeout_s} c",
                     Outcome.KILLED: "остановлено по просьбе"}[forced]
    else:
        try:
            outcome, error = provider.classify(exit_code=proc.returncode, activities=acts, session_id=sid,
                                               stderr_tail=tail)
        except Exception as e:
            _log.exception("classify упал", extra=ctx)
            outcome, error = Outcome.CRASH, clip(f"classify: {e}")

    final = provider.final_text(acts)
    structured = None
    if spec.schema is not None:
        try:
            structured = provider.structured(final, acts, spec.schema)
        except Exception:
            _log.exception("structured упал", extra=ctx)
    usage = None
    try:
        usage = provider.usage(sid) if sid and (provider.has(Cap.COST_MONEY) or provider.has(Cap.TOKENS)) else None
        if usage is None:
            usage = provider.stream_usage(acts)
    except Exception:
        _log.exception("usage упал", extra=ctx)
    logf.close()

    level = "info" if outcome is Outcome.OK else "warning"
    getattr(_log, level)("сессия: %s%s", outcome.value, f" ({error[:200]})" if error else "",
                         extra={**ctx, "session": sid, "exit": proc.returncode,
                                "secs": round((ended - started) / 1000, 1)})
    return RunResult(outcome=outcome, session_id=sid, final_text=final, structured=structured, usage=usage,
                     error=error, exit_code=proc.returncode, started_ms=started, ended_ms=ended,
                     log_path=log_path, activities=len(acts), last_activity_ms=last_act_ms[0],
                     silence_s=silence_s)
