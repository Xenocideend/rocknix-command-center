"""brightness: both screens' brightness for the Command Center, plus keeping them matched.

  bottom  the built-in AMOLED's backlight (sysfs, hardware): BottomBacklight
  top     the add-on has no reachable backlight or DDC/CI (see gamma_dim.py), so it's
          dimmed in software by gamma_dim.py, a helper holding sway's gamma control for
          DP-1: TopDim. 100% means no helper at all.
  match   keeps the top screen in step with the bottom at the ratio set when Match was
          turned on: matched_top()

Everything touching the system is injected, so tests run offline.
"""
import logging
import os
import subprocess
import sys

import screen_map

BACKLIGHT = "/sys/class/backlight/ae94000.dsi.0"
BOTTOM_MIN_PCT = 5  # never 0, a black panel with no way to see the slider
TOP_MIN_PCT = 20            # below this the add-on is unreadable in daylight
GAMMA_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gamma_dim.py")
log = logging.getLogger("rp5deck.brightness")


def ramps(size, level, min_level=0.05):
    """3 x size uint16 gamma ramps (R, G, B), linear, scaled by level, little endian bytes, the buffer
    zwlr_gamma_control_v1.set_gamma expects.
    """
    level = max(min_level, min(1.0, float(level)))
    one = bytearray()
    for i in range(size):
        v = int(round(65535 * (i / float(max(1, size - 1))) * level))
        one += v.to_bytes(2, "little")
    return bytes(one) * 3


def matched_top(bottom_pct, ratio):
    """The top brightness Match keeps: bottom x ratio, within TOP_MIN_PCT..100."""
    return int(max(TOP_MIN_PCT, min(100, round(bottom_pct * ratio))))


def match_ratio(top_pct, bottom_pct):
    return float(top_pct) / max(1.0, float(bottom_pct))


class BottomBacklight:
    def __init__(self, path=BACKLIGHT):
        self.path = path

    def _read(self, name):
        with open(os.path.join(self.path, name)) as f:
            return int(f.read().strip())

    def max(self):
        return self._read("max_brightness")

    def get_pct(self):
        """The live value as a percentage, or None if unreadable."""
        try:
            mx = self.max()
            return int(round(100.0 * self._read("brightness") / mx)) if mx else None
        except (OSError, ValueError):
            return None

    def set_pct(self, pct):
        pct = max(BOTTOM_MIN_PCT, min(100, int(pct)))
        try:
            raw = max(1, int(round(self.max() * pct / 100.0)))
            with open(os.path.join(self.path, "brightness"), "w") as f:
                f.write(str(raw))
            return True
        except (OSError, ValueError) as e:
            log.warning("bottom brightness: %s", e)
            return False


class TopDim:
    """Owns gamma_dim.py for one output. set_pct(100) stops it (sway puts the normal gamma back),
    anything lower starts it if needed and feeds it the level.
    """

    def __init__(self, output=None, spawn=None):
        self.output = output or screen_map.CURRENT.top
        self.spawn = spawn or (lambda: subprocess.Popen(
            [sys.executable, GAMMA_HELPER, output], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, bufsize=1))
        self.proc = None
        self.pct = 100

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def set_pct(self, pct):
        pct = max(TOP_MIN_PCT, min(100, int(pct)))
        self.pct = pct
        if pct >= 100:
            self.stop()
            return True
        if not self.running():
            try:
                self.proc = self.spawn()
            except OSError as e:
                log.warning("top brightness: cannot start the gamma helper (%s)", e)
                self.proc = None
                return False
        try:
            self.proc.stdin.write("%.3f\n" % (pct / 100.0))
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError) as e:
            log.warning("top brightness: gamma helper gone (%s)", e)
            self.proc = None
            return False

    def stop(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.stdin.close()  # EOF, the helper exits and sway puts gamma back
            p.wait(timeout=2)
        except Exception:                    # noqa: BLE001
            try:
                p.kill()
            except Exception:                # noqa: BLE001
                pass
