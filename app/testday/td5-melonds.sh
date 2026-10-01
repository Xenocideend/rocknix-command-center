#!/bin/sh
# Step 5: DS2b - melonDS window 2 = bottom screen only (research/DS2b-melonds-per-window.md
# section 6 item 1): append Screen1Sizing=5 to melonDS.ini (on /storage, never
# regenerated; only melonDS.toml is rebuilt from it at every launch).
#   sh td5-melonds.sh apply     back up the ini, append the line once
#   sh td5-melonds.sh check     WHILE the DS game runs: what melonDS actually used
#   sh td5-melonds.sh rollback  remove exactly that line (backup stays)
. "$(dirname "$0")/td-common.sh"
INI=/storage/.config/melonDS/melonDS.ini
TOML=/storage/.config/melonDS/melonDS.toml
BK=/storage/rp5deck-ds2b
case "${1:-check}" in
apply)
    td_sanity
    [ -f "$INI" ] || { echo "no $INI - launch melonDS once first"; exit 2; }
    mkdir -p "$BK" && cp -p "$INI" "$BK/melonDS.ini.bak-$TS" && echo "backup: $BK/melonDS.ini.bak-$TS"
    if grep -q '^Screen1Sizing=' "$INI"; then
        echo "already present: $(grep '^Screen1Sizing=' "$INI")  - not changed"
    else
        printf 'Screen1Sizing=5\n' >> "$INI" && echo "appended Screen1Sizing=5"
    fi
    grep -n '^Screen' "$INI"
    ;;
check)
    echo "--- ini ---"; grep -n '^Screen' "$INI" 2>/dev/null
    echo "--- toml (regenerated at this launch) ---"
    grep -n 'ScreenSizing\|ScreenLayout\|Enabled' "$TOML" 2>/dev/null
    echo "want: Instance0.Window0.ScreenSizing = 4 (top only) and Instance0.Window1.ScreenSizing = 5 (bottom only)"
    td_sway
    swaymsg -t get_tree -r > /tmp/td5-tree.json 2>/dev/null
    python3 -B -c "
import json
def walk(n, out=None):
    for c in n.get('nodes', []) + n.get('floating_nodes', []):
        if c.get('type') == 'output': out = c.get('name')
        if c.get('pid') and c.get('name'): print(out, repr(c.get('name')), 'focused' if c.get('focused') else '')
        walk(c, out)
walk(json.load(open('/tmp/td5-tree.json')))
" 2>&1
    ;;
rollback)
    [ -f "$INI" ] || exit 2
    cp -p "$INI" "$BK/melonDS.ini.before-rollback-$TS" 2>/dev/null
    sed -i '/^Screen1Sizing=5$/d' "$INI" && echo "removed Screen1Sizing=5"
    grep -n '^Screen' "$INI"
    ;;
*)
    echo "usage: $0 apply | check | rollback"; exit 2 ;;
esac
