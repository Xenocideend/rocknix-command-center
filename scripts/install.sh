#!/bin/sh
# Install the two dual-screen system scripts this repo ships outside the
# rp5deck app itself:
#   dual-screen-layout-and-power  -> /storage/.config/autostart/  (keeps the
#     Retroid Dual Screen add-on's layout, ES output and touch mapping
#     correct across a reboot, places each window on the right screen, and on
#     a single screen gives a web app's window a workspace of its own and puts
#     EmulationStation back on its own - see the script's own header)
#   dp-sleep-guard.sh/.service -> a systemd unit that turns the add-on's
#     DisplayPort output off across suspend and back on at resume (some
#     RP5 kernels reset instead of resuming if DP is left lit)
#
# Run this ON THE DEVICE, from the directory this script is in (i.e. after
# copying this repo's scripts/ folder to the device, e.g. with rk.py --put
# or scp). It does not touch rp5deck itself - see testday/td3-install.sh
# under app/ for that.
#
# Coming from the first release (25 Sep)? Run migrate-old-daemon-names.sh first,
# it stops the old 092-dual-screen-persist and 094-rp5deck and moves them
# aside. This script refuses to install next to an old daemon so two copies
# never run.
#
# Safe to re-run: any existing dual-screen-layout-and-power is backed up
# first, and dp-sleep-guard's install/enable steps are idempotent.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AUTOSTART=${INSTALL_AUTOSTART:-/storage/.config/autostart}
TS=$(date +%Y%m%d-%H%M%S)
fail=0

for old in 092-dual-screen-persist 092-dual-screen-layout-and-power; do
    if [ -f "$AUTOSTART/$old" ]; then
        echo "An older install is still here: $AUTOSTART/$old"
        echo "Run  sh $HERE/migrate-old-daemon-names.sh  first, then this script again."
        exit 1
    fi
done

echo "=== dual-screen-layout-and-power ==="
if [ -f "$AUTOSTART/dual-screen-layout-and-power" ]; then
    cp -p "$AUTOSTART/dual-screen-layout-and-power" "/storage/dual-screen-layout-and-power.bak-$TS"
    echo "  backed up existing copy to /storage/dual-screen-layout-and-power.bak-$TS"
fi
cp "$HERE/dual-screen-layout-and-power" "$AUTOSTART/dual-screen-layout-and-power"
chmod 755 "$AUTOSTART/dual-screen-layout-and-power"
echo "  installed: $AUTOSTART/dual-screen-layout-and-power"
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
for f in "$AUTOSTART/dual-screen-layout-and-power" /storage/.config/dp-sleep-guard.sh \
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
