"""charge_limit: Safe Charge, the charge limit slider and its on/off toggle.

rocknix-config/battery-charge-limit is what enforces the limit at boot and on demand. It reads
/storage/charge-limit ("END" or "END START", no file = DEFAULT_END) and writes two sysfs
thresholds,

    /sys/class/power_supply/pm8150b-charger/charge_control_end_threshold
    /sys/class/power_supply/pm8150b-charger/charge_control_start_threshold

END first, then START. Lowering END below the current start makes the kernel pull start down
to end-5 by itself, so an explicit START write always follows to land where you asked. This
does the same from Settings: same two paths, same order, same saved file shape, so a value set
here survives a reboot like running battery-charge-limit by hand, and running it by hand shows
up here since read_live() always reads the real sysfs files.

Range 50-95 in steps of 5, default 85 (more in patches/YT4-NOTES.md):
Battery University (BU-808) says partial charging like 85%/25% already avoids most of the wear
of full cycles, and the longest-life zone (75%/65%) gives up too much for a handheld that's
also played on battery.
Every similar OEM limit caps at 80-85%: Steam Deck 80%, macOS 80%, Samsung's Protect battery
85%, ASUS presets 60/80/100%.
85% is also battery-charge-limit's own default, so if you never open this page nothing
changes.
Voltage, not percentage, is the main wear factor, and 85% keeps the pack voltage well under
full. 50 (the script's own floor) is the low end for more longevity. The top is 95, not 100,
so "no limit" is only ever the toggle's off state and never a slider position.
The fuel gauge reads low near full (it said 92% when the charger itself called the cell full),
so the true charge is a bit higher than the number shown.
"""
import logging

log = logging.getLogger("rp5deck.charge_limit")

CONF_PATH = "/storage/charge-limit"
PSY_DIR = "/sys/class/power_supply/pm8150b-charger"
END_PATH = PSY_DIR + "/charge_control_end_threshold"
START_PATH = PSY_DIR + "/charge_control_start_threshold"

DEFAULT_END = 85  # battery-charge-limit's own default, unchanged
MIN_END = 50  # the script's own minimum for an explicit limit
MAX_END = 95  # the slider's top, 100 is the toggle's off state
STEP = 5
START_MARGIN = 5  # START >= 20 and <= END - 5, always true as MIN_END - 5 = 45


def is_pct(token):
    """The script's own is_pct(): digits only, 0-100. Not plain int(), which also takes "-5" or
    "1_000" (str.isdigit() refuses both).
    """
    return isinstance(token, str) and token.isdigit() and int(token) <= 100


def read_saved(conf_path=CONF_PATH):
    """(end, start) the way the script's read_conf() works them out from the saved file:
    DEFAULT_END/None (the script then uses start = end - 5) if the file is missing, empty, or its
    first line has nothing parseable. Never raises. The script turns any non-digit into a space
    first (`tr -c '0-9 \\n' ' '`) while this only splits on whitespace, so a hand edited "END=85"
    would read differently. That doesnt matter since both only ever write a bare "END" or
    "END START" line.
    """
    try:
        with open(conf_path, encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return DEFAULT_END, None
    fields = first_line.split()
    end = DEFAULT_END
    start = None
    if fields and is_pct(fields[0]):
        end = int(fields[0])
    if len(fields) > 1 and is_pct(fields[1]):
        start = int(fields[1])
    return end, start


def _read_int(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_live(end_path=END_PATH, start_path=START_PATH):
    """{"available", "end", "start"}: the real current kernel thresholds, never a value something
    meant to set. available False (both None) means this kernel has no charge limit sysfs at all
    (the script does nothing then too), and main.App shows the Settings section as unavailable
    instead of a control that can never do anything.
    """
    end, start = _read_int(end_path), _read_int(start_path)
    return {"available": end is not None and start is not None, "end": end, "start": start}


def _write_int(path, value):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(value))
        return True
    except OSError as e:
        log.warning("charge_limit: could not write %s: %s", path, e)
        return False


def apply(end, conf_path=CONF_PATH, end_path=END_PATH, start_path=START_PATH):
    """Writes the limit the same way and in the same order as battery-charge-limit: END to sysfs
    first, then START (end - START_MARGIN, the script's default), then the saved file (a bare
    "100" for off, "END START" otherwise, so the script's own `status` and a hand run read exactly
    what was set). Reads both sysfs files back after and reports a mismatch instead of trusting the
    writes, since the kernel can refuse a pair (it takes 50..100, and START has to stay >= 20 and
    5 below END). `end` >= 100 means safe charge off, anything else gets clamped to
    [MIN_END, MAX_END] first so a caller cant set a limit outside the range. Never raises, and
    the paths are parameters so tests never touch real sysfs.
    """
    off = end >= 100
    end = 100 if off else max(MIN_END, min(MAX_END, int(end)))
    start = end - START_MARGIN
    if not _write_int(end_path, end):
        # the script's own fallback on a refused END: back to no limit instead of leaving the kernel on
        # a stale unknown value
        _write_int(end_path, 100)
        return {"ok": False, "end": end, "start": start,
               "detail": "kernel rejected end=%d" % end}
    start_ok = _write_int(start_path, start)
    try:
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write("100" if off else "%d %d" % (end, start))
    except OSError as e:
        log.warning("charge_limit: could not write %s: %s", conf_path, e)
    live = read_live(end_path, start_path)
    mismatch = (not start_ok) or live["end"] != end or (not off and live["start"] != start)
    if mismatch:
        return {"ok": False, "end": end, "start": start,
               "detail": "kernel holds end=%s start=%s" % (live["end"], live["start"])}
    return {"ok": True, "end": end, "start": start, "detail": ""}
