#!/bin/sh
# Guided test of the Command Center on a handheld that is not the Retroid Pocket 5. It asks you to look at the screens and
# answer a few questions, collects what the device reports, and writes everything to one text file you can send back.
#
#     sh /storage/rp5deck/tools/tester-run.sh                 guided, about 10 minutes
#     sh /storage/rp5deck/tools/tester-run.sh --auto          collect only, asks nothing, installs nothing
#     sh /storage/rp5deck/tools/tester-run.sh --scripts DIR   where install-layout-daemon.sh and the two daemons are
#
# The log is /storage/rp5deck-test-log-DATE-TIME.txt. What goes into it: the model, the screens, touch screens and seats sway
# reports, brightness controls, the app's and the layout helper's log lines (game and ROM names, serial numbers, MAC and IP
# addresses are removed), and your answers. No passwords, sign-ins, saves or settings files. Read it before you send it.

APP=${RP5DECK_HOME:-/storage/rp5deck}
LOG_DIR=${TESTER_LOG_DIR:-/storage}
BL_DIR=${TESTER_BACKLIGHT_DIR:-/sys/class/backlight}
AUTOSTART=${INSTALL_AUTOSTART:-/storage/.config/autostart}
TS=$(date +%Y%m%d-%H%M%S)
LOG="$LOG_DIR/rp5deck-test-log-$TS.txt"
TMP=${TMPDIR:-/tmp}/rp5deck-tester.$$
MODE=guided
SCRIPTS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --auto) MODE=auto ;;
        --scripts) shift; SCRIPTS=${1:-} ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "usage: $0 [--auto] [--scripts DIR]"; exit 2 ;;
    esac
    shift
done

mkdir -p "$TMP" || exit 1
trap 'rm -rf "$TMP"' EXIT

redact() {
    sed -E 's/([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}/xx:xx:xx:xx:xx:xx/g; s/[Ss]erial[^|,]*/serial removed/g; s/[0-9]{1,3}(\.[0-9]{1,3}){3}/x.x.x.x/g'
}

say() { echo "$*"; echo "$*" | redact >> "$LOG"; }
section() { say ""; say "== $1 =="; }
capture() { "$@" 2>&1 | redact >> "$LOG"; }

# the app's log without game names, ROM paths or the name-guard's list of ROM titles
clean_log() {
    grep -v -E 'name guard|game-start|gamelist|no manual found|ES game' \
        | sed -E "s#/storage/roms/[^ ']*#/storage/roms/...#g; s#rom='[^']*'#rom='...'#g; s#name='[^']*'#name='...'#g"
}

wait_user() {
    if [ -n "${TESTER_HOOK:-}" ]; then sh "$TESTER_HOOK"; return 0; fi
    [ "$MODE" = auto ] && return 0
    echo
    echo "$1"
    printf 'Press Enter when done. '
    read -r _ || true
}

# ask ID "question": yes / no / skip and a note, logged as  ANSWER ID = yes | note: ...
ask() {
    id=$1
    if [ "$MODE" = auto ]; then
        echo "ANSWER $id = skipped (--auto)" >> "$LOG"
        return 0
    fi
    echo
    echo "$2"
    ans=""
    n=0
    while [ "$n" -lt 3 ]; do
        printf '  [y]es / [n]o / [s]kip: '
        read -r ans || ans=s
        case "$ans" in
            y|Y|yes) ans=yes; break ;;
            n|N|no) ans=no; break ;;
            s|S|skip|"") ans=skip; break ;;
        esac
        ans=""
        n=$((n + 1))
    done
    [ -n "$ans" ] || ans=skip
    printf '  Note, if there is anything to add (Enter for none): '
    read -r note || note=""
    echo "ANSWER $id = $ans | note: $note" | redact >> "$LOG"
}

confirm() {
    [ "$MODE" = auto ] && return 1
    echo
    echo "$1"
    printf '  [y]es / [N]o: '
    read -r a || a=n
    case "$a" in y|Y|yes) return 0 ;; esac
    return 1
}

# "name raw max" per backlight, for comparing before and after
bl_snapshot() {
    for d in "$BL_DIR"/*; do
        [ -d "$d" ] || continue
        echo "$(basename "$d") $(cat "$d/brightness" 2>/dev/null) $(cat "$d/max_brightness" 2>/dev/null)"
    done > "$1"
}
bl_changed() {
    c=$(grep -v -x -F -f "$1" "$2" | cut -d' ' -f1 | tr '\n' ' ')
    [ -n "$c" ] && echo "$c" || echo "nothing changed"
}

find_scripts() {
    for d in "$SCRIPTS" "$APP/../scripts" /storage/scripts /storage/rp5deck-scripts; do
        [ -n "$d" ] && [ -f "$d/install-layout-daemon.sh" ] && { echo "$d"; return 0; }
    done
    return 1
}

TOOLS="$APP/tools"
: > "$LOG" || { echo "cannot write $LOG"; exit 1; }
say "Command Center tester log, $MODE run, started $(date '+%Y-%m-%d %H:%M')"
say "app: $(grep -E '^VERSION' "$APP/version.py" 2>/dev/null)   uptime: $(uptime 2>/dev/null | sed 's/^ *//')"
say "(game names, serial numbers, MAC and IP addresses are removed from this file)"

section "1. what the device reports (before anything is changed)"
if [ -f "$TOOLS/device-probe.sh" ]; then
    PROBE_OUT=/dev/null capture sh "$TOOLS/device-probe.sh"
else
    say "tools/device-probe.sh is missing from $APP"
fi

section "2. what the app picked for this device"
(cd "$APP" 2>/dev/null && capture python3 screen_map.py)
(cd "$APP" 2>/dev/null && capture python3 screen_map.py --kind)
(cd "$APP" 2>/dev/null && capture python3 screen_map.py --env)
(cd "$APP" 2>/dev/null && capture python3 screen_map.py --backlights)
say "backlights now (name raw max): $(bl_snapshot "$TMP/bl0"; tr '\n' ';' < "$TMP/bl0")"

section "3. the layout helper"
say "autostart: $(ls "$AUTOSTART" 2>/dev/null | grep -E 'dual-screen|command-center' | tr '\n' ' ')"
if [ -x "$AUTOSTART/dual-screen-builtin-layout" ]; then
    capture bash "$AUTOSTART/dual-screen-builtin-layout" --status
    say "helper log:"
    tail -n 40 "$AUTOSTART/dual-screen-builtin-layout.log" 2>/dev/null | redact >> "$LOG"
else
    say "the built-in-screens helper is not installed"
fi

if sd=$(find_scripts) && confirm "Install the layout helper now? It keeps the bottom screen on, turns its touch on if ROCKNIX left it off, and switches ROCKNIX's own bottom-screen app off (it can be switched back on: see the guide). (the scripts are in $sd)"; then
    section "4. installing the layout helper"
    capture sh "$sd/install-layout-daemon.sh"
    say "waiting 15 seconds for it to settle"
    sleep 15
    say "helper status after the install:"
    [ -x "$AUTOSTART/dual-screen-builtin-layout" ] && capture bash "$AUTOSTART/dual-screen-builtin-layout" --status
    tail -n 30 "$AUTOSTART/dual-screen-builtin-layout.log" 2>/dev/null | redact >> "$LOG"
else
    section "4. installing the layout helper"
    say "not installed in this run (answered no, --auto, or install-layout-daemon.sh not found: use --scripts DIR)"
fi

section "5. your answers"
if [ "$MODE" = guided ]; then
    echo
    echo "Look at the two screens. The Command Center should be on the BOTTOM screen (the one ROCKNIX's own bottom-screen app"
    echo "would use) and EmulationStation on the other one. Answer for what you see now."
fi
ask command_center_visible "Is the Command Center (the grid of tiles with a tab strip at the top) shown on the second screen?"
ask touch_works "Tap a tile on the Command Center, then open Settings and drag a slider. Does it react?"
ask touch_position "Does it react exactly where you touch (not mirrored, turned round or off to one side)?"
ask es_other_screen "Is EmulationStation on the other screen, and can you use it with the buttons?"
ask tiles_hidden "Are the tiles 'Swap screens' and 'Top screen' missing from the Command Center's Home?"
ask keyboard "Tap the Keyboard tile. Does the on-screen keyboard appear, and is there a down arrow button to close it?"
ask game_lowerdeck "Start any RetroArch game. Does ROCKNIX's bottom-screen app stay away (the Command Center stays on the bottom)?"
ask ds_game "Start a Nintendo DS game if you have one. Does the game's second screen show where you expect?"

section "6. brightness: which backlight does which slider move?"
bl_snapshot "$TMP/a1"
say "before: $(tr '\n' ';' < "$TMP/a1")"
wait_user "Open Settings > Screens and drag 'Top screen brightness' to its LOWEST value (watch which screen gets darker)."
bl_snapshot "$TMP/a2"
say "after the top slider: $(tr '\n' ';' < "$TMP/a2")"
say "BACKLIGHT MOVED BY THE TOP SLIDER: $(bl_changed "$TMP/a1" "$TMP/a2")"
ask top_slider_dims_top "Did the TOP screen get darker (and not the bottom one)?"
wait_user "Put 'Top screen brightness' back up, then drag 'Bottom screen brightness' to its LOWEST value."
bl_snapshot "$TMP/a3"
say "after the bottom slider: $(tr '\n' ';' < "$TMP/a3")"
say "BACKLIGHT MOVED BY THE BOTTOM SLIDER: $(bl_changed "$TMP/a2" "$TMP/a3")"
ask bottom_slider_dims_bottom "Did the BOTTOM screen get darker (and not the top one)?"
wait_user "Put 'Bottom screen brightness' back to where you like it."
ask anything_else "Anything else that looked wrong or surprised you? (use the note)"

section "7. logs (cleaned)"
say "app log, last lines:"
[ -f "$APP/log/rp5deck.log" ] && tail -n 300 "$APP/log/rp5deck.log" | clean_log | redact >> "$LOG"
say "touch lines from the app log:"
grep -h -E 'touch devices|twin|touch' "$APP/log/rp5deck.log" 2>/dev/null | tail -n 20 | clean_log | redact >> "$LOG"
say "kernel messages about displays, panels and touch:"
dmesg 2>/dev/null | grep -i -E 'drm|panel|dsi|touch|backlight|goodix|ft5x' | tail -n 40 | redact >> "$LOG"

section "8. state after"
if [ -f "$TOOLS/device-probe.sh" ]; then
    PROBE_OUT=/dev/null capture sh "$TOOLS/device-probe.sh"
fi

say ""
say "Finished $(date '+%Y-%m-%d %H:%M')."
echo
echo "The log is: $LOG"
echo "Please read it, then send it. From a computer on the same network:"
echo "    scp root@<the handheld's address>:$LOG ."
echo "ANSWER lines are your answers. Thank you."
exit 0
