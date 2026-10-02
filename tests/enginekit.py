"""Shared engine test kit: a git project in a temp dir, a fake provider with scenarios taken in order."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ahub import config, providers, registry
from ahub.providers.base import RunSpec
from ahub.providers.fake import FakeProvider
from ahub.store import Store


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "core").mkdir()
    (root / "core" / "__init__.py").write_text("")
    (root / "core" / "a.py").write_text("X = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("from core.a import X\n\n\ndef test_x():\n    assert X == 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")


def make_project(tmp_path: Path, **extra) -> config.ProjectConfig:
    root = tmp_path / "proj"
    make_repo(root)
    data = {"schema_version": 2, "name": "P", "worktrees": str(tmp_path / "wt"), "python": sys.executable,
            "allowed_paths": ["core/**", "tests/**", "docs/**"],
            "timeouts": {"idle_s": 5, "retry_max": 2, "retry_pause_s": 0}}
    data.update(extra)
    return config.parse_project(data, root)


class ScriptedFake(FakeProvider):
    """Every run takes the next scenario from the queue; prompts and session ids land in calls."""

    def __init__(self, scenarios: list[dict]) -> None:
        super().__init__()
        self.scenarios = list(scenarios)
        self.calls: list[dict] = []

    def build_command(self, spec: RunSpec) -> list[str]:
        scenario = self.scenarios.pop(0) if self.scenarios else {"session": "ses_end", "steps": []}
        self.calls.append({"prompt": spec.prompt, "session_id": spec.session_id, "cwd": spec.cwd})
        inner = RunSpec(prompt=json.dumps(scenario), cwd=spec.cwd, model_id=spec.model_id,
                        session_id=spec.session_id)
        return super().build_command(inner)


def install_fake(store: Store, scenarios: list[dict]) -> ScriptedFake:
    fake = ScriptedFake(scenarios)
    providers.register("fake", fake)
    try:
        registry.add_model(store, "fake", "fake", "fake/model")
    except registry.RegistryError:
        pass
    return fake


def scout_ok(session: str = "ses_s", summary: str = "нашёл", report: str = "## Суть\nутечка в core/a.py:1\n",
             extra_steps: list | None = None) -> dict:
    steps = list(extra_steps or [])
    steps += [
        {"event": {"type": "tool_end", "tool": "read"}},
        {"event": {"type": "usage", "in": 100, "out": 10, "go": 0.01}},
        {"write": {"path": ".ahub/report.md", "text": report}},
        {"write": {"path": ".ahub/result.json", "text": json.dumps({"summary": summary, "status": "done"})}},
        {"event": {"type": "text", "text": "готово"}},
    ]
    return {"session": session, "steps": steps}
