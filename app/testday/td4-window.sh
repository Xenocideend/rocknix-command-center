#!/bin/sh
# td4-window.sh <nm1-build-dir> - the one ES-stopped window (owner "apply",
# 24 Sep): stop ES, re-add the dual-screen keys (td5-keys.sh), apply NM1
# (td4-nm1.sh from the NM2 build), start ES again - ES is ALWAYS restarted,
# whatever fails. Log: /storage/rp5deck-testlog/step4/window-<ts>.log
set -u
NM=${1:?usage: td4-window.sh /storage/rp5deck-nm2}
T=/storage/rp5deck/testday
mkdir -p /storage/rp5deck-testlog/step4
LOG=/storage/rp5deck-testlog/step4/window-$(date +%H%M%S).log
exec > "$LOG" 2>&1
restart_es() {
    echo "--- starting ES"
    systemctl start essway
    n=0; while [ -z "$(pidof emulationstation)" ] && [ $n -lt 60 ]; do sleep 1; n=$((n + 1)); done
    echo "ES pid: $(pidof emulationstation) after ${n}s"
}
trap restart_es EXIT
sh $T/td-mark.sh step4 begin es-window >/dev/null
echo "--- stopping ES"
systemctl stop essway
n=0; while [ -n "$(pidof emulationstation)" ] && [ $n -lt 20 ]; do sleep 1; n=$((n + 1)); done
if [ -n "$(pidof emulationstation)" ]; then echo "ES did not stop - nothing changed"; exit 1; fi
echo "ES stopped after ${n}s"
echo "=== keys ==="; sh $T/td5-keys.sh apply; echo "keys rc=$?"
echo "=== NM1 apply ==="; sh "$NM/testday/td4-nm1.sh" apply; echo "nm1 rc=$?"
