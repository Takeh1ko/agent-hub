"""Self-update onto new code: the fingerprint of the package, the health of the new code, re-exec.

Both long-running processes — the service and the Telegram bot — compare the code every CODE_CHECK_S and,
when it changed and the new code answers, replace themselves with the same command line (the pid stays, so
systemd never notices). A merge that adds a DB column must not leave hours-old code running.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

CODE_CHECK_S = 10.0  # how often to compare code (self-update)
_PROBE = "import ahub.service, ahub.engine, ahub.worker, ahub.cli; from ahub.store import Store; Store()"


def hub_env() -> dict[str, str]:
    """The environment of a process this hub starts: ours, plus this hub on PYTHONPATH.

    Such a process must run the code that started it, not whatever `ahub` the environment happens to
    import: with an editable install of another checkout that other code wins (its schema is not ours).
    """
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = f"{root}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else root
    return env


def code_fingerprint() -> str:
    """Fingerprint of the ahub package code (.py file mtimes and sizes): changed — time to restart."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.relative_to(root)}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()


def new_code_healthy() -> tuple[bool, str]:
    """New code imports and answers — otherwise do not switch (no crash loop)."""
    try:
        r = subprocess.run([sys.executable, "-c", _PROBE],
                           capture_output=True, text=True, timeout=60, env=hub_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[-300:]
    return True, ""


def restart_self() -> None:
    """Replace the process with the same command line (pid stays — systemd never notices)."""
    os.execv(sys.executable, [sys.executable, "-m", "ahub", *sys.argv[1:]] if sys.argv[0].endswith("ahub")
             else [sys.executable, *sys.argv])
