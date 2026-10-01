# td-common.sh - sourced by the testday/td*.sh scripts (TEST-DAY-CHECKLIST.md).
# POSIX sh (busybox ash / dash). Never prints system.cfg (it holds credentials).
set -u
TD_DIR=$(cd "$(dirname "$0")" && pwd)
BUILD=$(cd "$TD_DIR/.." && pwd)                 # the deployed rp5deck/ directory
TS=$(date +%Y%m%d-%H%M%S)
BK_ROOT=/storage/rp5deck-backups
HOME_DIR=/storage/rp5deck                       # 094's RP5DECK_HOME, 092's config.json
AUTOSTART=/storage/.config/autostart
FLAG_FILE=/storage/.config/profile.d/080-dual_screen_mode
SYSCFG=/storage/.config/system/configs/system.cfg
# rk.py's ssh shell has no session env: grim / headless Firefox need it (td2,
# test day: 'XDG_RUNTIME_DIR is invalid or not set'). Same values as 094.
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/var/run/0-runtime-dir}
export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-1}

# Hard stop unless this is the RP5 with the add-on screen lit, or a handheld with two built-in panels that the app's profile
# list names (untested on it, built from ROCKNIX's scripts), or the person installing says go on (TD_ALLOW_ANY_DEVICE=1).
td_sanity() {
    model=$(tr -d '\000' < "${TD_MODEL_FILE:-/proc/device-tree/model}" 2>/dev/null)
    case "$model" in
        *"Retroid Pocket 5"*) ;;
        *)
            known=$(cd "$BUILD" 2>/dev/null && python3 screen_map.py --known 2>/dev/null)
            if [ "$known" = builtin ]; then
                echo "sanity OK: $model has two built-in screens and a profile built from ROCKNIX's scripts; it has not been tried on this model"
                return 0
            fi
            if [ "${TD_ALLOW_ANY_DEVICE:-}" = 1 ]; then
                echo "sanity: '$model' is not a model the app knows, going on because TD_ALLOW_ANY_DEVICE=1"
                return 0
            fi
            echo "HARD STOP: model is '$model', not a Retroid Pocket 5 or one of the built-in dual-screen handhelds the app knows"
            echo "(to try anyway: TD_ALLOW_ANY_DEVICE=1 sh $0 ...)"
            exit 90 ;;
    esac
    st=$(cat /sys/class/drm/card0-DP-1/status 2>/dev/null)
    mode=$(cat /storage/dual-screen-mode 2>/dev/null)
    if [ "$st" != "connected" ] && [ "${TD_ALLOW_UNDOCKED:-}" = 1 ]; then
        echo "sanity OK: $model; DP-1 $st (undocked install asked for); dual-screen-mode $mode"
        return 0
    fi
    if [ "$st" != "connected" ]; then
        echo "HARD STOP: DP-1 is '$st' (dual-screen-mode '$mode'; 'charge' = off by design)."
        echo "Attach the Dual Screen add-on in display mode (showing a picture) and try again."
        exit 91
    fi
    echo "sanity OK: $model; DP-1 connected; dual-screen-mode $mode"
}

td_sway() {
    SWAYSOCK=$(find /run /tmp -name 'sway-ipc.*.sock' 2>/dev/null | head -1)
    export SWAYSOCK
}

# the pid(s) of processes whose command line contains $1 (never this shell's own)
# Keeps the newest TD_KEEP_BACKUPS (default 5) timestamped install backups (YYYYMMDD-HHMMSS folders) in BK_ROOT and
# removes older ones, each is a whole copy of the app. Folders with any other name are never touched.
td_prune_backups() {
    keep=${TD_KEEP_BACKUPS:-5}
    n=0
    for d in $(ls -1d "$BK_ROOT"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9] 2>/dev/null | sort -r); do
        n=$((n + 1))
        [ "$n" -gt "$keep" ] && rm -rf "$d" && echo "removed old backup $d"
    done
    return 0
}

# Writes HOME_DIR/device-profile.env, the screen and touch names the layout daemon reads (screen_map.py --env), from the
# installed app's own profile. Never fails an install: if the profile cannot be read the old file (or none) stays.
td_write_device_profile() {
    tmp="$HOME_DIR/device-profile.env.new"
    if (cd "$HOME_DIR" && python3 screen_map.py --env > "$tmp" 2>/dev/null) && grep -q "^\(INTERNAL\|BOTTOM_OUTPUT\)='" "$tmp"; then
        mv "$tmp" "$HOME_DIR/device-profile.env"
        echo "device profile: $(tr '\n' ' ' < "$HOME_DIR/device-profile.env")"
    else
        rm -f "$tmp"
        echo "device profile: not written (the layout daemon keeps its own names)"
    fi
    return 0
}

td_pids() {
    for p in /proc/[0-9]*; do
        pid=${p#/proc/}
        [ "$pid" = "$$" ] && continue
        cmd=$(tr '\000' ' ' 2>/dev/null < "$p/cmdline") || continue   # 2> first: quiet when a pid exits mid-scan
        case "$cmd" in *"$1"*) case "$cmd" in *td_pids*|*testday/td*) ;; *) echo "$pid" ;; esac ;; esac
    done
}
