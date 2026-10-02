"""register_project: меняет только projects, [telegram]/[usage] и комментарии целы."""

from __future__ import annotations

from ahub import config, paths
from ahub.commands.setup import register_project
from tests.conftest import write


def _clean_env(monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)


def test_keeps_sections_and_comments(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    proj = tmp_path / "webapp"
    proj.mkdir()
    write(paths.global_config_path(), '# мой хаб\nprojects = ["/srv/old"]\n\n'
          '[telegram]\ntoken = "t"\nchat_id = 5\nproxy = "http://127.0.0.1:8080"\n\n'
          '[usage]\ngo_month_limit = 60.0\n')
    assert register_project(proj) is True
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "# мой хаб" in text and '[telegram]' in text and '[usage]' in text
    assert 'token = "t"' in text and "go_month_limit = 60.0" in text
    hub = config.load_hub()
    assert str(proj) in hub.projects and "/srv/old" in hub.projects
    assert hub.tg_token == "t" and hub.tg_chat_id == 5
    assert hub.go_month_limit == 60.0


def test_replaces_multiline_projects(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    write(paths.global_config_path(), f'projects = [\n  "{a}",\n]\n\n[telegram]\ntoken = "t"\n')
    assert register_project(b) is True
    hub = config.load_hub()
    assert hub.projects == (str(a), str(b))
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert '[telegram]' in text and 'token = "t"' in text
    assert register_project(b) is False  # повтор — без дублей и без перезаписи
    assert config.load_hub().projects == (str(a), str(b))


def test_creates_file_and_inserts_before_section(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    proj = tmp_path / "n"
    proj.mkdir()
    assert register_project(proj) is True
    assert config.load_hub().projects == (str(proj),)
    # файла нет, но есть секция без projects — projects вставляется до неё
    other = tmp_path / "m"
    other.mkdir()
    write(paths.global_config_path(), '# коммент\n[telegram]\ntoken = "t"\n')
    assert register_project(other) is True
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert text.index("projects = ") < text.index("[telegram]")
    assert "# коммент" in text and 'token = "t"' in text
    assert config.load_hub().tg_token == "t"
