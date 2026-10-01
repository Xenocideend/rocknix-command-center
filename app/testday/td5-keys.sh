#!/bin/sh
# td5-keys.sh - re-add the DS2 dual-screen keys to system.cfg WITH ES STOPPED.
# Test day, 24 Sep: the keys DS2 appended at ~01:40 while ES ran were wiped
# when ES saved its in-memory copy at the 12:46 reboot (ES rewrites the whole
# file). So: refuse unless ES is stopped; back up; add; print only these keys.
#   sh td5-keys.sh apply      (ES must be stopped: systemctl stop essway)
#   sh td5-keys.sh check      (the three keys only; any time)
#   sh td5-keys.sh rollback <backup>
. "$(dirname "$0")/td-common.sh"
BK=/storage/rp5deck-backups/system-cfg
GAME="Example Game (Korea).zip"
show() {
    grep -E '^(3ds\.screen_layout|wiiu\.gamepad_enabled)=' "$SYSCFG"
    grep -F 'nds["' "$SYSCFG" | grep -F 'screen_layout'
    true
}
case "${1:-check}" in
apply)
    td_sanity
    if pidof emulationstation >/dev/null; then
        echo "REFUSED: ES is running - it would overwrite these at its next save."
        echo "Stop it first: systemctl stop essway"
        exit 3
    fi
    [ -f "/storage/roms/nds/$GAME" ] || { echo "no /storage/roms/nds/$GAME"; exit 2; }
    mkdir -p "$BK" && cp -p "$SYSCFG" "$BK/system.cfg.$TS" && echo "backup: $BK/system.cfg.$TS"
    [ -n "$(tail -c1 "$SYSCFG")" ] && echo >> "$SYSCFG"
    add() {   # line key-regex
        if grep -q "^$2=" "$SYSCFG"; then echo "  exists: $(grep "^$2=" "$SYSCFG")"
        else echo "$1" >> "$SYSCFG" && echo "  added: $1"; fi
    }
    add '3ds.screen_layout=5' '3ds\.screen_layout'
    add 'wiiu.gamepad_enabled=true' 'wiiu\.gamepad_enabled'
    k="nds[\"$GAME\"].screen_layout"
    if grep -qF "$k=" "$SYSCFG"; then echo "  exists: $(grep -F "$k=" "$SYSCFG")"
    else echo "$k=6" >> "$SYSCFG" && echo "  added: $k=6"; fi
    echo "--- managed keys now ---"; show
    ;;
check)
    echo "--- managed keys ---"; show
    echo "ES running: $(pidof emulationstation >/dev/null && echo yes || echo no)"
    ;;
rollback)
    [ -f "${2:-}" ] || { echo "usage: $0 rollback <backup file>"; exit 2; }
    pidof emulationstation >/dev/null && { echo "REFUSED: stop ES first"; exit 3; }
    cp -p "$2" "$SYSCFG" && echo "restored $2"
    ;;
*) echo "usage: $0 apply | check | rollback <backup>"; exit 2 ;;
esac
