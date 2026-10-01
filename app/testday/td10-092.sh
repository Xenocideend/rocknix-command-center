#!/bin/sh
# Step 10 prerequisite: SW1's 092 (reads screens.es_screen, moves ES) - the
# dual-screen/charger session's file. Needs the owner's OK AND a heads-up to
# that session. Push the PC copy first:
#   python rk.py --put-text ..\rocknix-config\dual-screen-layout-and-power /storage/rp5deck-i2/dual-screen-layout-and-power.new
#
#   sh td10-092.sh compare           read-only: which shell functions differ from
#                                    the installed 092; the charging/placement
#                                    ones must be byte-identical
#   sh td10-092.sh install --owner-ok  back up, install (takes effect at the next boot)
#   sh td10-092.sh rollback BACKUP   put the backed-up 092 back (next boot)
. "$(dirname "$0")/td-common.sh"
OLD=$AUTOSTART/dual-screen-layout-and-power
NEW=/storage/rp5deck-i2/dual-screen-layout-and-power.new
MUST_MATCH="power_present role_is try_wake_dp settle_to_sink dp_race_detected reset_port_for_dp dp_altmode_dir prefer_screen ensure_rp5deck_rules"

compare() {
    [ -f "$NEW" ] || { echo "push $NEW first"; return 2; }
    echo "installed: $(md5sum "$OLD")"
    echo "new:       $(md5sum "$NEW")"
    sh -n "$NEW" || { echo "FAIL: sh -n on the new 092"; return 3; }
    python3 -B - "$OLD" "$NEW" "$MUST_MATCH" <<'EOF'
import re, sys
def funcs(p):
    out, cur, body = {}, None, []
    for line in open(p, encoding="utf-8", errors="replace"):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", line)
        if cur is None and m:
            cur, body = m.group(1), [line]
            continue
        if cur is not None:
            body.append(line)
            if line.rstrip() == "}":
                out[cur] = "".join(body)
                cur = None
    return out
old, new, must = funcs(sys.argv[1]), funcs(sys.argv[2]), sys.argv[3].split()
print("changed:", sorted(k for k in old if k in new and old[k] != new[k]))
print("added:  ", sorted(k for k in new if k not in old))
print("removed:", sorted(k for k in old if k not in new))
bad = [k for k in must if k in old and old.get(k) != new.get(k)]
missing = [k for k in must if k not in old]
if missing:
    print("note: not in the installed 092 (cannot compare):", missing)
print("charging/placement functions byte-identical:", "YES" if not bad else "NO: %s" % bad)
sys.exit(1 if bad else 0)
EOF
}

case "${1:-compare}" in
compare)
    compare ;;
install)
    td_sanity
    [ "${2:-}" = "--owner-ok" ] || { echo "REFUSED: needs --owner-ok (owner said yes AND the charger session was told)"; exit 3; }
    compare || { echo "REFUSED: compare failed"; exit 3; }
    BK=$BK_ROOT/$TS; mkdir -p "$BK"
    cp -p "$OLD" "$BK/dual-screen-layout-and-power.installed" && echo "backup: $BK/dual-screen-layout-and-power.installed"
    cp "$NEW" "$OLD" && chmod +x "$OLD" && echo "installed; the running 092 keeps its old code until the next boot"
    echo "BACKUP=$BK   (rollback: sh $0 rollback $BK)"
    ;;
rollback)
    BK=${2:?usage: rollback BACKUP_DIR}
    [ -f "$BK/dual-screen-layout-and-power.installed" ] || { echo "no backup in $BK"; exit 2; }
    cp -p "$BK/dual-screen-layout-and-power.installed" "$OLD" && echo "restored $(md5sum "$OLD") - reboot to run it"
    ;;
*)
    echo "usage: $0 compare | install --owner-ok | rollback BACKUP_DIR"; exit 2 ;;
esac
