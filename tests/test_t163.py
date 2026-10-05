"""T163 model catalog: provider, real name, reasoning, plan and prices in one table.

Fake catalogs only (no network); the real binaries are never called here.
"""

from __future__ import annotations

import json

from ahub import catalog as _catalog
from ahub import cli, registry
from ahub.model import Role
from ahub.providers.base import CatalogEntry, PlanKind, infer_plan, infer_vendor
from ahub.providers.opencode import parse_models_verbose
from ahub.store import Store


def _entry(alias="spark", provider="opencode", model_id="opencode-go/muse-spark-1.3-contributor",
           variant="xhigh"):
    return registry.ModelEntry(alias, provider, model_id, variant, True, "")


def _fake_catalogs() -> dict[str, list[CatalogEntry]]:
    return {
        "opencode": [
            CatalogEntry("opencode-go/muse-spark-1.3-contributor", display_name="Muse Spark 1.3 Contributor",
                         vendor="Meta", plan=PlanKind.GO, price_in=0.1, price_out=0.2, price_cache=0.01,
                         context=1_000_000, reasoning=("low", "high", "xhigh"), status="active"),
            CatalogEntry("opencode/muse-spark-1.3-contributor-free", display_name="Muse Spark 1.3 Free",
                         vendor="Meta", plan=PlanKind.FREE, price_in=0, price_out=0,
                         context=200_000, reasoning=(), status="active"),
            CatalogEntry("openrouter/x/test", display_name="Test Model", vendor="OpenAI",
                         plan=PlanKind.PAYG, price_in=2.0, price_out=10.0, context=128_000,
                         reasoning=("low", "medium"), status="active"),
        ],
        "agy": [
            CatalogEntry("gemini-3.8-flash-high", display_name="Gemini 3.8 Flash (High)", vendor="Google",
                         plan=PlanKind.SUBSCRIPTION, reasoning=("high",)),
            CatalogEntry("gemini-3.8-flash-low", display_name="Gemini 3.8 Flash (Low)", vendor="Google",
                         plan=PlanKind.SUBSCRIPTION, reasoning=("low",)),
        ],
        "codex": [
            CatalogEntry("gpt-5.6-terra", display_name="GPT-5.6-Terra", vendor="OpenAI",
                         plan=PlanKind.SUBSCRIPTION, reasoning=("low", "medium", "high")),
        ],
    }


def test_infer_plan():
    assert infer_plan("opencode-go", "opencode-go/m", 0.1, 0.2) is PlanKind.GO
    assert infer_plan("opencode", "opencode/x-free", 1, 1) is PlanKind.FREE
    assert infer_plan("opencode", "opencode/x", 0, 0) is PlanKind.FREE
    assert infer_plan("openrouter", "openrouter/x", 1, 2) is PlanKind.PAYG
    assert infer_plan("opencode", "opencode/gpt-5.5", 2, 10) is PlanKind.PAYG
    assert infer_plan("agy", "gemini-3.8-flash-high", None, None) is PlanKind.SUBSCRIPTION
    assert infer_plan("codex", "gpt-5.6-terra", None, None) is PlanKind.SUBSCRIPTION


def test_infer_vendor():
    assert infer_vendor("deepseek-flash", "DeepSeek V4 Flash", "opencode-go/deepseek-v4-flash") == "DeepSeek"
    assert infer_vendor("gemini-flash", "Gemini 3.8 Flash", "gemini-3.8-flash-high") == "Google"
    assert infer_vendor("claude-sonnet", "Claude Sonnet 5", "x") == "Anthropic"
    assert infer_vendor("", "Space Bunny Free", "opencode/space-bunny-free") == ""
    assert infer_vendor("muse", "Muse Spark 1.3 Contributor", "opencode-go/muse-spark-1.3-contributor") == "Meta"


def test_catalog_entry_compat():
    e = CatalogEntry("opencode-go/m", display_name="Muse Spark", plan=PlanKind.GO,
                     price_in=0.1, price_out=0.2, reasoning=("low", "high"))
    assert e.variants == ("low", "high") and e.counter == "go" and e.note == "Muse Spark"
    assert CatalogEntry("a", plan=PlanKind.FREE).counter == "free"
    assert CatalogEntry("a", plan=PlanKind.PAYG).counter == "usd"
    assert CatalogEntry("a", plan=PlanKind.SUBSCRIPTION).counter == "quota"


def test_parse_verbose_new_fields():
    out = """opencode-go/muse-spark-1.3-contributor
{
  "id": "muse-spark-1.3-contributor",
  "providerID": "opencode-go",
  "name": "Muse Spark 1.3 Contributor",
  "family": "muse",
  "status": "active",
  "cost": {"input": 0.1, "output": 0.2, "cache": {"read": 0.01}},
  "limit": {"context": 1000000, "output": 32000},
  "variants": {"low": {}, "xhigh": {}}
}
"""
    (m,) = parse_models_verbose(out)
    assert m.model_id == "opencode-go/muse-spark-1.3-contributor"
    assert m.display_name == "Muse Spark 1.3 Contributor" and m.vendor == "Meta"
    assert m.plan is PlanKind.GO and m.price_in == 0.1 and m.price_cache == 0.01
    assert m.context == 1_000_000 and m.reasoning == ("low", "xhigh") and m.status == "active"
    # compat for the old readers
    assert m.variants == ("low", "xhigh") and m.counter == "go" and m.note == "Muse Spark 1.3 Contributor"


def test_agy_codex_catalog_entries():
    from ahub.providers.agy import parse_models as agy_parse
    from ahub.providers.codex import parse_models as codex_parse

    agy = agy_parse("gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n")
    assert agy[0].display_name == "Gemini 3.8 Flash (High)" and agy[0].vendor == "Google"
    assert agy[0].plan is PlanKind.SUBSCRIPTION and agy[0].price_in is None
    assert agy[0].reasoning == ("high",)
    codex = codex_parse(json.dumps({"models": [
        {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "visibility": "list",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        {"slug": "hidden", "display_name": "H", "visibility": "hide",
         "supported_reasoning_levels": []},
    ]}))
    assert [m.model_id for m in codex] == ["gpt-5.6-terra"]
    assert codex[0].plan is PlanKind.SUBSCRIPTION and codex[0].reasoning == ("low", "high")


def test_registry_plan_kind_and_cost_kind():
    assert registry.plan_kind(_entry("spark-free", "opencode",
                                     "opencode/muse-spark-1.3-contributor-free", "xhigh")) is PlanKind.FREE
    assert registry.plan_kind(_entry()) is PlanKind.GO
    assert registry.plan_kind(_entry("m", "openrouter", "openrouter/x", "")) is PlanKind.PAYG
    assert registry.plan_kind(_entry("gemini", "agy", "gemini-3.8-flash-high", "")) is PlanKind.SUBSCRIPTION
    # cost_kind keeps its callers working: free/paid/plan
    assert registry.cost_kind(_entry("spark-free", "opencode",
                                     "opencode/muse-spark-1.3-contributor-free", "")) == "free"
    assert registry.cost_kind(_entry("gemini", "agy", "gemini-3.8-flash-high", "")) == "plan"
    assert registry.cost_kind(_entry()) == "paid"


def test_reasoning_text():
    index = _catalog.index_by_id(_fake_catalogs())
    spark = _entry()
    info = index["opencode-go/muse-spark-1.3-contributor"]
    assert _catalog.reasoning_text(spark, info, index) == "xhigh (low, high)"
    free = _entry("spark-free", "opencode", "opencode/muse-spark-1.3-contributor-free", "xhigh")
    assert _catalog.reasoning_text(free, index["opencode/muse-spark-1.3-contributor-free"], index) == "xhigh"
    gemini = _entry("gemini", "agy", "gemini-3.8-flash-high", "")
    # agy siblings with the same base are the choice
    assert _catalog.reasoning_text(gemini, index["gemini-3.8-flash-high"], index) == "high (low)"
    unknown = _entry("x", "opencode", "opencode/x", "")
    assert _catalog.reasoning_text(unknown, None, {}) == "—"


def _en(monkeypatch):
    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()


def test_price_cells(monkeypatch):
    _en(monkeypatch)
    free = _entry("spark-free", "opencode", "opencode/muse-spark-1.3-contributor-free", "")
    assert _catalog.price_text(free, None) == "free"
    agy_e = _entry("gemini", "agy", "gemini-3.8-flash-high", "")
    assert _catalog.price_text(agy_e, _fake_catalogs()["agy"][0], quota_pct=0.31) == "quota 31%"
    assert _catalog.price_text(agy_e, _fake_catalogs()["agy"][0]) == "subscription"
    go_e = _entry()
    assert _catalog.price_text(go_e, _fake_catalogs()["opencode"][0],
                               go_pct=0.09, go_limit=60.0) == "Go 9% of $60"
    payg_e = _entry("m", "opencode", "openrouter/x/test", "")
    assert _catalog.price_text(payg_e, _fake_catalogs()["opencode"][2]) == "$2 / $10"


def test_context_and_model_text():
    assert _catalog.context_text(_fake_catalogs()["opencode"][0]) == "1M"
    assert _catalog.context_text(_fake_catalogs()["opencode"][1]) == "200k"
    assert _catalog.context_text(None) == "—"
    assert _catalog.model_text(_fake_catalogs()["opencode"][0]) == "Muse Spark 1.3 Contributor (Meta)"
    assert _catalog.model_text(_fake_catalogs()["opencode"][0], with_vendor=False) == "Muse Spark 1.3 Contributor"
    assert _catalog.model_text(None, fallback="opencode-go/m") == "opencode-go/m"


def test_build_rows_with_fake_catalogs(monkeypatch):
    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _fake_catalogs())
    monkeypatch.setattr(_catalog, "_quota_pct_for", lambda entry, store=None: 0.31)
    monkeypatch.setattr(_catalog, "_go_numbers", lambda: (0.09, 60.0))
    store = Store()
    rows, _extra = _catalog.build_rows(store)
    by_alias = {r.entry.alias: r for r in rows}
    assert by_alias["spark"].price == "Go 9% of $60"
    assert by_alias["spark-free"].price == "free"
    assert by_alias["gemini"].price == "quota 31%"
    assert by_alias["spark"].roles  # spark is a role default
    assert by_alias["spark"].model == "Muse Spark 1.3 Contributor (Meta)"


def test_models_table_grouped_role_json_narrow(monkeypatch, capsys):
    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _fake_catalogs())
    monkeypatch.setattr(_catalog, "_quota_pct_for", lambda entry, store=None: 0.31)
    monkeypatch.setattr(_catalog, "_go_numbers", lambda: (0.09, 60.0))
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 200)
    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "opencode" in out and "agy" in out and "codex" in out  # grouped by provider
    assert "spark" in out and "Muse Spark" in out and "go-plan" in out
    assert "quota 31%" in out and "Go 9% of $60" in out
    # --role: the same columns, only that menu
    assert cli.main(["models", "--role", Role.EXECUTOR.value]) == 0
    out_role = capsys.readouterr().out
    assert "spark" in out_role and "mimo-flash" in out_role
    # --json: every field raw
    assert cli.main(["--json", "models"]) == 0
    data = json.loads(capsys.readouterr().out)
    spark = next(m for m in data["models"] if m["alias"] == "spark")
    assert spark["display_name"] == "Muse Spark 1.3 Contributor" and spark["vendor"] == "Meta"
    assert spark["plan"] == "go-plan" and spark["price_in"] == 0.1 and spark["context"] == 1_000_000
    assert spark["alias_level"] == "xhigh" and "low" in spark["available"]
    # narrow: drop context, then vendor; never the alias or the plan
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 60)
    assert cli.main(["models"]) == 0
    narrow = capsys.readouterr().out
    assert "spark" in narrow and "go-plan" in narrow  # never dropped
    assert "context" not in narrow.lower()  # context column is gone
    assert "Meta" not in narrow  # vendor is gone too


def test_models_refresh_flag(monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(_catalog, "get_catalogs",
                        lambda refresh=False: seen.append(refresh) or _fake_catalogs())
    monkeypatch.setattr(_catalog, "_quota_pct_for", lambda entry, store=None: None)
    monkeypatch.setattr(_catalog, "_go_numbers", lambda: (None, None))
    assert cli.main(["models", "--refresh"]) == 0
    assert seen == [True]


def test_providers_short_lines(monkeypatch, capsys):
    import ahub.doctor as _doctor

    monkeypatch.setattr(_doctor, "provider_states", lambda *a, **k: [
        _doctor.ProviderState("opencode", True, True, detail="d", note="n", hint=""),
        _doctor.ProviderState("agy", True, True, detail="d", note="n", hint=""),
    ])
    monkeypatch.setattr("ahub.providers.agy.AgyProvider.quota", lambda self, force=False: [])
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _fake_catalogs())
    monkeypatch.setattr(_catalog, "_quota_pct_for", lambda entry, store=None: None)
    monkeypatch.setattr(_catalog, "_go_numbers", lambda: (None, None))
    assert cli.main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "spark — Muse Spark 1.3 Contributor (Meta)" in out
    assert cli.main(["--json", "providers"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any("models_detail" in p for p in data["providers"])


def test_opencode_cache_per_provider(tmp_path, monkeypatch):
    from ahub.providers import opencode as _oc

    monkeypatch.setattr("ahub.paths.data_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    def _fake_run(cmd, timeout=120, env=None):
        calls.append(cmd)
        if len(cmd) == 2 and cmd[1] == "models":
            return 0, "opencode-go/m\nopencode/m\n", ""
        pid = cmd[2]
        if pid == "opencode-go":
            return 0, ("opencode-go/m\n{\"providerID\": \"opencode-go\", \"name\": \"M\", "
                       "\"cost\": {\"input\": 0.1, \"output\": 0.2}, \"limit\": {\"context\": 10}, "
                       "\"variants\": {\"low\": {}}}\n"), ""
        return 0, ("opencode/m\n{\"providerID\": \"opencode\", \"name\": \"N\", "
                   "\"cost\": {\"input\": 0, \"output\": 0}, \"limit\": {\"context\": 5}}\n"), ""

    monkeypatch.setattr(_oc, "run_capture", _fake_run)
    prov = _oc.OpencodeProvider(binary="/bin/opencode-fake")
    first = prov.catalog()
    assert {m.model_id for m in first} == {"opencode-go/m", "opencode/m"}
    assert len([c for c in calls if "--verbose" in c]) == 2  # one call per provider id
    calls.clear()
    second = prov.catalog()  # cached for 24 h: no new calls
    assert {m.model_id for m in second} == {"opencode-go/m", "opencode/m"}
    assert not [c for c in calls if "--verbose" in c]
    prov.catalog(refresh=True)
    assert len([c for c in calls if "--verbose" in c]) == 2


def test_console_models_same_columns(monkeypatch):
    from ahub.tui.console import ConsoleApp

    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _fake_catalogs())
    monkeypatch.setattr(_catalog, "_quota_pct_for", lambda entry, store=None: None)
    monkeypatch.setattr(_catalog, "_go_numbers", lambda: (None, None))
    store = Store()
    app = ConsoleApp(store=store, all_projects=True)
    app._width = lambda: 200
    said: list[list[str]] = []
    app._say = lambda lines: said.append(list(lines))
    app.cmd_models([])
    text = "\n".join(said[0])
    assert "spark" in text and "Muse Spark" in text and "go-plan" in text
