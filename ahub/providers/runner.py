"""Общий запуск сессии поставщика: процесс, поток активности, сторож тишины, таймаут, остановка, лог.

Одинаково для всех поставщиков — модуль поставщика только собирает команду и разбирает строки.

- Процесс стартует в своей группе (start_new_session): остановка убивает и его детей (тесты, замки).
- stdout процесса пишется прямо в файл лога (не в пайп: opencode теряет хвост вывода в пайп при выходе —
  проверено 2026-09-30), поток читает файл следом; каждая строка разбирается поставщиком в Activity →
  on_activity; первый id сессии → on_session (вызывающий линкует его в базу сразу). stderr — в `<лог>.stderr`.
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
from ahub.providers.base import Act, Activity, Cap, Outcome, Provider, RunResult, RunSpec, Usage, clip
from ahub.time import now_ms

POLL_S = 0.2
TAIL_POLL_S = 0.05
MAX_LINE = 1_000_000  # байт на строку вывода; длиннее — обрезается
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


def merge_usage(a: Usage | None, b: Usage | None) -> Usage | None:
    """Учёт из данных поставщика и из потока: по каждому полю — большее (данные поставщика могут запаздывать)."""
    if a is None or b is None:
        return a if b is None else b

    def mx(x, y):
        return y if x is None else x if y is None else max(x, y)

    return Usage(*(mx(getattr(a, f), getattr(b, f)) for f in ("tokens_in", "tokens_out", "tokens_reasoning",
                                                             "cache_read", "cache_write", "cost_go", "cost_usd",
                                                             "quota")), context=a.context or b.context)


def run(provider: Provider, spec: RunSpec, *,
        on_activity: Callable[[Activity], None] | None = None,
        on_session: Callable[[str], None] | None = None,
        on_start: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> RunResult:
    started = now_ms()
    log_path = spec.log_path or str(Path(spec.cwd) / ".ahub" / f"{provider.name}_{started}.log")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = provider.build_command(spec)
    from ahub.prepare import scrub_env

    env = scrub_env(dict(os.environ))  # работнику — без токенов/паролей хаба (ключи моделей остаются)
    env.update(provider.env(spec))
    ctx = {"provider": provider.name, "model": spec.model_id, "cwd": spec.cwd}

    err_path = log_path + ".stderr"
    out_f = open(log_path, "ab")
    err_f = open(err_path, "ab")
    start_off = out_f.tell()
    try:
        proc = subprocess.Popen(cmd, cwd=spec.cwd, stdout=out_f, stderr=err_f, stdin=subprocess.DEVNULL,
                                env=env, start_new_session=True)
    except OSError as e:
        out_f.close()
        err_f.close()
        _log.error("не запустился: %s", e, extra=ctx)
        return RunResult(Outcome.NOT_STARTED, spec.session_id, error=clip(f"{cmd[0]}: {e}"),
                         started_ms=started, ended_ms=now_ms(), log_path=log_path)
    out_f.close()  # дескрипторы унаследовал процесс
    err_f.close()
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
    exited = threading.Event()

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

    def _handle(raw: bytes) -> None:
        line = raw[:MAX_LINE].decode("utf-8", "replace")
        try:
            acts = provider.parse_line(line, now_ms())
        except Exception:
            _log.exception("parse_line упал", extra=ctx)
            acts = []
        if acts:  # сторож тишины сбрасывается только распознанной активностью, не любым мусором
            last_line[0] = time.monotonic()
        for a in acts:
            _emit(a)

    def _tail() -> None:
        """Читать лог следом за процессом; после выхода — дочитать остаток."""
        buf = b""
        with open(log_path, "rb") as f:
            f.seek(start_off)
            while True:
                chunk = f.read(65536)
                if chunk:
                    buf += chunk
                    if len(buf) > MAX_LINE * 4 and b"\n" not in buf:
                        buf = buf[:MAX_LINE]  # строка без конца — не копить без предела
                    *lines, buf = buf.split(b"\n")
                    for ln in lines:
                        _handle(ln)
                    continue
                if exited.is_set():
                    rest = f.read()
                    if rest:
                        buf += rest
                        *lines, buf = buf.split(b"\n")
                        for ln in lines:
                            _handle(ln)
                        continue
                    if buf.strip():
                        _handle(buf)
                    return
                time.sleep(TAIL_POLL_S)

    t_out = threading.Thread(target=_tail, daemon=True)
    t_out.start()

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
        exited.set()
        t_out.join(timeout=30)

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
    try:
        with open(err_path, "rb") as ef:
            ef.seek(0, 2)
            ef.seek(max(0, ef.tell() - 4000))
            tail = ef.read().decode("utf-8", "replace")[-2000:]
    except OSError:
        tail = ""

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
        db_usage = provider.usage(sid) if sid and (provider.has(Cap.COST_MONEY) or provider.has(Cap.TOKENS)) else None
        usage = merge_usage(db_usage, provider.stream_usage(acts))
    except Exception:
        _log.exception("usage упал", extra=ctx)
    level = "info" if outcome is Outcome.OK else "warning"
    getattr(_log, level)("сессия: %s%s", outcome.value, f" ({error[:200]})" if error else "",
                         extra={**ctx, "session": sid, "exit": proc.returncode,
                                "secs": round((ended - started) / 1000, 1)})
    return RunResult(outcome=outcome, session_id=sid, final_text=final, structured=structured, usage=usage,
                     error=error, exit_code=proc.returncode, started_ms=started, ended_ms=ended,
                     log_path=log_path, activities=len(acts), last_activity_ms=last_act_ms[0],
                     silence_s=silence_s)
