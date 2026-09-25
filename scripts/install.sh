#!/bin/sh
# Install the two dual-screen system scripts this repo ships outside the
# rp5deck app itself:
#   092-dual-screen-persist  -> /storage/.config/autostart/  (keeps the
#     Retroid Dual Screen add-on's layout, ES output and touch mapping
#     correct across a reboot - see the script's own header for why)
#   dp-sleep-guard.sh/.service -> a systemd unit that turns the add-on's
#     DisplayPort output off across suspend and back on at resume (some
#     RP5 kernels reset instead of resuming if DP is left lit)
#
# Run this ON THE DEVICE, from the directory this script is in (i.e. after
# copying this repo's scripts/ folder to the device, e.g. with rk.py --put
# or scp). It does not touch rp5deck itself - see testday/td3-install.sh
# under app/ for that.
#
# Safe to re-run: any existing 092-dual-screen-persist is backed up first,
# and dp-sleep-guard's install/enable steps are idempotent.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AUTOSTART=/storage/.config/autostart
TS=$(date +%Y%m%d-%H%M%S)
fail=0

echo "=== 092-dual-screen-persist ==="
if [ -f "$AUTOSTART/092-dual-screen-persist" ]; then
    cp -p "$AUTOSTART/092-dual-screen-persist" "/storage/092-dual-screen-persist.bak-$TS"
    echo "  backed up existing copy to /storage/092-dual-screen-persist.bak-$TS"
fi
cp "$HERE/092-dual-screen-persist" "$AUTOSTART/092-dual-screen-persist"
chmod 755 "$AUTOSTART/092-dual-screen-persist"
echo "  installed: $AUTOSTART/092-dual-screen-persist"
echo "  it starts at the next boot (autostart), or run it directly to start it now."

echo
echo "=== dp-sleep-guard ==="
cp "$HERE/dp-sleep-guard.sh" /storage/.config/dp-sleep-guard.sh
chmod 755 /storage/.config/dp-sleep-guard.sh
mkdir -p /storage/.config/system.d
cp "$HERE/dp-sleep-guard.service" /storage/.config/system.d/dp-sleep-guard.service
systemctl daemon-reload
systemctl enable dp-sleep-guard.service 2>&1
echo "  is-enabled: $(systemctl is-enabled dp-sleep-guard.service 2>&1)"

echo
echo "=== verify ==="
for f in "$AUTOSTART/092-dual-screen-persist" /storage/.config/dp-sleep-guard.sh \
         /storage/.config/system.d/dp-sleep-guard.service; do
    if [ -f "$f" ]; then
        echo "  ok    $(md5sum "$f" | cut -d' ' -f1)  $f"
    else
        echo "  MISSING $f"
        fail=1
    fi
done

[ "$fail" = 0 ] && echo "INSTALL OK" || echo "INSTALL HAD ERRORS"
exit "$fail"
