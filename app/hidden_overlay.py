"""hidden_overlay - CC5: the Command Center over an emulator's second screen.

The owner (24 Sep, 01:48): "I need to be able to open the command center
still to adjust volume since the RP5 DS blocks the physical buttons." The
Retroid Dual Screen Add-on's shell covers the RP5's volume keys, and when a
DS / 3DS / Wii U game puts its second window on the panel's screen the main
panel goes HIDDEN (unmapped) so the game gets every touch (DESIGN.md "Three
display modes"). This module adds a fourth mode, OVERLAY, entered only from
HIDDEN:

  HIDDEN  --summon (Back / F1) or a tap on the corner handle-->  OVERLAY
  OVERLAY --Close / Back again / swipe up / auto-close timeout-->  HIDDEN

OVERLAY maps the panel's OWN layer surface (the one HIDDEN unmapped) full
panel over the emulator's window, on the wlr-layer-shell OVERLAY layer, with
keyboard interactivity NONE:

  * OVERLAY layer, not TOP: sway 1.11 stacks shell_top BELOW the fullscreen
    tree and shell_overlay ABOVE it (sway/tree/root.c:43-56); an emulator
    that fullscreens its second window would hide a TOP surface entirely.
    The layer is switched at runtime with zwlr_layer_surface_v1.set_layer
    (since v2; sway 1.11 advertises v4, server.c:73, and reparents the scene
    node on commit, desktop/layer_shell.c:273-277) - wl_layer.set_layer().
    Every other mode stays on the layer it had (TOP for the panel).
  * No focus steal: sway only moves keyboard focus to a layer surface whose
    keyboard_interactive is not NONE (seatop_default.c:383-386), and a
    touch-down never focuses a layer surface at all (seatop_default.c:659-
    684) - the same rule E1 verified for the panel, on either layer. The
    game keeps keyboard/controller focus and keeps running.
  * While it is up the emulator's bottom screen is covered (accepted by the
    owner); dismissing unmaps it through the same HIDDEN path DS3 verified,
    so the emulator gets its touch area back.

What it shows: the panel's own Command Center, "volume first" (the strip:
mute, master slider, %), restricted to tiles that make sense over a game
(Mixer - per-app volume, HUD) plus a big Close tile. The slider is the
FX-A slider (no jump on touch-down, a swipe cancels and restores, commit on
release only) and every change goes through main.App's existing audio path.

Why in-process, and not SW1's cc_overlay.py (Main asked for ONE overlay
mechanism): cc_overlay is a second process whose surface lives on the ES /
game screen. Showing it over the emulator's window on the OTHER screen
would mean rebinding its surface there and back on every open, deciding
from the panel's state.json (written up to 0.1 s late) whether the panel is
HIDDEN because of a game, and racing the panel on the same key press. The
panel already owns that decision (its ModeWatcher), already has an idle
surface on exactly that screen, and "dismiss" is literally its verified
HIDDEN transition. So: the panel handles Back while it is HIDDEN because of
an emulator window (and publishes "cc5": {"takes_summon": true} in its
state.json); cc_overlay defers in that case (patches/CC5-cc_overlay.patch)
and keeps everything else it does (its game-screen pull tab, Back while the
panel is in BAR or not running). With command_center.overlay_on_hidden off,
the panel does not take the press and SW1's behaviour (Command Center on
the game screen) comes back unchanged.

Only an EMULATOR WINDOW's HIDDEN counts. HIDDEN also means "undocked" and
"the panel's output is absent"; there the overlay is refused. The watcher
only reports mode CHANGES, so its reason can be stale (undocking while a
game runs stays HIDDEN): a summon re-reads the tree once (read-only,
sway_ipc.query_mode, on the io worker) before opening.

The trigger
-----------
1. Back (KEY_F1 on "InputPlumber Keyboard", command_center.hardware_button
   = btn_back_f1; summon.py reads it non-grabbing), now also in HIDDEN.
   The same press still reaches the focused emulator window - see
   F1_NOTES below: on ROCKNIX's builds none of melonDS / Azahar / Cemu /
   DSperate acts on a plain F1 (DraStic unverified).
2. Optional corner handle (command_center.corner_handle, default off): a
   HANDLE_PX x HANDLE_PX layer surface in one corner of the panel's screen,
   mapped only while the panel is HIDDEN because of an emulator window. A
   tap on it opens the overlay. It is a SECOND wl_surface (a second SDL
   window with a custom role, so SDL still decodes its touches and tags
   them with its windowID) given its own zwlr_layer_surface_v1 via
   wl_layer.LayerSurface: layer OVERLAY (it must sit above a fullscreen
   emulator window too), anchored to the two edges of its corner, size
   HANDLE_PX x HANDLE_PX, exclusive zone 0 (it never pushes the emulator's
   window aside), keyboard interactivity NONE (no focus, like the panel).
   Touch-area cost: its pixels stop reaching the game - 96 x 96 = 9,216 of
   the panel's 2,073,600 (0.44 %). A 4:3 DS / 3DS bottom screen scaled to
   1080 px high is 1440 px wide, leaving 240 px pillarbox bars each side,
   so when the emulator keeps the aspect ratio a corner handle sits entirely
   in black bar and costs the game nothing; a 16:9 Wii U GamePad view loses
   that corner. While the handle is mapped and the volume changes (the
   add-on shell leaves FN + D-pad volume usable), the handle briefly shows
   the new level instead of its icon - the cheap version of the HIDDEN
   volume OSD (no extra surface over the game).

The volume OSD over HIDDEN without the handle is not done: it would need a
surface over the game's touch screen for 1.5 s on every key press (stealing
the taps under it), or a second, OSD-only widget tree for the panel's
surface. With the handle on, the handle is that OSD.
"""
import logging
import os
import time

import config
import screen_swap
import summon
import sway_ipc
from sway_ipc import BAR, FULL, HIDDEN   # noqa: F401 - BAR/FULL re-exported for callers

log = logging.getLogger("rp5deck.cc5")

OVERLAY = "OVERLAY"

KEY_OVERLAY_ON_HIDDEN = ("command_center", "overlay_on_hidden")
KEY_CORNER_HANDLE = ("command_center", "corner_handle")
CORNERS = ("off", "top-left", "top-right", "bottom-left", "bottom-right")

OVERLAY_TIMEOUT_S = 20      # auto-close when command_center.auto_close_timeout_s is 0 ("never")
HANDLE_PX = 96              # ~6 mm at the panel's ~15.8 px/mm
HANDLE_RECHECK_S = 5.0      # re-read the tree while the handle is up (undock stays HIDDEN)
FLASH_S = 1.5               # the handle shows a changed level this long (main.OSD_SECONDS)

# The home tiles shown in OVERLAY: screens.OVERLAY_TILES (Mixer, HUD) + Close.

PULL = summon.PullDownStateMachine

F1_NOTES = """Which emulators react to a plain F1 (Back reaches the focused game
window too; nothing in ROCKNIX's sway config or input_sense consumes F1).
Read from upstream / ROCKNIX source on 24 Sep 2026 (ROCKNIX branch `next`,
projects/ROCKNIX/packages/emulators/standalone/...):

  melonDS   upstream: plain F1 = LOAD STATE slot 1 (src/frontend/qt_sdl/
            Window.cpp:337, actLoadState[i]->setShortcut(Qt::Key_F1 + i - 1);
            Shift+F1 saves). ROCKNIX's melonds-sa/patches/003-hotkeys.patch
            comments those shortcuts out (patch lines 252-253) and gates its
            own hotkeys on hotkeyDown(HK_HotkeyEnable); its InputPlumber
            melonDS.ini binds HKKey_SwapScreens=16777264 (Qt::Key_F1) but
            HotkeyEnable only to a joystick button (HKJoy_HotkeyEnable=10).
            => F1 alone does nothing; F1 WHILE holding that hotkey-enable
            button swaps the DS screens. A stock melonDS build would load
            slot 1 (an empty slot is only an OSD message).
  Azahar    no F1 in default_hotkeys (src/citra_qt/configuration/config.cpp:
            F2-F11 bound, F1 absent); ROCKNIX's qt-config.ini adds none.
  Cemu      no F1 anywhere (wxWidgets GUI; every hotkey defaults to none,
            wxCemuConfig.h); ROCKNIX's settings.xml has no <HotKeys>.
  DSperate  (ROCKNIX dsperate-sa, github.com/beebono/DSperate 2.0.1) default
            [hotkeys] F2-F10 bound, F1 absent (config/default.ini:317-329;
            "pause.alt = F1" at :308 is a syntax example, not a default).
  DraStic   closed source: UNVERIFIED. The shipped drastic.cfg binds save /
            load state to 's' / 'l' and nothing that decodes to F1.
The hotkey-enable+Back swap in melonDS and DraStic need one device check."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def setting_overlay_on_hidden(cfg):
    """command_center.overlay_on_hidden; True when absent or invalid (the
    config.py schema entry arrives with patches/CC5-config.patch)."""
    v = config.get_value(cfg, KEY_OVERLAY_ON_HIDDEN)
    return v if isinstance(v, bool) else True


def setting_corner_handle(cfg):
    """command_center.corner_handle; "off" when absent or invalid."""
    v = config.get_value(cfg, KEY_CORNER_HANDLE)
    return v if v in CORNERS else "off"


def hidden_cause(reason):
    """Why sway_ipc.compute_mode() said HIDDEN, from its reason string:
    "window" (a foreign window owns the panel's output - an emulator's
    second screen), "undocked", "absent" (the panel's own output is gone),
    or None (not a HIDDEN reason this knows). tests/test_cc5_overlay.py
    derives the reasons from compute_mode() itself, so a changed wording
    there turns a test red instead of silently disabling the overlay."""
    r = reason or ""
    if r.startswith("foreign window on "):
        return "window"
    if r.startswith("undocked"):
        return "undocked"
    if r.endswith(" absent"):
        return "absent"
    return None


def corner_anchor(corner, wl):
    """Layer-shell anchor bits for a corner name (None for "off")."""
    return {
        "top-left": wl.ANCHOR_TOP | wl.ANCHOR_LEFT,
        "top-right": wl.ANCHOR_TOP | wl.ANCHOR_RIGHT,
        "bottom-left": wl.ANCHOR_BOTTOM | wl.ANCHOR_LEFT,
        "bottom-right": wl.ANCHOR_BOTTOM | wl.ANCHOR_RIGHT,
    }.get(corner)


def level_of(master):
    """(volume rounded to 0.001, muted) from an audio.get_master() dict, or
    None when it is not a real reading."""
    if not master or master.get("state") != "ok" or master.get("volume") is None:
        return None
    return round(master["volume"], 3), master.get("muted")


def level_changed(prev, new):
    a, b = level_of(prev), level_of(new)
    return a is not None and b is not None and a != b


def _read_overlay_peer(run_dir):
    """SW1's game-screen overlay state (None if it is not running)."""
    return screen_swap.read_peer_state(os.path.join(run_dir, "overlay", "state.json"))


def paint_handle(g, w, h, level=None):
    """Draw the corner handle into a Canvas-like `g` of w x h: a dark tile
    with a speaker (the Command Center's volume), or - for FLASH_S after a
    volume change - the new level as a percentage (level = (volume, muted))."""
    import screens
    from ui import THEME
    g.fill_rect((0, 0, w, h), THEME["bar"])
    g.stroke_round_rect((3, 3, w - 6, h - 6), 14, THEME["accent"], 4)
    if level is None:
        s = w * 0.5
        screens.icon_speaker(g, ((w - s) / 2.0, (h - s) / 2.0, s, s), THEME["text"])
        return
    vol, muted = level
    if muted:
        s = w * 0.5
        screens.icon_speaker(g, ((w - s) / 2.0, (h - s) / 2.0, s, s), THEME["danger"], True)
        return
    g.text("%d" % int(round(max(0.0, min(1.5, vol)) * 100)), (0, 0, w, h), int(h * 0.42),
           THEME["text"], True, "center")


# ---------------------------------------------------------------------------
# The controller (UI thread; no SDL needed - tests drive the real one)
# ---------------------------------------------------------------------------
class OverlayController:
    """Owns OVERLAY for one main.App. main.App calls:

      intercept_mode(mode, reason) first thing in on_mode (True = swallow)
      apply_layer(mode)            before (re)mapping the surface
      after_mode(old, new, reason) at the end of on_mode
      after_pull(old, new, reason) at the end of on_pull
      on_summon(ev)                from on_summon_button (True = handled)
      activity()                   on every touch
      on_master(prev, m)           on every master reading
      config_changed()             a command_center.* setting changed
      handle_event(etype, window_id, finger_id) for SDL input (True = ours)
      render_handle()              once per loop iteration
      state(), shutdown()

    query() -> (mode, reason) re-reads sway's tree (read-only); tests pass
    a fake. handle_factory(app, output) -> a handle object (see SdlHandle);
    None means "SdlHandle when SDL is up, else no handle"."""

    def __init__(self, app, query=None, handle_factory=None, peer_state=None):
        self.app = app
        self.query = query or self._query_sway
        self.handle_factory = handle_factory
        self.peer_state = peer_state or (lambda: _read_overlay_peer(self.app.run_dir))
        self.handle = None
        self.handle_error = None
        self.handle_blocked = None      # a recheck said "not a game window": why
        self.hidden_reason = None       # the watcher's HIDDEN reason the overlay sits on
        self.leaving = False
        self.verifying = False
        self.timer = None
        self.recheck_timer = None
        self.recheck_inflight = False
        self.flash_timer = None
        self.opens = 0
        self.dismissals = 0
        self.ended = 0
        self.refusals = 0
        self.last_refusal = None
        self.events = []                # [what, reason, t] (last 12, for state.json)

    # -- settings ----------------------------------------------------------
    def enabled(self):
        return setting_overlay_on_hidden(self.app.cfg)

    def corner(self):
        return setting_corner_handle(self.app.cfg)

    def takes_summon(self):
        """The panel answers Back while HIDDEN because of an emulator window
        (cc_overlay.py reads this from state.json and defers)."""
        return bool(self.enabled() and self.app.summon_reader is not None)

    def is_open(self):
        return self.app.mode == OVERLAY

    def _note(self, what, reason):
        self.events = (self.events + [[what, reason, round(time.time(), 3)]])[-12:]
        self.app.state_dirty = True

    def _refuse(self, why):
        self.refusals += 1
        self.last_refusal = why
        log.info("CC5 overlay refused: %s", why)
        self._note("refused", why)

    # -- the mode --------------------------------------------------------------
    def layer_for(self, mode):
        wl = self.app.wl
        if mode == OVERLAY:
            return getattr(wl, "LAYER_OVERLAY", 3)
        level = getattr(self.app, "layer_level", None)      # SW1's hook, once merged
        return level() if level is not None else getattr(wl, "LAYER_TOP", 2)

    def apply_layer(self, mode):
        """Put the panel's surface on the layer `mode` needs, before it is
        (re)mapped: the request is double-buffered and lands with the
        show()/set_geometry() commit that follows."""
        set_layer = getattr(self.app.layer, "set_layer", None)
        if set_layer is not None:
            set_layer(self.layer_for(mode))

    def intercept_mode(self, mode, reason):
        app = self.app
        if self.leaving or app.mode != OVERLAY:
            return False
        if mode == OVERLAY:
            return True
        if mode == HIDDEN and hidden_cause(reason) == "window":
            self.hidden_reason = reason     # the game's window is still there: stay up
            return True
        # The window went away (FULL / BAR) or the screen did (undocked):
        # the overlay ends, and on_mode carries on to that mode.
        self._close_parts("overlay_end_%s" % str(mode).lower())
        self.ended += 1
        log.info("CC5 overlay ended by mode %s (%s)", mode, reason)
        self._note("ended", "%s: %s" % (mode, reason))
        return False

    def after_mode(self, old, new, reason):
        if new == HIDDEN:
            self.hidden_reason = reason
        if new != old:
            self.handle_blocked = None
        self._apply_handle()

    def after_pull(self, old, new, reason):
        if self.app.mode == OVERLAY and new == PULL.COMPANION and not self.leaving:
            self.dismiss(reason)

    # -- opening ------------------------------------------------------------------
    def on_summon(self, ev):
        """The summon button. Handles it (True) in OVERLAY (closes) and in
        HIDDEN (opens, or refuses with a logged reason); False otherwise, so
        main.App keeps its FULL / BAR behaviour unchanged."""
        app = self.app
        if app.mode not in (HIDDEN, OVERLAY):
            return False
        app.last_summon = {"binding": ev.binding, "device": ev.device_path, "t": time.time()}
        log.info("summon button %s on %s (mode %s)", ev.binding, ev.device_path, app.mode)
        if app.mode == OVERLAY:
            self.dismiss("summon_button")
            return True
        if not self.enabled():
            self._refuse("command_center.overlay_on_hidden is off")
            return True
        if screen_swap.peer_open(self.peer_state()):
            self._refuse("the game-screen Command Center is open: this press closes it")
            return True
        self.request_open("summon_button")
        return True

    def on_handle_tap(self):
        log.info("corner handle tapped")
        self.request_open("corner_handle")

    def request_open(self, reason):
        """Re-read sway's tree once (the watcher's reason may be stale), then
        open if the panel is still HIDDEN because of an emulator window."""
        app = self.app
        if app.mode != HIDDEN:
            self._refuse("mode is %s, not HIDDEN" % app.mode)
            return
        if self.verifying:
            return
        self.verifying = True
        app.io_worker.submit(self._safe_query, done=lambda res: self._verified(res, reason))

    def _safe_query(self):
        try:
            return self.query()
        except Exception as e:          # noqa: BLE001 - becomes "unknown", never a crash
            log.warning("CC5: sway query failed: %s", e)
            return (None, "query failed: %s" % e)

    def _query_sway(self):
        return sway_ipc.query_mode(internal=self.app.output, external=self.app.es_output)

    def _verified(self, res, reason):
        self.verifying = False
        app = self.app
        if app.mode != HIDDEN:
            self._refuse("mode changed to %s meanwhile" % app.mode)
            return
        try:
            mode, why = res
        except (TypeError, ValueError):
            mode, why = None, "unreadable query result"
        if mode is None:
            # sway could not be asked: fall back to the watcher's own reason
            log.warning("CC5: tree re-read failed (%s); using the watcher's reason", why)
            mode, why = HIDDEN, app.mode_reason
        if mode != HIDDEN or hidden_cause(why) != "window":
            self._refuse("not an emulator window on the panel's screen (%s: %s)" % (mode, why))
            return
        self.open(reason, why)

    def open(self, reason, why=None):
        app = self.app
        if app.mode != HIDDEN:
            return False
        self.hidden_reason = why or app.mode_reason
        app.on_mode(OVERLAY, "CC5 overlay (%s) over: %s" % (reason, self.hidden_reason))
        if app.mode != OVERLAY:
            return False
        app.ui.set_overlay(True)
        app.pull.open(reason)
        app._request_master()           # show the current level, not the last one seen
        self._arm_timeout()
        self.opens += 1
        log.info("CC5 overlay open (%s)", reason)
        self._note("open", reason)
        return True

    # -- closing -----------------------------------------------------------------
    def _close_parts(self, reason):
        """Everything but the mode change: timer, fingers, pull-down, tiles."""
        app = self.app
        self.leaving = True
        try:
            self._cancel(self.timer)
            self.timer = None
            app.router.cancel_all()     # a finger on the slider: restored, never committed
            if app.pull.is_open():
                app.pull._goto(PULL.COMPANION, reason)
            app.ui.set_overlay(False)
        finally:
            self.leaving = False

    def dismiss(self, reason):
        """Close the overlay: unmap (back to HIDDEN), so the emulator gets
        its touch area back."""
        app = self.app
        if app.mode != OVERLAY or self.leaving:
            return
        self._close_parts(reason)
        self.leaving = True
        try:
            app.on_mode(HIDDEN, self.hidden_reason or "CC5 overlay closed")
        finally:
            self.leaving = False
        self.dismissals += 1
        log.info("CC5 overlay closed (%s)", reason)
        self._note("close", reason)

    # -- auto-close -----------------------------------------------------------------
    def timeout_s(self):
        """The overlay's own countdown: only when the Command Center's
        auto-close is 0 ("never") - otherwise the pull-down's countdown runs
        (main.App._cc_tick) and closes it through after_pull. Over a game a
        forgotten overlay would keep the touch screen away from it."""
        pull = self.app.pull
        if pull is not None and pull.auto_close_timeout_s:
            return None
        return OVERLAY_TIMEOUT_S

    def _arm_timeout(self):
        self._cancel(self.timer)
        self.timer = None
        t = self.timeout_s()
        if t:
            self.timer = self.app.call_later(t, self._timeout)

    def _timeout(self):
        self.timer = None
        app = self.app
        if app.mode != OVERLAY:
            return
        if app.ui.bar.slider.dragging or app.ui.mixer.dragging():
            self._arm_timeout()         # a finger is still on a slider
            return
        self.dismiss("timeout")

    def activity(self):
        if self.app.mode == OVERLAY:
            self._arm_timeout()

    @staticmethod
    def _cancel(h):
        if h is not None:
            h[3] = True

    # -- settings ---------------------------------------------------------------
    def config_changed(self):
        self._apply_handle()
        self.app.state_dirty = True

    # -- the corner handle ----------------------------------------------------------
    def handle_wanted(self):
        app = self.app
        return (self.corner() != "off" and app.mode == HIDDEN
                and hidden_cause(app.mode_reason) == "window" and self.handle_blocked is None)

    def _make_handle(self):
        if self.handle is not None or self.handle_error is not None:
            return self.handle
        factory = self.handle_factory
        if factory is None:
            if getattr(self.app, "sdl", None) is None or getattr(self.app, "layer", None) is None:
                return None
            factory = SdlHandle
        try:
            self.handle = factory(self.app, self.app.output)
        except Exception as e:          # noqa: BLE001 - the panel runs on without a handle
            self.handle_error = str(e)
            log.exception("CC5: corner handle could not be created; running without it")
        return self.handle

    def _apply_handle(self):
        want = self.handle_wanted()
        if want:
            h = self._make_handle()
            if h is None:
                return
            h.show(self.corner(), self.app.output)
            self._arm_recheck()
        else:
            if self.handle is not None:
                self.handle.hide()
            self._cancel(self.recheck_timer)
            self.recheck_timer = None
            self._cancel(self.flash_timer)
            self.flash_timer = None

    def _arm_recheck(self):
        if self.recheck_timer is None:
            self.recheck_timer = self.app.call_later(HANDLE_RECHECK_S, self._recheck)

    def _recheck(self):
        self.recheck_timer = None
        if not self.handle_wanted() or self.recheck_inflight:
            return
        self.recheck_inflight = True
        self.app.io_worker.submit(self._safe_query, done=self._rechecked)
        self._arm_recheck()

    def _rechecked(self, res):
        self.recheck_inflight = False
        try:
            mode, why = res
        except (TypeError, ValueError):
            return
        if mode is None:
            return                      # unknown: keep what is there
        if mode != HIDDEN or hidden_cause(why) != "window":
            self.handle_blocked = "%s: %s" % (mode, why)
            log.info("CC5: corner handle hidden (%s)", self.handle_blocked)
            self._note("handle_hidden", self.handle_blocked)
            self._apply_handle()

    def on_master(self, prev, m):
        """A volume change while the handle is up (FN + D-pad: the add-on
        shell only covers the side keys): the handle shows the level."""
        h = self.handle
        if h is None or not h.visible or not level_changed(prev, m):
            return
        h.flash(level_of(m))
        self._cancel(self.flash_timer)
        self.flash_timer = self.app.call_later(FLASH_S, self._unflash)

    def _unflash(self):
        self.flash_timer = None
        if self.handle is not None:
            self.handle.flash(None)

    def handle_event(self, etype, window_id, finger_id):
        h = self.handle
        if h is None or window_id != h.window_id:
            return False
        if h.on_input(etype, finger_id):
            self.on_handle_tap()
        return True

    def render_handle(self):
        if self.handle is not None:
            try:
                self.handle.render()
            except Exception:           # noqa: BLE001
                log.exception("CC5: corner handle render failed; dropping the handle")
                self._drop_handle("render failed")

    def _drop_handle(self, why):
        h, self.handle = self.handle, None
        self.handle_error = why
        if h is not None:
            try:
                h.destroy()
            except Exception:           # noqa: BLE001
                log.exception("CC5: corner handle destroy")

    def shutdown(self):
        self._cancel(self.timer)
        self._cancel(self.recheck_timer)
        self._cancel(self.flash_timer)
        h, self.handle = self.handle, None
        if h is not None:
            h.destroy()

    # -- state.json ---------------------------------------------------------------
    def state(self):
        h = self.handle
        return {
            "overlay_on_hidden": self.enabled(),
            "takes_summon": self.takes_summon(),
            "open": self.is_open(),
            "hidden_reason": self.hidden_reason,
            "timeout_s": self.timeout_s(),
            "opens": self.opens, "dismissals": self.dismissals, "ended": self.ended,
            "refusals": self.refusals, "last_refusal": self.last_refusal,
            "events": self.events[-6:],
            "handle": {"setting": self.corner(), "wanted": self.handle_wanted(),
                       "visible": bool(h and h.visible), "error": self.handle_error,
                       "blocked": self.handle_blocked,
                       "state": h.state() if h is not None else None},
        }


# ---------------------------------------------------------------------------
# The corner handle's surface (device: SDL3 + wl_layer; tests use a fake)
# ---------------------------------------------------------------------------
class SdlHandle:
    """A second SDL window with a custom-role wl_surface, given the layer
    role by wl_layer.LayerSurface: OVERLAY layer, corner anchor, HANDLE_PX
    square, exclusive zone 0, keyboard NONE. Created on first use, so a
    panel with the handle off never makes a second window. Its touches come
    through SDL tagged with window_id; main.App hands them over via
    OverlayController.handle_event()."""

    def __init__(self, app, output):
        import ctypes
        sdl, wl = app.sdl, app.wl
        self.app, self.sdl, self.wl = app, sdl, wl
        self.win = self.ren = self.tex = self.canvas = self.layer = None
        self.visible = False
        self.level = None
        self.dirty = True
        self.presents = 0
        self.taps = 0
        self._down = set()
        self.corner = None
        get_id = sdl.lib.SDL_GetWindowID
        get_id.restype, get_id.argtypes = ctypes.c_uint32, [ctypes.c_void_p]
        props = sdl.CreateProperties()
        sdl.SetBooleanProperty(props, b"SDL.window.create.wayland.surface_role_custom", True)
        sdl.SetBooleanProperty(props, b"SDL.window.create.opengl", True)
        sdl.SetNumberProperty(props, b"SDL.window.create.width", HANDLE_PX)
        sdl.SetNumberProperty(props, b"SDL.window.create.height", HANDLE_PX)
        # Never "Bottom"/"Secondary"/"Screen 2" in a title (ROCKNIX's rules).
        sdl.SetStringProperty(props, b"SDL.window.create.title", b"rp5deck-handle")
        self.win = sdl.CreateWindowWithProperties(props)
        sdl.DestroyProperties(props)
        if not self.win:
            raise RuntimeError("handle window: %s" % sdl.error())
        try:
            self.window_id = get_id(self.win)
            self.ren = sdl.CreateRenderer(self.win, None)
            if not self.ren:
                raise RuntimeError("handle renderer: %s" % sdl.error())
            wprops = sdl.GetWindowProperties(self.win)
            surface = sdl.GetPointerProperty(wprops, b"SDL.window.wayland.surface", None)
            if not surface:
                raise RuntimeError("handle: SDL did not expose its wl_surface")
            self.tex = sdl.CreateTexture(self.ren, sdl.PIXELFORMAT_ARGB8888,
                                         sdl.TEXTUREACCESS_STREAMING, HANDLE_PX, HANDLE_PX)
            self.canvas = app.gfx.Canvas(HANDLE_PX, HANDLE_PX)
            self.output = output
            self.layer = wl.LayerSurface(
                app.layer.display, surface, output_name=output, layer=wl.LAYER_OVERLAY,
                namespace="rp5deck-handle", anchor=wl.ANCHOR_TOP | wl.ANCHOR_RIGHT,
                size=(HANDLE_PX, HANDLE_PX), exclusive_zone=0, keyboard=wl.KEYBOARD_NONE,
                log=lambda m: log.info("handle: %s", m))
            self.layer.create()
            self.layer.hidden = True    # nothing presented yet: not mapped (wl_layer.hide docs)
        except Exception:
            self.destroy()
            raise
        log.info("CC5: corner handle surface created on %s (window %d)", output, self.window_id)

    def show(self, corner, output):
        wl = self.wl
        if output != self.output:
            self.layer.rebind(output)   # SW1 moved the panel: the handle follows
            self.output = output
            self.dirty = True
        anchor = corner_anchor(corner, wl)
        if anchor is None:
            self.hide()
            return
        if not self.visible or corner != self.corner:
            self.layer.show(anchor, (HANDLE_PX, HANDLE_PX), 0)
            self.corner = corner
            self.visible = True
            self.dirty = True

    def hide(self):
        if self.visible:
            self.layer.hide()
            self.visible = False
        self._down.clear()

    def flash(self, level):
        if level != self.level:
            self.level = level
            self.dirty = True

    def on_input(self, etype, finger_id):
        """True on a completed tap (down then up on the handle)."""
        sdl = self.sdl
        if etype == sdl.EV_FINGER_DOWN or etype == sdl.EV_MOUSE_DOWN:
            self._down.add(finger_id)
        elif etype == sdl.EV_FINGER_UP or etype == sdl.EV_MOUSE_UP:
            if finger_id in self._down:
                self._down.discard(finger_id)
                self.taps += 1
                return self.visible
        elif etype == sdl.EV_FINGER_CANCELED:
            self._down.discard(finger_id)
        return False

    def render(self):
        from ctypes import c_void_p
        if not (self.visible and self.dirty and self.layer.can_present):
            return
        sdl, c = self.sdl, self.canvas
        with c.clipped((0, 0, HANDLE_PX, HANDLE_PX)):
            paint_handle(c, HANDLE_PX, HANDLE_PX, self.level)
        data, stride = c.pixels()
        sdl.UpdateTexture(self.tex, None, c_void_p(data), stride)
        sdl.RenderClear(self.ren)
        sdl.RenderTexture(self.ren, self.tex, None, None)
        sdl.RenderPresent(self.ren)
        self.layer.presented()
        self.presents += 1
        self.dirty = False

    def state(self):
        return {"window_id": getattr(self, "window_id", None), "corner": self.corner,
                "output": getattr(self, "output", None), "presents": self.presents,
                "taps": self.taps,
                "geometry": list(self.layer.geometry[:1]) + [list(self.layer.geometry[1]),
                                                             self.layer.geometry[2]]
                if self.layer else None}

    def destroy(self):
        # the layer role goes before SDL destroys the wl_surface (main.py's order)
        sdl = self.sdl
        if self.layer is not None:
            try:
                self.layer.destroy()
            except Exception:           # noqa: BLE001
                log.exception("CC5: handle layer destroy")
            self.layer = None
        if self.tex:
            sdl.DestroyTexture(self.tex)
            self.tex = None
        if self.canvas is not None:
            self.canvas.free()
            self.canvas = None
        if self.ren:
            sdl.DestroyRenderer(self.ren)
            self.ren = None
        if self.win:
            sdl.DestroyWindow(self.win)
            self.win = None
        self.visible = False
