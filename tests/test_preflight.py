"""Preflight: фейковый git-worktree и фейковый /proc, хуки, замок, rules_sha."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from hub.cli import main
from hub.config import ProjectConfig
from hub.gate.preflight import preflight, run_hook
from hub.store import Store


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _mk_repo(base: Path, with_test: bool = True) -> tuple[Path, str]:
    """Чистый git-репо + начальный коммит; вернуть (путь, base_sha)."""
    repo = base / "wt"
    repo.mkdir(parents=True)
    _git(base, "init", "-b", "main", repo.name) if False else None
    # init прямо в каталоге:
    r = subprocess.run(["git", "init", "-b", "main"], cwd=str(repo),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    if with_test:
        (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    return repo, sha


def _mk_project(root: Path, lock: Path, setup: str = "") -> ProjectConfig:
    rules_dir = root / "docs" / "agents"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "rules.md").write_text("# правила\n", encoding="utf-8")
    return ProjectConfig(
        root=str(root),
        rules="docs/agents/rules.md",
        python=sys.executable,
        test_lock=str(lock),
        allowed_paths=["hub/**", "tests/**"],
        hooks=__import__("hub.config", fromlist=["Hooks"]).Hooks(task_setup=setup),
    )


def _proc_flock(proc_root: Path, pid: int, lock: Path, cwd: Path) -> None:
    d = proc_root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(f"flock\x00{lock}\x00pytest\x00-q\x00".encode())
    try:
        (d / "cwd").symlink_to(str(cwd))
    except FileExistsError:
        pass
    (d / "stat").write_text(
        f"{pid} (flock) R 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n",
        encoding="utf-8")


def test_no_task(tmp_path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    assert preflight(Store(), "нет-такой", proj).reason == "no-task"


def test_dirty(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-dirty", worktree=str(repo), base_sha=sha)
    (repo / "a.txt").write_text("грязь\n", encoding="utf-8")
    got = preflight(store, "T-dirty", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason == "dirty" and not got.ok
    assert not (store.get_task("T-dirty") or {}).get("rules_sha")


def test_base_moved(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-moved", worktree=str(repo), base_sha=sha)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "второй")
    got = preflight(store, "T-moved", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason == "base-moved"


def test_no_rules(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    # Удалить rules.
    (proj_root / "docs" / "agents" / "rules.md").unlink()
    store = Store()
    store.upsert_task(id="T-norules", worktree=str(repo), base_sha=sha)
    got = preflight(store, "T-norules", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason == "no-rules"
    assert not (store.get_task("T-norules") or {}).get("rules_sha")


def test_setup_fail(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock, setup="exit 1")
    store = Store()
    store.upsert_task(id="T-setup", worktree=str(repo), base_sha=sha)
    got = preflight(store, "T-setup", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason.startswith("setup-fail") and not got.ok
    assert ":" in got.reason
    assert not (store.get_task("T-setup") or {}).get("rules_sha")


def test_setup_fail_tail_has_stderr(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(
        proj_root, lock,
        setup=f"{sys.executable} -c \"import sys; print('бах-err', file=sys.stderr); sys.exit(3)\"",
    )
    store = Store()
    store.upsert_task(id="T-setup2", worktree=str(repo), base_sha=sha)
    got = preflight(store, "T-setup2", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason.startswith("setup-fail")
    assert "бах-err" in got.reason


def test_collect_fail(tmp_path):
    repo, sha = _mk_repo(tmp_path, with_test=False)
    (repo / "test_bad.py").write_text("def test_x(:\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "битый тест")
    sha2 = _git(repo, "rev-parse", "HEAD")
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-coll", worktree=str(repo), base_sha=sha2)
    got = preflight(store, "T-coll", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason == "collect-fail"


def test_locked(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    lock.write_text("", encoding="utf-8")
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-lock", worktree=str(repo), base_sha=sha)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _proc_flock(proc_root, 4242, lock, repo)
    got = preflight(store, "T-lock", proj, proc_root=str(proc_root))
    assert got.reason.startswith("locked:")
    assert "4242" in got.reason


def test_ok_and_rules_sha(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-ok", worktree=str(repo), base_sha=sha)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    got = preflight(store, "T-ok", proj, proc_root=str(proc_root))
    assert got.ok and got.reason == ""
    saved = store.get_task("T-ok")
    assert saved is not None
    expect = hashlib.sha256(
        (proj_root / "docs" / "agents" / "rules.md").read_bytes()).hexdigest()
    assert saved.get("rules_sha") == expect


def test_run_hook_tails_and_env(tmp_path):
    code, out, err = run_hook("echo out; echo err >&2", {}, tmp_path)
    assert code == 0 and "out" in out and "err" in err
    # Хвосты ≤ 2000 симв.
    big = f"{sys.executable} -c \"print('x'*5000)\""
    code, out, _ = run_hook(big, {}, tmp_path)
    assert code == 0 and len(out) <= 2000
    # env = os.environ + HUB_*.
    code, out, _ = run_hook("echo $HUB_TASK_ID", {"HUB_TASK_ID": "T-zzz"}, tmp_path)
    assert code == 0 and "T-zzz" in out
    # Не-ноль.
    code, _, _ = run_hook("exit 7", {}, tmp_path)
    assert code == 7


def test_cli_preflight_ok_fail(tmp_path, capsys, monkeypatch):
    import hub.commands.preflight as pf_cmd

    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-cli", worktree=str(repo), base_sha=sha)
    proc_empty = tmp_path / "proc"
    proc_empty.mkdir()
    # Подменить preflight чтобы не зависеть от /proc и perf: вызвать напрямую.
    # CLI сам строит Store() и load_project; подготовим .hub.toml в proj_root.
    (proj_root / ".hub.toml").write_text(
        "schema_version = 1\n"
        f'name = "P"\nroot = "{proj_root}"\n'
        'rules = "docs/agents/rules.md"\n'
        f'python = "{sys.executable}"\ntest_lock = ""\n'
        'allowed_paths = ["hub/**"]\n',
        encoding="utf-8")
    monkeypatch.setattr(pf_cmd, "preflight", lambda s, tid, p: preflight(
        s, tid, p, proc_root=str(proc_empty)))
    assert main(["preflight", "T-cli", "--project", str(proj_root)]) == 0
    assert "OK T-cli" in capsys.readouterr().out
    assert main(["preflight", "нет-такой", "--project", str(proj_root)]) == 1
    assert "FAIL нет-такой no-task" in capsys.readouterr().out


def test_setup_env_forwarded(tmp_path):
    # preflight передаёт HUB_TASK_ID/WORKTREE/PROJECT_ROOT в хук.
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(
        proj_root, lock,
        setup='test -n "$HUB_TASK_ID" && test -n "$HUB_WORKTREE" && test -n "$HUB_PROJECT_ROOT"',
    )
    store = Store()
    store.upsert_task(id="T-env", worktree=str(repo), base_sha=sha)
    got = preflight(store, "T-env", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.ok, got.reason


def test_python_missing_is_collect_fail(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    proj.python = "/nonexistent/bin/python"
    store = Store()
    store.upsert_task(id="T-pymiss", worktree=str(repo), base_sha=sha)
    got = preflight(store, "T-pymiss", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason == "collect-fail"


def test_order_dirty_beats_locked(tmp_path):
    # Грязь + занятый замок → первая неуспешная (dirty), дальше не идём.
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    lock.write_text("", encoding="utf-8")
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-ord1", worktree=str(repo), base_sha=sha)
    (repo / "a.txt").write_text("грязь\n", encoding="utf-8")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _proc_flock(proc_root, 7777, lock, repo)
    got = preflight(store, "T-ord1", proj, proc_root=str(proc_root))
    assert got.reason == "dirty"


def test_order_base_moved_beats_no_rules(tmp_path):
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-ord2", worktree=str(repo), base_sha=sha)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "второй")
    (proj_root / "docs" / "agents" / "rules.md").unlink()
    got = preflight(store, "T-ord2", proj, proc_root=str(tmp_path / "proc-пусто"))
    assert got.reason == "base-moved"


def test_store_fail_no_exception(tmp_path):
    # БД только на чтение → объект PreflightResult, не исключение.
    repo, sha = _mk_repo(tmp_path)
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    lock = tmp_path / "t.lock"
    proj = _mk_project(proj_root, lock)
    store = Store()
    store.upsert_task(id="T-ro", worktree=str(repo), base_sha=sha)
    db = Path(store.path)
    db.chmod(0o444)
    try:
        got = preflight(store, "T-ro", proj, proc_root=str(tmp_path / "proc-пусто"))
    finally:
        db.chmod(0o644)
    # Либо ok (root игнорирует 444), либо store-fail — но не исключение.
    assert hasattr(got, "reason") and isinstance(got.reason, str)
