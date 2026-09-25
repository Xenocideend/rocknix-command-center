#!/usr/bin/env python3
"""rp5deck - touch companion app for the Retroid Pocket 5's bottom screen.

    python3 main.py [--seconds N] [--output NAME]

Settings live in one JSON file (config.py; default /storage/rp5deck/config.json,
schema v1, edited live from the Settings sheet): cfg["output"] is the screen
the panel binds to (default DSI-1), cfg["es_output"] the screen
EmulationStation is on (default DP-1). --output overrides for one run.

Navigation v2 (DESIGN.md, I1): the default view is the COMPANION (art /
video of the game selected in ES, or of the running game; companion.py). The
COMMAND CENTER is pulled down over it (summon.PullDownStateMachine): swipe
down from the top edge, a tap on the pull tab, or the hardware button
(command_center.hardware_button) opens it; swipe up, Close / Back, the button
again, or the auto-close timeout close it. Its first row is master volume.
ES selection / game events come from the es-hooks (esevents.py) plus a
/runningGame poll. A brief volume overlay shows on the companion whenever the
level changes while the Command Center is closed.

A zwlr_layer_surface_v1 on DSI-1 (layer TOP, keyboard interactivity NONE),
so a tap never takes keyboard focus from EmulationStation / the game on the
top screen (DESIGN.md). Three display modes come from sway IPC:
FULL (covers every DSI-1 pixel), BAR (bottom strip with an exclusive zone,
while an rp5deck-* window uses the rest), HIDDEN (unmapped: something else
owns DSI-1, or the device is undocked).

CC5 (hidden_overlay.py): a fourth mode, OVERLAY, entered only from HIDDEN
when an emulator's second window owns the panel's screen: the summon button
(or the optional corner handle) maps this same surface full panel over that
window, on the layer-shell OVERLAY layer (above fullscreen windows), still
keyboard NONE, showing the Command Center volume first; Close / the button
again / swipe up / an auto-close timeout unmap it back to HIDDEN, so the
emulator gets its touch screen back. The game keeps focus and keeps running.

Environment:
  RP5DECK_AUDIO_DRYRUN=1   audio setters log instead of running (readers stay real)
  RP5DECK_LOG_DIR          log directory (default /storage/rp5deck/log; 512 KB x 2)
  RP5DECK_RUN_DIR          runtime directory for state.json (default /run/rp5deck)
  RP5DECK_STDERR=1         also log to stderr
  RP5DECK_DEBUG=1          SIGUSR1 injects a scripted finger drag on the volume
                           slider into SDL's own event queue (testing only: sway's
                           `seat cursor set` delivers no motion during a press;
                           the Command Center must be open)
  RP5DECK_ES_SPOOL         the ES hooks' spool dir, one file per event (default
                           /var/run/rp5deck/es-events; if unset, the directory of
                           RP5DECK_ES_EVENT_FILE + /es-events is used)
  RP5DECK_OSK              on-screen keyboard for Firefox: auto | button | off
                           (default auto; config apps.osk_mode wins when present)

Browser / Discord (HF1, web_tiles.py): the tiles start Firefox (rp5deck-web);
092's rules put the window on DSI-1, the mode goes BAR and the strip shows
that app's controls. rp5deck never sends a
sway command for this: the owner taps Firefox to type (sway focuses it), and
focus_guard.py returns focus to ES when the window closes.

Screen swap (SW1, screen_swap.py): the panel binds to whichever output
EmulationStation is NOT on - as sway shows ES, so a setting 092 has not acted
on can never put the panel under ES - and follows a swap live by re-creating
its layer surface on the other output (wl_layer.rebind). The Command Center
tile "Swap screens" (and Settings > Screens) writes screens.es_screen; 092
moves ES. cc_overlay.py (a second process) is the same Command Center on the
game screen, as a pull-down; App's hooks (layer_level, placement_output,
initial_mode, start_watchers) are what it overrides.

Exit codes: 0 clean (SIGTERM/SIGINT/--seconds), 1 crash, 2 startup failure,
3 the compositor closed the layer surface, 4 the surface could not be
(re)mapped. The 094 supervisor restarts on any of them.
"""
import argparse
import heapq
import json
import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
from ctypes import byref, c_int, c_void_p

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import appearance_view  # noqa: E402  (Appearance)
import audioctl   # noqa: E402
import bar_autohide  # noqa: E402  (AH)
import charge_limit  # noqa: E402  (YT4)
import charge_stuck_view  # noqa: E402  (CHG)
import cleanstate_view  # noqa: E402  (CC1)
import companion  # noqa: E402
import config     # noqa: E402
import device     # noqa: E402
import dualscreen_keys_view  # noqa: E402  (DS)
import esevents   # noqa: E402
import hidden_overlay  # noqa: E402  (CC5)
import hotkeys     # noqa: E402  (CC7)
import hotkeys_view  # noqa: E402  (CC7)
import hud        # noqa: E402
import rgb_leds   # noqa: E402  (RG)
import rgb_view   # noqa: E402  (RG)
import rocknix_keyboard  # noqa: E402
import screens    # noqa: E402
import settings_view  # noqa: E402
import summon     # noqa: E402
import app_tabs   # noqa: E402  (CC6: the app tabs; window moves only in window_switcher.py)
import sway_ipc   # noqa: E402
import system_sleep  # noqa: E402  (YT4)
import ui         # noqa: E402
import browser    # noqa: E402
import osk        # noqa: E402
import web_tiles  # noqa: E402
import screen_swap  # noqa: E402
import swap_ui    # noqa: E402
import palettes   # noqa: E402  (Appearance)
from sway_ipc import BAR, FULL, HIDDEN   # noqa: E402

VERSION = "I1"
BAR_H = screens.BAR_H
VOLUME_HZ = 20.0
HUD_PERIOD = 1.0
BATTERY_PERIOD = 30.0
REMAP_WATCHDOG = 3.0
OSD_SECONDS = 1.5
SAVE_DELAY = 0.8
ES_POLL_PERIOD = 3.0
SLEEP_DELAY_S = 1.0            # YT4: lets the Sleep tap's own release paint first
CHARGE_TOAST_S = 2.5           # YT4: how long a charge-limit error toast stays up
# DS: dual-screen settings guard (dualscreen_keys.py) - reading system.cfg
# (a few hundred lines) is cheap, so a modest interval is enough; the owner's
# rule is WARN ONLY here, never a write from this timer.
DS_POLL_S = 300.0
# CHG: "charger connected but not charging" guard (charge_stuck.py) - a
# handful of sysfs reads, cheap enough for the owner's own "every 10 s".
CHARGE_STUCK_POLL_S = 10.0
# SW2: the in-app mode watchdog (main.App._reconcile). Recomputes mode from a
# fresh sway tree and applies it only after seeing the SAME disagreement on
# two consecutive checks RECONCILE_PERIOD apart (never on the first sighting:
# a single stale read must not fight a transition that is already in flight).
RECONCILE_PERIOD = 5.0
RECONCILE_CONFIRM = 2
RECONCILE_MIN_GAP = 10.0        # rate limit: no more than one correction this often
# SW2: 094-rp5deck's hang detector kills a child whose heartbeat is this
# stale; main.py touches its heartbeat file about this often (well under the
# supervisor's own default stale threshold).
HEARTBEAT_PERIOD = 2.0
PULL = summon.PullDownStateMachine
ES_SCREEN_KEY = ("screens", "es_screen")

log = logging.getLogger("rp5deck")


def setup_logging(name="rp5deck.log"):
    log_dir = os.environ.get("RP5DECK_LOG_DIR", "/storage/rp5deck/log")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, name)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=1)
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if os.environ.get("RP5DECK_STDERR") == "1":
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return path


class Poster:
    """Hands callables from worker threads to the main thread and wakes
    SDL_WaitEventTimeout with a user event (SDL_PushEvent is thread-safe).
    At most one wake event is outstanding at a time."""

    def __init__(self):
        self.q = queue.SimpleQueue()
        self.ev_type = None
        self._lock = threading.Lock()
        self._pending = False

    def __call__(self, fn, *args):
        self.q.put((fn, args))
        self.wake()

    def wake(self):
        if self.ev_type is None:
            return
        with self._lock:
            if self._pending:
                return
            self._pending = True
        import sdl3
        ev = sdl3.SDL_Event()
        sdl3.UserEvent.from_buffer(ev).type = self.ev_type
        sdl3.PushEvent(byref(ev))

    def drain(self):
        with self._lock:
            self._pending = False
        while True:
            try:
                fn, args = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:       # noqa: BLE001
                log.exception("posted callback %r failed", fn)


class LogLimiter:
    """Log an exception with its traceback the first time a key fails, then
    at most once per `interval` seconds with a count of the repeats in
    between, so a call that fails on every tick cannot flood the 512 KB log.
    Thread-safe (workers call it)."""

    def __init__(self, logger, interval=60.0, clock=time.monotonic):
        self.log = logger
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._state = {}            # key -> [last logged at, repeats since]

    def exception(self, key, msg, *args):
        """Call from inside an except block. Returns True if it logged."""
        now = self.clock()
        with self._lock:
            st = self._state.get(key)
            if st is not None and now - st[0] < self.interval:
                st[1] += 1
                return False
            repeats = st[1] if st is not None else 0
            self._state[key] = [now, 0]
        if repeats:
            msg += " (%d more like this since the last report)" % repeats
        self.log.exception(msg, *args)
        return True


class _Failed:
    """The result a guarded() call posts when the call raised: `done`
    callbacks must clear their in-flight flags on it (RV2-m6), and it can
    never be mistaken for a real reading (None means "unknown" to several
    readers)."""

    def __repr__(self):
        return "FAILED"


FAILED = _Failed()


def guarded(fn, limiter):
    """Wrap fn so an exception is logged (rate-limited) and becomes FAILED,
    and the `done` callback still runs. Works with any worker (the tests'
    synchronous one too), since nothing about submit() changes."""
    name = getattr(fn, "__name__", repr(fn))

    def call(*args):
        try:
            return fn(*args)
        except Exception:           # noqa: BLE001
            limiter.exception(name, "%s failed", name)
            return FAILED
    call.__name__ = name
    call.__wrapped__ = fn
    return call


class Worker:
    """One background thread running submitted calls in order; results are
    posted back to the main thread."""

    def __init__(self, name, post):
        self.q = queue.Queue()
        self.post = post
        self.faillog = LogLimiter(log)
        self.t = threading.Thread(target=self._run, name=name, daemon=True)
        self.t.start()

    def submit(self, fn, *args, done=None):
        self.q.put((fn, args, done))

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            fn, args, done = item
            try:
                res = fn(*args)
            except Exception:       # noqa: BLE001
                self.faillog.exception(getattr(fn, "__name__", repr(fn)),
                                       "worker call %r failed", fn)
                continue            # no result is posted: callers keep their old state
            if done is not None:
                self.post(done, res)

    def stop(self, timeout=4.0):
        self.q.put(None)
        self.t.join(timeout)


class App:
    ROLE = "panel"                  # cc_overlay.OverlayApp: "overlay"

    def __init__(self, args):
        self.args = args
        self.stop_reason = None
        self.exit_code = 0
        self.post = Poster()
        self.timers = []
        self._seq = 0
        self.mode = None
        self.mode_reason = ""
        self.backend = audioctl.Backend()
        self.master = None
        self.last_set = None
        self.last_commit = None
        self.hud_timer = None
        self.hud_inflight = False
        self.hud_last = None
        self.mixer_inflight = False
        self.mixer_again = False
        self.vol_timer = None
        self.vol_throttle = audioctl.Throttle(1.0 / VOLUME_HZ, self._send_master)
        self.vol_live_sent = False      # a live set_master went out during this press
        self._set_lock = threading.Lock()
        self._set_value = None          # newest live value for the one queued set
        self._set_queued = False
        # master read-backs (RV2-M1): at most one get_master in flight; none
        # while a finger is on the slider or a commit / restore is pending
        self.master_inflight = False
        self.master_again = False
        self.commit_pending = False
        self.faillog = LogLimiter(log)
        self.stream_throttles = {}
        self.stream_timers = {}
        self.stream_live_sent = set()   # stream ids with a live set during this press
        self.state_dirty = True
        self.state_written = 0.0
        self.run_dir = os.environ.get("RP5DECK_RUN_DIR", "/run/rp5deck")
        self.counts = {"input_down": 0, "presents": 0, "resizes": 0}
        self.last_input = None
        self.unmapped_since = None
        self.started = time.time()
        self.w = self.h = 0
        self.sdl = self.win = self.ren = self.tex = self.canvas = None
        self.layer = None
        self.watcher = None
        self.subscriber = None
        self.audio_worker = self.hud_worker = None
        self._sig_r = self._sig_w = None
        self.debug = os.environ.get("RP5DECK_DEBUG") == "1"
        self.cfg, self.cfg_note = config.load()
        # Appearance: resolve the saved theme (default/a preset/custom)
        # into ui.THEME BEFORE any widget is built, so the very first
        # frame already shows it - screens.DeckUI's Root/Home/Bar/Sheet
        # constructions below read THEME live via bg="..."/color="..."
        # keys now (ui.py's Container.bg_color()/Label.color_value()), but
        # only once ui.THEME itself holds the right values.
        palettes.apply_theme(self.cfg)
        if args.output:
            self.cfg["output"] = args.output
        self.output = self.cfg["output"]          # the screen the Command Center lives on
        self.es_output = self.cfg["es_output"]    # the screen ES / games live on
        # SW1: --output pins the surface for this run; otherwise it follows ES
        self.output_forced = bool(args.output)
        self.placement = None           # screen_swap.Placement, last applied
        self.screen_watcher = None
        self.rebinds = 0
        # SW2: the reconcile watchdog (a backstop for "stuck": see _reconcile)
        self.reconcile_candidate = None     # (mode, reason) seen once, awaiting confirmation
        self.reconcile_last = 0.0           # monotonic time of the last correction applied
        self.reconcile_next = 0.0           # monotonic time of the next scheduled check
        self.reconcile_corrections = 0
        self.reconcile_skips = 0
        self.heartbeat_written = 0.0
        self._unsaved = {}              # key_path -> value not yet written (save_changes)
        self.last_gesture = None
        # Navigation v2 (I1)
        self.ui = self.router = self.gestures = None
        self.pull = None                # summon.PullDownStateMachine
        self.companion = None           # companion.CompanionController
        self.video = None               # companion.VideoWorker
        self.media_worker = self.io_worker = None
        self.es_watcher = None
        self.name_guard = None          # name_guard summary (RV1-M3), once scanned
        self.summon_reader = self.summon_thread = None
        self.summon_presses = 0
        self.last_summon = None
        self.pull_log = []
        self.osd_timer = None
        self.osd_shows = 0
        self.cc_tick_timer = None
        self.save_timer = None
        self.settings_changes = 0
        self.config_saves = 0
        self.vtex = None
        self.vtex_size = None
        self.vseq = None
        self.video_src = self.video_dst = None
        self.counts["video_uploads"] = 0
        # HF1: Browser / Discord
        self.web = None                 # web_tiles.WebApps
        self.web_worker = self.search_worker = None
        self.web_parts = {}             # tests inject fakes (browser, player, keyboard, ...)
        # W2b: the YouTube TV (leanback) tile - its OWN Firefox instance/
        # profile/app_id/Marionette port (browser.TV_*), never web_tiles.WebApps.
        self.ytapp = None                # web_tiles.YtAppSession
        self.ytapp_parts = {}            # tests inject a fake browser
        # AH: the YouTube TV app's auto-hidden strip (bar_autohide.py) - the
        # only BAR app kind that does not reserve the fixed BAR_H strip.
        self.ytauto = None               # bar_autohide.BarAutoHide
        # CC1: the Clean state tile (cleanstate_view -> cleanstate.Helper)
        self.clean = None               # cleanstate_view.CleanStateController
        self.clean_worker = None        # discover (~5 s) / execute run here, never on the UI
        self.clean_parts = {}           # tests inject helper_factory
        # CC5: the Command Center over an emulator's second screen
        self.cc5 = None                 # hidden_overlay.OverlayController
        self.cc5_parts = {}             # tests inject query / handle_factory / peer_state
        # CC6: the app tabs (app_tabs.TabsController; the panel only)
        self.tabs = None
        self.tabs_parts = {}            # tests inject switcher / avail / submit
        # RG: Stick lights (rgb_leds.py device control, rgb_view.py the sheet)
        self.rgb = None                 # rgb_view.RGBController
        self.rgb_timer = None           # keeper_tick() poll (rgb_leds.KEEPER_PERIOD_S)
        # YT4: the Sleep tile - guards a double tap and the ~1 s paint delay
        self.sleep_pending = False
        self.sleep_timer = None
        self.sleep_parts = {}            # tests inject a fake suspend()
        # YT4: Safe charge - main.App.charge_parts injects fake charge_limit
        # module-level functions (read_live/apply) so tests never touch real
        # sysfs; charge_available mirrors the last read_live() probe.
        self.charge_parts = {}
        self.charge_available = True
        self.charge_probe_inflight = False
        # DS: dual-screen settings guard (dualscreen_keys.py / _view.py)
        self.dualscreen = None          # dualscreen_keys_view.DualScreenKeysController
        self.ds_worker = None           # check() / restore() run here, never on the UI thread
        self.ds_parts = {}              # tests inject checker/restorer
        self.ds_timer = None
        # CHG: "charger connected but not charging" guard (charge_stuck.py)
        self.charge_stuck = None        # charge_stuck_view.ChargeStuckController
        self.charge_stuck_parts = {}    # tests inject a fake sampler
        self.charge_stuck_timer = None

    # -- timers -----------------------------------------------------------
    def call_at(self, t, fn):
        self._seq += 1
        h = [t, self._seq, fn, False]
        heapq.heappush(self.timers, h)
        return h

    def call_later(self, delay, fn):
        return self.call_at(time.monotonic() + delay, fn)

    @staticmethod
    def cancel(h):
        if h is not None:
            h[3] = True

    def _run_timers(self, now):
        while self.timers and self.timers[0][0] <= now:
            h = heapq.heappop(self.timers)
            if not h[3]:
                h[2]()

    # -- signals ----------------------------------------------------------
    def _install_signals(self):
        # Must happen BEFORE SDL_Init (and SDL_HINT_NO_SIGNAL_HANDLERS is set
        # too), so Python - not SDL - owns SIGTERM/SIGINT. Python's C-level
        # handler writes the signal number to the wakeup fd even while the
        # main thread is blocked inside SDL_WaitEventTimeout; a helper thread
        # turns that byte into an SDL user event, which wakes the loop.
        self._sig_r, self._sig_w = os.pipe()
        os.set_blocking(self._sig_w, False)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        if self.debug:
            signal.signal(signal.SIGUSR1, lambda s, f: None)   # handled via the wakeup fd
        signal.set_wakeup_fd(self._sig_w, warn_on_full_buffer=False)
        threading.Thread(target=self._sig_thread, name="rp5deck-signals", daemon=True).start()

    def _on_signal(self, signum, frame):
        if self.stop_reason is None:
            self.stop_reason = signal.Signals(signum).name

    def _sig_thread(self):
        # If this thread ends, SIGTERM still sets stop_reason (the Python
        # handler), but the loop only notices at its next wake-up (up to 60 s
        # when idle), so its death must be visible in the log (RV5b).
        while True:
            try:
                data = os.read(self._sig_r, 16)
            except OSError:
                log.exception("signal pipe read failed; signals now wait for the next "
                              "wake-up of the main loop")
                return
            if not data:
                log.error("signal pipe closed; signals now wait for the next wake-up "
                          "of the main loop")
                return
            for b in data:
                self.post(self._signal_stop, b)

    def _signal_stop(self, signum):
        if self.debug and signum == signal.SIGUSR1:
            self._debug_drag()
            return
        if self.stop_reason is None:
            try:
                self.stop_reason = signal.Signals(signum).name
            except ValueError:
                self.stop_reason = "signal %d" % signum

    # -- startup ----------------------------------------------------------
    def setup(self):
        import gfx
        import sdl3
        import wl_layer
        self.sdl, self.gfx, self.wl = sdl3, gfx, wl_layer
        self._install_signals()

        self._initial_placement()
        log.info("config: %s -> output=%s es_output=%s", self.cfg_note, self.output,
                 self.es_output)
        mode0, why0 = self.initial_mode()
        log.info("initial mode %s (%s)", mode0, why0)

        sdl3.SetHint(sdl3.HINT_TOUCH_MOUSE_EVENTS, b"0")
        sdl3.SetHint(sdl3.HINT_MOUSE_TOUCH_EVENTS, b"0")
        sdl3.SetHint(sdl3.HINT_NO_SIGNAL_HANDLERS, b"1")
        # Without this SDL creates an idle inhibitor for its window, which
        # would stop the handheld from ever blanking or sleeping.
        sdl3.SetHint(sdl3.HINT_VIDEO_ALLOW_SCREENSAVER, b"1")
        if not sdl3.Init(sdl3.INIT_VIDEO | sdl3.INIT_EVENTS):
            raise StartupError("SDL_Init: %s" % sdl3.error())
        drv = sdl3.GetCurrentVideoDriver()
        if drv != b"wayland":
            raise StartupError("video driver is %r, need wayland" % drv)
        ev_type = sdl3.RegisterEvents(1)
        if ev_type in (0, 0xFFFFFFFF):
            raise StartupError("SDL_RegisterEvents failed")
        self.post.ev_type = ev_type

        props = sdl3.CreateProperties()
        sdl3.SetBooleanProperty(props, b"SDL.window.create.wayland.surface_role_custom", True)
        sdl3.SetBooleanProperty(props, b"SDL.window.create.opengl", True)
        sdl3.SetNumberProperty(props, b"SDL.window.create.width", 1920)
        sdl3.SetNumberProperty(props, b"SDL.window.create.height", 1080)
        # Never "Bottom"/"Secondary"/"Screen 2" in a title: ROCKNIX's sway
        # rules match those words.
        sdl3.SetStringProperty(props, b"SDL.window.create.title", b"rp5deck")
        self.win = sdl3.CreateWindowWithProperties(props)
        sdl3.DestroyProperties(props)
        if not self.win:
            raise StartupError("SDL_CreateWindowWithProperties: %s" % sdl3.error())
        self.ren = sdl3.CreateRenderer(self.win, None)
        if not self.ren:
            raise StartupError("SDL_CreateRenderer: %s" % sdl3.error())
        sdl3.SetRenderVSync(self.ren, 0)
        wprops = sdl3.GetWindowProperties(self.win)
        display = sdl3.GetPointerProperty(wprops, b"SDL.window.wayland.display", None)
        surface = sdl3.GetPointerProperty(wprops, b"SDL.window.wayland.surface", None)
        if not display or not surface:
            raise StartupError("SDL did not expose wl_display / wl_surface")
        log.info("renderer %s", sdl3.GetRendererName(self.ren).decode())

        self.layer = wl_layer.LayerSurface(
            display, surface, output_name=self.output, layer=self.layer_level(),
            namespace="rp5deck", anchor=wl_layer.ANCHOR_ALL, size=(0, 0),
            exclusive_zone=0, keyboard=wl_layer.KEYBOARD_NONE,
            log=lambda m: log.info(m))
        w, h = self.layer.create()
        self.layer.pending_size()
        log.info("layer surface on %s: TOP, keyboard NONE, %dx%d",
                 self.layer.output.name, w, h)

        n = c_int()
        ids = sdl3.GetTouchDevices(byref(n))
        names = [sdl3.GetTouchDeviceName(ids[k]).decode() for k in range(n.value)] if ids else []
        if ids:
            sdl3.free(ids)
        log.info("touch devices: %s", names)

        self.title = device.app_title()
        log.info("title: %s", self.title)
        sdl3.SetRenderDrawColor(self.ren, 0, 0, 0, 255)
        self.build_ui(w, h)
        self._resize(w, h)

        self.audio_worker = Worker("rp5deck-audio", self.post)
        self.hud_worker = Worker("rp5deck-hud", self.post)
        log.info("audio dry-run: %s", self.backend.dryrun)
        self._request_master()
        self.subscriber = self.backend.subscriber(
            lambda kind: self.post(self.on_audio_event, kind))
        self.subscriber.start()
        self.start_services()

        self.mode = FULL
        self.mode_reason = "startup"
        if mode0 != FULL:
            self.on_mode(mode0, why0)
        self.start_watchers(mode0)

        self._clock_tick()
        self._battery_tick()
        self._rgb_tick()                 # RG: keeper_tick() poll
        self._ds_tick()                  # DS: dual-screen settings guard poll
        self._charge_stuck_tick()        # CHG: "charger not charging" guard poll

    # -- SW1: screen placement (hooks cc_overlay.OverlayApp overrides) ------
    def layer_level(self):
        return self.wl.LAYER_TOP

    def placement_output(self, p):
        """The output this process's surface belongs on."""
        return p.cc_output

    def initial_mode(self):
        mode0, why0 = sway_ipc.query_mode(internal=self.output, external=self.es_output)
        if mode0 is None:
            log.warning("initial mode unknown (%s); starting FULL", why0)
            mode0, why0 = FULL, "initial: %s" % why0
        return mode0, why0

    def start_watchers(self, mode0):
        self.watcher = sway_ipc.ModeWatcher(
            lambda m, r: self.post(self._on_watched_mode, m, r,
                                   self.watcher.internal, self.watcher.external),
            initial=mode0, internal=self.output, external=self.es_output)
        self.watcher.start()
        self._start_screen_watcher()

    def _on_watched_mode(self, mode, reason, internal, external):
        """ModeWatcher (background thread) computed this against the
        (internal, external) pair it had AT THAT MOMENT, then posted it here
        - but on_placement can rebind and recompute mode (synchronously,
        against the fresh output pairing) from an event queued AHEAD of this
        one, in the same drain() pass. Applying a mode computed for an output
        pairing we have since moved away from would silently undo that fresh,
        correct recompute (the real bug, 24 Sep: ES's transient post-game
        blip on DSI-1, main.on_placement rebinding DP-1->DSI-1 there, and
        this exact race leaving the panel HIDDEN and blank until restart -
        see the bug report for the full trace). Safe to just drop: whichever
        of on_placement's own recompute or the watcher's NEXT real
        computation (using the now-updated watcher.internal/external) runs
        next will reflect the current placement correctly."""
        if (internal, external) != (self.output, self.es_output):
            log.info("mode event dropped (stale): computed for %s/%s, now on %s/%s (%s)",
                     internal, external, self.output, self.es_output, reason)
            return
        self.on_mode(mode, reason)

    def _start_screen_watcher(self):
        if self.output_forced:
            log.info("screens: --output %s pins the surface; not following ES", self.output)
            return
        self.screen_watcher = screen_swap.ScreenWatcher(
            lambda p: self.post(self.on_placement, p), initial=self.placement)
        self.screen_watcher.start()

    def _initial_placement(self):
        """Where ES is right now decides where this surface is created."""
        if self.output_forced:
            return
        setting = screen_swap.read_setting()
        p, err = screen_swap.query_placement(setting)
        if p is None:
            p = screen_swap.fallback_placement(setting)
            log.warning("screens: sway not answering (%s); assuming %s", err, p)
        self.placement = p
        self.es_output = p.es_output
        self.output = self.placement_output(p)
        log.info("screens: %s -> this %s binds to %s", p, self.ROLE, self.output)

    def on_placement(self, p):
        """ScreenWatcher (main thread, via post): ES moved, the add-on came
        or went, or the setting changed while ES could not be seen."""
        self.placement = p
        self._refresh_screen_setting()
        if self.output_forced:
            return
        self.es_output = p.es_output
        want = self.placement_output(p)
        if want != self.output and not self._rebind(want, p.source):
            return
        if self.watcher is not None:
            self.watcher.internal, self.watcher.external = self.output, self.es_output
            mode, why = sway_ipc.query_mode(internal=self.output, external=self.es_output)
            if mode is not None:
                self.watcher.current = mode
                self.on_mode(mode, "placement: %s" % why)
        self.state_dirty = True

    def _rebind(self, output, reason):
        log.info("SCREENS: moving the %s surface %s -> %s (%s)", self.ROLE, self.output,
                 output, reason)
        try:
            self.layer.rebind(output)
        except Exception:           # noqa: BLE001
            # A fresh process binds to the right output by itself (setup ->
            # _initial_placement), so a failed rebind costs a restart only.
            log.exception("rebind to %s failed; exiting so the supervisor starts a fresh "
                          "surface there", output)
            self.stop_reason = "rebind to %s failed" % output
            self.exit_code = 4
            return False
        self.output = output
        self.rebinds += 1
        self._output_changed(output)
        if self.router is not None:
            self.router.cancel_all()
        if self.ui is not None:
            self.ui.root.damage_all()
        return True

    def _output_changed(self, output):
        """Things of the main panel that name its output (HF1's web apps:
        the BAR check and the on-screen keyboard's --output)."""
        web = getattr(self, "web", None)
        if web is None:
            return
        if hasattr(web, "internal"):
            web.internal = output
        kb = getattr(web, "keyboard", None)
        if kb is not None and hasattr(kb, "output"):
            kb.output = output

    def _refresh_screen_setting(self):
        """The file is the truth for the swap (the overlay may have changed
        it): keep this process's copy and the Settings toggle in step,
        without queueing a save."""
        cur = screen_swap.read_setting()
        if config.get_value(self.cfg, ES_SCREEN_KEY) == cur:
            return
        config.set_value(self.cfg, ES_SCREEN_KEY, cur)
        rows = getattr(self.ui.settings, "rows", None) if self.ui is not None and \
            self.ui.settings is not None else None
        if rows and ES_SCREEN_KEY in rows:
            rows[ES_SCREEN_KEY].refresh_from_cfg()

    def ask_swap_screens(self):
        """The "Swap screens" tile: confirm first."""
        cur = screen_swap.read_setting()
        docked = self.placement.docked if self.placement is not None else True
        title, text, yes = swap_ui.swap_prompt(cur, docked)
        self.ui.cc.confirm.ask(title, text, yes, on_yes=self.swap_screens_now,
                               on_no=self.close_sheet)
        self.ui.open(swap_ui.CONFIRM_SHEET)
        log.info("screen -> confirm (%s)", title)
        self.state_dirty = True

    def swap_screens_now(self):
        cur = screen_swap.read_setting()            # the file, not a possibly stale copy
        new = screen_swap.swapped_value(cur)
        log.info("SCREENS: swap requested on the %s: es_screen %s -> %s (092 moves ES at "
                 "its next poll; this surface follows ES)", self.ROLE, cur, new)
        config.set_value(self.cfg, ES_SCREEN_KEY, new)
        self.on_setting(ES_SCREEN_KEY, new)
        self.ui.close()
        self.after_swap_requested()
        self.state_dirty = True

    def after_swap_requested(self):
        if self.pull is not None and self.pull.is_open():
            self.pull._goto(PULL.COMPANION, "swap")

    def build_ui(self, w, h):
        """The widget tree and the navigation around it (no SDL needed, so
        tests build the real thing): companion view + controller, settings
        sheet, Command Center, pull-down state machine, swipe recogniser,
        touch router. Needs self.w/self.h, self.title; creates the media
        worker and the video worker unless a test already injected them."""
        self.w, self.h = w, h
        if not getattr(self, "title", None):
            self.title = device.app_title()
        if self.media_worker is None:
            self.media_worker = Worker("rp5deck-media", self.post)
        if self.io_worker is None:
            self.io_worker = Worker("rp5deck-io", self.post)
        if self.web_worker is None:
            self.web_worker = Worker("rp5deck-web", self.post)
        if self.search_worker is None:
            self.search_worker = Worker("rp5deck-search", self.post)
        if self.clean_worker is None:
            self.clean_worker = Worker("rp5deck-clean", self.post)
        if self.ds_worker is None:
            self.ds_worker = Worker("rp5deck-ds", self.post)
        if self.video is None:
            self.video = companion.VideoWorker(lambda: self.post(self.on_video_frame),
                                               log_fn=log.info)
        view = companion.CompanionView(self)
        settings = settings_view.SettingsSheet(
            self.cfg, self.on_setting, self.close_settings, app_version=VERSION,
            # Appearance: self.appearance (appearance_view.AppearanceController)
            # is built further down, after self.ui exists (its sheet needs
            # add_sheet()) - these two callables are only ever CALLED once
            # the sheet is up and the owner taps a button, by which point
            # self.appearance is set, so the late lookup here is safe.
            on_edit_colours=lambda: self.appearance.open(),
            on_reset_appearance=self.reset_appearance)
        hotkeys_sheet = hotkeys_view.HotkeysSheet(self)   # CC7
        self.ui = screens.DeckUI(self, w, h, self.title, companion=view, settings=settings,
                                 hotkeys=hotkeys_sheet)
        self.pull = PULL.from_config(self.cfg.get("command_center"), on_change=self.on_pull)
        kw = summon.swipe_recognizer_kwargs(
            config.get_value(self.cfg, ("command_center", "swipe_sensitivity")))
        # Taps and drags go to widgets; the recogniser watches every stroke
        # and claims the ones the pull-down wants (only in FULL mode: in BAR
        # the surface is a 140 px strip and HIDDEN receives nothing).
        # YT3: BAR mode is otherwise gestureless for every app (checked: the
        # Browser/Discord strip has no swipe-up either, since `self.mode` is
        # never FULL/OVERLAY while their window fills DSI-1) - added here for
        # the YouTube App's tv strip ONLY (gated on `self.ytauto.active`,
        # bar_autohide.BarAutoHide's own "BAR + Bar.app == 'tv'" flag), a
        # swipe up on the strip or its auto-hidden handle opens the Command
        # Center the same way tapping Home does (on_gesture, below).
        self.gestures = ui.SwipeRecognizer(
            lambda: self.h,
            wants=lambda g: (self.mode in (FULL, hidden_overlay.OVERLAY) and self.pull.wants(g))
            or (self.mode == BAR and self.ytauto is not None and self.ytauto.active
                and g in ("swipe_up", "swipe_up_from_bottom")),
            on_gesture=self.on_gesture, **kw)
        self.router = ui.TouchRouter(self.ui.root, log=log.info, gestures=self.gestures)
        self.companion = companion.CompanionController(
            view, self._submit_media, self, self.video, lambda: self.cfg)
        self.web = self._make_web()
        self.ytapp = self._make_ytapp()  # W2b
        # AH: 0 (config.get_value default if the key is missing) disables
        # auto-hide, same "0 = off" convention as auto_close_timeout_s.
        self.ytauto = bar_autohide.BarAutoHide(
            self, timeout_s=config.get_value(self.cfg, ("youtube", "tv_bar_hide_s")))
        self.cc5 = hidden_overlay.OverlayController(self, **self.cc5_parts)
        self.clean = cleanstate_view.CleanStateController(self, self._submit_clean,
                                                          **self.clean_parts)
        self.ui.add_sheet("cleanstate", self.clean.sheet)
        # DS: dual-screen settings guard - a Home banner, not a tile; see
        # dualscreen_keys_view.py.
        self.dualscreen = dualscreen_keys_view.DualScreenKeysController(
            self, self._submit_ds, **self.ds_parts)
        self.ui.add_sheet("dualscreen", self.dualscreen.sheet)
        # CHG: "charger connected but not charging" - a Home banner, no
        # sheet (read-only sysfs; nothing here to confirm). Shares io_worker
        # (charge_limit's own worker, plain sysfs reads - unlike DS's
        # restore(), never blocks for seconds).
        self.charge_stuck = charge_stuck_view.ChargeStuckController(
            self, self._submit_io, **self.charge_stuck_parts)
        # RG: Stick lights - initial state loads from config.get_value()
        # through rgb_leds.state_from_config() (the "lights" schema group
        # in config.py).
        lights = rgb_leds.Controller(state=rgb_leds.state_from_config(self.cfg))
        self.rgb = rgb_view.RGBController(self, lights)
        self.ui.add_sheet("lights", self.rgb.sheet)
        # Appearance: appearance_view.AppearanceController owns the
        # "Custom colours" sheet (opened from the Settings > Appearance
        # page's own button, not a Home tile - see close_appearance()
        # below for why it returns to "settings" rather than Home).
        self.appearance = appearance_view.AppearanceController(self, self.cfg)
        self.ui.add_sheet("appearance_custom", self.appearance.sheet)
        # Tile customisation: push the persisted order/hidden set into
        # Home (construction order / nothing-hidden until config.py's
        # "command_center.tile_order"/"hidden_tiles" say otherwise -
        # on_tile_layout_changed() below writes them back).
        self.ui.cc.home.set_order(config.get_value(self.cfg, ("command_center", "tile_order")))
        self.ui.cc.home.set_hidden(config.get_value(self.cfg, ("command_center", "hidden_tiles")))
        self.ui.cc.set_bar_app(None, keys_offered=self.web.policy.enabled())
        self.ui.set_view("companion")
        self.companion.refresh()
        self._update_active()
        self.tabs = app_tabs.TabsController(self, **self.tabs_parts)     # CC6
        self.ui.cc.set_tabs(self.tabs.strip)

    def _submit_media(self, fn, *args, done=None):
        self.media_worker.submit(fn, *args, done=done)

    def _apps_setting(self, key, ok, default):
        """An apps.* value if the config has a valid one (config.py has no
        such keys yet: the HF1 report asks for them), else the default."""
        v = config.get_value(self.cfg, ("apps", key))
        try:
            return v if v is not None and ok(v) else default
        except Exception:           # noqa: BLE001 - a bad value is ignored, not fatal
            return default

    def _make_web(self):
        height = self._apps_setting("osk_height_px",
                                    lambda v: isinstance(v, int) and 200 <= v <= 600,
                                    osk.DEFAULT_HEIGHT)
        home = self._apps_setting("browser_home_url",
                                  lambda v: isinstance(v, str) and v.startswith("https://"),
                                  browser.HOME_URL)
        parts = {
            "keyboard": osk.Wvkbd(output=self.output, height=height),
            "osk_mode": osk.resolve_mode(config.get_value(self.cfg, ("apps", "osk_mode"))),
            "registry": web_tiles.ChildRegistry(os.path.join(self.run_dir, "children.json")),
            "home_url": home,
        }
        parts.update(self.web_parts)
        return web_tiles.WebApps(self, self._submit_web, self._submit_search,
                                 internal=self.output, **parts)

    def _make_ytapp(self):
        """W2b: its own Firefox instance/profile - see web_tiles.YtAppSession's
        own doc for why this is not a third WebApps label."""
        parts = {"registry": self.web.registry,
                # YT4: live-updatable via on_setting(), read once at startup here
                "swipe_natural": config.get_value(self.cfg, ("youtube", "tv_swipe_natural"))}
        parts.update(self.ytapp_parts)
        return web_tiles.YtAppSession(self, self._submit_web, **parts)

    def _submit_web(self, fn, *args, done=None):
        self.web_worker.submit(fn, *args, done=done)

    def _submit_search(self, fn, *args, done=None):
        self.search_worker.submit(fn, *args, done=done)

    def _submit_clean(self, fn, *args, done=None):
        self.clean_worker.submit(fn, *args, done=done)

    def _submit_ds(self, fn, *args, done=None):
        self.ds_worker.submit(fn, *args, done=done)

    def _submit_io(self, fn, *args, done=None):
        self.io_worker.submit(fn, *args, done=done)

    def start_services(self):
        """ES events (hooks + /runningGame poll) and the summon button reader.
        Both survive their device being absent (the PC, tests, undocked)."""
        import name_guard
        # HF1: a Firefox / mpv / wvkbd orphaned by a hard crash of the last run
        # (no shutdown ran) is stopped before the mode watcher starts.
        self.web.reap_stale()
        self.es_watcher = esevents.Watcher(lambda ev: self.post(self.on_es_event, ev),
                                           running_probe=esevents.probe_running_game,
                                           poll_interval=ES_POLL_PERIOD, log_fn=log.info)
        self.es_watcher.start()
        # RV1-M3: warn (log + companion view) if ES would shell-parse any game
        # name/path through the hooks. Scans on its own daemon thread.
        name_guard.check_async(on_done=lambda r: self.post(self.on_name_guard, r),
                               log_fn=log.warning)
        binding = config.get_value(self.cfg, ("command_center", "hardware_button"))
        if binding and binding != "none":
            self.summon_reader = summon.SummonButtonReader(
                binding, on_summon=lambda ev: self.post(self.on_summon_button, ev),
                log=log.info)
            self.summon_thread = threading.Thread(target=self._summon_run, name="rp5deck-summon",
                                                  daemon=True)
            self.summon_thread.start()
            log.info("summon button: %s", binding)
        else:
            log.info("summon button: none (swipe / pull tab only)")

    def _stop_summon(self):
        r = self.summon_reader
        if r is None:
            return
        r.stop()
        if self.summon_thread is not None:
            self.summon_thread.join(1.0)
        r.close()

    def _summon_run(self):
        try:
            self.summon_reader.run()
        except Exception:           # noqa: BLE001
            log.exception("summon reader died")

    def _resize(self, w, h):
        sdl = self.sdl
        if self.tex:
            sdl.DestroyTexture(self.tex)
        if self.vtex:
            sdl.DestroyTexture(self.vtex)
            self.vtex, self.vtex_size, self.vseq = None, None, None
        if self.canvas:
            self.canvas.free()
        self.w, self.h = w, h
        sdl.SetWindowSize(self.win, w, h)
        self.tex = sdl.CreateTexture(self.ren, sdl.PIXELFORMAT_ARGB8888,
                                     sdl.TEXTUREACCESS_STREAMING, w, h)
        # cairo ARGB32 is premultiplied; the companion leaves a transparent
        # hole over the video texture drawn underneath.
        if not sdl.SetTextureBlendMode(self.tex, sdl.BLENDMODE_BLEND_PREMULTIPLIED):
            # The UI is opaque except over the video, so the panel still looks
            # right; only video-under-UI compositing would be wrong.
            log.warning("premultiplied blend refused by the renderer: %s", sdl.error())
        self.canvas = self.gfx.Canvas(w, h)
        self.ui.set_size(w, h)
        self._update_active()
        self.counts["resizes"] += 1
        self.state_dirty = True
        log.info("surface size %dx%d", w, h)

    # -- main loop --------------------------------------------------------
    def run(self):
        self.setup()
        ev = self.sdl.SDL_Event()
        deadline = time.monotonic() + self.args.seconds if self.args.seconds else None
        log.info("running%s", " for %.0fs" % self.args.seconds if deadline else "")
        while self.stop_reason is None:
            now = time.monotonic()
            if deadline and now >= deadline:
                self.stop_reason = "timeout %.0fs" % self.args.seconds
                break
            self.post.drain()
            self._run_timers(now)
            if self.layer.closed:
                self.stop_reason = "layer surface closed by compositor"
                self.exit_code = 3
                break
            self._check_mapped(now)
            self._reconcile(now)
            self._touch_heartbeat(now)
            self._apply_configure()
            self._render()
            self._write_state_if_due(now)
            if self.stop_reason is not None:
                break
            timeout = self._next_timeout(deadline)
            if self.sdl.WaitEventTimeout(byref(ev), timeout):
                self._handle(ev)
                while self.sdl.PollEvent(byref(ev)):
                    self._handle(ev)
        log.info("exiting: %s", self.stop_reason)
        return self.exit_code

    def _next_timeout(self, deadline):
        now = time.monotonic()
        t = now + 60.0
        if self.timers:
            t = min(t, self.timers[0][0])
        if deadline:
            t = min(t, deadline)
        if self.state_dirty:
            t = min(t, self.state_written + 0.1)
        if self.mode != HIDDEN and not self.layer.can_present:
            t = min(t, now + 0.1)       # waiting for a configure
        # SW2: the loop must wake for these on its own - nothing else
        # guarantees a timer inside HEARTBEAT_PERIOD/RECONCILE_PERIOD (an
        # idle companion with the Command Center closed can otherwise go
        # 30s+ between wakes, right up against 094-rp5deck's hang threshold).
        t = min(t, self.heartbeat_written + HEARTBEAT_PERIOD)
        t = min(t, self.reconcile_next)
        return max(0, int((t - now) * 1000) + 1)

    def _check_mapped(self, now):
        if self.mode == HIDDEN or self.layer.can_present:
            self.unmapped_since = None
            return
        if self.unmapped_since is None:
            self.unmapped_since = now
        elif now - self.unmapped_since > REMAP_WATCHDOG:
            log.error("surface not configured %.1fs after a map request; exiting so "
                      "the supervisor starts a fresh surface", now - self.unmapped_since)
            self.stop_reason = "remap watchdog"
            self.exit_code = 4

    # -- SW2: the in-app mode watchdog (a backstop for "stuck") -------------
    def _reconcile(self, now):
        """Every RECONCILE_PERIOD, recompute mode from a fresh sway tree
        against the CURRENT placement/output and compare it to self.mode. If
        a mode event is ever missed or misapplied (a race between threads:
        see _on_watched_mode's own fix for the one already found), this
        eventually notices and corrects it - but only after seeing the SAME
        disagreement on two consecutive checks, so a single stale read can
        never fight anything already in flight. In particular it never
        fights screen_swap.HOLD_S's placement hold: a real transient foreign
        window (the ES quirk HOLD_S/this watchdog both exist because of)
        self-reverts well inside one RECONCILE_PERIOD, so it never survives
        long enough to become a second, confirming sighting.

        Skipped entirely (not even counted as a sighting, so a skip during a
        blip cannot half-arm the confirmation) while: there is no
        ModeWatcher to correct at all (cc_overlay.py, the game-screen
        overlay, has none - it has no independent HIDDEN/FULL/BAR judgement
        to second-guess); CC5's OVERLAY is up (a real window IS there, by
        design - see hidden_overlay.py); the Command Center is open or
        opening (self.pull.state != COMPANION - a touch mid-transition must
        not be yanked out from under a finger); or a rebind's configure has
        not landed yet (self.layer not configured: a transition in flight)."""
        if now < self.reconcile_next:
            return
        self.reconcile_next = now + RECONCILE_PERIOD
        if self.watcher is None or self.mode == hidden_overlay.OVERLAY:
            self.reconcile_candidate = None
            return
        if self.pull is not None and self.pull.state != PULL.COMPANION:
            self.reconcile_candidate = None
            return
        if self.layer is None or not self.layer.configured:
            self.reconcile_candidate = None
            return
        mode, reason = sway_ipc.query_mode(internal=self.output, external=self.es_output)
        if mode is None or mode == self.mode:
            self.reconcile_candidate = None
            return
        if self.reconcile_candidate != (mode, reason):
            self.reconcile_candidate = (mode, reason)
            return
        self.reconcile_candidate = None
        if now - self.reconcile_last < RECONCILE_MIN_GAP:
            self.reconcile_skips += 1
            return
        log.warning("watchdog: mode %s should be %s (%s) - corrected", self.mode, mode, reason)
        self.reconcile_last = now
        self.reconcile_corrections += 1
        self.state_dirty = True
        self.on_mode(mode, "watchdog: %s" % reason)

    # -- SW2: heartbeat (094-rp5deck's hang detector) -----------------------
    def heartbeat_path(self):
        return os.path.join(self.run_dir, "heartbeat")

    def _touch_heartbeat(self, now):
        """Touched from the main loop itself (not a thread): a stale file
        while the process is still alive means the loop is hung, not just
        busy - 094-rp5deck's hang detector kills and restarts on exactly
        that (see its own comment)."""
        if now - self.heartbeat_written < HEARTBEAT_PERIOD:
            return
        self.heartbeat_written = now
        try:
            path = self.heartbeat_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write("%.3f" % time.time())
            os.replace(tmp, path)
        except Exception:            # noqa: BLE001 - a failed heartbeat write must not crash the loop
            log.exception("could not write heartbeat")

    def _apply_configure(self):
        new = self.layer.pending_size()
        if new and new[0] and new[1] and new != (self.w, self.h):
            self._resize(*new)

    def _render(self):
        if self.cc5 is not None:
            self.cc5.render_handle()        # CC5: the corner handle's own surface
        if self.mode == HIDDEN or not self.layer.can_present:
            return
        vnew = self._upload_video_frame()       # may damage the companion (hole)
        dmg = self.ui.root.take_damage()
        if dmg is None and not vnew:
            return
        sdl = self.sdl
        if dmg is not None:
            with self.canvas.clipped(dmg):
                self.ui.root.paint(self.canvas)
            data, stride = self.canvas.pixels()
            x, y, w, h = dmg
            rect = sdl.SDL_Rect(x, y, w, h)
            sdl.UpdateTexture(self.tex, byref(rect), c_void_p(data + y * stride + x * 4), stride)
        sdl.RenderClear(self.ren)
        if self._video_visible():
            sdl.RenderTexture(self.ren, self.vtex, byref(self.video_src), byref(self.video_dst))
        sdl.RenderTexture(self.ren, self.tex, None, None)
        sdl.RenderPresent(self.ren)
        self.layer.presented()
        self.counts["presents"] += 1

    def _video_visible(self):
        return bool(self.vtex and self.video_dst is not None and self.companion is not None
                    and self.companion.view.video_on and self.ui.showing == "companion")

    def _upload_video_frame(self):
        """UI thread: copy the video worker's newest frame (if any) into the
        video's OWN XRGB8888 texture. Never renders video itself."""
        c = self.companion
        if self.video is None or c is None or c.video_path is None or \
                self.ui.showing != "companion":
            return False
        sdl = self.sdl
        with self.video.frame() as f:
            if f is None or f.seq == self.vseq:
                return False
            if self.vtex is None or self.vtex_size != (f.w, f.h):
                if self.vtex:
                    sdl.DestroyTexture(self.vtex)
                self.vtex = sdl.CreateTexture(self.ren, sdl.PIXELFORMAT_XRGB8888,
                                              sdl.TEXTUREACCESS_STREAMING, f.w, f.h)
                sdl.SetTextureBlendMode(self.vtex, sdl.BLENDMODE_NONE)
                sdl.SetTextureScaleMode(self.vtex, sdl.SCALEMODE_LINEAR)
                self.vtex_size = (f.w, f.h)
            sdl.UpdateTexture(self.vtex, None, c_void_p(f.address), f.stride)
            self.vseq = f.seq
            fw, fh = f.w, f.h
        rects = c.video_frame_ready(fw, fh)
        if rects is None:
            return False
        src, dst = rects
        self.video_src, self.video_dst = sdl.SDL_FRect(*src), sdl.SDL_FRect(*dst)
        self.counts["video_uploads"] += 1
        return True

    def on_video_frame(self):
        """Posted by the video worker: nothing to do here - the wake alone
        makes the loop run _render(), which uploads the frame."""

    def _handle(self, ev):
        sdl = self.sdl
        t = sdl.event_type(ev)
        if t == self.post.ev_type:
            return                  # drained at the top of the loop
        if t in (sdl.EV_FINGER_DOWN, sdl.EV_FINGER_MOTION, sdl.EV_FINGER_UP,
                 sdl.EV_FINGER_CANCELED):
            f = sdl.TouchFinger.from_buffer(ev)
            if self.cc5 is not None and self.cc5.handle_event(t, f.windowID, ("f", f.fingerID)):
                return                  # CC5: a touch on the corner handle's window
            pid = ("f", f.fingerID)
            x, y = f.x * self.w, f.y * self.h
            self._activity()
            if t == sdl.EV_FINGER_DOWN:
                self._down(pid, x, y, "touch")
            elif t == sdl.EV_FINGER_MOTION:
                self.router.move(pid, x, y)
            elif t == sdl.EV_FINGER_UP:
                self.router.up(pid, x, y)
            else:
                log.info("input cancel %s", pid)
                self.router.cancel(pid)
        elif t in (sdl.EV_MOUSE_DOWN, sdl.EV_MOUSE_UP):
            m = sdl.MouseButton.from_buffer(ev)
            if m.which == sdl.TOUCH_MOUSEID or m.button != sdl.BUTTON_LEFT:
                return
            if self.cc5 is not None and self.cc5.handle_event(t, m.windowID, ("m", 0)):
                return                  # CC5: the corner handle's window
            self._activity()
            if m.down:
                self._down(("m", 0), m.x, m.y, "pointer")
            else:
                self.router.up(("m", 0), m.x, m.y)
        elif t == sdl.EV_MOUSE_MOTION:
            m = sdl.MouseMotion.from_buffer(ev)
            if m.which != sdl.TOUCH_MOUSEID and m.state & sdl.BUTTON_LMASK:
                self.router.move(("m", 0), m.x, m.y)
        elif t == sdl.EV_WINDOW_EXPOSED:
            self.ui.root.damage_all()
        elif t == sdl.EV_QUIT:
            if self.stop_reason is None:
                self.stop_reason = "SDL_EVENT_QUIT"

    def _down(self, pid, x, y, kind):
        self.counts["input_down"] += 1
        w = self.router.down(pid, x, y)
        self.last_input = {"kind": kind, "x": round(x), "y": round(y), "mode": self.mode,
                           "widget": (w.name if w is not None else None), "t": time.time()}
        self.state_dirty = True

    # -- debug self-test -----------------------------------------------------
    def _debug_drag(self, v0=None, v1=0.60, dur=0.5, hz=60):
        """Push DOWN, MOTION x N, UP finger events for the volume slider into
        SDL's own queue, 1/hz apart. This exercises the real TouchFinger
        decode -> router -> slider -> throttle -> audio worker path in the
        running process. It is not system input: nothing reaches sway.
        The drag starts on the knob (v0 = the slider's value): the slider is
        relative, so a drag starting elsewhere would not end at v1."""
        s = self.ui.bar.slider
        if self.mode == HIDDEN or not s.shown() or not s.enabled:
            log.warning("DEBUG self-test drag skipped: slider not available")
            return
        if v0 is None:
            v0 = s.value
        y = (s.rect[1] + s.rect[3] / 2.0) / self.h
        n = max(1, int(dur * hz))
        sdl = self.sdl
        steps = [(sdl.EV_FINGER_DOWN, v0)]
        steps += [(sdl.EV_FINGER_MOTION, v0 + (v1 - v0) * k / n) for k in range(1, n + 1)]
        steps += [(sdl.EV_FINGER_UP, v1)]
        log.info("DEBUG self-test: injecting finger drag %.2f -> %.2f (%d moves over %.2fs) "
                 "with SDL_PushEvent (app-internal, not compositor input)", v0, v1, n, dur)
        for i, (t, v) in enumerate(steps):
            self.call_later(i / float(hz),
                            lambda t=t, v=v: self._push_finger(t, s.x_for(v) / self.w, y))

    def _push_finger(self, etype, nx, ny):
        sdl = self.sdl
        ev = sdl.SDL_Event()
        f = sdl.TouchFinger.from_buffer(ev)
        f.type = etype
        f.touchID = 1
        f.fingerID = 4242
        f.x, f.y = nx, ny
        f.pressure = 1.0 if etype != sdl.EV_FINGER_UP else 0.0
        sdl.PushEvent(byref(ev))

    def on_gesture(self, name):
        log.info("gesture %s (pull-down state %s)", name, self.pull.state)
        self.last_gesture = {"name": name, "t": time.time()}
        # YT3: the tv strip's own swipe-up (gestures.wants(), above) - this is
        # a park-and-show-home action (main.App.on_tv_home), not a pull-down
        # STATE transition: self.pull.state is already COMMAND_CENTER while
        # BAR shows the strip, so pull.handle_gesture()'s swipe_up (which
        # steps COMMAND_CENTER -> COMPANION) would be the wrong move entirely.
        if self.mode == BAR and self.ytauto is not None and self.ytauto.active and \
                name in ("swipe_up", "swipe_up_from_bottom"):
            self.on_tv_home()
        else:
            self.pull.handle_gesture(name)
        self.state_dirty = True

    # -- Navigation v2: the pull-down Command Center ------------------------
    def _activity(self):
        """Any touch while the Command Center is open restarts its
        auto-close countdown (summon.py's public rearm_timeout(), FX-D)."""
        if self.pull is not None and self.pull.is_open():
            self.pull.rearm_timeout()
        if self.cc5 is not None:
            self.cc5.activity()         # CC5: the overlay's own countdown
        if self.ytauto is not None:
            # AH: every touch that reaches main.App while BAR shows the tv
            # app IS a touch on the strip/handle - nothing else is mapped on
            # the surface in that state (bar_autohide.py's module doc).
            self.ytauto.touch()

    def on_pull(self, old, new, reason):
        log.info("PULL %s -> %s (%s)", old, new, reason)
        self.pull_log = (self.pull_log + [[old, new, reason, round(time.time(), 3)]])[-20:]
        self.router.cancel_all()        # a finger on a view that is going away is let go
        if new == PULL.COMPANION:
            self.cancel(self.hud_timer)
            self.hud_timer = None
            self.ui.close()
            self.ui.set_view("companion")
        elif new == PULL.COMMAND_CENTER:
            self.ui.close()             # opening (or leaving Settings) lands on home
            self.ui.set_view("cc")
            self._hide_osd()
        elif new == PULL.SETTINGS:
            self.ui.open("settings")
            self.ui.set_view("cc")
        self._update_active()
        self._arm_cc_tick()
        self.state_dirty = True
        if self.cc5 is not None:
            self.cc5.after_pull(old, new, reason)   # CC5: closing it over a game -> HIDDEN
        if self.tabs is not None:
            self.tabs.after_pull(old, new, reason)  # CC6: refresh the tabs

    def _pull_open(self, reason):
        # The pull tab: always a way in, even with swipe-down disabled and no
        # hardware button (else Settings would be unreachable). open() is
        # the public equivalent (FX-D): it already no-ops outside COMPANION.
        if self.mode == FULL:
            self.pull.open(reason)

    def open_command_center(self):
        self._pull_open("pull_tab")

    def close_command_center(self):
        self.pull.back_tap()

    def on_summon_button(self, ev):
        self.summon_presses += 1
        if self.cc5 is not None and self.cc5.on_summon(ev):
            self.state_dirty = True
            return                      # CC5: HIDDEN (open over the game) / OVERLAY (close)
        self.last_summon = {"binding": ev.binding, "device": ev.device_path, "t": time.time()}
        log.info("summon button %s on %s (mode %s)", ev.binding, ev.device_path, self.mode)
        if self.mode != FULL:
            return                      # nothing to show it on (the overlay takes it)
        overlay = screen_swap.read_peer_state(os.path.join(self.run_dir, "overlay",
                                                           "state.json"))
        if screen_swap.peer_open(overlay):
            return                      # this press closes the overlay's Command Center
        self.pull.summon_button()
        self.state_dirty = True

    def _arm_cc_tick(self):
        if self.cc_tick_timer is None and self.pull.is_open() and self.pull.auto_close_timeout_s:
            self.cc_tick_timer = self.call_later(1.0, self._cc_tick)

    def _cc_tick(self):
        self.cc_tick_timer = None
        if self.mode == BAR:
            # HF1: the strip is all that is on screen while Firefox / a video
            # fills the panel; the Command Center (e.g. the YouTube results)
            # must still be there when the window closes, so its countdown
            # restarts instead of closing it.
            self.pull.rearm_timeout()
        else:
            self.pull.tick()
        self._arm_cc_tick()

    def _update_active(self):
        """The companion may play video only while it is really on screen."""
        if self.companion is None or self.ui is None:
            return
        on = (self.mode == FULL and self.ui.showing == "companion"
              and not (self.layer is not None and self.layer.hidden))
        self.companion.set_active(on)

    # -- ES events ------------------------------------------------------------
    def on_es_event(self, ev):
        log.info("ES %s (%s): system=%r rom=%r name=%r", ev.kind, ev.source, ev.system,
                 ev.rom_path, ev.name)
        self.companion.on_es_event(ev)
        if ev.kind == esevents.WAKE and self.rgb is not None:
            self.rgb.lights.on_es_wake()   # RG: ROCKNIX may have just re-applied its own colour
        self.state_dirty = True

    def on_name_guard(self, result):
        import name_guard
        self.name_guard = {"ok": bool(result.get("ok")),
                           "findings": len(result.get("findings") or ()),
                           "nothing_scanned": bool(result.get("nothing_scanned"))}
        if self.companion is not None:
            self.companion.set_warning(name_guard.summary(result))
        self.state_dirty = True

    # -- volume overlay ---------------------------------------------------------
    def _osd_wanted(self, prev, m):
        if not config.get_value(self.cfg, ("audio", "show_volume_overlay")):
            return False
        if self.pull.state != PULL.COMPANION or self.mode != FULL or \
                self.ui.showing != "companion":
            return False
        ok = lambda d: d is not None and d.get("state") == "ok" and d.get("volume") is not None
        if not ok(prev) or not ok(m):
            return False                # the first read, or audio not ready: not a change
        return (round(prev["volume"], 3), prev.get("muted")) != (round(m["volume"], 3),
                                                                 m.get("muted"))

    def _show_osd(self, m):
        osd = self.companion.view.osd
        osd.set_level(m["volume"], bool(m.get("muted")))
        osd.set_visible(True)
        self.osd_shows += 1
        self.cancel(self.osd_timer)
        self.osd_timer = self.call_later(OSD_SECONDS, self._hide_osd)

    def _hide_osd(self):
        self.cancel(self.osd_timer)
        self.osd_timer = None
        if self.companion is not None:
            self.companion.view.osd.set_visible(False)

    # -- settings ---------------------------------------------------------------
    def open_settings(self):
        self.pull.open_gear()
        self._probe_charge_limit()   # YT4: show live sysfs values, not a stale cache

    def close_settings(self):
        self.pull.back_tap()

    # -- YT4: Safe charge (charge_limit.py) --------------------------------------
    def _probe_charge_limit(self):
        """On every Settings open: read the REAL kernel thresholds (never
        trust self.cfg's last-saved value alone - the owner may have run
        095-charge-limit by hand, or this may be the very first open with
        nothing saved yet) and push them into self.cfg / the sheet's rows,
        so battery.safe_charge_enabled / safe_charge_end_pct always start
        an editing session showing what the device is actually doing right
        now. If the kernel has no charge-limit sysfs at all, the section is
        shown disabled with a note instead of a control that could never do
        anything."""
        if self.charge_probe_inflight:
            return
        self.charge_probe_inflight = True
        read_live = self.charge_parts.get("read_live", charge_limit.read_live)
        self.io_worker.submit(read_live, done=self._charge_probed)

    def _charge_probed(self, live):
        self.charge_probe_inflight = False
        self.charge_available = bool(live.get("available"))
        settings = getattr(self.ui, "settings", None)
        if settings is None:
            return
        end_row = settings.rows.get(("battery", "safe_charge_end_pct"))
        enabled_row = settings.rows.get(("battery", "safe_charge_enabled"))
        if not self.charge_available:
            for row in (end_row, enabled_row):
                if row is not None:
                    row.control.set_enabled(False)
                    row.set_note("Not supported by this kernel", warn=True)
            self.state_dirty = True
            return
        end = live.get("end")
        enabled = end is not None and end < 100
        end_pct = end if enabled and end is not None else config.get_value(
            self.cfg, ("battery", "safe_charge_end_pct"))
        config.set_value(self.cfg, ("battery", "safe_charge_enabled"), enabled)
        config.set_value(self.cfg, ("battery", "safe_charge_end_pct"), end_pct)
        for row in (end_row, enabled_row):
            if row is not None:
                row.control.set_enabled(True)
                row.clear_note()
                row.refresh_from_cfg()
        self.state_dirty = True

    def _apply_charge_limit(self):
        """Both battery.* keys share this: the toggle off means "no limit"
        (end=100) regardless of whatever percentage the slider is parked
        at; the toggle on means the slider's own value. Runs off the UI
        thread (real sysfs I/O); a read-back mismatch shows an error note
        on the % row for CHARGE_TOAST_S rather than trusting the write."""
        if not self.charge_available:
            return
        enabled = config.get_value(self.cfg, ("battery", "safe_charge_enabled"))
        pct = config.get_value(self.cfg, ("battery", "safe_charge_end_pct"))
        end = pct if enabled else 100
        apply_fn = self.charge_parts.get("apply", charge_limit.apply)
        self.io_worker.submit(apply_fn, end, done=self._charge_applied)

    def _charge_applied(self, res):
        if res.get("ok"):
            return
        log.warning("charge_limit: %s", res.get("detail"))
        settings = getattr(self.ui, "settings", None)
        row = settings.rows.get(("battery", "safe_charge_end_pct")) if settings else None
        if row is not None:
            row.set_note(("Could not apply (%s)" % res.get("detail", ""))[:40])
            self.call_later(CHARGE_TOAST_S, row.clear_note)
        self.state_dirty = True

    def on_setting(self, key_path, value):
        """SettingsSheet already validated and stored the value in self.cfg
        (same dict). Save soon; apply live settings now; restart-flagged ones
        wait (the sheet shows "applies after restart")."""
        key_path = tuple(key_path)
        self.settings_changes += 1
        f = config.field_for(key_path)
        log.info("setting %s = %r%s", ".".join(key_path), value,
                 " (applies after restart)" if f and f["restart"] else "")
        self._unsaved[key_path] = value
        if key_path[0] == "screens":
            # SW1: 092 reads the file every poll - write it now, not in 0.8 s
            self.cancel(self.save_timer)
            self._save_now()
        else:
            self._schedule_save()
        if f is None or f["restart"]:
            return
        if key_path[0] in ("companion", "manuals"):
            self.companion.config_changed()
        elif key_path[0] == "appearance":
            # Any of the 4 appearance.* keys can change what the effective
            # theme is (theme_preset directly; the 3 custom_* keys only
            # while theme_preset == "custom") - palettes.apply_theme()
            # re-derives the whole 16-key THEME dict from self.cfg every
            # time rather than patching one key, so there is only one
            # code path to trust regardless of which of the 4 changed.
            palettes.apply_theme(self.cfg)
            # Device field test 25 Sep: mutating ui.THEME is not enough -
            # ui.py's Container/Label already read THEME live at DRAW
            # time, but a widget only draws again once something marks it
            # (or an ancestor) damaged (screens.py's own docstring: "the
            # main loop redraws and uploads only the union of the damaged
            # rectangles"). A settings row's own CycleButton invalidates
            # ITSELF on tap (settings_view.CycleButton.clicked()), so it
            # alone kept updating; nothing else on screen was ever told it
            # needed a redraw, so the rest of the (already-built, never
            # rebuilt) Settings sheet just kept showing whatever pixels
            # were already in the framebuffer from the last time THAT area
            # was actually repainted - observed on the device as the whole
            # Settings page freezing at one preset's colours (background,
            # tabs, other rows, Back, footer) while only the cycle button
            # itself kept changing. damage_all() is the same fix already
            # used everywhere else in this file that a change can affect
            # pixels no single invalidated widget owns (on_mode(),
            # _rebind(), _output_changed()) - mark the WHOLE surface
            # damaged so next frame repaints everything with the new THEME,
            # regardless of which widgets happened to invalidate themselves.
            if self.ui is not None:
                self.ui.root.damage_all()
        elif key_path == ("command_center", "swipe_down_enabled"):
            self.pull.swipe_down_enabled = bool(value)
        elif key_path == ("command_center", "auto_close_timeout_s"):
            self.pull.auto_close_timeout_s = int(value or 0)
            self.pull.rearm_timeout()
            self._arm_cc_tick()
        elif key_path == ("youtube", "tv_bar_hide_s"):
            if self.ytauto is not None:
                self.ytauto.timeout_s = int(value or 0)   # AH: 0 disables auto-hide
        elif key_path == ("youtube", "tv_swipe_natural"):
            if self.ytapp is not None:
                self.ytapp.swipe_natural = bool(value)   # YT4: live, no restart needed
        elif key_path in (("battery", "safe_charge_enabled"), ("battery", "safe_charge_end_pct")):
            self._apply_charge_limit()                   # YT4
        elif key_path == ("command_center", "swipe_sensitivity"):
            kw = summon.swipe_recognizer_kwargs(value)
            self.gestures.edge, self.gestures.distance = kw["edge"], kw["distance"]
        elif key_path in (hidden_overlay.KEY_CORNER_HANDLE, hidden_overlay.KEY_OVERLAY_ON_HIDDEN):
            if self.cc5 is not None:
                self.cc5.config_changed()   # CC5: read from self.cfg; the handle follows now
        # audio.show_volume_overlay is read whenever the level changes;
        # audio.volume_step_pct has no consumer yet (the hardware keys use
        # /usr/bin/volume's own step of 5).
        self.state_dirty = True

    def _schedule_save(self):
        self.cancel(self.save_timer)
        self.save_timer = self.call_later(SAVE_DELAY, self._save_now)

    def _save_now(self):
        """Writes only the keys changed since the last save, on top of the
        file as it is now (config.save_changes): the game-screen overlay is
        a second writer (its "Swap screens"), and a whole-snapshot save from
        this process's older copy would put its swap back."""
        self.save_timer = None
        changes, self._unsaved = self._unsaved, {}
        if not changes:
            return
        self.io_worker.submit(config.save_changes, changes, done=self._saved)

    def _saved(self, res):
        self.config_saves += 1
        written, notes = res
        log.info("config saved%s", (" (%s)" % "; ".join(notes)) if notes else "")
        self.state_dirty = True

    # -- modes ------------------------------------------------------------
    def geometry(self, mode):
        wl = self.wl
        if mode == BAR:
            h, zone = BAR_H, BAR_H
            if self.ytauto is not None:
                # AH: every app but the YouTube TV tile keeps the fixed
                # reserved strip unchanged (bar_autohide.wants() gates this).
                h, zone = self.ytauto.geometry(self.ui.cc.bar.app, BAR_H, BAR_H)
            return wl.ANCHOR_BOTTOM | wl.ANCHOR_LEFT | wl.ANCHOR_RIGHT, (0, h), zone
        return wl.ANCHOR_ALL, (0, 0), 0

    def _apply_bar_geometry(self):
        """AH: bar_autohide.BarAutoHide's timer/touch callbacks, and this
        class's own on_mode() below for the tv app's first BAR entry -
        geometry() there runs once BEFORE web.on_mode()/ytapp.on_mode() flip
        screens.Bar.app to "tv", so it must be recomputed and re-pushed once
        it has. Never touches show()/hide(): only set_geometry() on an
        already-mapped surface (a no-op call while HIDDEN, nothing is
        mapped)."""
        if self.mode == HIDDEN:
            return
        anchor, size, zone = self.geometry(self.mode)
        self.layer.set_geometry(anchor, size, zone)
        self.ui.root.damage_all()

    def on_mode(self, mode, reason):
        if self.cc5 is not None and self.cc5.intercept_mode(mode, reason):
            return                      # CC5: the overlay stays up over the game's window
        if mode == self.mode:
            return
        old, self.mode, self.mode_reason = self.mode, mode, reason
        log.info("MODE %s -> %s (%s)", old, mode, reason)
        self.router.cancel_all()
        if self.pull is not None:
            self.pull.mode_changed(mode)    # HIDDEN closes the Command Center
        if mode != FULL:
            self._hide_osd()
        if mode == HIDDEN:
            self.layer.hide()
        else:
            anchor, size, zone = self.geometry(mode)
            if self.cc5 is not None:
                self.cc5.apply_layer(mode)  # CC5: OVERLAY rides the overlay layer, others TOP
            if self.layer.hidden:
                self.layer.show(anchor, size, zone)
            else:
                self.layer.set_geometry(anchor, size, zone)
            self.ui.root.damage_all()
            if self.ui.sheet == "hud" and self.hud_timer is None:
                self.hud_timer = self.call_later(0, self._hud_tick)
        if self.web is not None:
            self.web.on_mode(old, mode, reason)
        if self.ytapp is not None:
            self.ytapp.on_mode(old, mode, reason)   # W2b
        if self.ytauto is not None:
            self.ytauto.on_mode(old, mode, reason)  # AH: arms/disarms the auto-hide timer
        self._update_active()       # video only in FULL, on the companion
        self.state_dirty = True
        if self.cc5 is not None:
            self.cc5.after_mode(old, mode, reason)  # CC5: the corner handle
        if self.tabs is not None:
            self.tabs.after_mode(old, mode, reason)  # CC6: a tab's follow-up, the tabs

    # -- periodic ---------------------------------------------------------
    def _clock_tick(self):
        self.ui.bar.tick_clock()
        self.companion.tick_clock()
        self.call_later(60.05 - (time.time() % 60.0), self._clock_tick)

    def _battery_tick(self):
        b = hud.read_battery_stats()
        self._apply_battery(b)
        self.call_later(BATTERY_PERIOD, self._battery_tick)

    def _rgb_tick(self):
        """RG: rgb_leds.Controller.keeper_tick() - cheap (two small sysfs
        reads) unless ROCKNIX actually stomped the sticks; never acts in
        mode "rocknix". Re-armed regardless of the previous tick's result,
        same shape as _clock_tick/_battery_tick."""
        if self.rgb is not None:
            self.rgb.lights.keeper_tick()
        self.rgb_timer = self.call_later(rgb_leds.KEEPER_PERIOD_S, self._rgb_tick)

    def _apply_battery(self, b):
        status = b.get("battery_status")
        charging = status == "Charging" or (bool(b.get("charger_online")) and status == "Full")
        self.ui.bar.battery.set_battery(b.get("battery_percent"), charging)

    # -- audio --------------------------------------------------------------
    def apply_master(self, m):
        prev, self.master = self.master, m
        if self._osd_wanted(prev, m):
            self._show_osd(m)
        if self.cc5 is not None:
            self.cc5.on_master(prev, m)     # CC5: the corner handle shows a changed level
        if self.ui.bar.slider.dragging:
            log.info("master update while dragging ignored: %s", m)
            return
        self.ui.bar.set_master(m)
        self.ui.home.sub.set_text((m or {}).get("sink_description") or "")
        self.state_dirty = True

    def on_audio_event(self, kind):
        if kind == "master":
            self._request_master()
        elif kind == "streams" and self.ui.sheet == "mixer":
            self._refresh_streams()

    # Master read-backs (RV2-M1). Each live set during a drag makes pw-mon
    # report a change; reading back per report queued one get_master (two
    # wpctl runs) per change on the single audio worker, delaying the commit
    # and landing stale values after release. Now: at most one read in
    # flight, plus an "again" flag; no read while the finger is down or
    # until the commit (or a cancel's restore) has been applied; and a read
    # that comes back in that window is dropped, never shown.
    def _master_blocked(self):
        return self.commit_pending or (self.ui is not None and self.ui.bar.slider.dragging)

    def _request_master(self):
        if self.master_inflight or self._master_blocked():
            self.master_again = True
            return
        self.master_inflight = True
        self.master_again = False
        self.audio_worker.submit(guarded(self.backend.get_master, self.faillog),
                                 done=self._master_done)

    def _master_done(self, m):
        self.master_inflight = False
        if m is FAILED:
            pass                        # logged by guarded(); the next event retries
        elif self._master_blocked():
            self.master_again = True    # stale by the time it landed: re-read later
        else:
            self.apply_master(m)
        self._master_resume()

    def _master_resume(self):
        if self.master_again and not self._master_blocked():
            self._request_master()

    def on_volume_drag(self, v):
        self.ui.bar.show_percent(v)
        due = self.vol_throttle.push(v, time.monotonic())
        if due is not None and self.vol_timer is None:
            self.vol_timer = self.call_at(due, self._vol_due)
        self.state_dirty = True

    def _vol_due(self):
        self.vol_timer = None
        due = self.vol_throttle.due(time.monotonic())
        if due is not None:
            self.vol_timer = self.call_at(due, self._vol_due)

    def _send_master(self, v):
        """A live set from the throttle. If wpctl is slower than the throttle
        interval, sets would pile up on the worker ahead of the commit, so
        at most one is queued and it sends the newest value when it runs."""
        self.last_set = v
        self.vol_live_sent = True
        with self._set_lock:
            self._set_value = v
            if self._set_queued:
                return
            self._set_queued = True
        self.audio_worker.submit(self._run_live_set)

    def _run_live_set(self):
        # worker thread
        with self._set_lock:
            v, self._set_queued = self._set_value, False
        return self.backend.set_master(v)

    def _stop_volume_drag(self):
        self.cancel(self.vol_timer)
        self.vol_timer = None
        self.vol_throttle.finish()

    def on_volume_release(self, v):
        """A real release that changed the value: commit it, once."""
        self._stop_volume_drag()
        self.vol_live_sent = False
        self.last_commit = v
        log.info("volume released at %.2f -> commit", v)
        self.ui.bar.show_percent(v)
        self.commit_pending = True      # read-backs wait for it (RV2-M1)
        self.audio_worker.submit(guarded(self.backend.commit_master, self.faillog), v,
                                 done=self._commit_done)
        self.state_dirty = True

    def on_volume_cancel(self, v0, changed=False):
        """The press ended without a commit (RV1-M1): a stroke that became a
        swipe, a view closing under the finger, shutdown, or a release that
        changed nothing. The slider is already back at v0. Nothing is
        committed; a live change that already went out is undone with one
        live set back to v0 (the persisted level was never touched)."""
        self._stop_volume_drag()
        if changed:
            self.ui.bar.show_percent(v0)
        if self.vol_live_sent:
            self.vol_live_sent = False
            self.last_set = v0
            log.info("volume drag cancelled -> restoring %.2f (live only, no commit)", v0)
            self.commit_pending = True
            self.audio_worker.submit(guarded(self.backend.set_master, self.faillog), v0,
                                     done=self._commit_done)
        else:
            if changed:
                log.info("volume drag cancelled before any change was sent")
            self._master_resume()
        self.state_dirty = True

    def _commit_done(self, ok):
        self.commit_pending = False
        if ok is not FAILED and not ok:
            log.warning("audio setter reported failure")
        if not self.backend.dryrun:
            self._request_master()
        else:
            # In dry-run nothing changed, so a re-read would just undo the UI;
            # only a change reported meanwhile is worth reading.
            self._master_resume()

    def on_mute_toggle(self, state):
        log.info("mute toggled -> %s", state)
        self.audio_worker.submit(guarded(self.backend.toggle_master_mute, self.faillog),
                                 done=self._after_setter)
        self.state_dirty = True

    def _after_setter(self, ok):
        if ok is not FAILED and not ok:
            log.warning("audio setter reported failure")
        if not self.backend.dryrun:
            # In dry-run nothing changed, so a re-read would just undo the UI.
            self._request_master()

    def _stream_throttle(self, sid):
        th = self.stream_throttles.get(sid)
        if th is None:
            def send(v, sid=sid):
                self.stream_live_sent.add(sid)
                self.audio_worker.submit(self.backend.set_stream_volume, sid, v)
            th = audioctl.Throttle(1.0 / VOLUME_HZ, send)
            self.stream_throttles[sid] = th
        return th

    def on_stream_drag(self, sid, v):
        th = self._stream_throttle(sid)
        due = th.push(v, time.monotonic())
        if due is not None and self.stream_timers.get(sid) is None:
            def fire(sid=sid):
                self.stream_timers[sid] = None
                nxt = self._stream_throttle(sid).due(time.monotonic())
                if nxt is not None:
                    self.stream_timers[sid] = self.call_at(nxt, fire)
            self.stream_timers[sid] = self.call_at(due, fire)

    def on_stream_release(self, sid, v):
        self.cancel(self.stream_timers.pop(sid, None))
        self._stream_throttle(sid).finish()
        self.stream_live_sent.discard(sid)
        self.audio_worker.submit(self.backend.set_stream_volume, sid, v)
        if self.mixer_again:
            self._refresh_streams()

    def on_stream_cancel(self, sid, v0, changed=False):
        """Like on_volume_cancel for a mixer row: undo a live change, commit
        nothing (a stream's volume has no separate persisted value, so the
        restore IS the whole undo)."""
        self.cancel(self.stream_timers.pop(sid, None))
        self._stream_throttle(sid).finish()
        if sid in self.stream_live_sent:
            self.stream_live_sent.discard(sid)
            log.info("stream %s drag cancelled -> restoring %.2f", sid, v0)
            self.audio_worker.submit(self.backend.set_stream_volume, sid, v0)
        if self.mixer_again:
            self._refresh_streams()

    def on_stream_mute(self, sid, state):
        self.audio_worker.submit(self.backend.toggle_stream_mute, sid)

    def _refresh_streams(self):
        if self.mixer_inflight or self.ui.mixer.dragging():
            self.mixer_again = True
            return
        self.mixer_inflight = True
        self.mixer_again = False
        self.audio_worker.submit(guarded(self.backend.list_streams, self.faillog),
                                 done=self._streams_done)

    def _streams_done(self, streams):
        self.mixer_inflight = False
        if streams is FAILED:
            streams = None              # shown as "could not read"; the next event retries
        if self.ui.sheet == "mixer":
            self.ui.mixer.set_streams(streams)
            self.state_dirty = True
            if self.mixer_again:
                self._refresh_streams()

    # -- navigation --------------------------------------------------------
    def on_battery(self):
        if self.ui.compact:
            # BAR mode has no HUD, by design: the surface is a 140 px strip
            # with an exclusive zone, and the rest of DSI-1 belongs to the
            # rp5deck window above it (Firefox / YouTube). Showing the HUD
            # would mean re-anchoring the layer surface full-panel over that
            # window and back, re-flowing it twice - a change to B1's verified
            # geometry for a sheet that is one close-the-app away in FULL.
            log.info("battery tapped in BAR mode: no HUD in BAR (see on_battery)")
            return
        self.open_hud()

    def open_hud(self):
        self.ui.open("hud")
        self.cancel(self.hud_timer)
        self.hud_timer = self.call_later(0, self._hud_tick)
        log.info("screen -> hud")
        self.state_dirty = True

    def open_mixer(self):
        self.ui.open("mixer")
        self.ui.mixer.streams = "loading"
        self.ui.mixer.msg.set_text("Reading audio streams…")
        self.ui.mixer.msg2.set_text("")
        self._refresh_streams()
        log.info("screen -> mixer")
        self.state_dirty = True

    # -- CC7: Hotkey cheat sheet ------------------------------------------------
    def open_hotkeys(self):
        info = getattr(self.companion, "info", None)
        running_system = info.get("system") if info and info.get("running") else None
        contexts, order = hotkeys.load(running_system=running_system)
        self.ui.hotkeys.set_data(contexts, order)
        self.ui.open("hotkeys")
        log.info("screen -> hotkeys (running_system=%r)", running_system)
        self.state_dirty = True

    # -- CC1: Clean state (cleanstate_view / cleanstate) ------------------------
    def open_clean_state(self):
        """The tile: check what runs + ES's health, then the confirm sheet.
        Every stop / restart is cleanstate.Helper's, behind that confirm."""
        log.info("tile -> Clean state")
        self.clean.open()
        self.state_dirty = True

    # -- DS: dual-screen settings guard (dualscreen_keys_view.py) --------------
    def on_dualscreen_restore(self):
        """The Home banner's own Restore button (never a tile): opens the
        confirm sheet naming the missing keys - dualscreen_keys.restore()
        only ever runs after that confirmation."""
        log.info("banner -> dual-screen settings")
        if os.environ.get("RP5DECK_DEMO_NOTICE"):
            log.info("demo notice: Restore does nothing")
            return
        self.dualscreen.open()

    def _ds_tick(self):
        if self._demo_notice("dualscreen"):
            pass
        elif self.dualscreen is not None:
            self.dualscreen.poll()
        self.ds_timer = self.call_later(DS_POLL_S, self._ds_tick)

    # -- DEMO: guide screenshots of the two banners ---------------------------
    # RP5DECK_DEMO_NOTICE=charge|dualscreen (in $RP5DECK_HOME/env, 094 restart)
    # shows that banner with sample text instead of polling, so the guide can
    # photograph it on the real screen without breaking system.cfg or the
    # charger. Its button does nothing in demo mode. Unset = normal polling.
    DEMO_NOTICE_TEXT = {
        "charge": charge_stuck_view.MESSAGE,
        "dualscreen": "Dual-screen settings missing: 3ds.screen_layout, wiiu.gamepad_enabled",
    }

    def _demo_notice(self, which):
        """True if RP5DECK_DEMO_NOTICE names `which`; then (re)shows its
        sample text on Home instead of the real poll."""
        if os.environ.get("RP5DECK_DEMO_NOTICE") != which:
            return False
        home = getattr(getattr(self, "ui", None), "home", None)
        if home is not None:
            getattr(home, "set_%s_notice" % which)(self.DEMO_NOTICE_TEXT[which])
        return True

    # -- CHG: "charger connected but not charging" (charge_stuck_view.py) ------
    def on_charge_notice_dismiss(self):
        """The Home banner's own Dismiss button: hides it until the charger
        is next unplugged, even if the underlying condition has not
        actually cleared yet (charge_stuck_view.ChargeStuckController's own
        re-arm rule)."""
        log.info("banner -> charger not charging (dismissed)")
        if os.environ.get("RP5DECK_DEMO_NOTICE"):
            log.info("demo notice: Dismiss does nothing")
            return
        self.charge_stuck.dismiss()

    def _charge_stuck_tick(self):
        if self._demo_notice("charge"):
            pass
        elif self.charge_stuck is not None:
            self.charge_stuck.poll()
        self.charge_stuck_timer = self.call_later(CHARGE_STUCK_POLL_S, self._charge_stuck_tick)

    # -- RG: Stick lights (rgb_leds.py / rgb_view.py) ---------------------------
    def open_lights(self):
        log.info("tile -> Stick lights")
        self.rgb.open()

    # -- Appearance (palettes.py / appearance_view.py) ---------------------------
    def close_appearance(self):
        """Back from the Custom colours editor returns to Settings (it was
        opened FROM there, via the Appearance page's own button) rather
        than Home - unlike every other add_sheet() sheet in this file,
        which all open from a Home tile and so close back to Home via
        close_sheet(). settings.rows holds every FieldRow by key_path
        (settings_view.py's own coverage contract) - refresh the "Colour
        theme" row here because AppearanceController.open() may have just
        flipped appearance.theme_preset to "custom" behind Settings' back,
        and SettingsSheet itself is never rebuilt to notice on its own."""
        self.ui.open("settings")
        row = self.ui.settings.rows.get(("appearance", "theme_preset"))
        if row is not None:
            row.refresh_from_cfg()
        self.state_dirty = True

    def reset_appearance(self):
        """Settings > Appearance > "Reset to default": every appearance.*
        key back to its own schema default, applied live and persisted
        exactly like any other setting change (on_setting()) - not a
        special-cased path of its own."""
        for path in (("appearance", "theme_preset"), ("appearance", "custom_accent"),
                    ("appearance", "custom_bg"), ("appearance", "custom_text")):
            default = config.field_for(path)["default"]
            if config.set_value(self.cfg, path, default):
                self.on_setting(path, default)
        row = self.ui.settings.rows.get(("appearance", "theme_preset"))
        if row is not None:
            row.refresh_from_cfg()

    # -- Keyboard (rocknix_keyboard.py) ------------------------------------
    def toggle_keyboard(self):
        """An immediate toggle, not a sheet: signal ROCKNIX's own
        touchkeyboard.service wvkbd-mobintl directly (rocknix_keyboard.
        toggle() - it never starts/stops the process, only the same SIGRTMIN
        input_sense already sends). On success close the Command Center so
        the keys reach the game/ES underneath, same as the Close button
        (close_command_center() -> self.pull.back_tap()). If nothing is
        running (the ES setting is off), leave the Command Center open and
        show a hint instead - same 2.5 s auto-clear app_tabs.py's own
        bar.show_hint()/clear_hint() pair uses."""
        log.info("tile -> Keyboard")
        if rocknix_keyboard.toggle():
            self.close_command_center()
        elif self.ui.bar.show_hint("Turn on System Settings > Enable Touchscreen Keyboard in ES"):
            self.call_later(2.5, self.ui.bar.clear_hint)

    # -- YT4: Sleep tile (system_sleep.py) -----------------------------------
    def open_sleep(self):
        """Owner-approved: a brief "Sleeping..." hint (state_dirty below
        gets it painted), then `systemctl suspend` off the UI thread after
        SLEEP_DELAY_S so that paint has a real chance to land before the
        panel/whole device freezes for the suspend call. Guarded against a
        double tap: a second tap while sleep_pending is already set (still
        in the delay, or the suspend call itself is in flight/blocked for
        the whole sleep duration) is a no-op."""
        if self.sleep_pending:
            return
        self.sleep_pending = True
        log.info("tile -> Sleep")
        self.ui.bar.show_hint("Sleeping…")
        self.cancel(self.sleep_timer)
        self.sleep_timer = self.call_later(SLEEP_DELAY_S, self._sleep_now)
        self.state_dirty = True

    def _sleep_now(self):
        self.sleep_timer = None
        suspend = self.sleep_parts.get("suspend", system_sleep.suspend)
        self.io_worker.submit(suspend, done=self._slept)

    def _slept(self, res):
        self.sleep_pending = False
        ok, detail = res
        self.ui.bar.clear_hint()
        if not ok:
            log.warning("sleep: %s", detail)
            if self.ui.bar.show_hint(("Could not sleep: %s" % detail)[:60]):
                self.call_later(2.5, self.ui.bar.clear_hint)
        self.state_dirty = True

    # -- Tile customisation (screens.Home "Edit tiles" mode) -----------------
    def on_tile_layout_changed(self, order, hidden):
        """screens.Home calls this after every committed change (a drag
        drop, a hide/show tap, Reset tile layout, or Done) - order is
        always a full permutation of config.HOME_TILE_KEYS, hidden a
        subset never containing "home.settings" (Home's own guard, backed
        by config.py's schema refusing to store it either way). Persists
        immediately, the same one-commit-at-a-time model settings_view.py
        uses, not just on Done, so a crash mid-session cannot lose it."""
        changes = {}
        if config.set_value(self.cfg, ("command_center", "tile_order"), order):
            changes[("command_center", "tile_order")] = order
        if config.set_value(self.cfg, ("command_center", "hidden_tiles"), hidden):
            changes[("command_center", "hidden_tiles")] = hidden
        if changes:
            config.save_changes(changes)
        self.state_dirty = True

    # -- HF1: Browser / Discord (web_tiles.WebApps) ---------------------------
    def open_browser(self):
        log.info("tile -> Browser")
        self._web_tile("Browser", None)
        self.state_dirty = True

    def open_discord(self):
        log.info("tile -> Discord")
        self._web_tile("Discord", browser.DISCORD_URL)
        self.state_dirty = True

    def _web_tile(self, label, url):
        """CC6: an emulator window on this screen is parked first (else
        Firefox would tile next to it and the panel go HIDDEN) and a parked
        Firefox comes back; then HF1's open_web, unchanged."""
        if self.tabs is None:
            self.web.open_web(label, url)
            return
        self.tabs.run(app_tabs.wsw.WEB, lambda: self.web.open_web(label, url), "tile " + label)

    def open_ytapp(self):
        """W2b: the YouTube TV tile's own Home tile (patches/W2b-screens.patch) -
        same "park whatever else is shown on this screen first" pattern
        _web_tile() already uses for Browser/Discord, its own window kind."""
        log.info("tile -> YouTube App")
        if self.tabs is None:
            self.ytapp.open()
        else:
            self.tabs.run(app_tabs.wsw.YTAPP, self.ytapp.open, "tile YouTube App")
        self.state_dirty = True

    def on_app_action(self, name):
        """Every BAR-strip app control and the launch sheet's button."""
        if name == "tv.close":
            self.on_tv_close()
        elif name == "tv.home":
            self.on_tv_home()
        elif name.startswith("tv.") and self.ytapp is not None:
            self.ytapp.action(name)             # W2b: the D-pad, its own session
        else:
            self.web.action(name)
        self.state_dirty = True

    # -- YT3: the tv strip's Close / Home (owner feedback: no way to kill the
    # YouTube App or tab out of it) ------------------------------------------
    def on_tv_close(self):
        """Ends the YouTube App session outright: YtAppSession.close() asks
        its Firefox to quit (Marionette:Quit, falling back to SIGTERM); once
        its window actually leaves DSI-1, sway reports FULL again and
        rp5deck's normal Command Center follows - the same "nothing here
        forces the mode itself" pattern web_tiles.WebApps' own Close already
        uses for Browser/Discord (main.App.on_mode drives the transition,
        not this handler)."""
        log.info("tv strip -> Close")
        if self.ytapp is not None:
            self.ytapp.close()
        if self.ytauto is not None:
            self.ytauto.stop()          # cancel the auto-hide timer right away

    def on_tv_home(self):
        """Parks the YouTube App (same park-not-close app_tabs.py already
        does for Browser/Discord - the session and its Firefox stay alive)
        and opens the Command Center's home directly - a single, obvious tap,
        not the multi-app Tabs picker (show_app_tabs, below) the owner never
        recognised. The tab strip still lists the YouTube App afterwards
        (app_tabs.build_tabs), so it is easy to get back to."""
        log.info("tv strip -> Home")
        if self.tabs is not None:
            self.tabs.select(app_tabs.CC)
        elif self.pull is not None:
            self.pull.open("tv_home")
        if self.ytauto is not None:
            self.ytauto.stop()          # cancel the auto-hide timer right away

    def show_app_tabs(self):
        """CC6: the BAR strip's Tabs button - park this app's window (it keeps
        running) and open the Command Center with the tab strip."""
        log.info("bar -> Tabs")
        if self.tabs is not None:
            self.tabs.select(app_tabs.CC)
        self.state_dirty = True

    # -- manual viewer (companion) ---------------------------------------------
    def open_manual(self):
        self.companion.open_manual()
        self._update_active()
        self.state_dirty = True

    def close_manual(self):
        self.companion.close_manual()
        self.state_dirty = True

    def manual_page(self, delta):
        self.companion.manual_page(delta)
        self.state_dirty = True

    def close_sheet(self):
        log.info("screen %s -> home", self.ui.screen)
        self.cancel(self.hud_timer)
        self.hud_timer = None
        self.ui.close()
        self.state_dirty = True

    def _hud_tick(self):
        self.hud_timer = None
        if self.ui.sheet != "hud" or self.mode == HIDDEN:
            return
        if not self.hud_inflight:
            self.hud_inflight = True
            self.hud_worker.submit(guarded(hud.sample, self.faillog), done=self._hud_done)
        self.hud_timer = self.call_later(HUD_PERIOD, self._hud_tick)

    def _hud_done(self, s):
        self.hud_inflight = False
        if s is FAILED:
            return                      # logged (rate-limited); the next tick retries
        running = self.companion.running if self.companion is not None else None
        if running is not None and not s.get("running_game"):
            # hud.read_running_game() never reports a running game (it only
            # looks for a "msg" key, which ES sends only when NOTHING runs);
            # the companion knows, from the hooks / the /runningGame poll.
            s = dict(s, running_game=running.name or os.path.basename(running.rom_path))
        self.hud_last = s
        self.ui.hud.set_sample(s)
        self._apply_battery(s)
        self.state_dirty = True

    # -- state dump ---------------------------------------------------------
    def _write_state_if_due(self, now):
        if self.state_dirty and now - self.state_written >= 0.1:
            self.write_state()
            self.state_written = now

    def state(self, running=True):
        s = self.hud_last or {}
        slider = self.ui.bar.slider
        return {
            "version": VERSION,
            "pid": os.getpid(),
            "running": running,
            "stop_reason": self.stop_reason,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "t": round(time.time(), 3),
            "uptime_s": round(time.time() - self.started, 1),
            "title": self.ui.title,
            "config": {"note": self.cfg_note, "output": self.output,
                       "es_output": self.es_output},
            "screens": {"role": self.ROLE, "output": self.output, "es_output": self.es_output,
                        "placement": list(self.placement) if self.placement else None,
                        "forced": self.output_forced, "rebinds": self.rebinds,
                        "setting": config.get_value(self.cfg, ES_SCREEN_KEY)},
            "last_gesture": self.last_gesture,
            "view": self.ui.showing,
            "pulldown": {"state": self.pull.state if self.pull else None,
                         "log": self.pull_log[-6:],
                         "swipe_down_enabled": self.pull.swipe_down_enabled if self.pull else None,
                         "auto_close_s": self.pull.auto_close_timeout_s if self.pull else None},
            "summon": {"binding": config.get_value(self.cfg, ("command_center",
                                                              "hardware_button")),
                       "reader": bool(self.summon_reader), "presses": self.summon_presses,
                       "last": self.last_summon},
            "cc5": self.cc5.state() if self.cc5 is not None else None,
            "companion": self.companion.state() if self.companion else None,
            "video": {"worker_state": getattr(self.video, "state", None),
                      "error": getattr(self.video, "error", None),
                      "frames_rendered": getattr(self.video, "frames", None),
                      "players_created": getattr(self.video, "players_created", None),
                      "players_closed": getattr(self.video, "players_closed", None),
                      "uploads": self.counts.get("video_uploads", 0),
                      "texture": list(self.vtex_size) if self.vtex_size else None,
                      "drawn": self._video_visible() if self.ui else False},
            "osd": {"visible": bool(self.companion and self.companion.view.osd.visible),
                    "shows": self.osd_shows},
            "es_events": {"path": getattr(self.es_watcher, "path", None),
                          "watch": getattr(self.es_watcher, "mode", None),
                          "seen": getattr(self.es_watcher, "events_seen", 0)},
            "name_guard": self.name_guard,
            "settings": {"changes": self.settings_changes, "saves": self.config_saves},
            "mode": self.mode,
            "mode_reason": self.mode_reason,
            "surface": {
                "size": [self.w, self.h],
                "configured": bool(self.layer and self.layer.configured),
                "hidden": bool(self.layer and self.layer.hidden),
                "can_present": bool(self.layer and self.layer.can_present),
                "configures": self.layer.configure_count if self.layer else 0,
                "geometry": list(self.layer.geometry[:1]) + [list(self.layer.geometry[1]),
                                                             self.layer.geometry[2]]
                if self.layer else None,
            },
            "counts": dict(self.counts),
            "screen": self.ui.screen,
            "sheet": self.ui.sheet,
            "volume": {
                "master": self.master,
                "ui_percent": int(round(slider.value * 100)),
                "readout": self.ui.bar.readout.text,
                "dragging": slider.dragging,
                "ui_muted": self.ui.bar.mute.state,
                "last_set": self.last_set,
                "last_commit": self.last_commit,
                "read_inflight": self.master_inflight,
                "commit_pending": self.commit_pending,
            },
            "audio_dryrun": self.backend.dryrun,
            "tabs": self.tabs.state() if self.tabs is not None else None,     # CC6
            "ytapp": self.ytapp.state() if self.ytapp is not None else None,      # W2b
            "audio_calls": [list(c) for c in list(self.backend.calls)[-30:]],
            "hud": {
                "open": self.ui.sheet == "hud",
                "samples": self.ui.hud.samples,
                "last": {k: s.get(k) for k in ("battery_percent", "battery_power_w",
                                               "gpu_mhz", "gpu_load_percent", "uptime_s",
                                               "ram_available_mb")} if s else None,
                "cpu_mhz": [c.get("cur_mhz") for c in (s.get("cpu_clusters") or [])] if s else None,
            },
            "mixer": {"streams": (self.ui.mixer.streams if not isinstance(self.ui.mixer.streams, list)
                                  else len(self.ui.mixer.streams)),
                      "message": self.ui.mixer.msg.text},
            "apps": self.web.state() if self.web else None,
            "clean_state": self.clean.state() if self.clean else None,
            "lights": self.rgb.lights.state.to_dict() if self.rgb else None,   # RG
            "last_input": self.last_input,
            "sway_events_seen": self.watcher.events_seen if self.watcher else 0,
            "targets": self.ui.targets() if self.mode != HIDDEN else {},
        }

    def write_state(self, running=True):
        self.state_dirty = False
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            path = os.path.join(self.run_dir, "state.json")
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state(running), f, indent=1, default=str)
            os.replace(tmp, path)
        except Exception:           # noqa: BLE001
            log.exception("could not write state.json")

    # -- shutdown -------------------------------------------------------------
    def shutdown(self):
        # A drag in progress is cancelled: its slider goes back to the value
        # from before the press, a live volume change is undone (queued on
        # the audio worker, which drains before it stops), nothing is
        # committed (RV1-M1: only a real release commits).
        try:
            if getattr(self, "router", None):
                self.router.cancel_all()
        except Exception:           # noqa: BLE001
            log.exception("cancel_all at shutdown")
        # A setting changed less than SAVE_DELAY ago is saved now, before the
        # io worker stops (RV2-m3).
        try:
            if self.save_timer is not None and not self.save_timer[3]:
                self.cancel(self.save_timer)
                log.info("saving a pending settings change before exit")
                self._save_now()
        except Exception:           # noqa: BLE001
            log.exception("flushing the pending settings save")
        for name, stop in (("sway watcher", lambda: self.watcher and self.watcher.stop()),
                           ("screen watcher",
                            lambda: self.screen_watcher and self.screen_watcher.stop()),
                           ("audio subscriber", lambda: self.subscriber and self.subscriber.stop()),
                           ("es events", lambda: self.es_watcher and self.es_watcher.stop()),
                           ("summon reader", self._stop_summon),
                           ("web apps", lambda: self.web and self.web.shutdown()),
                           ("video", lambda: self.video and self.video.shutdown()),
                           ("audio worker", lambda: self.audio_worker and self.audio_worker.stop()),
                           ("media worker", lambda: self.media_worker and self.media_worker.stop(1.0)),
                           ("io worker", lambda: self.io_worker and self.io_worker.stop(2.0)),
                           ("web worker", lambda: self.web_worker and self.web_worker.stop(1.0)),
                           ("search worker",
                            lambda: self.search_worker and self.search_worker.stop(0.5)),
                           ("clean worker",
                            lambda: self.clean_worker and self.clean_worker.stop(0.5)),
                           ("hud worker", lambda: self.hud_worker and self.hud_worker.stop(1.0))):
            try:
                stop()
            except Exception:       # noqa: BLE001
                log.exception("stopping %s", name)
        try:
            if getattr(self, "ui", None) is not None:
                self.write_state(running=False)
        except Exception:           # noqa: BLE001
            log.exception("final state")
        sdl = self.sdl
        if self.cc5 is not None:
            try:
                self.cc5.shutdown()         # CC5: the corner handle's surface and window
            except Exception:       # noqa: BLE001
                log.exception("corner handle shutdown")
        # Order matters: the layer role goes before SDL destroys the wl_surface.
        if self.layer:
            try:
                self.layer.destroy()
            except Exception:       # noqa: BLE001
                log.exception("layer destroy")
        if sdl:
            if self.vtex:
                sdl.DestroyTexture(self.vtex)
            if self.tex:
                sdl.DestroyTexture(self.tex)
            if self.canvas:
                self.canvas.free()
            if self.ren:
                sdl.DestroyRenderer(self.ren)
            if self.win:
                sdl.DestroyWindow(self.win)
            self.post.ev_type = None
            sdl.Quit()
        log.info("clean exit")


class StartupError(Exception):
    pass


def main(argv=None, app_cls=None, log_name="rp5deck.log",
         description="rp5deck bottom-screen companion"):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="exit cleanly after N seconds (testing); 0 = run until signalled")
    ap.add_argument("--output", default=None,
                    help="screen to bind to for this run (overrides config.json 'output')")
    args = ap.parse_args(argv)
    path = setup_logging(log_name)
    log.info("rp5deck %s starting: pid %d, python %s, log %s", VERSION, os.getpid(),
             sys.version.split()[0], path)
    app = (app_cls or App)(args)
    try:
        rc = app.run()
    except StartupError as e:
        log.error("startup failed: %s", e)
        rc = 2
    except Exception:               # noqa: BLE001
        log.exception("crashed")
        rc = 1
    finally:
        app.shutdown()
    log.info("exit code %d", rc)
    logging.shutdown()
    return rc


if __name__ == "__main__":
    sys.exit(main())
