"""ahub service run | status | pause | resume | install | start | stop."""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
import time

from ahub import log as hublog
from ahub import model
from ahub import paths
from ahub import procs
from ahub.cliutil import CliError, emit
from ahub.service import HEARTBEAT_KEY, PAUSE_KEY, Service, live_workers
from ahub.store import Store
from ahub.time import fmt_local, now_ms


def cmd_run(args) -> int:
    hublog.setup()
    hublog.install_excepthook("service")
    Service(Store()).run_forever(poll_s=args.poll)
    return 0


def cmd_status(args) -> int:
    from ahub.i18n import t

    store = Store()
    live = live_workers()
    hb = store.meta_get(HEARTBEAT_KEY)
    age = (now_ms() - int(hb)) // 1000 if hb else None
    paused = store.meta_get(PAUSE_KEY) == "1"
    queued = store.list_tasks(states={model.State.QUEUED})
    state = t("service.alive") if age is not None and age < 30 else t("service.dead")
    tick = t("service.tick", age=age) if age is not None else t("service.no_tick")
    suffix = t("service.paused_suffix") if paused else ""
    lines = [t("service.status", state=state, tick=tick, paused=suffix)]
    for tid, pid in sorted(live.items()):
        tsk = store.get_task(tid)
        lines.append(t("service.live_line", tid=tid, pid=pid, state=tsk.state.value if tsk else "?"))
    for tq in queued:
        reason = t("service.queued_reason", reason=tq.state_reason) if tq.state_reason else ""
        lines.append(t("service.queued_line", tid=tq.id, reason=reason))
    emit(args, {"heartbeat_age_s": age, "paused": paused, "live": live,
                "queued": [{"id": t.id, "reason": t.state_reason} for t in queued],
                "heartbeat": fmt_local(int(hb)) if hb else None}, "\n".join(lines))
    return 0


def cmd_pause(args, on: bool) -> int:
    from ahub.i18n import t

    store = Store()
    if on:
        store.meta_set(PAUSE_KEY, "1")
    else:
        store.meta_del(PAUSE_KEY)
    emit(args, {"paused": on}, t("service.paused_on") if on else t("service.paused_off"))
    return 0


UNIT = """[Unit]
Description={description}
After=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart={python} -m ahub {command}
Restart=always
RestartSec=10
KillMode=process
{env}

[Install]
WantedBy=default.target
"""
UNITS = {"ahub.service": ("agent-hub: сервис (очередь, процессы задач, наблюдатель)", "service run"),
         "ahub-bot.service": ("agent-hub: Telegram-бот (связь с Claude)", "bot run")}
LABELS = {"ahub.service": "dev.ahub.service", "ahub-bot.service": "dev.ahub.bot"}
PLIST_FILES = {"ahub.service": "dev.ahub.service.plist", "ahub-bot.service": "dev.ahub.bot.plist"}
BOT_SKIP = "бот не установлен: нет [telegram] token"
_ENV_KEYS = ("PATH", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "LANG",
             "AHUB_LANG", "AHUB_TZ", "AHUB_HOME")


def unit_text(name: str) -> str:
    """Юнит: окружение сессии, которого systemd не видит, — явно (PATH: claude/ahub; системный прокси)."""
    desc, command = UNITS[name]
    env = ["Environment=PYTHONUNBUFFERED=1"]
    for k, v in sorted(os.environ.items()):
        if k.upper() in _ENV_KEYS:
            env.append(f'Environment="{k}={v}"')
    return UNIT.format(description=desc, python=sys.executable, command=command, env="\n".join(env))


def _env_dict() -> dict[str, str]:
    """То же окружение словарём — для EnvironmentVariables plist."""
    env = {"PYTHONUNBUFFERED": "1"}
    for k, v in sorted(os.environ.items()):
        if k.upper() in _ENV_KEYS and k not in env:
            env[k] = v
    return env


def _log_names(name: str) -> tuple[str, str]:
    stem = "bot" if name == "ahub-bot.service" else "service"
    return f"{stem}.out.log", f"{stem}.err.log"


def plist_dict(name: str) -> dict:
    """Словарь launchd-plist: те же запуск и окружение, что у юнита systemd."""
    _desc, command = UNITS[name]
    out_name, err_name = _log_names(name)
    logd = paths.log_dir()
    return {
        "Label": LABELS[name],
        "ProgramArguments": [sys.executable, "-m", "ahub", *command.split()],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": _env_dict(),
        "StandardOutPath": str(logd / out_name),
        "StandardErrorPath": str(logd / err_name),
    }


def plist_bytes(name: str) -> bytes:
    """Plist XML — через стандартный plistlib."""
    return plistlib.dumps(plist_dict(name), fmt=plistlib.FMT_XML)


def _want_units() -> list[str]:
    """Сервис — всегда; бот — только если Telegram включён."""
    from ahub import config

    names = ["ahub.service"]
    if config.load_hub().telegram_enabled:
        names.append("ahub-bot.service")
    return names


def cmd_install(args) -> int:
    """Служба ОС: systemd --user на Linux, launchd plist на macOS (автозапуск, KeepAlive/Restart).

    Юнит/plist бота — только если Telegram включён; процессы задач переживают
    перезапуск сервиса (systemd: KillMode=process; launchd процессы не трогает)."""
    from pathlib import Path

    plat = sys.platform
    if plat.startswith("linux"):
        os_kind = "linux"
    elif plat == "darwin":
        os_kind = "darwin"
    else:
        from ahub.i18n import t

        raise CliError(t("err.service_platform", plat=plat))
    if args.print:
        # --print показывает оба (и бота тоже) — что именно встанет в службу, решает запись.
        if os_kind == "darwin":
            text = "\n".join(f"# {PLIST_FILES[n]}\n{plist_bytes(n).decode('utf-8')}" for n in UNITS)
            emit(args, {"plists": [PLIST_FILES[n] for n in UNITS]}, text)
        else:
            text = "\n".join(f"# {n}\n{unit_text(n)}" for n in UNITS)
            emit(args, {"units": list(UNITS)}, text)
        return 0
    from ahub.i18n import t

    names = _want_units()
    bot_skip = len(names) == 1
    skip = f"\n{t('service.bot_skip')}" if bot_skip else ""
    if os_kind == "darwin":
        d = Path.home() / "Library" / "LaunchAgents"
        d.mkdir(parents=True, exist_ok=True)
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        written = []
        for n in names:
            p = d / PLIST_FILES[n]
            p.write_bytes(plist_bytes(n))
            written.append(str(p))
        hint = " && ".join(f"launchctl bootstrap gui/$(id -u) {p}" for p in written)
        text = t("service.installed_launchd", names=", ".join(PLIST_FILES[n] for n in names), dir=d, hint=hint)
        emit(args, {"dir": str(d), "plists": [PLIST_FILES[n] for n in names]}, text + skip)
        return 0
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text(unit_text(n), encoding="utf-8")
    text = t("service.installed_systemd", names_comma=", ".join(names), dir=d, names_space=" ".join(names))
    emit(args, {"dir": str(d), "units": names}, text + skip)
    return 0


def _run_argv() -> list[str]:
    """Команда фонового сервиса (тесты подменяют на sleep — настоящий сервис не запускают)."""
    return [sys.executable, "-m", "ahub", "service", "run"]


def _read_pid() -> int | None:
    """pid фонового сервиса из файла — только если это действительно наш `service run` (pid мог смениться)."""
    try:
        pid = int(paths.service_pid_path().read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    args = procs.cmdline(pid)
    if not procs.alive(pid) or "service" not in args or "run" not in args:
        return None
    return pid


def _heartbeat_age_s() -> int | None:
    """Возраст тика сервиса (как в status); нет тика — None."""
    try:
        hb = Store().meta_get(HEARTBEAT_KEY)
    except Exception:
        return None
    if not hb:
        return None
    try:
        return (now_ms() - int(hb)) // 1000
    except (ValueError, TypeError):
        return None


def cmd_start(args) -> int:
    """Фон без службы ОС: `service run` отдельным процессом, pid — в файле данных.

    Уже запущен (жив pid из файла) — ничего не делаю, код 0. Сервис уже жив по
    сердцебиению (служба ОС или чужой запуск) — второй не запускаю."""
    from ahub.i18n import t

    pid = _read_pid()
    if pid is not None:
        emit(args, {"pid": pid, "already": True}, t("service.already_pid", pid=pid))
        return 0
    age = _heartbeat_age_s()
    if age is not None and age < 30:
        emit(args, {"heartbeat_age_s": age, "already": True}, t("service.already_heartbeat", age=age))
        return 0
    logd = paths.log_dir()
    logd.mkdir(parents=True, exist_ok=True)
    logf = logd / "service.log"
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    out = open(logf, "ab")
    try:
        p = subprocess.Popen(_run_argv(), stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(paths.data_dir()), env=dict(os.environ))
    finally:
        out.close()
    paths.service_pid_path().write_text(str(p.pid), encoding="utf-8")
    emit(args, {"pid": p.pid, "log": str(logf)}, t("service.started", pid=p.pid, log=logf))
    return 0


def cmd_stop(args) -> int:
    """Остановить фон `service start`: SIGTERM по pid из файла, ждать до 10 с, файл удалить."""
    from ahub.i18n import t

    pf = paths.service_pid_path()
    pid = _read_pid()
    if pid is None:
        pf.unlink(missing_ok=True)
        emit(args, {"running": False}, t("service.not_running"))
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as e:
        raise CliError(t("err.service_no_perm", pid=pid, err=e)) from e
    deadline = time.monotonic() + 10
    while procs.alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if procs.alive(pid):
        raise CliError(t("err.service_not_stopped", pid=pid))
    pf.unlink(missing_ok=True)
    emit(args, {"pid": pid, "stopped": True}, t("service.stopped", pid=pid))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("service", help=t("help.service"))
    sub = p.add_subparsers(dest="service_cmd", required=True)
    r = sub.add_parser("run", help=t("help.service_run"))
    r.add_argument("--poll", type=float, default=2.0)
    r.set_defaults(func=cmd_run)
    i = sub.add_parser("install", help=t("help.service_install"))
    i.add_argument("--print", action="store_true", help=t("help.service_install_print"))
    i.set_defaults(func=cmd_install)
    s = sub.add_parser("status", help=t("help.service_status"))
    s.set_defaults(func=cmd_status)
    for name, on in (("pause", True), ("resume", False)):
        x = sub.add_parser(name)
        x.set_defaults(func=lambda args, _on=on: cmd_pause(args, _on))
    st = sub.add_parser("start", help=t("help.service_start"))
    st.set_defaults(func=cmd_start)
    sp = sub.add_parser("stop", help=t("help.service_stop"))
    sp.set_defaults(func=cmd_stop)
