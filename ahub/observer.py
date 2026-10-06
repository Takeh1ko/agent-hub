"""Observer (V22–V23, architecture §8): watches the hub, not the projects.

1. Every 5 min — code, no model: active-task pulse, WARNING/ERROR in hub logs for the window, provider health,
   service heartbeat, queue stall, undelivered events with no orchestrator around. Clean — stay silent.
   Suspicion → observer model (observer role, default Spark high) triages: false alarm → journal;
   real one → alarm. The same problem (signature) is not re-triaged for REPEAT_MS unless it changed.
2. Every 30 min — model regardless, checklist-driven (cover for silent logs and a lying pulse).
3. Alarm → alarm event (wakes Claude). TG bridge sends to the human: critical — at once, normal — if nobody
   acked within ESCALATE_MS (comms.alarms_for_tg).
4. The service tracks the last quick check; a gap over WATCHDOG_MS — the service raises its own alarm.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ahub import comms, config, events, paths, procs, providers, pulse, registry, ui
from ahub import log as hublog
from ahub.i18n import t as _t
from ahub.model import Role, State
from ahub.providers.base import RunSpec
from ahub.providers.opencode import extract_json
from ahub.providers.runner import run as run_session
from ahub.store import Store
from ahub.time import fmt_local, now_ms

QUICK_MS = 5 * 60_000
DEEP_MS = 30 * 60_000
REPEAT_MS = 60 * 60_000
WATCHDOG_MS = 15 * 60_000
QUEUE_STUCK_MS = 10 * 60_000
UNACKED_MS = 20 * 60_000
HEARTBEAT_STALE_MS = 60_000
# the verdict vocabulary of the model's answer (TRIAGE_PROMPT asks for exactly these words) and the two
# of them that raise an alarm event — a wire protocol with the model, not text to translate
VERDICTS: tuple[str, ...] = ("ok", "false_alarm", "alarm", "critical")
ALARMING: frozenset[str] = frozenset({"alarm", "critical"})
LAST_QUICK = "observer_last_quick"
LAST_DEEP = "observer_last_deep"
SEEN_PREFIX = "observer_seen:"
LOG_DIGEST_BYTES = 3 * 1024  # log excerpt for the model — at most ~3 KB
_log = hublog.get("observer")


@dataclass
class Suspicion:
    sig: str  # signature for the repeat pause
    text: str
    critical: bool = False
    data: dict = field(default_factory=dict)


def _log_suspicions(since: int) -> list[Suspicion]:
    res = hublog.scan(since)
    recs = [r for r in res.records if r.get("comp") != "observer"]
    out = []
    for sig, n in hublog.summarize(recs, limit=5):
        sample = next(r for r in recs if hublog.signature(r) == sig)
        out.append(Suspicion(f"log:{sig}", _t("observer.log_group", n=n, lvl=sample.get("lvl"),
                                                              comp=sample.get("comp"),
                                                              msg=str(sample.get("msg"))[:160]),
                             critical=sample.get("lvl") == "CRITICAL", data={"count": n, "sample": sample}))
    if res.broken_lines:
        out.append(Suspicion("log:broken", _t("observer.log_broken", n=res.broken_lines)))
    return out


def quick_check(store: Store, *, projects: list[config.ProjectConfig] | None = None, now: int | None = None,
                since: int | None = None, health: bool = True,
                live: dict[int, int] | None = None) -> list[Suspicion]:
    """Code-only check. Returns suspicions (empty — all clean).

    live — the task processes the pulse sees (None — scan /proc; a test passes its own map, otherwise the
    pulse of its task depends on what else runs on the machine).
    """
    ts = now if now is not None else now_ms()
    if projects is None:
        projects, _ = config.load_projects()
    sus: list[Suspicion] = []
    from ahub.service import ORPHAN_GRACE_MS

    for tid, pl in pulse.all_pulses(store, projects=projects, now=ts, live=live).items():
        if pl.state in ("silent", "dead"):
            t = store.get_task(tid)
            if pl.state == "dead" and ((t.lease_until or 0) + ORPHAN_GRACE_MS > ts
                                       or ts - t.updated_at < ORPHAN_GRACE_MS):
                continue  # service may still pick it up (orphan grace) — not an alarm
            sus.append(Suspicion(f"pulse:{tid}:{pl.state}", _t("observer.pulse", tid=tid, mark=pl.mark,
                                                                                   reason=pl.reason,
                                                                                   state=t.state.value),
                                 data={"task": tid, "state": pl.state}))
    last = int(store.meta_get(LAST_QUICK) or 0)
    sus += _log_suspicions(since if since is not None else (last or ts - QUICK_MS))
    hb = store.meta_get("service_heartbeat")
    if hb is None or ts - int(hb) > HEARTBEAT_STALE_MS:
        if hb:
            text = _t("observer.no_heartbeat_since", time=fmt_local(int(hb)))
        else:
            text = _t("observer.no_heartbeat")
        sus.append(Suspicion("service:heartbeat", text, critical=True))
    queued = store.list_tasks(states={State.QUEUED})
    for t in queued:
        if not t.state_reason and ts - t.updated_at > QUEUE_STUCK_MS:
            sus.append(Suspicion(f"queue:{t.id}", _t("observer.queue_stuck", id=t.id,
                                                                     mins=(ts - t.updated_at) // 60000)))
    sus += _loop_suspicions(store, now=ts)
    if not events.present(store, now=ts):
        old = [e for e in events.unacked(store) if ts - e.ts > UNACKED_MS and e.kind != "alarm"]
        if old:
            sus.append(Suspicion("delivery:unacked", _t("observer.unacked", n=len(old),
                                                                         mins=UNACKED_MS // 60000),
                                 data={"events": [e.id for e in old[:5]]}))
    if health:
        bad = proxy_problem()
        if bad:
            sus.append(Suspicion("proxy", bad, critical=True))
        for name in providers.names():
            try:
                h = providers.get(name).health()
            except Exception as e:  # a provider module must not crash the observer
                sus.append(Suspicion(f"health:{name}:exc", _t("observer.health_exc", name=name, err=e)))
                continue
            if not h.ok and name == "opencode":
                sus.append(Suspicion(f"health:{name}", _t("observer.health_bad", name=name,
                                                                             problems="; ".join(h.problems)[:200]),
                                     critical=True))
    store.meta_set(LAST_QUICK, str(ts))
    return sus


def _loop_suspicions(store: Store, now: int) -> list[Suspicion]:
    """Cheap code rule (no model): a task re-picked more than M times per hour loops."""
    from ahub import loops as _loops

    try:
        _, _, per_hour = _loops.limits_of()
    except Exception:
        per_hour = 6
    out: list[Suspicion] = []
    for t in store.list_tasks():
        try:
            n = _loops.re_picks_last_hour(store, t, now)
        except Exception:
            continue
        if n > per_hour:
            out.append(Suspicion(f"loop:{t.id}", _loops.loop_alarm_text(t, n),
                                 data={"task": t.id, "picks": n}))
    return out


def proxy_problem(env: dict | None = None, timeout: float = 3.0) -> str:
    """System proxy accepting connections? Empty — yes or no proxy set."""
    import os
    import socket
    from urllib.parse import urlparse

    env = env if env is not None else dict(os.environ)
    url = env.get("HTTPS_PROXY") or env.get("https_proxy") or env.get("ALL_PROXY") or env.get("all_proxy")
    if not url:
        return ""
    u = urlparse(url if "://" in url else f"http://{url}")
    host, port = u.hostname or "127.0.0.1", u.port or 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ""
    except OSError as e:
        return _t("observer.proxy_down", host=host, port=port, err=e.__class__.__name__)


def _fresh(store: Store, sus: list[Suspicion], now: int) -> list[Suspicion]:
    """Suspicions not triaged in the last REPEAT_MS (or changed since)."""
    out = []
    for s in sus:
        key = SEEN_PREFIX + s.sig
        seen = store.meta_get(key)
        if seen and now - int(seen.split("|", 1)[0]) < REPEAT_MS:
            continue  # same problem (signature) — at most once per REPEAT_MS, counters in text don't count
        out.append(s)
    return out


def _mark_seen(store: Store, sus: list[Suspicion], now: int) -> None:
    for s in sus:
        store.meta_set(SEEN_PREFIX + s.sig, f"{now}|{s.text[:80]}")


def _bot_pids(proc_root: str | Path = "/proc") -> list[int]:
    """Live bot pids: processes with `bot run` in cmdline."""
    out = []
    for pid in procs.pids(proc_root):
        args = procs.cmdline(pid, proc_root)
        if any(x == "bot" and y == "run" for x, y in zip(args, args[1:], strict=False)) and procs.alive(pid, proc_root):
            out.append(pid)
    return out


def window_since(store: Store, *, deep: bool, now: int) -> int:
    """Check window start: since the last check of the same kind (scheduled or quick), else its period."""
    if deep:
        return int(store.meta_get(LAST_DEEP) or 0) or now - DEEP_MS
    return int(store.meta_get(LAST_QUICK) or 0) or now - QUICK_MS


def log_digest(since: int, *, now: int | None = None, limit_bytes: int = LOG_DIGEST_BYTES,
               proc_root: str | Path = "/proc") -> str:
    """WARNING/ERROR excerpt for the window: groups (signature, count), pid and last-record time. At most limit."""
    ts_now = now if now is not None else now_ms()
    try:
        res = hublog.scan(since, until_ms=ts_now)
    except OSError:
        return _t("observer.log_unavailable")
    recs = [r for r in res.records if r.get("comp") != "observer"]
    if not recs:
        return _t("observer.log_empty")
    lines: list[str] = []
    for sig, n in hublog.summarize(recs, limit=10):
        grp = [r for r in recs if hublog.signature(r) == sig]
        last = max(grp, key=lambda r: int(r.get("ts") or 0))
        ts = int(last.get("ts") or 0)
        pid = last.get("pid")
        mark = "" if isinstance(pid, int) and procs.alive(pid, proc_root) else _t("observer.dead_pid")
        msg = str(last.get("msg") or "")[:160].replace("\n", " ")
        lines.append(_t("observer.log_line", n=n, lvl=last.get("lvl"), comp=last.get("comp"), msg=msg,
                                            when=fmt_local(ts, now=ts_now) if ts else "?", ts=ts, pid=pid,
                                            mark=mark))
    if res.broken_lines:
        lines.append(_t("observer.broken_lines", n=res.broken_lines))
    text = "\n".join(lines)
    while len(text.encode("utf-8")) > limit_bytes and len(lines) > 1:
        lines.pop()  # drop rarest groups until it fits
        text = "\n".join(lines)
    raw = text.encode("utf-8")
    if len(raw) > limit_bytes:  # one group still too big — cut by bytes
        text = raw[:limit_bytes].decode("utf-8", "ignore")
    return text


def snapshot(store: Store, *, now: int, proc_root: str | Path = "/proc") -> str:
    """Short hub picture for the observer model."""
    from ahub import views
    from ahub.service import live_workers

    live = live_workers()
    none = _t("observer.none")
    with ui.plain():  # the snapshot is a prompt, not a screen — no colour, no marks
        status = views.status_text(store, live=live, now=now)
    lines = [_t("observer.snap_summary"), status]
    evs = store.events(after_id=max(0, store.last_event_id() - 40))
    lines.append(_t("observer.snap_events"))
    lines += [f"{fmt_local(e.ts)} {e.kind} T{e.task_id or '-'} {json.dumps(e.payload, ensure_ascii=False)[:140]}"
              for e in evs[-25:]]
    lines.append(_t("observer.snap_procs", val=live or none))
    bots = _bot_pids(proc_root)
    lines.append(_t("observer.snap_pids", service=os.getpid(), bot=bots[0] if bots else none,
                                          tasks=live or none))
    return "\n".join(lines)


TRIAGE_PROMPT = """You are the agent-hub observer (orchestrator of worker models). Your job is to tell whether
the hub itself works as it should: task processes are alive, models answer, the queue moves, logs have no real
errors. Projects and their code are not your concern. Change and run nothing.
Now is {now_str} (ts={now_ms}); look only at log records with ts >= {since} ({since_str}); older records are
history, not current problems. Read details in {log} if needed, but only records with ts >= {since}.

{kind}

## Code suspicions
{suspicions}

## Log digest for the window (WARNING/ERROR, grouped)
{log_digest}

{snapshot}

Answer with ONLY a JSON object:
{{"verdict": "ok | false_alarm | alarm | critical", "summary": "one sentence: what is happening",
  "action": "what Claude/human should do (if alarm/critical)"}}
critical — the hub cannot work (provider down, service stopped, all tasks stuck). alarm — a specific task/component
is broken and will not recover by itself. false_alarm — explainable (long tests, lock wait, routine retry).
"""

DEEP_CHECKLIST = """## Scheduled check (every 30 min) — go through the checklist
1. Are there tasks "working" but with no result for a long time (phase not changing, cost growing)?
2. Is the queue growing while slots are free?
3. Are there orphans (active without a process)?
4. Do costs match activity (spending without activity / activity without spending)?
5. Are logs silent while events show clear problems (failures, retries, silence)?
6. Are providers healthy (network/server failures in a row)?"""


def triage(store: Store, sus: list[Suspicion], *, deep: bool = False, now: int | None = None,
           project: config.ProjectConfig | None = None, since: int | None = None) -> dict:
    """Model triage. Returns {"verdict", "summary", "action", "cost_go"}; model failure — verdict "unknown"."""
    ts = now if now is not None else now_ms()
    try:
        entry = registry.pick(store, Role.OBSERVER, project)
    except registry.RegistryError as e:
        return {"verdict": "unknown", "summary": _t("observer.no_model", err=e), "action": "", "cost_go": 0.0}
    win = since if since is not None else window_since(store, deep=deep, now=ts)
    cwd = paths.state_dir() / "observer" / str(ts)
    cwd.mkdir(parents=True, exist_ok=True)
    from ahub.prompts import reply_language_line

    prompt = TRIAGE_PROMPT.format(
        log=hublog.log_file(), kind=DEEP_CHECKLIST if deep else "## Suspicion triage",
        suspicions="\n".join(f"- {'CRITICAL ' if s.critical else ''}{s.text}" for s in sus) or "- none",
        snapshot=snapshot(store, now=ts),
        now_str=fmt_local(ts, now=ts), now_ms=ts, since=win, since_str=fmt_local(win, now=ts),
        log_digest=log_digest(win, now=ts)) + "\n\n" + reply_language_line()
    prov = providers.get(entry.provider)
    r = run_session(prov, RunSpec(prompt=prompt, cwd=str(cwd), model_id=entry.model_id, variant=entry.variant,
                                  log_path=str(cwd / "observer.log"), timeout_s=15 * 60, idle_s=600))
    data = extract_json(r.final_text) or {}
    cost = (r.usage.cost_go or 0.0) if r.usage else 0.0
    verdict = str(data.get("verdict", "")).strip()
    if not r.ok or verdict not in VERDICTS:
        return {"verdict": "unknown", "summary": _t("observer.bad_model", outcome=r.outcome.value,
                                                                          err=r.error[:100]),
                "action": "", "cost_go": cost}
    return {"verdict": verdict, "summary": str(data.get("summary", ""))[:300],
            "action": str(data.get("action", ""))[:300], "cost_go": cost}


def _report(store: Store, kind: str, verdict: str, summary: str, details: dict, cost: float, now: int) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO observer_report(ts, kind, verdict, summary, details_json, cost_go) VALUES(?,?,?,?,?,?)",
                  (now, kind, verdict, summary, json.dumps(details, ensure_ascii=False, default=str), cost))


def cycle(store: Store, *, now: int | None = None, deep_due: bool | None = None, use_model: bool = True,
          projects: list[config.ProjectConfig] | None = None) -> str:
    """One observer pass. Returns verdict: ok | false_alarm | alarm | critical | unknown."""
    ts = now if now is not None else now_ms()
    last_deep = int(store.meta_get(LAST_DEEP) or 0)
    deep = deep_due if deep_due is not None else ts - last_deep >= DEEP_MS
    win = window_since(store, deep=deep, now=ts)  # before quick_check: it moves LAST_QUICK
    sus = quick_check(store, projects=projects, now=ts)
    fresh = _fresh(store, sus, ts)
    looped = [s for s in fresh if s.sig.startswith("loop:")]
    if looped:
        # cheap code rule, no model: too many re-picks — alarm at once, then keep going:
        # the other fresh suspicions are still triaged below and LAST_DEEP still moves
        _mark_seen(store, looped, ts)
        loop_text = "; ".join(s.text for s in looped)[:400]
        _report(store, "quick", "alarm", loop_text, {"suspicions": [s.text for s in looped]}, 0.0, ts)
        comms.raise_alarm(store, loop_text, critical=False,
                          details={"suspicions": [s.text for s in looped][:5]})
        _log.warning("observer alarm (loop): %s", loop_text[:200])
        fresh = [s for s in fresh if not s.sig.startswith("loop:")]
    if not fresh and not deep:
        if looped:
            return "alarm"
        _report(store, "quick", "ok", _t("observer.clean") if not sus else _t("observer.known", n=len(sus)),
                {}, 0.0, ts)
        return "ok"
    crit_code = [s for s in fresh if s.critical]
    if not use_model:
        verdict = "critical" if crit_code else ("alarm" if fresh else "ok")
        res = {"verdict": verdict, "summary": "; ".join(s.text for s in fresh)[:300], "action": "", "cost_go": 0.0}
    else:
        res = triage(store, fresh, deep=deep, now=ts, since=win)
        if res["verdict"] == "unknown" and crit_code:  # model silent but code sees critical — don't stay quiet
            res["verdict"] = "critical"
            res["summary"] = "; ".join(s.text for s in crit_code)[:300] + _t("observer.no_answer_suffix")
    if deep:
        store.meta_set(LAST_DEEP, str(ts))
    _mark_seen(store, fresh, ts)
    kind = "deep" if deep else "triage"
    _report(store, kind, res["verdict"], res["summary"], {"suspicions": [s.text for s in fresh],
                                                          "action": res["action"]}, res["cost_go"], ts)
    if res["verdict"] in ALARMING:
        text = res["summary"] + (f" → {res['action']}" if res["action"] else "")
        comms.raise_alarm(store, text[:400], critical=res["verdict"] == "critical",
                          details={"suspicions": [s.text for s in fresh][:5]})
        _log.warning("observer alarm (%s): %s", res["verdict"], text[:200])
    if looped and res["verdict"] not in ALARMING:
        return "alarm"  # the loop already alarmed above; the rest was clean
    return res["verdict"]


def watchdog(store: Store, *, now: int | None = None) -> bool:
    """For the service: observer missed its check → code-raised alarm. True — raised."""
    ts = now if now is not None else now_ms()
    last = store.meta_get(LAST_QUICK)
    if last is None or ts - int(last) <= WATCHDOG_MS:
        return False
    if store.meta_get("observer_watchdog_alarm") == last:
        return False
    store.meta_set("observer_watchdog_alarm", last)
    comms.raise_alarm(store, _t("observer.no_check", when=fmt_local(int(last))), critical=False)
    return True


def reports(store: Store, limit: int = 10) -> list[dict]:
    with store.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM observer_report ORDER BY id DESC LIMIT ?", (limit,))]
