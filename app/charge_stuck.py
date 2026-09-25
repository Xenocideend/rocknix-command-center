"""charge_stuck - "charger connected but not charging" guard (owner-approved
stopgap for a kernel bug, fix deferred).

On this kernel, after the handheld has been powering the add-on (sourcing
VBUS to it), a charger later plugged into the add-on can leave the pm8150b
charger wedged: the USB PD contract comes up (sink, 9 V - typec's own
power_role and the IIO USB-in voltage both agree a charger is present) but
the charger's input-current-limit stage never actually starts, so the
battery keeps discharging under what looks, from the OS's point of view,
like a normal charging session. Unplugging the charger from the add-on for
about 5 seconds and plugging it back in recovers it (untouched by this
module: it only ever reads sysfs/debugfs, never writes anything). This is
the stopgap the owner asked for: notice the wedged state and say so.

Two pieces:
  Sampler.sample()   one reading of the real hardware -> a plain dict; every
                     file is optional (missing/unreadable never raises), and
                     it is CHEAP while unplugged (a single sysfs read) since
                     it runs every SAMPLE_PERIOD_S (main.py) forever.
  evaluate(samples)  pure: a chronological list of those dicts -> (stuck,
                     reason). No filesystem, no clock beyond what is already
                     in the samples' own "t" field.

Detection, exactly as measured on the device (tonight, 25 Sep):
  plugged   /sys/class/typec/port0/power_role contains "[sink]" (the OTHER
            state, "[source] sink", is the handheld POWERING the add-on -
            battery drains ~860 mA then and that is NORMAL, never checked
            further) AND the USB-in voltage - /sys/bus/iio/devices/
            iio:device*/in_voltage_usb_in_v_div_16_input, microvolts, found
            by glob ONCE and cached (tonight: iio:device1, 8992720 on a 9 V
            charger) - is > PLUGGED_VOLTAGE_UV.
  current   /sys/class/power_supply/battery/current_now, microamps, POSITIVE
            = discharging (tonight stuck: +535000..+874000; good even while
            plugged, charging merely paused by the 85% safe-charge limit:
            ~0; negative while actually charging).
  ICL_STATUS (reg 0x1107) / POWER_PATH (reg 0x110b, logged only, never part
            of the decision) - read ONLY while plugged AND current_now is
            over DISCHARGE_UA (reading debugfs is not free; this is the
            expensive half of the check, gated by the two cheap reads
            above) - /sys/kernel/debug/regmap/0-02/registers, one line per
            register, each EXACTLY 9 bytes "rrrr: vv\\n": read_register()
            seeks straight to reg*9 rather than the 65536-register, ~0.2 s
            walk a naive full read would cost every SAMPLE_PERIOD_S.
            Good: ICL_STATUS 0x0b..0x0d (550-650 mA), POWER_PATH 0x95.
            Stuck (steady variant, tonight): ICL_STATUS 0x00, POWER_PATH
            0x13, both steady. Stuck (flipping variant, not seen tonight but
            known): ICL_STATUS flips 0x00 <-> "500 mA", POWER_PATH flips
            0x95 <-> 0x13, about once a second - a single 10 s sample only
            catches whichever value happened to be current, hence the
            "at least half the samples read ICL_STATUS==0" rule below
            rather than requiring every sample to agree.
  stuck     over the samples within WINDOW_S of the newest one: plugged in
            EVERY one, mean current_now > DISCHARGE_UA, AND ICL_STATUS==0 in
            at least half of the samples that got a register reading.
            Heavy gaming can push mean current over DISCHARGE_UA on a GOOD
            charger too, but ICL_STATUS reads nonzero throughout then - the
            ICL half-of-samples rule is what keeps that case from warning.
  fast clear over-rides the windowed rule instantly (never waits for a
            60 s-old bad sample to fall out of the window): the newest
            sample being unplugged, or the last TWO consecutive samples each
            showing current_now <= 0 or a nonzero ICL_STATUS reading.

Never touches the device beyond reading files; every path is a parameter
(tests never touch real sysfs/debugfs)."""
import glob
import logging
import time

log = logging.getLogger("rp5deck.charge_stuck")

TYPEC_POWER_ROLE = "/sys/class/typec/port0/power_role"
IIO_VOLTAGE_GLOB = "/sys/bus/iio/devices/iio:device*/in_voltage_usb_in_v_div_16_input"
BATTERY_CURRENT = "/sys/class/power_supply/battery/current_now"
REGMAP_PATH = "/sys/kernel/debug/regmap/0-02/registers"

REG_ICL_STATUS = 0x1107
REG_POWER_PATH = 0x110B
REG_LINE_LEN = 9              # "rrrr: vv\n" - never read any other way

PLUGGED_VOLTAGE_UV = 4_500_000       # > 4.5 V on the USB-in rail
DISCHARGE_UA = 100_000               # > 100 mA discharging (the windowed rule)
WINDOW_S = 60.0
MIN_SAMPLES = 3                      # a single noisy read can never alone declare "stuck"


# ---------------------------------------------------------------------------
# The seek-based register reader
# ---------------------------------------------------------------------------
def read_register(reg, path=REGMAP_PATH, opener=open):
    """The register's value (0-255), or None if the file is missing,
    unreadable, short, or malformed. Seeks straight to the register's own
    9-byte line - never reads anything else in the file. The register
    number encoded in the line is checked against `reg` as a sanity check
    against the offset math ever drifting silently."""
    try:
        with opener(path, "rb") as f:
            f.seek(reg * REG_LINE_LEN)
            line = f.read(REG_LINE_LEN)
    except OSError:
        return None
    if len(line) != REG_LINE_LEN or line[-1:] != b"\n":
        return None
    try:
        num_part, val_part = line[:-1].split(b":")
        if int(num_part, 16) != reg:
            return None
        return int(val_part.strip(), 16)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Pure: samples -> stuck?
# ---------------------------------------------------------------------------
def _looks_recovered(s):
    """One sample that, on its own, is evidence the charger is fine again -
    the fast-clear rule's per-sample test. Deliberately NOT the same
    threshold as the windowed "discharging" check (DISCHARGE_UA): the owner
    asked for current_now <= 0 here specifically, so recovery is not
    declared just because the load briefly dropped under 100 mA."""
    if s.get("current_ua") is not None and s["current_ua"] <= 0:
        return True
    return s.get("icl") is not None and s["icl"] != 0


def evaluate(samples, window_s=WINDOW_S, min_samples=MIN_SAMPLES):
    """samples: chronological (oldest first) list of {"t", "plugged",
    "current_ua", "icl"} - Sampler.sample()'s own shape; current_ua/icl are
    None when not read (unplugged, or the gate that guards a register read
    was not met). Returns (stuck: bool, reason: str) - reason is always
    plain key/value numbers, never file content."""
    if not samples:
        return False, "no samples yet"
    last = samples[-1]
    if not last["plugged"]:
        return False, "unplugged"
    recent = samples[-2:]
    if len(recent) == 2 and all(_looks_recovered(s) for s in recent):
        return False, "recovered: last 2 samples both look fine"
    window = [s for s in samples if last["t"] - s["t"] <= window_s]
    if len(window) < min_samples:
        return False, "not enough samples yet (%d/%d)" % (len(window), min_samples)
    if not all(s["plugged"] for s in window):
        return False, "not plugged for the whole window"
    currents = [s["current_ua"] for s in window if s["current_ua"] is not None]
    if not currents:
        return False, "no current_now readings in the window"
    mean_current = sum(currents) / len(currents)
    if mean_current <= DISCHARGE_UA:
        return False, "not discharging (mean current %.0f uA)" % mean_current
    icls = [s["icl"] for s in window if s["icl"] is not None]
    if not icls:
        return False, "no ICL_STATUS readings in the window"
    zero_count = sum(1 for v in icls if v == 0)
    if zero_count * 2 < len(icls):
        return False, "ICL_STATUS mostly nonzero (0 in %d/%d)" % (zero_count, len(icls))
    return True, ("mean current %.0f uA, ICL_STATUS==0 in %d/%d samples over the last %g s"
                 % (mean_current, zero_count, len(icls), window_s))


# ---------------------------------------------------------------------------
# Real hardware
# ---------------------------------------------------------------------------
def _read_text(opener, path):
    if not path:
        return None
    try:
        with opener(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _read_int(opener, path):
    if not path:
        return None
    try:
        with opener(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class Sampler:
    """Everything one sample() reads. Every path/callable is injectable
    (tests never touch real sysfs/debugfs); the voltage file's real path is
    found by glob once and cached, same "cache after the first real answer"
    shape as cleanstate.Helper's own I/O seams."""

    def __init__(self, power_role_path=TYPEC_POWER_ROLE, voltage_glob=IIO_VOLTAGE_GLOB,
                 current_path=BATTERY_CURRENT, regmap_path=REGMAP_PATH,
                 glob_fn=glob.glob, opener=open, clock=time.time):
        self.power_role_path = power_role_path
        self.voltage_glob = voltage_glob
        self.current_path = current_path
        self.regmap_path = regmap_path
        self.glob_fn = glob_fn
        self.opener = opener
        self.clock = clock
        self._voltage_path = None          # cached once a glob actually matches
        self._regmap_warned = False        # "log once at debug" when debugfs is unreadable

    def _voltage_file(self):
        if not self._voltage_path:
            matches = sorted(self.glob_fn(self.voltage_glob))
            self._voltage_path = matches[0] if matches else None
        return self._voltage_path

    def sample(self):
        """One reading -> evaluate()'s own sample shape. Cheap while
        unplugged: a single text read, nothing else. Never raises."""
        t = self.clock()
        role = _read_text(self.opener, self.power_role_path)
        if role is None or "[sink]" not in role:
            return {"t": t, "plugged": False, "current_ua": None, "icl": None}
        voltage = _read_int(self.opener, self._voltage_file())
        if voltage is None or voltage <= PLUGGED_VOLTAGE_UV:
            return {"t": t, "plugged": False, "current_ua": None, "icl": None}
        current = _read_int(self.opener, self.current_path)
        icl = None
        if current is not None and current > DISCHARGE_UA:
            icl = read_register(REG_ICL_STATUS, self.regmap_path, opener=self.opener)
            if icl is None:
                if not self._regmap_warned:
                    log.debug("charge_stuck: could not read %s (debugfs not mounted, or not "
                             "root?) - never warning without a real ICL_STATUS reading",
                             self.regmap_path)
                    self._regmap_warned = True
            else:
                power_path = read_register(REG_POWER_PATH, self.regmap_path, opener=self.opener)
                log.debug("charge_stuck: current=%d uA ICL_STATUS=0x%02x POWER_PATH=%s",
                         current, icl,
                         ("0x%02x" % power_path) if power_path is not None else "?")
        return {"t": t, "plugged": True, "current_ua": current, "icl": icl}
