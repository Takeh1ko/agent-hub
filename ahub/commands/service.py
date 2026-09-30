"""ahub service run | status | pause | resume."""

from __future__ import annotations

from ahub import log as hublog
from ahub import model
from ahub.cliutil import emit
from ahub.service import HEARTBEAT_KEY, PAUSE_KEY, Service, live_workers
from ahub.store import Store
from ahub.time import fmt_local, now_ms


def cmd_run(args) -> int:
    hublog.setup()
    hublog.install_excepthook("service")
    Service(Store()).run_forever(poll_s=args.poll)
    return 0


def cmd_status(args) -> int:
    store = Store()
    live = live_workers()
    hb = store.meta_get(HEARTBEAT_KEY)
    age = (now_ms() - int(hb)) // 1000 if hb else None
    paused = store.meta_get(PAUSE_KEY) == "1"
    queued = store.list_tasks(states={model.State.QUEUED})
    lines = [f"сервис: {'жив' if age is not None and age < 30 else 'не отвечает'}"
             + (f" (тик {age} с назад)" if age is not None else " (ни одного тика)")
             + ("; очередь на паузе" if paused else "")]
    for tid, pid in sorted(live.items()):
        t = store.get_task(tid)
        lines.append(f"  T{tid} pid {pid} {t.state.value if t else '?'}")
    for t in queued:
        lines.append(f"  T{t.id} в очереди" + (f": {t.state_reason}" if t.state_reason else ""))
    emit(args, {"heartbeat_age_s": age, "paused": paused, "live": live,
                "queued": [{"id": t.id, "reason": t.state_reason} for t in queued],
                "heartbeat": fmt_local(int(hb)) if hb else None}, "\n".join(lines))
    return 0


def cmd_pause(args, on: bool) -> int:
    store = Store()
    if on:
        store.meta_set(PAUSE_KEY, "1")
    else:
        store.meta_del(PAUSE_KEY)
    emit(args, {"paused": on}, "очередь на паузе" if on else "очередь снова работает")
    return 0


UNIT = """[Unit]
Description=agent-hub v2 (ahub service)
After=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart={python} -m ahub service run
Restart=always
RestartSec=10
KillMode=process
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def cmd_install(args) -> int:
    """Юнит systemd --user: автоперезапуск, лимит перезапусков; процессы задач не убиваются (KillMode=process)."""
    import sys
    from pathlib import Path

    import os

    unit = Path.home() / ".config" / "systemd" / "user" / "ahub.service"
    text = UNIT.format(python=sys.executable)
    # systemd не видит окружения сессии: прокси (Koala) — явно, иначе модели и Telegram без сети
    proxy = [f"Environment={k}={v}" for k, v in sorted(os.environ.items())
             if k.upper() in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")]
    if proxy:
        text = text.replace("Environment=PYTHONUNBUFFERED=1", "\n".join(["Environment=PYTHONUNBUFFERED=1", *proxy]))
    if args.print:
        emit(args, {"unit": text}, text)
        return 0
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(text, encoding="utf-8")
    emit(args, {"path": str(unit)}, f"записан {unit}\nвключить: systemctl --user daemon-reload && "
                                     f"systemctl --user enable --now ahub && loginctl enable-linger $USER")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("service", help="хаб-сервис: очередь и процессы задач")
    sub = p.add_subparsers(dest="service_cmd", required=True)
    r = sub.add_parser("run", help="запустить сервис (передний план; systemd)")
    r.add_argument("--poll", type=float, default=2.0)
    r.set_defaults(func=cmd_run)
    i = sub.add_parser("install", help="юнит systemd --user")
    i.add_argument("--print", action="store_true", help="только показать")
    i.set_defaults(func=cmd_install)
    s = sub.add_parser("status", help="жив ли сервис, процессы задач, очередь")
    s.set_defaults(func=cmd_status)
    for name, on in (("pause", True), ("resume", False)):
        x = sub.add_parser(name)
        x.set_defaults(func=lambda args, _on=on: cmd_pause(args, _on))
