"""Процесс задачи: python -m ahub.worker T12

Запускается сервисом (V09b) в своей группе процессов; ведёт одну задачу движком и выходит.
Код выхода: 0 — задача пришла к решению (любому), 2 — нет задачи/проекта, 3 — занята другим владельцем.
"""

from __future__ import annotations

import sys

from ahub import config
from ahub import log as hublog
from ahub.engine import Engine
from ahub.i18n import t as _t
from ahub.model import parse_task_id
from ahub.store import Store

CMD_MARK = "ahub.worker"  # по нему сервис находит процессы задач в /proc


def find_project(name: str) -> config.ProjectConfig | None:
    projects, errors = config.load_projects()
    for p in projects:
        if p.name == name:
            return p
    for e in errors:
        hublog.get("worker").warning("конфиг проекта: %s", e)
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(_t("worker.usage"), file=sys.stderr)
        return 2
    hublog.setup()
    hublog.install_excepthook("worker")
    tid = parse_task_id(args[0])
    lg = hublog.get("worker", task=tid)
    store = Store()
    task = store.get_task(tid)
    if task is None:
        lg.error("нет задачи")
        return 2
    project = find_project(task.project)
    if project is None:
        lg.error("проект %s не найден в конфиге хаба", task.project)
        return 2
    lg.info("старт процесса задачи")
    res = Engine(store, project, tid).run()
    lg.info("процесс задачи завершён: %s %s", res.state.value, res.reason[:200])
    return 3 if res.reason == _t("engine.busy") else 0


if __name__ == "__main__":
    raise SystemExit(main())
