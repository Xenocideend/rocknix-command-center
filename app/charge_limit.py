"""charge_limit - Safe Charge (owner: "change the 85% charge limit to a
safe-charge slider ... and a toggle to disable safe charge mode").

rocknix-config/095-charge-limit (Main's file, never edited here - see
patches/YT4-NOTES.md for the one change suggested to Main) is what actually
enforces a charge limit at boot and on demand: it reads /storage/charge-
limit ("END" or "END START"; no file = DEFAULT_END) and writes two sysfs
thresholds,

    /sys/class/power_supply/pm8150b-charger/charge_control_end_threshold
    /sys/class/power_supply/pm8150b-charger/charge_control_start_threshold

END first, then START - lowering END below the CURRENT start threshold
makes the kernel pull start down to end-5 itself (095's own comment), so an
explicit START write always follows to land exactly where the owner asked,
not wherever the kernel's own side effect left it. This module mirrors that
exact contract from the Settings UI side: same two file paths, same write
order, same saved-file shape - a value set here survives the next boot
exactly as if the owner had run 095 by hand, and running 095 by hand is
reflected back here, since read_live() always reads the real sysfs files,
never a value rp5deck cached.

Range/step/default (a short review, cited in full in patches/YT4-NOTES.md):
50-95 in steps of 5, default 85.
  - Battery University's own guidance (BU-808): partial charging - e.g.
    85%/25% - already gives most of full-charge cycling's capacity loss
    avoided; the highest-longevity zone (75%/65%) trades away too much
    usable capacity for a handheld that is also played ON battery, not just
    charged.
  - Every OEM equivalent surveyed caps at 80-85%, never lower, for a
    similar always-plugged-in-sometimes device: Steam Deck's SteamOS
    "Battery Charge Limit" (fixed 80%), Apple's macOS "Charge Limit" (80%),
    Samsung's "Protect battery" (85% on older One UI, still the "Basic"
    ceiling on newer), ASUS Battery Health Charging's presets (60/80/100%).
  - 85% is also 095-charge-limit's OWN existing DEFAULT (unchanged here on
    purpose): an owner who never opens this Settings page keeps exactly the
    behaviour they already had.
  - The RP5's own cell (this device's own charger fix) floats at 4.40 V,
    cutting at 0.5C - voltage, not percentage, is the primary stress factor
    Battery University cites, and 85% keeps real pack voltage meaningfully
    below the 4.40 V float the charger would otherwise hold it at
    indefinitely. 50% (095's own accepted floor) is offered as the
    slider's low end for an owner who wants to trade more capacity for
    longevity; 95 (not 100) is the slider's own top so "no limit" stays the
    toggle's OFF state alone, never also a slider position.
  - The fuel gauge reads LOW near full (measured: it reported 92% when the
    charger itself considered the cell full), so any of these percentages
    corresponds to a somewhat higher true state of charge than the number
    shown - 095's own comment, repeated here since it is exactly why a
    slider reading "85" is not literally 85% of the cell's real capacity.
"""
import logging

log = logging.getLogger("rp5deck.charge_limit")

CONF_PATH = "/storage/charge-limit"
PSY_DIR = "/sys/class/power_supply/pm8150b-charger"
END_PATH = PSY_DIR + "/charge_control_end_threshold"
START_PATH = PSY_DIR + "/charge_control_start_threshold"

DEFAULT_END = 85            # 095-charge-limit's own DEFAULT - unchanged here
MIN_END = 50                 # 095's own accepted minimum for an explicit limit
MAX_END = 95                  # the slider's own top; 100 is the toggle's OFF state
STEP = 5
START_MARGIN = 5             # 095: START must be >= 20 and <= END - 5 (always true
                              # here since MIN_END - START_MARGIN = 45 >= 20)


def is_pct(token):
    """095's own is_pct(): digits only, 0-100 - not a Python int() call
    alone, since that would also accept "-5" or "1_000" (str.isdigit()
    refuses both)."""
    return isinstance(token, str) and token.isdigit() and int(token) <= 100


def read_saved(conf_path=CONF_PATH):
    """(end, start) the way 095's own read_conf() would compute them from
    the saved file: DEFAULT_END/None (095 then derives start = end - 5) if
    the file is missing, empty, or its first line has nothing parseable.
    Never raises. Simplification versus 095's own `tr -c '0-9 \\n' ' '`
    (which turns any non-digit character into a space before splitting):
    this only whitespace-splits, so a hand-edited line with digits glued to
    other text (e.g. "END=85") would not parse the same way there as here -
    harmless in practice, since both 095 and this module only ever WRITE a
    bare "END" or "END START" line themselves."""
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
    """{"available", "end", "start"} - the REAL, current kernel thresholds,
    never a value this module or 095 merely intended to set. "available"
    False (both None) means this kernel build has no charge-limit sysfs at
    all (095 itself no-ops the same way, "this kernel has no charge limit
    support") - the caller (main.App) shows the Settings section as
    unavailable rather than a control that can never actually do anything."""
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
    """Write the limit through, in 095-charge-limit's own order and shape:
    END to sysfs first, then START (end - START_MARGIN - 095's own
    derivation for "no explicit start"), then the saved file (a bare "100"
    for "off", "END START" otherwise - 095's own two write shapes, so its
    own `status` display and any hand run of it reads exactly what this
    module set). Reads both sysfs files back afterwards and reports a
    mismatch rather than trusting either write silently succeeded - a
    kernel can reject an END/START pair for reasons this module cannot
    predict (095's own comment: "kernel accepts 50..100"; START must stay
    >= 20 and >= 5 below END). `end` >= 100 is "safe charge off" (095's own
    "no limit"); any other value is clamped into [MIN_END, MAX_END] first,
    so a caller can never accidentally arm a limit outside the reviewed
    range. Never raises - every filesystem call is wrapped. Paths are
    parameters (never a hardcoded /sys/.../ open) so tests never touch real
    sysfs."""
    off = end >= 100
    end = 100 if off else max(MIN_END, min(MAX_END, int(end)))
    start = end - START_MARGIN
    if not _write_int(end_path, end):
        # 095's own fallback on a rejected END: force back to "no limit"
        # rather than leave the kernel holding a stale, unknown value.
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
