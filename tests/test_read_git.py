"""Git-слой на настоящем временном репозитории."""

from __future__ import annotations

import subprocess

from hub.read.git import branch_commits, diff_stat, is_dirty, worktrees


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "agent/T01")
    (repo / "a.txt").write_text("1\n2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "второй")
    (repo / "a.txt").write_text("1\n2\n3\n", encoding="utf-8")
    _git(repo, "commit", "-am", "третий")
    return repo


def test_branch_commits_and_diff(tmp_path):
    repo = _repo(tmp_path)
    assert branch_commits(str(repo), "main", "agent/T01") == 2
    assert branch_commits(str(repo), "main", "нет-ветки") == 0
    stat = diff_stat(str(repo), "main", "agent/T01")
    assert "a.txt" in stat
    assert diff_stat(str(repo), "main", "нет-ветки") == ""


def test_is_dirty(tmp_path):
    repo = _repo(tmp_path)
    assert not is_dirty(str(repo))
    (repo / "a.txt").write_text("грязь\n", encoding="utf-8")
    assert is_dirty(str(repo))


def test_worktrees(tmp_path):
    repo = _repo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", str(wt), "main")
    paths = [w["path"] for w in worktrees(str(repo))]
    assert str(repo) in paths and str(wt) in paths
    assert worktrees(str(tmp_path)) == []
