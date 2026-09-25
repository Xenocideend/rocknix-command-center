#!/bin/sh
# rp5deck ES event hook: screensaver-start  (installed by tools/install-es-hooks.sh)
#
# EmulationStation launches this ASYNCHRONOUSLY: Scripting.cpp executeScript()
# builds a command string and Platform.cpp runs it with fork + execl("/bin/sh",
# "sh", "-c", ...) without waiting. So this can never stall ES, but hooks for
# back-to-back events run CONCURRENTLY and can finish out of order.
#
# Each event is therefore written as its OWN file in a spool directory
# (default /var/run/rp5deck/es-events, tmpfs), named so the names sort in the
# order ES fired the events:
#     <start>-<anchor>-<pid>.ev        (fixed-width decimal fields)
# anchor = the `sh -c` process ES forked for THIS event (the topmost "sh"
#   ancestor). ES creates it on its own thread, one event after the other
#   (the hook itself then runs detached), so its start time (clock ticks
#   since boot, /proc/<anchor>/stat field 22) and then its PID give ES's
#   fire order however late this script itself gets to run.
#   Run outside ES (tests), the anchor is this process. pid = $$ (unique).
# The record goes to a hidden temp name in the same directory, then mv onto
# the final name, so a reader never sees a half-written record. esevents.py
# consumes the files in name order and deletes them. If nothing consumes them
# (the app is not running) the oldest are dropped beyond MAX.
# Record format (esevents.py): NUL-terminated key=value fields. NUL cannot
# occur in an argument, so whatever ES passes is carried byte-exact, and the
# arguments only ever go through printf's %s, never the format string.
#
# WARNING: ES has ALREADY shell-parsed the arguments before this runs: it
# double-quotes only values that contain a space and escapes nothing, so `$`,
# backticks, and (in space-less values) ' & ( ) ; | < > \ are interpreted by
# the shell first. Nothing here can undo that; see name_guard.py (install
# refuses when the library has such names) and es-upstream/.
#
# Must be fast, never block, never print, and always exit 0.
EVENT=screensaver-start
sp=${RP5DECK_ES_SPOOL:-}
if [ -z "$sp" ]; then
    case ${RP5DECK_ES_EVENT_FILE:-} in
        */*) sp=${RP5DECK_ES_EVENT_FILE%/*}/es-events ;;
        *) sp=/var/run/rp5deck/es-events ;;
    esac
fi
MAX=512
KEEP=256
{
    [ -d "$sp" ] || mkdir -p "$sp" || exit 0
    t="$sp/.tmp.$$"
    {
        printf 'rp5deck-es-event=1\000event=%s\000argc=%s\000' "$EVENT" "$#"
        i=0
        for a in "$@"; do
            i=$((i + 1))
            printf 'arg%s=%s\000' "$i" "$a"
        done
    } > "$t" || { rm -f "$t"; exit 0; }
    # -- the ordering key (shell builtins only: no fork) --------------------
    ap=$$
    st=
    p=$PPID
    n=0
    set -f
    while [ "$n" -lt 32 ] && [ -r "/proc/$p/stat" ] && read -r l < "/proc/$p/stat"; do
        c=${l#*\(}
        c=${c%\)*}
        [ "$c" = sh ] || break
        set -- ${l##*\) }
        ap=$p
        st=${20}
        p=$2
        n=$((n + 1))
    done
    if [ "$ap" = "$$" ] && [ -r "/proc/$$/stat" ] && read -r l < "/proc/$$/stat"; then
        set -- ${l##*\) }
        st=${20}
    fi
    set +f
    k=
    for v in "$st:12" "$ap:10" "$$:10"; do
        w=${v##*:}
        v=${v%:*}
        case $v in ''|*[!0-9]*) v=0 ;; esac
        while [ "${#v}" -lt "$w" ]; do v=0$v; done
        k=$k-$v
    done
    mv -f "$t" "$sp/${k#-}.ev" || { rm -f "$t"; exit 0; }
    # -- bound the spool (only does work when nothing consumes it) ------------
    set -- "$sp"/[0-9]*.ev
    if [ "$#" -gt "$MAX" ]; then
        drop=$(($# - KEEP))
        total=$#
        i=0
        for x in "$@"; do
            i=$((i + 1))
            [ "$i" -le "$drop" ] && set -- "$@" "$x"
        done
        shift "$total"
        rm -f "$@"
    fi
} </dev/null >/dev/null 2>&1
exit 0
