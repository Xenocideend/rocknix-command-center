#!/bin/sh
# td-mark.sh <step-id> <begin|snap|end> [note...]
# Test-day logging (owner: "logging on each step"). Writes a marker line into
# the rp5deck logs so every later line can be attributed to a step, and
# snapshots the evidence for that step into /storage/rp5deck-testlog/<step>/:
# state.json, the sway tree (names/outputs/focus only), the flag, touch
# send_events, pids, and the tails of the three rp5deck logs.
# Read-only apart from the marker lines and the snapshot directory.
set -u
step=${1:?step id}; phase=${2:-snap}; shift 2 2>/dev/null; note="$*"
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/var/run/0-runtime-dir}
export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-1}
L=/storage/rp5deck/log
D=/storage/rp5deck-testlog/$step
mkdir -p "$D"
ts=$(date +%H:%M:%S)
line="=== TESTDAY $step $phase $ts${note:+ - $note} ==="
for f in "$L/rp5deck.log" "$L/command-center-app.log" "$L/rp5deck-overlay.log"; do
    [ -f "$f" ] && echo "$line" >> "$f"
done
echo "$line" >> /storage/rp5deck-testlog/INDEX.txt
S="$D/$phase-$ts"
mkdir -p "$S"
cp /run/rp5deck/state.json "$S/state.json" 2>/dev/null
SWAYSOCK=$(find /run /tmp -name 'sway-ipc.*.sock' 2>/dev/null | head -1)
export SWAYSOCK
swaymsg -t get_tree -r > "$S/tree.json" 2>/dev/null
python3 -B - "$S/tree.json" > "$S/windows.txt" 2>&1 <<'PY'
import json, sys
def walk(n, out=None):
    for c in n.get("nodes", []) + n.get("floating_nodes", []):
        if c.get("type") == "output":
            out = c.get("name")
        if c.get("pid") and c.get("name") is not None:
            print(out, c.get("app_id") or c.get("window_properties", {}).get("class"),
                  repr(c.get("name")), "FOCUSED" if c.get("focused") else "")
        walk(c, out)
try:
    walk(json.load(open(sys.argv[1])))
except Exception as e:
    print("tree unreadable:", e)
PY
{
    echo "flag: $(cat /storage/.config/profile.d/080-dual_screen_mode 2>/dev/null)"
    for t in "0:0:generic_ft5x06_(a0)" "8746:1:RetroidPocket_RDS_Touchscreen"; do
        echo "touch $t: $(swaymsg -t get_inputs -r 2>/dev/null | python3 -B -c "
import json,sys
for i in json.load(sys.stdin):
    if i.get('identifier') == sys.argv[1]:
        print(i.get('libinput', {}).get('send_events'), 'mapped:', i.get('mapped_to_output', '?'))
" "$t" 2>/dev/null)"
    done
    echo "ES running game: $(curl -s -m 2 http://127.0.0.1:1234/runningGame 2>/dev/null)"
    echo "battery: $(cat /sys/class/power_supply/battery/capacity 2>/dev/null)%"
} > "$S/facts.txt"
for f in rp5deck.log command-center-app.log rp5deck-overlay.log; do
    [ -f "$L/$f" ] && tail -n 60 "$L/$f" > "$S/$f.tail"
done
echo "$line"
cat "$S/facts.txt"
echo "--- windows ---"; cat "$S/windows.txt"
echo "snapshot: $S"
