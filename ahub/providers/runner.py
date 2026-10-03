"""Shared provider session run: process, activity stream, silence watchdog, timeout, stop, log.

Same for all providers — the provider module only builds the command and parses lines.

- The process starts in its own group (start_new_session): stopping kills it and its children (tests, locks).
- Process env: the hub environment without its secrets, plus the provider's own proxy from the hub config
  ([providers.<name>] in config.toml — the hub's own processes and the bot keep the hub environment).
- Process stdout goes straight to the log file (not a pipe: opencode drops the tail output to a pipe on exit —
  verified 2026-09-30), the stream tails the file; each line is parsed by the provider into Activity →
  on_activity; the first session id → on_session (the caller links it into the DB immediately).
  stderr goes to `<log>.stderr`.
- Silence watchdog: no output lines for idle_s seconds and no child processes → interrupt, Outcome.SILENCE.
  Children around (tests, lock wait) — silence is explained, keep waiting.
- should_stop() — the core asks to stop (budget, command): interrupt, Outcome.KILLED.
- A network failure seen in the stream before silence/timeout/stop → TRANSIENT (retry fits better).
- After any turn end (including clean ones) leftover agent processes are reaped: the whole process group plus
  descendants seen during the turn that escaped the group (setsid). Otherwise abandoned background processes
  live forever (09-30 case: 12 × `yes` from the T28 task agent loaded all cores for a day).
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
from ahub.i18n import t as _t
from ahub.providers.base import Act, Activity, Cap, Outcome, Provider, RunResult, RunSpec, Usage, clip
from ahub.time import now_ms

POLL_S = 0.2
TAIL_POLL_S = 0.05
MAX_LINE = 1_000_000  # bytes per output line; longer lines are cut
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
    """Usage from provider data and from the stream: per-field max (provider data may lag)."""
    if a is None or b is None:
        return a if b is None else b

    def mx(x, y):
        return y if x is None else x if y is None else max(x, y)

    return Usage(*(mx(getattr(a, f), getattr(b, f)) for f in ("tokens_in", "tokens_out", "tokens_reasoning",
                                                             "cache_read", "cache_write", "cost_go", "cost_usd",
                                                             "quota")), context=a.context or b.context)


def reap(pgid: int | None, tracked: dict[int, int | None]) -> list[int]:
    """Kill the agent process group and tracked descendants (start-time check). Returns killed pids."""
    killed: list[int] = []
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for pid, st in tracked.items():
        if st is not None and procs.start_time(pid) == st and procs.alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
    if pgid is None and not killed:
        return killed
    time.sleep(0.5)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for pid in killed:
        if procs.alive(pid) and procs.start_time(pid) == tracked.get(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return killed


TRACK_S = 2.0  # how often to snapshot agent descendants


def hub_proxy(name: str) -> tuple[str | None, str | None]:
    """The provider's own proxy from the hub config ([providers.<name>]). None — inherit the hub env."""
    from ahub import config

    try:
        p = config.load_hub().provider_proxy(name)
    except config.ConfigError as e:
        _log.warning("hub config, own proxy ignored: %s", str(e)[:200])
        return None, None
    return p.proxy, p.no_proxy


def run(provider: Provider, spec: RunSpec, *,
        on_activity: Callable[[Activity], None] | None = None,
        on_session: Callable[[str], None] | None = None,
        on_start: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> RunResult:
    started = now_ms()
    log_path = spec.log_path or str(Path(spec.cwd) / ".ahub" / f"{provider.name}_{started}.log")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = provider.build_command(spec)
    from ahub.prepare import apply_proxy, scrub_env

    # the worker gets no hub tokens/passwords (model keys stay) and the provider's own proxy
    env = apply_proxy(scrub_env(dict(os.environ)), *hub_proxy(provider.name))
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
        _log.error("failed to start: %s", e, extra=ctx)
        return RunResult(Outcome.NOT_STARTED, spec.session_id, error=clip(f"{cmd[0]}: {e}"),
                         started_ms=started, ended_ms=now_ms(), log_path=log_path)
    out_f.close()  # the process inherited the descriptors
    err_f.close()
    if on_start is not None:
        try:
            on_start(proc.pid)
        except Exception:  # a callback must not fail the step
            _log.exception("on_start failed", extra=ctx)

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
                    _log.exception("on_session failed", extra=ctx)
        if on_activity is not None:
            try:
                on_activity(act)
            except Exception:
                _log.exception("on_activity failed", extra=ctx)

    def _handle(raw: bytes) -> None:
        line = raw[:MAX_LINE].decode("utf-8", "replace")
        try:
            acts = provider.parse_line(line, now_ms())
        except Exception:
            _log.exception("parse_line failed", extra=ctx)
            acts = []
        if acts:  # the silence watchdog resets only on recognized activity, not any noise
            last_line[0] = time.monotonic()
        for a in acts:
            _emit(a)

    def _tail() -> None:
        """Tail the log behind the process; after exit — read the remainder."""
        buf = b""
        with open(log_path, "rb") as f:
            f.seek(start_off)
            while True:
                chunk = f.read(65536)
                if chunk:
                    buf += chunk
                    if len(buf) > MAX_LINE * 4 and b"\n" not in buf:
                        buf = buf[:MAX_LINE]  # unterminated line — don't buffer unboundedly
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
    tracked: dict[int, int | None] = {}  # agent descendants (pid → start time) — reap after the turn
    last_track = 0.0
    try:
        while True:
            try:
                proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if now - last_track >= TRACK_S:
                last_track = now
                for d in procs.descendants(proc.pid):
                    if d not in tracked:
                        tracked[d] = procs.start_time(d)
            if should_stop is not None:
                try:
                    stop = bool(should_stop())
                except Exception:
                    _log.exception("should_stop failed", extra=ctx)
                    stop = False
                if stop:
                    forced = Outcome.KILLED
                    _kill_group(proc)
                    break
            if now >= deadline:
                forced = Outcome.TIMEOUT
                _kill_group(proc)
                break
            if spec.idle_s and procs.has_children(proc.pid):
                last_line[0] = now  # children (tests, lock) — silence is explained; silence counts from when they leave
            elif spec.idle_s and now - last_line[0] >= spec.idle_s:
                silence_s = int(now - last_line[0])
                forced = Outcome.SILENCE
                _kill_group(proc)
                break
    finally:
        exited.set()
        t_out.join(timeout=30)
        try:
            left = reap(proc.pid, tracked)  # pgid = leader pid (start_new_session)
            if left:
                _log.warning("reaped stray agent processes: %s", left[:10], extra=ctx)
        except Exception:
            _log.exception("reaping agent leftovers failed", extra=ctx)

    ended = now_ms()
    with acts_lock:
        acts = list(activities)
    sid = sid_box[0]
    if sid is None and provider.has(Cap.FIND_SESSION):
        try:
            sid = provider.find_session(spec.cwd, started)
        except Exception:
            _log.exception("find_session failed", extra=ctx)
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
            error = {Outcome.SILENCE: _t("runner.silence", secs=silence_s),
                     Outcome.TIMEOUT: _t("runner.timeout", secs=spec.timeout_s),
                     Outcome.KILLED: _t("runner.killed")}[forced]
    else:
        try:
            outcome, error = provider.classify(exit_code=proc.returncode, activities=acts, session_id=sid,
                                               stderr_tail=tail)
        except Exception as e:
            _log.exception("classify failed", extra=ctx)
            outcome, error = Outcome.CRASH, clip(f"classify: {e}")

    final = provider.final_text(acts)
    structured = None
    if spec.schema is not None:
        try:
            structured = provider.structured(final, acts, spec.schema)
        except Exception:
            _log.exception("structured failed", extra=ctx)
    usage = None
    try:
        db_usage = provider.usage(sid) if sid and (provider.has(Cap.COST_MONEY) or provider.has(Cap.TOKENS)) else None
        usage = merge_usage(db_usage, provider.stream_usage(acts))
    except Exception:
        _log.exception("usage failed", extra=ctx)
    level = "info" if outcome is Outcome.OK else "warning"
    getattr(_log, level)("session: %s%s", outcome.value, f" ({error[:200]})" if error else "",
                         extra={**ctx, "session": sid, "exit": proc.returncode,
                                "secs": round((ended - started) / 1000, 1)})
    return RunResult(outcome=outcome, session_id=sid, final_text=final, structured=structured, usage=usage,
                     error=error, exit_code=proc.returncode, started_ms=started, ended_ms=ended,
                     log_path=log_path, activities=len(acts), last_activity_ms=last_act_ms[0],
                     silence_s=silence_s)
