#!/bin/sh
# install-es-hooks.sh - install (or remove) rp5deck's EmulationStation event
# hooks. Main runs it on the device during V4 (never run by the fix batches).
#
#   sh install-es-hooks.sh            name guard, then install the 8 hooks, then
#                                     self-test them through ES's own command line
#   sh install-es-hooks.sh --force    install even if the name guard objects
#   sh install-es-hooks.sh --remove   remove everything an install can create
#   sh install-es-hooks.sh --check    show what is installed + the guard, change nothing
#   options: --scripts-dir DIR   (default /storage/.config/emulationstation/scripts)
#            --spool-dir DIR     (default /var/run/rp5deck/es-events)
#            --rom-root DIR      (repeatable; NM2, 24 Sep: narrows the name guard to
#                                 systems under DIR - it now scans what ES's own
#                                 es_systems.cfg says it will show, not the old ROM
#                                 roots; default: every system, or DIR the old way if
#                                 es_systems.cfg cannot be read - see name_guard.py)
#            --es-systems-cfg FILE (name guard's ES system list; default: try
#                                 name_guard.py's DEFAULT_ES_SYSTEMS_CFG)
#            --gamelist-dir DIR  (repeatable; extra gamelist dirs for the guard)
#            --no-selftest
#   (directory arguments must not contain spaces)
#
# HOW ES RUNS HOOKS (Scripting.cpp executeScript + Platform.cpp, read from
# source): ASYNCHRONOUSLY, fire-and-forget, through `sh -c` - a hook cannot
# stall ES, but hooks can overlap. ES builds that command line itself: an
# argument is double-quoted ONLY if it contains a space, '"' is stripped and
# nothing is escaped, so the shell parses game names before the hook runs
# (RV1-M3). Hence, by the owner's decision of 23 Sep:
#   1. name_guard.py scans every gamelist <name>/<path> and ROM file name ES's
#      own es_systems.cfg says it will actually show (NM2, 24 Sep: no longer
#      the whole ROM tree - Steam's game data and a theme's assets are never
#      under a declared system path, so they can no longer be flagged or
#      block an install); if any value would be shell-parsed dangerously,
#      install REFUSES (exit 3), prints the entries, and leaves no rp5deck
#      hook installed. --force installs anyway (the entries are still
#      printed).
#   2. the self-test runs every installed hook through a line-by-line port of
#      executeScript()'s quoting plus Platform.cpp's `sh -c` wrapper, with
#      safe names (apostrophe + space, &amp; with spaces, non-ASCII), and
#      requires the spooled record to match byte for byte.
#
# Installs the SPLIT form only (scripts/<event>/rp5deck-<event>.sh). Files are
# staged in scripts/.rp5deck-stage.<pid>/ (a hidden DIRECTORY: ES runs neither
# its contents nor directories) and renamed into place, so an interrupted
# install never leaves a half-written or duplicate hook where ES looks.
# Any failure (copy, verify, self-test) rolls back: no rp5deck hook is left.
#
# ES only discovers event scripts at STARTUP (B9a): after installing or
# removing, restart ES with   systemctl restart essway   (never sway.service).
# This script does not restart anything itself. It never deletes a file it
# did not name (other scripts in the event directories are left alone).
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
APP=$(cd "$HERE/.." && pwd)
SRC="$APP/es-hooks"
BASE=/storage/.config/emulationstation/scripts
SPOOL=/var/run/rp5deck/es-events
LEGACY_RECORD=/var/run/rp5deck/es-event
EVENTS="game-selected system-selected game-start game-end screensaver-start screensaver-stop sleep wake"
ACTION=install
SELFTEST=1
FORCE=0
GUARD_ARGS=

while [ $# -gt 0 ]; do
    case "$1" in
        --remove) ACTION=remove ;;
        --check) ACTION=check ;;
        --force) FORCE=1 ;;
        --no-selftest) SELFTEST=0 ;;
        --scripts-dir) shift; BASE=${1:?--scripts-dir needs a directory} ;;
        --spool-dir) shift; SPOOL=${1:?--spool-dir needs a directory}
                     LEGACY_RECORD="${SPOOL%/*}/es-event" ;;
        --rom-root) shift; GUARD_ARGS="$GUARD_ARGS --root ${1:?--rom-root needs a directory}" ;;
        --es-systems-cfg) shift
                        GUARD_ARGS="$GUARD_ARGS --es-systems-cfg ${1:?--es-systems-cfg needs a file}" ;;
        --gamelist-dir) shift
                        GUARD_ARGS="$GUARD_ARGS --gamelist-dir ${1:?--gamelist-dir needs a directory}" ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

TMPD=
STAGE=
cleanup() {
    [ -n "$STAGE" ] && rm -rf "$STAGE"
    [ -n "$TMPD" ] && rm -rf "$TMPD"
    STAGE=
    TMPD=
}
trap 'cleanup; exit 130' INT TERM HUP
trap cleanup EXIT

mktmpd() {
    [ -n "$TMPD" ] && return 0
    TMPD=$(mktemp -d 2>/dev/null) || TMPD=
    if [ -z "$TMPD" ]; then
        TMPD="/tmp/rp5deck-inst.$$"
        rm -rf "$TMPD"
        mkdir -p "$TMPD"
    fi
}

# Remove every rp5deck hook (and every leftover of an interrupted install).
remove_hooks() {
    rc=0
    for ev in $EVENTS; do
        dst="$BASE/$ev/rp5deck-$ev.sh"
        if [ -e "$dst" ]; then
            if rm -f "$dst"; then echo "removed $dst"; else echo "FAILED to remove $dst" >&2; rc=1; fi
        fi
        for x in "$BASE/$ev"/rp5deck-"$ev".sh.tmp.*; do      # the pre-FX-B installer's temps
            [ -e "$x" ] || continue
            rm -f "$x" && echo "removed leftover $x"
        done
        [ -d "$BASE/$ev" ] && rmdir "$BASE/$ev" 2>/dev/null    # only if now empty
    done
    for x in "$BASE"/.rp5deck-stage.*; do
        [ -e "$x" ] || continue
        rm -rf "$x" && echo "removed leftover $x"
    done
    return $rc
}

# The name guard's JSON result as a short report.
render_guard() {
    python3 - "$1" <<'PYEOF'
import json, sys
try:
    r = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("name guard: unreadable result (%s)" % e); sys.exit(0)
print("name guard: scanned %d files and %d gamelists (%d entries) under %s"
      % (r.get("files_checked", 0), r.get("gamelists", 0), r.get("entries_checked", 0),
         ", ".join(r.get("roots") or []) or "nothing"))
if r.get("roots_missing"):
    print("name guard: not found: %s" % ", ".join(r["roots_missing"]))
f = r.get("findings") or []
for x in f[:100]:
    where = x["file"] + (":%d" % x["line"] if x.get("line") else "")
    print("  DANGEROUS %s [%s %s] %r: %s" % (where, x["source"], x["field"], x["value"],
                                            "; ".join(x["reasons"])))
if len(f) > 100:
    print("  ... and %d more (python3 name_guard.py --json for all)" % (len(f) - 100))
if r.get("error_count"):
    print("name guard: %d read errors, first: %s" % (r["error_count"], r["errors"][0]))
PYEOF
}

# run_guard -> GUARD_RC: 0 clean, 1 findings, 3 nothing scanned, else it is broken
run_guard() {
    mktmpd
    gj="$TMPD/name-guard.json"
    if ! command -v python3 >/dev/null 2>&1; then
        echo "name guard: python3 not found - cannot check the library" >&2
        GUARD_RC=127
        return 0
    fi
    # GUARD_ARGS is a list of options: split on purpose
    python3 "$APP/name_guard.py" --json $GUARD_ARGS > "$gj" 2> "$TMPD/name-guard.err"
    GUARD_RC=$?
    [ -s "$gj" ] && render_guard "$gj"
    [ -s "$TMPD/name-guard.err" ] && sed 's/^/name guard: /' "$TMPD/name-guard.err" >&2
    return 0
}

fail=0
case "$ACTION" in
check)
    for ev in $EVENTS; do
        dst="$BASE/$ev/rp5deck-$ev.sh"
        if [ -f "$dst" ]; then
            if cmp -s "$SRC/rp5deck-$ev.sh" "$dst"; then s="installed (matches)"; else s="installed (DIFFERS from $SRC)"; fi
            [ -x "$dst" ] || s="$s, NOT executable"
        else
            s="absent"
        fi
        echo "$ev: $s"
    done
    for x in "$BASE"/.rp5deck-stage.* "$BASE"/*/rp5deck-*.sh.tmp.*; do
        [ -e "$x" ] && echo "leftover from an interrupted install: $x (--remove deletes it)"
    done
    n=0
    for x in "$SPOOL"/[0-9]*.ev; do [ -e "$x" ] && n=$((n + 1)); done
    echo "spool $SPOOL: $n pending event file(s)"
    run_guard
    case $GUARD_RC in
        0) echo "name guard: OK" ;;
        1) echo "name guard: DANGEROUS names found (install would refuse)" ;;
        3) echo "name guard: nothing scanned (install would refuse)" ;;
        *) echo "name guard: could not run (rc=$GUARD_RC; install would refuse)" ;;
    esac
    exit 0
    ;;
remove)
    remove_hooks || fail=1
    for x in "$SPOOL"/[0-9]*.ev "$SPOOL"/.tmp.* "$LEGACY_RECORD" "${LEGACY_RECORD%/*}"/.es-event.*; do
        [ -e "$x" ] && rm -f "$x"
    done
    [ -d "$SPOOL" ] && rmdir "$SPOOL" 2>/dev/null && echo "removed $SPOOL"
    rmdir "${SPOOL%/*}" 2>/dev/null                    # only if empty (the app may use it)
    rmdir "$BASE" 2>/dev/null && echo "removed empty $BASE"
    echo "Restart ES for this to take effect: systemctl restart essway"
    exit $fail
    ;;
esac

# -- install ---------------------------------------------------------------
for ev in $EVENTS; do
    if [ ! -f "$SRC/rp5deck-$ev.sh" ]; then
        echo "missing $SRC/rp5deck-$ev.sh" >&2
        exit 1
    fi
done
for x in "$BASE"/.rp5deck-stage.* "$BASE"/*/rp5deck-*.sh.tmp.*; do   # an interrupted install
    [ -e "$x" ] || continue
    rm -rf "$x" && echo "removed leftover $x"
done

run_guard
if [ "$GUARD_RC" != 0 ]; then
    case $GUARD_RC in
        1) why="the name guard found game names/paths that ES would shell-parse (listed above)" ;;
        3) why="the name guard found no ROM root to scan, so it cannot say the library is safe" ;;
        *) why="the name guard could not run (rc=$GUARD_RC)" ;;
    esac
    if [ "$FORCE" = 1 ]; then
        echo "WARNING: $why. --force given: installing anyway."
    else
        echo "REFUSED: $why." >&2
        echo "ES passes these values through 'sh -c' with naive quoting (RV1-M3): '\$' and" >&2
        echo "backticks run as root, and & ( ) ; | < > ' break the command. Rename the files" >&2
        echo "or fix the gamelist names, or pass --force to install anyway." >&2
        remove_hooks
        echo "RESULT: REFUSED (no rp5deck hook is installed)"
        exit 3
    fi
fi

mkdir -p "$BASE" || { echo "cannot create $BASE" >&2; exit 1; }
STAGE="$BASE/.rp5deck-stage.$$"
rm -rf "$STAGE"
mkdir "$STAGE" || { echo "cannot create $STAGE" >&2; exit 1; }
for ev in $EVENTS; do
    cp "$SRC/rp5deck-$ev.sh" "$STAGE/rp5deck-$ev.sh" && chmod 755 "$STAGE/rp5deck-$ev.sh" \
        && cmp -s "$SRC/rp5deck-$ev.sh" "$STAGE/rp5deck-$ev.sh" \
        || { echo "FAILED to stage rp5deck-$ev.sh" >&2; fail=1; break; }
done
if [ "$fail" = 0 ]; then
    for ev in $EVENTS; do
        dir="$BASE/$ev"
        dst="$dir/rp5deck-$ev.sh"
        mkdir -p "$dir" && mv -f "$STAGE/rp5deck-$ev.sh" "$dst" && cmp -s "$SRC/rp5deck-$ev.sh" "$dst" \
            || { echo "FAILED $dst" >&2; fail=1; break; }
        echo "installed $dst"
    done
fi
rm -rf "$STAGE"
STAGE=

# -- self-test ---------------------------------------------------------------
# es_cmd SCRIPT [ARG1 [ARG2 [ARG3]]] -> $ES_CMD: a line-by-line port of
# ROCKNIX/emulationstation-next es-core/src/Scripting.cpp executeScript():
#   command = script, in "..." if it contains a space; then for arg1..arg3:
#   stop at the first EMPTY one; if it starts with '"', drop its first and last
#   character; strip every '"'; append " \"data\"" if it contains a space,
#   else " data". Nothing is escaped.
es_cmd() {
    _s=$1
    case $_s in *" "*) ES_CMD="\"$_s\"" ;; *) ES_CMD=$_s ;; esac
    for _a in "${2-}" "${3-}" "${4-}"; do
        [ -n "$_a" ] || break
        case $_a in '"'*) _a=${_a#?}; _a=${_a%?} ;; esac
        _d=
        while :; do
            case $_a in
                *'"'*) _d=$_d${_a%%\"*}; _a=${_a#*\"} ;;
                *) _d=$_d$_a; break ;;
            esac
        done
        case $_d in *" "*) ES_CMD="$ES_CMD \"$_d\"" ;; *) ES_CMD="$ES_CMD $_d" ;; esac
    done
}
# es_run LOGDIR: Platform.cpp ProcessStartInfo::run()'s wrapper around the
# command (as quoted in review/RV1-findings.md), handed to /bin/sh -c exactly
# as ES's execl does. ES does not wait for it; the self-test does.
es_run() {
    sh -c "(((($ES_CMD 2> $1/es_script_stderr.log ; echo \$? >&3) | head -300 > $1/es_script_stdout.log) 3>&1) | (read xs; exit \$xs))"
}
now_ns() {      # empty if this date has no %N (busybox without FEATURE_DATE_NANO)
    _t=$(date +%s%N 2>/dev/null) || _t=
    case $_t in ''|*[!0-9]*) _t= ;; esac
    echo "$_t"
}

if [ "$SELFTEST" = 1 ] && [ "$fail" = 0 ]; then
    mktmpd
    J="Jimmy White's Whirlwind Snooker"
    JR="/storage/roms/megadrive/$J (Europe).md"
    R="Ren &amp; Stimpy Show Presents"
    RR="/storage/roms/snes/Ren & Stimpy Show, The (USA).sfc"
    P="Pokémon Edición Azul (ポケモン)"
    PR="/storage/roms/gb/Pokémon Azul.gb"
    i=0
    # event|label|arg1|arg2|arg3   (every value is SAFE by the name guard's rules)
    for spec in \
        "game-selected|apostrophe+space|megadrive|$JR|$J" \
        "game-selected|&amp; with spaces|snes|$RR|$R" \
        "game-selected|non-ASCII|gb|$PR|$P" \
        "game-selected|no spaces|gb|/storage/roms/gb/Tetris.gb|Tetris" \
        "system-selected|system only|megadrive||" \
        "game-start|apostrophe+space|$JR|$J (Europe).md|$J" \
        "game-start|non-ASCII|$PR|Pokémon Azul.gb|$P" \
        "game-end|&amp; with spaces|$RR|Ren & Stimpy Show, The (USA).sfc|$R" \
        "game-end|no spaces|/storage/roms/gb/Tetris.gb|Tetris.gb|Tetris" \
        "screensaver-start|no args|||" \
        "screensaver-stop|no args|||" \
        "sleep|no args|||" \
        "wake|no args|||"
    do
        i=$((i + 1))
        ev=${spec%%|*}; rest=${spec#*|}
        label=${rest%%|*}; rest=${rest#*|}
        a1=${rest%%|*}; rest=${rest#*|}
        a2=${rest%%|*}; a3=${rest#*|}
        sp="$TMPD/c$i"
        ld="$TMPD/l$i"
        mkdir -p "$sp" "$ld"
        {   # the expected record: ES stops at the first empty argument
            printf '%s\000%s\000' "$ev" "$label"
            for a in "$a1" "$a2" "$a3"; do [ -n "$a" ] || break; printf '%s\000' "$a"; done
        } > "$TMPD/c$i.want"
        es_cmd "$BASE/$ev/rp5deck-$ev.sh" "$a1" "$a2" "$a3"
        t0=$(now_ns)
        RP5DECK_ES_SPOOL="$sp" es_run "$ld"
        rc=$?
        t1=$(now_ns)
        us=-
        [ -n "$t0" ] && [ -n "$t1" ] && us=$(( (t1 - t0) / 1000 ))
        noisy=0
        [ -s "$ld/es_script_stderr.log" ] || [ -s "$ld/es_script_stdout.log" ] && noisy=1
        echo "$rc $us $noisy" > "$TMPD/c$i.rc"
    done
    out=$(RP5DECK_APP="$APP" python3 - "$TMPD" "$i" <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["RP5DECK_APP"])
import esevents
tmp, n = sys.argv[1], int(sys.argv[2])
bad = 0
for i in range(1, n + 1):
    want = open(os.path.join(tmp, "c%d.want" % i), "rb").read().split(b"\0")[:-1]
    want = [w.decode("utf-8", "surrogateescape") for w in want]
    ev, label, args = want[0], want[1], tuple(want[2:])
    rc, us, noisy = open(os.path.join(tmp, "c%d.rc" % i)).read().split()
    sp = os.path.join(tmp, "c%d" % i)
    names = sorted(os.listdir(sp))
    evs = [x for x in names if esevents.spool_key(x)]
    other = [x for x in names if x not in evs]
    why = []
    if rc != "0":
        why.append("exit %s" % rc)
    if noisy != "0":
        why.append("the hook printed something")
    if len(evs) != 1:
        why.append("%d spool files" % len(evs))
    if other:
        why.append("left behind %r" % other)
    if len(evs) == 1:
        with open(os.path.join(sp, evs[0]), "rb") as f:
            data = f.read()
        e = esevents.parse_record(data)
        if e is None:
            why.append("unparsable record")
        elif e.kind != ev or e.args != args:
            why.append("got %s %r, want %s %r" % (e.kind, e.args, ev, args))
        elif data != esevents.build_record(ev, list(args)):
            why.append("record bytes differ")
    cost = "" if us == "-" else " (%s us)" % us
    if why:
        bad += 1
        print("selftest %s [%s]: FAIL %s" % (ev, label, "; ".join(why)))
    else:
        print("selftest %s [%s]: PASS via ES's sh -c quoting, args=%r%s" % (ev, label, args, cost))
sys.exit(1 if bad else 0)
PYEOF
)
    prc=$?
    echo "$out"
    [ "$prc" = 0 ] || fail=1
fi

if [ "$fail" != 0 ]; then
    echo "rolling back: removing every rp5deck hook" >&2
    remove_hooks
fi
echo
echo "ES only reads event scripts at startup. To activate: systemctl restart essway"
[ "$fail" = 0 ] && echo "RESULT: OK" || echo "RESULT: FAILED"
exit $fail
