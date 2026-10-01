#!/bin/sh
# Installs the layout daemon this device needs, from the folder this script is in:
#
#   a Retroid Pocket with the Dual Screen add-on  -> dual-screen-layout-and-power
#   a handheld with two built-in panels            -> dual-screen-builtin-layout
#
# Which one comes from the Command Center's own device profile (python3 /storage/rp5deck/screen_map.py --kind), so the app has to
# be installed first. The other kind of daemon is never left installed next to it (it is stopped and moved aside), and the
# device-profile.env both read is written if it is missing. Safe to re-run: what it replaces is backed up first.
#
#     sh install-layout-daemon.sh            install and start
#     sh install-layout-daemon.sh --no-start install only
#     sh install-layout-daemon.sh --kind     print the kind and exit
#
# For tests and odd layouts: INSTALL_AUTOSTART (default /storage/.config/autostart), INSTALL_BACKUP_DIR (default /storage)
# and RP5DECK_HOME (default /storage/rp5deck).
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AUTOSTART=${INSTALL_AUTOSTART:-/storage/.config/autostart}
APP=${RP5DECK_HOME:-/storage/rp5deck}
TS=$(date +%Y%m%d-%H%M%S)
BK=${INSTALL_BACKUP_DIR:-/storage}
START=1
case "${1:-}" in
    --no-start) START=0 ;;
    --kind) ;;
    "") ;;
    *) echo "usage: $0 [--no-start | --kind]"; exit 2 ;;
esac

kind=$(cd "$APP" 2>/dev/null && python3 screen_map.py --kind 2>/dev/null)
case "$kind" in
    addon|builtin) ;;
    *) echo "Cannot tell which daemon this device needs: install the Command Center first ($APP/screen_map.py --kind said '$kind')."; exit 1 ;;
esac
[ "${1:-}" = "--kind" ] && { echo "$kind"; exit 0; }

case "$kind" in
    addon) want=dual-screen-layout-and-power; other=dual-screen-builtin-layout; lock=/run/dual-screen-layout-and-power.pid
           otherlock=/run/dual-screen-builtin-layout.pid ;;
    builtin) want=dual-screen-builtin-layout; other=dual-screen-layout-and-power; lock=/run/dual-screen-builtin-layout.pid
             otherlock=/run/dual-screen-layout-and-power.pid ;;
esac
[ -f "$HERE/$want" ] || { echo "$HERE/$want is missing"; exit 1; }

stop_daemon() {
    # $1 = pid file. Only the pid the file names, and only while that process is still this daemon.
    p=$(cat "$1" 2>/dev/null)
    [ -n "$p" ] && [ -d "/proc/$p" ] || return 0
    case "$(tr '\000' ' ' < "/proc/$p/cmdline" 2>/dev/null)" in
        *dual-screen-*) kill "$p" 2>/dev/null; sleep 2 ;;
    esac
}

echo "=== $want (this device needs the $kind daemon) ==="

# the other kind must not stay installed
if [ -f "$AUTOSTART/$other" ]; then
    [ "$START" = 1 ] && stop_daemon "$otherlock"
    mv "$AUTOSTART/$other" "$BK/$other.disabled-$TS"
    echo "  moved the $other daemon aside (it is for the other kind of device)"
fi

if [ -f "$AUTOSTART/$want" ]; then
    cp -p "$AUTOSTART/$want" "$BK/$want.bak-$TS"
    echo "  backed up the existing copy"
fi
mkdir -p "$AUTOSTART"
cp "$HERE/$want" "$AUTOSTART/$want" && chmod 755 "$AUTOSTART/$want"
echo "  installed: $AUTOSTART/$want"

# the names the daemon reads
if [ ! -s "$APP/device-profile.env" ]; then
    tmp="$APP/device-profile.env.new"
    if (cd "$APP" && python3 screen_map.py --env > "$tmp" 2>/dev/null) && grep -q "^\(INTERNAL\|BOTTOM_OUTPUT\)='" "$tmp"; then
        mv "$tmp" "$APP/device-profile.env"
        echo "  wrote $APP/device-profile.env"
    else
        rm -f "$tmp"
        echo "  could not write $APP/device-profile.env (the daemon will use its own names or do nothing)"
    fi
fi

if [ "$START" = 1 ]; then
    stop_daemon "$lock"
    rm -f "$lock"
    "$AUTOSTART/$want"
    sleep 6
    p=$(cat "$lock" 2>/dev/null)
    if [ -n "$p" ] && [ -d "/proc/$p" ]; then echo "  running, pid $p"; else echo "  NOT RUNNING: see $AUTOSTART/$want.log"; fi
fi
[ "$kind" = builtin ] && echo "  switch it off with /storage/.disable-dualscreen; give lowerdeck back with: $AUTOSTART/$want --restore-lowerdeck"
exit 0
