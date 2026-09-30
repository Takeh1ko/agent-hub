"""История: длительность от created_at до finished_at — минуты, часы."""

from __future__ import annotations

from types import SimpleNamespace

from ahub import transitions
from ahub.commands.task import cmd_history
from ahub.model import State
from ahub.store import Store

BASE = 1_000_000_000_000
MIN = 60_000


def _history_text(capsys, n: int = 20) -> str:
    args = SimpleNamespace(project=None, n=n, json=False)
    assert cmd_history(args) == 0
    return capsys.readouterr().out


def test_korotkaya_v_minutakh(capsys):
    store = Store()
    tid = store.create_task(project="P", kind="scout", title="короткая", now=BASE)
    transitions.move(store, tid, State.REJECTED, now=BASE + 45 * MIN)
    out = _history_text(capsys)
    assert "45 мин" in out


def test_dlinnaya_v_chasakh(capsys):
    store = Store()
    tid = store.create_task(project="P", kind="scout", title="длинная", now=BASE)
    transitions.move(store, tid, State.REJECTED, now=BASE + 90 * MIN)
    out = _history_text(capsys)
    assert "1 ч 30 мин" in out
    assert "90 мин" not in out


def test_granitsa_60_minut(capsys):
    store = Store()
    t1 = store.create_task(project="P", kind="scout", title="граница низ", now=BASE)
    transitions.move(store, t1, State.REJECTED, now=BASE + 59 * MIN)
    t2 = store.create_task(project="P", kind="scout", title="граница верх", now=BASE)
    transitions.move(store, t2, State.REJECTED, now=BASE + 60 * MIN)
    out = _history_text(capsys)
    assert "59 мин" in out
    assert "1 ч 0 мин" in out
