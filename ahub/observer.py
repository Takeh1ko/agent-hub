"""Наблюдатель (V22–V23, architecture §8): следит за хабом, а не за проектами.

1. Каждые 5 мин — код, без модели: пульс активных задач, WARNING/ERROR в логах хаба за период, здоровье поставщиков,
   сердцебиение сервиса, застой очереди, неразобранные события при отсутствии оркестратора. Чисто — ничего.
   Подозрение → модель-наблюдатель (роль observer, по умолчанию Spark high) разбирает: ложная тревога → журнал;
   настоящая → тревога. Одна и та же проблема (подпись) повторно не разбирается REPEAT_MS, пока не изменилась.
2. Каждые 30 мин — модель в любом случае, по чек-листу (страховка от молчащих логов и врущего пульса).
3. Тревога → событие alarm (будит Claude). TG-мост шлёт человеку: критичную — сразу, обычную — если за
   ESCALATE_MS никто не подтвердил (comms.alarms_for_tg).
4. Сервис знает время последней быстрой проверки; пропуск > WATCHDOG_MS — сервис сам поднимает тревогу.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ahub import comms, config, events, paths, procs, providers, pulse, registry
from ahub import log as hublog
from ahub.model import ACTIVE, Role, State
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
LAST_QUICK = "observer_last_quick"
LAST_DEEP = "observer_last_deep"
SEEN_PREFIX = "observer_seen:"
LOG_DIGEST_BYTES = 3 * 1024  # выжимка лога для модели — не больше ~3 КБ
_log = hublog.get("observer")


@dataclass
class Suspicion:
    sig: str  # подпись для паузы на повтор
    text: str
    critical: bool = False
    data: dict = field(default_factory=dict)


def _log_suspicions(since: int) -> list[Suspicion]:
    res = hublog.scan(since)
    recs = [r for r in res.records if r.get("comp") != "observer"]
    out = []
    for sig, n in hublog.summarize(recs, limit=5):
        sample = next(r for r in recs if hublog.signature(r) == sig)
        out.append(Suspicion(f"log:{sig}", f"лог: {n}× {sample.get('lvl')} {sample.get('comp')}: "
                                           f"{str(sample.get('msg'))[:160]}",
                             critical=sample.get("lvl") == "CRITICAL", data={"count": n, "sample": sample}))
    if res.broken_lines:
        out.append(Suspicion("log:broken", f"лог: {res.broken_lines} битых строк"))
    return out


def quick_check(store: Store, *, projects: list[config.ProjectConfig] | None = None, now: int | None = None,
                since: int | None = None, health: bool = True) -> list[Suspicion]:
    """Проверка кодом. Возвращает подозрения (пусто — всё чисто)."""
    ts = now if now is not None else now_ms()
    if projects is None:
        projects, _ = config.load_projects()
    sus: list[Suspicion] = []
    from ahub.service import ORPHAN_GRACE_MS

    for tid, pl in pulse.all_pulses(store, projects=projects, now=ts).items():
        if pl.state in ("silent", "dead"):
            t = store.get_task(tid)
            if pl.state == "dead" and ((t.lease_until or 0) + ORPHAN_GRACE_MS > ts or ts - t.updated_at < ORPHAN_GRACE_MS):
                continue  # сервис ещё может подхватить (грейс сиротства) — не тревога
            sus.append(Suspicion(f"pulse:{tid}:{pl.state}", f"T{tid} {pl.mark} {pl.reason} (этап {t.state.value})",
                                 data={"task": tid, "state": pl.state}))
    last = int(store.meta_get(LAST_QUICK) or 0)
    sus += _log_suspicions(since if since is not None else (last or ts - QUICK_MS))
    hb = store.meta_get("service_heartbeat")
    if hb is None or ts - int(hb) > HEARTBEAT_STALE_MS:
        sus.append(Suspicion("service:heartbeat", "сервис не тикает" + (f" с {fmt_local(int(hb))}" if hb else ""),
                             critical=True))
    queued = store.list_tasks(states={State.QUEUED})
    for t in queued:
        if not t.state_reason and ts - t.updated_at > QUEUE_STUCK_MS:
            sus.append(Suspicion(f"queue:{t.id}", f"T{t.id} в очереди {(ts - t.updated_at) // 60000} мин без причины"))
    if not events.present(store, now=ts):
        old = [e for e in events.unacked(store) if ts - e.ts > UNACKED_MS and e.kind != "alarm"]
        if old:
            sus.append(Suspicion("delivery:unacked", f"{len(old)} событий ждут оркестратора > {UNACKED_MS // 60000} мин,"
                                                     " Claude не слушает", data={"events": [e.id for e in old[:5]]}))
    if health:
        bad = proxy_problem()
        if bad:
            sus.append(Suspicion("proxy", bad, critical=True))
        for name in providers.names():
            try:
                h = providers.get(name).health()
            except Exception as e:  # модуль поставщика не должен ронять наблюдателя
                sus.append(Suspicion(f"health:{name}:exc", f"поставщик {name}: проверка упала: {e}"))
                continue
            if not h.ok and name == "opencode":
                sus.append(Suspicion(f"health:{name}", f"поставщик {name}: " + "; ".join(h.problems)[:200],
                                     critical=True))
    store.meta_set(LAST_QUICK, str(ts))
    return sus


def proxy_problem(env: dict | None = None, timeout: float = 3.0) -> str:
    """Системный прокси принимает соединения? Пусто — да или прокси не задан."""
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
        return f"прокси {host}:{port} не отвечает ({e.__class__.__name__}) — модели и Telegram без сети; проверь системный прокси"


def _fresh(store: Store, sus: list[Suspicion], now: int) -> list[Suspicion]:
    """Подозрения, которые ещё не разбирали за REPEAT_MS (или которые изменились)."""
    out = []
    for s in sus:
        key = SEEN_PREFIX + s.sig
        seen = store.meta_get(key)
        if seen and now - int(seen.split("|", 1)[0]) < REPEAT_MS:
            continue  # та же проблема (подпись) — не чаще раза в REPEAT_MS, счётчики в тексте не в счёт
        out.append(s)
    return out


def _mark_seen(store: Store, sus: list[Suspicion], now: int) -> None:
    for s in sus:
        store.meta_set(SEEN_PREFIX + s.sig, f"{now}|{s.text[:80]}")


def _bot_pids(proc_root: str | Path = "/proc") -> list[int]:
    """Живые pid бота: процессы с `bot run` в cmdline."""
    out = []
    for pid in procs.pids(proc_root):
        args = procs.cmdline(pid, proc_root)
        if any(x == "bot" and y == "run" for x, y in zip(args, args[1:])) and procs.alive(pid, proc_root):
            out.append(pid)
    return out


def window_since(store: Store, *, deep: bool, now: int) -> int:
    """Начало окна проверки: с прошлой проверки того же вида (плановой или быстрой), иначе — её период."""
    if deep:
        return int(store.meta_get(LAST_DEEP) or 0) or now - DEEP_MS
    return int(store.meta_get(LAST_QUICK) or 0) or now - QUICK_MS


def log_digest(since: int, *, now: int | None = None, limit_bytes: int = LOG_DIGEST_BYTES,
               proc_root: str | Path = "/proc") -> str:
    """Выжимка WARNING/ERROR за окно: группы (подпись, счёт), pid и время последней записи. Не больше limit."""
    ts_now = now if now is not None else now_ms()
    try:
        res = hublog.scan(since, until_ms=ts_now)
    except OSError:
        return "лог недоступен"
    recs = [r for r in res.records if r.get("comp") != "observer"]
    if not recs:
        return "нет WARNING/ERROR за окно"
    lines: list[str] = []
    for sig, n in hublog.summarize(recs, limit=10):
        grp = [r for r in recs if hublog.signature(r) == sig]
        last = max(grp, key=lambda r: int(r.get("ts") or 0))
        ts = int(last.get("ts") or 0)
        pid = last.get("pid")
        mark = "" if isinstance(pid, int) and procs.alive(pid, proc_root) else " — мёртвый pid"
        msg = str(last.get("msg") or "")[:160].replace("\n", " ")
        lines.append(f"{n}× {last.get('lvl')} {last.get('comp')}: {msg}"
                     f" (последняя {fmt_local(ts, now=ts_now) if ts else '?'} ts={ts}, pid={pid}{mark})")
    if res.broken_lines:
        lines.append(f"битых строк: {res.broken_lines}")
    text = "\n".join(lines)
    while len(text.encode("utf-8")) > limit_bytes and len(lines) > 1:
        lines.pop()  # выбрасываем самые редкие группы, пока не влезет
        text = "\n".join(lines)
    raw = text.encode("utf-8")
    if len(raw) > limit_bytes:  # одна группа всё равно большая — режем по байтам
        text = raw[:limit_bytes].decode("utf-8", "ignore")
    return text


def snapshot(store: Store, *, now: int, proc_root: str | Path = "/proc") -> str:
    """Короткая картина хаба для модели-наблюдателя."""
    from ahub import views
    from ahub.service import live_workers

    live = live_workers()
    lines = ["## Сводка", views.status_text(store, live=live, now=now)]
    evs = store.events(after_id=max(0, store.last_event_id() - 40))
    lines.append("## Последние события")
    lines += [f"{fmt_local(e.ts)} {e.kind} T{e.task_id or '-'} {json.dumps(e.payload, ensure_ascii=False)[:140]}"
              for e in evs[-25:]]
    lines.append(f"## Процессы задач: {live or 'нет'}")
    bots = _bot_pids(proc_root)
    lines.append(f"## Живые pid: сервис {os.getpid()}, бот {bots[0] if bots else 'нет'},"
                 f" задачи {live or 'нет'} — ошибки с других pid от мёртвых процессов (история)")
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
    """Разбор моделью. Возвращает {"verdict", "summary", "action", "cost_go"}; сбой модели — verdict "unknown"."""
    ts = now if now is not None else now_ms()
    try:
        entry = registry.pick(store, Role.OBSERVER, project)
    except registry.RegistryError as e:
        return {"verdict": "unknown", "summary": f"нет модели наблюдателя: {e}", "action": "", "cost_go": 0.0}
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
    if not r.ok or verdict not in ("ok", "false_alarm", "alarm", "critical"):
        return {"verdict": "unknown", "summary": f"модель наблюдателя не ответила по форме ({r.outcome.value}:"
                                                 f" {r.error[:100]})", "action": "", "cost_go": cost}
    return {"verdict": verdict, "summary": str(data.get("summary", ""))[:300],
            "action": str(data.get("action", ""))[:300], "cost_go": cost}


def _report(store: Store, kind: str, verdict: str, summary: str, details: dict, cost: float, now: int) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO observer_report(ts, kind, verdict, summary, details_json, cost_go) VALUES(?,?,?,?,?,?)",
                  (now, kind, verdict, summary, json.dumps(details, ensure_ascii=False, default=str), cost))


def cycle(store: Store, *, now: int | None = None, deep_due: bool | None = None, use_model: bool = True,
          projects: list[config.ProjectConfig] | None = None) -> str:
    """Один проход наблюдателя. Возвращает вердикт: ok | false_alarm | alarm | critical | unknown."""
    ts = now if now is not None else now_ms()
    last_deep = int(store.meta_get(LAST_DEEP) or 0)
    deep = deep_due if deep_due is not None else ts - last_deep >= DEEP_MS
    win = window_since(store, deep=deep, now=ts)  # до quick_check: он сдвигает LAST_QUICK
    sus = quick_check(store, projects=projects, now=ts)
    fresh = _fresh(store, sus, ts)
    if not fresh and not deep:
        _report(store, "quick", "ok", "чисто" if not sus else f"известное: {len(sus)}", {}, 0.0, ts)
        return "ok"
    crit_code = [s for s in fresh if s.critical]
    if not use_model:
        verdict = "critical" if crit_code else ("alarm" if fresh else "ok")
        res = {"verdict": verdict, "summary": "; ".join(s.text for s in fresh)[:300], "action": "", "cost_go": 0.0}
    else:
        res = triage(store, fresh, deep=deep, now=ts, since=win)
        if res["verdict"] == "unknown" and crit_code:  # модель не ответила, а код видит критичное — не молчим
            res["verdict"] = "critical"
            res["summary"] = "; ".join(s.text for s in crit_code)[:300] + " (модель наблюдателя не ответила)"
    if deep:
        store.meta_set(LAST_DEEP, str(ts))
    _mark_seen(store, fresh, ts)
    kind = "deep" if deep else "triage"
    _report(store, kind, res["verdict"], res["summary"], {"suspicions": [s.text for s in fresh],
                                                          "action": res["action"]}, res["cost_go"], ts)
    if res["verdict"] in ("alarm", "critical"):
        text = res["summary"] + (f" → {res['action']}" if res["action"] else "")
        comms.raise_alarm(store, text[:400], critical=res["verdict"] == "critical",
                          details={"suspicions": [s.text for s in fresh][:5]})
        _log.warning("тревога наблюдателя (%s): %s", res["verdict"], text[:200])
    return res["verdict"]


def watchdog(store: Store, *, now: int | None = None) -> bool:
    """Для сервиса: наблюдатель пропустил проверку → тревога кодом. True — подняли."""
    ts = now if now is not None else now_ms()
    last = store.meta_get(LAST_QUICK)
    if last is None or ts - int(last) <= WATCHDOG_MS:
        return False
    if store.meta_get("observer_watchdog_alarm") == last:
        return False
    store.meta_set("observer_watchdog_alarm", last)
    comms.raise_alarm(store, f"наблюдатель не проверял хаб с {fmt_local(int(last))}", critical=False)
    return True


def reports(store: Store, limit: int = 10) -> list[dict]:
    with store.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM observer_report ORDER BY id DESC LIMIT ?", (limit,))]
