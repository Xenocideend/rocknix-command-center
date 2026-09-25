#!/bin/sh
# B1 device verification for rp5deck: (a) draws on DSI-1 only, (b) navigation,
# HUD live values, mixer, volume drag -> DRYRUN set_master + exactly one
# commit_master, (c) focus never leaves EmulationStation (with a control that
# must go red), (d) FULL/HIDDEN/BAR modes from real windows, (e) clean exits.
#
# Audio is ALWAYS dry-run here (env.sh refuses to continue otherwise).
. /storage/rp5deck-dev/b1/env.sh
cd $D || exit 1
ST="python3 $D/st.py"
OUT=$D/verify
rm -rf $OUT; mkdir -p $OUT
LOG=$D/log/rp5deck.log

app_procs && { echo "HARD STOP: an rp5deck app is already running"; exit 1; }
# Another agent shares /storage/rp5deck-dev: prove the deployed files are the
# ones just pushed, and that nothing else occupies DSI-1 before measuring.
( cd $APP && md5sum *.py ) | awk '{print $1, $2}' | LC_ALL=C sort -k2 > $OUT/deployed-md5.txt
awk '{sub(/^\*/, "", $2); print $1, $2}' $D/expected-md5.txt | LC_ALL=C sort -k2 > $OUT/expected-md5.txt
cmp -s $OUT/deployed-md5.txt $OUT/expected-md5.txt || { echo "HARD STOP: deployed files differ from expected"; diff $OUT/expected-md5.txt $OUT/deployed-md5.txt; exit 1; }
echo "deployed files: md5 match ($(wc -l < $OUT/deployed-md5.txt) files)"
PRE=$(swaymsg -t get_tree | $ST mode)
echo "pre-flight mode from the live tree: $PRE"
case "$PRE" in FULL*) ;; *) echo "HARD STOP: DSI-1 is not free (another agent's window?)"; exit 1;; esac
echo "device name: $(python3 $APP/device.py | head -1)"
[ -n "$(swaymsg -t get_tree | $ST apps rp5deck-test)" ] && { echo "HARD STOP: rp5deck-test windows exist"; exit 1; }

focus_id() { swaymsg -t get_tree | $ST focus; }
refocus_es() { swaymsg '[app_id="emulationstation"] focus' >/dev/null; sleep 0.6; }
CLICKS=$OUT/clicks.txt
click() {  # layout x y
    echo "click $1 $2" >> $CLICKS
    swaymsg seat - cursor set "$1" "$2" >/dev/null
    swaymsg seat - cursor press button1 >/dev/null
    swaymsg seat - cursor release button1 >/dev/null
    sleep 0.5
}
drag() {   # y x0 x1 x2 ... (press at x0, move through the rest, release at the last)
    echo "drag $*" >> $CLICKS
    y=$1; shift
    swaymsg seat - cursor set "$1" "$y" >/dev/null
    swaymsg seat - cursor press button1 >/dev/null
    shift
    for x in "$@"; do swaymsg seat - cursor set "$x" "$y" >/dev/null; sleep 0.03; done
    swaymsg seat - cursor release button1 >/dev/null
    sleep 0.6
}
click_t() { set -- $($ST target "$1"); click "$1" "$2"; }
state() { $ST get "$1"; }
mark() { wc -l < $LOG; }
since() { tail -n +$(( $1 + 1 )) $LOG; }

ES=$(swaymsg -t get_tree | $ST esid)
echo "EmulationStation con_id: $ES"
[ "$ES" != none ] || { echo "HARD STOP: ES window not found"; exit 1; }
refocus_es
echo "baseline focus: $(focus_id)"
echo "pactl subscribe processes before: $(ps -o args= | grep -c '^pactl subscribe')"

echo
echo "=================== APP RUN ==================="
: > $LOG
: > $CLICKS
refocus_es
f_pre=$(focus_id)
timeout 240 python3 $APP/main.py --seconds 220 > $OUT/app.out 2>&1 &
TPID=$!
echo "started: wait for FULL + first frame: $($ST wait 's["mode"]=="FULL" and s["counts"]["presents"]>=1' 10) s"
sleep 1.5
echo "focus before start: $f_pre"
echo "focus after start : $(focus_id)"
APID=$(app_procs | awk '{print $1}' | tail -1)
echo "app python pid: $APID (timeout wrapper $TPID)"
echo "state title: $(state title)"
echo "state config: $(state config)"

echo
echo "--- (a) draws on DSI-1 only ---"
grim -t ppm -o DSI-1 $OUT/a_dsi.ppm; grim -t ppm -o DP-1 $OUT/a_dp.ppm; grim -o DSI-1 $OUT/home.png
echo "(a) DSI-1 bg 18,20,28: $($ST count $OUT/a_dsi.ppm 18,20,28)"
echo "(a) DP-1  bg 18,20,28: $($ST count $OUT/a_dp.ppm 18,20,28)"
echo "(a) DSI-1 accent 61,139,253: $($ST count $OUT/a_dsi.ppm 61,139,253)"
echo "(a) DP-1  accent 61,139,253: $($ST count $OUT/a_dp.ppm 61,139,253)"

echo
echo "--- (b)+(c) navigation, focus watched ---"
refocus_es
before=$(focus_id)
timeout 120 swaymsg -r -t subscribe -m '["window","workspace"]' > $OUT/run_events.log 2>&1 &
SUBPID=$!; sleep 0.5
M0=$(mark)

click_t home.hud
echo "HUD: sheet=$(state sheet) after $($ST wait 's["sheet"]=="hud"' 2) s; focus $(focus_id)"
u1=$(state hud.last.uptime_s); n1=$(state hud.samples)
sleep 3.2
u2=$(state hud.last.uptime_s); n2=$(state hud.samples)
echo "HUD live: samples $n1 -> $n2, uptime_s $u1 -> $u2, battery $(state hud.last.battery_percent)%, gpu_mhz $(state hud.last.gpu_mhz), gpu_load $(state hud.last.gpu_load_percent), cpu_mhz $(state hud.cpu_mhz)"
grim -o DSI-1 $OUT/hud.png
click_t hud.back
echo "back: sheet=$(state sheet) screen=$(state screen); focus $(focus_id)"
n3=$(state hud.samples); sleep 2.2; n4=$(state hud.samples)
echo "HUD closed: samples $n3 -> $n4 (must not grow)"

click_t bar.battery
echo "battery tap: sheet=$(state sheet) after $($ST wait 's["sheet"]=="hud"' 2) s; focus $(focus_id)"
click_t hud.back
echo "back: screen=$(state screen)"

click_t home.mixer
echo "mixer: $($ST wait 's["sheet"]=="mixer" and s["mixer"]["streams"]!="loading"' 8) s; streams=$(state mixer.streams) message=$(state mixer.message); focus $(focus_id)"
grim -o DSI-1 $OUT/mixer.png
click_t mixer.back
echo "back: screen=$(state screen)"

click_t home.browser
echo "browser tile: screen=$(state screen); focus $(focus_id)"
grim -o DSI-1 $OUT/soon.png
click_t soon.back
click_t home.youtube
echo "youtube tile: screen=$(state screen)"
click_t soon.back
echo "back: screen=$(state screen)"

echo
echo "--- volume drag 0.30 -> 0.60 (DRYRUN) ---"
echo "slider before: ui_percent=$(state volume.ui_percent) real master=$(state volume.master)"
set -- $($ST track bar.slider 0.30); Y=$2; X30=$1
X35=$($ST track bar.slider 0.35 | cut -d' ' -f1); X40=$($ST track bar.slider 0.40 | cut -d' ' -f1)
X45=$($ST track bar.slider 0.45 | cut -d' ' -f1); X50=$($ST track bar.slider 0.50 | cut -d' ' -f1)
X55=$($ST track bar.slider 0.55 | cut -d' ' -f1); X60=$($ST track bar.slider 0.60 | cut -d' ' -f1)
M1=$(mark)
drag $Y $X30 $X35 $X40 $X45 $X50 $X55 $X60
since $M1 | grep -E 'DRYRUN|volume released|input (down|up)' > $OUT/drag.txt
cat $OUT/drag.txt
echo "set_master lines: $(grep -c 'DRYRUN set_master' $OUT/drag.txt)"
echo "commit_master lines: $(grep -c 'DRYRUN commit_master' $OUT/drag.txt)"
echo "state after drag: ui_percent=$(state volume.ui_percent) last_set=$(state volume.last_set) last_commit=$(state volume.last_commit) readout=$(state volume.readout) dragging=$(state volume.dragging)"
echo "focus after drag: $(focus_id)"

echo "(sway delivers no pointer motion to the surface during a synthesised press: compare the"
echo " press and release coordinates above - the release arrives where the press was)"

echo
echo "--- volume drag 0.30 -> 0.60 as FINGER events injected into the app's own SDL queue ---"
M4=$(mark)
kill -USR1 $APID
echo "commit seen after $($ST wait 's["volume"]["last_commit"]==0.6 and not s["volume"]["dragging"]' 5) s"
sleep 0.4
since $M4 | grep -E 'DEBUG|DRYRUN|volume released|input (down|up)' > $OUT/drag2.txt
cat $OUT/drag2.txt
python3 - $OUT/drag2.txt <<'PY'
import re, sys
t = open(sys.argv[1]).read()
def secs(line):
    h, m, s = line.split()[1].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)
sets = [(secs(l), float(re.search(r"set_master\(([0-9.]+)\)", l).group(1)))
        for l in t.splitlines() if "DRYRUN set_master" in l]
commits = [float(x) for x in re.findall(r"DRYRUN commit_master\(([0-9.]+)\)", t)]
vals = [v for _, v in sets]
gaps = [round(b[0] - a[0], 3) for a, b in zip(sets, sets[1:])]
print("self-test set_master count:", len(vals), "values:", vals)
print("self-test strictly increasing:", all(a < b for a, b in zip(vals, vals[1:])),
      " all within [0.30, 0.60]:", all(0.30 <= v <= 0.60 for v in vals))
print("self-test min gap between sends: %s s (throttle 0.05 s)" % (min(gaps) if gaps else None))
print("self-test commit_master count:", len(commits), "values:", commits)
PY
echo "state after self-test drag: ui_percent=$(state volume.ui_percent) last_commit=$(state volume.last_commit) last_input=$(state last_input)"
echo "focus after self-test drag: $(focus_id)"

echo
echo "--- mute toggle twice (DRYRUN) ---"
M2=$(mark)
click_t bar.mute
echo "ui_muted=$(state volume.ui_muted)"
click_t bar.mute
echo "ui_muted=$(state volume.ui_muted)"
since $M2 | grep 'DRYRUN toggle_master_mute'
echo "toggle lines: $(since $M2 | grep -c 'DRYRUN toggle_master_mute')"

after=$(focus_id)
sleep 0.3
kill $SUBPID 2>/dev/null
echo
echo "(c) focus before interaction: $before"
echo "(c) focus after  interaction: $after"
echo "(c) sway focus events during interaction: $(grep -c '"change": *"focus"' $OUT/run_events.log)"
echo "(c) app input downs so far: $(state counts.input_down)"
echo "(c) non-DRYRUN audio setter calls in log: $(since $M0 | grep -E 'commit_master\(.*-> /usr/bin/volume [0-9]+: ' | grep -vc DRYRUN)"

echo
echo "--- (d) modes ---"
refocus_es
foot --app-id rp5deck-test-other >/dev/null 2>&1 &
FOOT1=$!
n=0; while [ "$(swaymsg -t get_tree | $ST app rp5deck-test-other)" = absent ]; do sleep 0.2; n=$((n+1)); [ $n -gt 50 ] && break; done
echo "foot other mapped at: $(swaymsg -t get_tree | $ST app rp5deck-test-other)"
sleep 1.5
echo "mode with foot on DP-1 (must stay FULL): $(state mode)"
swaymsg '[app_id="rp5deck-test-other"] move container to output DSI-1' >/dev/null
echo "moved: now at $(swaymsg -t get_tree | $ST app rp5deck-test-other)"
echo "-> HIDDEN after $($ST wait 's["mode"]=="HIDDEN"' 5) s; reason: $(state mode_reason); surface hidden=$(state surface.hidden)"
swaymsg -t get_tree > $OUT/sway-tree-HIDDEN-real-capture-2026-09-23.json
refocus_es
sleep 0.5
grim -t ppm -o DSI-1 $OUT/d_hidden.ppm; grim -o DSI-1 $OUT/hidden.png
echo "(d) HIDDEN: DSI-1 bg 18,20,28: $($ST count $OUT/d_hidden.ppm 18,20,28)"
IN0=$(state counts.input_down)
click 960 1080
echo "(d) click on DSI-1 while HIDDEN: app input downs $IN0 -> $(state counts.input_down); focus now $(focus_id) (expected: the foot window)"
refocus_es
swaymsg '[app_id="rp5deck-test-other"] kill' >/dev/null
echo "closed foot other -> FULL after $($ST wait 's["mode"]=="FULL" and s["surface"]["can_present"]' 5) s; size $(state surface.size)"
sleep 0.7
grim -t ppm -o DSI-1 $OUT/d_full2.ppm
echo "(d) back to FULL: DSI-1 bg 18,20,28: $($ST count $OUT/d_full2.ppm 18,20,28)"
swaymsg -t get_tree > $OUT/sway-tree-FULL-real-capture-after-other-2026-09-23.json
refocus_es

foot --app-id rp5deck-test-bar >/dev/null 2>&1 &
FOOT2=$!
n=0; while [ "$(swaymsg -t get_tree | $ST app rp5deck-test-bar)" = absent ]; do sleep 0.2; n=$((n+1)); [ $n -gt 50 ] && break; done
swaymsg '[app_id="rp5deck-test-bar"] move container to output DSI-1' >/dev/null
echo "-> BAR after $($ST wait 's["mode"]=="BAR" and s["surface"]["size"][1]==140 and s["counts"]["presents"]>0' 5) s; reason: $(state mode_reason)"
sleep 0.8
echo "(d) BAR surface size $(state surface.size) geometry $(state surface.geometry)"
echo "(d) foot window rect in BAR mode: $(swaymsg -t get_tree | $ST app rp5deck-test-bar)  (exclusive zone -> height 1080-140=940)"
swaymsg -t get_tree > $OUT/sway-tree-BAR-real-capture-2026-09-23.json
refocus_es
sleep 0.4
grim -t ppm -o DSI-1 $OUT/d_bar.ppm; grim -o DSI-1 $OUT/bar.png
echo "(d) BAR: bottom 140 rows bg-bar 27,30,39: $($ST count $OUT/d_bar.ppm 27,30,39 940 1080)"
echo "(d) BAR: top 940 rows   bg-bar 27,30,39: $($ST count $OUT/d_bar.ppm 27,30,39 0 940)"
echo "(d) BAR: top 940 rows   bg     18,20,28: $($ST count $OUT/d_bar.ppm 18,20,28 0 940)"
f1=$(focus_id); M3=$(mark)
click_t bar.mute
echo "(d) tap on the bar in BAR mode: focus $f1 -> $(focus_id); log: $(since $M3 | grep -E 'input down|DRYRUN' | tr '\n' ' ')"
click_t bar.mute
swaymsg '[app_id="rp5deck-test-bar"] kill' >/dev/null
echo "closed foot bar -> FULL after $($ST wait 's["mode"]=="FULL" and s["surface"]["size"][1]==1080' 5) s"
sleep 0.7
grim -t ppm -o DSI-1 $OUT/d_full3.ppm
echo "(d) FULL again: DSI-1 bg 18,20,28: $($ST count $OUT/d_full3.ppm 18,20,28)"
refocus_es
echo "mode transitions in log:"; grep -E 'MODE ' $LOG

echo
echo "--- (e) SIGTERM ---"
T0=$(date +%s)
kill -TERM $APID
wait $TPID; RC=$?
echo "(e) SIGTERM -> wrapper exit $RC after $(( $(date +%s) - T0 )) s"
tail -4 $LOG
sleep 0.5
echo "(e) leftover app: $(app_procs || echo none)"
echo "(e) pactl subscribe processes after: $(ps -o args= | grep -c '^pactl subscribe')"
echo "(e) rp5deck-test windows left: $(swaymsg -t get_tree | $ST apps rp5deck-test | tr '\n' ' ')"
echo "(e) foot processes left: $(ps -o pid= -o args= | grep 'foot --app-id rp5deck' | grep -v grep || echo none)"
echo "(e) final state running=$(state running) stop_reason=$(state stop_reason)"

echo
echo "--- (e) hard timeout (timeout 6, no --seconds) ---"
: > $LOG
timeout 6 python3 $APP/main.py > $OUT/app2.out 2>&1; RC=$?
echo "(e) timeout exit $RC"; grep -E 'exiting|clean exit|exit code|Traceback' $LOG
sleep 0.5
echo "(e) leftover app: $(app_procs || echo none); pactl subscribe: $(ps -o args= | grep -c '^pactl subscribe')"

echo
echo "=================== CONTROL (app NOT running), same clicks ==================="
grim -t ppm -o DSI-1 $OUT/ctl_dsi.ppm
echo "(a) control DSI-1 bg 18,20,28: $($ST count $OUT/ctl_dsi.ppm 18,20,28)"
refocus_es
cbefore=$(focus_id)
timeout 60 swaymsg -r -t subscribe -m '["window","workspace"]' > $OUT/ctl_events.log 2>&1 &
SUBPID=$!; sleep 0.5
head -13 $CLICKS > $OUT/ctl_clicks.txt
while read kind a b rest; do
    case $kind in
        click) swaymsg seat - cursor set "$a" "$b" >/dev/null; swaymsg seat - cursor press button1 >/dev/null
               swaymsg seat - cursor release button1 >/dev/null; sleep 0.5
               echo "  control click $a,$b -> focus $(focus_id)";;
        drag)  set -- $a $b $rest; y=$1; shift
               swaymsg seat - cursor set "$1" "$y" >/dev/null; swaymsg seat - cursor press button1 >/dev/null; shift
               for x in "$@"; do swaymsg seat - cursor set "$x" "$y" >/dev/null; sleep 0.03; done
               swaymsg seat - cursor release button1 >/dev/null; sleep 0.6
               echo "  control drag at y=$y -> focus $(focus_id)";;
    esac
done < $OUT/ctl_clicks.txt
cafter=$(focus_id)
kill $SUBPID 2>/dev/null
echo "(c) control focus before: $cbefore"
echo "(c) control focus after : $cafter"
echo "(c) control sway focus events: $(grep -c '"change": *"focus"' $OUT/ctl_events.log)"
case "$cafter" in "$ES "*) echo "CONTROL INVALID: focus did not move";; *) echo "control valid: focus moved to [$cafter]";; esac
refocus_es
echo "final focus: $(focus_id)"
echo "final leftover app: $(app_procs || echo none)"
echo "clicks replayed: $(wc -l < $OUT/ctl_clicks.txt) of $(wc -l < $CLICKS) recorded"
