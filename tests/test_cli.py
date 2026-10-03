from __future__ import annotations

import json

from ahub import cli, paths
from tests.conftest import write


def test_version_text_and_json(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.startswith("ahub 3.")
    assert cli.main(["--json", "version"]) == 0
    assert json.loads(capsys.readouterr().out)["version"].startswith("3.")


def test_config_by_cwd(tmp_path, monkeypatch, capsys):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\nmax_parallel = 4\n')
    monkeypatch.chdir(tmp_path / "p")
    assert cli.main(["--json", "config"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["project"]["name"] == "P" and data["project"]["max_parallel"] == 4
    assert data["problems"] == []


def test_config_by_name_from_hub(tmp_path, monkeypatch, capsys):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\n')
    write(paths.global_config_path(), f'projects = ["{tmp_path / "p"}"]\n')
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "-P", "P"]) == 0
    assert capsys.readouterr().out.startswith("P ")


def test_config_errors_are_one_line(tmp_path, monkeypatch, capsys):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nmax_parallel = 0\n')
    monkeypatch.chdir(tmp_path / "p")
    assert cli.main(["config"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("ошибка:") and "name" in err and "max_parallel" in err
    assert "Traceback" not in err


def test_no_project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config"]) == 2
    assert "не относится ни к одному проекту" in capsys.readouterr().err


def test_projects_reports_problems(tmp_path, capsys):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\nrules = "nope.md"\n')
    write(paths.global_config_path(), f'projects = ["{tmp_path / "p"}", "{tmp_path / "gone"}"]\n')
    assert cli.main(["projects"]) == 1
    out = capsys.readouterr().out
    # the table of the projects, the config problem of each under its row, the entry that does not load
    assert out.splitlines()[0].split() == ["имя", "корень", "состояние"]
    assert "1 проблем" in out and "nope.md" in out and "gone" in out
