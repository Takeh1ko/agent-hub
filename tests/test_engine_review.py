"""Engine on the review kind: the input (branch, commit, range, files), the panel, the result.

The findings are the result: the hub writes report.md and result.json from the verdicts, the task goes to
"done" (nothing is merged) and `ahub accept` closes it like a scout's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahub import accept, review, tasks
from ahub.engine import Engine, review_material
from ahub.i18n import set_lang
from ahub.model import Kind, State
from ahub.store import Store
from tests.enginekit import git, install_fake, make_project


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def review_task(store, project, inp, **kw):
    return tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="посмотреть свежий код",
                                              model="fake", review_input=inp, **kw), project, collect=False)


def run(store, project, tid):
    return Engine(store, project, tid, sleep=lambda s: None).run()


def verdict(v="approve", findings=None, round_no=1, model="fake", session="ses_rev"):
    body = {"verdict": v, "summary": "посмотрел", "findings": findings or []}
    return {"session": session, "steps": [
        {"write": {"path": f".ahub/review_r{round_no}_{model}.json", "text": json.dumps(body, ensure_ascii=False)}},
        {"event": {"type": "text", "text": "готово"}}]}


def silent(session="ses_rev"):
    """A reviewer that answers in text and writes no verdict."""
    return {"session": session, "steps": [{"event": {"type": "text", "text": "разбор текстом без файла"}}]}


HIGH = [{"severity": "high", "file": "core/b.py", "line": 12, "issue": "в ветке Y = 2, должно быть 3",
         "fix": "поставить 3"}]
MEDIUM = [{"severity": "medium", "file": "core/a.py", "line": 1, "issue": "нет проверки на None",
           "fix": "добавить guard"}]
LOW = [{"severity": "low", "file": "core/a.py", "line": 2, "issue": "имя X слишком короткое"}]


def branch_with_work(project, name="feature") -> str:
    """A branch with one commit in the project root; returns the sha of its tip."""
    root = project.root
    git(root, "checkout", "-q", "-b", name)
    (Path(root) / "core" / "b.py").write_text("Y = 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "фича")
    sha = git_out(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", project.work_branch)
    return sha


def git_out(cwd, *args) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


# --- the input ---


def test_branch_input(store, project):
    branch_with_work(project)
    fake = install_fake(store, [verdict()])
    t = review_task(store, project, "feature")
    assert run(store, project, t.id).state is State.DONE
    prompt = fake.calls[0]["prompt"]
    assert "diff --git" in prompt and "Y = 2" in prompt
    assert "feature" in prompt and "## Gates" not in prompt  # a review task has no gates


def test_sha_input(store, project):
    sha = branch_with_work(project)
    fake = install_fake(store, [verdict()])
    t = review_task(store, project, sha)
    assert run(store, project, t.id).state is State.DONE
    assert "diff --git" in fake.calls[0]["prompt"] and "Y = 2" in fake.calls[0]["prompt"]


def test_range_input(store, project):
    first = git_out(project.root, "rev-parse", "HEAD")
    sha = branch_with_work(project)
    fake = install_fake(store, [verdict()])
    t = review_task(store, project, f"{first}..{sha}")
    assert run(store, project, t.id).state is State.DONE
    assert "Y = 2" in fake.calls[0]["prompt"]


def test_files_input(store, project):
    fake = install_fake(store, [verdict()])
    t = review_task(store, project, "core/a.py")
    assert run(store, project, t.id).state is State.DONE
    prompt = fake.calls[0]["prompt"]
    assert "diff --git" not in prompt and "### core/a.py" in prompt and "X = 1" in prompt


def test_input_the_copy_cannot_resolve(store, project):
    install_fake(store, [verdict()])
    t = review_task(store, project, "no-such-branch")
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "вход ревью непригоден" in res.reason


def test_input_never_leaves_the_copy(store, project):
    """`--input ../../etc/hosts` is not a review — nothing outside the worktree is read."""
    install_fake(store, [verdict()])
    t = review_task(store, project, "../../etc/hosts")
    assert run(store, project, t.id).state is State.NEEDS_DECISION


def test_material_of_the_review_input(project, tmp_path):
    """review_material is what the panel reads; the copy is the one of the task."""
    from ahub import workspace

    sha = branch_with_work(project)
    ws = workspace.ensure(project, 999)
    assert "Y = 2" in review_material(project, ws.path, "feature")
    assert "Y = 2" in review_material(project, ws.path, sha)
    assert "X = 1" in review_material(project, ws.path, "core/a.py")


# --- the result ---


def test_findings_become_the_report(store, project):
    branch_with_work(project)
    set_lang("en")  # the summary line is user text — check the English catalog here
    install_fake(store, [verdict(v="changes", findings=[*HIGH, *MEDIUM, *LOW])])
    t = review_task(store, project, "feature")
    res = run(store, project, t.id)
    assert res.state is State.DONE
    done = store.events(task_id=t.id, needs_reaction=True)[-1].payload
    assert done["summary"] == "3 findings: 1 high, 1 medium, 1 low" and done["findings"] == 3
    assert done["report_bytes"] > 0
    row = store.get_task(t.id)
    assert row.state_reason == '{"code":"review_findings","n":3}'
    base = Path(row.worktree) / ".ahub"
    report = (base / "report.md").read_text(encoding="utf-8")
    assert report.startswith("## Summary\n3 findings: 1 high, 1 medium, 1 low")
    assert report.index("### high") < report.index("### medium") < report.index("### low")
    assert "- `core/b.py:12` — в ветке Y = 2, должно быть 3\n  fix: поставить 3" in report
    result = json.loads((base / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "done" and result["summary"] == done["summary"]


def test_summary_line_in_both_languages():
    from ahub.engine import _findings_summary

    findings = [review.Finding("high", "core/a.py", 1, "ошибка"), review.Finding("low", "core/a.py", 2, "вкусно")]
    set_lang("en")
    assert _findings_summary(findings) == "2 findings: 1 high, 1 low"
    assert _findings_summary([]) == "no findings"
    set_lang("ru")
    assert _findings_summary([]) == "замечаний нет"


def test_no_findings(store, project):
    set_lang("en")
    install_fake(store, [verdict()])
    t = review_task(store, project, "core/a.py")
    res = run(store, project, t.id)
    assert res.state is State.DONE and store.get_task(t.id).state_reason == '{"code":"review_agree"}'
    assert store.events(task_id=t.id, needs_reaction=True)[-1].payload["summary"] == "no findings"
    report = (Path(store.get_task(t.id).worktree) / ".ahub" / "report.md").read_text(encoding="utf-8")
    assert report == "## Summary\nno findings\n"


def test_low_findings_only_agree(store, project):
    """A reviewer with low findings only has effectively approved — the summary counts them all anyway."""
    install_fake(store, [verdict(v="changes", findings=LOW)])
    t = review_task(store, project, "core/a.py")
    assert run(store, project, t.id).state is State.DONE
    assert store.events(task_id=t.id, needs_reaction=True)[-1].payload["findings"] == 1


def test_reviewer_may_not_change_files(store, project):
    naughty = verdict()
    naughty["steps"].insert(0, {"write": {"path": "core/a.py", "text": "испорчено\n"}})
    install_fake(store, [naughty])
    t = review_task(store, project, "core/a.py")
    assert run(store, project, t.id).state is State.DONE
    assert (Path(store.get_task(t.id).worktree) / "core" / "a.py").read_text() == "X = 1\n"


def test_missing_verdict_retries_once_then_needs_decision(store, project):
    fake = install_fake(store, [silent(), silent(session="ses_rev2")])
    t = review_task(store, project, "core/a.py")
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "не сдали вердикт" in res.reason
    assert len(fake.calls) == 2 and fake.calls[0]["session_id"] is None and fake.calls[1]["session_id"] == "ses_rev"
    assert "You did not write" in fake.calls[1]["prompt"]


def test_missing_verdict_repaired_in_the_same_session(store, project):
    fake = install_fake(store, [silent(), verdict(session="ses_other")])
    t = review_task(store, project, "core/a.py")
    assert run(store, project, t.id).state is State.DONE
    assert len(fake.calls) == 2 and fake.calls[1]["session_id"] == "ses_rev"


def test_review_state_path(store, project):
    install_fake(store, [verdict()])
    t = review_task(store, project, "core/a.py")
    assert run(store, project, t.id).state is State.DONE
    states = [e.payload.get("to") for e in store.events(task_id=t.id) if e.kind == "state"]
    assert states == ["preparing", "working", "reviewing", "done"]


def test_accept_closes_the_task_without_a_merge(store, project):
    install_fake(store, [verdict(v="changes", findings=HIGH)])
    t = review_task(store, project, "core/a.py")
    assert run(store, project, t.id).state is State.DONE
    before = git_out(project.root, "rev-parse", "HEAD")
    accept.accept(store, project, t.id)
    row = store.get_task(t.id)
    assert row.state is State.ACCEPTED and git_out(project.root, "rev-parse", "HEAD") == before
    assert not Path(row.worktree).exists()
