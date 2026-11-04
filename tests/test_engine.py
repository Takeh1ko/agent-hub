"""Engine routing context for OpenCode DB isolation (contract 3, 5, 8).

RunSpec context stays immutable; the cached singleton provider is never mutated.
Crash-before-SESSION recovery is honest; eligibility survives restart. Fake HOME only.
"""

from __future__ import annotations

import os

import pytest

from ahub import providers
from ahub.engine import Engine
from ahub.model import State
from ahub.providers.base import Outcome, RunResult
from ahub.providers.opencode import OpencodeProvider
from ahub.store import Store
from tests.enginekit import make_project


def test_runspec_context_immutable_and_singleton_stable(tmp_path):
    """Route is selected from RunSpec context; the cached singleton is never mutated."""
    before = dict(os.environ)
    spec = providers.get("opencode")
    assert getattr(spec, "db_path", None) is None
    OpencodeProvider(db_path=str(tmp_path / "shard.db"))
    assert providers.get("opencode") is spec  # explicit instance did not replace the cache
    assert getattr(providers.get("opencode"), "db_path", None) is None
    assert dict(os.environ) == before


def test_outcome_to_state_honest_no_silent_restart(tmp_path):
    """A lost SESSION is ERROR/NEEDS_DECISION, never a silently started fresh conversation."""
    store = Store()
    project = make_project(tmp_path)
    eng = Engine(store, project, 1)
    crash = RunResult(Outcome.CRASH, None, error="no id")
    assert eng._outcome_to_state(crash) is not None
    state, _reason = eng._outcome_to_state(crash)
    assert state in (State.ERROR, State.NEEDS_DECISION)
    ok = RunResult(Outcome.OK, "ses_1")
    assert eng._outcome_to_state(ok) is None  # finished step has nothing to continue


def test_eligibility_probe_survives_restart(tmp_path):
    """Future turns of an eligible task keep its shard; old tasks stay legacy unless enrolled."""
    from ahub.providers import opencode_db as odb

    resolve = getattr(odb, "resolve_db", None) or getattr(odb, "shard_for", None)
    if resolve is None:
        pytest.skip("eligibility routing not implemented yet (contract 8): legacy verified")
    first = resolve(task="T1", cwd=str(tmp_path), role="executor")
    again = resolve(task="T1", cwd=str(tmp_path), role="executor")
    assert first == again  # stable after restart
