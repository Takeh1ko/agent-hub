"""Gates: the project test resource lock — acceptance runs one at a time (the queue does not hold it)."""

from __future__ import annotations

import sys
import threading

from ahub import config, gates
from tests.conftest import write

# The acceptance test leaves a trace: when it started and when it finished.
MARKER = '''\
import pathlib
import time


def test_marker():
    log = pathlib.Path(__file__).with_name("marker.log")
    with log.open("a") as f:
        f.write("start\\n")
    time.sleep(1.5)
    with log.open("a") as f:
        f.write("end\\n")
'''


def test_acceptance_runs_serialized_by_lock(tmp_path):
    root = tmp_path / "proj"
    write(root / "tests" / "test_marker.py", MARKER)
    project = config.parse_project({
        "schema_version": 2, "name": "P", "python": sys.executable, "allowed_paths": ["tests/**"],
        "resources": {"db": {"lock": str(tmp_path / "db.lock"), "capacity": 1}}, "test_resource": "db",
    }, root)

    out: dict[int, tuple] = {}

    def run(i: int) -> None:
        out[i] = gates.run_acceptance(project, str(root), ["tests/test_marker.py"], task_label=f"T{i}")

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=180)
    assert all(ok for ok, _tail, _cmd in out.values()), out
    # no overlap: one acceptance finished before the second started
    assert (root / "tests" / "marker.log").read_text().split() == ["start", "end", "start", "end"]


def test_acceptance_without_test_resource_runs_unlocked(tmp_path):
    root = tmp_path / "proj"
    write(root / "tests" / "test_marker.py", "def test_marker():\n    assert True\n")
    project = config.parse_project({"schema_version": 2, "name": "P", "python": sys.executable}, root)
    ok, _tail, cmd = gates.run_acceptance(project, str(root), ["tests/test_marker.py"], task_label="T1")
    assert ok and cmd == "pytest -q tests/test_marker.py"
