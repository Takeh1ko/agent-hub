"""The screens a person reads: snapshot-style tests (width 100, plain text, no ANSI).

One snapshot per screen the CLI draws: the home screen, doctor, providers, models, setup --yes and
`--help`. The temp directory is replaced with {tmp}, so a snapshot only changes when the layout does.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from ahub import __version__, cli, doctor, paths
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
        f"ahub {__version__} · no project in this directory · service not running\n"
        "  the hub is not configured yet — run `ahub setup` to get started\n"
        "  • ahub setup\n"
        "  • ahub doctor\n"
        "  • ahub models\n")


def test_home_screen_builds_only_emitted_representation(capsys, monkeypatch, tmp_path):
    """Running ahub builds only text; ahub --json builds only data (no double evaluation)."""
    import ahub.home

    monkeypatch.chdir(tmp_path)
    data_calls = 0
    text_calls = 0

    orig_data = ahub.home.data
    orig_text = ahub.home.text

    def mock_data(*a, **kw):
        nonlocal data_calls
        data_calls += 1
        return orig_data(*a, **kw)

    def mock_text(*a, **kw):
        nonlocal text_calls
        text_calls += 1
        return orig_text(*a, **kw)

    monkeypatch.setattr(ahub.home, "data", mock_data)
    monkeypatch.setattr(ahub.home, "text", mock_text)

    run(capsys)
    assert text_calls == 1
    assert data_calls == 0

    run(capsys, "--json")
    assert text_calls == 1
    assert data_calls == 1


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
    assert lines[0] == f"ahub {__version__} · shop · service alive"
    assert lines[1] == "Active"
    assert lines[2].split() == ["task", "kind", "title", "state", "model", "idle"]  # the table head
    row = lines[3].split()
    assert row[1:7] == ["T1", "code", "Setup", "wizard:", "choose", "providers"]  # the title, word by word
    assert row[-3:] == ["spark:xhigh", "0", "min"]  # the model:effort and the idle age
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
    assert "    accept              accept (merge code)" in out  # the column is aligned
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
  ✓ python            Python 3.12.3
  ✓ git               git 2.43.0
The hub
  ✓ config            config ok ({tmp}/.config/ahub/config.toml)
  ✗ service           not running (no OS service)
    → ahub service install
  ✓ models            role defaults fit the login
  ✓ network           no system proxy
Providers
  ✓ opencode          found /usr/bin/opencode
  ✓ opencode answers  answers (1.2.3)
  ✓ opencode login    login: opencode-go (opencode-go available)
  ✗ agy               no login data
    → run agy in a terminal and sign in
  – codex             not found in PATH
Claude Code
  ✓ claude            found /usr/bin/claude
  ✗ claude skill      skill {tmp}/.claude/skills/ahub/SKILL.md, no Bash(ahub:*)
                      ({tmp}/.claude/settings.json)
    → ahub setup --claude
Optional
  – telegram          not configured (optional)
3 problems — the fix is under each check
"""


def test_doctor_sections_aligned_marks_and_the_fix_under_them(capsys, monkeypatch, home):
    checks = [doctor.Check(c.name, c.ok, c.detail.replace("{tmp}", str(home)), c.fix) for c in CHECKS]
    monkeypatch.setattr(doctor, "run_all", lambda *a, **k: checks)
    rc, out = run(capsys, "doctor")
    assert rc == 1
    assert snap(out, home) == DOCTOR
    assert "\033[" not in out  # plain into a pipe


def test_doctor_name_column_is_the_longest_name_and_the_detail_wraps_under_itself():
    """The name column is as wide as the longest name and nothing more; a long detail wraps under itself."""
    from ahub.commands import doctor as cmd_doctor

    checks = [doctor.Check("python", True, "Python 3.12.3", ""),
              doctor.Check("opencode_health", True, "answers (1.2.3)", ""),
              doctor.Check("claude_skill", False,
                           "skill /root/.claude/skills/ahub/SKILL.md, no Bash(ahub:*) (/root/.claude/settings.json)",
                           "ahub setup --claude")]
    lines = cmd_doctor._lines(checks, 60)
    at = {name: [ln for ln in lines if name in ln][0] for name in ("Python 3.12.3", "answers (1.2.3)")}
    assert at["Python 3.12.3"].index("Python 3.12.3") == 22  # "  ✓ " + "opencode answers" + 2
    assert at["answers (1.2.3)"].index("answers (1.2.3)") == 22
    wrapped = [ln for ln in lines if "settings.json" in ln][0]
    assert wrapped.strip() == "(/root/.claude/settings.json)" and wrapped.index("(") == 22  # under its first line


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
name      found  login  enabled
opencode  ✓      ✓      on
  · bunny — opencode/space-bunny-free · — · free · free
  · deepseek-flash — opencode-go/deepseek-v4.1-flash · high · Go plan · Go plan
  · mimo-flash — opencode-go/mimo-v2.6-flash · — · Go plan · Go plan
  · spark — opencode-go/muse-spark-1.3-contributor · xhigh · Go plan · Go plan
  · spark-free — opencode/muse-spark-1.3-contributor-free · xhigh · free · free
  · opencode-go: paid Spark available
agy       ✓      ✓      on
  · gemini — gemini-3.8-flash-high · high · subscription · —
  · Gemini via Antigravity, window quota (no money)
codex     ✗      –      on
  · codex — gpt-5.6-terra · — · subscription · —
  · codex-fast — gpt-5.6-luna · — · subscription · —
  · uses your ChatGPT plan
  → install codex: npm i -g @openai/codex, then codex login
"""


def test_providers_table_with_a_note_and_a_hint_under_each_row(capsys, monkeypatch):
    monkeypatch.setattr(doctor, "provider_states", lambda *a, **k: _provider_states())
    # quota windows and the live catalog come from the machine — the snapshot pins the layout
    monkeypatch.setattr("ahub.providers.agy.AgyProvider.quota", lambda self, force=False: [])
    monkeypatch.setattr("ahub.catalog.get_catalogs", lambda refresh=False: {})
    monkeypatch.setattr("ahub.catalog._quota_pct_for", lambda entry, store=None: None)
    monkeypatch.setattr("ahub.catalog._go_numbers", lambda: (None, None))
    monkeypatch.setattr("ahub.catalog.go_summary", lambda: "")
    monkeypatch.setattr("ahub.catalog.quota_summary", lambda provider: "")
    rc, out = run(capsys, "providers")
    assert rc == 0 and out == PROVIDERS
    assert run(capsys, "providers", "disable", "codex") == (0, "codex: off\nNext  ahub status\n")
    assert run(capsys, "providers", "enable", "codex") == (0, "codex: on\nNext  ahub models\n")


MODELS = """\
opencode
  alias                         model                         reasoning  plan     $ in / $ out  rol…
  bunny                         opencode/space-bunny-free     —          free     free          —
  deepseek-flash(project-deni…  opencode-go/deepseek-v4.1-f…  high       Go plan  Go plan       —
  mimo-flash                    opencode-go/mimo-v2.6-flash   —          Go plan  Go plan       —
  spark                         opencode-go/muse-spark-1.3…   xhigh      Go plan  Go plan       exe…
  spark-free                    opencode/muse-spark-1.3-con…  xhigh      free     free          —
agy
  alias   model                  reasoning  plan          $ in / $ out  roles
  gemini  gemini-3.8-flash-high  high       subscription  —             —
codex
  alias       model          reasoning  plan          $ in / $ out  roles
  codex       gpt-5.6-terra  —          subscription  —             —
  codex-fast  gpt-5.6-luna   —          subscription  —             —
"""


def test_models_menus_and_the_full_list(capsys, monkeypatch, tmp_path):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\n[models]\ndeny = ["deepseek"]\n')
    monkeypatch.chdir(tmp_path / "p")
    # the live catalog comes from the machine — the snapshot pins the layout with an empty one
    monkeypatch.setattr("ahub.catalog.get_catalogs", lambda refresh=False: {})
    monkeypatch.setattr("ahub.catalog._quota_pct_for", lambda entry, store=None: None)
    monkeypatch.setattr("ahub.catalog._go_numbers", lambda: (None, None))
    monkeypatch.setattr("ahub.catalog.go_summary", lambda: "")
    monkeypatch.setattr("ahub.catalog.quota_summary", lambda provider: "")
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
                                                      "the only model answers\n")


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
4. Service
    service files written: {tmp}/.config/systemd/user/ahub.service
    next  systemctl --user daemon-reload && systemctl --user enable --now ahub.service
5. Claude Code
    Claude skill: {tmp}/.claude/skills/ahub/SKILL.md
    CLAUDE.md: block added
    permission Bash(ahub:*) allowed ({tmp}/shop/.claude/settings.json)
    for other agents (Codex, Cursor): claude mcp add ahub -- ahub mcp
Summary
  Project      shop · {tmp}/shop
  Config       config {tmp}/d/config/config.toml
  Providers    opencode, agy
  Models       executor=spark-free, reviewer=spark-free, scout=spark-free, routine=spark-free,
               observer=spark-free, drafter=spark-free
  Service      —
  Claude Code  skill + CLAUDE.md + Bash(ahub:*)
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
4. Service
    service files written: {tmp}/.config/systemd/user/ahub.service
    next  systemctl --user daemon-reload && systemctl --user enable --now ahub.service
5. Claude Code
    skipped — later: ahub setup --claude
Summary
  Project      shop · {tmp}/shop
  Config       config {tmp}/d/config/config.toml
  Providers    opencode, agy
  Models       executor=spark-free, reviewer=spark-free, scout=spark-free, routine=spark-free,
               observer=spark-free, drafter=spark-free
  Service      —
  Claude Code  —
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
    assert len(data["checks"]) == len(CHECKS) and set(data["checks"][0]) == {"name", "ok", "detail", "fix",
                                                                             "buckets"}
    assert run(capsys, "--json", "models", "--role", "executor")[0] == 0
    data = json.loads(run(capsys, "--json", "models", "--role", "executor")[1])
    assert data["roles"]["executor"][0] == {"alias": "spark", "effort": "", "ref": "spark",
                                                    "default": True}
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
    block = lines[lines.index("Review findings") + 1:]
    assert block[0].split() == ["high", "core/b.py:1"]
    assert block[1].strip() == "Y должен быть 3"  # the whole issue, on its own line
    assert block[2].strip() == "fix: поставить 3"  # and what to do
    assert block[-1].startswith("Next  ahub accept T1")  # and the decision commands, as before
    from ahub import views

    assert len(out.encode()) <= views.L2_LIMIT


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
    assert cli.main(["task", "edit", t.label, "--rounds", "0"]) == 2  # 0 rounds is a mistake, like -1
    assert "rounds 0: allowed 1-5" in capsys.readouterr().err
    assert cli.main(["task", "edit", t.label, "--rounds", "-1"]) == 2
    assert cli.main(["task", "edit", t.label, "--review", "no-such-model"]) == 2
    assert "no model" in capsys.readouterr().err
    # a review task runs its panel: --review names it, --model renames it to one reviewer, --rounds is
    # nothing for it and both flags together are the same mistake as at creation
    r = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="look", review_input="main",
                                           review_models=["spark"]), project, collect=False)
    out = run(capsys, "task", "edit", r.label, "--review", "bunny")[1].strip()
    assert out == f"{r.label}: review panel bunny ×1"
    assert store.get_task(r.id).review == {"models": ["bunny"], "rounds": 1}
    out = run(capsys, "task", "edit", r.label, "--model", "spark")[1].strip()
    assert out == f"{r.label}: review panel spark ×1"  # the panel, not the executor (it is spark already)
    assert store.get_task(r.id).review == {"models": ["spark"], "rounds": 1}
    assert store.get_task(r.id).executor == "spark"
    assert cli.main(["task", "edit", r.label, "--review", "bunny", "--rounds", "2"]) == 2
    assert "--rounds does not apply to a review task" in capsys.readouterr().err
    assert cli.main(["task", "edit", r.label, "--review", "bunny", "--model", "spark"]) == 2
    assert "--review and --model together" in capsys.readouterr().err
    assert store.get_task(r.id).review == {"models": ["spark"], "rounds": 1}  # refused — untouched
    one = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="look too", review_input="main",
                                             model="spark"), project, collect=False)
    out = run(capsys, "task", "edit", one.label, "--model", "bunny")[1].strip()
    assert out == f"{one.label}: executor spark → bunny"  # no panel — the executor is the reviewer



def test_ahub_model_names_the_panel_of_a_review_task(capsys, monkeypatch, tmp_path):
    """What reviews a review task is its panel — `ahub model` renames that, not the unused executor."""
    from ahub import tasks
    from ahub.model import Kind
    from tests.enginekit import install_fake, make_project

    project = make_project(tmp_path)
    store = Store()
    install_fake(store, [])
    in_project(project, tmp_path, monkeypatch)
    panel = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="look", review_input="main",
                                               review_models=["spark", "bunny"]), project, collect=False)
    rc, out = run(capsys, "model", panel.label, "bunny")
    assert rc == 0 and out.splitlines()[0] == f"{panel.label}: review panel spark, bunny → bunny"
    row = store.get_task(panel.id)
    assert row.review == {"models": ["bunny"], "rounds": 1} and row.executor == "spark"  # fallback reviewer
    one = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="one", review_input="main",
                                             model="spark"), project, collect=False)
    out = run(capsys, "model", one.label, "bunny")[1].splitlines()[0]
    assert out == f"{one.label}: model spark → bunny" and store.get_task(one.id).executor == "bunny"


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
    # a review task: its panel is named the same way before the review, and by no way after it
    r = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="look", review_input="main",
                                           review_models=["spark"]), project, collect=False)
    assert cli.main(["model", r.label, "bunny"]) == 0  # before: the panel takes the model
    assert store.get_task(r.id).review == {"models": ["bunny"], "rounds": 1}
    for st in (State.PREPARING, State.WORKING, State.REVIEWING):
        transitions.move(store, r.id, st)
    transitions.move(store, r.id, State.NEEDS_DECISION)
    for argv, flag in ((["task", "edit", r.label, "--review", "spark"], "--review"),
                       (["task", "edit", r.label, "--model", "spark"], "--model"),
                       (["model", r.label, "spark"], "ahub model")):
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        assert "the review has already started" in err and f"({flag})" in err
    assert store.get_task(r.id).review == {"models": ["bunny"], "rounds": 1}  # untouched


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
    from tests.enginekit import make_project

    project = make_project(tmp_path / "proj")
    registry.set_provider_enabled("codex", False)
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
    assert out == ("  #   status  text                     task\n"
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

    # a broken project: one problem per line under the label, not one wrapped paragraph
    from ahub import config as cfgmod

    monkeypatch.setattr(cfgmod, "check_project", lambda cfg: ["python: not an executable file /nope/python",
                                                             "allowed_paths: core/** does not exist"])
    rc, out = run(capsys, "config")
    lines = out.splitlines()
    assert rc == 1  # problems — the exit code of a refusal
    at = next(i for i, ln in enumerate(lines) if ln.startswith("  Problems"))
    assert lines[at] == "  Problems  ! python: not an executable file /nope/python"
    assert lines[at + 1] == "            ! allowed_paths: core/** does not exist"  # aligned under it
    assert lines[-1].startswith("  Next  ahub setup ")  # and the Next line is still last


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


def test_open_findings_is_the_newest_round_and_skips_the_low_ones(tmp_path):
    """Only the round the panel decided on matters, and a low finding never blocks (review.dedup/panel)."""
    import json as _json

    from ahub import review, views  # noqa: F401
    from ahub.model import Kind
    from ahub.store import Task

    high = {"severity": "high", "file": "core/b.py", "line": 1, "issue": "Y должен быть 3", "fix": "поставить 3"}
    low = {"severity": "low", "file": "core/b.py", "line": 2, "issue": "имя переменной"}
    other = {"severity": "medium", "file": "core/c.py", "line": 7, "issue": "нет проверки", "fix": "добавить"}
    wt = tmp_path / "wt"
    (wt / ".ahub").mkdir(parents=True)
    for round_no, findings in ((1, [high]), (2, [high, low]), (3, [high, other, low])):
        (wt / ".ahub" / f"review_r{round_no}_fake.json").write_text(
            _json.dumps({"verdict": "changes", "summary": "", "findings": findings}), encoding="utf-8")
    task = Task(id=1, project="P", kind=Kind.CODE, title="x", worktree=str(wt), state="needs_decision")
    found, more = views.open_findings(task, limit=10)
    assert [(f.severity, f.file, f.line) for f in found] == [("high", "core/b.py", 1), ("medium", "core/c.py", 7)]
    assert more == 0  # the low finding is not counted as a lost one
    assert found[0].fix == "поставить 3"  # the fix is what a person needs
    # round 1 only — the findings of an earlier round are not open any more
    (wt / ".ahub" / "review_r3_fake.json").unlink()
    (wt / ".ahub" / "review_r2_fake.json").unlink()
    found, more = views.open_findings(task, limit=10)
    assert [(f.severity, f.file) for f in found] == [("high", "core/b.py")] and more == 0
    # the cap: `limit` findings and the rest counted
    (wt / ".ahub" / "review_r1_fake.json").unlink()
    (wt / ".ahub" / "review_r2_a.json").write_text(_json.dumps(
        {"verdict": "changes", "summary": "",
         "findings": [{"severity": "high", "file": f"core/{i}.py", "line": i, "issue": f"finding {i}"}
                      for i in (1, 2, 3)]}), encoding="utf-8")
    (wt / ".ahub" / "review_r2_b.json").write_text(_json.dumps(
        {"verdict": "changes", "summary": "",
         "findings": [{"severity": "high", "file": "core/9.py", "line": 9, "issue": "finding 9"}]}),
        encoding="utf-8")
    found, more = views.open_findings(task, limit=2)
    assert [f.file for f in found] == ["core/1.py", "core/2.py"] and more == 2
    assert review.dedup(found) == found  # the sort is the panel's own


def test_the_home_screen_caps_the_task_list(capsys, monkeypatch, tmp_path):
    """Six active tasks — five rows and a count of the rest (the same rule as the L1 overview)."""
    from ahub import home, transitions
    from ahub.model import Kind, State

    monkeypatch.setattr("ahub.home.now_ms", lambda: NOW + 30_000)  # a frozen clock (the idle age)
    write(paths.global_config_path(), "projects = []\n")
    write(tmp_path / "shop" / ".hub.toml", 'schema_version = 2\nname = "shop"\n')
    monkeypatch.chdir(tmp_path / "shop")
    store = Store()
    for i in range(home.MAX_TASKS + 1):
        tid = store.create_task(project="shop", kind=Kind.SCOUT, title=f"find the leak {i}", now=NOW)
        transitions.move(store, tid, State.PREPARING, now=NOW)
    lines = home.text(w=W).splitlines()
    task_rows = [ln for ln in lines if re.match(r"^\s*\S*\s+T\d+\s+scout", ln)]
    assert len(task_rows) == home.MAX_TASKS  # the sixth one is counted, not dropped silently
    assert "  +1 more — ahub status" in lines
    assert "T6" not in "\n".join(task_rows)


def test_home_offers_continue_for_a_task_that_failed(capsys, monkeypatch, tmp_path):
    """The next commands follow the state, like `ahub status T<id>`: an error is continued, not accepted."""
    from ahub import home, reasons, transitions
    from ahub.model import Kind, State

    monkeypatch.setattr("ahub.home.now_ms", lambda: NOW + 30_000)
    write(paths.global_config_path(), "projects = []\n")
    write(tmp_path / "shop" / ".hub.toml", 'schema_version = 2\nname = "shop"\n')
    monkeypatch.chdir(tmp_path / "shop")
    store = Store()
    failed = store.create_task(project="shop", kind=Kind.CODE, title="fix the leak", now=NOW)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, failed, st, now=NOW)
    transitions.move(store, failed, State.ERROR, reason=reasons.dump("quota", err="limit"), now=NOW)
    lines = home.text(w=W).splitlines()
    row = lines.index("Waiting for your decision") + 1
    assert lines[row].strip().startswith("T1  error")
    assert lines[row + 1].strip().startswith("Next  ahub continue T1")  # views.next_resume, not next_decide
    assert lines[-5] == "Next"  # and the suggested commands below, as before
    assert "accept T1" not in lines[row + 1]

    # with a decision pending, the decision commands come first
    done = store.create_task(project="shop", kind=Kind.SCOUT, title="find the leak", now=NOW)
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, done, st, now=NOW)
    lines = home.text(w=W).splitlines()
    assert "ahub accept T2" in lines[row + 2]  # the decision outranks the resume


def test_observer_run_prints_the_verdict_and_the_summary_once(capsys, monkeypatch):
    """One verdict word, the summary wrapped under it once — a long summary must not make a long line."""
    from ahub import observer

    summary = "The pipeline was red because the retry pause grew with every attempt. " * 4
    monkeypatch.setattr(observer, "cycle", lambda store, **kw: "alarm")
    monkeypatch.setattr(observer, "reports", lambda store, n: [
        {"ts": NOW, "kind": "quick", "verdict": "alarm", "summary": summary, "cost_go": 0.0}])
    rc, out = run(capsys, "observer", "run", "--no-model")
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "alarm"  # the verdict word, not the verdict plus the whole summary
    assert sum(1 for ln in lines if "retry pause" in ln) >= 1
    assert out.count("retry pause") >= 1 and summary not in out  # the summary is fitted, not pasted
    assert max(len(ln) for ln in lines) <= W  # nothing wider than the width (plain into a pipe)
    assert lines[-1].strip().startswith("Next  ahub alarms")


def test_the_findings_block_stays_inside_its_byte_budget(tmp_path):
    """Findings are shown whole, but the block cannot eat the L2 budget: the rest is counted."""
    import json as _json

    from ahub import views
    from ahub.model import Kind
    from ahub.store import Store, Task

    long_issue = "Очень длинная находка ревью. " * 20
    wt = tmp_path / "wt"
    (wt / ".ahub").mkdir(parents=True)
    (wt / ".ahub" / "review_r1_fake.json").write_text(_json.dumps(
        {"verdict": "changes", "summary": "",
         "findings": [{"severity": "high", "file": f"core/{i}.py", "line": i, "issue": long_issue,
                       "fix": long_issue} for i in range(1, 9)]}), encoding="utf-8")
    task = Task(id=1, project="P", kind=Kind.CODE, title="findings", executor="bunny", worktree=str(wt),
                state="needs_decision")
    text = views.task_text(Store(), task, w=W)
    lines = text.splitlines()
    block = lines[lines.index("Review findings") + 1:]
    shown = [ln for ln in block if ln.strip().startswith(("high", "medium", "low"))]
    assert 0 < len(shown) < 8  # what does not fit is not squeezed in
    # and it is counted: what is shown plus what is named is the whole list
    line = next(ln for ln in block if "more findings" in ln)
    assert line.strip() == f"+{8 - len(shown)} more findings — ahub log T1"
    assert len(text.encode()) <= views.L2_LIMIT
    assert len(text.encode()) < views.L2_LIMIT // 2  # the budget leaves room for the summary and the report


def test_a_broken_config_gives_one_error_line_not_a_traceback(capsys, monkeypatch, tmp_path):
    """`ahub` with no arguments reads the config like every other command: one line + exit 2."""
    from ahub.cliutil import CliError

    def broken(**kw) -> str:
        raise CliError("T1: no such task", hint="ahub top")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ahub.home.text", broken)
    write(paths.global_config_path(), "projects = []\n")
    rc = cli.main([])
    err = capsys.readouterr().err
    assert rc == 2 and err.splitlines() == ["error: T1: no such task", "  hint: ahub top"]


def test_the_waiting_list_is_capped_like_the_active_one(capsys, monkeypatch, tmp_path):
    """12 tasks waiting a decision must not push the whole screen down — the screen is a glance."""
    from ahub import home, transitions
    from ahub.model import Kind, State

    monkeypatch.setattr("ahub.home.now_ms", lambda: NOW + 30_000)
    write(paths.global_config_path(), "projects = []\n")
    write(tmp_path / "shop" / ".hub.toml", 'schema_version = 2\nname = "shop"\n')
    monkeypatch.chdir(tmp_path / "shop")
    store = Store()
    for i in range(home.MAX_TASKS + 4):
        task = store.create_task(project="shop", kind=Kind.SCOUT, title=f"scout {i}", now=NOW)
        for st in (State.PREPARING, State.WORKING, State.DONE):
            transitions.move(store, task, st, now=NOW)
    lines = home.text(w=W).splitlines()
    head = lines.index("Waiting for your decision") + 1
    rows = [ln for ln in lines[head:] if ln.strip().startswith("T")]
    assert len(rows) == home.MAX_TASKS  # the rest is counted, not listed
    assert lines[head + home.MAX_TASKS].strip() == "+4 more — ahub status"
    assert "T" + str(home.MAX_TASKS + 4) not in lines[head + home.MAX_TASKS + 1]
    assert lines[head + home.MAX_TASKS + 1].strip().startswith("Next  ahub accept T1")  # the oldest decision


def test_the_next_line_survives_a_full_screen(capsys, monkeypatch, tmp_path):
    """A full report, a full summary, questions and six findings: the screen may lose its tail,
    but never the way out — `ahub accept T1` is the point of the view."""
    import json as _json

    from ahub import reasons, views
    from ahub.model import Kind
    from ahub.store import Store, Task

    long = "Подробности о состоянии задачи и о том, что именно было сделано. " * 60
    findings = [{"severity": "high", "file": f"core/{i}.py", "line": i,
                 "issue": "Находка ревью номер %d: код делает не то, что от него ждут в этом месте. " % i * 6,
                 "fix": "Исправить так, чтобы поведение совпадало с контрактом вызова. " * 6} for i in range(6)]
    wt = tmp_path / "wt"
    (wt / ".ahub").mkdir(parents=True)
    (wt / ".ahub" / "review_r1_fake.json").write_text(
        _json.dumps({"verdict": "changes", "summary": "", "findings": findings}), encoding="utf-8")
    (wt / ".ahub" / "report.md").write_text("## Суть\n" + long + "\n\n## Подробно\n" + long, encoding="utf-8")
    (wt / ".ahub" / "result.json").write_text(_json.dumps(
        {"summary": long, "questions": [long[:120] + f" {i}" for i in range(3)], "notes": long},
        ensure_ascii=False), encoding="utf-8")
    task = Task(id=1, project="P", kind=Kind.CODE, title="большая задача", executor="bunny",
                worktree=str(wt), state="needs_decision", review={"models": ["fake"], "rounds": 2},
                state_reason=reasons.dump("quota", err="подробности ошибки провайдера. " * 20))
    text = views.task_text(Store(), task, w=W)
    assert len(text.encode()) <= views.L2_LIMIT  # L2 stays within the cap of contracts §5
    assert text.splitlines()[-1].strip().startswith("Next  ahub accept T1")  # the way out is the last line
    assert "rework" in text.splitlines()[-1] and "reject" in text.splitlines()[-1]

    # the same screen under a tighter cap: what is given up is the tail of the blocks above
    monkeypatch.setattr(views, "L2_LIMIT", 2400)
    cut = views.task_text(Store(), task, w=W)
    assert len(cut.encode()) <= 2400 and text.splitlines()[-1] in cut  # the Next line survived the clip
    assert "…" in cut  # what was given up is a block above it, not the way out


def test_one_problem_is_singular_in_english(capsys, monkeypatch, tmp_path):
    """English plurals: one problem is "1 problem", not "1 problems" (the catalogue carries both forms)."""
    from ahub import config as cfgmod

    monkeypatch.setenv("AHUB_LANG", "en")
    # ahub doctor: exactly one failed check
    checks = [doctor.Check("python", True, "Python 3.12.3", ""),
              doctor.Check("git", True, "git 2.43.0", ""),
              doctor.Check("service", False, "not running (no OS service)", "ahub service install")]
    monkeypatch.setattr(doctor, "run_all", lambda root=None, step=None: checks)
    rc, out = run(capsys, "doctor")
    assert rc == 1 and out.splitlines()[-1] == "1 problem — the fix is under the check"
    assert "1 problems" not in out

    # ahub config: one problem is one line, not "1 problems"
    root = tmp_path / "shop"
    write(root / ".hub.toml", 'schema_version = 2\nname = "shop"\n')
    monkeypatch.chdir(root)
    monkeypatch.setattr(cfgmod, "check_project", lambda cfg: ["python: not an executable file /nope/python"])
    rc, out = run(capsys, "config")
    assert rc == 1 and "! python: not an executable file" in out


def test_the_home_screen_has_a_json_shape(capsys, monkeypatch, tmp_path):
    """`ahub --json` with no subcommand: the raw fields of the screen, like every other command."""
    import json as _json

    from ahub import transitions
    from ahub.model import Kind, State

    monkeypatch.setattr("ahub.home.now_ms", lambda: NOW + 30_000)
    write(paths.global_config_path(), "projects = []\n")
    write(tmp_path / "shop" / ".hub.toml", 'schema_version = 2\nname = "shop"\n')
    monkeypatch.chdir(tmp_path / "shop")
    store = Store()
    task = store.get_task(store.create_task(project="shop", kind=Kind.CODE, title="починить", now=NOW))
    transitions.move(store, task.id, State.PREPARING, now=NOW)
    rc = cli.main(["--json"])
    data = _json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["project"] == "shop" and data["scope"] == {"all": False, "projects": ["shop"]}
    assert [t["label"] for t in data["tasks"]] == [task.label]  # the raw fields, not the rendered table
    assert data["tasks"][0]["state"] == "preparing" and "pulse" in data["tasks"][0]
    assert data["waiting"] == [] and data["configured"] is True and data["version"]


def test_the_home_screen_is_the_project_of_the_directory(capsys, monkeypatch, tmp_path):
    """Like every handle, the home screen is scoped: the tasks of another project are not its rows
    (architecture §9). `ahub --all` is the owner's view of the whole hub."""
    from ahub import home, transitions
    from ahub.model import Kind, State

    monkeypatch.setattr("ahub.home.now_ms", lambda: NOW + 30_000)
    write(paths.global_config_path(), "projects = []\n")
    for name in ("shop", "blog"):
        write(tmp_path / name / ".hub.toml", f'schema_version = 2\nname = "{name}"\n')
    monkeypatch.chdir(tmp_path / "shop")
    store = Store()
    mine = store.get_task(store.create_task(project="shop", kind=Kind.CODE, title="моя задача", now=NOW))
    transitions.move(store, mine.id, State.PREPARING, now=NOW)
    other = store.get_task(store.create_task(project="blog", kind=Kind.CODE, title="чужая задача", now=NOW))
    transitions.move(store, other.id, State.PREPARING, now=NOW)
    lines = home.text(w=W).splitlines()
    assert "моя задача" in "\n".join(lines) and "чужая задача" not in "\n".join(lines)
    every = home.text(w=W, all_projects=True).splitlines()
    assert "чужая задача" in "\n".join(every)  # --all — every project


def test_providers_shows_every_model_of_a_provider(capsys, monkeypatch):
    """The models are not a table cell: a long list is wrapped under the row, not clipped at the width."""
    from ahub import registry

    monkeypatch.setattr(doctor, "provider_states", lambda *a, **k: [
        doctor.ProviderState("opencode", True, True, detail="found /bin/opencode", note="", hint="")])
    aliases = [f"spark-{i:02d}" for i in range(24)]
    monkeypatch.setattr(registry, "models", lambda store: [
        registry.ModelEntry(a, "opencode", f"opencode-go/{a}", "", True, "") for a in aliases])
    monkeypatch.setattr("ahub.catalog.get_catalogs", lambda refresh=False: {})
    monkeypatch.setattr("ahub.catalog._quota_pct_for", lambda entry, store=None: None)
    monkeypatch.setattr("ahub.catalog._go_numbers", lambda: (None, None))
    monkeypatch.setattr("ahub.providers.agy.AgyProvider.quota", lambda self, force=False: [])
    rc, out = run(capsys, "providers")
    lines = out.splitlines()
    models = [ln.strip() for ln in lines if "spark-" in ln]
    assert rc == 0
    assert len(models) == len(aliases)  # one short line per model, none cut
    assert all(a in " ".join(models) for a in aliases)
    assert "…" not in "".join(models)
    assert max(len(ln) for ln in lines) <= W  # and wrapped to the width


def test_the_root_scope_flags_survive_a_subcommand(capsys, monkeypatch, tmp_path):
    """`ahub --all status` / `ahub --project B status T2`: argparse copies the subparser's namespace over
    the root one, so the root flags are merged back in — and a flag on the command itself still wins."""
    from ahub import transitions
    from ahub.model import Kind, State

    monkeypatch.setattr("ahub.views.now_ms", lambda: NOW)
    for name in ("A", "B"):
        write(tmp_path / name / ".hub.toml", f'schema_version = 2\nname = "{name}"\n')
    write(paths.global_config_path(), f'projects = ["{tmp_path / "A"}", "{tmp_path / "B"}"]\n')
    monkeypatch.chdir(tmp_path / "A")
    store = Store()
    mine = store.get_task(store.create_task(project="A", kind=Kind.CODE, title="задача A", now=NOW))
    transitions.move(store, mine.id, State.PREPARING, now=NOW)
    other = store.get_task(store.create_task(project="B", kind=Kind.CODE, title="задача B", now=NOW))
    transitions.move(store, other.id, State.PREPARING, now=NOW)
    own = home_lines(capsys, monkeypatch, "--all", "status")
    assert "задача A" in own and "задача B" in own  # --all before the subcommand — every project
    assert "задача B" not in home_lines(capsys, monkeypatch, "status")  # the plain command — this project

    # --project B before the subcommand names another project's task…
    rc = cli.main(["--project", "B", "status", other.label])
    assert rc == 0 and "задача B" in capsys.readouterr().out
    # …and refuses one of this project, as every handle does in its own scope
    rc = cli.main(["--project", "B", "status", mine.label])
    assert rc == 2 and "belongs to A" in capsys.readouterr().err  # refused, with the way out
    # a flag on the command itself wins over the root one — and --all still means every project
    assert cli.main(["--project", "A", "status", "--project", "B"]) == 0
    assert cli.main(["--project", "A", "status", "--project", "B", mine.label]) == 2
    assert "belongs to A" in capsys.readouterr().err  # B on the command won over A on the root
    assert cli.main(["--all", "status", "--project", "B"]) == 0
    out = capsys.readouterr().out
    assert "задача A" in out and "задача B" in out  # --all is the owner's view


def home_lines(capsys, monkeypatch, *argv: str) -> str:
    assert cli.main(list(argv)) == 0
    return capsys.readouterr().out


def test_the_next_block_wraps_to_the_given_width(capsys, monkeypatch, tmp_path):
    """The Next block is a block like the others: it wraps to the caller's width, not to COLUMNS."""
    import ahub.home
    from ahub import ui
    from ahub.i18n import t as real_t

    long_cmd = 'ahub task new --kind code --title "почини тест, который падает в CI" --project shop'
    monkeypatch.chdir(tmp_path)  # no hub configured — the screen stops right after the Next block
    monkeypatch.setattr(ahub.home, "_t", lambda k, **kw: long_cmd if k == "home.next_setup" else real_t(k, **kw))
    out = ahub.home.text(w=40)
    block = out.split(ui.BULLET, 1)[1].splitlines()  # the Next block: the bullet and its wrapped lines
    assert max(len(ui.BULLET + ln) for ln in block) <= 40, block
    assert len(block) > 3  # the long command really wraps — it is not simply shorter than the width
    assert long_cmd[:20] in out
