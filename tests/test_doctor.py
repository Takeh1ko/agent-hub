"""ahub doctor: installation check on a faked env (HOME is tmp via conftest)."""

from __future__ import annotations

import dataclasses
import json
import shutil
import socket
import subprocess
import types
from pathlib import Path

from ahub import cli, doctor, paths
from ahub.providers.base import Health
from ahub.providers.fake import FakeProvider
from ahub.service import HEARTBEAT_KEY
from ahub.store import Store
from ahub.time import now_ms
from tests.conftest import write


def test_python_ok_and_bad():
    c = doctor.check_python((3, 11))
    assert c.name == "python" and c.ok is True and "3.11" in c.detail and not c.fix
    c = doctor.check_python((3, 10))
    assert c.ok is False and "3.10" in c.detail and c.fix


def _fake_run_ok(cmd, **kw):
    assert cmd[:2] == ["/usr/bin/git", "--version"] or cmd[1] == "--version"
    return subprocess.CompletedProcess(cmd, 0, stdout="git version 2.43.0\n", stderr="")


def test_git_ok_missing_fail(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr(subprocess, "run", _fake_run_ok)
    c = doctor.check_git()
    assert c.ok is True and "2.43.0" in c.detail
    monkeypatch.setattr(shutil, "which", lambda name: None)
    c = doctor.check_git()
    assert c.ok is False and c.fix
    def _boom(cmd, **kw):
        raise OSError("no exec")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert doctor.check_git().ok is False


def test_config_ok_and_bad():
    c = doctor.check_config()
    assert c.name == "config" and c.ok is True
    write(paths.global_config_path(), 'lang = "de"\n')
    c = doctor.check_config()
    assert c.ok is False and c.detail and c.fix


def test_service_alive_dead_unit(monkeypatch):
    c = doctor.check_service()
    assert c.ok is False and "install" in c.fix  # no heartbeat, no unit in tmp HOME
    Store().meta_set(HEARTBEAT_KEY, str(now_ms()))
    assert doctor.check_service().ok is True
    Store().meta_set(HEARTBEAT_KEY, str(now_ms() - 600_000))
    unit = Path.home() / ".config" / "systemd" / "user" / "ahub.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    c = doctor.check_service()
    assert c.ok is False and "start" in c.fix and str(unit) in c.detail


def test_opencode_bin_found_missing(monkeypatch, tmp_path):
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake) if name == "opencode" else None)
    c = doctor.check_opencode()
    assert c.ok is True and str(fake) in c.detail
    monkeypatch.setattr(shutil, "which", lambda name: None)
    c = doctor.check_opencode()
    assert c.ok is False and c.fix


def test_opencode_health_ok_bad(monkeypatch):
    from ahub import providers

    monkeypatch.setattr(providers, "get",
                        lambda name: types.SimpleNamespace(health=lambda: Health(True, (), {"version": "1.2.3"})))
    c = doctor.check_opencode_health()
    assert c.ok is True and "1.2.3" in c.detail
    monkeypatch.setattr(providers, "get",
                        lambda name: types.SimpleNamespace(health=lambda: Health(False, ("db gone",), {})))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    c = doctor.check_opencode_health()
    assert c.ok is False and "db gone" in c.detail


def _write_auth(data: dict) -> Path:
    p = doctor.auth_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_auth_providers_only_keys_no_values(monkeypatch, tmp_path):
    secret = "SECRET-KEY-12345"
    _write_auth({"opencode-go": {"apiKey": secret}, "opencode": {"type": "api", "key": secret}})
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no `opencode auth list`, only the file
    provs = doctor.auth_providers()
    assert provs == ["opencode", "opencode-go"]
    assert doctor.has_go_login(provs) is True
    c = doctor.check_opencode_auth()
    assert c.ok is True and "opencode-go" in c.detail
    assert secret not in c.detail and secret not in c.fix
    assert secret not in json.dumps([vars(x) for x in doctor.run_all()], ensure_ascii=False)


def test_auth_list_colors_and_free_only(monkeypatch):
    colored = "\x1b[0m\n\x1b[90mCreds\n\x1b[0m\u25cf  OpenCode Zen \x1b[90mapi\n\u25cf  OpenCode Go \x1b[90mapi\n"
    def _run(cmd, **kw):
        assert cmd[-2:] == ["auth", "list"]
        return subprocess.CompletedProcess(cmd, 0, stdout=colored, stderr="")
    monkeypatch.setattr(subprocess, "run", _run)
    assert doctor._providers_from_auth_list("/fake/opencode") == {"opencode", "opencode-go"}
    # file without go -> free-only detail (auth list disabled)
    def _no_run(cmd, **kw):
        raise OSError("no binary")
    monkeypatch.setattr(subprocess, "run", _no_run)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _write_auth({"opencode": {"k": "v"}})
    c = doctor.check_opencode_auth()
    assert c.ok is True and "opencode" in c.detail
    # no login at all
    p = doctor.auth_file_path()
    if p.exists():
        p.unlink()
    c = doctor.check_opencode_auth()
    assert c.ok is False and "auth login" in c.fix


def test_agy_health_and_missing(monkeypatch, tmp_path):
    """agy found — ok True/False per the provider's health(); not found — "no data" (None)."""
    from ahub import providers
    from ahub.providers.agy import AgyProvider
    from tests.provider_contract import AGY_DATA, agy_state, fake_agy

    def only_agy(path):
        monkeypatch.setattr(shutil, "which", lambda name: str(path) if name == "agy" else None)

    broken = tmp_path / "broken" / "agy"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    broken.chmod(0o755)
    only_agy(broken)
    monkeypatch.setitem(providers._cache, "agy", AgyProvider(binary=str(broken)))
    c = doctor.check_agy()
    assert c.ok is False and "agy" in c.detail and "agy" in c.fix  # no login — a hint

    healthy = AgyProvider(binary=str(fake_agy(tmp_path)), env={"AHUB_AGY_FAKE_DATA": str(AGY_DATA)})
    agy_state(tmp_path)
    only_agy(healthy.binary)
    monkeypatch.setitem(providers._cache, "agy", healthy)
    c = doctor.check_agy()
    assert c.ok is True and "1.2.15-fake" in c.detail and not c.fix

    only_agy(tmp_path / "void" / "agy")
    assert doctor.check_agy().ok is None


def test_codex_health_and_missing(monkeypatch, tmp_path):
    """codex found — ok True/False per the provider's health(); not found — "no data" (None)."""
    from ahub import providers
    from ahub.providers.codex import CodexProvider
    from tests.provider_contract import CODEX_DATA, fake_codex

    def only_codex(path):
        monkeypatch.setattr(shutil, "which", lambda name: str(path) if name == "codex" else None)

    env = {"AHUB_CODEX_FAKE_DATA": str(CODEX_DATA)}
    healthy = CodexProvider(binary=str(fake_codex(tmp_path)), env=env)
    only_codex(healthy.binary)
    monkeypatch.setitem(providers._cache, "codex", healthy)
    c = doctor.check_codex()
    assert c.ok is True and "0.153.4-fake" in c.detail and not c.fix

    class _NoLogin(CodexProvider):
        def login(self):
            return False, "Not logged in"

    broken = _NoLogin(binary=str(fake_codex(tmp_path / "b")), env=env)
    only_codex(broken.binary)
    monkeypatch.setitem(providers._cache, "codex", broken)
    c = doctor.check_codex()
    assert c.ok is False and "codex login" in c.fix and "codex login" in c.detail

    only_codex(tmp_path / "void" / "codex")
    assert doctor.check_codex().ok is None


def _only_codex(monkeypatch, path):
    monkeypatch.setattr(shutil, "which", lambda name: str(path) if name == "codex" else None)


def _no_sandbox_codex(monkeypatch, tmp_path, name="c"):
    """A logged-in codex whose OS sandbox does not start (Ubuntu 24.04 with AppArmor)."""
    from ahub import providers
    from ahub.providers.codex import CodexProvider
    from tests.provider_contract import CODEX_DATA, fake_codex

    class _NoSandbox(CodexProvider):
        def sandbox_ok(self):
            return False, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"

    prov = _NoSandbox(binary=str(fake_codex(tmp_path / name)), env={"AHUB_CODEX_FAKE_DATA": str(CODEX_DATA)})
    _only_codex(monkeypatch, prov.binary)
    monkeypatch.setitem(providers._cache, "codex", prov)
    return prov


def test_codex_sandbox_hint_appArmor(tmp_path, monkeypatch):
    """AppArmor blocks unprivileged user namespaces — both fixes in one hint, the sysctl is the admin's."""
    flag = tmp_path / "apparmor_restrict_unprivileged_userns"
    monkeypatch.setattr(doctor, "APPARMOR_USERNS_FLAG", flag)
    assert doctor.apparmor_blocks_userns() is False  # no such file — AppArmor is not in the kernel
    flag.write_text("1\n")
    assert doctor.apparmor_blocks_userns() is True
    fix = doctor.codex_sandbox_fix()
    assert "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in fix
    assert "/etc/sysctl.d/" in fix  # and how to keep it
    assert 'sandbox = "danger-full-access"' in fix  # the config way

    _no_sandbox_codex(monkeypatch, tmp_path)
    c = doctor.check_codex()
    assert c.ok is False and "bwrap" in c.detail
    assert "sysctl -w" in c.fix and 'danger-full-access' in c.fix
    assert "codex login" not in c.fix  # the login is fine — the sandbox is the problem


def test_codex_sandbox_hint_without_appArmor(tmp_path, monkeypatch):
    """No AppArmor flag (a container without user namespaces): the sysctl would not help — one fix."""
    monkeypatch.setattr(doctor, "APPARMOR_USERNS_FLAG", tmp_path / "no_such_flag")
    fix = doctor.codex_sandbox_fix()
    assert "sysctl" not in fix and 'sandbox = "danger-full-access"' in fix

    flag = tmp_path / "apparmor_restrict_unprivileged_userns"
    flag.write_text("0\n")
    assert doctor.apparmor_blocks_userns() is False  # not restricting — nothing to allow

    _no_sandbox_codex(monkeypatch, tmp_path)
    c = doctor.check_codex()
    assert c.ok is False and "sysctl" not in c.fix and 'danger-full-access' in c.fix


def test_codex_no_sandbox_hint_without_a_broken_sandbox(monkeypatch, tmp_path):
    """A broken codex for another reason gets the login fix only, not the sandbox advice."""
    from ahub import providers
    from ahub.providers.codex import CodexProvider
    from tests.provider_contract import CODEX_DATA, fake_codex

    class _NoLogin(CodexProvider):
        def login(self):
            return False, "Not logged in"

    prov = _NoLogin(binary=str(fake_codex(tmp_path)), env={"AHUB_CODEX_FAKE_DATA": str(CODEX_DATA)})
    _only_codex(monkeypatch, prov.binary)
    monkeypatch.setitem(providers._cache, "codex", prov)
    c = doctor.check_codex()
    assert c.ok is False and "codex login" in c.fix and "danger-full-access" not in c.fix


def test_models_go_and_free_fix():
    assert doctor.check_models(["opencode", "opencode-go"]).ok is True
    c = doctor.check_models(["opencode"])
    assert c.ok is False and "executor" in c.detail
    assert "ahub models role executor --set-default spark-free" in c.fix
    c = doctor.check_models([])
    assert c.ok is False and c.fix


def test_models_fix_commands_execute(capsys):
    """Suggested fix must run as-is: --add when the alias is out of the menu, then --set-default."""
    c = doctor.check_models([])
    assert c.ok is False and c.fix
    assert "--add spark-free" in c.fix  # spark-free is not in role menus by default
    for group in c.fix.split("; "):
        for part in group.split(" && "):
            assert part.startswith("ahub ")
            assert cli.main(part.split()[1:]) == 0
        capsys.readouterr()
    assert doctor.check_models([]).ok is True


class _Scenario(FakeProvider):
    """Fake provider playing one fixed scenario: the probe sends a prompt of its own."""

    scenario = "{}"

    def build_command(self, spec):
        return super().build_command(dataclasses.replace(spec, prompt=self.scenario))


def test_probe_model_answers_silent_error_and_provider(monkeypatch):
    """One tiny live request through the provider: what the model does is what the probe reports."""
    from ahub import providers, registry

    entry = registry.ModelEntry("free1", "fake", "fake/model")

    def _play(scenario: str) -> None:
        prov = _Scenario()
        prov.scenario = scenario
        monkeypatch.setitem(providers._cache, "fake", prov)

    _play('{"session": "ses_p", "steps": [{"event": {"type": "text", "text": "OK"}}]}')
    ok, detail = doctor.probe_model(entry, timeout_s=20)
    assert ok is True and "free1" in detail and "OK" in detail

    _play('{"session": "ses_p", "steps": [{"sleep": 30}]}')  # silent — the case of 2026-10-02
    ok, detail = doctor.probe_model(entry, timeout_s=6)
    assert ok is False and "free1" in detail

    _play('{"session": "ses_p", "steps": [{"event": {"type": "error", "message": "401 Unauthorized"}}],'
          ' "exit": 1}')
    ok, detail = doctor.probe_model(entry, timeout_s=20)
    assert ok is False and "401" in detail

    ok, detail = doctor.probe_model(registry.ModelEntry("x", "no-such-provider", "m"))
    assert ok is False and "no-such-provider" in detail


def test_pick_free_probes_candidates_then_warns(monkeypatch):
    """The first free model is dead — the second becomes the default; none answers — as before + a warning."""
    from ahub import registry

    store = Store()
    tried: list[str] = []

    def _probe(entry, timeout_s=doctor.PROBE_TIMEOUT_S):
        tried.append(entry.alias)
        ok = entry.alias == "bunny"
        return ok, f"{entry.alias}: {'ответил' if ok else 'молчит'}"

    monkeypatch.setattr(doctor, "probing_enabled", lambda: True)
    monkeypatch.setattr(doctor, "probe_model", _probe)
    assert doctor.pick_free(store) == ("bunny", "")
    assert tried == ["spark-free", "bunny"]

    monkeypatch.setattr(doctor, "probe_model", lambda entry, timeout_s=60: (False, "молчит"))
    alias, warning = doctor.pick_free(store)
    assert alias == "spark-free" and "spark-free, bunny" in warning and "ahub doctor" in warning
    # a registry without any free alias — the old fallback, no crash
    monkeypatch.setattr(registry, "free_candidates", lambda store: [])
    assert doctor.pick_free(store) == ("spark-free", "")
    assert doctor._free_alias(store) == "spark-free"


def test_provider_states_from_the_checks(monkeypatch, tmp_path):
    """T50: every provider gets found / logged in / a note / an install hint from the same checks."""
    from ahub import providers
    from ahub.providers.agy import AgyProvider
    from ahub.providers.codex import CodexProvider
    from tests.provider_contract import CODEX_DATA, fake_codex

    opencode = tmp_path / "bin" / "opencode"
    opencode.parent.mkdir(parents=True)
    opencode.write_text("#!/bin/sh\n")
    opencode.chmod(0o755)
    # opencode is there and logged in (auth.json keys only), agy is missing, codex is there without a login
    monkeypatch.setattr(shutil, "which", lambda name: str(opencode) if name == "opencode" else None)
    _write_auth({"opencode-go": {"apiKey": "S3CRET"}, "opencode": {"k": "v"}})
    env = {"AHUB_CODEX_FAKE_DATA": str(CODEX_DATA)}

    class _NoLogin(CodexProvider):
        def login(self):
            return False, "Not logged in"

    codex_fake = fake_codex(tmp_path / "b")
    monkeypatch.setitem(providers._cache, "codex", _NoLogin(binary=str(codex_fake), env=env))
    monkeypatch.setitem(providers._cache, "agy", AgyProvider(binary=str(tmp_path / "void" / "agy")))
    monkeypatch.setattr(shutil, "which",
                        lambda name: {"opencode": str(opencode), "codex": str(codex_fake)}.get(name))

    states = {s.name: s for s in doctor.provider_states()}
    assert list(states) == ["opencode", "agy", "codex"]  # every provider ahub knows
    assert states["opencode"].found and states["opencode"].logged_in
    assert "opencode-go" in states["opencode"].note  # the paid Spark is available
    assert states["opencode"].hint == ""  # nothing to fix
    assert states["agy"].found is False and "Antigravity" in states["agy"].hint
    assert states["codex"].found and not states["codex"].logged_in
    assert "codex login" in states["codex"].hint
    assert "ChatGPT" in states["codex"].note
    assert "S3CRET" not in json.dumps([vars(s) for s in states.values()], ensure_ascii=False)
    # the wizard line: the mark, the state, the note, the hint
    line = doctor.provider_line(states["codex"])
    assert line.startswith("! codex") and "\u00b7 " + states["codex"].note in line
    assert line.splitlines()[-1].strip().startswith("\u2192")
    assert doctor.provider_line(states["agy"]).startswith("\u2717 agy")
    assert doctor.install_hint("no-such") == ""
    assert doctor.provider_state("no-such").found is False


def test_probe_models_runs_at_once_and_recommends(monkeypatch):
    """T50: several models are probed together; the recommendation prefers a paid answerer."""
    from ahub import registry

    store = Store()
    tried: list[str] = []

    def _probe(entry, timeout_s=doctor.PROBE_TIMEOUT_S):
        tried.append(entry.alias)
        return entry.alias in {"bunny", "spark"}, f"{entry.alias}: ok"

    monkeypatch.setattr(doctor, "probing_enabled", lambda: True)
    monkeypatch.setattr(doctor, "probe_model", _probe)
    entries = [registry.get(store, a) for a in ("bunny", "spark", "spark-free")]
    results = doctor.probe_models(entries)
    assert set(tried) == {"bunny", "spark", "spark-free"}  # all three at once
    assert results["bunny"][0] and results["spark"][0] and not results["spark-free"][0]
    assert doctor.recommend_model(entries, results) == "spark"  # paid before free
    assert doctor.recommend_model(entries, {a: (False, "") for a in tried}) == ""
    assert "ahub doctor" in doctor.probe_none_warning(["spark-free", "bunny"])
    # probing off — no request at all, the caller keeps its own fallback
    monkeypatch.setattr(doctor, "probing_enabled", lambda: False)
    assert doctor.probe_models(entries) == {}


def test_probing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AHUB_PROBE", "0")
    assert doctor.probing_enabled() is False
    monkeypatch.setenv("AHUB_PROBE", "1")
    assert doctor.probing_enabled() is True
    monkeypatch.delenv("AHUB_PROBE")
    assert doctor.probing_enabled() is True


def test_network_no_proxy_and_down(monkeypatch):
    for v in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(v, raising=False)
    assert doctor.check_network().ok is True
    import ahub.observer as obs

    monkeypatch.setattr(obs, "proxy_problem", lambda *a, **k: "proxy 1.2.3.4:5 is not responding (X)")
    c = doctor.check_network()
    assert c.ok is False and c.fix


def test_network_proxy_ok(monkeypatch):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
        for v in ("https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(v, raising=False)
        c = doctor.check_network()
        assert c.ok is True and str(port) in c.detail
    finally:
        srv.close()


def _opencode_line(monkeypatch, tmp_path, section: str) -> str:
    """The opencode doctor line with [providers.opencode] in the config (the binary is a fake file)."""
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake) if name == "opencode" else None)
    write(paths.global_config_path(), section)
    return doctor.check_opencode().detail


def test_provider_own_proxy_on_the_line(monkeypatch, tmp_path):
    """A provider with [providers.<name>] — its line shows the proxy and whether it answers."""
    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    assert doctor.provider_proxy_detail("opencode") == ""  # no section — nothing to say
    assert _opencode_line(monkeypatch, tmp_path, "[usage]\ngo_month_limit = 5.0\n") == f"found {tmp_path / 'opencode'}"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # nothing listens there — the proxy does not answer
    detail = _opencode_line(monkeypatch, tmp_path,
                            f'[providers.opencode]\nproxy = "http://user:secret@127.0.0.1:{port}"\n')
    assert f"127.0.0.1:{port}" in detail and "does not answer" in detail
    assert "secret" not in detail  # only host:port — the URL may carry a password

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    live = srv.getsockname()[1]
    try:
        detail = _opencode_line(monkeypatch, tmp_path,
                                f'[providers.opencode]\nproxy = "http://127.0.0.1:{live}"\n')
        assert f"127.0.0.1:{live}" in detail and "answers" in detail
    finally:
        srv.close()

    assert "none" in _opencode_line(monkeypatch, tmp_path, '[providers.opencode]\nproxy = ""\n')


def test_claude_and_skill(monkeypatch, tmp_path):
    from ahub.tg import launcher

    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(launcher, "claude_bin", lambda: str(fake))
    assert doctor.check_claude().ok is True
    monkeypatch.setattr(launcher, "claude_bin", lambda: str(tmp_path / "gone"))
    assert doctor.check_claude().ok is False
    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    c = doctor.check_claude()
    assert c.ok is False and c.fix
    assert doctor.check_claude_skill().ok is False
    p = doctor.skill_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# skill\n", encoding="utf-8")
    # T51: the skill alone is not enough — without Bash(ahub:*) Claude Code asks on every command
    root = tmp_path / "proj"
    c = doctor.check_claude_skill(root)
    assert c.ok is False and "Bash(ahub:*)" in c.detail and c.fix == "ahub setup --claude"
    write(root / ".claude" / "settings.json", '{"permissions": {"allow": ["Bash(git:*)"]}}\n')
    assert doctor.bash_allowed(root) is False
    c = doctor.check_claude_skill(root)
    assert c.ok is False and "Bash(ahub:*)" in c.detail
    write(root / ".claude" / "settings.json", '{"permissions": {"allow": ["Bash(ahub:*)"]}}\n')
    assert doctor.bash_allowed(root) is True
    c = doctor.check_claude_skill(root)
    assert c.ok is True and "Bash(ahub:*)" in c.detail and not c.fix
    # a settings.json that is not JSON is not a permission
    write(root / ".claude" / "settings.json", "{ nope\n")
    assert doctor.bash_allowed(root) is False
    assert doctor.check_claude_skill(root).ok is False


def test_claude_config_override_needs_file_and_exec(tmp_path):
    write(paths.global_config_path(), f'[paths]\nclaude = "{tmp_path}/nope"\n')
    assert doctor.check_claude().ok is False
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")  # not executable yet
    write(paths.global_config_path(), f'[paths]\nclaude = "{fake}"\n')
    assert doctor.check_claude().ok is False
    fake.chmod(0o755)
    c = doctor.check_claude()
    assert c.ok is True and str(fake) in c.detail


def test_telegram_off_configured(monkeypatch):
    assert doctor.check_telegram().ok is None  # no [telegram] token
    write(paths.global_config_path(), 'projects = []\n[telegram]\ntoken = "bot123"\nchat_id = 1\n')
    import importlib.util as iu

    monkeypatch.setattr(iu, "find_spec", lambda name: object())
    assert doctor.check_telegram().ok is True
    monkeypatch.setattr(iu, "find_spec", lambda name: None)
    c = doctor.check_telegram()
    assert c.ok is False and "ahub[telegram]" in c.fix


def test_run_all_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(doctor, "check_git", _boom)
    checks = doctor.run_all()
    assert len(checks) == 14
    git = next(c for c in checks if c.name == "git")
    assert git.ok is False and "boom" in git.detail


def test_cli_codes_and_json(capsys, monkeypatch):
    secret = "SECRET-CLI-999"
    _write_auth({"opencode": {"apiKey": secret}})
    assert cli.main(["--json", "doctor"]) in (0, 1)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data["checks"], list) and len(data["checks"]) == 14
    assert secret not in out
    for c in data["checks"]:
        assert set(c) == {"name", "ok", "detail", "fix"}
        assert c["ok"] in (True, False, None)
    # forced all-ok -> exit 0, one fail -> exit 1 with marks and fix arrow
    monkeypatch.setattr(doctor, "run_all",
                        lambda: [doctor.Check("python", True, "d", ""),
                                 doctor.Check("git", None, "d", "")])
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "\u2713" in out and "\u2013" in out
    monkeypatch.setattr(doctor, "run_all",
                        lambda: [doctor.Check("python", False, "bad", "fix it")])
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "\u2717" in out and "\u2192" in out and "fix it" in out
