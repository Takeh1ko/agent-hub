from __future__ import annotations

import os

import pytest

from ahub import config, paths
from tests.conftest import write

V2 = """
schema_version = 2
name = "Demo"
worktrees = "$HOME/demo-wt"
work_branch = "market"
python = "/usr/bin/python3"
allowed_paths = ["core/**", "tests/**"]
max_parallel = 3
test_resource = "test_db"

[resources]
test_db = { lock = "/tmp/demo.lock" }
payments = { capacity = 1 }
short = "/tmp/short.lock"

[hooks]
task_setup = "make db"

[models]
deny = ["deepseek"]

[budget]
go = 2.5

[secrets]
exclude = ["config/prod.toml"]

[timeouts]
idle_s = 600
retry_max = 50
"""


def test_parse_v2_full(tmp_path):
    f = write(tmp_path / "demo" / ".hub.toml", V2)
    cfg = config.load_project(tmp_path / "demo")
    assert cfg.name == "Demo"
    assert cfg.root == str(tmp_path / "demo")  # by default — the file's directory
    assert cfg.source == str(f)
    assert cfg.worktrees == os.path.expandvars("$HOME/demo-wt")
    assert cfg.work_branch == "market"
    assert cfg.branch_prefix == "ahub/"
    assert cfg.allowed_paths == ("core/**", "tests/**")
    assert cfg.max_parallel == 3
    assert cfg.resources["test_db"].lock == "/tmp/demo.lock"
    assert cfg.resources["payments"].capacity == 1 and cfg.resources["payments"].lock == ""
    assert cfg.resources["short"].lock == "/tmp/short.lock"
    assert cfg.test_resource == "test_db"
    assert cfg.hooks.task_setup == "make db"
    assert cfg.models_deny == ("deepseek",)
    assert cfg.budget_go == 2.5 and cfg.budget_usd == 0.0
    assert "config/prod.toml" in cfg.secret_excludes and ".env" in cfg.secret_excludes
    assert cfg.timeouts.idle_s == 600
    assert cfg.timeouts.retry_max == 10  # the cap


def test_find_upward(tmp_path):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\n')
    deep = tmp_path / "p" / "a" / "b"
    deep.mkdir(parents=True)
    assert config.load_project(deep).name == "P"
    with pytest.raises(FileNotFoundError):
        config.load_project(tmp_path)


def test_all_errors_at_once():
    data = {
        "schema_version": 2,
        "max_parallel": 0,
        "allowed_paths": "a/**, b/**",  # a comma-separated string is fine
        "test_resource": "nope",
        "budget": {"go": -1},
        "resources": {"x": {"capacity": "два"}},
    }
    with pytest.raises(config.ConfigError) as ei:
        config.parse_project(data, "/tmp")
    errs = " | ".join(ei.value.errors)
    assert "name" in errs
    assert "max_parallel" in errs
    assert "test_resource" in errs
    assert "budget.go" in errs
    assert "resources.x.capacity" in errs
    assert len(ei.value.errors) == 5


def test_unknown_schema_version():
    with pytest.raises(config.ConfigError, match="schema_version"):
        config.parse_project({"schema_version": 7, "name": "x"}, "/tmp")


def test_bad_toml(tmp_path):
    write(tmp_path / ".hub.toml", "name = \n")
    with pytest.raises(config.ConfigError, match="TOML"):
        config.load_project(tmp_path)


def test_v1_file_translated(tmp_path):
    write(tmp_path / ".hub.toml", """
schema_version = 1
name = "Old"
root = "/srv/old"
test_lock = "/tmp/old.lock"
work_branch = "market"
idle_s = 300
retry_max = 2
[defaults]
executor = "muse"
budget_go = 1.5
budget_usd = 0.0
[levels]
easy = "gemini"
""")
    cfg = config.load_project(tmp_path)
    assert cfg.name == "Old" and cfg.root == "/srv/old"
    assert cfg.resources["test_lock"].lock == "/tmp/old.lock"
    assert cfg.test_resource == "test_lock"
    assert cfg.budget_go == 1.5
    assert cfg.timeouts.idle_s == 300 and cfg.timeouts.retry_max == 2


def test_check_project(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    cfg = config.parse_project({"schema_version": 2, "name": "R", "python": str(tmp_path / "nopy"),
                                "rules": "docs/rules.md", "worktrees": str(tmp_path / "x" / "y")}, root)
    problems = config.check_project(cfg)
    assert any(p.startswith("python") for p in problems)
    assert any(p.startswith("rules") for p in problems)
    assert any(p.startswith("worktrees") for p in problems)
    assert not any(p.startswith("root") for p in problems)


def test_hub_config_and_projects(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    write(a / ".hub.toml", 'schema_version = 2\nname = "A"\n')
    write(b / ".hub.toml", 'schema_version = 2\nname = "A"\n')  # duplicate name
    write(paths.global_config_path(),
          f'projects = ["{a}", "{b}", "{tmp_path / "missing"}"]\n')
    projects, errors = config.load_projects()
    assert [p.name for p in projects] == ["A"]
    assert any("уже занято" in e for e in errors)
    assert any("missing" in e for e in errors)


def test_hub_config_ignores_v1_location(tmp_path):
    write(tmp_path / ".config" / "agent-hub" / "config.toml", 'projects = ["/somewhere"]\n')
    assert config.load_hub().projects == ()


def test_project_for_root_and_worktrees(tmp_path):
    outer = config.parse_project({"schema_version": 2, "name": "Outer",
                                  "worktrees": str(tmp_path / "outer-wt")}, tmp_path / "outer")
    inner = config.parse_project({"schema_version": 2, "name": "Inner"}, tmp_path / "outer" / "inner")
    for d in (tmp_path / "outer" / "inner" / "x", tmp_path / "outer-wt" / "T1"):
        d.mkdir(parents=True)
    assert config.project_for(tmp_path / "outer" / "inner" / "x", [outer, inner]).name == "Inner"
    assert config.project_for(tmp_path / "outer-wt" / "T1", [outer, inner]).name == "Outer"
    assert config.project_for(tmp_path, [outer, inner]) is None


def test_paths_follow_env(tmp_path, monkeypatch):
    assert paths.db_path() == tmp_path / "ahub-home" / "ahub.db"
    assert paths.log_dir() == tmp_path / "ahub-home" / "state" / "logs"
    assert paths.global_config_path() == tmp_path / "ahub-home" / "config" / "config.toml"
    monkeypatch.delenv("AHUB_HOME")
    assert paths.global_config_path() == tmp_path / ".config" / "ahub" / "config.toml"
    assert paths.db_path() == tmp_path / ".local/share/ahub/ahub.db"
    assert paths.log_dir() == tmp_path / ".local/state/ahub/logs"


def test_hub_telegram_and_usage(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), """
projects = []

[telegram]
token = "bot123"
chat_id = 42
proxy = "http://127.0.0.1:8080"

[usage]
go_month_limit = 60.0
""")
    hub = config.load_hub()
    assert hub.tg_token == "bot123"
    assert hub.tg_chat_id == 42
    assert hub.tg_proxy == "http://127.0.0.1:8080"
    assert hub.go_month_limit == 60.0
    assert hub.telegram_enabled


def test_hub_telegram_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    hub = config.load_hub()
    assert hub.tg_token == "" and hub.tg_chat_id is None
    assert hub.tg_proxy == "" and hub.go_month_limit is None
    assert not hub.telegram_enabled


def test_hub_telegram_env_override(tmp_path, monkeypatch):
    write(paths.global_config_path(),
          '[telegram]\ntoken = "file"\nchat_id = 1\nproxy = "http://127.0.0.1:8080"\n')
    monkeypatch.setenv("AHUB_TG_TOKEN", "env-token")
    monkeypatch.setenv("AHUB_TG_CHAT", "99")
    hub = config.load_hub()
    assert hub.tg_token == "env-token" and hub.tg_chat_id == 99
    # not overridden by the environment
    assert hub.tg_proxy == "http://127.0.0.1:8080"


def test_hub_proxy_bad_scheme(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), '[telegram]\nproxy = "socks5://127.0.0.1:1080"\n')
    with pytest.raises(config.ConfigError, match="telegram.proxy"):
        config.load_hub()


def test_hub_go_limit_zero_bad(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    for bad in ("0", "-5"):
        write(paths.global_config_path(), f"[usage]\ngo_month_limit = {bad}\n")
        with pytest.raises(config.ConfigError, match="go_month_limit"):
            config.load_hub()


def test_hub_telegram_env_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AHUB_TG_TOKEN", "t")
    monkeypatch.setenv("AHUB_TG_CHAT", "7")
    hub = config.load_hub()
    assert hub.tg_token == "t" and hub.tg_chat_id == 7 and hub.telegram_enabled


def test_hub_telegram_bad_types(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), '[telegram]\ntoken = 123\nchat_id = "abc"\nproxy = 5\n'
          '[usage]\ngo_month_limit = "много"\n')
    with pytest.raises(config.ConfigError) as ei:
        config.load_hub()
    errs = " | ".join(ei.value.errors)
    assert "telegram.token" in errs
    assert "telegram.chat_id" in errs
    assert "telegram.proxy" in errs
    assert "usage.go_month_limit" in errs


def test_hub_telegram_env_bad_type(tmp_path, monkeypatch):
    monkeypatch.setenv("AHUB_TG_CHAT", "не-число")
    with pytest.raises(config.ConfigError, match="AHUB_TG_CHAT"):
        config.load_hub()


def test_hub_paths_parsed(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), """
[paths]
opencode = "$HOME/bin/opencode"
claude = "~/.claude/local/claude"
opencode_db = "$HOME/data/opencode.db"
""")
    hub = config.load_hub()
    assert hub.opencode == os.path.expandvars("$HOME/bin/opencode")
    assert hub.claude == os.path.expanduser("~/.claude/local/claude")
    assert hub.opencode_db == os.path.expandvars("$HOME/data/opencode.db")


def test_hub_paths_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    hub = config.load_hub()
    assert hub.opencode == "" and hub.claude == "" and hub.opencode_db == ""


def test_hub_paths_bad_types(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), "[paths]\nopencode = 123\nclaude = true\nopencode_db = 5\n")
    with pytest.raises(config.ConfigError) as ei:
        config.load_hub()
    errs = " | ".join(ei.value.errors)
    assert "paths.opencode" in errs
    assert "paths.claude" in errs
    assert "paths.opencode_db" in errs


def test_hub_provider_proxy_set(tmp_path, monkeypatch):
    """[providers.<name>] — the provider's own proxy; other providers keep inheriting."""
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), """
[providers.opencode]
proxy = "socks5h://127.0.0.1:1080"
no_proxy = "localhost,127.0.0.1"

[providers.agy]
proxy = ""
""")
    hub = config.load_hub()
    oc = hub.provider_proxy("opencode")
    assert oc.proxy == "socks5h://127.0.0.1:1080" and oc.no_proxy == "localhost,127.0.0.1"
    assert hub.provider_proxy("agy").proxy == ""  # explicitly no proxy
    assert hub.provider_proxy("codex") == config.ProviderProxy()  # no section — inherit


def test_hub_provider_proxy_absent_inherits(tmp_path, monkeypatch):
    """Absent key — None (inherit), "" — explicitly none; the keys are independent."""
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    assert config.load_hub().provider_proxy("opencode") == config.ProviderProxy(None, None)
    write(paths.global_config_path(), "[providers.opencode]\n")  # a section without keys
    hub = config.load_hub()
    assert hub.provider_proxies == {} and hub.provider_proxy("opencode") == config.ProviderProxy(None, None)
    write(paths.global_config_path(), '[providers.opencode]\nno_proxy = "localhost"\n')
    assert config.load_hub().provider_proxy("opencode") == config.ProviderProxy(None, "localhost")
    write(paths.global_config_path(), '[providers.opencode]\nno_proxy = ""\n')
    assert config.load_hub().provider_proxy("opencode") == config.ProviderProxy(None, "")
    write(paths.global_config_path(), '[providers.opencode]\nproxy = "http://127.0.0.1:8080"\n')
    assert config.load_hub().provider_proxy("opencode") == config.ProviderProxy("http://127.0.0.1:8080", None)


def test_hub_provider_proxy_bad_scheme(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), '[providers.agy]\nproxy = "ftp://127.0.0.1:8080"\n')
    with pytest.raises(config.ConfigError, match=r"providers\.agy\.proxy"):
        config.load_hub()
    write(paths.global_config_path(), "[providers.opencode]\nproxy = 5\n[providers.agy]\nno_proxy = 7\n")
    with pytest.raises(config.ConfigError) as ei:
        config.load_hub()
    errs = " | ".join(ei.value.errors)
    assert "providers.opencode.proxy" in errs and "providers.agy.no_proxy" in errs


def test_hub_provider_proxy_section_bad_types(tmp_path, monkeypatch):
    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), "providers = 5\n")
    with pytest.raises(config.ConfigError, match=r"\[providers\]"):
        config.load_hub()


def test_python_bin_explicit_venv_or_path(tmp_path):
    cfg = config.parse_project({"schema_version": 2, "name": "A"}, tmp_path)
    assert cfg.python_bin() == "python3"
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    assert cfg.python_bin() == str(venv_py)
    assert config.parse_project({"schema_version": 2, "name": "A", "python": "/opt/py"}, tmp_path).python_bin() == "/opt/py"
