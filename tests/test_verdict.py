"""Вердикт панели ревью: ready / next / arbiter."""

from __future__ import annotations

from hub.gate.verdict import Review, verdict

LONG = "о" * 60  # валидное обоснование (≥ 50 символов)


def test_all_approve_ready():
    rs = [Review("approve"), Review("approve", file="a.py", line=1, body="мелочь")]
    assert verdict(rs, round=1) == "ready"
    assert verdict(rs, round=2) == "ready"


def test_changes_next_then_arbiter():
    rs = [Review("approve"), Review("changes", file="a.py", line=3, body="баг")]
    assert verdict(rs, round=1) == "next"
    assert verdict(rs, round=2) == "arbiter"


def test_valid_dispute_arbiter_at_once():
    rs = [Review("dispute", file="hub/x.py", line=10, body=LONG)]
    assert verdict(rs, round=1) == "arbiter"
    assert verdict(rs, round=1, max_rounds=3) == "arbiter"


def test_dispute_without_file_counts_as_changes():
    rs = [Review("dispute", body=LONG)]  # нет file:line
    assert verdict(rs, round=1) == "next"
    assert verdict(rs, round=2) == "arbiter"


def test_dispute_short_body_counts_as_changes():
    rs = [Review("dispute", file="hub/x.py", line=10, body="коротко 10с")]
    assert verdict(rs, round=1) == "next"
    assert verdict(rs, round=2) == "arbiter"


def test_dispute_zero_line_counts_as_changes():
    rs = [Review("dispute", file="hub/x.py", line=0, body=LONG)]
    assert verdict(rs, round=1) == "next"
    assert verdict(rs, round=2) == "arbiter"


def test_approve_plus_valid_dispute_arbiter():
    rs = [Review("approve"), Review("dispute", file="hub/x.py", line=1, body=LONG)]
    assert verdict(rs, round=1) == "arbiter"


def test_changes_beats_valid_dispute():
    rs = [Review("changes", file="a.py", line=1, body="баг"),
          Review("dispute", file="b.py", line=2, body=LONG)]
    assert verdict(rs, round=1) == "next"
    assert verdict(rs, round=2) == "arbiter"
