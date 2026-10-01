#!/usr/bin/env python3
"""The Command Center's summon button reader, and the pull-down state machine that decides when
the Command Center is open. Stdlib only.

Part 1: a background evdev reader that never grabs
----------------------------------------------------
Watches the one button set in command_center.hardware_button ("btn_back_f1",
"btn_c_paddle", "btn_z_paddle" or "none") and calls back on a debounced press. It:

  - opens devices by name (looked up again in /proc/bus/input/devices on every connect),
    never a fixed /dev/input/eventN, since the numbers change across reboots and hotplug
  - never grabs or writes anything, it only reads
  - costs about 0 CPU idle (it blocks in select())
  - survives hotplug: EOF or an OSError means the device is gone, and it goes back to
    finding it by name with a backoff
  - stops cleanly from another thread (a socketpair wakes the select(), a pipe would hang
    the PC tests since Windows select() only takes sockets)

input_event on aarch64 is 24 bytes: timeval (2 x 8-byte long), u16 type, u16 code,
s32 value. INPUT_EVENT_FORMAT uses '=' so the size is exact, not assumed.

What each binding arrives as (read on the RP5):

  btn_back_f1  -> KEY_F1 (59) on "InputPlumber Keyboard". InputPlumber's active
                  capability map (retroid_mcu.yaml) maps BTN_BACK to KeyF1, and the
                  virtual keyboard advertises key 59.
  btn_c_paddle -> BTN_TRIGGER_HAPPY4 (707), right paddle
  btn_z_paddle -> BTN_TRIGGER_HAPPY3 (706), left paddle
                  both on "Sony Interactive Entertainment DualSense Wireless Controller"
                  (match the name exactly, the Motion Sensors / Touchpad siblings share
                  the prefix).

The paddle codes only show up when InputPlumber emulates a DualSense Edge: the kernel's
hid-playstation only maps the paddle bits to BTN_TRIGGER_HAPPY1-4 for the Edge's product
id, and this RP5's profile uses the plain "ds5" target. So today a paddle press sends
nothing. BTN_TL2/BTN_TR2 are live but thats the analog triggers fully pressed, so they
would false-fire in games and arent used. The RP5 has no rear paddles anyway, which is why
Back is the default.

Part 2: the pull-down state machine
------------------------------------
Pure logic with an injected clock and no device access. States are COMPANION (default)
and COMMAND_CENTER, with SETTINGS as a sheet inside the Command Center. Every event is a
method call (swipe_down_from_top(), swipe_up(), back_tap(), summon_button(), open_gear(),
mode_changed(mode), tick(now)), so tests drive it without any UI.
"""
import collections
import errno
import select
import socket
import struct
import time

# --------------------------------------------------------------------------
# Part 1: evdev event parsing and the button reader
# --------------------------------------------------------------------------

DEVICES_PATH = "/proc/bus/input/devices"
EVENT_DIR = "/dev/input"

EV_KEY = 0x01

# see the module docstring for where each of these comes from
KEY_F1_CODE = 0x3B  # 59, InputPlumber Keyboard: the RP5 Back button
BTN_C_PADDLE_CODE = 0x2C3  # 707, BTN_TRIGGER_HAPPY4: the kernel's right paddle code
BTN_Z_PADDLE_CODE = 0x2C2  # 706, BTN_TRIGGER_HAPPY3: the kernel's left paddle code

DS5_NAME = "Sony Interactive Entertainment DualSense Wireless Controller"
KEYBOARD_NAME = "InputPlumber Keyboard"

# binding name (command_center.hardware_button) -> (device name, evdev code)
#
# A paddle binding watches the DualSense, which wakes Python on every stick and trigger event
# of the running game (about 1.7% of a core at 2000 events/s) and risks the evdev buffer
# overflowing if the thread gets starved. _resolve() only opens the one device the binding
# names, so the default (Back, on "InputPlumber Keyboard") stays silent during play.
BINDING_TARGETS = {
    "btn_back_f1": (KEYBOARD_NAME, KEY_F1_CODE),
    "btn_c_paddle": (DS5_NAME, BTN_C_PADDLE_CODE),
    "btn_z_paddle": (DS5_NAME, BTN_Z_PADDLE_CODE),
}

# timeval (long, long) + u16 type + u16 code + s32 value. '=' means native byte order with
# standard sizes, so its always 24 bytes on a 64-bit time_t kernel.
INPUT_EVENT_FORMAT = "=qqHHi"
INPUT_EVENT_STRUCT = struct.Struct(INPUT_EVENT_FORMAT)
INPUT_EVENT_SIZE = INPUT_EVENT_STRUCT.size
assert INPUT_EVENT_SIZE == 24, INPUT_EVENT_SIZE

InputEvent = collections.namedtuple("InputEvent", "sec usec type code value")

SummonEvent = collections.namedtuple(
    "SummonEvent", "binding device_name device_path code value ts sec usec")


def parse_event(data):
    """Unpacks 24 raw bytes from /dev/input/eventN into an InputEvent. Raises ValueError if its
    not exactly one event.
    """
    if len(data) != INPUT_EVENT_SIZE:
        raise ValueError("expected %d bytes, got %d" % (INPUT_EVENT_SIZE, len(data)))
    return InputEvent(*INPUT_EVENT_STRUCT.unpack(data))


def parse_proc_input_devices(text):
    """Parses /proc/bus/input/devices into [{"name": str, "handlers": [str, ...]}], one per device
    block.
    """
    devices = []
    cur = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                devices.append(cur)
                cur = {}
            continue
        if line.startswith("N: Name="):
            val = line.split("=", 1)[1].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            cur["name"] = val
        elif line.startswith("H: Handlers="):
            cur["handlers"] = line.split("=", 1)[1].split()
    if cur:
        devices.append(cur)
    return devices


def find_event_path(name, text=None, devices_path=DEVICES_PATH, event_dir=EVENT_DIR):
    """"/dev/input/eventN" for the device whose Name= is exactly `name` (not a prefix, several
    InputPlumber devices share one), or None if its not there. Reads the file every call so a
    hotplug renumbering is picked up. `text` replaces reading devices_path, for tests.
    """
    if text is None:
        try:
            with open(devices_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None
    for dev in parse_proc_input_devices(text):
        if dev.get("name") == name:
            for h in dev.get("handlers", ()):
                if h.startswith("event"):
                    return event_dir + "/" + h
    return None


def _read_exact(fd, n):
    """Reads exactly n bytes, or fewer at EOF. Never raises on a short read, the caller decides
    what that means.
    """
    buf = b""
    while len(buf) < n:
        chunk = fd.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


class SummonButtonReader:
    """Non-grabbing reader for one summon binding (call run() from a thread).

    binding: one of BINDING_TARGETS' keys, or "none" (watches nothing, run() idles until stop()).

    on_summon(SummonEvent) is called on each debounced press (value == 1), at most once per
    `debounce_s`. Autorepeat and release never call it.

    on_any_key(InputEvent, device_path, device_name), if given, gets every EV_KEY event on the
    watched device, a diagnostic hook for tools/summon-watch.py.

    find_event_path_fn/open_fn/select_fn/clock can be swapped in for tests.
    """

    def __init__(self, binding, on_summon=None, on_any_key=None, log=None,
                 debounce_s=0.3, rescan_idle_s=2.0, backoff_s=(0.5, 1.0, 2.0, 5.0),
                 find_event_path_fn=find_event_path, open_fn=open,
                 select_fn=select.select, clock=time.monotonic):
        self._binding = binding
        self._on_summon = on_summon
        self._on_any_key = on_any_key
        self._log = log
        self._debounce_s = debounce_s
        self._rescan_idle_s = rescan_idle_s
        self._backoff_s = backoff_s
        self._find_event_path = find_event_path_fn
        self._open_fn = open_fn
        self._select_fn = select_fn
        self._clock = clock
        self._last_press = None
        self._stop = False
        # A socketpair not os.pipe(): Windows select() (the PC tests) only takes sockets, so a pipe
        # would hang every test that uses the real select.
        self._wake_r, self._wake_w = socket.socketpair()

    def _logmsg(self, msg):
        if self._log:
            self._log(msg)

    def stop(self):
        """Asks run() to return. Safe from another thread, it wakes a blocked select() right away."""
        if self._stop:
            return
        self._stop = True
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def close(self):
        for sock in (self._wake_r, self._wake_w):
            try:
                sock.close()
            except OSError:
                pass

    # -- resolution -------------------------------------------------------

    def _resolve(self):
        if self._binding == "none":
            return None
        target = BINDING_TARGETS.get(self._binding)
        if target is None:
            self._logmsg("summon: unknown binding %r" % (self._binding,))
            return None
        device_name, code = target
        path = self._find_event_path(device_name)
        if path is None:
            return None
        return path, code, device_name

    def _wait_or_stop(self, timeout):
        """Blocks up to `timeout` seconds (None = forever) for a stop request. True if stop() was
        called, False if it timed out.
        """
        r, _, _ = self._select_fn([self._wake_r], [], [], timeout)
        if self._wake_r in r:
            try:
                self._wake_r.recv(4096)
            except OSError:
                pass
            return True
        return False

    # -- main loop ----------------------------------------------------------

    def run(self):
        """Blocks until stop() is called from another thread. About 0 CPU idle: every wait is a
        select() with no timeout or a bounded backoff, never a busy loop.
        """
        backoff_i = 0
        while not self._stop:
            target = self._resolve()
            if target is None:
                if self._wait_or_stop(self._rescan_idle_s):
                    break
                continue
            path, code, device_name = target
            try:
                fd = self._open_fn(path, "rb", buffering=0)
            except OSError as e:
                self._logmsg("summon: open failed for %s: %s" % (path, e))
                delay = self._backoff_s[min(backoff_i, len(self._backoff_s) - 1)]
                backoff_i += 1
                if self._wait_or_stop(delay):
                    break
                continue
            backoff_i = 0
            try:
                self._read_loop(fd, path, code, device_name)
            except OSError as e:
                self._logmsg("summon: %s went away (%s), rescanning" % (path, e))
            finally:
                try:
                    fd.close()
                except OSError:
                    pass

    def _read_loop(self, fd, path, code, device_name):
        while not self._stop:
            r, _, _ = self._select_fn([fd, self._wake_r], [], [], None)
            if self._wake_r in r:
                try:
                    self._wake_r.recv(4096)
                except OSError:
                    pass
                return
            if fd in r:
                data = _read_exact(fd, INPUT_EVENT_SIZE)
                if not data:
                    raise OSError(errno.ENODEV, "read EOF on %s" % path)
                if len(data) != INPUT_EVENT_SIZE:
                    continue  # short read, wait for the next one
                ev = parse_event(data)
                self._handle_event(ev, path, code, device_name)

    def _handle_event(self, ev, path, code, device_name):
        if ev.type != EV_KEY:
            return
        if self._on_any_key:
            self._on_any_key(ev, path, device_name)
        if ev.value == 2:
            return  # autorepeat, never a fresh press
        if ev.code != code or ev.value != 1:
            return  # not our code, or a release
        now = self._clock()
        if self._last_press is not None and (now - self._last_press) < self._debounce_s:
            return
        self._last_press = now
        if self._on_summon:
            self._on_summon(SummonEvent(binding=self._binding, device_name=device_name,
                                         device_path=path, code=code, value=ev.value,
                                         ts=now, sec=ev.sec, usec=ev.usec))


# --------------------------------------------------------------------------
# Part 2: the pull-down state machine
# --------------------------------------------------------------------------

# swipe_sensitivity -> kwargs for ui.SwipeRecognizer. Higher sensitivity means a bigger
# catch zone at the top and less travel. "medium" matches SwipeRecognizer's own defaults
# (edge=60, distance=120).
SWIPE_SENSITIVITY_PARAMS = {
    "low": {"edge": 40, "distance": 160},
    "medium": {"edge": 60, "distance": 120},
    "high": {"edge": 90, "distance": 80},
}


def swipe_recognizer_kwargs(sensitivity):
    """kwargs for ui.SwipeRecognizer(height_fn, **kwargs) for a swipe_sensitivity value. An unknown
    value falls back to "medium" so a bad settings file cant break the gesture.
    """
    return dict(SWIPE_SENSITIVITY_PARAMS.get(sensitivity, SWIPE_SENSITIVITY_PARAMS["medium"]))


class PullDownStateMachine:
    """The summon and close logic. Pure state plus an injected clock, no device, widget or IPC.

    States:
      COMPANION       default view (game art/video)
      COMMAND_CENTER  the tile grid, master volume on top
      SETTINGS        the Settings sheet inside COMMAND_CENTER

    on_change(old_state, new_state, reason) is called on every change (reason is "swipe_down",
    "swipe_up", "back_tap", "summon_button", "open_gear", "timeout", "mode_hidden" or
    "mode_undocked").
    """

    COMPANION = "companion"
    COMMAND_CENTER = "command_center"
    SETTINGS = "settings"

    def __init__(self, clock=time.monotonic, on_change=None,
                 swipe_down_enabled=True, auto_close_timeout_s=0,
                 hardware_button="none"):
        self._clock = clock
        self._on_change = on_change
        self.swipe_down_enabled = swipe_down_enabled
        self.auto_close_timeout_s = auto_close_timeout_s or 0
        self.hardware_button = hardware_button
        self.state = self.COMPANION
        self._deadline = None

    @classmethod
    def from_config(cls, command_center_cfg, clock=time.monotonic, on_change=None):
        """Build from the config's command_center dict: swipe_down_enabled, swipe_sensitivity (used by
        swipe_recognizer_kwargs(), not kept here), hardware_button, auto_close_timeout_s.
        """
        cfg = command_center_cfg or {}
        return cls(
            clock=clock,
            on_change=on_change,
            swipe_down_enabled=bool(cfg.get("swipe_down_enabled", True)),
            auto_close_timeout_s=int(cfg.get("auto_close_timeout_s", 0) or 0),
            hardware_button=cfg.get("hardware_button", "none"),
        )

    # -- internals ----------------------------------------------------------

    def _goto(self, new_state, reason):
        old = self.state
        if old == new_state:
            return
        self.state = new_state
        if new_state in (self.COMMAND_CENTER, self.SETTINGS):
            self._arm_timeout()
        else:
            self._deadline = None
        if self._on_change:
            self._on_change(old, new_state, reason)

    def _arm_timeout(self):
        """Private alias for rearm_timeout(), main.py still calls it by this name."""
        self.rearm_timeout()

    # -- inputs ---------------------------------------------------------

    def swipe_down_from_top(self):
        """A swipe down that started in the top `edge` band. Does nothing unless were in COMPANION and
        swipe-down is on.
        """
        if self.swipe_down_enabled and self.state == self.COMPANION:
            self._goto(self.COMMAND_CENTER, "swipe_down")

    def swipe_up(self):
        """Closes one level: SETTINGS -> COMMAND_CENTER, or COMMAND_CENTER -> COMPANION. Nothing from
        COMPANION.
        """
        if self.state == self.SETTINGS:
            self._goto(self.COMMAND_CENTER, "swipe_up")
        elif self.state == self.COMMAND_CENTER:
            self._goto(self.COMPANION, "swipe_up")

    def back_tap(self):
        """The on-screen Back (not the hardware summon button). Same one-level close as swipe_up."""
        if self.state == self.SETTINGS:
            self._goto(self.COMMAND_CENTER, "back_tap")
        elif self.state == self.COMMAND_CENTER:
            self._goto(self.COMPANION, "back_tap")

    def open(self, reason="open"):
        """Opens the Command Center whatever swipe_down_enabled and hardware_button say, for callers
        that are the way in (the pull tab, and the overlay over an emulator's second screen).
        Does nothing unless in COMPANION.
        """
        if self.state == self.COMPANION:
            self._goto(self.COMMAND_CENTER, reason)

    def close(self, reason="close"):
        """Fully closes back to COMPANION in one step from any state (swipe_up/back_tap stop at the
        Command Center when Settings is open). Does nothing when already closed.
        """
        self._goto(self.COMPANION, reason)

    def rearm_timeout(self):
        """Restarts the auto-close countdown from now with the current auto_close_timeout_s (0 turns
        it off). Safe in any state, it only matters once tick() runs while the Command Center or
        Settings is open.
        """
        self._deadline = (self._clock() + self.auto_close_timeout_s
                           if self.auto_close_timeout_s > 0 else None)

    def open_gear(self):
        """The gear icon inside the Command Center opens the Settings sheet."""
        if self.state == self.COMMAND_CENTER:
            self._goto(self.SETTINGS, "open_gear")

    def summon_button(self):
        """The hardware button toggles: opens from COMPANION, fully closes from the Command Center or
        Settings. Does nothing for hardware_button == "none" (the reader never starts then anyway).
        """
        if self.hardware_button == "none":
            return
        if self.state == self.COMPANION:
            self._goto(self.COMMAND_CENTER, "summon_button")
        else:
            self._goto(self.COMPANION, "summon_button")

    def mode_changed(self, mode):
        """The display mode changed. HIDDEN or UNDOCKED closes the Command Center back to COMPANION,
        the bottom screen may now be a game's touchscreen or gone. OVERLAY leaves it alone since the
        overlay opens the Command Center itself with open().
        """
        if mode in ("HIDDEN", "UNDOCKED"):
            self._goto(self.COMPANION, "mode_%s" % mode.lower())

    def tick(self, now=None):
        """Call regularly (like once a frame). Fires the auto-close timeout if its armed and due, a
        full close to COMPANION from Settings or the Command Center.
        """
        if self._deadline is None:
            return
        now = self._clock() if now is None else now
        if now >= self._deadline:
            self._goto(self.COMPANION, "timeout")

    def handle_gesture(self, name):
        """Glue for ui.SwipeRecognizer's on_gesture. "swipe_down" (not from the top edge) and
        "swipe_up_from_bottom" are handled like their plain versions, only opening needs the top
        edge.
        """
        if name == "swipe_down_from_top":
            self.swipe_down_from_top()
        elif name in ("swipe_up", "swipe_up_from_bottom"):
            self.swipe_up()

    def wants(self, gesture_name):
        """SwipeRecognizer/TouchRouter's wants(name): True if the Command Center should take this
        gesture instead of the widget under the finger (like a mixer slider).
        """
        if gesture_name == "swipe_down_from_top":
            return self.swipe_down_enabled and self.state == self.COMPANION
        if gesture_name in ("swipe_up", "swipe_up_from_bottom"):
            return self.state in (self.COMMAND_CENTER, self.SETTINGS)
        if gesture_name == "swipe_down":
            return self.state == self.SETTINGS  # pages Settings (main.on_gesture)
        return False

    def is_open(self):
        return self.state != self.COMPANION


if __name__ == "__main__":
    # Quick manual check: print which device each binding resolves to.
    for _binding in ("btn_back_f1", "btn_c_paddle", "btn_z_paddle"):
        _name, _code = BINDING_TARGETS[_binding]
        _path = find_event_path(_name)
        print("%-14s %-70s code=0x%03x -> %s" % (_binding, _name, _code, _path or "NOT FOUND"))
