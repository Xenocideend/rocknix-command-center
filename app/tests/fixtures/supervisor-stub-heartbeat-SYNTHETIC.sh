#!/bin/sh
# supervisor-stub-heartbeat-SYNTHETIC.sh - like supervisor-stub-child-
# SYNTHETIC.sh (starts counter, TERM handling) but ALSO writes its heartbeat
# file every second, for up to $0.heartbeat_stop_after SECONDS of its own run
# (default: never stops - a live heartbeat for the whole run), the same way
# the REAL main.py does (main.App._touch_heartbeat/heartbeat_path): under
# $RP5DECK_RUN_DIR, computed here rather than read from some 094-internal
# variable, since 094 never exports one - only RP5DECK_RUN_DIR itself. Used
# by test_supervisor.py to prove 094-rp5deck's hang detector actually fires:
# a run that stops heartbeating partway through, while the process itself
# stays alive (no exit, no TERM), must get SIGTERM'd (SIGKILL'd if that is
# not enough) by the supervisor's own heartbeat_monitor, show up as a fresh
# "start" through the ordinary restart path, and log "hung".
#
# Same control files as supervisor-stub-child-SYNTHETIC.sh:
#   $0.starts             incremented on every start.
#   $0.run_seconds        total seconds to run before exiting on its own
#                          (default: a large number - the hang detector is
#                          expected to end the run first).
#   $0.exit_code           exit code if the run completes on its own.
#   $0.heartbeat_stop_after   seconds of heartbeat-writing before going
#                          silent (default: never stops); read FRESH every
#                          iteration, so a test can arm it after the stub is
#                          already running.
set -u

starts_file="$0.starts"
run_seconds_file="$0.run_seconds"
exit_code_file="$0.exit_code"
stop_hb_file="$0.heartbeat_stop_after"
hb_path="${RP5DECK_RUN_DIR:-/run/rp5deck}/heartbeat"

n=$(cat "$starts_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$starts_file"

# kill our own sleep on the way out, as supervisor-stub-child-SYNTHETIC.sh
sp=""
trap '[ -n "$sp" ] && kill "$sp" 2>/dev/null; exit 0' TERM INT

secs=3600
[ -f "$run_seconds_file" ] && secs=$(cat "$run_seconds_file")

elapsed=0
while [ "$elapsed" -lt "$secs" ]; do
    # re-read every iteration (not just once at start): a test can arm this
    # AFTER the stub is already running, to stop an already-live heartbeat.
    stop_after=999999
    [ -f "$stop_hb_file" ] && stop_after=$(cat "$stop_hb_file" 2>/dev/null || echo 999999)
    if [ "$elapsed" -lt "$stop_after" ]; then
        mkdir -p "$(dirname "$hb_path")" 2>/dev/null
        date +%s > "$hb_path.tmp" 2>/dev/null && mv "$hb_path.tmp" "$hb_path" 2>/dev/null
    fi
    sleep 1 &
    sp=$!
    wait "$sp" 2>/dev/null
    elapsed=$((elapsed + 1))
done

rc=0
[ -f "$exit_code_file" ] && rc=$(cat "$exit_code_file")
exit "$rc"
