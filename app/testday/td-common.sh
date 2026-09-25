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

# Hard stop unless this is the RP5 with the add-on screen lit.
td_sanity() {
    model=$(tr -d '\000' < /proc/device-tree/model 2>/dev/null)
    case "$model" in
        *"Retroid Pocket 5"*) ;;
        *) echo "HARD STOP: model is '$model', not a Retroid Pocket 5"; exit 90 ;;
    esac
    st=$(cat /sys/class/drm/card0-DP-1/status 2>/dev/null)
    mode=$(cat /storage/dual-screen-mode 2>/dev/null)
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
td_pids() {
    for p in /proc/[0-9]*; do
        pid=${p#/proc/}
        [ "$pid" = "$$" ] && continue
        cmd=$(tr '\000' ' ' 2>/dev/null < "$p/cmdline") || continue   # 2> first: quiet when a pid exits mid-scan
        case "$cmd" in *"$1"*) case "$cmd" in *td_pids*|*testday/td*) ;; *) echo "$pid" ;; esac ;; esac
    done
}
