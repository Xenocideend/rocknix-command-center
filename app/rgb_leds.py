"""rgb_leds: the RP5's analog stick lights.

On the device:
  /sys/class/leds/rgb:l1..rgb:l4   left stick ring (4 LEDs)
  /sys/class/leds/rgb:r1..rgb:r4   right stick ring (4 LEDs)
  each is a multicolour LED with brightness 0-255 and multi_index "blue green red" (that
  order isnt assumed, read_current() parses whatever multi_index says), and multi_intensity
  like "0 20 255" for blue=0 green=20 red=255. The single channel l:r1/l:g1/... entries
  in the same class are never touched.

ROCKNIX ships the tool that drives all 4 LEDs of both rings together:

    /usr/bin/analog_sticks_ledcontrol <brightness> <right_r> <right_g> <right_b> <left_r> <left_g> <left_b>

7 args, right first and then left, the opposite of this module's LightState (left, right), so
build_argv()/apply() swap the pair. ledcontrol writes brightness and multi_intensity (in b g r
order) itself for all 4 LEDs of each stick. This never writes sysfs directly to set a colour,
the only command it runs is that tool, argv only with no shell, and only after all 7 args pass
build_argv()'s allow list (int, not bool, 0-255).

ROCKNIX puts its own stick colour back on power events: powerstate runs
`ledcontrol $(get_setting led.color)` on charge changes, rocknix-fake-suspend runs
`ledcontrol ${LED_STATE}` on wake, and input_sense's FN_B does `ledcontrol` or
`ledcontrol poweroff`. So in "off" or "colour" mode this can lose at any moment, and
Controller.keeper_tick() notices and wins it back cheaply (two small sysfs reads), and gives up
after a few tries in a row with one log line instead of spamming the tool. In "rocknix" mode
the keeper does nothing, so ES's own LED menu and battery mode are left alone.

This never reads or writes system.cfg (ES rewrites it from memory and it can hold
credentials). Settings are saved through config.py's "lights" group instead.
state_from_config()/config_changes_for() are the two ends, both through
config.get_value()/config.save_changes(), so there's no schema here to keep in sync.
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

# A small named palette, plus custom through hue_to_rgb(). Every preset is a full saturation,
# full value hue. The per-stick brightness (0-255) sent to ledcontrol scales intensity
# separately, the same brightness + r g b split the tool uses.
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

RATE_LIMIT_S = 0.08  # 80 ms, at most this often while a slider drags
KEEPER_PERIOD_S = 5.0          # how often keeper_tick() actually checks
KEEPER_MAX_TRIES = 3  # failed re-applies in a row before giving up


# ---------------------------------------------------------------------------
# Pure colour helpers, no device and no I/O
# ---------------------------------------------------------------------------
def hue_to_rgb(hue, sat=1.0):
    """hue (any float, wraps mod 360) [+ optional saturation 0-1] -> (r, g, b) ints 0-255 at full
    value. The only HSV maths in this module.
    """
    hue = float(hue) % 360.0
    sat = 0.0 if sat < 0.0 else 1.0 if sat > 1.0 else float(sat)
    c = sat  # value fixed at 1.0 so chroma == saturation
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
    """Rough inverse of hue_to_rgb, (r, g, b) 0-255 -> (hue, sat), so rgb_view can put its hue
    slider near a preset or saved colour. Grey (sat 0) gives hue 0, which is fine since nothing
    reads hue for a colour that has none.
    """
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
    """mode is one of MODES, left/right are (r, g, b) 0-255, linked means the same colour on both
    sticks (colour_for()/with_colour() keep them together), and brightness is the single 0-255
    sent as ledcontrol's first arg. Immutable, replace()/with_colour() return a new LightState.
    """
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
# config.py bridge (the "lights" group)
# ---------------------------------------------------------------------------
def state_from_config(cfg, cfg_mod=None):
    """Builds a LightState from a loaded config dict. Falls back to LightState()'s defaults for
    anything config.get_value() cant answer (it returns None for an unknown key, never raises).
    """
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
    """The only place ledcontrol's argv gets built. All 7 numbers are checked as real ints 0-255
    (never a bool, a string or out of range) before anything reaches subprocess.
    tools/rg_break_tests.py breaks this to prove it.
    """
    vals = [brightness] + list(right) + list(left)
    if len(vals) != 7:
        raise ValueError("ledcontrol needs exactly 7 arguments, got %d" % len(vals))
    vals = [_validate_byte_arg(v) for v in vals]
    return [TOOL] + [str(v) for v in vals]


def _run_tool(argv, timeout=3.0):
    """The only function here that calls subprocess, argv only (no shell=True). It refuses anything
    that isnt exactly [TOOL, 7 numeric strings] even if a caller got past build_argv().
    """
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
    """mode "rocknix" does nothing at all so ES's own LED menu and battery mode keep working. "off"
    runs ledcontrol with brightness 0 and every channel 0. "colour" runs it with the state's
    brightness and its two colours (right first, per the tool). Returns None for "rocknix"
    (nothing ran), else run()'s bool.
    """
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
    """Reads back what's really lit on both sticks right now, using multi_index for the real
    channel order. Returns {"left": (r,g,b), "right": (r,g,b), "brightness": int}, or None if
    either ring cant be read (missing driver, wrong device, permissions), which the keeper takes
    as cant tell, do nothing.
    """
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
    return True  # MODE_ROCKNIX never gets here


def _describe(current):
    if current is None:
        return "unreadable"
    return "left=%s right=%s brightness=%d" % (current["left"], current["right"],
                                                current["brightness"])


# ---------------------------------------------------------------------------
# Controller: owns the live LightState, rate limits drags, runs the keeper
# ---------------------------------------------------------------------------
class Controller:
    """`run` (the ledcontrol call), `read` (read_current) and `now` (time.monotonic) can all be
    swapped so tests never touch a device. Not thread aware, main.py calls it from the UI thread
    and its own timers like everything else.
    """

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
        """A one-off change (mode, linked, a preset tap, a slider's final on_release value) applies
        right away with no rate limit.
        """
        self.state = state
        return self._apply_now()

    def drag(self, state):
        """A slider mid-drag applies now if `rate_limit` seconds have passed since the last apply,
        otherwise it just remembers the value. Call drag_flush() when the drag ends so the final
        value always gets applied.
        """
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
        """Call about every keeper_period seconds, plus on demand (force=True on app start or an ES wake
        event). Never acts in "rocknix" mode. read_current() is two small sysfs reads, it only
        re-applies (a subprocess call) when the device disagrees with `state`, and gives up with one
        log line after `max_tries` re-applies in a row that didnt stick. Returns a short status for
        tests.
        """
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
        """main.py calls this on ES's wake event, the moment ROCKNIX's own wake path most likely just ran
        `ledcontrol ${LED_STATE}` over whatever this set.
        """
        return self.keeper_tick(force=True)
