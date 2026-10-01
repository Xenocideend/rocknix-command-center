#!/bin/sh
# Step 4: NM1 bad-name fix (tools/fix-bad-names.py; recipe NM1-DRYRUN.txt).
#   sh td4-nm1.sh dry      plan only (default of the tool): writes
#                          /storage/rp5deck-nm1/nm1-plan.txt and .json
#   sh td4-nm1.sh apply    ONLY with ES stopped (systemctl stop essway first):
#                          gamelist backups + a JSON undo log in /storage/rp5deck-nm1
#   sh td4-nm1.sh undo LOG revert an earlier apply (ES stopped again)
#   sh td4-nm1.sh verify   re-run the name guard after ES is back
. "$(dirname "$0")/td-common.sh"
D=/storage/rp5deck-nm1
mkdir -p "$D"
cd "$BUILD" || exit 1
es_stopped() { [ -z "$(pidof emulationstation)" ]; }
case "${1:-dry}" in
dry)
    td_sanity
    python3 -B tools/fix-bad-names.py --out "$D/nm1-plan.txt"
    echo "rc=$?"
    python3 -B tools/fix-bad-names.py --json --out "$D/nm1-plan.json" > /dev/null
    echo "plan: $D/nm1-plan.txt ($(wc -l < "$D/nm1-plan.txt") lines), $D/nm1-plan.json"
    ;;
apply)
    td_sanity
    es_stopped || { echo "REFUSED: EmulationStation is running (pid $(pidof emulationstation)). Run: systemctl stop essway"; exit 3; }
    python3 -B tools/fix-bad-names.py --apply --i-stopped-es --log-dir "$D"
    rc=$?
    echo "rc=$rc; undo logs: $(ls -t "$D"/*undo*.json 2>/dev/null | head -3 | tr '\n' ' ')"
    echo "NOW: systemctl start essway"
    exit $rc
    ;;
undo)
    LOG=${2:?usage: undo /storage/rp5deck-nm1/<undo log>.json}
    es_stopped || { echo "REFUSED: stop ES first (systemctl stop essway)"; exit 3; }
    python3 -B tools/fix-bad-names.py --undo "$LOG"
    echo "rc=$? - NOW: systemctl start essway"
    ;;
verify)
    python3 -B name_guard.py --json > "$D/guard-after.json"
    python3 -B -c "import json,sys; d=json.load(open(sys.argv[1])); print('name guard findings after NM1:', len(d['findings']))" "$D/guard-after.json"
    python3 -B name_guard.py --show 60 2>&1 | tail -40
    ;;
*)
    echo "usage: $0 dry | apply | undo LOG | verify"; exit 2 ;;
esac
