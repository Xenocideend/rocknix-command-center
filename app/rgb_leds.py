"""rgb_leds - RGB stick-light control for the RP5's SM8250 analog stick
rings (owner request: "add RGB controls for the thumb sticks").

Real device facts (read on the device 24 Sep 2026):
  /sys/class/leds/rgb:l1..rgb:l4   left stick ring (4 LEDs)
  /sys/class/leds/rgb:r1..rgb:r4   right stick ring (4 LEDs)
  each is a MULTICOLOUR led class device: brightness 0-255
  (max_brightness 255), multi_index = "blue green red" (NOTE the order -
  never assume it, read_current() parses whatever multi_index actually
  says), multi_intensity e.g. "0 20 255" meaning blue=0 green=20 red=255.
  There are also single-channel l:r1/l:g1/... entries under the same
  class - this module never touches them.

ROCKNIX ships the tool that actually knows how to drive all 4 LEDs of
both rings together:

    /usr/bin/analog_sticks_ledcontrol <brightness> \
        <right_r> <right_g> <right_b> <left_r> <left_g> <left_b>

7 args, RIGHT first, then LEFT - the opposite order of this module's own
LightState (left, right), so build_argv()/apply() must swap the pair when
constructing argv. ledcontrol writes brightness and multi_intensity
(in b g r order) itself, for all 4 LEDs of each stick, via
/sys/devices/platform/multi-led{l,r}{1..4}/leds/rgb:{l,r}{n}/. This module
NEVER writes sysfs directly to set a colour - the only command it ever
runs is this tool, argv-only (no shell), and only after every one of the
7 arguments passes build_argv()'s allow-list (int, not bool, 0-255).

ROCKNIX re-applies ITS OWN idea of the stick colour on power events:
powerstate runs `ledcontrol $(get_setting led.color)` on charge/discharge
changes, rocknix-fake-suspend runs `ledcontrol ${LED_STATE}` on wake, and
input_sense's FN_B does `ledcontrol` / `ledcontrol poweroff`. So once this
module is in "off" or "colour" mode it is in a tug-of-war it can lose at
any moment - Controller.keeper_tick() is the side that notices and wins it
back, cheaply (two small sysfs reads), without spamming the tool if
ROCKNIX keeps stomping (it gives up after a few tries in a row and logs
once). In "rocknix" mode the keeper does nothing at all - ES's own LED
menu and battery mode are left alone, on purpose.

This module never reads or writes system.cfg (ES rewrites it from memory
and it can hold credentials) - persistence goes through config.py's own
"lights" group instead (patches/RG-config.patch, applied later by
Main/CM): state_from_config()/config_changes_for() are the two ends of
that bridge, both going through config.get_value()/config.save_changes()
so this module has no schema of its own to keep in sync.
"""
import logging
import subprocess
import time

import config

log = logging.getLogger("rp5deck.rgb")

TOOL = "/usr/bin/analog_sticks_ledcontrol"
SYSFS_DIR = "/sys/class/leds"
STICKS = ("left", "right")

MODE_ROCKNIX, MODE_OFF, MODE_COLOUR = "rocknix", "off", "colour"
MODES = (MODE_ROCKNIX, MODE_OFF, MODE_COLOUR)

# A small named palette, plus custom via hue_to_rgb(). Every preset is a
# full-saturation, full-value hue (the per-stick "brightness" 0-255 sent to
# ledcontrol scales overall intensity separately from these channel values,
# matching the tool's own <brightness> + <r> <g> <b> split).
PRESETS = [
    ("red", (255, 0, 0)),
    ("orange", (255, 120, 0)),
    ("yellow", (255, 230, 0)),
    ("green", (0, 200, 60)),
    ("cyan", (0, 220, 220)),
    ("blue", (30, 90, 255)),
    ("purple", (150, 60, 255)),
    ("pink", (255, 60, 180)),
    ("white", (255, 255, 255)),
]
PRESET_BY_NAME = dict(PRESETS)

DEFAULT_COLOUR = (255, 0, 0)
DEFAULT_BRIGHTNESS = 200

RATE_LIMIT_S = 0.08            # 80 ms: at most this often while a slider drags
KEEPER_PERIOD_S = 5.0          # how often keeper_tick() actually checks
KEEPER_MAX_TRIES = 3           # consecutive failed re-applies before giving up


# ---------------------------------------------------------------------------
# Pure colour helpers (no device, no I/O - safe to unit test directly)
# ---------------------------------------------------------------------------
def hue_to_rgb(hue, sat=1.0):
    """hue (any float, wraps mod 360) [+ optional saturation 0-1] -> an
    (r, g, b) tuple of ints 0-255, at full value. The only place HSV maths
    happens in this module."""
    hue = float(hue) % 360.0
    sat = 0.0 if sat < 0.0 else 1.0 if sat > 1.0 else float(sat)
    c = sat                      # value fixed at 1.0, so chroma == saturation
    x = c * (1 - abs((hue / 60.0) % 2 - 1))
    m = 1.0 - c
    if hue < 60:
        r1, g1, b1 = c, x, 0.0
    elif hue < 120:
        r1, g1, b1 = x, c, 0.0
    elif hue < 180:
        r1, g1, b1 = 0.0, c, x
    elif hue < 240:
        r1, g1, b1 = 0.0, x, c
    elif hue < 300:
        r1, g1, b1 = x, 0.0, c
    else:
        r1, g1, b1 = c, 0.0, x
    return (int(round((r1 + m) * 255)), int(round((g1 + m) * 255)),
           int(round((b1 + m) * 255)))


def rgb_to_hue(rgb):
    """Approximate inverse of hue_to_rgb: (r, g, b) 0-255 -> (hue, sat) so
    rgb_view can park its hue slider at roughly the right spot for a preset
    or a stored colour. Grey (sat 0) returns hue 0 - the slider position is
    then meaningless, which is fine: nothing reads hue for a colour that
    has none."""
    r, g, b = (c / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0, 0.0
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return (h * 60.0) % 360.0, (d / mx if mx else 0.0)


def _clamp_byte(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    return 0 if v < 0 else 255 if v > 255 else v


def _clamp_rgb(rgb_val):
    r, g, b = rgb_val
    return (_clamp_byte(r), _clamp_byte(g), _clamp_byte(b))


def _hex_to_rgb(h):
    if not isinstance(h, str):
        return DEFAULT_COLOUR
    h = h.strip().lstrip("#")
    if len(h) != 6:
        return DEFAULT_COLOUR
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return DEFAULT_COLOUR


def _rgb_to_hex(rgb_val):
    r, g, b = _clamp_rgb(rgb_val)
    return "#%02x%02x%02x" % (r, g, b)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class LightState:
    """mode in MODES; left/right are (r, g, b) 0-255 each; linked means
    "the same colour on both sticks" (colour_for()/with_colour() enforce
    it - the two colours cannot drift apart while linked); brightness is
    the single 0-255 sent to ledcontrol's first argument. Immutable:
    replace()/with_colour() return a new LightState."""
    __slots__ = ("mode", "linked", "left", "right", "brightness")

    def __init__(self, mode=MODE_ROCKNIX, linked=True, left=DEFAULT_COLOUR,
                 right=DEFAULT_COLOUR, brightness=DEFAULT_BRIGHTNESS):
        self.mode = mode if mode in MODES else MODE_ROCKNIX
        self.linked = bool(linked)
        left = _clamp_rgb(left)
        right = _clamp_rgb(right)
        if self.linked:
            right = left
        self.left = left
        self.right = right
        self.brightness = _clamp_byte(brightness)

    def replace(self, **kw):
        d = dict(mode=self.mode, linked=self.linked, left=self.left,
                 right=self.right, brightness=self.brightness)
        d.update(kw)
        return LightState(**d)

    def colour_for(self, stick):
        return self.left if (self.linked or stick == "left") else self.right

    def with_colour(self, stick, rgb_val):
        rgb_val = _clamp_rgb(rgb_val)
        if self.linked or stick == "left":
            return self.replace(left=rgb_val, right=rgb_val if self.linked else self.right)
        return self.replace(right=rgb_val)

    def to_dict(self):
        return {"mode": self.mode, "linked": self.linked, "left": self.left,
               "right": self.right, "brightness": self.brightness}

    def __eq__(self, other):
        return isinstance(other, LightState) and self.to_dict() == other.to_dict()

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return "LightState(%r)" % (self.to_dict(),)


# ---------------------------------------------------------------------------
# config.py bridge (RG-config.patch adds the "lights" group there)
# ---------------------------------------------------------------------------
def state_from_config(cfg, cfg_mod=None):
    """Build a LightState from a loaded config dict. Falls back to
    LightState()'s own defaults for anything config.get_value() cannot
    answer (an unpatched config.py with no "lights" schema yet -
    get_value() returns None for an unknown key path, never raises)."""
    m = cfg_mod or config
    mode = m.get_value(cfg, ("lights", "mode")) or MODE_ROCKNIX
    linked = m.get_value(cfg, ("lights", "linked"))
    linked = True if linked is None else bool(linked)
    left = _hex_to_rgb(m.get_value(cfg, ("lights", "left")) or _rgb_to_hex(DEFAULT_COLOUR))
    right = _hex_to_rgb(m.get_value(cfg, ("lights", "right")) or _rgb_to_hex(DEFAULT_COLOUR))
    brightness = m.get_value(cfg, ("lights", "brightness"))
    if brightness is None:
        brightness = DEFAULT_BRIGHTNESS
    return LightState(mode=mode, linked=linked, left=left, right=right, brightness=brightness)


def config_changes_for(state):
    """{key_path: value} ready for config.save_changes() / config.set_value()."""
    return {
        ("lights", "mode"): state.mode,
        ("lights", "linked"): state.linked,
        ("lights", "left"): _rgb_to_hex(state.left),
        ("lights", "right"): _rgb_to_hex(state.right),
        ("lights", "brightness"): state.brightness,
    }


# ---------------------------------------------------------------------------
# The device side: argv allow-list, apply(), read_current()
# ---------------------------------------------------------------------------
def _validate_byte_arg(v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("refused non-int ledcontrol arg %r" % (v,))
    if not (0 <= v <= 255):
        raise ValueError("refused out-of-range ledcontrol arg %r" % (v,))
    return v


def build_argv(brightness, right, left):
    """THE only place argv for ledcontrol is built. Every one of the 7
    numbers is validated as a real int 0-255 (never a bool, never a
    string, never out of range) before it can reach subprocess - this is
    what tools/rg_break_tests.py's allow-list break targets."""
    vals = [brightness] + list(right) + list(left)
    if len(vals) != 7:
        raise ValueError("ledcontrol needs exactly 7 arguments, got %d" % len(vals))
    vals = [_validate_byte_arg(v) for v in vals]
    return [TOOL] + [str(v) for v in vals]


def _run_tool(argv, timeout=3.0):
    """The only function that ever calls subprocess for this module -
    argv-only (no shell=True), and refuses anything that is not exactly
    [TOOL, 7 numeric strings] even if a caller upstream slipped past
    build_argv() (defence in depth, cheap to keep)."""
    if len(argv) != 8 or argv[0] != TOOL:
        raise ValueError("refused command: %r" % (argv,))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("rgb: ledcontrol failed to run: %s", e)
        return False
    if proc.returncode != 0:
        log.warning("rgb: ledcontrol exited %d: %s", proc.returncode, proc.stderr.strip())
        return False
    return True


def apply(state, run=None):
    """mode "rocknix": do nothing at all - hands off completely, so ES's
    own LED menu / battery mode keep working. "off": ledcontrol with
    brightness 0 and every channel 0. "colour": ledcontrol with the
    state's brightness and its two colours (RIGHT first, per the tool).
    Returns None for "rocknix" (nothing ran), else run()'s bool result."""
    run = run or _run_tool
    if state.mode == MODE_ROCKNIX:
        return None
    if state.mode == MODE_OFF:
        argv = build_argv(0, (0, 0, 0), (0, 0, 0))
    elif state.mode == MODE_COLOUR:
        argv = build_argv(state.brightness, state.right, state.left)
    else:
        raise ValueError("unknown lights mode %r" % (state.mode,))
    return run(argv)


def _read_led(sysfs_dir, name):
    base = "%s/%s" % (sysfs_dir, name)
    try:
        with open("%s/brightness" % base) as f:
            brightness = int(f.read().strip())
        with open("%s/multi_intensity" % base) as f:
            intensity = f.read().split()
        with open("%s/multi_index" % base) as f:
            index = f.read().split()
    except (OSError, ValueError):
        return None
    if len(intensity) != 3 or len(index) != 3:
        return None
    try:
        values = [int(v) for v in intensity]
    except ValueError:
        return None
    order = dict(zip(index, values))
    if not all(k in order for k in ("red", "green", "blue")):
        return None
    return brightness, (order["red"], order["green"], order["blue"])


def read_current(sysfs_dir=SYSFS_DIR):
    """Read back what is really lit on both sticks right now, using
    multi_index to learn the real channel order (never assume "blue green
    red" holds - read_current() parses whatever the file actually says).
    Returns {"left": (r,g,b), "right": (r,g,b), "brightness": int}, or
    None if either ring is unreadable (missing driver, wrong device,
    permissions) - the keeper treats None as "cannot tell, do nothing"."""
    left = _read_led(sysfs_dir, "rgb:l1")
    right = _read_led(sysfs_dir, "rgb:r1")
    if left is None or right is None:
        return None
    lb, lrgb = left
    rb, rrgb = right
    return {"left": lrgb, "right": rrgb, "brightness": lb if lb == rb else max(lb, rb)}


def _matches(current, state):
    if state.mode == MODE_OFF:
        return current["brightness"] == 0
    if state.mode == MODE_COLOUR:
        return (current["brightness"] == state.brightness and current["left"] == state.left
               and current["right"] == state.right)
    return True                 # MODE_ROCKNIX: caller never gets here


def _describe(current):
    if current is None:
        return "unreadable"
    return "left=%s right=%s brightness=%d" % (current["left"], current["right"],
                                                current["brightness"])


# ---------------------------------------------------------------------------
# Controller: owns the live LightState, rate-limits drags, runs the keeper
# ---------------------------------------------------------------------------
class Controller:
    """`run` (ledcontrol invoker), `read` (read_current) and `now`
    (time.monotonic) are all injectable so tests never touch a real
    device. Not thread-aware by itself - main.py calls it from the UI
    thread / its own timer callbacks, same as the rest of the app."""

    def __init__(self, state=None, run=None, read=None, now=None,
                rate_limit=RATE_LIMIT_S, keeper_period=KEEPER_PERIOD_S,
                max_tries=KEEPER_MAX_TRIES, log_fn=None):
        self.state = state or LightState()
        self._run = run or _run_tool
        self._read = read or read_current
        self._now = now or time.monotonic
        self.rate_limit = rate_limit
        self.keeper_period = keeper_period
        self.max_tries = max_tries
        self._log = log_fn or log.info
        self._last_apply_t = float("-inf")
        self._pending_state = None
        self._last_keeper_t = float("-inf")
        self._fail_streak = 0
        self._gave_up = False

    # -- applying -------------------------------------------------------------
    def _apply_now(self):
        self._last_apply_t = self._now()
        self._pending_state = None
        self._fail_streak = 0
        self._gave_up = False
        return apply(self.state, run=self._run)

    def set_state(self, state):
        """A discrete change (mode / linked / a preset tap / a slider's
        final on_release value): apply immediately, no rate limit."""
        self.state = state
        return self._apply_now()

    def drag(self, state):
        """A slider mid-drag: apply now if `rate_limit` seconds have
        passed since the last apply, else just remember the value - call
        drag_flush() when the drag ends so the final value is never
        dropped (DESIGN: "always apply the final value")."""
        self.state = state
        t = self._now()
        if t - self._last_apply_t >= self.rate_limit:
            return self._apply_now()
        self._pending_state = state
        return None

    def drag_flush(self):
        if self._pending_state is not None:
            self.state = self._pending_state
            return self._apply_now()
        return None

    # -- keeper -----------------------------------------------------------
    def keeper_tick(self, force=False):
        """Call ~every keeper_period seconds, plus on demand (force=True:
        app start, an ES "wake" event). Never acts in mode "rocknix".
        Cheap: read_current() is two small sysfs reads; only re-applies
        (a subprocess call) when the device disagrees with `state`, and
        gives up after `max_tries` consecutive re-applies that did not
        stick, logging once. Returns a short status string (used by
        tests; main.py does not need it)."""
        if self.state.mode == MODE_ROCKNIX:
            return "rocknix"
        t = self._now()
        if not force and t - self._last_keeper_t < self.keeper_period:
            return "too-soon"
        self._last_keeper_t = t
        if self._gave_up:
            return "gave-up"
        current = self._read()
        if current is not None and _matches(current, self.state):
            self._fail_streak = 0
            return "ok"
        self._fail_streak += 1
        if self._fail_streak > self.max_tries:
            self._gave_up = True
            self._log("rgb: giving up on restoring stick lights after %d tries"
                      % self.max_tries)
            return "gave-up"
        was = _describe(current)
        apply(self.state, run=self._run)
        self._log("rgb: restored stick lights (ROCKNIX changed them to %s)" % was)
        return "restored"

    def on_es_wake(self):
        """main.py calls this from its ES "wake" event handler (esevents
        WAKE) - the moment ROCKNIX's own wake path is most likely to have
        just run `ledcontrol ${LED_STATE}` over whatever this module set."""
        return self.keeper_tick(force=True)

    def reset_backoff(self):
        self._fail_streak = 0
        self._gave_up = False
