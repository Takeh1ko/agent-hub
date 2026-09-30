"""Фейковый агент для тестов: настоящий процесс, который играет сценарий из JSON.

Запуск: python -m ahub.providers.fake_agent <файл-сценария> [--session ID]

Сценарий: {"session": "ses_x", "steps": [...], "exit": 0}
Шаги:
  {"event": {...}}                   — напечатать JSON-строку события (type: text|tool_start|tool_end|step|error|usage)
  {"sleep": 0.3}                     — пауза
  {"child": 1.5}                     — запустить дочерний процесс на N секунд и ждать его (молчание с ребёнком)
  {"bg": 30, "detach": false}        — бросить фоновый процесс и выйти (detach — ещё и setsid)
  {"write": {"path": "a.txt", "text": "..."}}  — записать файл в cwd
  {"git_commit": "сообщение"}        — git add -A && git commit в cwd
  {"result": {...}}                  — .ahub/result.json с commit = текущий HEAD
  {"stderr": "текст"}                — строка в stderr
  {"crash": true}                    — завершиться сразу без результата (код 137)
Если передан --session, id сессии = он (продолжение), иначе session из сценария.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    scenario = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    sid = scenario.get("session", "")
    if "--session" in argv:
        sid = argv[argv.index("--session") + 1]
    if sid and not scenario.get("hide_session"):
        print(json.dumps({"type": "session", "sessionID": sid}), flush=True)
    for step in scenario.get("steps", []):
        if "event" in step:
            ev = dict(step["event"])
            ev.setdefault("sessionID", sid)
            print(json.dumps(ev, ensure_ascii=False), flush=True)
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
        elif "bg" in step:
            # Брошенный фоновый процесс (как `yes > /dev/null &` агента); detach — ещё и уйти из группы (setsid).
            subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({float(step['bg'])})"],
                             start_new_session=bool(step.get("detach")), stdout=subprocess.DEVNULL)
            time.sleep(float(step.get("settle", 2.5)))  # чтобы сторож успел заметить потомка
        elif "child" in step:
            subprocess.run([sys.executable, "-c", f"import time; time.sleep({float(step['child'])})"])
        elif "write" in step:
            p = Path.cwd() / step["write"]["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(step["write"].get("text", ""), encoding="utf-8")
        elif "git_commit" in step:
            subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", step["git_commit"]], check=True, capture_output=True)
        elif "result" in step:
            # Итог работника с настоящим HEAD (как сделал бы работник): commit подставляется сам.
            res = dict(step["result"])
            head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            res.setdefault("commit", head)
            res.setdefault("status", "done")
            p = Path.cwd() / ".ahub" / "result.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        elif "stderr" in step:
            print(step["stderr"], file=sys.stderr, flush=True)
        elif step.get("crash"):
            return 137
    return int(scenario.get("exit", 0))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
