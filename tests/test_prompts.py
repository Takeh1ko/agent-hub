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
        prompts.scout_prompt(project, task)[0],
        prompts.code_prompt(project, code_task)[0],
        prompts.reply_language_line(),
        prompts.report_heading(),
        prompts.arbiter_heading(),
        prompts.orchestrator_heading(),
        prompts.orchestrator_heading(rework=True),
        prompts.final_line(),
        review.review_prompt(project, task, "diff", gate, 1, "m")[0],
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
    prompt, _, _ = prompts.scout_prompt(project, store.get_task(tid))
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
    prompt, _, _ = prompts.scout_prompt(project, store.get_task(tid))
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


def test_quality_bar_in_template_not_builtin(tmp_path, monkeypatch):
    """Built-in hub layer carries only submission contract, no taste; Quality bar is in edit template."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset

    _reset()
    from ahub import prompts
    from ahub.commands.prompts import _edit_template
    from ahub.store import Store
    from tests.enginekit import make_project

    store = Store()
    project = make_project(tmp_path)
    code_tid = store.create_task(project="P", kind="code", title="fix")
    store.update_task(code_tid, limits={"paths": ["core/**"], "accept": ["tests/test_a.py::test_x"]})
    code, _, _ = prompts.code_prompt(project, store.get_task(code_tid))

    # Built-in prompt has no quality bar
    assert "## Quality bar" not in code
    assert "## How to submit" in code
    assert "## Allowed files" in code
    assert "## Acceptance" in code

    # Template for code has quality bar
    tmpl = _edit_template("code", "project")
    assert "## Quality bar" in tmpl
    assert "Smallest diff" in tmpl
    assert "dead code" in tmpl
    assert "except Exception" in tmpl
    assert "fails without it" in tmpl
    assert "No new dependencies" in tmpl


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
    prompt, _, _ = review.review_prompt(project, task, "diff", gate, 1, "m")
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

    prompt, summary, layers = prompts.code_prompt(project, task)

    lines = prompt.splitlines()
    assert "## Global guidance" in lines
    assert "## Project guidance" in lines
    assert "## Local guidance" in lines
    assert "## Allowed files" in lines
    assert "## Acceptance (must be green)" in lines
    assert "## How to submit (required)" in lines

    # One heading per scope
    assert prompt.count("## Global guidance") == 1
    assert prompt.count("## Project guidance") == 1
    assert prompt.count("## Local guidance") == 1

    # Check headings and content
    assert "## Global guidance\nGLOBAL_ALL_RULE\n\nGLOBAL_CODE_RULE" in prompt
    assert "## Project guidance\nPROJECT_ALL_RULE\n\nPROJECT_CODE_RULE" in prompt
    assert "## Local guidance\nLOCAL_ALL_RULE\n\nLOCAL_CODE_RULE" in prompt

    # Verify exact order
    i_g_all = prompt.index("GLOBAL_ALL_RULE")
    i_g_code = prompt.index("GLOBAL_CODE_RULE")
    i_p_all = prompt.index("PROJECT_ALL_RULE")
    i_p_code = prompt.index("PROJECT_CODE_RULE")
    i_l_all = prompt.index("LOCAL_ALL_RULE")
    i_l_code = prompt.index("LOCAL_CODE_RULE")
    i_spec = prompt.index("Spec for T1")
    i_builtin = prompt.index("## Allowed files")
    i_submit = prompt.index("## How to submit")

    assert i_g_all < i_g_code < i_p_all < i_p_code < i_l_all < i_l_code < i_spec < i_builtin < i_submit

    # Returned summary
    assert summary == "built-in + global(all, code) + project(all, code) + local(all, code)"


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
    prompt, summary, _ = prompts.code_prompt(project, task)
    assert "## Global guidance" not in prompt
    assert "## Project guidance" not in prompt
    assert "## Local guidance" not in prompt
    assert prompt.startswith(f"# Task {task.label} (code): implement feature")
    assert "## How to submit" in prompt
    assert summary == "built-in"

    # Only one file present (e.g. project code.md)
    write(paths.project_prompts_dir(project.root) / "code.md", "ONLY_CODE_RULE")
    prompt2, summary2, _ = prompts.code_prompt(project, task)
    assert "## Global guidance" not in prompt2
    assert "## Local guidance" not in prompt2
    assert prompt2.count("## Project guidance") == 1
    assert "ONLY_CODE_RULE" in prompt2
    assert summary2 == "built-in + project(code)"


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
    code_prompt, summary, _ = review.review_prompt(project, code_task, "diff text", gate, 1, "model")

    assert "PROJECT_REVIEW_RULE" in code_prompt
    assert "PROJECT_CODE_RULE" not in code_prompt
    assert "Stay in the copy (git worktree); never touch real data" in code_prompt
    assert "secrets (.env, keys, /etc)" in code_prompt
    assert summary == "built-in + project(all, review)"
    # Built-in submission instructions are at the end
    assert code_prompt.rfind("verdict") > code_prompt.find("PROJECT_REVIEW_RULE")

    # 2. Review task kind
    rev_tid = store.create_task(project="P", kind="review", title="review branch", limits={"input": "feature"})
    rev_task = store.get_task(rev_tid)
    rev_prompt, rev_summary, _ = review.review_prompt(project, rev_task, "diff text", gate, 1, "model")

    assert "PROJECT_REVIEW_RULE" in rev_prompt
    assert "PROJECT_CODE_RULE" not in rev_prompt
    assert "Stay in the copy (git worktree); never touch real data" in rev_prompt
    assert "secrets (.env, keys, /etc)" in rev_prompt
    assert rev_summary == "built-in + project(all, review)"


def test_check_thresholds(tmp_path, monkeypatch, capsys):
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
    assert "trim guidance under 4 KB" in issues_warn[0].fix

    # 3. Size > 16 KB error (refusal level)
    write(paths.project_prompts_dir(project.root) / "code.md", "x" * 16385)
    issues_err = prompts.check_prompts_for_project(project)
    assert len(issues_err) == 1
    assert issues_err[0].severity == "error"
    assert "16.0 KB" in issues_err[0].message
    assert "trim guidance under 16 KB" in issues_err[0].fix

    # 4. Unknown file in prompts directory -> warning (not refusal error)
    write(paths.project_prompts_dir(project.root) / "code.md", "short")
    write(paths.project_prompts_dir(project.root) / "unknown.txt", "notes")
    issues_unknown = prompts.check_prompts_for_project(project)
    assert any(i.severity == "warning" and "unknown.txt" in str(i.path) for i in issues_unknown)

    # Doctor check includes prompts: warning does not fail doctor
    doctor_res = doctor.check_prompts(Path(project.root))
    assert doctor_res.ok
    assert "unknown.txt" in doctor_res.detail

    # CLI check returns 0 for warnings
    monkeypatch.chdir(project.root)
    assert cli.main(["prompts", "check"]) == 0

    # 5. Size > 16 KB is error: CLI returns 1, doctor fails with ahub prompts check fix
    write(paths.project_prompts_dir(project.root) / "code.md", "x" * 16385)
    assert cli.main(["prompts", "check"]) == 1
    out_err = capsys.readouterr().out
    assert "trim guidance under 16 KB" in out_err
    doctor_err = doctor.check_prompts(Path(project.root))
    assert not doctor_err.ok
    assert doctor_err.fix == "ahub prompts check"

    # Clean up -> returns 0
    (paths.project_prompts_dir(project.root) / "unknown.txt").unlink()
    write(paths.project_prompts_dir(project.root) / "code.md", "short")
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
    assert "## Quality bar" in content
    assert "Smallest diff" in content

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
    rev_content = local_path.read_text(encoding="utf-8")
    assert "<!-- Guidance for review (local) -->" in rev_content
    assert "blocker: any SQL" in rev_content

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
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

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

    # ahub prompts show all with no guidance prints hint
    assert cli.main(["prompts", "show", "all"]) == 0
    out_empty_all = capsys.readouterr().out
    assert "no guidance for all" in out_empty_all

    # ahub prompts show scout/code/routine/review all succeed and contain submit section
    for r in ("scout", "code", "routine", "review"):
        assert cli.main(["prompts", "show", r]) == 0
        r_out = capsys.readouterr().out
        assert "## How to submit" in r_out

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

    # ahub prompts show all shows only guidance
    write(paths.project_prompts_dir(project.root) / "all.md", "CUSTOM_ALL_GUIDANCE")
    assert cli.main(["prompts", "show", "all"]) == 0
    out_all = capsys.readouterr().out
    assert "CUSTOM_ALL_GUIDANCE" in out_all
    assert "## Project guidance" in out_all
    assert "## Allowed files" not in out_all
    assert "## How to submit" not in out_all

    # Unknown role
    import pytest
    with pytest.raises(SystemExit) as exc:
        cli.main(["prompts", "show", "invalid_role"])
    assert exc.value.code == 2


def test_cli_project_flag_before_subcommand(tmp_path, monkeypatch, capsys):
    """ahub prompts --project P show code and ahub prompts --project P check work when --project precedes subcmd."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset

    _reset()
    from pathlib import Path

    from ahub import cli, paths
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n')
    write(paths.project_prompts_dir(project.root) / "code.md", "P_CODE_GUIDANCE")

    # Run from another directory
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    # 1. ahub prompts --project <path> show code
    assert cli.main(["prompts", "--project", str(project.root), "show", "code"]) == 0
    out_show = capsys.readouterr().out
    assert "P_CODE_GUIDANCE" in out_show

    # 2. ahub prompts --project <path> check
    assert cli.main(["prompts", "--project", str(project.root), "check"]) == 0
    out_check = capsys.readouterr().out
    assert "all prompt guidance files ok" in out_check

    # 3. Non-existent project returns 2 (CliError caught by cli.main)
    assert cli.main(["prompts", "--project", "non_existent_project_xyz", "check"]) == 2


def test_end_to_end_code_task_records_prompts(tmp_path, monkeypatch):
    """An end-to-end code task with the fake provider records prompts in limits and views."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    from ahub import paths, tasks, views
    from ahub.engine import Engine
    from ahub.model import Kind
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import install_fake, make_project

    store = Store()
    project = make_project(tmp_path)
    write(paths.project_prompts_dir(project.root) / "code.md", "CODE_STYLE_GUIDANCE")

    def work_step(session="ses_code"):
        return {
            "session": session,
            "steps": [
                {"write": {"path": "core/b.py", "text": "Y = 2\n"}},
                {"git_commit": "feat: feature"},
                {"result": {"summary": "implemented", "files": ["core/b.py"]}},
                {"event": {"type": "text", "text": "готово"}},
            ],
        }

    install_fake(store, [work_step()])
    t = tasks.create(
        store,
        tasks.TaskSpec(
            project="P",
            kind=Kind.CODE,
            title="implement feature",
            model="fake",
            paths=["core/**"],
            accept=["tests/test_a.py::test_x"],
            review_level=0,
        ),
        project,
        collect=False,
    )

    Engine(store, project, t.id, sleep=lambda s: None).run()

    task = store.get_task(t.id)
    assert task.limits.get("prompts") == "built-in + project(code)"
    card = views.task_text(store, task)
    assert "prompts: built-in + project(code)" in card


def test_end_to_end_review_task_records_prompts(tmp_path, monkeypatch):
    """An end-to-end review task with the fake provider records prompts in limits and views."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    import json
    import subprocess
    from pathlib import Path

    from ahub import paths, tasks, views
    from ahub.engine import Engine
    from ahub.model import Kind
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import git, install_fake, make_project

    def git_out(cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()

    store = Store()
    project = make_project(tmp_path)
    root = project.root

    # Prepare branch with commit to review
    git(root, "checkout", "-q", "-b", "feature")
    (Path(root) / "core" / "b.py").write_text("Y = 2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "фича")
    sha = git_out(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", project.work_branch)

    write(paths.project_prompts_dir(project.root) / "review.md", "REVIEW_GUIDANCE")

    verdict_body = {"verdict": "approve", "summary": "looks good", "findings": []}
    review_step = {
        "session": "ses_rev",
        "steps": [
            {"write": {"path": ".ahub/review_r1_fake.json", "text": json.dumps(verdict_body)}},
            {"event": {"type": "text", "text": "готово"}},
        ],
    }

    install_fake(store, [review_step])
    t = tasks.create(
        store,
        tasks.TaskSpec(
            project="P",
            kind=Kind.REVIEW,
            title="review feature branch",
            model="fake",
            review_input=sha,
        ),
        project,
        collect=False,
    )

    Engine(store, project, t.id, sleep=lambda s: None).run()

    task = store.get_task(t.id)
    assert task.limits.get("prompts") == "built-in + project(review)"
    card = views.task_text(store, task)
    assert "prompts: built-in + project(review)" in card


def test_end_to_end_rework_fresh_task_keeps_no_rework_notes(tmp_path, monkeypatch):
    """An end-to-end rework with fresh_session pops rework_notes and does not restore it."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    from ahub import tasks
    from ahub.engine import Engine
    from ahub.model import Kind
    from ahub.store import Store
    from tests.enginekit import install_fake, make_project

    store = Store()
    project = make_project(tmp_path)

    def work_step(session="ses_rework"):
        return {
            "session": session,
            "steps": [
                {"write": {"path": "core/b.py", "text": "Y = 2\n"}},
                {"git_commit": "feat: rework fix"},
                {"result": {"summary": "fixed", "files": ["core/b.py"]}},
                {"event": {"type": "text", "text": "готово"}},
            ],
        }

    install_fake(store, [work_step()])
    t = tasks.create(
        store,
        tasks.TaskSpec(
            project="P",
            kind=Kind.CODE,
            title="rework task",
            model="fake",
            paths=["core/**"],
            accept=["tests/test_a.py::test_x"],
            review_level=0,
        ),
        project,
        collect=False,
    )
    lim = dict(store.get_task(t.id).limits)
    lim["rework_notes"] = "PLEASE FIX BUG"
    lim["fresh_session"] = True
    store.update_task(t.id, limits=lim)

    Engine(store, project, t.id, sleep=lambda s: None).run()

    task = store.get_task(t.id)
    assert "rework_notes" not in task.limits
    assert "fresh_session" not in task.limits
    assert task.limits.get("prompts") == "built-in"


def test_non_utf8_guidance_handled(tmp_path, monkeypatch, capsys):
    """Non-UTF-8 guidance file is skipped in assembly, reported as error in check and doctor."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    from pathlib import Path

    from ahub import cli, doctor, paths, prompts
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n')
    store = Store()
    tid = store.create_task(project="P", kind="code", title="task")
    store.update_task(tid, limits={"paths": ["core/**"], "accept": []})
    task = store.get_task(tid)

    bad_file = paths.project_prompts_dir(project.root) / "code.md"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_bytes(b"\xcf\xf0\xe8\xe2\xe5\xf2")  # cp1251 "Привет", invalid UTF-8

    # 1. code_prompt does not crash; file is ignored and recorded as skipped
    prompt, summary, _ = prompts.code_prompt(project, task)
    assert "## Project guidance" not in prompt
    assert summary == "built-in + project(code: skipped, non-UTF-8)"

    # 2. rules_text does not crash
    assert prompts.rules_text(project) == prompts.DEFAULT_RULES

    # 3. check_prompts_for_project flags it as error
    issues = prompts.check_prompts_for_project(project)
    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) == 1
    assert "code.md" in str(errors[0].path)
    assert "UTF-8" in errors[0].message
    assert "save file as UTF-8" in errors[0].fix

    # 4. ahub prompts check returns 1 and prints fix
    monkeypatch.chdir(project.root)
    assert cli.main(["prompts", "check"]) == 1
    out = capsys.readouterr().out
    assert "save file as UTF-8" in out

    # 5. ahub doctor check_prompts fails
    doc_check = doctor.check_prompts(Path(project.root))
    assert not doc_check.ok
    assert "code.md" in doc_check.detail


def test_routine_task_uses_routine_guidance(tmp_path, monkeypatch):
    """A routine kind task loads routine.md guidance, not code.md, and records project(routine)."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub import paths, prompts
    from ahub.model import Kind
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    store = Store()
    write(paths.project_prompts_dir(project.root) / "routine.md", "ROUTINE_SPECIFIC_RULE")
    write(paths.project_prompts_dir(project.root) / "code.md", "CODE_SPECIFIC_RULE")

    tid = store.create_task(project="P", kind=Kind.ROUTINE, title="routine task")
    store.update_task(tid, limits={"paths": ["core/**"], "accept": []})
    task = store.get_task(tid)

    prompt, summary, _ = prompts.code_prompt(project, task)
    assert "ROUTINE_SPECIFIC_RULE" in prompt
    assert "CODE_SPECIFIC_RULE" not in prompt
    assert summary == "built-in + project(routine)"


def test_check_prompts_scans_local_prompts_dir(tmp_path, monkeypatch):
    """check_prompts_for_project scans local prompts dir for unknown files and size limits."""
    from ahub import paths, prompts
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    local_dir = paths.local_prompts_dir(project.name)

    write(local_dir / "unknown.txt", "some notes")
    write(local_dir / "all.md", "x" * 16385)

    issues = prompts.check_prompts_for_project(project)
    local_issues = [i for i in issues if str(local_dir) in str(i.path)]
    assert len(local_issues) == 2

    unknown_issue = next(i for i in local_issues if "unknown.txt" in str(i.path))
    assert unknown_issue.severity == "warning"

    size_issue = next(i for i in local_issues if "all.md" in str(i.path))
    assert size_issue.severity == "error"


def test_check_prompts_scans_global_prompts_dir(tmp_path):
    """check_prompts_for_project scans global prompts dir for unknown files and size limits."""
    from ahub import paths, prompts
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    global_dir = paths.global_prompts_dir()

    write(global_dir / "unknown_global.txt", "some notes")
    write(global_dir / "code.md", "x" * 16385)

    issues = prompts.check_prompts_for_project(project)
    global_issues = [i for i in issues if str(global_dir) in str(i.path)]
    assert len(global_issues) == 2

    unknown_issue = next(i for i in global_issues if "unknown_global.txt" in str(i.path))
    assert unknown_issue.severity == "warning"

    size_issue = next(i for i in global_issues if "code.md" in str(i.path))
    assert size_issue.severity == "error"

    # Also checks global dir when project is None
    issues_no_proj = prompts.check_prompts_for_project(None)
    global_no_proj = [i for i in issues_no_proj if str(global_dir) in str(i.path)]
    assert len(global_no_proj) == 2


def test_local_prompts_dir_hostile_name():
    """local_prompts_dir sanitises hostile project names preventing path traversal."""
    from ahub import paths

    # Hostile name with path traversal
    hostile = "../../etc/passwd"
    p = paths.local_prompts_dir(hostile)
    assert ".." not in p.parts
    # Resolves strictly inside config_dir() / "projects"
    assert p.is_relative_to(paths.config_dir() / "projects")

    # Name is just ".."
    p_dotdot = paths.local_prompts_dir("..")
    assert ".." not in p_dotdot.parts
    assert p_dotdot.is_relative_to(paths.config_dir() / "projects")

    # Accept lock path also uses same safe name
    lp = paths.accept_lock_path(hostile)
    assert ".." not in lp.name
    assert "/" not in lp.name


def test_check_prompts_reports_unreadable_file(tmp_path, monkeypatch):
    """check_prompts_for_project reports unreadable file with error severity."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    from pathlib import Path
    from unittest.mock import patch

    from ahub import paths, prompts
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    file_path = paths.project_prompts_dir(project.root) / "code.md"
    write(file_path, "code guidance")

    orig_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == file_path:
            raise PermissionError("Permission denied")
        return orig_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", failing_read_text):
        issues = prompts.check_prompts_for_project(project)
        errs = [i for i in issues if i.severity == "error" and str(file_path) in str(i.path)]
        assert len(errs) == 1
        assert "cannot read file" in errs[0].message
        assert "check file permissions" in errs[0].fix


def test_refuse_bytes_assemble_guidance(tmp_path, monkeypatch):
    """Guidance files > 16 KB are refused and treated as missing in assemble_guidance."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    from ahub import paths, prompts
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    store = Store()
    write(paths.project_prompts_dir(project.root) / "code.md", "x" * 16385)

    tid = store.create_task(project="P", kind="code", title="task")
    store.update_task(tid, limits={"paths": ["core/**"], "accept": []})
    task = store.get_task(tid)

    prompt, summary, _ = prompts.code_prompt(project, task)
    assert "## Project guidance" not in prompt
    assert summary == "built-in + project(code: skipped, too big)"


def test_skipped_unreadable_file_leaves_note_in_summary_and_log(tmp_path, monkeypatch, caplog):
    """An unreadable guidance file is skipped and leaves a note in log and card summary."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    import logging
    from pathlib import Path

    from ahub import paths, prompts, views
    from ahub.store import Store
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    store = Store()
    tid = store.create_task(project="P", kind="code", title="task")
    store.update_task(tid, limits={"paths": ["core/**"], "accept": []})
    task = store.get_task(tid)

    code_path = paths.global_prompts_dir() / "code.md"
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_path.write_text("SOME_CODE_RULE", encoding="utf-8")

    # Simulate unreadable file by monkeypatching read_text on Path
    orig_read_text = Path.read_text

    def mock_read_text(self, *args, **kwargs):
        if self == code_path:
            raise OSError("Permission denied")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mock_read_text)

    with caplog.at_level(logging.WARNING):
        prompt, summary, layers = prompts.code_prompt(project, task)

    # 1. Guidance not included
    assert "SOME_CODE_RULE" not in prompt
    # 2. Log contains warning
    assert any("skipped" in r.message and "unreadable" in r.message for r in caplog.records)
    # 3. Card summary records skipped note
    assert summary == "built-in + global(code: skipped, unreadable)"

    # 4. Russian card displays translated words
    store.update_task(tid, limits={"prompts": summary})
    task = store.get_task(tid)
    monkeypatch.setenv("AHUB_LANG", "ru")
    _reset()

    card_ru = views.task_text(store, task)
    assert "промпты: встроенный + глобальный(code: пропущен, не удаётся прочитать)" in card_ru


def test_code_task_reviewer_summary_recorded(tmp_path, monkeypatch):
    """When a code task runs a review round, the reviewer prompt summary is recorded on the task."""
    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset
    _reset()

    from ahub import engine, gates, paths
    from ahub.providers.base import Outcome, RunResult
    from ahub.store import Store
    from tests.conftest import write
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    write(paths.project_prompts_dir(project.root) / "review.md", "REVIEW_RULE")

    store = Store()
    tid = store.create_task(project="P", kind="code", title="feature")
    store.update_task(tid, limits={"paths": ["src/**"], "accept": []})
    task = store.get_task(tid)

    eng = engine.Engine(store, project, tid)
    g = gates.GateResult(base="base", head="head", diffstat="1 file")

    # Mock session to return empty run result without spawning real process
    def mock_session(role, model, prompt, **kwargs):
        return RunResult(outcome=Outcome.OK, session_id="s1")

    monkeypatch.setattr(eng, "session", mock_session)
    monkeypatch.setattr(eng, "_revert_reviewer", lambda t: None)
    eng._review_round(task, g, ["mock_model"], 1, 1, material="diff")

    updated = store.get_task(tid)
    assert "prompts" in updated.limits
    assert updated.limits["prompts"] == "built-in + project(review)"


