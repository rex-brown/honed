#!/usr/bin/env bash
# Full-corpus run from a frozen copy of the code and configuration (ARCHITECTURE.md section 8).
#
# The copy: src/, honed.toml and yardstick/ (the judge prompts) go to data/runtime/, overwriting what is there, and
# every stage runs from it with PYTHONPATH=data/runtime/src, so edits to the working tree can't reach a run in
# progress. The copied honed.toml resolves its paths from its own directory, so data/runtime/data links back to
# data/: the store, blobs, clones and heartbeat stay the real ones. Before any stage, the script checks that
# `import honed` resolves to the copy and not to the editable install.
#
# Stages: harvest (the whole [corpus]: the human-review corpus with its bug-targeted and approval-only samples, and
# the AI-feedback corpus; run again while it stops at the GraphQL point budget), pack, label --wait-for-reset,
# audit-judge --wait-for-reset. All output is appended, timestamped, to data/corpus_run.log.
# data/corpus_run_status.json holds {stage, started_at, updated_at, exit_code}: exit_code is null while running,
# and updated_at is refreshed every minute. data/llm_status.json is the model-call heartbeat.
#
# Idempotent: running it again resumes (harvest cursors, existing packs are skipped, judgments and model answers are
# cached). Exits non-zero when a stage fails: 3 when it stopped cleanly and can resume (a plan limit that is not a
# window reset, the call cap, the network), 2 when the frozen copy can't be set up, else the stage's exit code.
#
# Usage: scripts/full_corpus_run.sh [--resume] [--repo OWNER/NAME ...] [--limit N] [--max-calls N]
#                                   [--package NAME] [--config-file FILE]
#   --repo goes to harvest, pack and label; --limit to harvest and label; --max-calls to label and audit-judge.
#   Without options it runs the whole corpus.
#   --resume: after a crash past harvest, restart at pack (then label, then audit-judge) on the frozen copy as it
#   is: no copy (data/runtime/ is not refreshed from the working tree), no harvest. It appends to the same log and
#   keeps the status file's started_at. It runs the package the copy holds, with the copy's config file: honed and
#   honed.toml, or prreview and prreview.toml for a copy frozen before the project was renamed to Honed.
#   --package NAME: the package the stages run, as `python -m NAME.cli` (default: honed; on --resume, the one the
#   frozen copy holds). --config-file FILE: its configuration file, at the root and in the copy (default: NAME.toml;
#   on --resume, the one the frozen copy holds).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data"
RUNTIME="$DATA/runtime"
LOG="$DATA/corpus_run.log"
STATUS="$DATA/corpus_run_status.json"
PIDFILE="$DATA/corpus_run.pid"
PY="$ROOT/.venv/bin/python"
HARVEST_ROUNDS=40  # harvest re-runs after a point-budget stop, at most
DEFAULT_PACKAGE=honed
KNOWN_PACKAGES=(honed prreview)  # what a frozen copy may hold: prreview is the package's name before the rename

REPOS=()
LIMIT=()
MAX_CALLS=()
RESUME=0
PACKAGE=""
CONFIG_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPOS+=(--repo "$2"); shift 2 ;;
    --limit) LIMIT=(--limit "$2"); shift 2 ;;
    --max-calls) MAX_CALLS=(--max-calls "$2"); shift 2 ;;
    --resume) RESUME=1; shift ;;
    --package) PACKAGE="$2"; shift 2 ;;
    --config-file) CONFIG_FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^$/p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

resolve_names() {  # PACKAGE and CONFIG_FILE: as given, else the defaults, else (--resume) what the frozen copy holds
  local candidate
  if [ -z "$PACKAGE" ]; then
    PACKAGE="$DEFAULT_PACKAGE"
    if [ "$RESUME" -eq 1 ]; then
      for candidate in "${KNOWN_PACKAGES[@]}"; do
        if [ -f "$RUNTIME/src/$candidate/__init__.py" ]; then PACKAGE="$candidate"; break; fi
      done
    fi
  fi
  if [ -z "$CONFIG_FILE" ]; then
    CONFIG_FILE="$PACKAGE.toml"
    if [ "$RESUME" -eq 1 ] && [ ! -f "$RUNTIME/$CONFIG_FILE" ]; then
      for candidate in "${KNOWN_PACKAGES[@]}"; do
        if [ -f "$RUNTIME/$candidate.toml" ]; then CONFIG_FILE="$candidate.toml"; break; fi
      done
    fi
  fi
}
resolve_names
case "$PACKAGE" in
  [!A-Za-z_]* | *[!A-Za-z0-9_]*) echo "--package: not a Python package name: $PACKAGE" >&2; exit 2 ;;
esac
case "$CONFIG_FILE" in
  '' | . | .. | */*) echo "--config-file: not a file name: $CONFIG_FILE" >&2; exit 2 ;;
esac

mkdir -p "$RUNTIME"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "a corpus run is already in progress (pid $(cat "$PIDFILE")); not starting another" >&2
  exit 2
fi
echo $$ > "$PIDFILE"

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ "$RESUME" -eq 1 ] && [ -f "$STATUS" ]; then  # the run being resumed keeps its start time
  PREVIOUS="$(sed -n 's/^ *"started_at": "\([^"]*\)".*/\1/p' "$STATUS")"
  [ -n "$PREVIOUS" ] && STARTED="$PREVIOUS"
fi
echo setup > "$RUNTIME/.stage"
echo null > "$RUNTIME/.exit_code"
rmdir "$STATUS.lock" 2>/dev/null

write_status() {  # rewrite the status file from the state files, under a lock shared with the heartbeat
  local tries=0
  until mkdir "$STATUS.lock" 2>/dev/null; do
    tries=$((tries + 1)); [ "$tries" -gt 50 ] && break; sleep 0.1
  done
  printf '{\n  "stage": "%s",\n  "started_at": "%s",\n  "updated_at": "%s",\n  "exit_code": %s\n}\n' \
    "$(cat "$RUNTIME/.stage")" "$STARTED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(cat "$RUNTIME/.exit_code")" \
    > "$STATUS.tmp.$1" && mv -f "$STATUS.tmp.$1" "$STATUS"
  rmdir "$STATUS.lock" 2>/dev/null
}

say() {  # a timestamped line in the log and on stdout
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) [corpus_run] $*"
  echo "$line" | tee -a "$LOG"
}

stamp() {  # prefix every line with a UTC timestamp, as it arrives
  "$PY" -u -c '
import datetime, sys
for line in iter(sys.stdin.readline, ""):
    sys.stdout.write(datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ ") + line)
'
}

STAGE_PID=""
HEARTBEAT_PID=""
finish() {
  local code=$?
  trap - EXIT
  [ -n "$STAGE_PID" ] && pkill -TERM -P "$STAGE_PID" 2>/dev/null
  [ -n "$HEARTBEAT_PID" ] && kill "$HEARTBEAT_PID" 2>/dev/null
  echo "$code" > "$RUNTIME/.exit_code"
  write_status main
  say "exit $code (stage $(cat "$RUNTIME/.stage"))"
  rm -f "$PIDFILE"
  exit "$code"
}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

write_status main
( while sleep 60 && kill -0 $$ 2>/dev/null; do write_status beat; done ) &  # stops with the runner
HEARTBEAT_PID=$!

# ---- the frozen copy ---------------------------------------------------------------------------------------------

[ -x "$PY" ] || { say "no $PY: run \`uv sync\` first"; exit 2; }
if [ "$RESUME" -eq 1 ]; then
  say "setup: --resume: reusing the frozen copy in $RUNTIME as it is (nothing copied): $PACKAGE with $CONFIG_FILE"
  [ -f "$RUNTIME/src/$PACKAGE/__init__.py" ] && [ -f "$RUNTIME/$CONFIG_FILE" ] && [ -d "$RUNTIME/yardstick" ] \
    && [ -L "$RUNTIME/data" ] \
    || { say "setup: --resume needs a frozen copy of $PACKAGE with $CONFIG_FILE in $RUNTIME; run without --resume"
         exit 2; }
else
  say "setup: freezing src/, $CONFIG_FILE and yardstick/ into $RUNTIME"
  [ -f "$ROOT/src/$PACKAGE/__init__.py" ] && [ -f "$ROOT/$CONFIG_FILE" ] \
    || { say "setup: no package $PACKAGE in src/, or no $CONFIG_FILE in $ROOT"; exit 2; }
  rsync -a --delete --exclude '__pycache__' "$ROOT/src/" "$RUNTIME/src/" \
    && rsync -a --delete --exclude '__pycache__' "$ROOT/yardstick/" "$RUNTIME/yardstick/" \
    && cp -f "$ROOT/$CONFIG_FILE" "$RUNTIME/$CONFIG_FILE" \
    && ln -sfn .. "$RUNTIME/data" \
    || { say "setup: copying failed"; exit 2; }
  for stale in "${KNOWN_PACKAGES[@]}"; do  # an earlier freeze's config file of another name: one config per copy
    [ "$stale.toml" = "$CONFIG_FILE" ] || rm -f "$RUNTIME/$stale.toml"
  done
fi
export PYTHONPATH="$RUNTIME/src" PYTHONUNBUFFERED=1
cd "$RUNTIME" || exit 2
LOADED="$("$PY" -c "import $PACKAGE; print($PACKAGE.__file__)")"
if [ "$LOADED" != "$RUNTIME/src/$PACKAGE/__init__.py" ]; then
  say "setup: $PACKAGE resolves to $LOADED, not the frozen copy; refusing to run"
  exit 2
fi
say "setup: $PACKAGE resolves to $LOADED (the frozen copy)"

run_stage() {  # run_stage NAME ARGS...: one command of the frozen package, logged; returns its exit code
  local name="$1" code
  shift
  echo "$name" > "$RUNTIME/.stage"
  write_status main
  say "stage $name: $PACKAGE $*"
  ( "$PY" -m "$PACKAGE.cli" --config "$RUNTIME/$CONFIG_FILE" "$@" 2>&1 | stamp >> "$LOG" ) &
  STAGE_PID=$!
  wait "$STAGE_PID"
  code=$?
  STAGE_PID=""
  say "stage $name: exit $code"
  return "$code"
}

# ---- stages ------------------------------------------------------------------------------------------------------

if [ "$RESUME" -eq 1 ]; then
  say "stage harvest: skipped (--resume)"
else
  round=1
  while :; do
    run_stage harvest harvest ${REPOS[@]+"${REPOS[@]}"} ${LIMIT[@]+"${LIMIT[@]}"}
    code=$?
    [ "$code" -eq 0 ] && break
    if [ "$code" -eq 3 ] && [ "$round" -lt "$HARVEST_ROUNDS" ]; then
      say "harvest stopped at the GraphQL point budget; resuming (round $((round + 1)))"
      round=$((round + 1))
      continue
    fi
    exit "$code"
  done
fi
run_stage pack pack ${REPOS[@]+"${REPOS[@]}"} || exit $?
run_stage label label --wait-for-reset ${REPOS[@]+"${REPOS[@]}"} ${LIMIT[@]+"${LIMIT[@]}"} \
  ${MAX_CALLS[@]+"${MAX_CALLS[@]}"} || exit $?
run_stage audit-judge audit-judge --wait-for-reset ${MAX_CALLS[@]+"${MAX_CALLS[@]}"} || exit $?
echo done > "$RUNTIME/.stage"
exit 0
