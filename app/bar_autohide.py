"""bar_autohide: hides the BAR strip on its own for the YouTube TV app only.

The YouTube TV app (web_tiles.YtAppSession, screens.Bar's "tv" kind) reserved the same 140 px
BAR_H strip as every other BAR app, which left YouTube's 16:9 leanback UI with black bars at the
sides instead of filling the 1920x1080 panel. The fix is only for this app, not a new sway_ipc
mode. compute_mode() still reports plain BAR, and only what main.App's geometry() gives the
layer surface changes, only while screens.Bar.app == "tv". Browser and Discord keep the fixed
140 px strip.

States (main.App drives them through on_mode()/touch(), this class is bookkeeping plus one
timer with the same call_later/cancel idiom as the pull-down's auto-close):

  inactive  screens.Bar.app isnt "tv" (or the mode isnt BAR), the other BAR apps' fixed
            140 px strip, untouched.
  SHOWN     the tv app is BAR's window, the full strip is drawn and reserves 0 px (exclusive
            zone 0, so the leanback window already has the full 1080 and geometry() never
            reflows it when the strip's height changes). Entered on activation and by
            touch() while HIDDEN.
  HIDDEN    `timeout_s` after the last touch, the layer surface shrinks to `handle_h`
            (screens.HANDLE_H), a thin grab handle you can always touch, still zone 0.
            touch() (a tap on the handle or the start of a swipe up, both the same
            FINGER_DOWN since the handle is the only thing there to get it) goes back to
            SHOWN and rearms.

It doesnt need the OVERLAY layer or wl_layer.set_layer(). The TV window is a normal tiled BAR
window, never fullscreen, so sway already stacks the panel's TOP layer surface above it, unlike
hidden_overlay.py which needs OVERLAY to get above a fullscreen emulator window. Only the
exclusive zone and size change.

wants()/geometry() are pure and tested without a host. on_mode()/touch() only need
host.call_later/host.cancel and one read of host.ui.cc.bar.app, so a fake with those three
covers the rest.
"""
import logging

import screens
import sway_ipc

log = logging.getLogger("rp5deck.bar_autohide")

SHOWN, HIDDEN = "shown", "hidden"
DEFAULT_TIMEOUT_S = 4.0
# the fallback for when screens has no HANDLE_H, kept the same as its value there
HANDLE_H = getattr(screens, "HANDLE_H", 24)
TV_APP = "tv"


class BarAutoHide:
    def __init__(self, host, timeout_s=DEFAULT_TIMEOUT_S, handle_h=HANDLE_H):
        self.host = host
        self.timeout_s = timeout_s
        self.handle_h = handle_h
        self.active = False      # BAR mode and screens.Bar.app == "tv" right now
        self.state = SHOWN  # SHOWN | HIDDEN, only matters while active
        self.timer = None

    # -- pure ---------------------------------------------------------------
    def wants(self, app):
        """Whether `app` (screens.Bar.app: None | "web" | "yt" | "tv") is this feature's BAR app. Every
        other kind takes main.App.geometry()'s fixed 140 px path.
        """
        return app == TV_APP

    def geometry(self, app, default_h, default_zone):
        """main.App.geometry(BAR)'s (size_h, zone) for `app`. The default (BAR_H, BAR_H) passes through
        for every app this doesnt want. For "tv" it's (BAR_H or handle_h, 0) by state, zone 0 either
        way, so the window only reflows on entering or leaving the tv app.
        """
        if not self.wants(app):
            return default_h, default_zone
        size_h = self.handle_h if self.state == HIDDEN else default_h
        return size_h, 0

    # -- host hooks (main.App.on_mode / a touch reaching the strip) ---------
    def on_mode(self, old, new, reason=None):
        """Call after web.on_mode()/ytapp.on_mode(), since they're what flips screens.Bar.app, so `app`
        already says what entering or staying in BAR means this time.
        """
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
        """Any touch that reached main.App while BAR shows the tv app, since there's nothing else on the
        surface to get it, hidden or shown. Brings the strip back if hidden and always rearms the
        timeout.
        """
        if not self.active:
            return
        was_hidden = self.state == HIDDEN
        self.state = SHOWN
        self._arm()
        if was_hidden:
            self._apply()

    def stop(self):
        """main.App shutdown, or a safety call from tests, so the timer never fires once nothing owns
        this.
        """
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
