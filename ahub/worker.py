"""Task process: python -m ahub.worker T12

Started by the service (V09b) in its own process group; drives one task with the engine and exits.
Exit code: 0 — task reached a decision (any), 2 — no task/project, 3 — held by another owner,
4 — the owner poll keeps failing or the hub code went stale under a live update
(the code cannot read the schema; the service re-picks the task).

SIGTERM/SIGINT: the provider process group is stopped (runner stops a run by group), then the process
exits — the task stays active and the service picks it up as an orphan, on the current code.
"""

from __future__ import annotations

import signal
import sys

from ahub import config
from ahub import log as hublog
from ahub.engine import Engine, PollFailed, _stale_code_error
from ahub.i18n import t as _t
from ahub.model import parse_task_id
from ahub.providers import runner
from ahub.store import Store

CMD_MARK = "ahub.worker"  # the service finds task processes in /proc by it


def find_project(name: str) -> config.ProjectConfig | None:
    projects, errors = config.load_projects()
    for p in projects:
        if p.name == name:
            return p
    for e in errors:
        hublog.get("worker").warning("project config: %s", e)
    return None


def stop_provider(signum, frame) -> None:
    """Signal handler: no provider process may outlive this process."""
    runner.request_stop()
    raise SystemExit(0)


def install_signal_handlers() -> None:
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, stop_provider)
        except ValueError:  # not the main thread (tests, an embedding process)
            pass


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
        lg.error("no task")
        return 2
    project = find_project(task.project)
    if project is None:
        lg.error("project %s not found in hub config", task.project)
        return 2
    lg.info("task process starting")
    install_signal_handlers()
    try:
        res = Engine(store, project, tid).run()
    except PollFailed:
        return 4  # the engine has logged it once; the task stays for the service to re-pick
    except (ImportError, AttributeError) as e:
        if not _stale_code_error(e):
            raise
        lg.error("stale hub code (%s: %s) — the task is left to the service",
                 type(e).__name__, str(e)[:200])
        try:
            runner.request_stop()
        except Exception:
            lg.exception("stopping the provider failed")
        return 4
    lg.info("task process done: %s %s", res.state.value, res.reason[:200])
    return 3 if res.busy else 0


if __name__ == "__main__":
    raise SystemExit(main())
