"""The home screen of `ahub` (no arguments): one header, what is going on, what to run next.

Not argparse usage — a person opening the command wants to know whether the hub works and what is
running. The layout is ui.py's (a header line, kv rows, a few suggested commands); the words come
through t().
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import ahub
from ahub import config, paths, pulse, reasons, scope, ui, views
from ahub.i18n import t as _t
from ahub.model import ACTIVE, WAITING_DECISION
from ahub.service import HEARTBEAT_KEY, live_workers
from ahub.store import Store, Task
from ahub.time import now_ms

MAX_TASKS = 5  # the screen is a glance, not a list: `ahub status` has all of them


def _head(project: str | None, alive: bool, quota_line: str = "", w: int | None = None) -> str:
    """The header of a terminal: a rounded box, one line inside — the product, the version, the project,
    the service. A pipe gets the same words as one plain line (`_line_head`)."""
    svc = _t("home.svc_running") if alive else _t("home.svc_stopped")
    rest = _t("home.head_tty", version=ahub.__version__,
              project=project or _t("home.no_project"), svc=svc)
    inside = f"{ui.styled('✻ ahub', 'accent')} {ui.styled(rest, 'dim')}"
    if quota_line:
        inside += f" {ui.styled('· ' + quota_line, 'dim')}"
    return ui.box([inside], w=w)


def _line_head(project: str | None, alive: bool, quota_line: str = "") -> str:
    svc = _t("home.head_up") if alive else _t("home.head_down")
    head = ui.styled(_t("home.head", version=ahub.__version__,
                        project=project or _t("home.no_project"), svc=svc), "bold")
    if quota_line:
        head += " · " + quota_line
    return head


def _project_here() -> str | None:
    """The project of the current directory, '' — no .hub.toml anywhere above (not an error here)."""
    try:
        return config.load_project(Path.cwd()).name
    except (FileNotFoundError, config.ConfigError):
        return None


def _configured() -> bool:
    return paths.global_config_path().is_file()


def _scope(*, all_projects: bool, project: str | None) -> scope.Scope:
    """`--all` — every project (the owner), `--project X` — that one, else the project of this
    directory (outside every project — every project, like `ahub status` in the same place)."""
    if all_projects:
        return scope.OWNER
    if project:
        return scope.Scope((project,))
    return scope.of_dir(Path.cwd())


def _suggestions(*keys: str, w: int | None = None) -> str:
    """The 'Next' block: three or four commands — one dim line on a terminal, one bullet each in a pipe."""
    words = [_t(k) for k in keys]
    return ui.styled(" · ".join(words), "dim") if ui.colour_on() else ui.bullets(words, indent=2, w=w)


def text(*, w: int | None = None, all_projects: bool = False, project: str | None = None) -> str:
    """The whole screen as one string (the caller prints it).

    The scope is the project's own, like every handle (architecture §9): the tasks of the directory's
    project, `ahub --all` — every project. A terminal reads the screen as items — the header in a box,
    every task a `⏺` line, the way out under it — while a pipe gets the compact screen of contracts §5.
    """
    sc = _scope(all_projects=all_projects, project=project)
    store = Store()
    now = now_ms()
    hb = store.meta_get(HEARTBEAT_KEY)
    alive = bool(hb) and now - int(hb) < 30_000
    items = ui.colour_on()
    quota_line = ""
    try:
        from ahub import quota
        for t in store.list_tasks(states=ACTIVE):
            if quota.is_gemini_task(store, t):
                _prov, buckets = quota.get_model_buckets(store, t.executor or "gemini-flash")
                b_5h = next((b for b in buckets if b.group == "Gemini" and b.window == "5h"), None)
                if b_5h:
                    quota_line = quota.format_5h_line(b_5h)
                    break
    except (sqlite3.Error, KeyError, ValueError, OSError):
        pass
    out = [_head(_project_here(), alive, quota_line, w)] if items else [_line_head(_project_here(), alive, quota_line)]
    if not _configured():
        out.append(ui.item(_t("home.unconfigured"), status="working") if items
                   else ui.para(_t("home.unconfigured"), indent=2, w=w))
        out.append(_suggestions("home.next_setup", "home.next_doctor", "home.next_models", w=w))
        return "\n".join(out)

    live = live_workers()
    projects, _errors = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects, now=now)
    active = store.list_tasks(states=ACTIVE, projects=sc.projects or None)
    shown, rest = active[:MAX_TASKS], max(0, len(active) - MAX_TASKS)
    waiting = store.list_tasks(states=WAITING_DECISION, projects=sc.projects or None)
    waiting_shown, waiting_rest = waiting[:MAX_TASKS], max(0, len(waiting) - MAX_TASKS)

    if items:
        for task in shown:  # the task is the item line: the id and the title, the details under it
            out.append(ui.item(f"{task.label}  {task.title}",
                               [views._item_details(task, now), views._pulse_detail(pulses.get(task.id))],
                               w=w, status="working"))
        if rest:  # the screen is a glance — what does not fit is counted, not dropped
            out.append(ui.hint(_t("home.more_tasks", n=rest), w=w))
        if not shown:
            out.append(ui.item(_t("home.no_tasks"), status="working"))
    elif shown:
        out.append(ui.section(_t("home.sec_tasks")))
        out.append(ui.table(["", _t("views.col_id"), _t("views.col_kind"), _t("views.col_title"),
                             _t("views.col_state"), _t("views.col_model"), _t("views.col_idle")],
                            _task_rows(shown, live, pulses, now), max_width=[1, 6, 7, None, 13, 10, 8],
                            indent=2, w=w))
        if rest:  # the screen is a glance — what does not fit is counted, not dropped
            out.append(ui.para(_t("home.more_tasks", n=rest), indent=2, w=w))
    else:
        out.append(ui.para(_t("home.no_tasks"), indent=2, w=w))
    if waiting_shown:
        if items:
            lines = [_t(views.next_key(t), label=t.label) for t in waiting_shown if views._offers_next(t)]
            out.append(ui.item(_t("home.waiting_you"), lines, w=w, status="waiting"))
            if waiting_rest:  # capped like the list above — the screen is a glance, not a queue
                out.append(ui.hint(_t("home.more_tasks", n=waiting_rest), w=w))
        else:
            focus = _focus(waiting_shown)
            out.append(ui.section(_t("home.sec_decide")))
            out.append(ui.kv([(t.label, [views.state_word(t.state), reasons.text(t.state_reason)])
                              for t in waiting_shown], indent=2, w=w))
            if waiting_rest:  # capped like the list above — the screen is a glance, not a queue
                out.append(ui.para(_t("home.more_tasks", n=waiting_rest), indent=2, w=w))
            out.append(ui.kv([(_t("views.lbl_next"), _t(views.next_key(focus), label=focus.label))],
                             indent=2, w=w))  # the same "Next" line as ahub status T<id>
    if not items:
        out.append(ui.section(_t("home.sec_next")))
    out.append(_suggestions(*_next_keys(alive, waiting), w=w))
    return "\n".join(out)


def data(*, all_projects: bool = False, project: str | None = None) -> dict:
    """`ahub --json` with no subcommand: the raw fields of the screen (the text is for people)."""
    sc = _scope(all_projects=all_projects, project=project)
    store = Store()
    now = now_ms()
    hb = store.meta_get(HEARTBEAT_KEY)
    live = live_workers()
    projects, errors = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects, now=now)
    out: dict = {
        "version": ahub.__version__,
        "project": _project_here(),
        "service": "alive" if (hb and now - int(hb) < 30_000) else "down",
        "configured": _configured(),
        "scope": {"all": sc.all, "projects": list(sc.projects)},
        "live": sorted(live),
        "config_errors": list(errors),
        "tasks": [],
        "waiting": [],
    }
    for task in store.list_tasks(states=ACTIVE, projects=sc.projects or None)[:MAX_TASKS]:
        pl = pulses.get(task.id)
        out["tasks"].append({"id": task.id, "label": task.label, "project": task.project,
                             "kind": task.kind.value, "title": task.title, "state": task.state.value,
                             "phase": task.phase, "model": task.executor, "pulse": pl.mark if pl else ""})
    for task in store.list_tasks(states=WAITING_DECISION, projects=sc.projects or None)[:MAX_TASKS]:
        out["waiting"].append({"id": task.id, "label": task.label, "project": task.project,
                               "state": task.state.value, "state_reason_text": reasons.text(task.state_reason),
                               "next": _t(views.next_key(_focus([task])), label=task.label)})
    all_buckets = []
    from ahub import providers as provider_mod
    for n in list(provider_mod.names()) + (["fake"] if "fake" in provider_mod._cache else []):
        try:
            p = provider_mod.get(n)
            if hasattr(p, "quota"):
                all_buckets.extend(p.quota())
        except (KeyError, OSError, ValueError):
            pass
    out["buckets"] = [b.to_dict() for b in all_buckets]
    return out


def _focus(waiting: list) -> Task:
    """The task the "Next" line is about: a decision first (done / needs decision), else a resume."""
    from ahub.views import DECISION_STATES

    return next((t for t in waiting if t.state in DECISION_STATES), waiting[0])


def _task_rows(tasks: list, live: dict[int, int], pulses: dict, now: int) -> list[list[str]]:
    rows = []
    for task in tasks:
        pl = pulses.get(task.id)
        mark = ui.badge(pl.mark, "", pl.state) if pl else ("⚫" if task.id not in live else "")
        rows.append([mark, task.label, task.kind.value, task.title, views.state_cell(task),
                     task.executor or "—", views.age(task.updated_at, now)])
    return rows


def _next_keys(alive: bool, waiting: list) -> tuple[str, ...]:
    """Three or four next commands, chosen by what the screen says."""
    if waiting:
        return ("home.next_status", "home.next_top", "home.next_doctor", "home.next_task")
    if not alive:
        return ("home.next_setup", "home.next_doctor", "home.next_status", "home.next_task")
    return ("home.next_watch", "home.next_status", "home.next_top", "home.next_task")
