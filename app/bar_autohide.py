"""bar_autohide - AH: auto-hide the BAR strip for the YouTube TV app only.

The owner (24 Sep): the YouTube TV app (web_tiles.YtAppSession, screens.Bar's
"tv" app kind) reserves the same BAR_H = 140 px strip every other BAR app
does (Browser/Discord/mpv), which leaves YouTube's 16:9 leanback UI with
black bars at the sides instead of filling the whole 1920x1080 panel. The
fix is BAR-app-specific, not a new sway_ipc mode: sway_ipc.compute_mode()
still reports plain BAR (an rp5deck-* window on DSI-1); only what main.App's
geometry() hands the layer surface for that BAR changes, and only while
screens.Bar.app == "tv" (screens.py's own BAR_H docstring / HF1's per-app
strip is otherwise unchanged - Browser/Discord/mpv still get the fixed
140 px reserved zone exactly as before).

States (main.App owns the transitions via on_mode()/touch(); this class is
pure bookkeeping plus one timer, the same call_later/cancel idiom as the
pull-down Command Center's own auto-close countdown - main.py's _activity()/
summon.py's rearm_timeout()):

  inactive  screens.Bar.app is not "tv" (or mode is not BAR at all): every
            other BAR app's fixed 140 px reserved strip, unaffected.
  SHOWN     the tv app is BAR's window; the full strip is drawn and reserves
            0 px (exclusive zone 0: the leanback window already gets the
            full 1080 - main.py's geometry() never reflows it again just
            because the strip's own drawn height changes). Entered on
            activation and by touch() while HIDDEN.
  HIDDEN    `timeout_s` after the last touch with nothing new: the layer
            surface itself shrinks to `handle_h` (screens.HANDLE_H) - a
            thin always-touchable grab handle, still zone 0 - so the window
            gets the same full 1080 it already had. touch() (a tap on the
            handle, or the down-touch of a swipe up - both are the same
            FINGER_DOWN this class ever sees, since the handle is the ONLY
            thing on screen to receive it) returns to SHOWN and rearms.

Not the OVERLAY layer, and not wl_layer.set_layer() at all: the TV window
is a normal tiled BAR window, never fullscreen, so sway already stacks the
panel's own TOP layer surface above it (sway/tree/root.c) - unlike CC5's
hidden_overlay.py, which needs OVERLAY specifically to get above a
FULLSCREEN emulator window. Only the exclusive zone/size need to change.

wants()/geometry() are pure and unit-tested without a real host; on_mode()/
touch() need only host.call_later/host.cancel (main.App's timer heap) and
one attribute read (host.ui.cc.bar.app) - a fake with those three is enough
for every other test.
"""
import logging

import screens
import sway_ipc

log = logging.getLogger("rp5deck.bar_autohide")

SHOWN, HIDDEN = "shown", "hidden"
DEFAULT_TIMEOUT_S = 4.0
# screens.HANDLE_H only exists once patches/AH-screens.patch is applied (this
# module itself needs no such patch - see AH-NOTES.md); the fallback keeps
# this importable either way, and is kept in exact step with the patch.
HANDLE_H = getattr(screens, "HANDLE_H", 24)
TV_APP = "tv"


class BarAutoHide:
    def __init__(self, host, timeout_s=DEFAULT_TIMEOUT_S, handle_h=HANDLE_H):
        self.host = host
        self.timeout_s = timeout_s
        self.handle_h = handle_h
        self.active = False      # BAR mode and screens.Bar.app == "tv" right now
        self.state = SHOWN       # SHOWN | HIDDEN - only meaningful while active
        self.timer = None

    # -- pure ---------------------------------------------------------------
    def wants(self, app):
        """Whether `app` (screens.Bar.app: None | "web" | "yt" | "tv") is
        this feature's own BAR app. Every other kind is main.App.geometry()'s
        existing fixed-140-reserved path, untouched."""
        return app == TV_APP

    def geometry(self, app, default_h, default_zone):
        """main.App.geometry(BAR)'s own (size_h, zone) for `app` - the
        default (BAR_H, BAR_H) pass through unchanged for every app this
        does not want. For "tv": (BAR_H or handle_h, 0) depending on state -
        zone is 0 in BOTH states, so the window is never reflowed by a
        show/hide, only by entering/leaving the tv app entirely."""
        if not self.wants(app):
            return default_h, default_zone
        size_h = self.handle_h if self.state == HIDDEN else default_h
        return size_h, 0

    # -- host hooks (main.App.on_mode / a touch reaching the strip) ---------
    def on_mode(self, old, new, reason=None):
        """Call after web.on_mode()/ytapp.on_mode() (they are what actually
        flips screens.Bar.app), so `app` below already reflects what BAR
        entering/staying means this time - see main.py's on_mode() ordering
        note (patches/AH-NOTES.md)."""
        app = self._bar_app()
        was_active = self.active
        self.active = (new == sway_ipc.BAR) and self.wants(app)
        if self.active:
            if not was_active:
                self.state = SHOWN
            self._arm()
        elif was_active:
            self._cancel()
            self.state = SHOWN          # reset for the next time it opens
        if self.active != was_active:
            self._apply()

    def touch(self):
        """Any touch that reached main.App while BAR is showing the tv app -
        there is nothing else on the surface to have received it, hidden or
        shown (see the module docstring). Brings the strip back if it was
        hidden and always rearms the timeout."""
        if not self.active:
            return
        was_hidden = self.state == HIDDEN
        self.state = SHOWN
        self._arm()
        if was_hidden:
            self._apply()

    def stop(self):
        """main.App shutdown, or a defensive call from tests: the timer
        must never fire again once nothing owns this object any more."""
        self._cancel()
        self.active = False

    # -- internals ------------------------------------------------------------
    def _bar_app(self):
        ui = getattr(self.host, "ui", None)
        cc = getattr(ui, "cc", None)
        bar = getattr(cc, "bar", None)
        return getattr(bar, "app", None)

    def _arm(self):
        self._cancel()
        if self.timeout_s and self.timeout_s > 0:
            self.timer = self.host.call_later(self.timeout_s, self._on_timeout)

    def _cancel(self):
        if self.timer is not None:
            self.host.cancel(self.timer)
            self.timer = None

    def _on_timeout(self):
        self.timer = None
        if not self.active or self.state == HIDDEN:
            return
        self.state = HIDDEN
        self._apply()

    def _apply(self):
        apply_geom = getattr(self.host, "_apply_bar_geometry", None)
        if apply_geom is not None:
            apply_geom()
