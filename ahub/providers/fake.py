"""Fake provider: full contract over fake_agent (a real subprocess, no network).

The prompt is a fake_agent JSON scenario (or a path to a scenario file). Usage comes from stream
usage events, the session log and state — from the recorded log. Used in runner, engine, and gate tests.

Two env switches make it usable outside the tests too (both are read by the installed CLI):
- AHUB_FAKE_PROVIDER=1 — the model "fake" is added to the registry and made the default of every role
  (registry.seed), so `ahub task new` can run a task on this provider without a network or a real model.
- AHUB_FAKE_QUEUE=<dir> — the scenarios, one *.json per turn, taken in order (an empty queue answers
  an empty result).
Neither has a CLI flag: both exist for tests and for tools/smoke.sh (the CI smoke test).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ahub.providers.base import Act, Activity, Cap, Health, ModelInfo, Provider, RunSpec, SessionState, Usage

TRANSIENT_MARKERS = ("unexpected server error", "cannot connect", "econnrefused", "etimedout", "status 5", "429")

ENV_FLAG = "AHUB_FAKE_PROVIDER"  # =1 — this provider becomes selectable (the model alias is "fake")
ENV_QUEUE = "AHUB_FAKE_QUEUE"  # =<dir> — scenarios for the turns, *.json in order

ALIAS = "fake"  # the model alias the registry gets under ENV_FLAG
MODEL_ID = "fake/model"


def selectable_from_env() -> bool:
    """AHUB_FAKE_PROVIDER=1 — the fake provider is a normal registry entry (not a test-only import)."""
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


class FakeProvider(Provider):
    name = "fake"
    capabilities = frozenset({Cap.RESUME, Cap.STREAM, Cap.STRUCTURED, Cap.TOKENS, Cap.COST_MONEY,
                              Cap.EXPORT, Cap.CATALOG, Cap.HEALTH, Cap.FIND_SESSION})

    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    def build_command(self, spec: RunSpec) -> list[str]:
        src = spec.prompt.strip()
        queue = os.environ.get(ENV_QUEUE)
        if not src.startswith("{") and queue:
            # Cross-process e2e tests: next scenario from the queue dir (in order).
            files = sorted(Path(queue).glob("*.json"))
            if files:
                src = files[0].read_text(encoding="utf-8")
                files[0].rename(files[0].with_suffix(".used"))
            else:
                src = '{"session": "ses_empty", "steps": []}'
        if src.startswith("{"):
            p = Path(spec.cwd) / ".ahub" / f"scenario_{abs(hash(src))}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
            src = str(p)
        cmd = [sys.executable, "-m", "ahub.providers.fake_agent", src]
        if spec.session_id:
            cmd += ["--session", spec.session_id]
        return cmd

    def env(self, spec: RunSpec) -> dict[str, str]:
        env = dict(spec.env)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        return env

    def parse_line(self, line: str, now: int) -> list[Activity]:
        s = line.strip()
        if not s.startswith("{"):
            return []
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            return []
        if not isinstance(ev, dict):
            return []
        out: list[Activity] = []
        sid = ev.get("sessionID")
        if sid:
            out.append(Activity(Act.SESSION, now, text=str(sid)))
        t = ev.get("type")
        if t == "text":
            out.append(Activity(Act.TEXT, now, text=str(ev.get("text", ""))))
        elif t == "tool_start":
            out.append(Activity(Act.TOOL_START, now, tool=str(ev.get("tool", ""))))
        elif t == "tool_end":
            out.append(Activity(Act.TOOL_END, now, tool=str(ev.get("tool", ""))))
        elif t == "step":
            out.append(Activity(Act.STEP, now))
        elif t == "usage":
            out.append(Activity(Act.USAGE, now, data={"usage": Usage(
                tokens_in=ev.get("in"), tokens_out=ev.get("out"), cost_go=ev.get("go"), cost_usd=ev.get("usd"))}))
        elif t == "error":
            text = str(ev.get("message", ""))
            low = text.lower()
            out.append(Activity(Act.ERROR, now, text=text, data={
                "transient": any(m in low for m in TRANSIENT_MARKERS),
                "quota": "quota" in low or "rate limit" in low,
                "no_access": "unauthorized" in low or "401" in low,
            }))
        return out

    def structured(self, final_text: str, activities: list[Activity], schema: dict | None) -> dict | None:
        s = final_text.strip()
        if s.startswith("```"):
            s = s.strip("`").split("\n", 1)[-1]
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def find_session(self, cwd: str, started_after_ms: int) -> str | None:
        return None  # with hide_session in the scenario — the id stays unknown (honest degradation check)

    def export(self, session_id: str) -> dict | None:
        return {"session": session_id, "note": "fake"}

    def session_state(self, session_id: str) -> SessionState | None:
        return None

    def catalog(self) -> list[ModelInfo]:
        return [ModelInfo(MODEL_ID, ("low", "high"), counter="go", price_in=0.1, price_out=0.2)]

    def health(self) -> Health:
        if self.healthy:
            return Health(ok=True)
        return Health(ok=False, problems=("fake: disabled",))
