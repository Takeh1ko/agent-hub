"""service install (systemd/launchd) and service start/stop without an OS service."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

from ahub import cli, paths, procs
from ahub.commands import service as svccmd
from ahub.service import HEARTBEAT_KEY
from ahub.store import Store
from ahub.time import now_ms
from tests.conftest import write


def _no_telegram(monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)


def _with_telegram():
    write(paths.global_config_path(), 'projects = []\n[telegram]\ntoken = "bot123"\nchat_id = 42\n')


def test_env_keys_extra():
    assert {"AHUB_LANG", "AHUB_TZ", "AHUB_HOME"} <= set(svccmd._ENV_KEYS)
    assert "AHUB_HOME" in svccmd.unit_text("ahub.service")  # AHUB_HOME is set by the conftest fixture


def test_install_linux_no_telegram(capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _no_telegram(monkeypatch)
    assert cli.main(["service", "install"]) == 0
    d = Path.home() / ".config" / "systemd" / "user"
    assert (d / "ahub.service").exists()
    assert not (d / "ahub-bot.service").exists()
    out = capsys.readouterr().out
    assert "ahub.service" in out and "бот не установлен" in out


def test_install_linux_with_telegram(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _no_telegram(monkeypatch)
    _with_telegram()
    assert cli.main(["service", "install"]) == 0
    d = Path.home() / ".config" / "systemd" / "user"
    assert (d / "ahub.service").exists() and (d / "ahub-bot.service").exists()


def test_install_darwin_plist(capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _no_telegram(monkeypatch)
    assert cli.main(["service", "install"]) == 0
    d = Path.home() / "Library" / "LaunchAgents"
    svc = d / "dev.ahub.service.plist"
    assert svc.exists()
    assert not (d / "dev.ahub.bot.plist").exists()  # no bot unit without telegram
    pl = plistlib.loads(svc.read_bytes())
    assert pl["Label"] == "dev.ahub.service"
    assert pl["ProgramArguments"] == [sys.executable, "-m", "ahub", "service", "run"]
    assert pl["RunAtLoad"] is True and pl["KeepAlive"] is True
    assert pl["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"
    assert "PATH" in pl["EnvironmentVariables"]
    assert str(paths.log_dir()) in pl["StandardOutPath"] + pl["StandardErrorPath"]
    out = capsys.readouterr().out
    assert "launchctl bootstrap gui/$(id -u)" in out and "бот не установлен" in out


def test_install_darwin_with_telegram(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _no_telegram(monkeypatch)
    _with_telegram()
    assert cli.main(["service", "install"]) == 0
    d = Path.home() / "Library" / "LaunchAgents"
    pl = plistlib.loads((d / "dev.ahub.bot.plist").read_bytes())
    assert pl["Label"] == "dev.ahub.bot"
    assert pl["ProgramArguments"] == [sys.executable, "-m", "ahub", "bot", "run"]


def test_install_print_both_os(monkeypatch, capsys):
    _no_telegram(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli.main(["service", "install", "--print"]) == 0
    assert "dev.ahub.service" in capsys.readouterr().out
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli.main(["service", "install", "--print"]) == 0
    assert "-m ahub service run" in capsys.readouterr().out


def test_install_other_os_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert cli.main(["service", "install", "--print"]) == 2


def _fake_sleep(monkeypatch):
    monkeypatch.setattr(svccmd, "_run_argv",
                        lambda: [sys.executable, "-c", "import time; time.sleep(30)", "service", "run"])


def test_start_stop_fake(monkeypatch, capsys):
    _fake_sleep(monkeypatch)
    assert paths.service_pid_path().parent == paths.data_dir()
    assert cli.main(["service", "start"]) == 0
    pid = int(paths.service_pid_path().read_text(encoding="utf-8").strip())
    assert procs.alive(pid)
    assert (paths.log_dir() / "service.log").exists()
    capsys.readouterr()
    assert cli.main(["service", "start"]) == 0  # a repeat — does not start a second one
    assert int(paths.service_pid_path().read_text(encoding="utf-8").strip()) == pid
    assert "уже запущен" in capsys.readouterr().out
    assert cli.main(["service", "stop"]) == 0
    assert not paths.service_pid_path().exists()
    assert not procs.alive(pid)
    assert cli.main(["service", "stop"]) == 0  # stopping nothing — quiet, code 0


def test_start_refuses_when_heartbeat_alive(monkeypatch, capsys):
    _fake_sleep(monkeypatch)
    Store().meta_set(HEARTBEAT_KEY, str(now_ms()))
    assert cli.main(["service", "start"]) == 0
    assert "уже работает" in capsys.readouterr().out
    assert not paths.service_pid_path().exists()


def _ok_run(calls: list):
    import subprocess as _sp

    def _fake(cmd, **kw):
        calls.append(list(cmd))
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _fake


def test_enable_linux_order(monkeypatch):
    """T48: Linux enable runs daemon-reload then enable --now, in order."""
    calls: list = []
    monkeypatch.setattr(svccmd.subprocess, "run", _ok_run(calls))
    assert svccmd.enable_service("linux", ["ahub.service"], ["/x/ahub.service"]) == []
    assert calls == [["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", "ahub.service"]]


def test_enable_darwin_order(monkeypatch):
    """T48: macOS enable bootstraps each plist."""
    calls: list = []
    monkeypatch.setattr(svccmd.subprocess, "run", _ok_run(calls))
    monkeypatch.setattr(svccmd.os, "getuid", lambda: 501)
    written = ["/Users/u/Library/LaunchAgents/dev.ahub.service.plist"]
    assert svccmd.enable_service("darwin", ["ahub.service"], written) == []
    assert calls == [["launchctl", "bootstrap", "gui/501", written[0]]]
    assert svccmd.enable_commands("darwin", ["ahub.service"], written) == calls


def test_enable_darwin_tolerates_already_loaded(monkeypatch):
    """T48: launchctl 'already loaded' is not a failure."""
    import subprocess as _sp

    def _fake(cmd, **kw):
        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="Bootstrap failed: already loaded")

    monkeypatch.setattr(svccmd.subprocess, "run", _fake)
    assert svccmd.enable_service("darwin", ["ahub.service"], ["/p.plist"]) == []


def test_enable_failure_collects_and_continues(monkeypatch):
    """T48: a failing daemon-reload does not skip enable --now; errors are returned."""
    import subprocess as _sp

    calls: list = []

    def _fake(cmd, **kw):
        calls.append(list(cmd))
        if "daemon-reload" in cmd:
            return _sp.CompletedProcess(cmd, 1, stdout="", stderr="boom-reload")
        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="boom-enable")

    monkeypatch.setattr(svccmd.subprocess, "run", _fake)
    fails = svccmd.enable_service("linux", ["ahub.service"], ["/x"])
    assert len(calls) == 2  # second command still ran
    assert len(fails) == 2 and "boom-reload" in fails[0][1] and "boom-enable" in fails[1][1]


def test_wait_for_heartbeat_alive(monkeypatch):
    """T48: fresh tick returns its age without waiting."""
    monkeypatch.setattr(svccmd, "_heartbeat_age_s", lambda: 4)
    assert svccmd.wait_for_heartbeat(timeout_s=15.0) == 4


def test_wait_for_heartbeat_timeout(monkeypatch):
    """T48: no tick — None after the timeout (mocked, no 15s sleep)."""
    monkeypatch.setattr(svccmd, "_heartbeat_age_s", lambda: None)
    assert svccmd.wait_for_heartbeat(timeout_s=0) is None
