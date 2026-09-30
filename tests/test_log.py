from __future__ import annotations

import json
import multiprocessing as mp
import time

from ahub import log


def _lines():
    return [json.loads(x) for x in log.log_file().read_text(encoding="utf-8").splitlines() if x.strip()]


def test_json_records_with_context():
    log.setup()
    lg = log.get("service", task=12)
    lg.info("старт")
    lg.warning("медленно %d c", 30, extra={"provider": "opencode"})
    try:
        raise ValueError("плохо")
    except ValueError:
        lg.exception("сбой шага")
    recs = _lines()
    assert [r["lvl"] for r in recs] == ["INFO", "WARNING", "ERROR"]
    assert recs[1]["comp"] == "service" and recs[1]["task"] == 12 and recs[1]["provider"] == "opencode"
    assert recs[1]["msg"] == "медленно 30 c"
    assert "ValueError: плохо" in recs[2]["exc"]


def test_setup_idempotent():
    log.setup()
    log.setup()
    log.get("x").warning("один раз")
    assert len(_lines()) == 1


def test_scan_filters():
    log.setup()
    t0 = int(time.time() * 1000)
    log.get("a").info("инфо")
    log.get("a").warning("предупреждение 1")
    log.get("b").error("ошибка 2")
    with log.log_file().open("a", encoding="utf-8") as f:
        f.write("не json\n")
    res = log.scan(t0 - 1)
    assert [r["msg"] for r in res.records] == ["предупреждение 1", "ошибка 2"]
    assert res.broken_lines == 1
    assert [r["msg"] for r in log.scan(t0 - 1, component="b").records] == ["ошибка 2"]
    assert log.scan(t0 - 1, min_level="ERROR").records[0]["comp"] == "b"
    assert log.scan(int(time.time() * 1000) + 60_000).records == []


def test_signature_groups_numbers():
    a = {"lvl": "ERROR", "comp": "task", "msg": "T12 сбой 500"}
    b = {"lvl": "ERROR", "comp": "task", "msg": "T13 сбой 502"}
    assert log.signature(a) == log.signature(b)
    assert log.summarize([a, b]) == [(log.signature(a), 2)]


def test_rotation_keeps_scanning(monkeypatch):
    log.setup()
    t0 = int(time.time() * 1000)
    log.get("a").warning("до ротации")
    assert log.rotate_if_needed(max_bytes=1)
    log.get("a").warning("после ротации")  # обработчик переоткрыл новый файл
    assert log.log_file().with_name("ahub.log.1").exists()
    assert [r["msg"] for r in log.scan(t0 - 1).records] == ["до ротации", "после ротации"]
    assert not log.rotate_if_needed(max_bytes=10**9)


def _writer(n, tag):
    log._configured = None
    log.setup()
    lg = log.get("p", tag=tag)
    for i in range(n):
        lg.warning("строка %d " + "x" * 200, i)


def test_many_processes_no_broken_lines():
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_writer, args=(200, i)) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    res = log.scan(0)
    assert res.broken_lines == 0
    assert len(res.records) == 800
