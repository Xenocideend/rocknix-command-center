#!/bin/sh
# Read-only report of what the Command Center needs to know about this handheld: model, outputs, touchscreens, seats,
# backlights, the sway lines ROCKNIX wrote and which profile the app picked. Changes nothing on the device. Serial numbers
# and MAC addresses are blanked. Run it over SSH or from the ES terminal, then send the text it prints (also saved in
# /storage/rp5deck-probe.txt):
#
#     sh /storage/rp5deck/tools/device-probe.sh

OUT=${PROBE_OUT:-/storage/rp5deck-probe.txt}
APP=$(dirname "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")")
[ -f "$APP/screen_map.py" ] || APP=/storage/rp5deck

. /etc/profile >/dev/null 2>&1

report() {
    echo "== Command Center device probe =="
    echo "date: $(date '+%Y-%m-%d %H:%M')"
    echo "model: $(tr -d '\0' < /proc/device-tree/model 2>/dev/null || cat /sys/class/dmi/id/product_name 2>/dev/null)"
    echo "quirk device: ${QUIRK_DEVICE:-unknown}"
    echo "dual screen flag: ${DEVICE_HAS_DUAL_SCREEN:-unset}"
    grep -E '^(NAME|VERSION|BUILD_ID)=' /etc/os-release 2>/dev/null
    echo "kernel: $(uname -r)"
    echo "command center: $(grep -E '^VERSION' "$APP/version.py" 2>/dev/null)"

    echo
    echo "== profile the app picked =="
    (cd "$APP" 2>/dev/null && python3 screen_map.py 2>&1)
    (cd "$APP" 2>/dev/null && python3 screen_map.py --env 2>&1)

    echo
    echo "== DRM connectors =="
    for c in /sys/class/drm/card*-*; do
        [ -e "$c/status" ] && echo "$(basename "$c"): $(cat "$c/status" 2>/dev/null) $(cat "$c/enabled" 2>/dev/null)"
    done

    echo
    echo "== sway outputs =="
    swaymsg -r -t get_outputs 2>/dev/null | python3 -c '
import json, sys
try:
    outs = json.load(sys.stdin)
except Exception:
    print("sway did not answer"); sys.exit()
for o in outs:
    r = o.get("rect") or {}
    m = o.get("current_mode") or {}
    print("%s: active=%s power=%s transform=%s scale=%s rect=%sx%s+%s+%s mode=%sx%s@%s make=%s model=%s focused=%s" % (
        o.get("name"), o.get("active"), o.get("power"), o.get("transform"), o.get("scale"), r.get("width"), r.get("height"),
        r.get("x"), r.get("y"), m.get("width"), m.get("height"), m.get("refresh"), o.get("make"), o.get("model"),
        o.get("focused")))
'

    echo
    echo "== sway inputs (touch and keyboards) =="
    swaymsg -r -t get_inputs 2>/dev/null | python3 -c '
import json, sys
try:
    ins = json.load(sys.stdin)
except Exception:
    print("sway did not answer"); sys.exit()
for i in ins:
    if i.get("type") in ("touch", "keyboard", "pointer", "switch", "tablet_tool"):
        lib = i.get("libinput") or {}
        print("%s | %s | %s | send_events=%s" % (i.get("identifier"), i.get("name"), i.get("type"), lib.get("send_events")))
'

    echo
    echo "== sway seats =="
    swaymsg -r -t get_seats 2>/dev/null | python3 -c '
import json, sys
try:
    seats = json.load(sys.stdin)
except Exception:
    print("sway did not answer"); sys.exit()
for s in seats:
    print("%s: devices=%s" % (s.get("name"), [d.get("identifier") for d in s.get("devices", [])
                                              if d.get("type") in ("touch", "pointer")]))
'

    echo
    echo "== windows (app id, title, output, workspace) =="
    swaymsg -r -t get_tree 2>/dev/null | python3 -c '
import json, sys
try:
    t = json.load(sys.stdin)
except Exception:
    print("sway did not answer"); sys.exit()
def walk(n, out="", ws=""):
    if n.get("type") == "output":
        out = n.get("name", "")
    if n.get("type") == "workspace":
        ws = n.get("name", "")
    if n.get("pid") and (n.get("app_id") or (n.get("window_properties") or {}).get("class")):
        print("%s | %s | %s | ws %s" % (n.get("app_id") or n["window_properties"].get("class"), (n.get("name") or "")[:60], out, ws))
    for c in n.get("nodes", []) + n.get("floating_nodes", []):
        walk(c, out, ws)
walk(t)
'

    echo
    echo "== backlights =="
    for b in /sys/class/backlight/*; do
        [ -d "$b" ] && echo "$(basename "$b"): $(cat "$b/brightness" 2>/dev/null)/$(cat "$b/max_brightness" 2>/dev/null)"
    done

    echo
    echo "== power supplies =="
    ls /sys/class/power_supply 2>/dev/null | tr '\n' ' '
    echo

    echo
    echo "== sway config lines about screens, touch and seats =="
    grep -E 'map_to_output|calibration|power (on|off)|events (enabled|disabled)|seat |focus output|lowerdeck|for_window \[title' \
        /storage/.config/sway/config 2>/dev/null | cut -c1-200

    echo
    echo "== related processes =="
    for p in lowerdeck wvkbd-mobintl output_monitor emulationstation rp5deck; do
        printf '%s: ' "$p"
        pgrep -f "$p" 2>/dev/null | head -3 | tr '\n' ' '
        echo
    done
}

report 2>&1 | sed -E 's/([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}/xx:xx:xx:xx:xx:xx/g; s/[Ss]erial[^|,]*/serial removed/g' | tee "$OUT"
echo
echo "saved to $OUT"
