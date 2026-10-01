"""hidden_overlay: the Command Center over an emulator's second screen, or over ES undocked.

The Dual Screen add-on's shell covers the RP5's volume keys, and when a DS, 3DS or Wii U game
puts its second window on the panel's screen the panel goes HIDDEN so the game gets every
touch. You still need to get at volume, so this adds a fourth mode, OVERLAY, only entered
from HIDDEN:

  HIDDEN  --Back (F1) or a tap on the corner handle-->  OVERLAY
  OVERLAY --Close / Back again / swipe up / auto-close-->  HIDDEN

OVERLAY maps the panel's own layer surface full panel on the wlr-layer-shell OVERLAY layer
with keyboard interactivity NONE:

OVERLAY, not TOP: sway stacks shell_top below the fullscreen tree and shell_overlay above
it (sway/tree/root.c:43-56), so a fullscreen second window would hide a TOP surface. The
layer is switched live with wl_layer.set_layer(). Every other mode stays on TOP.

No focus steal: sway only gives keyboard focus to a layer surface whose interactivity
isnt NONE, and a touch never focuses a layer surface (seatop_default.c), so the game
keeps the controller and keeps running.

The emulator's bottom screen is covered while it's up. Closing unmaps it the same way
HIDDEN does, so the game gets its touch area back.

It shows the volume strip (mute, master slider, %) and the tiles that make sense over a game
plus a big Close. Every change goes through main.App's normal audio path.

Why in this process and not cc_overlay.py: cc_overlay is a second process on the game
screen. Showing it here would mean moving its surface over and back on every open, guessing
from state.json (up to 0.1 s late) whether the panel is HIDDEN for a game, and racing the
panel on the same key press. The panel already knows why it's HIDDEN, already has a surface
on that screen, and closing is just its normal HIDDEN transition. So the panel takes Back
while HIDDEN for an emulator window and says so in state.json ("cc5": {"takes_summon":
true}), and cc_overlay stands aside then. With command_center.overlay_on_hidden off the panel
leaves the press alone and cc_overlay shows the Command Center on the game screen like before.

HIDDEN has three causes: an emulator window, undocked, or the panel's output missing. The
overlay opens for the first two. The watcher only reports mode changes so its reason can be
stale (undocking mid game stays HIDDEN), so a summon rereads the tree once on the io worker
before opening.

The trigger
-----------
1. Back (KEY_F1 on "InputPlumber Keyboard", read without grabbing by summon.py). The same
   press still reaches the emulator, but none of melonDS, Azahar, Cemu or DSperate on
   ROCKNIX do anything with a plain F1 (see F1_NOTES, DraStic not checked).
2. The corner handle (command_center.corner_handle, off by default): a HANDLE_PX square layer
   surface in one corner, mapped only while HIDDEN for an emulator window. Tap it to open
   the overlay. It's a second SDL window with a custom role so SDL still decodes its touches,
   on the OVERLAY layer, exclusive zone 0 and keyboard NONE like the panel. It costs
   96 x 96 of the panel's 2,073,600 pixels (0.44 %). A 4:3 DS/3DS screen scaled to 1080 high
   leaves 240 px black bars each side, so with the aspect ratio kept the handle sits in the
   bar and costs nothing. A 16:9 Wii U GamePad view loses that corner. When the volume
   changes (FN + D-pad still works through the shell) the handle shows the new level for a
   moment instead of its icon.

There's no volume popup over HIDDEN without the handle. It would need a surface over the
game's touch screen on every key press, eating the taps under it. With the handle on, the
handle is that popup.
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

OVERLAY_TIMEOUT_S = 20  # auto-close when command_center.auto_close_timeout_s is 0 (never)
HANDLE_PX = 96  # about 6 mm at the panel's ~15.8 px/mm
HANDLE_RECHECK_S = 5.0      # re-read the tree while the handle is up (undock stays HIDDEN)
FLASH_S = 1.5               # the handle shows a changed level this long (main.OSD_SECONDS)

# the home tiles shown in OVERLAY: screens.OVERLAY_TILES plus Close

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
    """command_center.overlay_on_hidden, True when missing or invalid."""
    v = config.get_value(cfg, KEY_OVERLAY_ON_HIDDEN)
    return v if isinstance(v, bool) else True


def setting_corner_handle(cfg):
    """command_center.corner_handle, "off" when missing or invalid."""
    v = config.get_value(cfg, KEY_CORNER_HANDLE)
    return v if v in CORNERS else "off"


def hidden_cause(reason):
    """Why sway_ipc.compute_mode() said HIDDEN, from its reason string: "window" (an emulator's
    second screen owns the panel's output), "undocked", "absent" (the panel's output is gone), or
    None. test_cc5_overlay builds the reasons from compute_mode() itself, so changed wording there
    turns a test red instead of quietly turning the overlay off.
    """
    r = reason or ""
    if r.startswith("foreign window on "):
        return "window"
    if r.startswith("undocked"):
        return "undocked"
    if r.endswith(" absent"):
        return "absent"
    return None


def corner_anchor(corner, wl):
    """layer shell anchor bits for a corner name (None for "off")"""
    return {
        "top-left": wl.ANCHOR_TOP | wl.ANCHOR_LEFT,
        "top-right": wl.ANCHOR_TOP | wl.ANCHOR_RIGHT,
        "bottom-left": wl.ANCHOR_BOTTOM | wl.ANCHOR_LEFT,
        "bottom-right": wl.ANCHOR_BOTTOM | wl.ANCHOR_RIGHT,
    }.get(corner)


def level_of(master):
    """(volume rounded to 0.001, muted) from an audio.get_master() dict, or None when it isnt a
    real reading.
    """
    if not master or master.get("state") != "ok" or master.get("volume") is None:
        return None
    return round(master["volume"], 3), master.get("muted")


def level_changed(prev, new):
    a, b = level_of(prev), level_of(new)
    return a is not None and b is not None and a != b


def _read_overlay_peer(run_dir):
    """the game-screen overlay's state (None if it isnt running)"""
    return screen_swap.read_peer_state(os.path.join(run_dir, "overlay", "state.json"))


def paint_handle(g, w, h, level=None):
    """Draws the corner handle into a Canvas-like g of w x h: a dark tile with a speaker, or for
    FLASH_S after a volume change the new level as a percentage (level = (volume, muted)).
    """
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
# The controller (UI thread, no SDL needed, tests drive the real one)
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
          render_handle()              once per loop
          state(), shutdown()

    query() -> (mode, reason) rereads sway's tree read only, tests pass a fake.
    handle_factory(app, output) makes the handle, None means SdlHandle when SDL is up, else no
    handle.
    """

    def __init__(self, app, query=None, handle_factory=None, peer_state=None):
        self.app = app
        self.query = query or self._query_sway
        self.handle_factory = handle_factory
        self.peer_state = peer_state or (lambda: _read_overlay_peer(self.app.run_dir))
        self.handle = None
        self.handle_error = None
        self.handle_blocked = None  # why a recheck said it's not a game window
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
        self.events = []  # [what, reason, t], last 12, for state.json

    # -- settings ----------------------------------------------------------
    def enabled(self):
        return setting_overlay_on_hidden(self.app.cfg)

    def corner(self):
        return setting_corner_handle(self.app.cfg)

    def allowed(self, cause):
        """May the overlay open over a panel HIDDEN for `cause`? Over an emulator's
        window, and undocked, where the panel's screen is the only one."""
        return cause in ("window", "undocked")

    def handle_corner(self):
        """Where the floating button goes: the corner picked in Settings, "off" by default so
        theres no icon over the game (the Back button still opens it)."""
        return self.corner()

    def over_steam(self):
        running = getattr(getattr(self.app, "companion", None), "running", None)
        return getattr(running, "system", "") == "steam"

    def _offer_settings_over_steam(self):
        """Steam is a place to work from, not a game to protect, and the Steam page of Settings has to be
        reachable while it is open."""
        home = self.app.ui.cc.home
        base = tuple(t for t in home.overlay_tiles if t != "home.settings")
        home.overlay_tiles = base + ("home.settings",) if self.over_steam() else base

    def over_game(self):
        """Show the over-a-game tiles (Mixer, HUD, Hotkeys, Performance, Notes, Quit game), or
        the full Command Center when its opened over ES itself (undocked)."""
        if hidden_cause(self.hidden_reason) == "window":
            return True
        return getattr(getattr(self.app, "companion", None), "running", None) is not None

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
        level = getattr(self.app, "layer_level", None)  # the game-screen overlay's hook
        return level() if level is not None else getattr(wl, "LAYER_TOP", 2)

    def apply_layer(self, mode):
        """Puts the panel's surface on the layer `mode` needs before it's (re)mapped. The request lands
        with the show()/set_geometry() commit that follows.
        """
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
            self.hidden_reason = reason  # the game's window is still there, stay up
            return True
        # The window went away (FULL/BAR) or the screen did (undocked). The overlay ends and on_mode
        # carries on to that mode.
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
        """The summon button. Handles it (True) in OVERLAY by closing and in HIDDEN by opening or
        refusing with a logged reason. False otherwise so main.App keeps its FULL/BAR behaviour.
        """
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
        """Rereads sway's tree once (the watcher's reason may be stale), then opens if the panel is
        still HIDDEN for a reason the overlay allows.
        """
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
            # couldnt ask sway, fall back to the watcher's own reason
            log.warning("CC5: tree re-read failed (%s); using the watcher's reason", why)
            mode, why = HIDDEN, app.mode_reason
        if mode != HIDDEN or not self.allowed(hidden_cause(why)):
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
        self._offer_settings_over_steam()
        app.ui.set_overlay(self.over_game())
        app.pull.open(reason)
        app._request_master()           # show the current level, not the last one seen
        self._arm_timeout()
        self.opens += 1
        log.info("CC5 overlay open (%s)", reason)
        self._note("open", reason)
        return True

    # -- closing -----------------------------------------------------------------
    def _close_parts(self, reason):
        """everything but the mode change: timer, fingers, pull-down, tiles"""
        app = self.app
        self.leaving = True
        try:
            self._cancel(self.timer)
            self.timer = None
            app.router.cancel_all()  # a finger on the slider gets restored, never committed
            if app.pull.is_open():
                app.pull._goto(PULL.COMPANION, reason)
            app.ui.set_overlay(False)
        finally:
            self.leaving = False

    def dismiss(self, reason):
        """Closes the overlay by unmapping (back to HIDDEN) so the emulator gets its touch area back."""
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
        """The overlay's own countdown, only when the Command Center's auto-close is 0 (never).
        Otherwise the pull-down's countdown (main.App._cc_tick) closes it through after_pull. Over a
        game a forgotten overlay would keep the touch screen from it.
        """
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
        if app.ui.bar.slider.dragging or app.ui.mixer.dragging() or getattr(app, "charge_warning_on", False):
            self._arm_timeout()         # a finger on a slider, or the charger warning is up
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
        return (self.handle_corner() != "off" and app.mode == HIDDEN
                and self.allowed(hidden_cause(app.mode_reason)) and self.handle_blocked is None)

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
            h.show(self.handle_corner(), self.app.output)
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
            return  # unknown, keep what's there
        if mode != HIDDEN or not self.allowed(hidden_cause(why)):
            self.handle_blocked = "%s: %s" % (mode, why)
            log.info("CC5: corner handle hidden (%s)", self.handle_blocked)
            self._note("handle_hidden", self.handle_blocked)
            self._apply_handle()

    def on_master(self, prev, m):
        """A volume change while the handle is up (FN + D-pad, the shell only covers the side keys)
        makes the handle show the level.
        """
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
# The corner handle's surface (SDL3 + wl_layer on the device, a fake in tests)
# ---------------------------------------------------------------------------
class SdlHandle:
    """A second SDL window with a custom role wl_surface, given the layer role by
    wl_layer.LayerSurface: OVERLAY layer, corner anchor, HANDLE_PX square, exclusive zone 0,
    keyboard NONE. Made on first use, so with the handle off there's never a second window. Its
    touches come through SDL tagged with window_id, and main.App passes them to
    OverlayController.handle_event().
    """

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
        # never "Bottom", "Secondary" or "Screen 2" in a title, ROCKNIX's rules match on those
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
            self.layer.hidden = True  # nothing presented yet so it's not mapped (see wl_layer.hide)
        except Exception:
            self.destroy()
            raise
        log.info("CC5: corner handle surface created on %s (window %d)", output, self.window_id)

    def show(self, corner, output):
        wl = self.wl
        if output != self.output:
            self.layer.rebind(output)  # the panel moved screens, the handle follows
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
        """True on a finished tap (down then up on the handle)."""
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
        # the layer role goes before SDL destroys the wl_surface (same order as main.py)
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
