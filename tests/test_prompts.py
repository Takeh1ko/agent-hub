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
        launcher.PROMPT.format(project_line=launcher.project_line("P"), messages="hi", status="ok",
                               lang_line=launcher._owner_lang_line()),
        launcher.PROMPT.format(project_line=launcher.OWNER_LINE, messages="hi", status="ok",
                               lang_line=launcher._owner_lang_line()),
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
    import sys

    from ahub import config, drafts

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


def test_quality_bar_in_code_not_scout(tmp_path, monkeypatch):
    """Code/routine prompts carry the quality bar; scout prompts do not."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset

    _reset()
    from ahub import prompts
    from ahub.store import Store
    from tests.enginekit import make_project

    store = Store()
    project = make_project(tmp_path)
    scout_tid = store.create_task(project="P", kind="scout", title="find leak")
    code_tid = store.create_task(project="P", kind="code", title="fix")
    store.update_task(code_tid, limits={"paths": ["core/**"], "accept": ["tests/test_a.py::test_x"]})
    routine_tid = store.create_task(project="P", kind="routine", title="tidy")
    store.update_task(routine_tid, limits={"paths": ["docs/**"], "accept": []})
    scout = prompts.scout_prompt(project, store.get_task(scout_tid))
    code = prompts.code_prompt(project, store.get_task(code_tid))
    routine = prompts.code_prompt(project, store.get_task(routine_tid))
    for text in (code, routine):
        assert "## Quality bar" in text
        assert "Smallest diff" in text
        assert "dead code" in text
        assert "except Exception" in text
        assert "fails without it" in text
        assert "linter, if it has one" in text
        assert "ruff" not in text
        assert "No new dependencies" in text
    assert "## Quality bar" not in scout
    assert "## Quality bar" not in prompts.scout_delivery()


def test_scout_delivery_cites_sources(tmp_path, monkeypatch):
    """Scout reports cite file:line for every claim and note what was not checked."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset

    _reset()
    from ahub import prompts

    delivery = prompts.scout_delivery()
    assert "file:line" in delivery
    assert "what you did not check" in delivery


def test_reviewer_checks_quality_bar(tmp_path, monkeypatch):
    """The code reviewer prompt covers dead code, broad excepts and stub tests."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset

    _reset()
    from ahub import gates, review
    from ahub.store import Store
    from tests.enginekit import make_project

    store = Store()
    project = make_project(tmp_path)
    tid = store.create_task(project="P", kind="code", title="fix")
    task = store.get_task(tid)
    gate = gates.GateResult(base="b", head="h", diffstat="1 file")
    prompt = review.review_prompt(project, task, "diff", gate, 1, "m")
    assert "dead code" in prompt
    assert "except Exception" in prompt
    assert "stub test" in prompt.lower()
    assert "tests that do not test" in prompt


def test_assembly_order_and_headings(tmp_path, monkeypatch):
    """Layers assemble in exact order: global all, project all, local all, then role global, project, local,
    under their respective headings, followed by task spec and built-in hub layer LAST."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub import paths, prompts
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    store = Store()
    tid = store.create_task(project="P", kind="code", title="implement feature", spec="Spec for T1")
    store.update_task(tid, limits={"paths": ["core/**"], "accept": ["tests/test_a.py::test_x"]})
    task = store.get_task(tid)

    # 1. Global guidance
    write(paths.global_prompts_dir() / "all.md", "GLOBAL_ALL_RULE")
    write(paths.global_prompts_dir() / "code.md", "GLOBAL_CODE_RULE")

    # 2. Project guidance
    write(paths.project_prompts_dir(project.root) / "all.md", "PROJECT_ALL_RULE")
    write(paths.project_prompts_dir(project.root) / "code.md", "PROJECT_CODE_RULE")

    # 3. Local guidance
    write(paths.local_prompts_dir(project.name) / "all.md", "LOCAL_ALL_RULE")
    write(paths.local_prompts_dir(project.name) / "code.md", "LOCAL_CODE_RULE")

    prompt = prompts.code_prompt(project, task)

    # Check headings and content
    assert "## Global guidance\nGLOBAL_ALL_RULE" in prompt
    assert "## Project guidance\nPROJECT_ALL_RULE" in prompt
    assert "## Local guidance\nLOCAL_ALL_RULE" in prompt
    assert "## Global guidance\nGLOBAL_CODE_RULE" in prompt
    assert "## Project guidance\nPROJECT_CODE_RULE" in prompt
    assert "## Local guidance\nLOCAL_CODE_RULE" in prompt

    # Verify exact order
    i_g_all = prompt.index("GLOBAL_ALL_RULE")
    i_p_all = prompt.index("PROJECT_ALL_RULE")
    i_l_all = prompt.index("LOCAL_ALL_RULE")
    i_g_code = prompt.index("GLOBAL_CODE_RULE")
    i_p_code = prompt.index("PROJECT_CODE_RULE")
    i_l_code = prompt.index("LOCAL_CODE_RULE")
    i_spec = prompt.index("Spec for T1")
    i_builtin = prompt.index("## Allowed files")
    i_submit = prompt.index("## How to submit")

    assert i_g_all < i_p_all < i_l_all < i_g_code < i_p_code < i_l_code < i_spec < i_builtin < i_submit

    # Task limits recorded summary
    assert task.limits.get("prompts") == "built-in + global(all, code) + project(all, code) + local(all, code)"


def test_missing_files(tmp_path, monkeypatch):
    """Missing prompt files produce no guidance headings; only task header and built-in layer."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub import paths, prompts
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    store = Store()
    tid = store.create_task(project="P", kind="code", title="implement feature", spec="Spec for T1")
    store.update_task(tid, limits={"paths": ["core/**"], "accept": []})
    task = store.get_task(tid)

    # All files missing
    prompt = prompts.code_prompt(project, task)
    assert "## Global guidance" not in prompt
    assert "## Project guidance" not in prompt
    assert "## Local guidance" not in prompt
    assert prompt.startswith(f"# Task {task.label} (code): implement feature")
    assert "## How to submit" in prompt
    assert task.limits.get("prompts") == "built-in"

    # Only one file present (e.g. project code.md)
    write(paths.project_prompts_dir(project.root) / "code.md", "ONLY_CODE_RULE")
    prompt2 = prompts.code_prompt(project, task)
    assert "## Global guidance" not in prompt2
    assert "## Local guidance" not in prompt2
    assert prompt2.count("## Project guidance") == 1
    assert "ONLY_CODE_RULE" in prompt2
    assert task.limits.get("prompts") == "built-in + project(code)"


def test_legacy_rules(tmp_path, monkeypatch):
    """rules = '...' in .hub.toml is used as project all.md when .hub/prompts/all.md does not exist."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub import paths, prompts
    from tests.conftest import write
    from tests.enginekit import make_project

    # Create project with legacy rules
    legacy_file = tmp_path / "proj" / "rules.md"
    write(legacy_file, "LEGACY_RULES_CONTENT")
    project = make_project(tmp_path, rules="rules.md")

    # Case 1: .hub/prompts/all.md does not exist
    sections, summary, _ = prompts.assemble_guidance(project, "code")
    assert any("LEGACY_RULES_CONTENT" in s for s in sections)
    assert "project(all)" in summary
    assert prompts.rules_text(project) == "LEGACY_RULES_CONTENT"

    issues = prompts.check_prompts_for_project(project)
    hints = [i for i in issues if i.severity == "hint"]
    assert len(hints) == 1
    assert "rules" in hints[0].message and ".hub/prompts/all.md" in hints[0].fix

    # Case 2: .hub/prompts/all.md exists
    write(paths.project_prompts_dir(project.root) / "all.md", "MODERN_RULES_CONTENT")
    sections2, summary2, _ = prompts.assemble_guidance(project, "code")
    assert any("MODERN_RULES_CONTENT" in s for s in sections2)
    assert not any("LEGACY_RULES_CONTENT" in s for s in sections2)
    assert "project(all)" in summary2
    assert prompts.rules_text(project) == "MODERN_RULES_CONTENT"

    issues2 = prompts.check_prompts_for_project(project)
    hints2 = [i for i in issues2 if i.severity == "hint"]
    assert len(hints2) == 1
    assert "ignored" in hints2[0].message or "игнорируется" in hints2[0].message or "remove" in hints2[0].fix


def test_review_role_used_for_code_review_and_review_kind(tmp_path, monkeypatch):
    """Reviewers of code tasks and review kind tasks use the 'review' role guidance."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub import gates, paths, review
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    store = Store()

    # Set up prompt files
    write(paths.project_prompts_dir(project.root) / "all.md", "PROJECT_ALL_RULE")
    write(paths.project_prompts_dir(project.root) / "code.md", "PROJECT_CODE_RULE")
    write(paths.project_prompts_dir(project.root) / "review.md", "PROJECT_REVIEW_RULE")

    # 1. Code task review session
    code_tid = store.create_task(project="P", kind="code", title="fix issue")
    code_task = store.get_task(code_tid)
    gate = gates.GateResult(base="b", head="h", diffstat="1 file")
    code_prompt = review.review_prompt(project, code_task, "diff text", gate, 1, "model")

    assert "PROJECT_REVIEW_RULE" in code_prompt
    assert "PROJECT_CODE_RULE" not in code_prompt
    assert "PROJECT_ALL_RULE" in code_prompt
    assert code_task.limits.get("prompts") == "built-in + project(all, review)"
    # Built-in submission instructions are at the end
    assert code_prompt.rfind("verdict") > code_prompt.find("PROJECT_REVIEW_RULE")

    # 2. Review task kind
    rev_tid = store.create_task(project="P", kind="review", title="review branch", limits={"input": "feature"})
    rev_task = store.get_task(rev_tid)
    rev_prompt = review.review_prompt(project, rev_task, "diff text", gate, 1, "model")

    assert "PROJECT_REVIEW_RULE" in rev_prompt
    assert "PROJECT_CODE_RULE" not in rev_prompt
    assert rev_task.limits.get("prompts") == "built-in + project(all, review)"


def test_check_thresholds(tmp_path, monkeypatch):
    """ahub prompts check: warning >4 KB, refusal error >16 KB, unknown file names."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from pathlib import Path

    from ahub import cli, doctor, paths, prompts
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n')

    # 1. Clean
    write(paths.project_prompts_dir(project.root) / "code.md", "short code guidance")
    assert prompts.check_prompts_for_project(project) == []

    # 2. Size > 4 KB warning
    write(paths.project_prompts_dir(project.root) / "code.md", "x" * 4097)
    issues_warn = prompts.check_prompts_for_project(project)
    assert len(issues_warn) == 1
    assert issues_warn[0].severity == "warning"
    assert "4.0 KB" in issues_warn[0].message

    # 3. Size > 16 KB error (refusal level)
    write(paths.project_prompts_dir(project.root) / "code.md", "x" * 16385)
    issues_err = prompts.check_prompts_for_project(project)
    assert len(issues_err) == 1
    assert issues_err[0].severity == "error"
    assert "16.0 KB" in issues_err[0].message

    # 4. Unknown file in prompts directory
    write(paths.project_prompts_dir(project.root) / "code.md", "short")
    write(paths.project_prompts_dir(project.root) / "unknown.txt", "notes")
    issues_unknown = prompts.check_prompts_for_project(project)
    assert any(i.severity == "error" and "unknown.txt" in str(i.path) for i in issues_unknown)

    # Doctor check includes prompts
    doctor_res = doctor.check_prompts(Path(project.root))
    assert not doctor_res.ok
    assert "unknown.txt" in doctor_res.detail

    # CLI check returns 1 on error
    monkeypatch.chdir(project.root)
    assert cli.main(["prompts", "check"]) == 1

    # Remove unknown file -> returns 0
    (paths.project_prompts_dir(project.root) / "unknown.txt").unlink()
    assert cli.main(["prompts", "check"]) == 0


def test_edit_creates_template(tmp_path, monkeypatch, capsys):
    """ahub prompts edit <role> creates template if missing and invokes editor or prints path."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from pathlib import Path

    from ahub import cli, paths
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n')
    monkeypatch.chdir(project.root)

    # Unset EDITOR and VISUAL to test fallback output
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)

    # Default: project scope
    code_path = paths.project_prompts_dir(project.root) / "code.md"
    assert not code_path.exists()
    assert cli.main(["prompts", "edit", "code"]) == 0
    out = capsys.readouterr().out
    assert str(code_path) in out
    assert code_path.is_file()
    content = code_path.read_text(encoding="utf-8")
    assert "<!-- Guidance for code (project) -->" in content

    # Global scope
    global_path = paths.global_prompts_dir() / "scout.md"
    assert not global_path.exists()
    assert cli.main(["prompts", "edit", "scout", "--global"]) == 0
    assert global_path.is_file()
    assert "<!-- Guidance for scout (global) -->" in global_path.read_text(encoding="utf-8")

    # Local scope
    local_path = paths.local_prompts_dir(project.name) / "review.md"
    assert not local_path.exists()
    assert cli.main(["prompts", "edit", "review", "--local"]) == 0
    assert local_path.is_file()
    assert "<!-- Guidance for review (local) -->" in local_path.read_text(encoding="utf-8")

    # Test with EDITOR set
    monkeypatch.setenv("EDITOR", "true")
    assert cli.main(["prompts", "edit", "code"]) == 0


def test_card_line(monkeypatch):
    """The task card (views.task_text) shows the dim prompts line."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub import views
    from ahub.store import Store

    store = Store()
    tid = store.create_task(project="P", kind="code", title="do something")
    store.update_task(tid, limits={"prompts": "built-in + global(code) + project(all, code)"})
    task = store.get_task(tid)

    card = views.task_text(store, task)
    assert "prompts: built-in + global(code) + project(all, code)" in card


def test_ahub_home_isolation(tmp_path, monkeypatch):
    """An isolated instance never reads the real ~/.config prompts."""
    from ahub import paths, prompts
    from tests.conftest import write
    from tests.enginekit import make_project

    # Real user home config
    fake_user_home = tmp_path / "user_home"
    monkeypatch.setenv("HOME", str(fake_user_home))
    write(fake_user_home / ".config" / "ahub" / "prompts" / "all.md", "LEAK REAL HOME")

    # Isolated AHUB_HOME
    isolated_dir = tmp_path / "isolated"
    monkeypatch.setenv("AHUB_HOME", str(isolated_dir))

    # paths.global_prompts_dir() points to isolated_dir / config / prompts
    assert paths.global_prompts_dir() == isolated_dir / "config" / "prompts"
    assert paths.local_prompts_dir("P") == isolated_dir / "config" / "projects" / "P" / "prompts"

    project = make_project(tmp_path)

    # When isolated global prompts has content:
    write(isolated_dir / "config" / "prompts" / "all.md", "ISOLATED HOME PROMPT")
    sections, _, _ = prompts.assemble_guidance(project, "code")
    assert any("ISOLATED HOME PROMPT" in s for s in sections)
    assert not any("LEAK REAL HOME" in s for s in sections)

    # When isolated global prompts is removed:
    (isolated_dir / "config" / "prompts" / "all.md").unlink()
    sections_empty, _, _ = prompts.assemble_guidance(project, "code")
    assert not any("LEAK REAL HOME" in s for s in sections_empty)


def test_cli_prompts_and_show(tmp_path, monkeypatch, capsys):
    """ahub prompts and ahub prompts show CLI subcommands."""
    import json
    from pathlib import Path

    from ahub import cli, paths
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n')
    monkeypatch.chdir(project.root)

    # ahub prompts: shows table and hints for empty roles
    assert cli.main(["prompts"]) == 0
    out = capsys.readouterr().out
    assert "all" in out and "code" in out and "review" in out

    # Add a project prompt
    write(paths.project_prompts_dir(project.root) / "code.md", "CUSTOM_CODE_GUIDANCE")

    # ahub prompts show code
    assert cli.main(["prompts", "show", "code"]) == 0
    out_show = capsys.readouterr().out
    assert "CUSTOM_CODE_GUIDANCE" in out_show
    assert "## Project guidance" in out_show
    assert "## Allowed files" in out_show

    # ahub prompts show code --json
    assert cli.main(["--json", "prompts", "show", "code"]) == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["role"] == "code"
    assert "project(code)" in data["summary"]
    assert any(g["scope"] == "project" and "CUSTOM_CODE_GUIDANCE" in g["content"] for g in data["guidance"])
    assert "CUSTOM_CODE_GUIDANCE" in data["prompt"]

    # Unknown role
    import pytest
    with pytest.raises(SystemExit) as exc:
        cli.main(["prompts", "show", "invalid_role"])
    assert exc.value.code == 2

