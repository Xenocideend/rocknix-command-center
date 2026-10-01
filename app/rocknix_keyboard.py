"""rocknix_keyboard: toggles ROCKNIX's own on-screen keyboard, so you can type into emulator
settings from the Command Center.

There's no on-screen keyboard while a game or emulator has focus. rp5deck's osk.py only runs
wvkbd-mobintl for Firefox (`-L <height> --output <name>`), a different process that's never
touched here.

On the device, ROCKNIX runs `touchkeyboard.service` -> `/usr/bin/rocknix-touchscreen-keyboard`,
a loop that, while `rocknix.touchscreen-keyboard.enabled == 1` (ES's "ENABLE TOUCHSCREEN
KEYBOARD"), runs
    wvkbd-mobintl ... -l simple --hidden
in the foreground (with DEVICE_HAS_DUAL_SCREEN=false pinned here, no `--output` and no
`-H 400`). If the loop finds a wvkbd-mobintl already running it killalls it, so this never
starts or stops that process, it only signals the one the service owns. input_sense's
FN(Home)+BTN_TOUCH toggle sends it `kill -34 $(pidof wvkbd-mobintl)`, SIGRTMIN+0 on this
glibc. The literal 34 is used, not signal.SIGRTMIN, since that constant isnt guaranteed to be
34 elsewhere and this only has to match what input_sense sends.

Matching the binary name isnt enough, since osk.py starts a wvkbd-mobintl too (for Firefox,
other flags, no --hidden). find_pid() only takes a /proc entry whose cmdline names the same
binary and has both `--hidden` and `-l simple` (as an adjacent pair), exactly ROCKNIX's own
service command. toggle() then signals only that pid, and nothing here ever starts, stops or
kills anything else.
"""
import logging
import os

log = logging.getLogger("rp5deck.rocknix_keyboard")

WVKBD_BIN = "wvkbd-mobintl"
TOGGLE_SIGNAL = 34  # SIGRTMIN+0 on this device, input_sense's own `kill -34`


def _cmdline_tokens(proc_dir, pid):
    path = os.path.join(proc_dir, pid, "cmdline")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return [t.decode("utf-8", "replace") for t in raw.split(b"\x00") if t]


def _is_rocknix_keyboard(tokens):
    if not tokens:
        return False
    if WVKBD_BIN not in os.path.basename(tokens[0]):
        return False
    if "--hidden" not in tokens:
        return False
    for i, t in enumerate(tokens[:-1]):
        if t == "-l" and tokens[i + 1] == "simple":
            return True
    return False


def find_pid(proc_dir="/proc"):
    """The pid of ROCKNIX's own touchkeyboard.service wvkbd-mobintl, or None if it isnt running (the
    ES setting is off, or the service hasnt started it yet). Never raises, a /proc entry that
    vanishes mid-scan or cant be read is just skipped.
    """
    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        tokens = _cmdline_tokens(proc_dir, name)
        if _is_rocknix_keyboard(tokens):
            return int(name)
    return None


def toggle(proc_dir="/proc", kill_fn=None):
    """Sends TOGGLE_SIGNAL to ROCKNIX's keyboard process if it's running. True if a signal was sent,
    False if there was no such process (the caller shows a hint, see main.py App.toggle_keyboard())
    or the signal failed (logged, not raised, a pid vanishing between find_pid() and the signal is
    fine).
    """
    kill_fn = kill_fn or os.kill
    pid = find_pid(proc_dir)
    if pid is None:
        return False
    try:
        kill_fn(pid, TOGGLE_SIGNAL)
    except OSError as e:
        log.warning("rocknix_keyboard: could not signal pid %d: %s", pid, e)
        return False
    return True
