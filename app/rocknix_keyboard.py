"""rocknix_keyboard - toggle ROCKNIX's own on-screen keyboard (owner
request: a Command Center control to "type into emulator settings").

There is no on-screen keyboard while a game or emulator has focus (rp5deck's
own osk.py only ever drives wvkbd-mobintl FOR FIREFOX, with `-L <height>
--output <name>`, drawn on the layer surface - a completely different
process from this module's target and never touched here).

Device facts (read on the device 24 Sep 2026):
  ROCKNIX runs `touchkeyboard.service` -> `/usr/bin/rocknix-touchscreen-
  keyboard`, a loop: while `rocknix.touchscreen-keyboard.enabled == 1` (an
  ES menu toggle, "ENABLE TOUCHSCREEN KEYBOARD"), each iteration runs
      wvkbd-mobintl ... -l simple --hidden
  in the foreground (with DEVICE_HAS_DUAL_SCREEN=false pinned on this
  device, that is no `--output`, no `-H 400`). Caution: if the loop finds a
  wvkbd-mobintl ALREADY running when it iterates, it `killall`s it - so this
  module must never start or stop that process itself, only ever signal the
  one instance the service already owns.
  input_sense's own FN(BTN_MODE=Home)+BTN_TOUCH toggle sends it
  `kill -34 $(pidof wvkbd-mobintl)` - SIGRTMIN+0 on this device's glibc
  (used here as the literal signal number 34, not the symbolic
  `signal.SIGRTMIN`, since that constant's value is not guaranteed to be 34
  off-device and this module only ever needs to match what input_sense
  itself already sends).

Identifying THE keyboard process: matching on the binary name alone is not
enough - rp5deck's own osk.py starts a wvkbd-mobintl too (for Firefox, with
different flags and no --hidden), and two rp5deck processes could plausibly
run at once. find_pid() only accepts a /proc entry whose cmdline names the
same binary AND carries both `--hidden` and `-l simple` (an adjacent
`"-l", "simple"` pair, not just "-l" and "simple" appearing anywhere) -
exactly ROCKNIX's own service invocation. toggle() then signals ONLY that
one pid; nothing here ever starts, stops or kills any other process."""
import logging
import os

log = logging.getLogger("rp5deck.rocknix_keyboard")

WVKBD_BIN = "wvkbd-mobintl"
TOGGLE_SIGNAL = 34              # SIGRTMIN+0 on this device: input_sense's own `kill -34`


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
    """The pid of ROCKNIX's own touchkeyboard.service wvkbd-mobintl, or
    None if it is not running (the ES setting is off, or the service has
    not started it yet). Never raises: a /proc entry that disappears mid-
    scan (a process exiting) or one this user cannot read is just skipped."""
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
    """Send TOGGLE_SIGNAL to ROCKNIX's own keyboard process if it is
    running. Returns True if a signal was sent, False if no such process
    was found (the caller shows a hint in that case - see main.py
    App.toggle_keyboard()) or the signal failed to send (logged, not
    raised: a stale pid disappearing between find_pid() and the signal is
    not this module's problem to solve)."""
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
