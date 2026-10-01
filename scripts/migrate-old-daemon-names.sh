#!/bin/sh
# Moves an older install's autostart files to the current names. The daemons got descriptive names (25 Sep 2026),
# so an install from before that still has 092-dual-screen-persist, 094-rp5deck and the rest. For each old name:
# stop it (pid file), move its autostart file into a backup folder, and carry its log over to the new name.
# Starts nothing, installing the new files does that. Safe to run again: missing files are skipped. Run it before
# installing the app and the layout daemon, or two copies of the same job end up running.
#
#   sh migrate-old-daemon-names.sh
A=${MIGRATE_AUTOSTART:-/storage/.config/autostart}
RUN=${MIGRATE_RUN:-/run}
BK=${MIGRATE_BACKUP:-/storage}/autostart-old-names-$(date +%Y%m%d-%H%M%S)
[ -d "$A" ] || { echo "nothing to migrate: $A does not exist"; exit 0; }
mkdir -p "$BK" || exit 1
for pair in \
    092-dual-screen-persist:dual-screen-layout-and-power \
    093-kernel-dt-guard:reapply-dual-screen-kernel-fix \
    094-rp5deck:command-center-app \
    095-charge-limit:battery-charge-limit \
    096-charge-sleep-guard:no-sleep-while-charger-stuck \
    097-perf-profile:gpu-cap-heavy-game-boost \
    092-dual-screen-layout-and-power:dual-screen-layout-and-power \
    093-reapply-dual-screen-kernel-fix:reapply-dual-screen-kernel-fix \
    094-command-center-app:command-center-app \
    095-battery-charge-limit:battery-charge-limit \
    096-no-sleep-while-charger-stuck:no-sleep-while-charger-stuck \
    097-gpu-cap-heavy-game-boost:gpu-cap-heavy-game-boost \
    098-hibernate-swap-file:hibernate-swap-file
do
    old=${pair%%:*}; new=${pair#*:}
    if [ -f "$RUN/$old.pid" ]; then
        p=$(cat "$RUN/$old.pid" 2>/dev/null)
        if [ -n "$p" ] && [ -d "/proc/$p" ]; then
            kill "$p" 2>/dev/null && echo "stopped $old (pid $p)"
            i=0; while [ -d "/proc/$p" ] && [ $i -lt 20 ]; do sleep 0.5; i=$((i + 1)); done
            [ -d "/proc/$p" ] && echo "WARNING: $old (pid $p) still running"
        fi
        rm -f "$RUN/$old.pid"
    fi
    [ -f "$A/$old" ] && mv "$A/$old" "$BK/" && echo "moved $A/$old -> $BK/"
    if [ -f "$A/$old.log" ]; then
        if [ -f "$A/$new.log" ]; then cat "$A/$new.log" >> "$A/$old.log" && rm -f "$A/$new.log"; fi
        mv "$A/$old.log" "$A/$new.log" && echo "log -> $new.log"
    fi
done
if [ -n "$(ls -A "$BK")" ]; then echo "old files kept in $BK"; else rmdir "$BK"; echo "nothing to move"; fi
ls "$A"
exit 0
