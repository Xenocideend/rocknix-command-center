#!/bin/sh
# Step 11: CC1's freeze test. ES is stopped with SIGSTOP, checked, and
# continued again. Keep `sh td11-es-freeze.sh cont` ready: never leave ES stopped.
#   sh td11-es-freeze.sh stop     SIGSTOP ES (the owner then taps Clean state)
#   sh td11-es-freeze.sh health   es_health.py (expect FROZEN while stopped)
#   sh td11-es-freeze.sh cont     SIGCONT ES (always safe to run)
#   sh td11-es-freeze.sh pids     ES / sway / rp5deck pids (before vs after a restart)
. "$(dirname "$0")/td-common.sh"
cd "$BUILD" || exit 1
ES=$(pidof emulationstation)
case "${1:-pids}" in
stop)
    td_sanity
    [ -n "$ES" ] || { echo "ES not running"; exit 2; }
    kill -STOP $ES && echo "ES $ES stopped (SIGSTOP). Undo: sh $0 cont"
    ;;
health)
    python3 -B es_health.py 2>&1 | tail -8 ;;
cont)
    [ -n "$ES" ] && kill -CONT $ES && echo "ES $ES continued (SIGCONT)"
    grep State /proc/$ES/status 2>/dev/null
    ;;
pids)
    echo "emulationstation: $ES"
    echo "sway: $(pidof sway)"
    echo "rp5deck main.py: $(td_pids "$HOME_DIR/main.py" | tr '\n' ' ')"
    ;;
*)
    echo "usage: $0 stop | health | cont | pids"; exit 2 ;;
esac
