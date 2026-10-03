"""i18n step 2: the EN/RU catalog, language choice, CLI output in both languages."""

from __future__ import annotations

import re
import string

import pytest

from ahub import cli, paths
from ahub.i18n import _reset, lang, set_lang, t


def test_keys_match_and_order():
    from ahub.i18n.en import MESSAGES as en
    from ahub.i18n.ru import MESSAGES as ru

    assert set(en) == set(ru) and len(en) > 50
    assert list(en) == list(ru)


def _fields(tpl: str) -> set[str]:
    out = set()
    for _, field, _, _ in string.Formatter().parse(tpl):
        if field is not None:
            out.add(field.split(".")[0].split("[")[0])
    return out


def test_placeholders_match():
    from ahub.i18n.en import MESSAGES as en
    from ahub.i18n.ru import MESSAGES as ru

    for key in en:
        assert _fields(en[key]) == _fields(ru[key]), key


def test_unknown_key():
    with pytest.raises(KeyError):
        t("no.such.key")


def test_lang_order(monkeypatch, tmp_path):
    from ahub import config

    # AHUB_LANG beats the config and LANG
    monkeypatch.setenv("AHUB_LANG", "en")
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text('lang = "ru"\n', encoding="utf-8")
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    _reset()
    assert lang() == "en"
    # config beats LANG
    monkeypatch.delenv("AHUB_LANG")
    _reset()
    assert lang() == "ru"
    assert config.load_hub().lang == "ru"
    # LANG ru → ru
    paths.global_config_path().write_text("", encoding="utf-8")
    _reset()
    assert lang() == "ru"
    # LC_MESSAGES ru → ru
    monkeypatch.delenv("LANG")
    monkeypatch.setenv("LC_MESSAGES", "ru_RU.UTF-8")
    _reset()
    assert lang() == "ru"
    # default is en
    monkeypatch.delenv("LC_MESSAGES")
    _reset()
    assert lang() == "en"


def test_config_lang_invalid(tmp_path):
    from ahub import config

    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text('lang = "de"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="lang"):
        config.load_hub()


def _run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_version_status_errors_both_langs(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for code, no_task, no_proj in (("ru", "нет задачи", "не относится"),
                                   ("en", "no such task", "belongs to no project")):
        monkeypatch.setenv("AHUB_LANG", code)
        _reset()
        assert lang() == code
        rc, out, _ = _run(capsys, "version")
        assert rc == 0 and out.startswith("ahub 3.")
        rc, _, err = _run(capsys, "status", "T99")
        assert rc == 2 and no_task in err
        rc, _, err = _run(capsys, "config")
        assert rc == 2 and no_proj in err
    rc, _, err = _run(capsys, "ack", "xx")
    assert rc == 2 and ("event numbers" in err or "номера событий" in err)


def test_lang_flag_overrides_env(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AHUB_LANG", "ru")
    _reset()
    rc, _, err = _run(capsys, "--lang", "en", "status", "T99")
    assert rc == 2 and "no such task" in err
    _reset()
    rc, _, err = _run(capsys, "status", "T99")
    assert rc == 2 and "нет задачи" in err


def test_set_lang_bad():
    with pytest.raises(ValueError):
        set_lang("de")


def test_locale_first_nonempty_wins(monkeypatch):
    from ahub import i18n

    monkeypatch.delenv("AHUB_LANG", raising=False)
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    i18n._reset()
    assert i18n.lang() == "en"
    monkeypatch.setenv("LC_ALL", "")
    i18n._reset()
    assert i18n.lang() == "ru"


def test_views_events_en(monkeypatch, tmp_path):
    """Step 3: L1/L2 and the DONE line in English (AHUB_LANG=en)."""
    from ahub import events, transitions, views
    from ahub.model import State
    from ahub.scope import Scope
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    assert lang() == "en"
    store = Store()
    now = 1_000_000_000_000
    active = store.create_task(project="P", kind="scout", title="find leak", executor="fake", now=now)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, active, st, now=now)
    store.update_task(active, phase="studying", round=2, now=now)
    waiting = store.create_task(project="P", kind="code", title="pay button", now=now)
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, waiting, st, reason="report ready",
                         payload={"summary": "found it", "report_bytes": 2150,
                                  "cost_go": 0.04, "cost_usd": 0.01}, now=now)
    queued = store.create_task(project="P", kind="scout", title="later", now=now)
    assert queued

    l1 = views.status_text(store, now=now, w=100)
    assert "studying" in l1 and "1 queued" in l1
    assert "unread events" in l1
    assert "DONE T2" in l1  # codes are not translated

    done_ev = [e for e in store.events(task_id=waiting, needs_reaction=True) if e.kind == "done"][0]
    line = events.format_line(done_ev, store.get_task(waiting))
    assert line.startswith("DONE T2 code «pay button» — report 2.1 KB")
    assert "$0.04" in line and "real" in line
    assert "отчёт" not in line and "КБ" not in line

    wt = tmp_path / "wt"
    (wt / ".ahub").mkdir(parents=True)
    (wt / ".ahub" / "report.md").write_text("## Summary\nleak in core/a.py:1\n", encoding="utf-8")
    (wt / ".ahub" / "result.json").write_text('{"summary": "found it"}', encoding="utf-8")
    store.update_task(waiting, worktree=str(wt), now=now)
    l2 = views.task_text(store, store.get_task(waiting), now=now, w=100)
    assert "State" in l2 and "done" in l2 and "Summary" in l2 and "found it" in l2
    assert "Report" in l2 and "KB" in l2
    assert "Next" in l2 and "ahub accept T2" in l2

    assert views.status_text(store, scope=Scope(("NOPE",)), now=now).startswith("quiet:")
    import re as _re

    assert not _re.search(r"[а-яА-ЯёЁ]", l1) and not _re.search(r"[а-яА-ЯёЁ]", l2 + line)


def test_words_is_a_real_mapping():
    from ahub import archive

    set_lang("en")
    words = dict(archive.STATE_WORDS)
    assert len(words) == len(archive.STATE_WORDS) > 0
    assert all(isinstance(v, str) and v for v in words.values())
    assert archive.STATE_WORDS.get("no-such-state", "x") == "x"


def test_step5_tasks_config_en(monkeypatch):
    """Step 5: task and config errors in English (AHUB_LANG=en)."""
    import sys

    from ahub import config, registry, tasks
    from ahub.model import Kind
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    assert lang() == "en"
    store = Store()
    project = config.parse_project(
        {"schema_version": 2, "name": "P", "python": sys.executable, "allowed_paths": ["core/**"]}, "/tmp")
    try:
        tasks.resolve(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title=""), project, collect=False)
        raise AssertionError("must fail")
    except tasks.TaskInvalid as e:
        errs = " | ".join(e.errors)
        assert "need a goal" in errs and "allowed files" in errs
        assert "нужна цель" not in errs
    try:
        config.parse_project({"schema_version": 2}, "/tmp")
        raise AssertionError("must fail")
    except config.ConfigError as e:
        assert "required field" in str(e) and "обязательное поле" not in str(e)
    try:
        registry.get(store, "nope")
        raise AssertionError("must fail")
    except registry.RegistryError as e:
        assert "no model" in str(e)


def test_step5_transitions_drafts_en(monkeypatch, tmp_path):
    """Step 5: transitions and drafts in English (AHUB_LANG=en)."""
    from ahub import config, drafts, prepare, transitions
    from ahub.model import State
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    assert lang() == "en"
    store = Store()
    try:
        transitions.move(store, 999, State.QUEUED)
        raise AssertionError("must fail")
    except transitions.TransitionError as e:
        assert "no task T999" in str(e)
    from ahub import reasons

    q = store.create_task(project="P", kind="scout", title="x")
    assert transitions.request_stop(store, q) == "stopped"
    stored = store.get_task(q).state_reason
    assert stored == '{"code":"stop_command"}'  # a code, not translated text
    assert reasons.text(stored) == "stopped by command"
    assert drafts.preview(store, 999) == "no draft #999"
    project = config.parse_project(
        {"schema_version": 2, "name": "P", "hooks": {"task_setup": "exit 3"}}, str(tmp_path))
    task = store.create_task(project="P", kind="scout", title="x")
    full = store.get_task(task)
    try:
        prepare.run_hook(project, "task_setup", full, str(tmp_path))
        raise AssertionError("must fail")
    except prepare.PrepareError as e:
        assert "hook task_setup: exit 3" in str(e) and "хук" not in str(e)


def test_step5_no_cyrillic_in_en_preview(monkeypatch, tmp_path):
    """Step 5: draft preview in English, no Cyrillic."""
    import json
    import re as _re

    from ahub import config, drafts
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    store = Store()
    project = config.parse_project({"schema_version": 2, "name": "P"}, str(tmp_path))
    did = drafts.create(store, project, "button", run_model=False)
    drafts._update(store, did, status="ready",
                   task_json=json.dumps({"kind": "code", "title": "button", "spec": "do it",
                                         "paths": ["core/**"], "accept": [], "review_level": 2}))
    text = drafts.preview(store, did)
    assert text.startswith("Draft #") and "Review: level 2" in text
    assert not _re.search(r"[а-яА-ЯёЁ]", text)


def test_step6_engine_gates_en(monkeypatch, tmp_path):
    """Step 6: engine reasons and gates in English (AHUB_LANG=en)."""
    import re as _re

    from ahub import config, gates, reasons
    from ahub.engine import Engine
    from ahub.model import State
    from ahub.providers.base import Outcome, RunResult
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    assert lang() == "en"
    store = Store()
    project = config.parse_project({"schema_version": 2, "name": "P"}, str(tmp_path))
    eng = Engine(store, project, 999)
    st, reason = eng._outcome_to_state(RunResult(Outcome.QUOTA, None, error="boom"))
    assert st is State.NEEDS_DECISION and reason == '{"code":"quota","err":"boom"}'
    assert reasons.text(reason) == "provider quota: boom"
    st, reason = eng._outcome_to_state(RunResult(Outcome.TIMEOUT, None, error="60"))
    assert "task time limit" in reasons.text(reason)
    g = gates.GateResult(base="b", head="h", tests_ok=False, diffstat="")
    assert "acceptance is red" in g.summary()
    assert not _re.search(r"[а-яА-ЯёЁ]", reasons.text(reason) + g.summary())


def test_step6_accept_service_en(monkeypatch, tmp_path):
    """Step 6: accept errors and queue reasons in English (AHUB_LANG=en)."""
    import re as _re

    import pytest

    from ahub import accept, config, reasons, service, tasks, transitions
    from ahub.model import Kind, State
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    store = Store()
    project = config.parse_project({"schema_version": 2, "name": "P", "max_parallel": 1,
                                    "worktrees": str(tmp_path / "wt")}, str(tmp_path))
    with pytest.raises(accept.DecisionError, match="no task T999"):
        accept.reject(store, project, 999)
    from tests.enginekit import install_fake

    install_fake(store, [])
    a = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="a",
                                           model="fake"), project, collect=False)
    transitions.move(store, a.id, State.STOPPED)
    b = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="b",
                                           model="fake", after=[a.id]), project, collect=False)
    svc = service.Service(store, [project], spawn=lambda tid: 9999, proc_root=tmp_path / "proc",
                          lock_busy=lambda p: False)
    (tmp_path / "proc").mkdir(exist_ok=True)
    svc.tick()
    stored = store.get_task(b.id).state_reason
    dep_reason = reasons.text(stored)
    assert dep_reason.startswith("waiting for T") and "to be accepted" in dep_reason
    assert stored == '{"code":"wait_accept","task":"T1","state":"stopped"}'
    assert not _re.search(r"[а-яА-ЯёЁ]", dep_reason)


def test_step6_pulse_providers_en(monkeypatch, tmp_path):
    """Step 6: pulse and provider reasons in English (AHUB_LANG=en)."""
    import re as _re

    from ahub import pulse, transitions
    from ahub.model import State
    from ahub.providers import opencode_db as odb
    from ahub.providers.fake import FakeProvider
    from ahub.providers.opencode import OpencodeProvider
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    store = Store()
    tid = store.create_task(project="P", kind="code", title="x", now=0)
    transitions.move(store, tid, State.PREPARING, now=0)
    transitions.move(store, tid, State.WORKING, now=0)
    p = pulse.task_pulse(store, store.get_task(tid), live={}, now=10)
    assert p.state == "dead" and p.reason == "no task process"
    _outcome, err = FakeProvider().classify(exit_code=0, activities=[], session_id=None, stderr_tail="")
    assert "no session id" in err
    st = odb.check_schema(tmp_path / "missing.db")
    assert not st.ok and any("no database at" in x for x in st.problems)
    h = OpencodeProvider(db_path=str(tmp_path / "no.db"), binary="/nonexistent-xyz").health()
    assert not h.ok and "no opencode executable" in h.problems[0]
    assert not _re.search(r"[а-яА-ЯёЁ]", p.reason + err + h.problems[0])


def test_every_literal_key_in_the_code_is_in_the_catalogues():
    """`t("a.b")` with a literal is checked here: a key that exists in the code but in neither
    catalogue only shows up as a KeyError in front of a person (`ahub alarms --ack` with two alarms)."""
    import ast
    from pathlib import Path

    from ahub.i18n.en import MESSAGES as en
    from ahub.i18n.ru import MESSAGES as ru

    used: set[str] = set()
    src = Path(__file__).resolve().parents[1] / "ahub"  # the tests run from anywhere
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("t", "_t")):
                continue
            key = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
            if isinstance(key, str) and re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", key):
                used.add(key)
    assert len(used) > 200  # the scan really found the calls
    assert not sorted(used - set(en)), sorted(used - set(en))
    assert not sorted(used - set(ru)), sorted(used - set(ru))


def _code_keys() -> tuple[set[str], set[str]]:
    """The keys the code asks for by name, and the prefixes it builds at run time —
    `t("setup.sum_" + k)`, `t(f"cli.group_{key}")`, `Words("archive.state_", …)`."""
    import ast
    from pathlib import Path

    full = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
    prefix = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]*_?\.?$")
    literal: set[str] = set()
    prefixes: set[str] = set()
    src = Path(__file__).resolve().parents[1] / "ahub"  # the tests run from anywhere
    for path in sorted(src.rglob("*.py")):
        if path.name in ("en.py", "ru.py") and path.parent.name == "i18n":
            continue  # the catalogues themselves — a key there is a key asked for by nobody
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
                head = node.values[0].value  # the f-string prefix: f"cli.group_{key}"
                if isinstance(head, str) and "." in head:
                    prefixes.add(head)
                continue
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value):
                continue
            value = node.value
            if value[-1] in "_." or prefix.fullmatch(value) and not full.fullmatch(value):
                prefixes.add(value)  # a tail is added to it at run time
            elif full.fullmatch(value):
                literal.add(value)
    return literal, prefixes


def test_no_key_is_orphaned():
    """A key nothing reads is a translation nobody keeps in sync — both catalogues only."""
    from ahub.i18n.en import MESSAGES as en
    from ahub.i18n.ru import MESSAGES as ru

    literal, prefixes = _code_keys()
    orphans = sorted(k for k in en if k not in literal and not any(k.startswith(p) for p in prefixes))
    assert not orphans, orphans
    assert not sorted(set(ru) - set(en))  # the same list, the same way round
