#!/bin/sh
# Stands in for `swaymsg` in tests/test_supervisor.py: 094-rp5deck only ever
# calls `swaymsg -t get_outputs`, to wait for DP-1 (the add-on) to appear.
# This stub answers immediately so the wait loop passes on its first try -
# unless SWAYMSG_NO_DP1 is set, in which case it reports an output list with
# no DP-1 at all (used to test the "still waiting" path).
if [ -n "${SWAYMSG_NO_DP1:-}" ]; then
    echo '[{"name": "DSI-1"}]'
else
    echo '[{"name": "DSI-1"}, {"name": "DP-1"}]'
fi
