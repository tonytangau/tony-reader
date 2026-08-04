#!/usr/bin/env bash
# run-reader.sh — process guard for reader_server.py
#
# Mirrors run-server.sh: launches the Reader API (:8081) in a loop,
# auto-restarting it 2s after a crash. Logs restarts to stdout. Handles
# SIGTERM/SIGINT so Ctrl+C kills the loop cleanly.
#
# Usage:
#   ./run-reader.sh
#   PYTHON=python3.11 ./run-reader.sh
#
# The Reader API is a separate process from the main Dot server (:8080)
# so it can be developed/restarted independently. The Reader UI (~/dot/reader/)
# is served as static files by the main server and talks to this :8081 API.

set -u

# --- config (overridable via env) -------------------------------------------
DOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
SERVER="${SERVER:-${DOT_DIR}/reader_server.py}"
RESTART_DELAY="${RESTART_DELAY:-2}"

# --- state ------------------------------------------------------------------
RUNNING=1
CHILD_PID=""

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

shutdown() {
    if [ "$RUNNING" -eq 0 ]; then
        return
    fi
    RUNNING=0
    log "received termination signal, stopping reader server (pid=${CHILD_PID:-none})…"
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$CHILD_PID" 2>/dev/null || break
            sleep 0.2
        done
        if kill -0 "$CHILD_PID" 2>/dev/null; then
            log "reader server did not exit on SIGTERM, sending SIGKILL"
            kill -KILL "$CHILD_PID" 2>/dev/null
        fi
        wait "$CHILD_PID" 2>/dev/null
    fi
    log "guard exiting."
    exit 0
}

trap shutdown INT TERM

# --- sanity checks ----------------------------------------------------------
if [ ! -f "$SERVER" ]; then
    log "ERROR: reader server not found at $SERVER"
    exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    log "ERROR: python interpreter '$PYTHON' not found in PATH"
    exit 1
fi

# --- main loop --------------------------------------------------------------
log "guard started — $PYTHON $SERVER"
log "restart delay: ${RESTART_DELAY}s  (Ctrl+C to stop)"

ATTEMPT=0
while [ "$RUNNING" -eq 1 ]; do
    ATTEMPT=$((ATTEMPT + 1))
    log "starting reader server (attempt #${ATTEMPT})…"

    "$PYTHON" "$SERVER" &
    CHILD_PID=$!

    wait "$CHILD_PID" 2>/dev/null
    EXIT_CODE=$?
    CHILD_PID=""

    if [ "$RUNNING" -eq 0 ]; then
        break
    fi

    log "reader server exited (code=${EXIT_CODE})"
    log "restarting in ${RESTART_DELAY}s…"
    sleep "$RESTART_DELAY"
done

log "guard stopped."
