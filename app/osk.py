"""osk - the on-screen keyboard for typing into Firefox on the bottom screen.

rp5deck's own text fields (YouTube search) use ui.Keyboard, drawn on the
layer surface. Typing into FIREFOX is different: the keys must reach
Firefox's text field, and Firefox only takes keys while it has keyboard
focus. The owner gives it focus by tapping the page (sway focuses a window
on a tap; nothing in rp5deck sends focus commands).

Backend: `wvkbd-mobintl`, ROCKNIX's own on-screen keyboard, already on the
device. It is a zwlr_layer_surface_v1 with keyboard interactivity off that
types through zwp_virtual_keyboard_v1 into whatever has focus. Proven on this
device by E1b (experiments/e1b-layer.sh): with `-L 300 --output DSI-1` it
drew on DSI-1's bottom 300 px, received the taps, and focus never moved -
so showing it cannot take focus from Firefox (or from a game).

Only the two flags E1b proved are used (`-L <height>`, `--output <name>`).
Show = start the process, hide = stop it: no reliance on --hidden or on
SIGUSR1/SIGUSR2, which this build has not been checked for.

Not covering the field: wvkbd anchors to the bottom edge and (upstream
main.c) sets an exclusive zone equal to its height, so sway shrinks the
Firefox window above it instead of drawing over it; rp5deck's BAR strip has
its own exclusive zone, so the keyboard stacks above the strip. After the
keyboard appears, rp5deck also asks Firefox (Marionette) to scroll the
focused element into view. The exclusive zone is UNVERIFIED on the device:
see the HF1 device checklist (step K3).

When to show it (OskPolicy, pure):
  auto    Firefox has sway focus AND its active element is editable (a
          Marionette probe) - or the owner pressed Keyboard. Pressing
          Keyboard while it is up hides it until the next text field.
  button  only the Keyboard button shows / hides it
  off     never; the Keyboard button is not offered
It is shown only while Firefox itself is focused: with ES or a game focused
the keys would go to them instead.
"""
import os
import signal
import subprocess

WVKBD_BIN = "wvkbd-mobintl"
DEFAULT_HEIGHT = 360            # px on the 1080 px panel: Firefox keeps 1080-140-360
MODES = ("auto", "button", "off")
DEFAULT_MODE = "auto"
HIDE_AFTER = 2                  # consecutive "not wanted" polls before an auto hide
STOP_WAIT = 1.0


def resolve_mode(cfg_value=None, env=None):
    """The OSK mode: config value if valid, else $RP5DECK_OSK, else auto."""
    env = os.environ if env is None else env
    for v in (cfg_value, env.get("RP5DECK_OSK")):
        if isinstance(v, str) and v.strip().lower() in MODES:
            return v.strip().lower()
    return DEFAULT_MODE


class OskPolicy:
    """Decides whether the keyboard should be up. Pure: no process, no clock.

    update(focused, editable) is fed by the poll (Firefox focused in sway,
    Marionette says the active element takes text) and returns the wanted
    visibility. toggle(focused) is the Keyboard button; it returns
    (wanted, hint) where hint is a short message for the strip or None.
    Showing is immediate; an auto hide needs HIDE_AFTER polls in a row, so
    a field-to-field hop does not make the keyboard flicker. A toggle or
    reset() applies at once."""

    def __init__(self, mode=DEFAULT_MODE):
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.manual = None          # True: owner asked for it; False: owner hid it
        self.wanted = False
        self._misses = 0
        self._last_editable = False

    def enabled(self):
        return self.mode != "off"

    def reset(self):
        self.manual = None
        self.wanted = False
        self._misses = 0
        self._last_editable = False

    def _want(self, focused, editable):
        if not focused or self.mode == "off":
            return False
        if self.mode == "button":
            return self.manual is True
        if self.manual is False:
            return False
        return bool(editable) or self.manual is True

    def update(self, focused, editable):
        if self.mode == "off":
            self.wanted = False
            return False
        editable = bool(editable) and bool(focused)
        if self.manual is False and editable and not self._last_editable:
            self.manual = None      # a NEW text field: the owner's "hide" is over
        self._last_editable = editable
        want = self._want(focused, editable)
        if want:
            self._misses = 0
            self.wanted = True
        elif self.wanted:
            self._misses += 1
            if self._misses >= HIDE_AFTER:
                self.wanted = False
                self._misses = 0
        return self.wanted

    def toggle(self, focused):
        if self.mode == "off":
            return False, None
        if self.wanted:
            self.manual = False
            self.wanted = False
            self._misses = 0
            return False, None
        self.manual = True
        if not focused:
            self.wanted = False
            return False, "Tap the page first"
        self.wanted = True
        self._misses = 0
        return True, None


class Wvkbd:
    """Runs wvkbd-mobintl on one output. show() starts it (idempotent),
    hide() stops it with a bounded wait and a SIGKILL fallback, so a hidden
    keyboard never lingers as a process - or as a surface over the panel.
    popen is injectable for tests."""

    def __init__(self, output="DSI-1", height=DEFAULT_HEIGHT, binary=WVKBD_BIN,
                 popen=subprocess.Popen):
        self.output = output
        self.height = int(height)
        self.binary = binary
        self.popen = popen
        self.proc = None
        self.starts = 0
        self.error = None

    def build_command(self):
        return [self.binary, "-L", str(self.height), "--output", self.output]

    def _env(self):
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", "/run/0-runtime-dir")
        env.setdefault("WAYLAND_DISPLAY", "wayland-1")
        return env

    def visible(self):
        return self.proc is not None and self.proc.poll() is None

    def pid(self):
        return self.proc.pid if self.visible() else None

    def show(self):
        if self.visible():
            return True
        try:
            self.proc = self.popen(self.build_command(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=self._env())
        except OSError as e:
            self.proc = None
            self.error = "cannot start %s: %s" % (self.binary, e)
            return False
        self.starts += 1
        self.error = None
        return True

    def hide(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=STOP_WAIT)
            except Exception:       # noqa: BLE001 - nothing more can be done
                pass
        except Exception:           # noqa: BLE001
            pass
