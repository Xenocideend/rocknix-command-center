#!/bin/sh
# Runs Steam inside the Command Center's desktop, so the Command Center stays up while Steam is on screen.
#
#     sh /storage/rp5deck/steam/install-steam-nested.sh install    turn it on (also at every boot)
#     sh /storage/rp5deck/steam/install-steam-nested.sh remove     put ROCKNIX's own Steam launch back
#     sh /storage/rp5deck/steam/install-steam-nested.sh status
#
# Run it from the installed app folder (/storage/rp5deck/steam). It puts two small files in place and shadows
# ROCKNIX's /usr/bin/start_steam_arm64.sh with start_steam_nested.sh (a bind mount, the real file is never
# touched, so `remove` or a reboot without the autostart file undoes it).
STORAGE=${STEAM_INSTALL_STORAGE:-/storage}
HERE=$(cd "$(dirname "$0")" && pwd)
AUTOSTART=$STORAGE/.config/autostart/steam-keep-command-center-alive
PROFILE=$STORAGE/.config/profile.d/085-steam-display-priority
LAUNCHER=${STEAM_INSTALL_LAUNCHER:-/usr/bin/start_steam_arm64.sh}
STEAM_DIR=$STORAGE/.local/share/Steam

mounted() { grep -q " $LAUNCHER " /proc/mounts 2>/dev/null; }

put() {
    # put SOURCE DEST: copy and mark executable, only when it differs
    if cmp -s "$1" "$2" 2>/dev/null; then
        echo "  already in place: $2"
    else
        mkdir -p "$(dirname "$2")" && cp "$1" "$2" && chmod 755 "$2" && echo "  installed: $2"
    fi
}

case "${1:-}" in
install)
    for f in start_steam_nested.sh steam-keep-command-center-alive 085-steam-display-priority; do
        [ -f "$HERE/$f" ] || { echo "missing $HERE/$f"; exit 1; }
    done
    if [ "$HERE" != "$STORAGE/rp5deck/steam" ]; then
        echo "run this from the installed app folder, $STORAGE/rp5deck/steam (the boot script points there)"
        exit 1
    fi
    put "$HERE/steam-keep-command-center-alive" "$AUTOSTART"
    put "$HERE/085-steam-display-priority" "$PROFILE"
    if [ -z "$STEAM_INSTALL_NO_MOUNT" ]; then
        sh "$AUTOSTART"
        mounted && echo "  Steam's launcher is replaced for this boot" || echo "  could not replace the launcher (is $LAUNCHER there?)"
    fi
    [ -d "$STEAM_DIR" ] || echo "WARNING: $STEAM_DIR is not a folder. Steam is not installed, or its link is broken (it should point at your Steam folder, /storage/roms/steam on the RP5)."
    echo "Restart EmulationStation (systemctl restart essway) so the next Steam launch uses it."
    ;;
remove)
    [ -z "$STEAM_INSTALL_NO_MOUNT" ] && mounted && umount "$LAUNCHER" && echo "  Steam's own launcher is back"
    for f in "$AUTOSTART" "$PROFILE"; do
        if [ -e "$f" ]; then rm -f "$f" && echo "  removed: $f"; else echo "  not installed: $f"; fi
    done
    ;;
status)
    [ -e "$AUTOSTART" ] && echo "boot script: installed" || echo "boot script: not installed"
    [ -e "$PROFILE" ] && echo "display priority file: installed" || echo "display priority file: not installed"
    mounted && echo "launcher: nested (Command Center stays up)" || echo "launcher: ROCKNIX's own"
    [ -d "$STEAM_DIR" ] && echo "Steam folder: found" || echo "Steam folder: missing"
    ;;
*)
    echo "usage: sh $0 install | remove | status"
    exit 2
    ;;
esac
