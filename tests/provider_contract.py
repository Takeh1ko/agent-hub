"""Общий набор проверок контракта поставщика. Используется для фейка (всегда) и живых поставщиков
(тесты с пометкой live, запуск: AHUB_LIVE=1 pytest -m live).

make_spec(kind, cwd) — поставщик-специфичное задание:
  kind="hello"  — короткий ответ, модель должна вернуть текст со словом PONG;
  kind="resume" — продолжение той же сессии, ответ со словом PONG2.
"""

from __future__ import annotations

from collections.abc import Callable

from ahub.providers.base import Act, Cap, Outcome, Provider, RunSpec
from ahub.providers.runner import run


def check_catalog_and_health(p: Provider) -> None:
    assert p.name
    if p.has(Cap.CATALOG):
        cat = p.catalog()
        assert cat, "каталог пуст"
        assert all(m.model_id for m in cat)
    h = p.health()
    assert isinstance(h.ok, bool)
    if not h.ok:
        assert h.problems, "нездоров без объяснения"


def check_session_cycle(p: Provider, make_spec: Callable[[str, str], RunSpec], cwd: str) -> None:
    acts = []
    sids = []
    spec = make_spec("hello", cwd)
    r = run(p, spec, on_activity=acts.append, on_session=sids.append)
    assert r.outcome is Outcome.OK, (r.outcome, r.error)
    assert "PONG" in r.final_text
    if p.has(Cap.STREAM):
        assert acts, "поставщик с потоком не прислал активность"
    if p.has(Cap.RESUME):
        assert r.session_id and sids == [r.session_id], "id сессии не пойман сразу"
        spec2 = make_spec("resume", cwd)
        spec2.session_id = r.session_id
        r2 = run(p, spec2)
        assert r2.outcome is Outcome.OK, (r2.outcome, r2.error)
        assert r2.session_id == r.session_id, "продолжение открыло новую сессию"
        assert "PONG2" in r2.final_text
    if p.has(Cap.TOKENS) or p.has(Cap.COST_MONEY):
        assert r.usage is not None, "учёт заявлен, но не отдан"
    if p.has(Cap.EXPORT) and r.session_id:
        assert p.export(r.session_id) is not None
    assert any(a.kind in (Act.TEXT, Act.STEP, Act.SESSION) for a in acts) or not p.has(Cap.STREAM)
    with open(r.log_path, encoding="utf-8") as f:
        assert f.read(), "сырой лог пуст"
