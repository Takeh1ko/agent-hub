"""The screens a person reads: snapshot-style tests (width 100, plain text, no ANSI).

One snapshot per screen the CLI draws: the home screen, doctor, providers, models, setup --yes and
`--help`. The temp directory is replaced with {tmp}, so a snapshot only changes when the layout does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ahub import cli, doctor, paths
from ahub.store import Store
from tests.conftest import write
from tests.enginekit import make_repo

W = 100
NOW = 1_700_000_000_000  # a frozen clock for the screens that print an age or a tick


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    """The snapshots are the English reference strings; the layout is deterministic at width 100."""
    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", "en")
    monkeypatch.setenv("COLUMNS", str(W))
    _reset()
    yield
    _reset()


def snap(text: str, where) -> str:
    """The text with the temp directory (the fake HOME, the data dir, the worktree) as {tmp}."""
    return text.replace(str(where), "{tmp}")


@pytest.fixture
def home(monkeypatch):
    """A short fake HOME: the paths a screen prints must wrap the same way in every run, and the length
    of the pytest temp path (the test name is in it) would decide how a line wraps."""
    import shutil
    import tempfile
    from pathlib import Path

    base = Path(tempfile.mkdtemp(prefix="ahub-home-"))  # the same length on every run
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setenv("AHUB_HOME", str(base / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "s"))
    yield base
    shutil.rmtree(base, ignore_errors=True)


def in_project(project, tmp_path, monkeypatch) -> None:
    """The project file and the current directory: a command run from the repository of a task sees it
    (ahub/scope.py refuses a task of another project)."""
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n'
          f'worktrees = "{tmp_path / "wt"}"\nallowed_paths = ["core/**", "tests/**"]\n')
    write(paths.global_config_path(), f'projects = ["{project.root}"]\n')
    monkeypatch.chdir(str(project.root))


def run(capsys, *argv: str) -> tuple[int, str]:
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_home_screen_without_a_hub(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc, out = run(capsys)  # not argparse usage: a screen, and exit 0
    assert rc == 0
    assert snap(out, tmp_path) == (
        "ahub 3.0.0 · no project in this directory · service not running\n"
        "  the hub is not configured yet — Run `ahub setup` to get started\n"
        "  • ahub setup\n"
        "  • ahub doctor\n"
        "  • ahub models\n")


def test_home_screen_with_work_and_a_decision(capsys, monkeypatch, tmp_path):
    from ahub import reasons, transitions
    from ahub.model import Kind, State
    from ahub.service import HEARTBEAT_KEY

    write(paths.global_config_path(), "projects = []\n")
    write(tmp_path / "shop" / ".hub.toml", 'schema_version = 2\nname = "shop"\n')
    monkeypatch.chdir(tmp_path / "shop")
    # a frozen clock: the idle age of the snapshot must not depend on how busy the machine is
    monkeypatch.setattr("ahub.home.now_ms", lambda: NOW + 30_000)
    now = NOW
    store = Store()
    working = store.create_task(project="shop", kind=Kind.CODE, title="Setup wizard: choose providers",
                                executor="spark", now=now)
    transitions.move(store, working, State.PREPARING, now=now)
    transitions.move(store, working, State.WORKING, now=now)
    store.update_task(working, phase="writing", now=now)
    waiting = store.create_task(project="shop", kind=Kind.SCOUT, title="find the leak", now=now)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, waiting, st, now=now)
    transitions.move(store, waiting, State.DONE, reason=reasons.dump("review_exhausted", n=2, highs=1), now=now)
    store.meta_set(HEARTBEAT_KEY, str(NOW + 30_000))
    lines = run(capsys)[1].splitlines()
    assert lines[0] == "ahub 3.0.0 · shop · service alive"
    assert lines[1] == "Active"
    assert lines[2].split() == ["task", "kind", "title", "state", "model", "idle"]  # the table head
    row = lines[3].split()
    assert row[1:7] == ["T1", "code", "Setup", "wizard:", "choose", "providers"]  # the title, word by word
    assert row[-3:] == ["spark", "30", "s"]  # the model and the idle age
    assert lines[4] == "Waiting for your decision"
    assert " ".join(lines[5].split()) == "T2 done review rounds exhausted (2 findings, high: 1)"
    assert "ahub accept T2" in lines[6]
    assert lines[7] == "Next"
    assert [ln.strip(" •") for ln in lines[8:]] == ["ahub status", "ahub top", "ahub doctor",
                                                   'ahub task new --kind scout --title "…"']


def test_help_groups_the_subcommands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("usage: ahub ")
    assert "  Tasks:\n" in out and "  Watching:\n" in out and "  Setup:\n" in out
    assert "  Models and providers:\n" in out and "  Integrations:\n" in out
    assert "positional arguments" not in out  # the groups replaced the flat list
    assert "    accept        accept (merge code)" in out  # the column is aligned
    assert out.rstrip().endswith("Every command has its own help: ahub <command> --help")


CHECKS = [
    doctor.Check("python", True, "Python 3.12.3", ""),
    doctor.Check("git", True, "git 2.43.0", ""),
    doctor.Check("config", True, "config ok ({tmp}/.config/ahub/config.toml)", ""),
    doctor.Check("service", False, "not running (no OS service)", "ahub service install"),
    doctor.Check("opencode", True, "found /usr/bin/opencode", ""),
    doctor.Check("opencode_health", True, "answers (1.2.3)", ""),
    doctor.Check("opencode_auth", True, "login: opencode-go (opencode-go available)", ""),
    doctor.Check("agy", False, "no login data", "run agy in a terminal and sign in"),
    doctor.Check("codex", None, "not found in PATH", ""),
    doctor.Check("models", True, "role defaults fit the login", ""),
    doctor.Check("network", True, "no system proxy", ""),
    doctor.Check("claude", True, "found /usr/bin/claude", ""),
    doctor.Check("claude_skill", False,
                 "skill {tmp}/.claude/skills/ahub/SKILL.md, no Bash(ahub:*) ({tmp}/.claude/settings.json)",
                 "ahub setup --claude"),
    doctor.Check("telegram", None, "not configured (optional)", ""),
]

DOCTOR = """\
System
  ✓ python                                  Python 3.12.3
  ✓ git                                     git 2.43.0
The hub
  ✓ config                                  config ok ({tmp}/.config/ahub/config.toml)
  ✗ service                                 not running (no OS service)
    → ahub service install
  ✓ models                                  role defaults fit the login
  ✓ network                                 no system proxy
Providers
  ✓ opencode                                found /usr/bin/opencode
  ✓ opencode answers                        answers (1.2.3)
  ✓ opencode login                          login: opencode-go (opencode-go available)
  ✗ agy                                     no login data
    → run agy in a terminal and sign in
  – codex                                   not found in PATH
Claude Code
  ✓ claude                                  found /usr/bin/claude
  ✗ claude skill                            skill {tmp}/.claude/skills/ahub/SKILL.md, no Bash(ahub:*)
                      ({tmp}/.claude/settings.json)
    → ahub setup --claude
Optional
  – telegram                                not configured (optional)
3 problems — the fix is under each check
"""


def test_doctor_sections_aligned_marks_and_the_fix_under_them(capsys, monkeypatch, home):
    checks = [doctor.Check(c.name, c.ok, c.detail.replace("{tmp}", str(home)), c.fix) for c in CHECKS]
    monkeypatch.setattr(doctor, "run_all", lambda *a, **k: checks)
    rc, out = run(capsys, "doctor")
    assert rc == 1
    assert snap(out, home) == DOCTOR
    assert "\033[" not in out  # plain into a pipe


def test_doctor_all_green_says_so(capsys, monkeypatch):
    monkeypatch.setattr(doctor, "run_all", lambda *a, **k: [doctor.Check("python", True, "Python 3.12.3", ""),
                                                            doctor.Check("telegram", None, "off", "")])
    rc, out = run(capsys, "doctor")
    assert rc == 0 and out.splitlines()[-1] == "everything works"


def _provider_states() -> list[doctor.ProviderState]:
    return [
        doctor.ProviderState("opencode", True, True, detail="found /bin/opencode",
                             note="opencode-go: paid Spark available", hint=""),
        doctor.ProviderState("agy", True, True, detail="found /bin/agy",
                             note="Gemini via Antigravity, window quota (no money)", hint=""),
        doctor.ProviderState("codex", False, False, detail="not found", note="uses your ChatGPT plan",
                             hint="install codex: npm i -g @openai/codex, then codex login"),
    ]


PROVIDERS = """\
name      found  login  enabled  models
opencode  ✓      ✓      on       bunny, deepseek-flash, mimo-flash, spark, spark-free, spark-high…
  · opencode-go: paid Spark available
agy       ✓      ✓      on       gemini, gemini-low
  · Gemini via Antigravity, window quota (no money)
codex     ✗      –      on       codex, codex-fast
  · uses your ChatGPT plan
  → install codex: npm i -g @openai/codex, then codex login
"""


def test_providers_table_with_a_note_and_a_hint_under_each_row(capsys, monkeypatch):
    monkeypatch.setattr(doctor, "provider_states", lambda *a, **k: _provider_states())
    rc, out = run(capsys, "providers")
    assert rc == 0 and out == PROVIDERS
    assert run(capsys, "providers", "disable", "codex") == (0, "codex: off\nNext  ahub status\n")
    assert run(capsys, "providers", "enable", "codex") == (0, "codex: on\nNext  ahub models\n")


MODELS = """\
  role      default     other models
  executor  spark       mimo-flash, deepseek-flash(project-denied)
  reviewer  spark       mimo-flash, deepseek-flash(project-denied)
  scout     spark       deepseek-flash(project-denied)
  routine   spark       mimo-flash
  observer  spark-high  spark-medium
  drafter   spark-high  spark

  model           provider  model id
  bunny           opencode  opencode/space-bunny-free
  codex           codex     gpt-5.6-terra
  codex-fast      codex     gpt-5.6-luna
  deepseek-flash  opencode  opencode-go/deepseek-v4.1-flash [high] (project-denied)
  gemini          agy       gemini-3.8-flash-high
  gemini-low      agy       gemini-3.8-flash-low
  mimo-flash      opencode  opencode-go/mimo-v2.6-flash
  spark           opencode  opencode-go/muse-spark-1.3-contributor [xhigh]
  spark-free      opencode  opencode/muse-spark-1.3-contributor-free [xhigh]
  spark-high      opencode  opencode-go/muse-spark-1.3-contributor [high]
  spark-medium    opencode  opencode-go/muse-spark-1.3-contributor [medium]
"""


def test_models_menus_and_the_full_list(capsys, monkeypatch, tmp_path):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\n[models]\ndeny = ["deepseek"]\n')
    monkeypatch.chdir(tmp_path / "p")
    rc, out = run(capsys, "models", "--all")
    assert rc == 0 and out == MODELS


def test_models_check_probes_with_a_result_line(capsys, monkeypatch):
    def _probe(entry, timeout_s=60):
        ok = entry.alias != "spark"
        return ok, f"{entry.alias}: {'answered (OK)' if ok else 'no answer (silence for 60s)'}"

    monkeypatch.setattr(doctor, "probe_model", _probe)
    rc, out = run(capsys, "models", "check", "bunny", "spark")
    assert rc == 1
    assert out == ("     model  probe\n"
                   "  ✓  bunny  answered (OK)\n"
                   "  ✗  spark  no answer (silence for 60s)\n"
                   "1 of 2 answer — the rest are silent (ahub doctor)\n")
    assert run(capsys, "models", "check", "bunny") == (0, "     model  probe\n"
                                                      "  ✓  bunny  answered (OK)\n"
                                                      "1 of 1 models answer\n")


def _setup_env(monkeypatch):
    from ahub.commands import service as svccmd
    from ahub.tg import launcher

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(doctor, "auth_providers", lambda *a, **k: [])
    monkeypatch.setattr(doctor, "provider_states", lambda *a, **k: _provider_states())
    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    monkeypatch.setattr(svccmd, "enable_service", lambda *a: [])
    monkeypatch.setattr(svccmd, "wait_for_heartbeat", lambda timeout_s=15.0: 2)


SETUP = """\
1. Project
    shop: created a v2 template (check allowed_paths, python, tests)
    shop  {tmp}/shop
    added to {tmp}/d/config/config.toml
2. Providers
    ✓ opencode — logged in · opencode-go: paid Spark available
    ✓ agy — logged in · Gemini via Antigravity, window quota (no money)
    ✗ codex — not found · uses your ChatGPT plan
    → install codex: npm i -g @openai/codex, then codex login
    providers on: opencode, agy
    providers off: codex
3. Models
    roles executor, reviewer, scout, routine, observer, drafter now default to spark-free
4. Claude Code
    Claude skill: {tmp}/.claude/skills/ahub/SKILL.md
    CLAUDE.md: block added
    permission Bash(ahub:*) allowed ({tmp}/shop/.claude/settings.json)
    for other agents (Codex, Cursor): claude mcp add ahub -- ahub mcp
5. Service
    service files written: {tmp}/.config/systemd/user/ahub.service
    next  systemctl --user daemon-reload && systemctl --user enable --now ahub.service
Summary
  Project      shop · {tmp}/shop
  Config       config {tmp}/d/config/config.toml
  Providers    opencode, agy
  Models       executor=spark-free, reviewer=spark-free, scout=spark-free, routine=spark-free,
               observer=spark-free, drafter=spark-free
  Claude Code  skill + CLAUDE.md + Bash(ahub:*)
  Service      —
  Next  ahub task new --kind scout --title "…" · ahub doctor
"""


def test_setup_yes_reports_the_steps_and_a_summary(capsys, monkeypatch, home):
    _setup_env(monkeypatch)
    root = home / "shop"
    make_repo(root)
    rc, out = run(capsys, "setup", str(root), "--yes", "--claude")
    assert rc == 0 and snap(out, home) == SETUP


SETUP_YES_NO_CLAUDE = """\
1. Project
    shop: created a v2 template (check allowed_paths, python, tests)
    shop  {tmp}/shop
    added to {tmp}/d/config/config.toml
2. Providers
    ✓ opencode — logged in · opencode-go: paid Spark available
    ✓ agy — logged in · Gemini via Antigravity, window quota (no money)
    ✗ codex — not found · uses your ChatGPT plan
    → install codex: npm i -g @openai/codex, then codex login
    providers on: opencode, agy
    providers off: codex
3. Models
    roles executor, reviewer, scout, routine, observer, drafter now default to spark-free
4. Claude Code
    skipped — later: ahub setup --claude
5. Service
    service files written: {tmp}/.config/systemd/user/ahub.service
    next  systemctl --user daemon-reload && systemctl --user enable --now ahub.service
Summary
  Project      shop · {tmp}/shop
  Config       config {tmp}/d/config/config.toml
  Providers    opencode, agy
  Models       executor=spark-free, reviewer=spark-free, scout=spark-free, routine=spark-free,
               observer=spark-free, drafter=spark-free
  Claude Code  —
  Service      —
  Next  ahub task new --kind scout --title "…" · ahub doctor
"""


def test_setup_yes_without_claude_leaves_the_step_empty(capsys, monkeypatch, home):
    _setup_env(monkeypatch)
    root = home / "shop"
    make_repo(root)
    rc, out = run(capsys, "setup", str(root), "--yes")
    assert rc == 0 and snap(out, home) == SETUP_YES_NO_CLAUDE


def test_the_json_shape_did_not_change(capsys, monkeypatch):
    """--json keeps the raw fields of every screen (the text is for people only)."""
    monkeypatch.setattr(doctor, "run_all", lambda *a, **k: CHECKS)
    assert run(capsys, "--json", "doctor")[0] == 1
    data = json.loads(run(capsys, "--json", "doctor")[1])
    assert len(data["checks"]) == len(CHECKS) and set(data["checks"][0]) == {"name", "ok", "detail", "fix"}
    assert run(capsys, "--json", "models", "--role", "executor")[0] == 0
    data = json.loads(run(capsys, "--json", "models", "--role", "executor")[1])
    assert data["roles"]["executor"][0] == {"alias": "spark", "default": True}
    rc, out = run(capsys, "--json", "providers", "enable", "opencode")
    assert (rc, json.loads(out)) == (0, {"ok": True, "name": "opencode", "enabled": True})


def test_review_findings_are_listed_in_the_task_detail(capsys, monkeypatch, tmp_path):
    """A task that ended with open findings says what they are — the verdicts are in the copy."""
    from tests.enginekit import install_fake, make_project
    from tests.test_engine_code import HIGH, code_task, verdict, work
    from tests.test_engine_code import run as run_engine

    project = make_project(tmp_path)
    in_project(project, tmp_path, monkeypatch)
    store = Store()
    install_fake(store, [work(), verdict(v="changes", findings=HIGH)])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    assert run_engine(store, project, t.id).state.value == "needs_decision"
    rc, out = run(capsys, "status", t.label)
    assert rc == 0
    lines = out.splitlines()
    assert "Review findings" in lines
    block = lines[lines.index("Review findings") + 1:]
    assert " ".join(block[0].split()) == "high core/b.py:1 Y должен быть 3"
    assert block[1].startswith("Next  ahub accept T1")  # and the decision commands, as before


def test_no_findings_block_without_the_copy(tmp_path):
    from ahub import views
    from ahub.model import Kind
    from ahub.store import Task

    store = Store()
    task = store.create_task(project="P", kind=Kind.CODE, title="x", executor="spark")
    assert views.open_findings(store.get_task(task)) == ([], 0)  # no copy, no verdicts
    from ahub import transitions
    from ahub.model import State

    done = store.create_task(project="P", kind=Kind.SCOUT, title="y", executor="spark")
    store.update_task(done, worktree=str(tmp_path))
    for st in (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED):
        transitions.move(store, done, st)
    assert views.open_findings(store.get_task(done)) == ([], 0)  # nothing to review after accept
    assert Task(id=1, project="P", kind=Kind.CODE, title="x").worktree == ""


def test_task_edit_changes_the_review_panel_and_the_executor(capsys, monkeypatch, tmp_path):
    from ahub import cli, tasks
    from ahub.model import Kind
    from tests.enginekit import install_fake, make_project

    project = make_project(tmp_path)
    store = Store()
    install_fake(store, [])
    in_project(project, tmp_path, monkeypatch)
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="fix", spec="do it",
                                           paths=["core/**"], accept=["tests/test_a.py"], model="spark"),
                     project, collect=False)
    rc, out = run(capsys, "task", "edit", t.label, "--review", "spark,bunny", "--rounds", "2")
    assert rc == 0 and out.strip() == f"{t.label}: review panel spark+bunny ×2"
    assert store.get_task(t.id).review == {"models": ["spark", "bunny"], "rounds": 2}
    rc, out = run(capsys, "task", "edit", t.label, "--model", "bunny")
    assert out.strip() == f"{t.label}: executor spark → bunny"
    assert store.get_task(t.id).executor == "bunny"
    assert store.get_task(t.id).limits["fresh_session"] is True  # never resume another model's session
    # nothing to change / too many rounds / a model that does not exist
    assert run(capsys, "task", "edit", t.label, "--model", "bunny")[1].strip() == f"{t.label}: nothing to change"
    assert cli.main(["task", "edit", t.label, "--rounds", "9"]) == 2
    assert "allowed 1-5" in capsys.readouterr().err  # MAX_ROUNDS
    assert cli.main(["task", "edit", t.label, "--review", "no-such-model"]) == 2
    assert "no model" in capsys.readouterr().err


def test_the_review_panel_is_locked_once_the_review_started(capsys, monkeypatch, tmp_path):
    from ahub import cli, tasks, transitions
    from ahub.model import Kind, State
    from tests.enginekit import install_fake, make_project

    project = make_project(tmp_path)
    store = Store()
    install_fake(store, [])
    in_project(project, tmp_path, monkeypatch)
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="fix", spec="do it",
                                           paths=["core/**"], accept=["tests/test_a.py"], model="spark",
                                           review_models=["spark"], review_rounds=1),
                     project, collect=False)
    for st in (State.PREPARING, State.WORKING, State.CHECKING, State.REVIEWING):
        transitions.move(store, t.id, st)
    transitions.move(store, t.id, State.NEEDS_DECISION)
    assert cli.main(["task", "edit", t.label, "--review", "bunny"]) == 2
    err = capsys.readouterr().err
    assert "the review has already started" in err and f"ahub status {t.label}" in err
    assert store.get_task(t.id).review["models"] == ["spark"]  # untouched


def test_an_error_is_one_line_and_names_the_command(capsys, monkeypatch, tmp_path):
    from ahub import cli, registry
    from ahub.cliutil import CliError, command_hint
    from ahub.store import Store

    monkeypatch.chdir(tmp_path)
    assert cli.main(["status", "T99"]) == 2
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("error: no such task T99")
    assert err.splitlines()[1] == "  hint: ahub status"  # what to do
    # a provider that is off: the refusal itself already names the command
    from ahub import tasks
    from ahub.commands.setup import set_provider_enabled
    from tests.enginekit import make_project

    project = make_project(tmp_path / "proj")
    set_provider_enabled("codex", False)
    with pytest.raises(registry.RegistryError) as ei:
        registry.check(Store(), "codex", None)
    assert command_hint(str(ei.value)) == "ahub providers enable codex"
    with pytest.raises(tasks.TaskInvalid) as ti:
        tasks.create(Store(), tasks.TaskSpec(project="P", kind="scout", title="x", model="codex"),
                     project, collect=False)
    assert command_hint(" | ".join(ti.value.errors)) == "ahub providers enable codex"
    # a directory that belongs to no project — the way out is the wizard
    assert cli.main(["config"]) == 2
    err = capsys.readouterr().err
    assert "belongs to no project" in err and err.splitlines()[1] == "  hint: ahub setup"
    assert command_hint("no project 'x' (known: shop)") == ""  # nothing obvious — no second line
    assert CliError("boom", hint="ahub doctor").hint == "ahub doctor"


def test_draft_list_is_a_table(capsys, tmp_path):
    import json as _json

    from ahub import drafts
    from tests.enginekit import make_project

    project = make_project(tmp_path)
    store = Store()
    did = drafts.create(store, project, "кнопка повторной оплаты", run_model=False)
    drafts._update(store, did, status="ready",
                   task_json=_json.dumps({"kind": "code", "title": "кнопка", "spec": "do it",
                                         "paths": ["core/**"], "review_level": 2}))
    rc, out = run(capsys, "draft", "list")
    assert rc == 0
    assert out == ("  #   status  words                    task\n"
                   f"  #{did}  ready   кнопка повторной оплаты  —\n")
    # not ready — the reason instead of a task
    drafts._update(store, did, status="failed", errors="boom")
    assert "failed" in run(capsys, "draft", "list")[1]
    assert run(capsys, "draft", "start", str(did))[0] == 2


def test_observer_reports_is_a_table(capsys, monkeypatch):
    from ahub import observer

    monkeypatch.setattr(observer, "reports", lambda store, n: [
        {"ts": 1_700_000_000_000, "kind": "quick", "verdict": "ok", "summary": "всё тихо", "cost_go": 0.0},
        {"ts": 1_700_000_060_000, "kind": "deep", "verdict": "alarm", "summary": "T1 молчит", "cost_go": 0.012},
    ])
    rc, out = run(capsys, "observer", "reports")
    assert rc == 0
    assert out.splitlines()[0].split() == ["when", "check", "verdict", "summary"]
    assert "T1 молчит ($0.012)" in out
    assert len(run(capsys, "observer", "reports", "-n", "1")[1].splitlines()) == 3  # the head and one row


def test_service_status_shows_the_queue_and_the_next_command(capsys, monkeypatch):
    from ahub import reasons
    from ahub.commands import service as svccmd
    from ahub.service import HEARTBEAT_KEY

    monkeypatch.setattr(svccmd, "live_workers", lambda: {})
    monkeypatch.setattr(svccmd, "now_ms", lambda: NOW)  # a frozen clock: "tick 0s ago" stays true
    store = Store()
    tid = store.create_task(project="P", kind="scout", title="later")
    store.update_task(tid, state_reason=reasons.dump("wait_accept", task="T1", state="queued"))
    store.meta_set(HEARTBEAT_KEY, str(NOW))
    rc, out = run(capsys, "service", "status")
    assert rc == 0
    assert out == ("  Service  alive (tick 0s ago)\n"
                   "Queue\n"
                   "  T1  queued  waiting for T1 to be accepted (queued)\n"
                   "  Next  ahub status · ahub top\n")
    store.meta_del(HEARTBEAT_KEY)
    rc, out = run(capsys, "service", "status")
    assert out.splitlines()[0] == "  Service  not responding (no ticks yet)"
    assert out.splitlines()[-1] == "  Next  ahub service install · ahub service start"


def test_config_is_a_kv_block(capsys, monkeypatch, tmp_path):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "shop"\nmax_parallel = 4\n'
          'allowed_paths = ["core/**", "tests/**"]\n[models]\ndeny = ["deepseek"]\n')
    monkeypatch.chdir(tmp_path / "p")
    rc, out = run(capsys, "config")
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == f"shop  {tmp_path / 'p'}"
    assert lines[1].split() == ["Branch", "main", "→", "tasks", "ahub/<ID>", "in", "—"]
    assert lines[2] == "  Python         python3"
    assert lines[3] == "  Parallel       4 at a time · resources — · tests under —"
    assert lines[4] == "  Budget         $1.5 Go · $0 real"  # the default budget of a code task
    assert lines[5] == "  Denied models  deepseek"
    assert lines[6] == "  Allowed files  core/**, tests/**"
    assert lines[7].startswith("  Next  ahub setup ")


def test_service_status_lists_the_task_processes_of_this_hub(capsys, monkeypatch):
    from ahub import transitions
    from ahub.commands import service as svccmd
    from ahub.model import Kind, State

    store = Store()
    tid = store.create_task(project="P", kind=Kind.SCOUT, title="find the leak")
    transitions.move(store, tid, State.PREPARING)
    monkeypatch.setattr(svccmd, "live_workers", lambda: {tid: 4242, 999: 1})  # 999 — not our task
    rc, out = run(capsys, "service", "status")
    assert rc == 0
    assert out.splitlines()[1] == "Task processes"
    assert out.splitlines()[2].split() == ["task", "pid", "state"]
    assert out.splitlines()[3].split() == ["T1", "4242", "preparing"]
    assert "T999" not in out  # a live pid of a task this hub does not know is not ours to show


def _boom(*a, **k):
    """The OS service files cannot be written (an unwritable home)."""
    raise PermissionError(13, "read-only file system")


def test_the_service_step_never_stops_setup(capsys, monkeypatch, home):
    """An OS without a service, or a home that cannot be written to: a line of the report, not a crash."""
    from ahub.commands import service as svccmd

    _setup_env(monkeypatch)
    root = home / "shop"
    make_repo(root)
    # an OS the OS service does not exist for — the guard runs before --yes asks for the files
    monkeypatch.setattr(sys, "platform", "freebsd13")
    rc, out = run(capsys, "setup", str(root), "--yes", "--service")
    assert rc == 0
    assert "OS service needs Linux or macOS" in out
    assert "Service      —" in out  # the summary still has the row
    # the files cannot be written (an unwritable home) — the step reports it and setup finishes
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(svccmd, "install_service_files", _boom)
    rc, out = run(capsys, "setup", str(root), "--yes")
    assert rc == 0
    assert "enable failed: install: PermissionError: [Errno 13] read-only file system" in out
    assert "Service      —" in out
