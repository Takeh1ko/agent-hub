"""findings: дедуп, усечения, лимит, битые файлы, CLI."""

from __future__ import annotations

import json

from hub.cli import main
from hub.read.findings import (
    Finding,
    dedup_findings,
    format_findings,
    load_findings,
)
from hub.store import Store


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_dedup_case_space():
    items = [
        Finding("a.py", 11, "нет проверки", "high", "muse"),
        Finding("a.py", 10, "Нет  проверки", "high", "muse"),
        Finding("a.py", 10, "нет проверки", "low", "mimo"),
        Finding("a.py", 10, "НЕТ   ПРОВЕРКИ", "high", "muse"),
    ]
    got = dedup_findings(items)
    # Первые три схлопнулись (осталось первое), строка 11 — нет; сортировка по (file, line).
    assert len(got) == 2
    assert got[0].line == 10 and got[0].author == "muse"
    assert got[1].line == 11


def test_issue_300_truncated():
    long_issue = "x" * 500
    out = format_findings([Finding("a.py", 1, long_issue, "high", "m")])
    line = out.strip()
    assert len(line.split("] ", 1)[1].rsplit(" (", 1)[0]) <= 300


def test_output_1500_bytes_50():
    items = [Finding(f"f{i % 5}.py", i, f"замечание номер {i} " + "д" * 200,
                     "medium", "muse") for i in range(50)]
    out = format_findings(dedup_findings(items))
    assert len(out.encode("utf-8")) <= 1500
    assert len(out.strip().splitlines()) >= 1


def test_load_skips_broken(tmp_path):
    wt = tmp_path / "wt"
    _write(wt / ".agent" / "review_r1.json", "{битый json")
    _write(wt / ".agent" / "review_r1_muse.json", {
        "verdict": "changes",
        "findings": [{"file": "a.py", "line": 3, "issue": "баг", "severity": "high"}],
    })
    got = load_findings(wt)
    assert len(got) == 1 and got[0].file == "a.py" and got[0].author == "muse"


def test_load_round_filter(tmp_path):
    wt = tmp_path / "wt"
    _write(wt / ".agent" / "review_r1.json", {
        "verdict": "approve",
        "findings": [{"file": "a.py", "line": 1, "issue": "одно"}],
    })
    _write(wt / ".agent" / "review_r2.json", {
        "verdict": "changes",
        "findings": [{"file": "b.py", "line": 2, "issue": "два"}],
    })
    assert {f.file for f in load_findings(wt)} == {"a.py", "b.py"}
    assert [f.file for f in load_findings(wt, round=2)] == ["b.py"]
    assert [f.file for f in load_findings(wt, round=1)] == ["a.py"]
    assert load_findings(tmp_path / "нет-каталога") == []


def test_cli_findings(tmp_path, capsys):
    wt = tmp_path / "wt-T55"
    _write(wt / ".agent" / "review_r1.json", {
        "verdict": "changes",
        "findings": [
            {"file": "x.py", "line": 7, "issue": "Нет  проверки", "severity": "high"},
            {"file": "x.py", "line": 7, "issue": "нет проверки", "severity": "low"},
        ],
    })
    s = Store()
    s.upsert_task(id="T55", stage="review r1", worktree=str(wt))
    assert main(["findings", "T55"]) == 0
    out = capsys.readouterr().out
    assert "x.py:7" in out
    assert len(out.strip().splitlines()) == 1
    assert main(["findings", "T55", "--round", "1", "--fix"]) == 0
    assert "x.py:7" in capsys.readouterr().out
