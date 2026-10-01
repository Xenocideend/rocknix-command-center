#!/bin/sh
# Step 3: install the verified build as /storage/rp5deck + 094 autostart
# (app + focus guard + game-screen overlay + the DEVICE_HAS_DUAL_SCREEN pin),
# with a backup of everything it replaces. Never touches 092/093, /flash,
# system.cfg or sway.
#
#   sh td3-install.sh install    back up, install, start 094 (focus guard in
#                                --dry-run until step 8 - see env below)
#   sh td3-install.sh status     what runs, the last log lines (read-only)
#   sh td3-install.sh guard-live drop --dry-run from the focus guard (step 8;
#                                restarts 094, which re-reads the env file)
#   sh td3-install.sh guard-dry  back to --dry-run for the guard (restarts 094)
#   sh td3-install.sh guard-log  the guard's lines in the 094 log (read-only)
#   sh td3-install.sh restart    stop + start 094 (env unchanged)
#   sh td3-install.sh kill9-app  HF1 S2: SIGKILL main.py (094 restarts it)
#   sh td3-install.sh kill-guard FOC step 5: SIGTERM focus_guard.py (094 restarts it)
#   sh td3-install.sh rollback BACKUP_DIR
#                                stop 094, remove it from autostart, put the
#                                old /storage/rp5deck back (the flag file is
#                                left as it is: pinned OFF is the owner's call)
. "$(dirname "$0")/td-common.sh"
ACTION=${1:-status}

stop_094() {
    if [ -f /run/command-center-app.pid ]; then
        kill "$(cat /run/command-center-app.pid)" 2>/dev/null && echo "sent SIGTERM to 094 ($(cat /run/command-center-app.pid))"
        i=0
        while [ -f /run/command-center-app.pid ] && [ $i -lt 20 ]; do sleep 0.5; i=$((i + 1)); done
    fi
    left=$(td_pids "$HOME_DIR/main.py"; td_pids "$HOME_DIR/focus_guard.py"; td_pids "$HOME_DIR/cc_overlay.py")
    [ -n "$left" ] && echo "WARNING: still running after 10 s: $left"
}

case "$ACTION" in
install)
    td_sanity
    [ -f "$BUILD/MANIFEST.md5" ] || { echo "run from the verified build dir"; exit 2; }
    [ "$BUILD" = "$HOME_DIR" ] && { echo "refusing: the build dir IS $HOME_DIR"; exit 2; }
    BK=$BK_ROOT/$TS
    mkdir -p "$BK" || exit 1
    echo "backup dir: $BK"
    # the old hand-started test instances (rp5deck-v4 / rp5deck-main): stop them
    for d in /storage/rp5deck-v4/rp5deck /storage/rp5deck-main/rp5deck; do
        for p in $(td_pids "$d/main.py"); do kill "$p" && echo "stopped old test instance $p ($d)"; done
    done
    stop_094
    [ -e "$AUTOSTART/command-center-app" ] && cp -p "$AUTOSTART/command-center-app" "$BK/command-center-app.old"
    [ -f "$FLAG_FILE" ] && cp -p "$FLAG_FILE" "$BK/080-dual_screen_mode"
    if [ -d "$HOME_DIR" ]; then
        mv "$HOME_DIR" "$BK/rp5deck.old" || exit 1
        echo "moved the old $HOME_DIR to $BK/rp5deck.old"
    fi
    cp -a "$BUILD" "$HOME_DIR" || { echo "FAIL: copy"; exit 1; }
    (cd "$HOME_DIR" && md5sum -c MANIFEST.md5 > /tmp/td3-md5.txt 2>&1) ||
        { echo "FAIL: the copy does not match the manifest - rolling back";
          mv "$HOME_DIR" "$BK/rp5deck.bad-copy";
          [ -d "$BK/rp5deck.old" ] && mv "$BK/rp5deck.old" "$HOME_DIR"; exit 3; }
    [ -f "$BK/rp5deck.old/config.json" ] && cp -p "$BK/rp5deck.old/config.json" "$HOME_DIR/config.json" &&
        echo "carried over the old config.json"
    # 26 Sep (the owner's Notes were lost at an update): carry over everything the device
    # created in the old app folder - files not in the old build's MANIFEST.md5 (notes/,
    # logs, ...) - unless the new build ships a file of that name. env is rewritten below.
    [ -f "$BK/rp5deck.old/MANIFEST.md5" ] && python3 - "$BK/rp5deck.old" "$HOME_DIR" <<'CARRY'
import os, shutil, sys
old, new = sys.argv[1], sys.argv[2]
built = set(l.split("  ./", 1)[1].strip() for l in open(os.path.join(old, "MANIFEST.md5")) if "  ./" in l)
n = 0
for root, dirs, files in os.walk(old):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        rel = os.path.relpath(os.path.join(root, f), old).replace(os.sep, "/")
        if rel in built or rel == "MANIFEST.md5" or rel.endswith(".pyc") or rel == "env" or rel.startswith("env.bak"):
            continue
        dst = os.path.join(new, rel)
        if os.path.exists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(root, f), dst)
        n += 1
print("carried over %d file(s) the device created (notes, logs, ...)" % n)
CARRY
    # env: the focus guard logs what it WOULD do until step 8 (FX-E's first pass)
    printf 'RP5DECK_GUARD_ARGS=--dry-run\n' > "$HOME_DIR/env"
    td_write_device_profile
    cp -p "$HOME_DIR/command-center-app" "$AUTOSTART/command-center-app" && chmod +x "$AUTOSTART/command-center-app"
    echo "installed $AUTOSTART/command-center-app ($(md5sum "$AUTOSTART/command-center-app" | cut -c1-32))"
    "$AUTOSTART/command-center-app"            # self-backgrounds
    sleep 8
    echo "BACKUP=$BK   (rollback: sh $0 rollback $BK)"
    td_prune_backups
    sh "$0" status
    ;;
status)
    echo "094 pid file: $(cat /run/command-center-app.pid 2>/dev/null || echo none)"
    echo "main.py: $(td_pids "$HOME_DIR/main.py" | tr '\n' ' ')  focus_guard: $(td_pids "$HOME_DIR/focus_guard.py" | tr '\n' ' ')  cc_overlay: $(td_pids "$HOME_DIR/cc_overlay.py" | tr '\n' ' ')"
    echo "080 flag: $(cat "$FLAG_FILE" 2>/dev/null)"
    echo "env: $(cat "$HOME_DIR/env" 2>/dev/null)"
    echo "--- 094 log (last 25) ---"
    tail -25 "$HOME_DIR/log/command-center-app.log" 2>/dev/null
    echo "--- rp5deck.log (last 10) ---"
    tail -10 "$HOME_DIR/log/rp5deck.log" 2>/dev/null
    ;;
guard-live)
    td_sanity
    cp -p "$HOME_DIR/env" "$HOME_DIR/env.bak-$TS" 2>/dev/null
    : > "$HOME_DIR/env"
    # the guard loop evaluated RP5DECK_GUARD_ARGS when 094 started, so killing
    # only the guard would bring it back in --dry-run: restart the whole 094
    stop_094
    "$AUTOSTART/command-center-app"
    sleep 8
    sh "$0" status
    ;;
guard-dry)
    printf 'RP5DECK_GUARD_ARGS=--dry-run\n' > "$HOME_DIR/env"
    stop_094
    "$AUTOSTART/command-center-app"
    sleep 8
    sh "$0" status
    ;;
guard-log)
    grep -n 'focus_guard\|focus guard' "$HOME_DIR/log/command-center-app.log" 2>/dev/null | tail -60
    echo "dry-run decisions so far: $(grep -c 'dry-run] would focus' "$HOME_DIR/log/command-center-app.log" 2>/dev/null)"
    ;;
restart)
    stop_094
    "$AUTOSTART/command-center-app"
    sleep 8
    sh "$0" status
    ;;
kill9-app)
    for p in $(td_pids "$HOME_DIR/main.py"); do kill -9 "$p" && echo "SIGKILL main.py $p"; done
    ;;
kill-guard)
    for p in $(td_pids "$HOME_DIR/focus_guard.py"); do kill "$p" && echo "SIGTERM focus_guard $p"; done
    ;;
rollback)
    BK=${2:?usage: rollback BACKUP_DIR}
    [ -d "$BK" ] || { echo "no such backup: $BK"; exit 2; }
    stop_094
    if [ -f "$BK/command-center-app.old" ]; then cp -p "$BK/command-center-app.old" "$AUTOSTART/command-center-app"
    else mv "$AUTOSTART/command-center-app" "$BK/command-center-app.i2-installed" 2>/dev/null; fi
    if [ -d "$BK/rp5deck.old" ]; then
        mv "$HOME_DIR" "$HOME_DIR.i2-removed-$TS" 2>/dev/null
        mv "$BK/rp5deck.old" "$HOME_DIR" && echo "restored the old $HOME_DIR"
    else
        mv "$HOME_DIR" "$HOME_DIR.i2-removed-$TS" 2>/dev/null && echo "moved the I2 install aside ($HOME_DIR.i2-removed-$TS)"
    fi
    echo "094 autostart now: $(ls "$AUTOSTART"/command-center-app 2>/dev/null || echo absent)"
    echo "(the 080 flag was left as it is; its backup is $BK/080-dual_screen_mode)"
    ;;
*)
    echo "usage: $0 install | status | guard-live | guard-dry | guard-log | restart | kill9-app | kill-guard | rollback BACKUP_DIR"; exit 2 ;;
esac
