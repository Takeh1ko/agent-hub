"""The home screen of `ahub` (no arguments): one header, what is going on, what to run next.

Not argparse usage — a person opening the command wants to know whether the hub works and what is
running. The layout is ui.py's (a header line, kv rows, a few suggested commands); the words come
through t().
"""

from __future__ import annotations

from pathlib import Path

import ahub
from ahub import config, paths, pulse, reasons, ui, views
from ahub.i18n import t as _t
from ahub.model import ACTIVE, WAITING_DECISION
from ahub.service import HEARTBEAT_KEY, live_workers
from ahub.store import Store
from ahub.time import now_ms

MAX_TASKS = 5  # the screen is a glance, not a list: `ahub status` has all of them


def _head(project: str | None, alive: bool) -> str:
    svc = _t("home.head_up") if alive else _t("home.head_down")
    return ui.styled(_t("home.head", version=ahub.__version__,
                        project=project or _t("home.no_project"), svc=svc), "bold")


def _project_here() -> str | None:
    """The project of the current directory, '' — no .hub.toml anywhere above (not an error here)."""
    try:
        return config.load_project(Path.cwd()).name
    except (FileNotFoundError, config.ConfigError):
        return None


def _configured() -> bool:
    return paths.global_config_path().is_file()


def _suggestions(*keys: str) -> str:
    """The 'Next' block: three or four commands, as one aligned column."""
    return ui.bullets([_t(k) for k in keys], indent=2)


def text(*, w: int | None = None) -> str:
    """The whole screen as one string (the caller prints it)."""
    store = Store()
    now = now_ms()
    hb = store.meta_get(HEARTBEAT_KEY)
    alive = bool(hb) and now - int(hb) < 30_000
    out = [_head(_project_here(), alive)]
    if not _configured():
        out.append(ui.para(_t("home.unconfigured") + " — " + _t("home.setup_hint"), indent=2, w=w))
        out.append(_suggestions("home.next_setup", "home.next_doctor", "home.next_models"))
        return "\n".join(out)

    live = live_workers()
    projects, _errors = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects, now=now)
    active = store.list_tasks(states=ACTIVE)[:MAX_TASKS]
    waiting = store.list_tasks(states=WAITING_DECISION)

    if active:
        out.append(ui.section(_t("home.sec_tasks")))
        out.append(ui.table(["", _t("views.col_id"), _t("views.col_kind"), _t("views.col_title"),
                             _t("views.col_state"), _t("views.col_model"), _t("views.col_idle")],
                            _task_rows(store, active, live, pulses, now), max_width=[1, 6, 7, None, 13, 10, 8],
                            indent=2, w=w))
    else:
        out.append(ui.para(_t("home.no_tasks"), indent=2, w=w))
    if waiting:
        out.append(ui.section(_t("home.sec_decide")))
        out.append(ui.kv([(t.label, [views.state_word(t.state), reasons.text(t.state_reason)])
                          for t in waiting], indent=2, w=w))
        out.append(ui.styled(ui.para(_t("views.next_decide", label=waiting[0].label), indent=2, w=w), "dim"))
    out.append(ui.section(_t("home.sec_next")))
    out.append(_suggestions(*_next_keys(alive, waiting)))
    return "\n".join(out)


def _task_rows(store: Store, tasks: list, live: dict[int, int], pulses: dict, now: int) -> list[list[str]]:
    rows = []
    for task in tasks:
        pl = pulses.get(task.id)
        mark = ui.badge(pl.mark, "", pl.state) if pl else ("⚫" if task.id not in live else "")
        rows.append([mark, task.label, task.kind.value, task.title, _state_cell(task),
                     task.executor or "—", views.age(task.updated_at, now)])
    return rows


def _state_cell(task) -> str:
    if task.state in ACTIVE and task.phase:
        return views.PHASE_WORDS.get(task.phase, task.phase)
    return views.state_word(task.state)


def _next_keys(alive: bool, waiting: list) -> tuple[str, ...]:
    """Three or four next commands, chosen by what the screen says."""
    if waiting:
        return ("home.next_status", "home.next_top", "home.next_doctor", "home.next_task")
    if not alive:
        return ("home.next_setup", "home.next_doctor", "home.next_status", "home.next_task")
    return ("home.next_watch", "home.next_status", "home.next_top", "home.next_task")
