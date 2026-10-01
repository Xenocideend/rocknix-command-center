#!/bin/sh
# Turn the Dual Screen add-on's display off or on without unplugging it.
#
# Off: dual-screen-layout-and-power disables DP-1, so the add-on's bridge gets no
# DisplayPort input and drops into its own power-saving mode; ES, games and
# emulator second windows all move to the built-in panel (as when undocked).
# On: DP-1 comes back and ES returns to the top screen. Unplugging the add-on
# also turns it back on for the next attach.
#
# Usage:
#   top-screen.sh            show state
#   top-screen.sh off|on|toggle
# Lives in the rp5deck app tree, so every Command Center deploy carries it
# (/storage/rp5deck/top-screen.sh); the ES entry "Top Screen On-Off"
# (roms/ports) runs `toggle`.
FLAG=/storage/top-screen-off

state() { [ -f "$FLAG" ] && echo off || echo on; }

case "${1:-status}" in
    off)    : > "$FLAG" ;;
    on)     rm -f "$FLAG" ;;
    toggle) if [ -f "$FLAG" ]; then rm -f "$FLAG"; else : > "$FLAG"; fi ;;
    status) ;;
    *) echo "usage: $0 [off|on|toggle|status]"; exit 2 ;;
esac
echo "top screen: $(state)  (092 applies it within ~5-10 s)"
echo "DP-1: $(cat /sys/class/drm/card0-DP-1/status 2>/dev/null || echo absent)"
