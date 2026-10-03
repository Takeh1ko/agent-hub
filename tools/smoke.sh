#!/usr/bin/env bash
# Smoke test "as a new user": build the wheel, install it into a fresh venv, then do what a new user
# does — version, doctor, the setup wizard in a clean HOME, and one task end to end on the fake provider.
# It catches what unit tests cannot: a wheel without its data files, a wizard that crashes in a clean
# HOME, a task that never reaches DONE.
#
# Only pip needs the network (the wheel's dependencies and the build backend). The real hub, the real
# HOME and the repository are never touched: HOME, AHUB_HOME and the task project all live in a temp dir.
#
# Usage: tools/smoke.sh     env: PYTHON (default python3), SMOKE_KEEP=1 (keep the temp dir), SMOKE_TIMEOUT_S
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
TIMEOUT_S="${SMOKE_TIMEOUT_S:-120}"
KEEP="${SMOKE_KEEP:-0}"

die() { echo "smoke: FAIL: $*" >&2; exit 1; }
step() { echo "smoke: $*"; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/ahub-smoke.XXXXXX")"
cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ] || [ "$KEEP" = "1" ]; then
    echo "smoke: temp dir kept: $TMP" >&2
  else
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

# --- 1. the wheel -----------------------------------------------------------------------------------
step "build the wheel"
"$PY" -m build --version >/dev/null 2>&1 || die "python -m build is missing — $PY -m pip install build"
"$PY" -m build --wheel --outdir "$TMP/dist" "$ROOT" > "$TMP/build.log" 2>&1 || {
  tail -30 "$TMP/build.log" >&2
  die "python -m build failed"
}
WHEEL="$(ls "$TMP"/dist/*.whl 2>/dev/null | head -1)" || die "no wheel in $TMP/dist"
step "wheel $(basename "$WHEEL")"

# The package data the wheel must carry: migrations (the DB cannot start without them) and the skill.
"$PY" - "$WHEEL" <<'EOF' || die "the wheel is missing package data"
import sys, zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
need = ["ahub/migrations/001_init.sql", "ahub/claude/SKILL.md"]
missing = [n for n in need if n not in names]
if missing:
    sys.exit("not in the wheel: " + ", ".join(missing))
print(f"smoke: package data in the wheel: {len([n for n in names if n.endswith('.sql')])} sql, SKILL.md")
EOF

# --- 2. a fresh venv, the wheel not editable ---------------------------------------------------------
step "install into a fresh venv"
"$PY" -m venv "$TMP/venv"
"$TMP/venv/bin/python" -m pip install --quiet "$WHEEL" > "$TMP/pip.log" 2>&1 || {
  tail -30 "$TMP/pip.log" >&2
  die "pip install of the wheel failed"
}
AHUB="$TMP/venv/bin/ahub"
[ -x "$AHUB" ] || die "no ahub script in the venv"

# --- 3. an isolated hub in a clean HOME --------------------------------------------------------------
export HOME="$TMP/home"
export AHUB_HOME="$TMP/hub"
export AHUB_PROBE=0
export AHUB_LANG=en
export AHUB_FAKE_PROVIDER=1   # the model "fake" becomes the role default — no network, no real model
export AHUB_FAKE_QUEUE="$TMP/fakeq"
unset PYTHONPATH               # the checkout must not shadow the installed package
mkdir -p "$HOME" "$AHUB_HOME" "$AHUB_FAKE_QUEUE"

REPO="$TMP/repo"
mkdir -p "$REPO"
GIT=(git -c init.defaultBranch=main -c user.email=smoke@example.com -c user.name=smoke -c commit.gpgsign=false)
step "a fresh git repo: $REPO"
"${GIT[@]}" init -q "$REPO"
printf '# smoke\n' > "$REPO/README.md"
"${GIT[@]}" -C "$REPO" add -A
"${GIT[@]}" -C "$REPO" commit -q -m init

step "ahub version"
"$AHUB" version

step "ahub doctor --json"
# doctor exits 1 when a check fails (no opencode in a clean environment is one) — the smoke only needs
# valid JSON and no crash; exit 2 or a traceback is the failure.
DOCTOR_RC=0
"$AHUB" --json doctor > "$TMP/doctor.json" || DOCTOR_RC=$?
[ "$DOCTOR_RC" -le 1 ] || die "ahub doctor exited $DOCTOR_RC"
"$PY" - "$TMP/doctor.json" <<'EOF' || die "ahub doctor did not print valid JSON"
import json, sys

with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
checks = data["checks"]
bad = [c["name"] for c in checks if c.get("ok") is False]
print(f"smoke: doctor: {len(checks)} checks, {len(bad)} failed{': ' + ', '.join(bad) if bad else ''}")
EOF

step "ahub setup --yes in a fresh HOME"
( cd "$REPO" && "$AHUB" setup --yes ) > "$TMP/setup.log" 2>&1 || {
  tail -30 "$TMP/setup.log" >&2
  die "ahub setup --yes failed"
}
[ -f "$REPO/.hub.toml" ] || die "ahub setup did not create .hub.toml"
step "setup done: .hub.toml created"

# --- 4. one task end to end ------------------------------------------------------------------------
step "write the fake provider scenario"
"$PY" - "$AHUB_FAKE_QUEUE/001.json" <<'EOF'
import json, sys

# What a scout worker must produce: report.md + result.json with status done.
scenario = {"session": "ses_smoke", "steps": [
    {"event": {"type": "tool_end", "tool": "read"}},
    {"event": {"type": "usage", "in": 100, "out": 10, "go": 0.01}},
    {"write": {"path": ".ahub/report.md", "text": "## Summary\nsmoke: the wheel works\n"}},
    {"write": {"path": ".ahub/result.json",
               "text": json.dumps({"summary": "smoke ok", "status": "done"})}},
    {"event": {"type": "text", "text": "done"}},
]}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(scenario, f)
EOF

step "ahub task new --kind scout"
( cd "$REPO" && "$AHUB" --json task new --kind scout --title "smoke: is the wheel alive?" \
    --spec "look at the repository and answer" ) || die "ahub task new failed"

step "ahub service start"
( cd "$REPO" && "$AHUB" service start ) > "$TMP/service.log" 2>&1 || {
  cat "$TMP/service.log" >&2
  die "ahub service start failed"
}

step "wait for T1 to reach done (timeout ${TIMEOUT_S}s)"
DEADLINE=$((SECONDS + TIMEOUT_S))
STATE=""
while :; do
  STATE="$("$AHUB" --json status T1 2>/dev/null | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["task"]["state"])' 2>/dev/null || true)"
  case "$STATE" in
    done) break ;;
    ""|queued|preparing|working|checking|reviewing|fixing)
      [ "$SECONDS" -lt "$DEADLINE" ] || break
      sleep 1
      ;;
    *)
      "$AHUB" result T1 || true
      die "T1 stopped at state=$STATE"
      ;;
  esac
done
[ "$STATE" = "done" ] || {
  tail -20 "$AHUB_HOME/state/logs/service.log" 2>/dev/null >&2 || true
  tail -40 "$AHUB_HOME/state/workers/T1.log" 2>/dev/null >&2 || true
  die "T1 did not reach done in ${TIMEOUT_S}s (last state: ${STATE:-unknown})"
}
( cd "$REPO" && "$AHUB" result T1 ) || true

( cd "$REPO" && "$AHUB" service stop ) >/dev/null 2>&1 || true

echo "SMOKE OK: wheel installed, doctor + setup in a clean HOME, scout T1 done (temp: $TMP)"