#!/usr/bin/env python3
"""summon - the Command Center's summon button reader, and the pull-down
state machine that decides when the Command Center is open (Navigation v2,
DESIGN.md). Pure Python, stdlib only, Python 3.12 (PC) / 3.14 (device).

Part 1: a background, NON-GRABBING evdev reader
------------------------------------------------
Watches one configured hardware button (`command_center.hardware_button` in
R6's config schema: "btn_c_paddle", "btn_z_paddle", "btn_back_f1", or "none")
and calls back on a debounced press edge. It:

  - opens devices BY NAME (re-resolved from /proc/bus/input/devices on every
    (re)connect attempt), never by a hardcoded /dev/input/eventN - event
    numbers are reassigned across reboots and hotplug;
  - never calls EVIOCGRAB or writes anything - it only reads;
  - costs ~0 CPU when idle: the read loop blocks in select(), it does not
    poll in a tight loop;
  - survives hotplug: a read that returns EOF (or an OSError, e.g. ENODEV)
    is treated as "device is gone", and the loop goes back to re-resolving
    the device by name, with a backoff between attempts;
  - can be stopped cleanly from another thread (a wake socket pair - a
    self-pipe, but socketpair() rather than os.pipe() so PC tests do not
    hang under Windows' socket-only select() - wakes the select() so a
    blocked reader still notices `stop()` promptly).

input_event on aarch64 (64-bit time_t, no padding) is 24 bytes:
    struct input_event {
        struct timeval time;   // 2 x 8-byte long  (tv_sec, tv_usec)
        __u16 type;
        __u16 code;
        __s32 value;
    };
INPUT_EVENT_FORMAT below packs/unpacks exactly that, with '=' (native byte
order, standard sizes, no compiler padding) so the 24-byte size is guaranteed
rather than assumed.

Which evdev codes the bindings actually arrive as
--------------------------------------------------
All of this was read from the live device on 2026-09-23 (RP5, ROCKNIX,
top screen off/charging - read-only, nothing here required the display):

  btn_back_f1 -> KEY_F1 (0x3b / 59) on "InputPlumber Keyboard".
    Evidence: (1) /usr/share/inputplumber/capability_maps/retroid_mcu.yaml
    (fetched from the device) maps `BTN_BACK -> keyboard: KeyF1`; this is the
    ACTIVE map - /usr/share/inputplumber/devices/01-retroid-controller.yaml
    ("Retroid Layout") selects capability_map_id: retroid_mcu; two sibling
    maps (retroid_type1/type2.yaml) map BTN_BACK to `QuickAccess` instead and
    have no paddle entries at all, so they are NOT what is active here - the
    real capture below (BTN_BACK present on the physical gamepad, KEY_F1
    present on the virtual keyboard) matches retroid_mcu, not type1/type2.
    (2) tests/fixtures/proc-input-devices-real-capture-2026-09-23.txt: the
    real /proc/bus/input/devices capture shows "InputPlumber Keyboard"
    (event8 that night) advertising KEY bit 59 = KEY_F1. (3) R7's research
    reached the same conclusion independently.

  btn_c_paddle (right paddle) -> BTN_TRIGGER_HAPPY4 (0x2c3 / 707)
  btn_z_paddle  (left paddle) -> BTN_TRIGGER_HAPPY3 (0x2c2 / 706)
    both on "Sony Interactive Entertainment DualSense Wireless Controller"
    (the virtual gamepad; NOT the same-prefixed "...Motion Sensors" /
    "...Touchpad" / "...Headset Jack" sibling devices - match the device
    NAME exactly).

    Evidence chain, and an important open caveat:
    - retroid_mcu.yaml maps `BTN_C -> gamepad.button: RightPaddle1` and
      `BTN_Z -> gamepad.button: LeftPaddle1` (both source events on the
      physical "Retroid Pocket Gamepad").
    - InputPlumber's own dualsense.rs target driver (fetched from
      github.com/ShadowBlip/InputPlumber, main branch) only *stores*
      RightPaddle1/LeftPaddle1 into internal state fields (`state.right_fn`,
      `state.left_fn`); it does not itself define evdev codes. It emulates a
      real Sony DualSense over uhid, so the codes are decided by whatever the
      Linux kernel's own driver (hid-playstation.c) does with that HID
      report.
    - hid-playstation.c's dualsense_parse_report() maps the paddle/function
      bits (DS_EDGE_BUTTONS_LEFT_PADDLE/RIGHT_PADDLE/FN1/FN2, byte
      ds_report->buttons[2] bits 4-7) to BTN_TRIGGER_HAPPY1-4 - but ONLY
      `if (ds->is_edge)`. `is_edge` is set true only when the HID device's
      USB/BT product id equals USB_DEVICE_ID_SONY_PS5_CONTROLLER_2 (0x0df2,
      the DualSense EDGE). The device this app talks to reports
      Vendor=054c Product=0ce6 (tests/fixtures/proc-input-devices-real-
      capture-2026-09-23.txt) - that is USB_DEVICE_ID_SONY_PS5_CONTROLLER,
      the BASE (non-Edge) DualSense. InputPlumber's own composite-device
      config for this RP5 (01-retroid-controller.yaml) also says
      `target_devices: [ds5, keyboard]` - the plain "ds5" target, not
      "ds5-edge" (both are valid target names per
      /usr/share/inputplumber/schema/composite_device_v1.json's enum).
    - CONSEQUENCE: as the device is configured tonight, the kernel driver's
      `if (ds->is_edge)` branch never runs, so BTN_TRIGGER_HAPPY1-4 are
      neither registered as capabilities nor ever emitted - a paddle press
      may currently produce NO evdev event at all on event9. The two extra
      button bits the virtual gamepad DOES advertise beyond a bare
      South/East/North/West/TL/TR/Select/Start/Mode/ThumbL/ThumbR set are
      BTN_TL2 (0x138) and BTN_TR2 (0x139) - real DualSense hardware's
      unconditional "trigger fully pressed" digital bit (DS_BUTTONS1_L2/R2,
      read regardless of is_edge), which is unrelated to the paddles. They
      are deliberately NOT used as the paddle codes here even though they are
      the only bits observed to be live, because they would false-fire on
      any game that reads a hard analog-trigger press.
    - BTN_TRIGGER_HAPPY3/4 are still the CORRECT codes to watch for: they are
      what the paddles would produce the moment InputPlumber's config is
      switched to the "ds5-edge" target (a fix that belongs to whoever owns
      that device profile, not to this file - it is outside
      rp5deck/summon.py). Until then, summon-watch.py's job is to prove,
      empirically and on the device with the owner actually pressing the
      paddles, whether any event arrives at all. See the B11a report for
      that result.
    - Independent corroboration of the *technique* (decoding the KEY=
      capability bitmap from /proc/bus/input/devices by hand): the same
      method applied to "gpio-keys" and "pm8941_resin" in the real capture
      predicts KEY_VOLUMEUP (115) and KEY_VOLUMEDOWN (114) respectively, and
      Main independently observed exactly those two codes fire on those two
      devices when the owner pressed the physical volume keys.

Part 2: the pull-down state machine
------------------------------------
Pure logic, an injected clock, no device access at all. States: COMPANION
(default) and COMMAND_CENTER; SETTINGS is modelled as a sheet nested inside
COMMAND_CENTER (DESIGN.md: "Settings is a sheet inside the Command Center").
Every external event is a method call - swipe_down_from_top(), swipe_up(),
back_tap(), summon_button(), open_gear(), mode_changed(mode), and tick(now)
for the auto-close timeout - so it can be driven from tests without any UI.
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

# See the module docstring for how each of these was derived and verified.
KEY_F1_CODE = 0x3B               # 59  - InputPlumber Keyboard: RP5 Back button
BTN_C_PADDLE_CODE = 0x2C3        # 707 - BTN_TRIGGER_HAPPY4 - kernel's right-paddle code
BTN_Z_PADDLE_CODE = 0x2C2        # 706 - BTN_TRIGGER_HAPPY3 - kernel's left-paddle code

DS5_NAME = "Sony Interactive Entertainment DualSense Wireless Controller"
KEYBOARD_NAME = "InputPlumber Keyboard"

# binding name (config.command_center.hardware_button) -> (device name, evdev code)
#
# RV1-m7 / RV2-m1 (review/RV1-findings.md, review/RV2-findings.md): a reader
# watching the DualSense (btn_c_paddle / btn_z_paddle) wakes Python on EVERY
# stick/trigger event of the running game - measured at 1.7% of a PC core at
# 2,000 events/s, with a secondary risk of the kernel's ~64-event evdev
# buffer overflowing (dropped SYN_DROPPED events, including the summon press
# itself) if the thread is GIL-starved. `_resolve()` below only ever looks up
# and opens the ONE device this binding's row names - never both - so the
# fix for the shipped default is already structural: btn_back_f1 (config.py's
# default, per B11a's device finding that the paddles emit nothing at all)
# watches only "InputPlumber Keyboard", which is silent during normal play.
# If the owner configures a paddle binding instead, this cost is real and
# accepted (there is no cheaper way to watch a paddle press today) - see
# TestSummonOpensOnlyTheBoundDevice below for the proof that btn_back_f1
# never touches the DualSense.
BINDING_TARGETS = {
    "btn_back_f1": (KEYBOARD_NAME, KEY_F1_CODE),
    "btn_c_paddle": (DS5_NAME, BTN_C_PADDLE_CODE),
    "btn_z_paddle": (DS5_NAME, BTN_Z_PADDLE_CODE),
}

# timeval (long tv_sec, long tv_usec) + u16 type + u16 code + s32 value.
# '=' forces native byte order with STANDARD (not compiler-padded) sizes, so
# this is always exactly 24 bytes on a 64-bit time_t kernel (aarch64, x86_64).
INPUT_EVENT_FORMAT = "=qqHHi"
INPUT_EVENT_STRUCT = struct.Struct(INPUT_EVENT_FORMAT)
INPUT_EVENT_SIZE = INPUT_EVENT_STRUCT.size
assert INPUT_EVENT_SIZE == 24, INPUT_EVENT_SIZE

InputEvent = collections.namedtuple("InputEvent", "sec usec type code value")

SummonEvent = collections.namedtuple(
    "SummonEvent", "binding device_name device_path code value ts sec usec")


def parse_event(data):
    """Unpack 24 raw bytes read from /dev/input/eventN into an InputEvent.
    Raises ValueError if `data` is not exactly one event's worth of bytes."""
    if len(data) != INPUT_EVENT_SIZE:
        raise ValueError("expected %d bytes, got %d" % (INPUT_EVENT_SIZE, len(data)))
    return InputEvent(*INPUT_EVENT_STRUCT.unpack(data))


def parse_proc_input_devices(text):
    """Parse the text of /proc/bus/input/devices into a list of
    {"name": str, "handlers": [str, ...]} dicts, one per device block
    (blocks are separated by a blank line)."""
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
    """Return "/dev/input/eventN" for the device whose /proc/bus/input/devices
    Name= is EXACTLY `name` (not a prefix - several InputPlumber devices share
    a name prefix, e.g. the DualSense and its "... Motion Sensors" sibling),
    or None if it is not currently present. Re-reads the file every call, so
    calling this again after a hotplug re-numbering finds the new number.

    `text`, if given, is used instead of reading `devices_path` - for tests."""
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
    """Read exactly n bytes, or fewer at EOF. Never raises on a short read -
    the caller decides what a short/empty read means."""
    buf = b""
    while len(buf) < n:
        chunk = fd.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


class SummonButtonReader:
    """Background-safe (but not itself threaded - call run() from a thread)
    non-grabbing reader for one configured summon binding.

    binding: one of BINDING_TARGETS' keys, or "none" (never watches anything;
    run() idles until stop()).

    on_summon(SummonEvent): called on each debounced press edge (value == 1),
    at most once per `debounce_s` seconds. Autorepeat (value == 2) and
    release (value == 0) never call it.

    on_any_key(InputEvent, device_path, device_name): if given, called for
    EVERY EV_KEY event seen on the watched device (any code, any value,
    including autorepeat) - a diagnostic hook, used by tools/summon-watch.py
    to show what a button actually sends even when it is not the configured
    binding's code.

    find_event_path_fn/open_fn/select_fn/clock are injectable for tests."""

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
        # A wake socket pair, not os.pipe(): select.select() on Windows (the
        # PC test platform) only accepts sockets, never plain pipe/file
        # handles, so a pipe-based self-pipe here would hang every PC test
        # that exercises the real select.select default. socket.socketpair()
        # is emulated portably by the stdlib on Windows and is a real,
        # select()-able pair of connected sockets on Linux (the device) too.
        self._wake_r, self._wake_w = socket.socketpair()

    def _logmsg(self, msg):
        if self._log:
            self._log(msg)

    def stop(self):
        """Ask run() to return. Safe to call from another thread; wakes a
        blocked select() immediately rather than waiting for a timeout."""
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
        """Block up to `timeout` seconds (None = forever) for a stop request.
        Returns True if stop() was called, False if the timeout elapsed."""
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
        """Blocks until stop() is called (from another thread) or the
        binding is "none" and stop() fires. Costs ~0 CPU while idle: every
        wait is a select() with either no timeout (blocked on real input) or
        a bounded idle/backoff timeout, never a busy loop."""
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
                    continue  # short/torn read; wait for the next one
                ev = parse_event(data)
                self._handle_event(ev, path, code, device_name)

    def _handle_event(self, ev, path, code, device_name):
        if ev.type != EV_KEY:
            return
        if self._on_any_key:
            self._on_any_key(ev, path, device_name)
        if ev.value == 2:
            return  # autorepeat - never a fresh press
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

# swipe_sensitivity (R6 §3 command_center.swipe_sensitivity) -> kwargs for
# ui.SwipeRecognizer(height_fn, edge=..., distance=..., ...). Higher
# sensitivity = a bigger top "from-top" catch zone and less travel needed.
# "medium" matches SwipeRecognizer's own built-in defaults (edge=60, distance=120).
SWIPE_SENSITIVITY_PARAMS = {
    "low": {"edge": 40, "distance": 160},
    "medium": {"edge": 60, "distance": 120},
    "high": {"edge": 90, "distance": 80},
}


def swipe_recognizer_kwargs(sensitivity):
    """kwargs to splat into ui.SwipeRecognizer(height_fn, **kwargs) for the
    given command_center.swipe_sensitivity value. Unknown values fall back to
    "medium" rather than raising - a bad settings file must not break the
    gesture, per config.py's own "a typo must not take the panel away" rule."""
    return dict(SWIPE_SENSITIVITY_PARAMS.get(sensitivity, SWIPE_SENSITIVITY_PARAMS["medium"]))


class PullDownStateMachine:
    """Navigation v2's summon/close logic (DESIGN.md). Pure state + an
    injected clock; does not touch any device, widget or IPC socket itself.

    States:
      COMPANION       default view (game art/video)
      COMMAND_CENTER  the tile grid, master volume at the top
      SETTINGS        the gear's sheet, nested inside COMMAND_CENTER

    on_change(old_state, new_state, reason) is called on every transition
    (reason is a short string: "swipe_down", "swipe_up", "back_tap",
    "summon_button", "open_gear", "timeout", "mode_hidden", "mode_undocked").
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
        """Build from the `command_center` sub-dict of R6's config schema
        (research/R6-settings.md §3): swipe_down_enabled, swipe_sensitivity
        (consumed via swipe_recognizer_kwargs(), not stored here),
        hardware_button, auto_close_timeout_s."""
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
        """Private alias, kept because main.py (I1) still calls this name
        directly at two call sites (a touch restarting the countdown, and
        auto_close_timeout_s changing live in Settings) - see
        rearm_timeout() and patches/FXD-main.patch."""
        self.rearm_timeout()

    # -- inputs ---------------------------------------------------------

    def swipe_down_from_top(self):
        """A swipe-down gesture that started within the recognizer's `edge`
        of the top. No-op unless we are in COMPANION and swipe-down is
        enabled (R6's command_center.swipe_down_enabled)."""
        if self.swipe_down_enabled and self.state == self.COMPANION:
            self._goto(self.COMMAND_CENTER, "swipe_down")

    def swipe_up(self):
        """Closes one level: SETTINGS -> COMMAND_CENTER, or
        COMMAND_CENTER -> COMPANION. No-op from COMPANION."""
        if self.state == self.SETTINGS:
            self._goto(self.COMMAND_CENTER, "swipe_up")
        elif self.state == self.COMMAND_CENTER:
            self._goto(self.COMPANION, "swipe_up")

    def back_tap(self):
        """An on-screen Back affordance (distinct from the configurable
        hardware summon button). Same one-level-down behaviour as swipe_up."""
        if self.state == self.SETTINGS:
            self._goto(self.COMMAND_CENTER, "back_tap")
        elif self.state == self.COMMAND_CENTER:
            self._goto(self.COMPANION, "back_tap")

    def open(self, reason="open"):
        """Open the Command Center whatever swipe_down_enabled and
        hardware_button say - for callers that ARE the way in: the pull tab,
        and CC5's overlay over an emulator's second screen
        (hidden_overlay.py), which the summon button opens in HIDDEN mode.
        No-op unless in COMPANION."""
        if self.state == self.COMPANION:
            self._goto(self.COMMAND_CENTER, reason)

    def close(self, reason="close"):
        """Public counterpart of open(): fully close back to COMPANION from
        whatever state we are in, in one step. Unlike swipe_up()/back_tap(),
        this does not stop at COMMAND_CENTER when SETTINGS is open - it is
        for callers that want "all the way closed" unconditionally (e.g.
        CC5's hidden_overlay.py, which today reaches for the private
        `_goto(PULL.COMPANION, reason)` to get exactly this; see the FX-D
        report). A no-op (via _goto's old==new check) when already
        COMPANION."""
        self._goto(self.COMPANION, reason)

    def rearm_timeout(self):
        """Public: restart the auto-close countdown from now, using the
        current auto_close_timeout_s (0/falsy disarms it). Safe to call in
        any state - it only has an observable effect once tick() is being
        driven while COMMAND_CENTER or SETTINGS is open. main.py's two call
        sites (a touch while the Command Center is open, and
        auto_close_timeout_s changing live) call the private _arm_timeout()
        alias below rather than this; see patches/FXD-main.patch."""
        self._deadline = (self._clock() + self.auto_close_timeout_s
                           if self.auto_close_timeout_s > 0 else None)

    def open_gear(self):
        """The gear icon inside the Command Center opens the Settings sheet."""
        if self.state == self.COMMAND_CENTER:
            self._goto(self.SETTINGS, "open_gear")

    def summon_button(self):
        """The configured hardware button: toggles - opens from COMPANION,
        fully closes (from either CC or SETTINGS) back to COMPANION.
        Honours hardware_button == "none" (never acts) even if something
        calls this directly; the real enforcement is that the reader is
        never started for "none", but the state machine defends too."""
        if self.hardware_button == "none":
            return
        if self.state == self.COMPANION:
            self._goto(self.COMMAND_CENTER, "summon_button")
        else:
            self._goto(self.COMPANION, "summon_button")

    def mode_changed(self, mode):
        """The display mode (main.py's FULL/BAR/HIDDEN, or "UNDOCKED" per
        DESIGN.md's "Undocked" section) changed. HIDDEN or UNDOCKED must
        close the Command Center and reset to COMPANION - the bottom screen
        may now be a game's own touchscreen (DS/3DS) or gone entirely.
        CC5's "OVERLAY" (the Command Center summoned over that touchscreen,
        hidden_overlay.py) leaves the state alone: the overlay opens the
        Command Center itself with open()."""
        if mode in ("HIDDEN", "UNDOCKED"):
            self._goto(self.COMPANION, "mode_%s" % mode.lower())

    def tick(self, now=None):
        """Call periodically (e.g. once per drawn frame). Fires the
        auto-close timeout if one is armed and has elapsed - a full close to
        COMPANION regardless of whether SETTINGS or COMMAND_CENTER was open."""
        if self._deadline is None:
            return
        now = self._clock() if now is None else now
        if now >= self._deadline:
            self._goto(self.COMPANION, "timeout")

    def handle_gesture(self, name):
        """Convenience glue for ui.SwipeRecognizer's on_gesture callback:
        dispatches its four gesture names to the two inputs that matter
        here. ("swipe_down" - not from the top edge - and
        "swipe_up_from_bottom" collapse onto the same handling as their
        edge-qualified/plain counterparts; only entry needs the top-edge
        qualifier, per DESIGN.md.)"""
        if name == "swipe_down_from_top":
            self.swipe_down_from_top()
        elif name in ("swipe_up", "swipe_up_from_bottom"):
            self.swipe_up()

    def wants(self, gesture_name):
        """SwipeRecognizer/TouchRouter's `wants(name)` predicate: True if the
        Command Center should claim this gesture instead of leaving it to
        whatever widget is under the finger (e.g. a mixer slider)."""
        if gesture_name == "swipe_down_from_top":
            return self.swipe_down_enabled and self.state == self.COMPANION
        if gesture_name in ("swipe_up", "swipe_up_from_bottom"):
            return self.state in (self.COMMAND_CENTER, self.SETTINGS)
        return False

    def is_open(self):
        return self.state != self.COMPANION


if __name__ == "__main__":
    # Quick manual smoke test: print device resolution for every binding.
    for _binding in ("btn_back_f1", "btn_c_paddle", "btn_z_paddle"):
        _name, _code = BINDING_TARGETS[_binding]
        _path = find_event_path(_name)
        print("%-14s %-70s code=0x%03x -> %s" % (_binding, _name, _code, _path or "NOT FOUND"))
