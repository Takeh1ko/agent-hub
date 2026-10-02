"""i18n step 7: prompts to models in English, the answer language follows the hub language."""

from __future__ import annotations

import re

_CYR = re.compile(r"[а-яА-ЯёЁ]")
# Cyrillic allowed in prompts when lang is ru: the essence heading, the final-word example,
# the arbiter and orchestrator headings (plan item 3; review/views understand both).
_ALLOWED_RU = ["Суть", "готово", "заблокировано", "причина", "Решение", "решения",
               "арбитра", "арбитр", "оркестратора", "оркестратор", "Указания", "доработка"]


def _strip_allowed(text: str) -> str:
    out = text
    for w in _ALLOWED_RU:
        out = out.replace(w, "").replace(w.capitalize(), "")
    # "## Суть" without the hashes is stripped too, via "Суть"
    return out


def _collect(lang: str, tmp_path) -> list[str]:
    import os

    os.environ["AHUB_LANG"] = lang
    from ahub.i18n import _reset

    _reset()
    from ahub import drafts, gates, observer, prompts, review
    from ahub.model import Kind
    from ahub.providers import opencode as oc
    from ahub.store import Store
    from ahub.tg import launcher
    from tests.enginekit import make_project

    store = Store()
    project = make_project(tmp_path)
    tid = store.create_task(project="P", kind="scout", title="find leak")
    task = store.get_task(tid)
    code_tid = store.create_task(project="P", kind="code", title="fix")
    code_task = store.get_task(code_tid)
    # code_task needs limits for code_delivery
    store.update_task(code_tid, limits={"paths": ["core/**"], "accept": ["tests/test_a.py::test_x"]})
    code_task = store.get_task(code_tid)

    gate = gates.GateResult(base="b", head="h", diffstat="1 file")
    out = [
        prompts.DEFAULT_RULES,
        prompts.SCOUT_DELIVERY,
        prompts.CONTINUE_PROMPT,
        prompts.STOP_PROMPT,
        prompts.scout_delivery(),
        prompts.repair_prompt("no result"),
        prompts.stop_prompt(),
        prompts.code_delivery(code_task),
        prompts.scout_prompt(project, task),
        prompts.code_prompt(project, code_task),
        prompts.reply_language_line(),
        prompts.report_heading(),
        prompts.arbiter_heading(),
        prompts.orchestrator_heading(),
        prompts.orchestrator_heading(rework=True),
        prompts.final_line(),
        review.review_prompt(project, task, "diff", gate, 1, "m"),
        review.fix_prompt([], notes="note"),
        drafts.PROMPT.format(text="want button", allowed="core/**", errors=""),
        drafts.PROMPT.format(text="x", allowed="a",
                             errors="\n## Previous attempt failed validation\nbad\nFix it."),
        observer.TRIAGE_PROMPT.format(now_str="n", now_ms=1, since=1, since_str="s", log="l",
                                      kind="k", suspicions="s", log_digest="d", snapshot="snap"),
        observer.DEEP_CHECKLIST,
        launcher.PROMPT.format(messages="hi", status="ok", lang_line=launcher._owner_lang_line()),
        oc.prompt_arg("x" * 70_000, str(tmp_path)),
        oc.prompt_arg("short", str(tmp_path)),
    ]
    # findings + the red-gate branch of fix_prompt
    f = review.Finding(severity="high", file="core/b.py", line=1, issue="Y must be 3", fix="set 3")
    out.append(review.fix_prompt([f], gate=gates.GateResult(
        base="b", head="h", tests_ok=False, tests_cmd="pytest", tests_tail="fail")))
    assert task.kind is Kind.SCOUT
    return out


def test_scout_report_language_ru(tmp_path, monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "ru")
    from ahub.i18n import _reset

    _reset()
    from ahub import prompts
    from ahub.store import Store
    from tests.enginekit import make_project

    store = Store()
    project = make_project(tmp_path)
    tid = store.create_task(project="P", kind="scout", title="где утечка")
    prompt = prompts.scout_prompt(project, store.get_task(tid))
    assert "in Russian" in prompt and "## Суть" in prompt and "## Summary" not in prompt


def test_scout_report_language_en(tmp_path, monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset

    _reset()
    from ahub import prompts
    from ahub.store import Store
    from tests.enginekit import make_project

    store = Store()
    project = make_project(tmp_path)
    tid = store.create_task(project="P", kind="code", title="leak")
    prompt = prompts.scout_prompt(project, store.get_task(tid))
    assert "in English" in prompt and "## Summary" in prompt and "Суть" not in prompt


def test_no_cyrillic_en(tmp_path, monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "en")
    for s in _collect("en", tmp_path):
        assert not _CYR.search(s), s[:200]


def test_no_cyrillic_ru_except_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "ru")
    for s in _collect("ru", tmp_path):
        rest = _strip_allowed(s)
        assert not _CYR.search(rest), s[:300]


def test_drafts_kind_ru_en(tmp_path):
    """Draft parsing accepts kind in both languages, the codes are English."""
    from ahub import config, drafts

    import sys

    project = config.parse_project(
        {"schema_version": 2, "name": "P", "python": sys.executable}, str(tmp_path))
    for kind_ru, kind_en in [("разведка", "scout"), ("код", "code"),
                             ("рутина", "routine"), ("ревью", "review")]:
        spec = drafts.to_spec(project, {"kind": kind_ru, "title": "t", "spec": "s"})
        assert spec.kind.value == kind_en
        spec = drafts.to_spec(project, {"kind": kind_en, "title": "t", "spec": "s"})
        assert spec.kind.value == kind_en


def test_verdict_codes_english():
    """Review/observer verdicts are English codes."""
    from ahub import review

    assert set(review.VERDICTS) == {"approve", "changes", "dispute"}
