#!/usr/bin/env python3
"""cc_overlay - SW1: the Command Center on the GAME / ES screen.

    python3 cc_overlay.py [--seconds N] [--output NAME]

The owner: "command center should be able to be used on both screens". The
main panel (main.py) lives on the screen ES is not on. This second process
puts the same Command Center on the screen ES IS on, as a pull-down over the
game:

  closed   a small pull tab in the top-right corner of the game screen
           (TAB_W x TAB_H; on 4:3 games that corner is black pillarbox). The
           rest of the game screen is not covered - the surface is only as
           big as the tab. Off with command_center.game_screen_tab; then
           nothing at all is mapped while closed.
  open     tap the tab, or drag it down: the Command Center covers the whole
           game screen - volume first, Mixer, HUD, Swap screens. Close /
           Back / swipe up / the auto-close timeout close it.

Focus: a zwlr_layer_surface_v1 on the OVERLAY layer (above fullscreen ES and
games; the main panel's TOP layer would be hidden under them) with keyboard
interactivity NONE - the same rule as the main panel (DESIGN.md E1): a tap
never takes keyboard focus, so the controls stay with the game throughout.

Which screen: screen_swap.ScreenWatcher, placement.es_output - ES as sway
shows it, so the overlay follows a swap live (wl_layer.rebind). Undocked
it stays unmapped (DSI-1), like the main panel.

The Back button (command_center.hardware_button) opens it here only when
the main panel cannot show its own Command Center - the panel's state.json
says it is not FULL (a DS game owns that screen, BAR, undocked) or the panel
is not running - so "the Back button summons on the non-ES screen by
default" and the Command Center is still one press away when that screen is
taken. Back closes it again. CC5: when the panel is HIDDEN because an
emulator's second window owns its screen, the panel opens its OWN Command
Center over that window (hidden_overlay.py) if its state.json says
"cc5": {"takes_summon": true}; this process then leaves the press alone
(panel_takes_summon). With command_center.overlay_on_hidden off, it opens
here as before.

Not here (one writer per setting, one place per app): Settings, Browser,
YouTube and Discord stay on the main panel; the overlay hides those tiles.
Its one config write is "Swap screens" (config.save_changes, read-modify-
write, so the main panel's own saves cannot undo it). It re-reads
config.json every CONFIG_POLL seconds for the tab / timeout / swipe settings.

Runs under the 094 supervisor (its own restart loop, warning-only give-up,
kill switch /storage/.disable-rp5deck-overlay). Logs to rp5deck-overlay.log,
state to $RP5DECK_RUN_DIR/overlay/state.json.
"""
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config       # noqa: E402
import main         # noqa: E402
import palettes     # noqa: E402  (Appearance)
import screen_swap  # noqa: E402
import screens      # noqa: E402
import summon       # noqa: E402
import swap_ui      # noqa: E402
import ui           # noqa: E402
from sway_ipc import FULL, HIDDEN   # noqa: E402

log = logging.getLogger("rp5deck.overlay")

TAB = "TAB"                 # overlay mode: only the pull tab is mapped
TAB_W, TAB_H = 260, 96
TAB_SWIPE = {"edge": TAB_H, "distance": 36}     # a short drag on the tab opens it
CONFIG_POLL = 2.0
OVERLAY_TILES = ("home.mixer", "home.hud", swap_ui.SWAP_TILE_NAME)
PULL = summon.PullDownStateMachine
CC5_MODES = ("HIDDEN", "OVERLAY")


def panel_takes_summon(panel):
    """CC5 (hidden_overlay.py): the main panel answers the Back button itself
    while an emulator's second window owns its screen - it opens its own
    Command Center over that window (OVERLAY) and closes it again. Its
    state.json says so with "cc5": {"takes_summon": true} (the setting
    command_center.overlay_on_hidden, and a running button reader). Then
    this process must not open a second Command Center on the same press.
    The flag is static for the press (a setting), not the panel's current
    OVERLAY/HIDDEN state, so reading state.json up to 0.1 s late cannot
    make both open or neither. An undocked panel is HIDDEN too, but this
    process never opens undocked anyway (on_summon_button returns first)."""
    if not panel or panel.get("mode") not in CC5_MODES:
        return False
    return bool((panel.get("cc5") or {}).get("takes_summon"))


class OverlayUI(screens.DeckUI):
    """DeckUI with the pull tab as its "companion" view (what shows while
    closed) and only the tiles that belong on the game screen."""

    def __init__(self, handlers, w, h, title, tab):
        screens.DeckUI.__init__(self, handlers, w, h, title, companion=tab, settings=None)
        keep = []
        for t in self.cc.home.tiles:
            if t.name in OVERLAY_TILES:
                keep.append(t)
            else:
                t.set_visible(False)
        self.cc.home.tiles = keep
        self.set_size(w, h)

    def _apply_view(self):
        closed = self.view == "companion"
        self.companion.set_visible(closed)
        self.cc.set_visible(not closed)

    @property
    def showing(self):
        if self.view == "companion":
            return "tab"
        return "bar" if self.cc.compact else "cc"


class OverlayApp(main.App):
    ROLE = "overlay"

    def __init__(self, args):
        main.App.__init__(self, args)
        base = os.environ.get("RP5DECK_RUN_DIR", "/run/rp5deck")
        self.run_dir = os.path.join(base, "overlay")
        self.panel_state_path = os.path.join(base, "state.json")
        self.docked = True
        self.peer_alive = None          # test seam for screen_swap.read_peer_state
        self.cfg_sig = self._cfg_sig()
        self.config_reloads = 0
        if not self.output_forced:
            self.output = self.cfg["es_output"]

    # -- main.App hooks --------------------------------------------------------
    def heartbeat_path(self):
        # SW2: 094-rp5deck checks this one by its OWN flat name
        # (RP5DECK_HEARTBEAT_OVERLAY / <run>/heartbeat-overlay), never under
        # self.run_dir's "overlay" subdirectory (that is state.json's own,
        # separate convention - see main.App.state()/write_state()).
        return os.path.join(os.path.dirname(self.run_dir), "heartbeat-overlay")

    def layer_level(self):
        return self.wl.LAYER_OVERLAY

    def placement_output(self, p):
        return p.es_output if p.docked else screen_swap.BUILTIN

    def initial_mode(self):
        self.docked = bool(self.placement.docked) if self.placement else True
        return self._closed_mode(), "overlay start"

    def start_watchers(self, mode0):
        self._start_screen_watcher()

    def geometry(self, mode):
        wl = self.wl
        if mode == TAB:
            return wl.ANCHOR_TOP | wl.ANCHOR_RIGHT, (TAB_W, TAB_H), 0
        return wl.ANCHOR_ALL, (0, 0), 0

    def _tab_enabled(self):
        return bool(config.get_value(self.cfg, ("command_center", "game_screen_tab")))

    def _closed_mode(self):
        return TAB if (self.docked and self._tab_enabled()) else HIDDEN

    # -- UI ----------------------------------------------------------------------
    def build_ui(self, w, h):
        self.w, self.h = w, h
        if not getattr(self, "title", None):
            import device
            self.title = device.app_title()
        if self.io_worker is None:
            self.io_worker = main.Worker("rp5deck-overlay-io", self.post)
        tab = swap_ui.PullTab(self.open_command_center)
        self.ui = OverlayUI(self, w, h, self.title, tab)
        cc = self.cfg.get("command_center") or {}
        self.pull = PULL(on_change=self.on_pull, swipe_down_enabled=True,
                         auto_close_timeout_s=int(cc.get("auto_close_timeout_s", 0) or 0),
                         hardware_button=cc.get("hardware_button", "none"))
        self.gestures = ui.SwipeRecognizer(lambda: self.h, wants=self._wants_gesture,
                                           on_gesture=self.on_gesture, **TAB_SWIPE)
        self.router = ui.TouchRouter(self.ui.root, log=log.info, gestures=self.gestures)
        self.companion = None
        self.ui.set_view("companion")

    def _swipe_params(self):
        if self.pull.is_open():
            return summon.swipe_recognizer_kwargs(
                config.get_value(self.cfg, ("command_center", "swipe_sensitivity")))
        return dict(TAB_SWIPE)

    def _apply_swipe_params(self):
        kw = self._swipe_params()
        self.gestures.edge, self.gestures.distance = kw["edge"], kw["distance"]

    def _wants_gesture(self, name):
        if not self.docked:
            return False
        if not self.pull.is_open():
            return name in ("swipe_down_from_top", "swipe_down")
        return self.pull.wants(name)

    def on_gesture(self, name):
        log.info("gesture %s (overlay %s)", name, self.pull.state)
        self.last_gesture = {"name": name, "t": time.time()}
        if not self.pull.is_open() and name in ("swipe_down_from_top", "swipe_down"):
            self.pull._goto(PULL.COMMAND_CENTER, "swipe_down")
        else:
            self.pull.handle_gesture(name)
        self.state_dirty = True

    def on_pull(self, old, new, reason):
        log.info("OVERLAY %s -> %s (%s)", old, new, reason)
        self.pull_log = (self.pull_log + [[old, new, reason, round(time.time(), 3)]])[-20:]
        self.router.cancel_all()
        self.ui.close()
        if new == PULL.COMPANION:
            self.ui.set_view("companion")
            self.on_mode(self._closed_mode(), "closed (%s)" % reason)
        else:
            self.ui.set_view("cc")
            self.on_mode(FULL, "opened (%s)" % reason)
        self._apply_swipe_params()
        self._arm_cc_tick()
        self.state_dirty = True

    def open_command_center(self):
        if self.docked and not self.pull.is_open():
            self.pull._goto(PULL.COMMAND_CENTER, "tab")

    def open_settings(self):
        log.info("overlay: Settings live on the main panel")

    def _update_active(self):
        pass

    def _clock_tick(self):
        self.ui.bar.tick_clock()
        self.call_later(60.05 - (time.time() % 60.0), self._clock_tick)

    # -- services ----------------------------------------------------------------
    def start_services(self):
        binding = config.get_value(self.cfg, ("command_center", "hardware_button"))
        if binding and binding != "none":
            import threading
            self.summon_reader = summon.SummonButtonReader(
                binding, on_summon=lambda ev: self.post(self.on_summon_button, ev),
                log=log.info)
            self.summon_thread = threading.Thread(target=self._summon_run,
                                                  name="rp5deck-overlay-summon", daemon=True)
            self.summon_thread.start()
        self.call_later(CONFIG_POLL, self._config_poll)

    def on_summon_button(self, ev):
        self.summon_presses += 1
        self.last_summon = {"binding": ev.binding, "device": ev.device_path, "t": time.time()}
        if not self.docked:
            return
        if self.pull.is_open():
            self.pull._goto(PULL.COMPANION, "summon_button")
            return
        panel = screen_swap.read_peer_state(self.panel_state_path, self.peer_alive)
        if screen_swap.peer_mode(panel) == FULL:
            return                  # the main panel shows its own Command Center
        if panel_takes_summon(panel):
            log.info("summon button: the main panel opens its Command Center over the "
                     "emulator's screen (CC5) - not here")
            return
        log.info("summon button: main panel is %s - opening here",
                 screen_swap.peer_mode(panel) or "not running")
        self.pull._goto(PULL.COMMAND_CENTER, "summon_button")

    def _cfg_sig(self):
        try:
            st = os.stat(config.config_path())
            return (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _config_poll(self):
        """Settings the overlay follows live: the tab, the auto-close
        timeout, swipe sensitivity - and now Appearance (this is a SEPARATE
        process from main.py, with its own ui.THEME, so a theme change made
        on the main panel only reaches this process by re-reading
        config.json, same as everything else here). Read-only: never saves
        the whole file."""
        sig = self._cfg_sig()
        if sig != self.cfg_sig:
            self.cfg_sig = sig
            self.cfg, self.cfg_note = config.load()
            self.config_reloads += 1
            self.pull.auto_close_timeout_s = int(
                config.get_value(self.cfg, ("command_center", "auto_close_timeout_s")) or 0)
            self._apply_swipe_params()
            palettes.apply_theme(self.cfg)
            # See main.App.on_setting()'s "appearance" branch: mutating
            # ui.THEME does not by itself get anything repainted - the
            # whole surface must be marked damaged so the next frame
            # actually redraws with the new theme.
            if self.ui is not None:
                self.ui.root.damage_all()
            if not self.pull.is_open() and self.mode != self._closed_mode():
                self.on_mode(self._closed_mode(), "game_screen_tab changed")
            self.state_dirty = True
        self.call_later(CONFIG_POLL, self._config_poll)

    # -- placement -----------------------------------------------------------------
    def on_placement(self, p):
        was = self.docked
        self.docked = bool(p.docked)
        main.App.on_placement(self, p)
        if not self.docked and self.pull.is_open():
            self.pull._goto(PULL.COMPANION, "mode_undocked")
        elif was != self.docked and not self.pull.is_open():
            self.on_mode(self._closed_mode(), "docked" if self.docked else "undocked")

    def after_swap_requested(self):
        if self.pull.is_open():
            self.pull._goto(PULL.COMPANION, "swap")


def run(argv=None):
    return main.main(argv, app_cls=OverlayApp, log_name="rp5deck-overlay.log",
                     description="rp5deck Command Center on the game screen (SW1)")


if __name__ == "__main__":
    sys.exit(run())
