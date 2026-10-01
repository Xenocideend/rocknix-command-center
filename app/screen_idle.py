"""screen_idle: the Command Center's panel follows ROCKNIX's screensaver.

EmulationStation owns the idle timer (ScreenSaverTime) and the style (ScreenSaverBehavior), and
rp5deck already hears its screensaver-start/stop and wake hooks (esevents). Docked, ES is on the
add-on and the built-in panel shows the Command Center, which ES's screensaver never touched.
So at screensaver-start:

    dim                             backlight to DIM_FRACTION of its value (never 0)
    black, random video, slideshow  backlight 0 (AMOLED, dark is most of the saving)
    suspend (or anything else)      nothing, the device is going to sleep anyway

Only the backlight is used. rp5deck never sends sway commands, and a backlight change wakes
instantly and keeps touch working, which `output power off` might not.

screensaver-stop, wake or a touch on the panel put it back. A touch that wakes the panel gets
swallowed so it cant press whatever's under your finger. Undocked nothing happens, ES is on the
built-in panel then.

What gets changed is written to STATE_PATH before the change, and restore_leftovers() (at
startup) undoes a record a crash left, so a dead rp5deck cant leave the panel dark or dimmed
past its next start. Every side effect is injected so tests run offline.
"""
import json
import logging
import os
import re

import esevents
import screen_map

ES_SETTINGS = "/storage/.config/emulationstation/es_settings.cfg"
BACKLIGHT = screen_map.backlight_path()
STATE_PATH = "/run/rp5deck-screen-idle.json"
DIM_FRACTION = 0.15
DIM_MIN = 8
POWER_OFF_STYLES = ("black", "random video", "slideshow")
log = logging.getLogger("rp5deck.screen_idle")


def es_screensaver_behavior(path=ES_SETTINGS):
    """ES only writes settings that differ from its default, and its default is 'dim'."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r'<string name="ScreenSaverBehavior" value="([^"]*)"', f.read())
    except OSError:
        return "dim"
    return m.group(1) if m else "dim"


class ScreenIdle:
    def __init__(self, is_docked, behavior=es_screensaver_behavior,
                 backlight=BACKLIGHT, state_path=STATE_PATH):
        self.is_docked = is_docked        # callable() -> bool
        self.behavior = behavior          # callable() -> str
        self.backlight = backlight
        self.state_path = state_path
        self.active = None                # None | {"kind": "dim"|"off", "brightness": n}

    # -- sysfs / state ---------------------------------------------------------
    def _read_brightness(self):
        with open(os.path.join(self.backlight, "brightness")) as f:
            return int(f.read().strip())

    def _write_brightness(self, v):
        with open(os.path.join(self.backlight, "brightness"), "w") as f:
            f.write(str(int(v)))

    def _save(self, st):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, self.state_path)

    def _clear(self):
        try:
            os.remove(self.state_path)
        except OSError:
            pass

    # -- actions ---------------------------------------------------------------
    def idle(self):
        if self.active is not None or not self.is_docked():
            return False
        style = (self.behavior() or "").strip().lower()
        if style == "dim":
            kind = "dim"
        elif style in POWER_OFF_STYLES:
            kind = "off"
        else:
            return False
        try:
            cur = self._read_brightness()
        except (OSError, ValueError) as e:
            log.warning("screen idle: backlight unreadable (%s)", e)
            return False
        if cur <= 0:
            return False  # already dark, nothing to restore to
        st = {"kind": kind, "brightness": cur}
        self._save(st)  # record first so a crash can be undone
        try:
            self._write_brightness(max(DIM_MIN, int(cur * DIM_FRACTION)) if kind == "dim" else 0)
        except OSError as e:
            log.warning("screen idle: backlight write failed (%s)", e)
            self._clear()
            return False
        self.active = st
        log.info("screen idle: %s (ES screensaver %r)", st["kind"], style)
        return True

    def wake(self, why="wake"):
        st = self.active
        if st is None:
            return False
        self._undo(st)
        self.active = None
        self._clear()
        log.info("screen idle: restored after %s (%s)", st["kind"], why)
        return True

    def _undo(self, st):
        try:
            self._write_brightness(st["brightness"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("screen idle: brightness restore failed (%s)", e)

    def restore_leftovers(self):
        """At startup, undoes a record a crashed rp5deck left behind."""
        try:
            with open(self.state_path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return False
        self._undo(st)
        self._clear()
        log.info("screen idle: undid a leftover %s from a previous run", st.get("kind"))
        return True

    # -- hooks -----------------------------------------------------------------
    def on_es_event(self, kind):
        if kind == esevents.SCREENSAVER_START:
            return self.idle()
        if kind in (esevents.SCREENSAVER_STOP, esevents.WAKE):
            return self.wake(kind)
        return False

    def on_touch(self):
        """True when the touch woke the panel (the caller swallows it)."""
        return self.wake("touch")

    def shutdown(self):
        self.wake("shutdown")
