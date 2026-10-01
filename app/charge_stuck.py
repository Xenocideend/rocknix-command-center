"""charge_stuck: the "charger connected but not charging" guard, a stopgap for a kernel bug.

After the handheld has been powering the add-on, a charger plugged into the add-on later can
leave the pm8150b charger wedged. The PD contract comes up (sink, 9 V, typec's power_role and
the IIO USB-in voltage both say a charger is there) but the input current limit never starts,
so the battery keeps draining under what looks like a normal charging session. Unplugging the
charger for about 5 seconds and plugging it back in fixes it. This only reads sysfs/debugfs,
never writes, it just notices the wedge and says so.

Two parts:
  Sampler.sample()   one reading of the hardware -> a plain dict. Every file is optional
                     (missing or unreadable never raises), and it's cheap while unplugged (one
                     sysfs read) since main.py runs it every SAMPLE_PERIOD_S forever.
  evaluate(samples)  pure: a list of those dicts, oldest first -> (stuck, reason). No
                     filesystem, no clock past each sample's own "t".

Detection, as measured on the device:
  plugged   /sys/class/typec/port0/power_role has "[sink]" (the other state, "[source] sink",
            is the handheld powering the add-on, which drains ~860 mA and is normal, never
            checked further) and the USB-in voltage
            (/sys/bus/iio/devices/iio:device*/in_voltage_usb_in_v_div_16_input, microvolts,
            found by glob once and cached, 8992720 on a 9 V charger) is over
            PLUGGED_VOLTAGE_UV.
  current   /sys/class/power_supply/battery/current_now, microamps, positive = draining
            (wedged: +535000..+874000, while fine but paused by the 85% limit: about 0,
            negative while really charging).
  ICL_STATUS (reg 0x1107) / POWER_PATH (reg 0x110b, only logged, never decides) are read only
            while plugged and current_now is over DISCHARGE_UA, since debugfs isnt free and
            the two cheap reads gate it. /sys/kernel/debug/regmap/0-02/registers has one line
            per register, exactly 9 bytes "rrrr: vv\\n", so read_register() seeks straight to
            reg*9 instead of walking 65536 registers (~0.2 s) every SAMPLE_PERIOD_S.
            Fine: ICL_STATUS 0x0b..0x0d (550-650 mA), POWER_PATH 0x95.
            Wedged, steady: ICL_STATUS 0x00, POWER_PATH 0x13.
            Wedged, flipping: ICL_STATUS 0x00 <-> "500 mA" and POWER_PATH 0x95 <-> 0x13 about
            once a second, so one 10 s sample only catches whichever it lands on. That's why
            the rule is "at least half the samples read ICL_STATUS==0", not every sample.
  stuck     over the samples within WINDOW_S of the newest: plugged in every one, mean
            current_now over DISCHARGE_UA, and ICL_STATUS==0 in at least half of the ones
            with a register reading. Heavy gaming can push the mean over DISCHARGE_UA on a
            good charger too, but ICL_STATUS stays nonzero then, and the half rule keeps that
            from warning.
  fast clear beats the window right away (never waits for an old bad sample to age out):
            the newest sample unplugged, or the last two samples in a row each showing
            current_now <= 0 or a nonzero ICL_STATUS.

Only ever reads files, and every path is a parameter so tests never touch real sysfs/debugfs.
"""
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
REG_LINE_LEN = 9  # "rrrr: vv\n", never read any other way

PLUGGED_VOLTAGE_UV = 4_500_000  # over 4.5 V on the USB-in rail
DISCHARGE_UA = 100_000  # over 100 mA draining (the windowed rule)
WINDOW_S = 60.0
MIN_SAMPLES = 3  # one noisy read can never declare stuck on its own


# ---------------------------------------------------------------------------
# The seek-based register reader
# ---------------------------------------------------------------------------
def read_register(reg, path=REGMAP_PATH, opener=open):
    """The register's value (0-255), or None if the file is missing, unreadable, short or malformed.
    Seeks straight to the register's own 9-byte line and reads nothing else. The register number
    in the line gets checked against `reg` in case the offset maths ever drifts.
    """
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
    """One sample that on its own shows the charger is fine again, the fast clear test. Not the same
    threshold as the windowed check (DISCHARGE_UA) on purpose, it's current_now <= 0 so recovery
    isnt called just because the load dipped under 100 mA.
    """
    if s.get("current_ua") is not None and s["current_ua"] <= 0:
        return True
    return s.get("icl") is not None and s["icl"] != 0


def evaluate(samples, window_s=WINDOW_S, min_samples=MIN_SAMPLES):
    """samples: oldest first, a list of {"t", "plugged", "current_ua", "icl"} in Sampler.sample()'s
    shape, current_ua/icl None when not read (unplugged, or the gate for a register read wasnt
    met). Returns (stuck: bool, reason: str), and reason is always plain key/value numbers, never
    file content.
    """
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
    """Everything one sample() reads. Every path and callable can be swapped (tests never touch real
    sysfs/debugfs). The voltage file is found by glob once and cached, same as cleanstate.Helper's
    I/O.
    """

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
        self._voltage_path = None  # cached once a glob actually matches
        self._regmap_warned = False  # log once at debug when debugfs cant be read

    def _voltage_file(self):
        if not self._voltage_path:
            matches = sorted(self.glob_fn(self.voltage_glob))
            self._voltage_path = matches[0] if matches else None
        return self._voltage_path

    def sample(self):
        """One reading in evaluate()'s sample shape. Cheap while unplugged, one text read and nothing
        else. Never raises.
        """
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
