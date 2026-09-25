#!/bin/sh
# Stands in for main.py / focus_guard.py in tests/test_supervisor.py: a
# controllable child that 094-rp5deck's restart loops can start, restart,
# and signal. The test makes TWO independent copies of this one script (one
# named as the "app", one as the "guard") so each can be driven separately -
# behaviour is read FRESH, from control files next to THIS script's own
# path ($0), on every start, so the test can change what happens on the
# NEXT restart without touching 094-rp5deck or this script:
#
#   $0.starts        incremented (created if absent) on every start - the
#                     test's ground truth for "how many times was I
#                     (re)started".
#   $0.run_seconds    how long to sleep before exiting on this run (default:
#                     a large number - "keeps running").
#   $0.exit_code      exit code to use if the sleep completes (default 0).
#   $0.swaysock_log   APPENDED, one line per start, with this start's
#                      $SWAYSOCK (ST2: proves 094-rp5deck re-resolves and
#                      hands each respawn the CURRENT socket path, not a
#                      stale one captured before sway restarted).
#
# A TERM is handled immediately (exit 0), same as SIGTERM behaviour real
# main.py/focus_guard.py must have for a clean, prompt supervisor stop.
set -u

starts_file="$0.starts"
run_seconds_file="$0.run_seconds"
exit_code_file="$0.exit_code"
swaysock_log="$0.swaysock_log"

n=$(cat "$starts_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$starts_file"
echo "${SWAYSOCK:-}" >> "$swaysock_log"

# Kill our own sleep on the way out: a plain `exit 0` left it orphaned for
# up to an hour after every TERM (25 Sep 2026 leak - see test_supervisor).
sp=""
trap '[ -n "$sp" ] && kill "$sp" 2>/dev/null; exit 0' TERM INT

secs=3600
[ -f "$run_seconds_file" ] && secs=$(cat "$run_seconds_file")

sleep "$secs" &
sp=$!
wait "$sp" 2>/dev/null

rc=0
[ -f "$exit_code_file" ] && rc=$(cat "$exit_code_file")
exit "$rc"
