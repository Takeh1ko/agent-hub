"""The ahub setup wizard: interactive, --yes, providers and models, service, Claude, Telegram, the config."""

from __future__ import annotations

import sys
from pathlib import Path

from ahub import cli, config, doctor, paths, registry
from ahub.model import Role
from ahub.store import Store
from tests.conftest import write
from tests.enginekit import make_repo


def _tty(monkeypatch, yes: bool = True):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: yes)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: yes)


def _answers(monkeypatch, items: list[str]):
    it = iter(items)

    def _fake(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("лишний input()")

    monkeypatch.setattr("builtins.input", _fake)


def _no_go(monkeypatch):
    monkeypatch.setattr(doctor, "auth_providers", lambda *a, **k: [])


def _fake_providers(monkeypatch, **states) -> list[doctor.ProviderState]:
    """Fixed provider states for the wizard: name → (found, logged in); opencode and agy on, codex missing."""
    plan = {"opencode": (True, True), "agy": (True, True), "codex": (False, False)}
    plan.update(states)
    out = []
    for name, (found, logged) in plan.items():
        hint = doctor.install_hint(name) if not found else ("" if logged else f"{name}: войти в терминале")
        out.append(doctor.ProviderState(name, found, logged, detail=f"found /bin/{name}" if found else "нет",
                                        note=f"заметка {name}", hint=hint))
    monkeypatch.setattr(doctor, "provider_states", lambda *a, **k: list(out))
    return out


def _fake_claude(monkeypatch, tmp_path: Path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: str(fake))
    return fake


def _probe_stub(monkeypatch, answering: set[str]) -> list[str]:
    """Live probe stub: only the aliases in `answering` answer. Returns the list of probed aliases."""
    tried: list[str] = []

    def _probe(entry, timeout_s=doctor.PROBE_TIMEOUT_S):
        tried.append(entry.alias)
        ok = entry.alias in answering
        return ok, f"{entry.alias}: {'ответил' if ok else 'молчит'}"

    monkeypatch.setattr(doctor, "probing_enabled", lambda: True)
    monkeypatch.setattr(doctor, "probe_model", _probe)
    return tried


def _enabled(name: str) -> bool:
    return config.load_hub().provider_enabled(name)


def _defaults() -> dict[str, str]:
    return {role.value: next(e.alias for e, d in registry.menu(Store(), role) if d) for role in Role}


def test_wizard_full_lang_project_free_telegram(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    _fake_claude(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    root = tmp_path / "shop"
    make_repo(root)
    write(paths.global_config_path(), '# мой хаб\n[usage]\ngo_month_limit = 60.0\n')
    _answers(monkeypatch, [
        "",  # language: the ru default
        str(root),  # project path
        "",  # providers: the default (found and logged in)
        "",  # models: yes (default)
        "n",  # service install: no
        "",  # service start: no (default)
        "",  # claude: yes (default)
        "y",  # telegram: yes
        "tok123",  # token
        "77",  # chat id
    ])
    assert cli.main(["setup"]) == 0
    capsys.readouterr()
    hub = config.load_hub()
    assert hub.lang == "ru"
    assert str(root) in hub.projects
    assert (root / ".hub.toml").exists()
    assert config.load_project(root).name == "shop"
    # no Go login — the roles are on the free alias
    assert doctor.check_models([]).ok is True
    free = doctor._free_alias(Store())
    for role in Role:
        menu = registry.menu(Store(), role)
        default = next((e for e, d in menu if d), None)
        assert default is not None and default.alias == free
    # Telegram is written, the sections and the comment stay intact
    assert hub.tg_token == "tok123" and hub.tg_chat_id == 77
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "# мой хаб" in text and "[usage]" in text and "go_month_limit = 60.0" in text
    assert "[telegram]" in text
    # the provider switch: found and logged in are on, the missing one cannot be
    assert _enabled("opencode") and _enabled("agy") and not _enabled("codex")
    # the skill and the block
    assert (Path.home() / ".claude/skills/ahub/SKILL.md").exists()
    assert "ahub:begin" in (root / "CLAUDE.md").read_text(encoding="utf-8")


def test_wizard_lists_every_provider_and_never_enables_a_missing_one(tmp_path, monkeypatch, capsys):
    """T50: opencode found+logged in, agy found without a login, codex missing (hint, never enabled)."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch, agy=(True, False), codex=(False, False))
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    root = tmp_path / "prov"
    make_repo(root)
    _answers(monkeypatch, [
        "", str(root),
        "",  # providers: Enter — found and logged in (opencode only)
        "",  # models: the free default (no probe in the test env)
        "n",  # service install: no
        "n",  # service start: no
        "n",  # telegram: no
    ])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "opencode" in out and "agy" in out and "codex" in out
    assert "npm i -g @openai/codex" in out  # the one-line install hint for the missing one
    assert "заметка agy" in out  # and a note for every provider
    assert _enabled("opencode") is True
    assert _enabled("agy") is False  # found, but not logged in — the rule does not enable it
    assert _enabled("codex") is False
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "[providers.opencode]" in text and "[providers.agy]" in text and "[providers.codex]" in text
    assert text.count("enabled = false") == 2


def test_wizard_reasks_on_an_unknown_provider(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    root = tmp_path / "typo"
    make_repo(root)
    _answers(monkeypatch, ["", str(root), "oops", "agy", "", "n", "n", "n"])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "oops" in out  # the mistake is named, with the known providers
    assert _enabled("opencode") is False and _enabled("agy") is True and _enabled("codex") is False


def test_wizard_asks_the_default_per_role_and_never_offers_a_dead_model(tmp_path, monkeypatch, capsys):
    """T50: every model is probed with ✓/✗; the executor gets a free alias, the reviewer a paid one."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    tried = _probe_stub(monkeypatch, {"spark", "bunny"})
    root = tmp_path / "pick"
    make_repo(root)
    _answers(monkeypatch, [
        "", str(root),
        "",  # providers: the default
        "bunny",  # executor — a free model that answered
        "spark",  # reviewer — a paid model that answered
        "n", "n", "n",  # service install, service start, telegram: no
    ])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "✓" in out and "✗" in out
    assert "spark-free" in out  # the dead model is shown, with the mark
    # every model of a provider that is on is probed once (concurrently — the order is not fixed)
    assert set(tried) == {"bunny", "deepseek-flash", "gemini", "gemini-low", "mimo-flash", "spark",
                          "spark-free", "spark-high", "spark-medium"}
    assert "codex" not in tried  # a provider that is off is never probed
    assert _defaults()["executor"] == "bunny" and _defaults()["reviewer"] == "spark"
    assert _defaults()["scout"] == "spark"  # its own default answered
    assert _defaults()["observer"] == "bunny"  # spark-high is dead — it follows the executor
    assert "executor=bunny" in out and "reviewer=spark" in out


def test_wizard_role_answer_must_have_answered(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    _probe_stub(monkeypatch, {"spark"})
    root = tmp_path / "dead"
    make_repo(root)
    _answers(monkeypatch, ["", str(root), "", "spark-free", "1", "", "n", "n", "n"])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "spark-free" in out  # named as the answer that cannot be used
    assert _defaults()["executor"] == "spark"  # then the number of the one that answered
    assert _defaults()["reviewer"] == "spark"  # Enter takes the recommended one


def test_a_provider_without_a_login_is_never_probed(tmp_path, monkeypatch, capsys):
    """T50: agy is enabled by hand but nobody is logged in — its models are not probed and not offered."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch, agy=(True, False))
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    tried = _probe_stub(monkeypatch, {"spark", "bunny"})
    root = tmp_path / "nolog"
    make_repo(root)
    _answers(monkeypatch, ["", str(root), "opencode,agy", "", "", "n", "n", "n"])
    assert cli.main(["setup"]) == 0
    capsys.readouterr()
    assert _enabled("agy") is True  # the user turned it on anyway
    assert not [a for a in tried if a.startswith("gemini")]  # but no login — no probe
    assert _defaults()["executor"] == "spark"


def test_roles_with_an_unoffered_default_follow_the_executor(tmp_path, monkeypatch, capsys):
    """A role default that is not offered (its provider is off) is not working — the role follows the executor."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)  # opencode and agy are logged in, codex is missing
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    tried = _probe_stub(monkeypatch, {"gemini"})
    root = tmp_path / "roles"
    make_repo(root)
    _answers(monkeypatch, [
        "", str(root),
        "agy",  # providers: only agy — the opencode models are not offered
        "", "",  # executor and reviewer: Enter (gemini answered)
        "n", "n", "n",  # service install, service start, telegram: no
    ])
    assert cli.main(["setup"]) == 0
    capsys.readouterr()
    assert _enabled("opencode") is False and _enabled("agy") is True
    assert tried and all(a.startswith("gemini") for a in tried)  # only the live provider is probed
    # every role, including the ones whose default was spark/spark-high of a switched-off provider
    assert set(_defaults().values()) == {"gemini"}
    store = Store()
    for role in Role:
        assert registry.pick(store, role, None).alias == "gemini"
        assert registry.menu(store, role), role.value  # the menu of every role has what it offers
    assert doctor.check_models([]).ok is True


def test_wizard_service_install_writes_units(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    from ahub.commands import service as svccmd
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    calls: list[tuple[str, list[str], list[str]]] = []

    def _fake_enable(os_kind, names, written):
        calls.append((os_kind, list(names), list(written)))
        return []

    monkeypatch.setattr(svccmd, "enable_service", _fake_enable)
    monkeypatch.setattr(svccmd, "wait_for_heartbeat", lambda timeout_s=15.0: 3)
    root = tmp_path / "proj"
    make_repo(root)
    _answers(monkeypatch, [
        "", str(root), "", "", "y",  # providers, models, install: yes
        "n",  # telegram: no
    ])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    unit = Path.home() / ".config" / "systemd" / "user" / "ahub.service"
    assert unit.exists()
    assert "systemctl --user" in out
    # T48: after agreeing the wizard enables the service and checks the heartbeat.
    assert calls and calls[0][0] == "linux" and calls[0][1] == ["ahub.service"]
    assert "loginctl enable-linger" in out


def test_yes_passes_without_input(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)

    def _boom(_prompt=""):
        raise AssertionError("input() при --yes")

    monkeypatch.setattr("builtins.input", _boom)
    root = tmp_path / "auto"
    make_repo(root)
    assert cli.main(["setup", str(root), "--yes"]) == 0
    out = capsys.readouterr().out
    assert str(root) in config.load_hub().projects
    assert (root / ".hub.toml").exists()
    assert doctor.check_models([]).ok is True
    assert _enabled("opencode") and _enabled("agy") and not _enabled("codex")
    assert "поставщики включены: opencode, agy" in out  # the summary of the automatic choice


def test_yes_picks_the_recommendation_paid_first(tmp_path, monkeypatch, capsys):
    """T50: --yes takes the recommended model — a paid one that answered, before a free one."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    tried = _probe_stub(monkeypatch, {"spark", "bunny"})
    root = tmp_path / "rec"
    make_repo(root)
    assert cli.main(["setup", str(root), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "spark" in tried and "bunny" in tried
    assert _defaults()["executor"] == "spark" and _defaults()["reviewer"] == "spark"
    assert "executor=spark" in out


def test_yes_picks_the_free_model_when_no_paid_one_answers(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    tried = _probe_stub(monkeypatch, {"bunny"})
    root = tmp_path / "recfree"
    make_repo(root)
    assert cli.main(["setup", str(root), "--yes"]) == 0
    capsys.readouterr()
    assert tried, "the probe ran"
    assert set(_defaults().values()) == {"bunny"}
    assert doctor.check_models([]).ok is True


def test_noninteractive_keeps_old_behavior_plus_free(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    root = tmp_path / "oldway"
    make_repo(root)
    assert cli.main(["setup", str(root), "--claude"]) == 0
    out = capsys.readouterr().out
    assert "шаблон v2" in out or "template" in out
    assert (root / ".hub.toml").exists()
    assert doctor.check_models([]).ok is True
    assert _enabled("opencode") is True and _enabled("codex") is False


def test_free_default_picks_the_candidate_that_answers(tmp_path, monkeypatch, capsys):
    """The first free model is dead — setup defaults to the one that answers, not to the dead one."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    tried = _probe_stub(monkeypatch, {"bunny"})
    root = tmp_path / "probe"
    make_repo(root)
    _answers(monkeypatch, ["", str(root), "", "", "", "n", "n", "n"])  # providers, executor, reviewer, service, tg
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "spark-free" in tried and "bunny" in tried
    assert "bunny" in out
    assert set(_defaults().values()) == {"bunny"}
    assert doctor.check_models([]).ok is True


def test_no_free_model_answers_warns_and_keeps_the_old_default(tmp_path, monkeypatch, capsys):
    """--yes: the probe still runs; with nothing answering — the old behaviour and a warning with a hint."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    tried = _probe_stub(monkeypatch, set())
    root = tmp_path / "dead"
    make_repo(root)
    assert cli.main(["setup", str(root), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "spark-free" in tried and "bunny" in tried
    assert "ahub doctor" in out and "ahub models check" in out
    assert set(_defaults().values()) == {"spark-free"}  # as before


def test_set_global_keeps_comments_and_sections(tmp_path, monkeypatch):
    from ahub.commands.setup import set_global

    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), '# коммент\nprojects = ["/srv/old"]\n\n[usage]\ngo_month_limit = 10.0\n')
    set_global("lang", "ru")
    set_global("token", "t", section="telegram")
    set_global("chat_id", 5, section="telegram")
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "# коммент" in text and 'lang = "ru"' in text
    assert "[telegram]" in text and 'token = "t"' in text and "chat_id = 5" in text
    assert "[usage]" in text and "go_month_limit = 10.0" in text
    hub = config.load_hub()
    assert hub.lang == "ru" and hub.tg_token == "t" and hub.tg_chat_id == 5


def test_wizard_enable_failure_continues(tmp_path, monkeypatch, capsys):
    """T48: enable fails — the wizard prints cmd+error, dead hint, and continues."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    from ahub.commands import service as svccmd
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    monkeypatch.setattr(svccmd, "enable_service",
                        lambda os_kind, names, written: [(["systemctl", "--user", "enable", "--now",
                                                           *names], "boom")])
    monkeypatch.setattr(svccmd, "wait_for_heartbeat", lambda timeout_s=15.0: None)
    root = tmp_path / "fail"
    make_repo(root)
    _answers(monkeypatch, ["", str(root), "", "", "y", "n"])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "systemctl" in out and "boom" in out  # command and error printed
    assert "loginctl enable-linger" in out  # Linux hint still printed, setup did not crash


def test_yes_installs_without_enable(tmp_path, monkeypatch, capsys):
    """T48: --yes writes units but does not enable."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    from ahub.commands import service as svccmd

    def _boom(*a, **k):
        raise AssertionError("enable при --yes без --service")

    monkeypatch.setattr(svccmd, "enable_service", _boom)
    monkeypatch.setattr(svccmd, "wait_for_heartbeat", _boom)
    root = tmp_path / "yesno"
    make_repo(root)
    assert cli.main(["setup", str(root), "--yes"]) == 0
    capsys.readouterr()
    assert (Path.home() / ".config" / "systemd" / "user" / "ahub.service").exists()


def test_yes_service_enables_without_questions(tmp_path, monkeypatch, capsys):
    """T48: --yes --service installs and enables without input()."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    from ahub.commands import service as svccmd

    def _boom(_prompt=""):
        raise AssertionError("input() при --yes --service")

    monkeypatch.setattr("builtins.input", _boom)
    seen: list[tuple[str, list[str], list[str]]] = []
    monkeypatch.setattr(svccmd, "enable_service",
                        lambda os_kind, names, written: seen.append((os_kind, names, written)) or [])
    monkeypatch.setattr(svccmd, "wait_for_heartbeat", lambda timeout_s=15.0: 2)
    root = tmp_path / "yessvc"
    make_repo(root)
    assert cli.main(["setup", str(root), "--yes", "--service"]) == 0
    out = capsys.readouterr().out
    assert seen and seen[0][0] == "linux"
    assert "loginctl enable-linger" in out


def test_service_flag_skips_wizard_questions(tmp_path, monkeypatch, capsys):
    """T48: --service in a TTY wizard enables without the service questions."""
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_providers(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    from ahub.commands import service as svccmd
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    seen: list = []
    monkeypatch.setattr(svccmd, "enable_service",
                        lambda os_kind, names, written: seen.append((os_kind, names)) or [])
    monkeypatch.setattr(svccmd, "wait_for_heartbeat", lambda timeout_s=15.0: 1)
    root = tmp_path / "flag"
    make_repo(root)
    # no "y" for service here — the flag skips the questions entirely.
    _answers(monkeypatch, ["", str(root), "", "", "n"])
    assert cli.main(["setup", "--service"]) == 0
    capsys.readouterr()
    assert seen and seen[0][0] == "linux"
