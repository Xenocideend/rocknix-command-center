#!/bin/sh
# Turn the add-on's DisplayPort output off across suspend, and back on at resume.
#
# Why: on the RP5 with kernel v3, suspending (s2idle) while DP-1 is lit resets
# the device instead of resuming; with DP-1 disabled in sway first it suspends
# and resumes cleanly (tested 24 Sep 2026: undocked OK, docked+DP off OK,
# docked+DP on = reset). Run by dp-sleep-guard.service: "off" before
# sleep.target, "on" when sleep.target stops after resume.
#
# Resume on battery (test day, 24 Sep, rp5deck session, owner's OK): the
# add-on loses power in s2idle and cold-starts on wake - its USB device (the
# touchscreen, usb 1-1) took ~5 s to enumerate again (error -71 until then),
# and the old immediate "output DP-1 enable" landed ~3 s before it was back,
# so the top screen stayed dark. On a charger the add-on stays powered and the
# link is ready at once. So "on" now WAITS (at most WAIT_MAX s) for the DP
# link (hpd = 1), settles, then enables. The add-on's USB device is NOT a
# precondition: on the device (14:55) hpd was 1 at once and usb 1-1 only came
# back 2.5 s AFTER the enable (the add-on wakes when DP drives it), so waiting
# for it cost 22 s. It is a post-check instead: if the touchscreen is not
# back within USB_MAX s of the enable, DP-1 is cycled off/on once. If hpd is
# still 0 at the end of the wait it does ONE port reset (charge-safe, below).
# The wait runs inside ExecStop on purpose: systemd runs this unit's jobs one
# at a time, so a power press during the wait queues the next suspend until
# the wait is over, and "off" then disables DP-1 before sleeping (enabling DP
# mid-suspend is exactly the reset case). A background waiter would also be
# killed with the unit's cgroup when ExecStop returns.
#
# Never blocks a suspend: always exits 0. Only re-enables what it disabled.
STATE=${DPSG_STATE:-/run/dp-sleep-guard.disabled}
LOG=${DPSG_LOG:-/storage/.config/dp-sleep-guard.log}
DRM=${DPSG_DRM:-/sys/class/drm/card0-DP-1}
EN=$DRM/enabled
TYPEC=${DPSG_TYPEC:-/sys/class/typec/port0}
USBDEV=${DPSG_USBDEV:-/sys/bus/usb/devices/1-1}
PSY=${DPSG_PSY:-/sys/class/power_supply}
SWAYMSG=${DPSG_SWAYMSG:-swaymsg}
WAIT_MAX=${DPSG_WAIT_MAX:-20}        # seconds to wait for the add-on
SETTLE=${DPSG_SETTLE:-1}             # seconds after hpd = 1, before enabling
USB_MAX=${DPSG_USB_MAX:-10}          # seconds for the add-on's USB after the enable
TICK=${DPSG_TICK:-0.25}
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
nap() { sleep "$1" 2>/dev/null || sleep 1; }
[ -n "${SWAYSOCK:-}" ] || SWAYSOCK=$(ls /run/0-runtime-dir/sway-ipc.*.sock 2>/dev/null | head -1)
export SWAYSOCK

hpd() { cat "$TYPEC"/port0-partner/port0-partner.*/displayport/hpd 2>/dev/null | head -1; }
link_up() { [ "$(hpd)" = "1" ]; }
usb_back() { [ "$(cat "$USBDEV/product" 2>/dev/null)" = "RDS Touchscreen" ]; }
wait_usb() {   # up to USB_MAX s; 0 if the add-on's USB device is there
    m=$(awk "BEGIN{print int($USB_MAX / $TICK)}"); j=0
    while [ $j -lt "$m" ] && ! usb_back; do nap "$TICK"; j=$((j + 1)); done
    usb_back
}

case "$1" in
    off)
        rm -f "$STATE"
        if [ "$(cat $EN 2>/dev/null)" != "enabled" ]; then
            log "suspend: DP-1 not enabled - nothing to do"
            exit 0
        fi
        if [ -z "$SWAYSOCK" ]; then
            log "suspend: DP-1 enabled but no sway socket - cannot disable"
            exit 0
        fi
        $SWAYMSG output DP-1 disable >/dev/null 2>&1
        i=0; while [ "$(cat $EN)" = "enabled" ] && [ $i -lt 20 ]; do nap 0.1; i=$((i + 1)); done
        if [ "$(cat $EN)" = "enabled" ]; then
            log "suspend: WARNING DP-1 still enabled after disable request"
        else
            touch "$STATE"
            log "suspend: DP-1 disabled"
        fi
        ;;
    on)
        [ -e "$STATE" ] || exit 0
        rm -f "$STATE"
        t0=$(date +%s)
        max_ticks=$(awk "BEGIN{print int($WAIT_MAX / $TICK)}")
        n=0
        while [ $n -lt "$max_ticks" ] && ! link_up; do nap "$TICK"; n=$((n + 1)); done
        waited=$(( $(date +%s) - t0 ))
        if link_up; then
            nap "$SETTLE"
            $SWAYMSG output DP-1 enable >/dev/null 2>&1
            log "resume: DP link up after ${waited}s (hpd=1) - DP-1 enable requested (now $(cat $EN 2>/dev/null), $(cat $DRM/status 2>/dev/null))"
            if wait_usb; then
                log "resume: add-on back (usb 1-1) $(( $(date +%s) - t0 ))s after resume"
            else
                $SWAYMSG output DP-1 disable >/dev/null 2>&1
                nap 1
                $SWAYMSG output DP-1 enable >/dev/null 2>&1
                if wait_usb; then r=back; else r="still not back"; fi
                log "resume: add-on USB not back ${USB_MAX}s after the enable - DP-1 cycled off/on once; add-on $r"
            fi
        else
            h=$(hpd); u=no
            usb_back && u=yes
            $SWAYMSG output DP-1 enable >/dev/null 2>&1
            log "resume: add-on NOT back after ${waited}s (hpd=${h:-none}, usb=$u) - DP-1 enable requested anyway (now $(cat $EN 2>/dev/null), $(cat $DRM/status 2>/dev/null))"
            if [ -d "$TYPEC/port0-partner" ] && [ "$h" = "0" ]; then
                # Charge-safe (charging session, 24 Sep): tcpm refuses the dock's
                # PR_Swap while port_type != dual, and forcing source while a
                # powered dock feeds us stops passthrough charging. So: never
                # when we already source, never when a dock is charging us, and
                # dual goes back the moment the role changes.
                pr=$(cat "$TYPEC/power_role" 2>/dev/null)
                fed=no
                for f in "$PSY"/tcpm-source-psy-*/online; do
                    [ "$(cat "$f" 2>/dev/null)" = "1" ] && fed=yes
                done
                case "$pr" in
                *"[source]"*)
                    log "resume: hpd stuck at 0 - no port reset (already source: $pr)" ;;
                *)
                    if [ "$fed" = yes ]; then
                        log "resume: hpd stuck at 0 - no port reset (a powered dock is charging us; a reset would cut it)"
                    else
                        echo source > "$TYPEC/port_type" 2>/dev/null
                        k=0
                        while [ "$(cat "$TYPEC/power_role" 2>/dev/null)" = "$pr" ] && [ $k -lt 20 ]; do
                            nap 0.1; k=$((k + 1))
                        done
                        echo dual > "$TYPEC/port_type" 2>/dev/null
                        log "resume: hpd stuck at 0 - one port reset (port_type source -> dual after $k ticks; role was: $pr)"
                    fi ;;
                esac
            fi
        fi
        ;;
esac
exit 0
