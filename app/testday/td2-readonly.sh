#!/bin/sh
# Step 2: every read-only device check the agents asked for, in one run.
# Writes only under /tmp and /storage/rp5deck-i2/td2/ (its own output).
# Runs Firefox HEADLESS once (HF1 P1: no window, closed again). Nothing else
# starts, stops or changes anything.
#   sh /storage/rp5deck-i2/rp5deck/testday/td2-readonly.sh
. "$(dirname "$0")/td-common.sh"
td_sanity
td_sway
OUT=/storage/rp5deck-i2/td2
mkdir -p "$OUT"
cd "$BUILD" || exit 1

h() { echo; echo "=== $* ==="; }

h "state before anything (094 installed? app running? flag)"
ls -la "$AUTOSTART"/09* 2>/dev/null
echo "080 flag: $(cat "$FLAG_FILE" 2>/dev/null)"
echo "/storage/rp5deck exists: $([ -d "$HOME_DIR" ] && echo yes || echo no)"
[ -f "$HOME_DIR/config.json" ] && echo "  it has a config.json (step 3 carries it over)"
echo "092 installed md5: $(md5sum "$AUTOSTART/dual-screen-layout-and-power" 2>/dev/null)"
echo "rp5deck main.py processes: $(td_pids 'rp5deck/main.py' | tr '\n' ' ')"
echo "focus_guard processes: $(td_pids 'focus_guard.py' | tr '\n' ' ')"
echo "ES pid: $(pidof emulationstation)"

h "ES hooks (install-es-hooks.sh --check; changes nothing)"
sh tools/install-es-hooks.sh --check 2>&1 | tail -15

h "name guard summary (the NM1 input)"
python3 -B name_guard.py --json > "$OUT/guard.json" 2>"$OUT/guard.err"
echo "rc=$? -> $OUT/guard.json ($(wc -c < "$OUT/guard.json") bytes)"
python3 -B -c "import json,sys; d=json.load(open(sys.argv[1])); print('findings:', len(d.get('findings', d) if isinstance(d, dict) else d))" "$OUT/guard.json" 2>&1

h "CC1: kill-combo file, emukill helper, PowerSaverMode, touch state"
echo "batocera-es-swissknife: $(command -v batocera-es-swissknife || echo 'absent (emukill is a no-op; cleanstate falls back)')"
echo "/tmp/.process-kill-data: $(cat /tmp/.process-kill-data 2>/dev/null || echo absent)"
grep -o 'PowerSaverMode" value="[a-z]*' /storage/.config/emulationstation/es_settings.cfg 2>/dev/null || echo "PowerSaverMode: not set (default)"
swaymsg -t get_inputs -r > /tmp/td2-inputs.json 2>/dev/null
python3 -B -c "
import json,sys
for d in json.load(open('/tmp/td2-inputs.json')):
    if 'ft5x06' in d.get('identifier','') or 'ft5x06' in d.get('name',''):
        print('touch', d.get('identifier'), 'send_events:', (d.get('libinput') or {}).get('send_events'))
" 2>&1

h "CC1: es_health (read-only, ~5 s)"
python3 -B es_health.py 2>&1 | tail -8

h "CC1: cleanstate plan (plan only, stops nothing)"
python3 -B cleanstate.py plan 2>&1 | tail -15

h "CC6 C1: window_switcher list (read-only)"
python3 -B window_switcher.py list 2>&1 | tail -15
h "CC6 C2: window_switcher switch internal (DRY RUN: prints, moves nothing)"
python3 -B window_switcher.py switch internal 2>&1 | tail -8

h "HF1 P2: wvkbd"
command -v wvkbd-mobintl || echo "wvkbd-mobintl: ABSENT (the Firefox keyboard falls back to the Keyboard button only)"
h "HF1 P1: page probe in headless Firefox (needs port 2828 free)"
RP5DECK_BROWSER_HEADLESS=1 timeout 120 python3 -B tools/hf1_probe_check.py 2>&1 | tail -12

h "CC7: which FN, key.dpad.events (key.* lines ONLY - never the whole system.cfg)"
grep -E '^key\.(dpad|function|hotkey|touchscreen)' "$SYSCFG" 2>/dev/null || echo "no key.dpad/function/hotkey/touchscreen lines (ROCKNIX defaults apply: dpad events OFF)"
grep -n 'dpad.events' /usr/bin/input_sense 2>/dev/null | head -5

h "CC4: grim output format on DP-1 (the sampler needs 8-bit non-interlaced PNG)"
if command -v grim >/dev/null; then
    grim -t png -o DP-1 /tmp/td2-dp1.png 2>&1 && python3 -B -c "
import struct,sys
b=open('/tmp/td2-dp1.png','rb').read(33)
w,h,depth,ctype,_,_,inter=struct.unpack('>IIBBBBB',b[16:29])
print('PNG %dx%d depth=%d colortype=%d interlace=%d -> %s' % (w,h,depth,ctype,inter,'OK' if depth==8 and ctype in (2,6) and inter==0 else 'UNSUPPORTED by theme_colour'))
"
    rm -f /tmp/td2-dp1.png
else
    echo "grim: ABSENT (CC4 'sample' source unusable; 'theme' default unaffected)"
fi

h "melonDS / Steam facts for later steps"
grep -n 'Screen1' /storage/.config/melonDS/melonDS.ini 2>/dev/null || echo "melonDS.ini: no Screen1 keys"
ls -la /usr/bin/start_steam_arm64.sh 2>/dev/null
echo
echo "RESULT: read-only checks done; output also in $OUT"
