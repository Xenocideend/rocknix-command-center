#!/usr/bin/env python3
"""rp5deck, the touch companion app for the Retroid Pocket 5's bottom screen.

    python3 main.py [--seconds N] [--output NAME]

Settings are one JSON file (config.py, default /storage/rp5deck/config.json) and the
Settings sheet edits it live. --output pins the screen for one run.

The default view is the companion (art or video of the game picked in ES, or the one
running). The Command Center pulls down over it with a swipe from the top edge, the pull
tab or the hardware button, and closes with a swipe up, Close/Back, the button again or
its timeout. Master volume is its first row.

Its a layer-shell surface on DSI-1 with keyboard interactivity NONE, so a tap never
steals focus from ES or the game up top. Modes come from sway: FULL (the whole panel),
BAR (a bottom strip while an rp5deck-* window uses the rest), HIDDEN (something else owns
the panel, or its undocked) and OVERLAY (hidden_overlay.py, the same surface over an
emulator's second window or undocked, on the overlay layer, until its closed or times out).

Environment:
  RP5DECK_AUDIO_DRYRUN=1   audio setters log instead of running (readers stay real)
  RP5DECK_LOG_DIR          log directory (default /storage/rp5deck/log, 512 KB x 2)
  RP5DECK_RUN_DIR          where state.json goes (default /run/rp5deck)
  RP5DECK_STDERR=1         also log to stderr
  RP5DECK_DEBUG=1          SIGUSR1 fakes a finger drag on the volume slider (testing only,
                           sway's seat cursor gives no motion during a press)
  RP5DECK_ES_SPOOL         the ES hooks' event spool dir (default /var/run/rp5deck/es-events)
  RP5DECK_OSK              Firefox's on-screen keyboard: auto | button | off

Exit codes: 0 clean, 1 crash, 2 startup failure, 3 the compositor closed the surface,
4 the surface couldnt be (re)mapped. command-center-app restarts on any of them.
"""
import argparse
import heapq
import json
import logging
import logging.handlers
import os
import subprocess
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
import steam_view  # noqa: E402
import companion  # noqa: E402
import config     # noqa: E402
import device     # noqa: E402
import screen_map  # noqa: E402
import dualscreen_keys_view  # noqa: E402  (DS)
import esevents   # noqa: E402
import hidden_overlay  # noqa: E402  (CC5)
import hotkeys     # noqa: E402  (CC7)
import hotkeys_view  # noqa: E402  (CC7)
import hud        # noqa: E402
import rgb_leds   # noqa: E402  (RG)
import rgb_view   # noqa: E402  (RG)
import rocknix_keyboard  # noqa: E402
import brightness  # noqa: E402  (batch 1: both screens' brightness)
import screen_idle  # noqa: E402  (14c: the panel follows ES's screensaver)
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
import button_colours  # noqa: E402  (batch 2: ES button prompt colours)
import screen_presets  # noqa: E402  (batch 2: layout size for the device's panel)
import version  # noqa: E402  (the version number and patch notes)
from sway_ipc import BAR, FULL, HIDDEN   # noqa: E402

VERSION = version.label()  # the number and release date, from version.py
BAR_H = screens.BAR_H
VOLUME_HZ = 20.0
HUD_PERIOD = 1.0
BATTERY_PERIOD = 30.0
REMAP_WATCHDOG = 3.0
OSD_SECONDS = 1.5
SAVE_DELAY = 0.8
ES_POLL_PERIOD = 3.0
SLEEP_DELAY_S = 1.0  # lets the Sleep tap's release paint first
CHARGE_TOAST_S = 2.5  # how long a charge limit error stays up
# how often the dual-screen settings guard looks at system.cfg (it only warns, never writes)
DS_POLL_S = 300.0
# how often the "charger in but not charging" guard checks (a few sysfs reads)
CHARGE_STUCK_POLL_S = 10.0
# dual-screen-layout-and-power writes this when the add-on stopped answering and needs a replug
ADDON_REPLUG_FLAG = "/run/rp5deck-addon-replug"
ADDON_REPLUG_POLL_S = 5.0
ADDON_REPLUG_TEXT = "The Dual Screen add-on isnt answering. Unplug it and plug it back in."
# The mode watchdog (_reconcile) only fixes a mode after the same disagreement shows up on
# two checks in a row, so one stale read cant fight a change thats already happening.
RECONCILE_PERIOD = 5.0
RECONCILE_CONFIRM = 2
RECONCILE_MIN_GAP = 10.0  # at most one correction this often
# command-center-app kills a child whose heartbeat gets this stale, we touch it way more
# often than that
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
    """Hands work from worker threads to the main thread and wakes SDL_WaitEventTimeout with a
    user event. Only one wake event is ever waiting.
    """

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
    """Logs an exception with its traceback the first time a key fails, then at most once per
    `interval` with a count of repeats, so something failing every tick cant flood the log.
    Safe from worker threads.
    """

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
    """What a guarded() call posts when it raised. `done` callbacks have to clear their
    in-flight flags on it, and it can never be mistaken for a real reading.
    """

    def __repr__(self):
        return "FAILED"


FAILED = _Failed()


def own_cgroup(path="/proc/self/cgroup"):
    """This process's cgroup lines ("" if unreadable), to refuse an ES restart from inside essway."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def guarded(fn, limiter):
    """Wraps fn so an exception gets logged (rate limited) and turns into FAILED, and `done`
    still runs.
    """
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
    """One background thread running calls in order, results posted back to the main thread."""

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
    ROLE = "panel"  # the game-screen overlay says "overlay"

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
        # volume read-backs: one get_master in flight at most, and none while a finger is on the
        # slider or a commit is pending
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
        self.surface = (0, 0)       # the real surface; self.w/h is the layout canvas
        self._pixels_warned = False  # one-time warning when canvas.pixels() has no data
        self.sdl = self.win = self.ren = self.tex = self.canvas = None
        self.layer = None
        self.watcher = None
        self.subscriber = None
        self.audio_worker = self.hud_worker = None
        self._sig_r = self._sig_w = None
        self.debug = os.environ.get("RP5DECK_DEBUG") == "1"
        self.cfg, self.cfg_note = config.load()
        # Load the saved theme into ui.THEME before any widget is built so the first frame
        # already has the right colours.
        palettes.apply_theme(self.cfg)
        if args.output:
            self.cfg["output"] = args.output
        self.output = self.cfg["output"]          # the screen the Command Center lives on
        self.es_output = self.cfg["es_output"]    # the screen ES / games live on
        # --output pins the surface for this run, otherwise it follows ES
        self.output_forced = bool(args.output)
        self.placement = None           # screen_swap.Placement, last applied
        self.screen_watcher = None
        self.rebinds = 0
        # the mode watchdog (see _reconcile)
        self.reconcile_candidate = None     # (mode, reason) seen once, awaiting confirmation
        self.reconcile_last = 0.0           # monotonic time of the last correction applied
        self.reconcile_next = 0.0           # monotonic time of the next scheduled check
        self.reconcile_corrections = 0
        self.reconcile_skips = 0
        self.heartbeat_written = 0.0
        self._unsaved = {}              # key_path -> value not yet written (save_changes)
        self.last_gesture = None
        # navigation
        self.ui = self.router = self.gestures = None
        self.pull = None                # summon.PullDownStateMachine
        self.companion = None           # companion.CompanionController
        self.screen_idle = None  # screen_idle.ScreenIdle
        self.bottom_bl = brightness.BottomBacklight()
        self.top_dim = brightness.TopDim(screen_map.CURRENT.top)
        self.video = None               # companion.VideoWorker
        self.media_worker = self.io_worker = None
        self.es_watcher = None
        self.name_guard = None  # name_guard summary once its scanned
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
        # Browser / Discord
        self.web = None                 # web_tiles.WebApps
        self.web_worker = self.search_worker = None
        self.web_parts = {}             # tests inject fakes (browser, player, keyboard, ...)
        # The YouTube TV tile has its own Firefox (profile, app_id, Marionette port), never
        # web_tiles.WebApps.
        self.ytapp = None                # web_tiles.YtAppSession
        self.ytapp_parts = {}            # tests inject a fake browser
        # the YouTube TV strip hides itself, the only BAR app that doesnt keep the fixed strip
        self.ytauto = None               # bar_autohide.BarAutoHide
        # the Clean state tile
        self.clean = None               # cleanstate_view.CleanStateController
        self.steam_view = None          # steam_view.SteamLibraryController
        self.clean_worker = None        # discover (~5 s) / execute run here, never on the UI
        self.clean_parts = {}           # tests inject helper_factory
        # the Command Center over an emulator's second screen
        self.cc5 = None                 # hidden_overlay.OverlayController
        self.cc5_parts = {}             # tests inject query / handle_factory / peer_state
        # the app tabs (panel only)
        self.tabs = None
        self.tabs_parts = {}            # tests inject switcher / avail / submit
        # stick lights (rgb_leds.py does the device, rgb_view.py the sheet)
        self.rgb = None                 # rgb_view.RGBController
        self.rgb_timer = None  # keeper_tick() poll
        # the Sleep tile (guards a double tap and the ~1 s paint delay)
        self.sleep_pending = False
        self.sleep_timer = None
        self.sleep_parts = {}            # tests inject a fake suspend()
        # Safe charge. Tests swap in fake charge_limit functions so they never touch real sysfs,
        # charge_available is the last read_live() result.
        self.charge_parts = {}
        self.charge_available = True
        self.charge_probe_inflight = False
        # dual-screen settings guard
        self.dualscreen = None          # dualscreen_keys_view.DualScreenKeysController
        self.ds_worker = None           # check() / restore() run here, never on the UI thread
        self.ds_parts = {}              # tests inject checker/restorer
        self.ds_timer = None
        # "charger in but not charging" guard
        self.charge_stuck = None        # charge_stuck_view.ChargeStuckController
        self.charge_stuck_parts = {}    # tests inject a fake sampler
        self.charge_stuck_timer = None
        self.charge_warning_on = False

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
        # This has to happen before SDL_Init so Python owns SIGTERM/SIGINT, not SDL. Python writes
        # the signal to the wakeup fd even while we're blocked in SDL_WaitEventTimeout, and a
        # helper thread turns that into an SDL event that wakes the loop.
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
        # If this thread dies SIGTERM still works but the loop only notices at its next wake (up to
        # 60 s idle), so make the death show up in the log.
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
        # Without this SDL keeps an idle inhibitor for its window and the handheld never blanks or
        # sleeps.
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
        # Never "Bottom"/"Secondary"/"Screen 2" in a title, ROCKNIX's sway rules match those words.
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
        self._rgb_tick()  # stick lights keeper poll
        self._ds_tick()  # dual-screen settings guard poll
        self._charge_stuck_tick()  # "charger not charging" guard poll
        self._replug_tick()  # the add-on replug notice from dual-screen-layout-and-power

    # -- screen placement (the game-screen overlay overrides these hooks) ------
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
        """ModeWatcher worked this out against the output pair it had at the time, but
        on_placement may have rebound and recomputed since (from an event queued ahead of this
        one). Applying a mode for a pairing we already moved away from would undo that fresh
        recompute and leave the panel HIDDEN and blank, so drop it. The next real computation
        will be right.
        """
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
        """ScreenWatcher (main thread): ES moved, the add-on came or went, or the setting changed
        while ES couldnt be seen.
        """
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
            # A fresh process lands on the right output by itself, so a failed rebind only costs a
            # restart.
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
        """Things that name the panel's output (the web apps' BAR check and the keyboard's --output)."""
        web = getattr(self, "web", None)
        if web is None:
            return
        if hasattr(web, "internal"):
            web.internal = output
        kb = getattr(web, "keyboard", None)
        if kb is not None and hasattr(kb, "output"):
            kb.output = output

    def _refresh_screen_setting(self):
        """The file is the truth for the swap (the overlay may have changed it), so keep our copy
        and the Settings toggle in step without queueing a save.
        """
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

    # -- Top screen / Performance tiles ------------------------------------------
    TOP_SCREEN_SH = os.path.join(HERE, "top-screen.sh")

    def ask_top_screen_off(self):
        self.ui.cc.confirm.ask(
            "Turn off the top screen?",
            "The add-on's screen and its power go off to save battery (about 1.7 W). "
            "The Command Center hides with it and ES moves to this screen. Turn it back on "
            "from ES > Ports > Top Screen On-Off, by unplugging the add-on, or by rebooting.",
            "Turn off", on_yes=self.top_screen_off_now, on_no=self.close_sheet)
        self.ui.open(swap_ui.CONFIRM_SHEET)
        self.state_dirty = True

    def top_screen_off_now(self):
        log.info("TOP SCREEN: off requested (092 applies it at its next poll)")
        try:
            subprocess.Popen(["sh", self.TOP_SCREEN_SH, "off"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            log.exception("top-screen.sh")
        self.ui.close()
        self.state_dirty = True

    # -- Quit game (overlay) and the Power sheet -----------------------------------
    def ask_quit_game(self):
        self.ui.cc.confirm.ask(
            "Quit the game?",
            "The running game closes without saving: anything since your last save is lost.",
            "Quit game", on_yes=self.quit_game_now, on_no=self.close_sheet)
        self.ui.open(swap_ui.CONFIRM_SHEET)
        self.state_dirty = True

    def quit_game_now(self):
        """Clean state's own game stop: ES's /emukill, then ROCKNIX's kill target, then a port's
        processes.
        """
        import cleanstate
        self.ui.close()
        self.ui.bar.show_hint("Quitting the game…")
        if self.clean is None:
            log.warning("quit game: no clean-state controller")
            return
        # the helper gets made the first time Clean state opens
        helper = self.clean.helper or self.clean.helper_factory()

        def job():
            plan = helper.discover(check_health=False)
            return helper.execute(plan, [cleanstate.KILL_EMULATOR], confirmed=True)
        self._submit_clean(job, done=self._quit_done)
        self.state_dirty = True

    def _quit_done(self, steps):
        ok = bool(steps) and all(getattr(s, "ok", False) for s in steps)
        log.info("QUIT GAME: %s", "; ".join(getattr(s, "text", str(s)) for s in steps or []))
        self.ui.bar.show_hint("Game closed" if ok else "The game is still running")
        self.state_dirty = True

    def open_power(self):
        self.ui.open("power")
        log.info("screen -> power")
        self.state_dirty = True

    POWER_CONFIRM = {
        "restart": ("Restart the device?", "Everything closes and the RP5 restarts.", "Restart",
                    ("systemctl", "reboot")),
        "shutdown": ("Shut down the device?", "Everything closes and the RP5 turns off.",
                     "Shut down", ("systemctl", "poweroff")),
    }

    def power_action(self, name):
        if name == "sleep":
            self.ui.close()
            self.open_sleep()
        elif name in self.POWER_CONFIRM:
            title, text, yes, cmd = self.POWER_CONFIRM[name]
            self.ui.cc.confirm.ask(title, text, yes, on_yes=lambda: self._power_run(name, cmd),
                                   on_no=self.close_sheet)
            self.ui.open(swap_ui.CONFIRM_SHEET)
        else:
            log.info("power: %s not available", name)
        self.state_dirty = True

    def _power_run(self, name, cmd):
        log.info("POWER: %s -> %s", name, " ".join(cmd))
        self.ui.close()
        self.ui.bar.show_hint("Restarting…" if name == "restart" else "Shutting down…")
        run = self.sleep_parts.get("power_run") or (
            lambda c: subprocess.run(c, timeout=30, capture_output=True))
        self.io_worker.submit(run, cmd, done=lambda _r: None)
        self.state_dirty = True

    PERF_MODES = ("auto", "max", "saver")
    PERF_LABELS = {"auto": "Auto: boost heavy games", "max": "Max: always boosted",
                   "saver": "Saver: always capped"}

    def perf_mode(self):
        import perf_profile
        return perf_profile.read_mode(perf_profile.MODE_PATH)

    def _apply_button_colours(self):
        """Rewrites ES's A/B/X/Y icons for the chosen scheme (or the one matching the theme), only
        when something actually changed.
        """
        choice = config.get_value(self.cfg, ("appearance", "button_colours"))
        theme = config.get_value(self.cfg, ("appearance", "theme_preset"))
        scheme = button_colours.resolve(choice, theme)
        if scheme == getattr(self, "_button_scheme", None):
            return
        try:
            done, msg = button_colours.apply(choice, theme)
        except OSError as e:
            log.warning("button colours: %s", e)
            return
        # ES loaded its icons when it started, so any change from Settings needs an ES restart to show
        self.es_restart_pending = True
        self._button_scheme = done
        self.refresh_es_restart_row()
        if self.ui is not None:
            self.ui.bar.show_hint(msg)

    # -- ES restart for new button icons -----------------------------------------------
    ES_RESTART_ARM_S = 5.0

    def _game_running(self):
        return getattr(getattr(self, "companion", None), "running", None) is not None

    def refresh_es_restart_row(self):
        cc = getattr(getattr(self, "ui", None), "cc", None)
        row = getattr(getattr(cc, "settings", None), "es_restart", None)
        if row is not None:
            row.show(getattr(self, "es_restart_pending", False), self._game_running(),
                     getattr(self, "es_restart_armed", False), getattr(self, "es_restart_busy", False))
            self.state_dirty = True

    def restart_es_for_buttons(self):
        """The ES restart row: the first tap arms it, a second tap within ES_RESTART_ARM_S restarts ES.
        Never while a game runs, a restart would close it."""
        if not getattr(self, "es_restart_pending", False) or self._game_running() or \
                getattr(self, "es_restart_busy", False):
            self.refresh_es_restart_row()
            return
        if not getattr(self, "es_restart_armed", False):
            self.es_restart_armed = True
            self.call_later(self.ES_RESTART_ARM_S, self._disarm_es_restart)
            self.refresh_es_restart_row()
            return
        self.es_restart_armed = False
        self.es_restart_busy = True
        self.refresh_es_restart_row()
        log.info("button colours: restarting ES so the new icons show")

        def job():
            import cleanstate
            if "essway" in own_cgroup():
                return False, "this process runs inside essway.service"
            r = subprocess.run(list(cleanstate.CMD_RESTART_ES), capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stdout + r.stderr).strip()[-120:]
        self._submit_clean(job, done=self._es_restart_done)

    def _disarm_es_restart(self):
        if getattr(self, "es_restart_armed", False):
            self.es_restart_armed = False
            self.refresh_es_restart_row()

    def _es_restart_done(self, result):
        ok, why = result if isinstance(result, tuple) else (False, str(result))
        self.es_restart_busy = False
        if ok:
            self.es_restart_pending = False
        log.info("button colours: ES restart %s", "done" if ok else "failed: " + why)
        self.refresh_es_restart_row()
        if self.ui is not None:
            self.ui.bar.show_hint("ES restarted with the new buttons" if ok else "ES didnt restart")

    def refresh_perf_tile(self):
        try:
            t = self.ui.cc.home._tiles_by_name["home.perf"]
        except (AttributeError, KeyError):
            return
        t.subtitle = self.PERF_LABELS.get(self.perf_mode(), self.PERF_LABELS["auto"])
        self.state_dirty = True

    def cycle_perf_mode(self):
        import perf_profile
        cur = self.perf_mode()
        new = self.PERF_MODES[(self.PERF_MODES.index(cur) + 1) % len(self.PERF_MODES)] \
            if cur in self.PERF_MODES else "auto"
        try:
            tmp = perf_profile.MODE_PATH + ".tmp"
            with open(tmp, "w") as f:
                f.write(new + "\n")
            os.replace(tmp, perf_profile.MODE_PATH)
            log.info("PERF: mode %s -> %s", cur, new)
        except OSError:
            log.exception("perf-mode write")
        self.refresh_perf_tile()

    def after_swap_requested(self):
        if self.pull is not None and self.pull.is_open():
            self.pull._goto(PULL.COMPANION, "swap")

    def build_ui(self, w, h):
        """Builds the widget tree and the navigation around it (no SDL needed, so tests build the
        real thing). Makes the media and video workers unless a test already put fakes in.
        """
        self.w, self.h = w, h
        # the scheme ES is showing now, only settings changes are applied (nothing at start)
        self._button_scheme = button_colours.resolve(
            config.get_value(self.cfg, ("appearance", "button_colours")),
            config.get_value(self.cfg, ("appearance", "theme_preset")))
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
            # self.appearance gets built further down (its sheet needs add_sheet()). These only get
            # called once the sheet is up and someone taps, so looking it up late is fine.
            on_edit_colours=lambda: self.appearance.open(),
            on_reset_appearance=self.reset_appearance,
            on_edit_tiles=self.edit_home_tiles,
            on_restart_es=self.restart_es_for_buttons)
        hotkeys_sheet = hotkeys_view.HotkeysSheet(self)
        self.ui = screens.DeckUI(self, w, h, self.title, companion=view, settings=settings,
                                 hotkeys=hotkeys_sheet)
        self.pull = PULL.from_config(self.cfg.get("command_center"), on_change=self.on_pull)
        kw = summon.swipe_recognizer_kwargs(
            config.get_value(self.cfg, ("command_center", "swipe_sensitivity")))
        # Taps and drags go to widgets. The recogniser watches every stroke and claims the ones the
        # pull-down wants (FULL only, BAR is a 140 px strip and HIDDEN gets nothing). The one
        # exception in BAR is the YouTube TV strip, where a swipe up opens the Command Center like
        # Home does.
        self.gestures = ui.SwipeRecognizer(
            lambda: self.h,
            wants=lambda g: (self.mode in (FULL, hidden_overlay.OVERLAY) and self.pull.wants(g))
            or (self.mode == BAR and self.ytauto is not None and self.ytauto.active
                and g in ("swipe_up", "swipe_up_from_bottom")),
            on_gesture=self.on_gesture, **kw)
        self.router = ui.TouchRouter(self.ui.root, log=log.info, gestures=self.gestures)
        self.companion = companion.CompanionController(
            view, self._submit_media, self, self.video, lambda: self.cfg)
        # dim this panel with ES's screensaver, and undo a crashed run's leftover dim
        self.screen_idle = screen_idle.ScreenIdle(is_docked=self._panel_is_ours_and_docked)
        try:
            self.screen_idle.restore_leftovers()
        except Exception:           # noqa: BLE001
            log.exception("screen idle: restoring a leftover")
        self.web = self._make_web()
        self.ytapp = self._make_ytapp()
        # 0 turns auto-hide off, same as auto_close_timeout_s
        self.ytauto = bar_autohide.BarAutoHide(
            self, timeout_s=config.get_value(self.cfg, ("youtube", "tv_bar_hide_s")))
        self.cc5 = hidden_overlay.OverlayController(self, **self.cc5_parts)
        self.clean = cleanstate_view.CleanStateController(self, self._submit_clean,
                                                          **self.clean_parts)
        self.ui.add_sheet("cleanstate", self.clean.sheet)
        self.steam_view = steam_view.SteamLibraryController(self, self._submit_io, self._submit_media)
        self.ui.add_sheet("steam", self.steam_view.sheet)
        # dual-screen settings guard, a Home banner not a tile
        self.dualscreen = dualscreen_keys_view.DualScreenKeysController(
            self, self._submit_ds, **self.ds_parts)
        self.ui.add_sheet("dualscreen", self.dualscreen.sheet)
        # "charger in but not charging", a Home banner with no sheet. Shares io_worker since its
        # just quick sysfs reads.
        self.charge_stuck = charge_stuck_view.ChargeStuckController(
            self, self._submit_io, **self.charge_stuck_parts)
        self.ui.add_sheet("charge_warning", screens.ChargeWarningSheet(
            charge_stuck_view.HEADLINE, charge_stuck_view.LINES))
        # stick lights load their first state from config (the "lights" group)
        lights = rgb_leds.Controller(state=rgb_leds.state_from_config(self.cfg))
        self.rgb = rgb_view.RGBController(self, lights)
        self.ui.add_sheet("lights", self.rgb.sheet)
        # The Custom colours sheet opens from Settings > Appearance, not a Home tile (so it goes
        # back to Settings, see close_appearance()).
        self.appearance = appearance_view.AppearanceController(self, self.cfg)
        self.ui.add_sheet("appearance_custom", self.appearance.sheet)
        # Push the saved tile order and hidden set into Home, on_tile_layout_changed() writes them
        # back.
        self.ui.cc.home.set_order(config.get_value(self.cfg, ("command_center", "tile_order")))
        self.ui.cc.home.set_hidden(config.get_value(self.cfg, ("command_center", "hidden_tiles")))
        self.refresh_perf_tile()  # the tile shows the saved mode
        self._apply_top_brightness()  # the add-on's saved dimming
        self.ui.cc.set_bar_app(None, keys_offered=self.web.policy.enabled())
        self.ui.set_view("companion")
        self.companion.refresh()
        self._update_active()
        self.tabs = app_tabs.TabsController(self, **self.tabs_parts)
        self.ui.cc.set_tabs(self.tabs.strip)

    def _submit_media(self, fn, *args, done=None):
        self.media_worker.submit(fn, *args, done=done)

    def _apps_setting(self, key, ok, default):
        """An apps.* value if the config has a valid one, else the default."""
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
        """Its own Firefox instance and profile (see web_tiles.YtAppSession for why its not a third
        WebApps label).
        """
        parts = {"registry": self.web.registry,
                # live via on_setting(), read once here at startup
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
        """ES events (hooks plus the /runningGame poll) and the summon button reader. Both cope with
        their device missing (the PC, tests, undocked).
        """
        import name_guard
        # A Firefox, mpv or wvkbd left behind by a hard crash of the last run gets stopped before
        # the mode watcher starts.
        self.web.reap_stale()
        self.es_watcher = esevents.Watcher(lambda ev: self.post(self.on_es_event, ev),
                                           running_probe=esevents.probe_running_game,
                                           poll_interval=ES_POLL_PERIOD, log_fn=log.info)
        self.es_watcher.start()
        # Warn (log and companion view) if ES would shell-parse a game name or path through the
        # hooks. Scans on its own thread.
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
        self.surface = (w, h)
        sdl.SetWindowSize(self.win, w, h)
        # the UI is laid out on a canvas sized for the chosen preset and the renderer stretches it
        # to the surface (on the RP5 at Auto theyre the same)
        w, h = screen_presets.layout_size(
            w, h, config.get_value(self.cfg, ("screens", "ui_resolution")))
        self.w, self.h = w, h
        self.tex = sdl.CreateTexture(self.ren, sdl.PIXELFORMAT_ARGB8888,
                                     sdl.TEXTUREACCESS_STREAMING, w, h)
        # cairo ARGB32 is premultiplied, the companion leaves a see-through hole over the video
        # texture drawn underneath.
        if not sdl.SetTextureBlendMode(self.tex, sdl.BLENDMODE_BLEND_PREMULTIPLIED):
            # The UI is opaque except over the video, so the panel still looks right, only
            # video-under-UI blending would be off.
            log.warning("premultiplied blend refused by the renderer: %s", sdl.error())
        self.canvas = self.gfx.Canvas(w, h)
        self.ui.set_size(w, h)
        self._update_active()
        self.counts["resizes"] += 1
        self.state_dirty = True
        log.info("surface size %dx%d%s", self.surface[0], self.surface[1],
                 "" if (w, h) == self.surface else " (layout %dx%d)" % (w, h))

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
        # the loop has to wake for these by itself, an idle companion can go 30 s+ between wakes
        # which is right up against command-center-app's hang threshold
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

    # -- mode watchdog (a backstop for a stuck mode) ------------------------------
    def _reconcile(self, now):
        """Every RECONCILE_PERIOD, work the mode out again from a fresh sway tree and compare it to
        self.mode. It only corrects after the same disagreement shows up twice in a row, so a
        single stale read or a short ES blip never fights something in flight.

        Skipped (not even counted) when theres no ModeWatcher (the game-screen overlay), while
        OVERLAY is up, while the Command Center is open or opening, or before a rebind's configure
        has landed.
        """
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

    # -- heartbeat for command-center-app's hang detector ---------------------------
    def heartbeat_path(self):
        return os.path.join(self.run_dir, "heartbeat")

    def _touch_heartbeat(self, now):
        """Touched from the main loop itself, so a stale file with the process still alive means the
        loop is hung, not just busy. command-center-app restarts on exactly that.
        """
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
        if new and new[0] and new[1] and new != self.surface:
            self._resize(*new)

    def _render(self):
        if self.cc5 is not None:
            self.cc5.render_handle()  # the corner handle's own surface
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
            if data is None:
                # The cairo surface has no pixel buffer (e.g. a layout-size edge case during
                # configure). We already took the damage, so re-mark the root dirty to repaint
                # next frame and skip this one instead of crashing on c_void_p(None + ...).
                if not self._pixels_warned:
                    log.warning("canvas has no pixel data (layout %dx%d surface %s) - skipping frame",
                                self.w, self.h, self.surface)
                    self._pixels_warned = True
                self.ui.root.damage_all()
                return
            x, y, w, h = dmg
            rect = sdl.SDL_Rect(x, y, w, h)
            sdl.UpdateTexture(self.tex, byref(rect), c_void_p(data + y * stride + x * 4), stride)
        sdl.RenderClear(self.ren)
        if self._video_visible():
            sdl.RenderTexture(self.ren, self.vtex, byref(self.video_src), byref(self._to_surface(self.video_dst)))
        sdl.RenderTexture(self.ren, self.tex, None, None)
        sdl.RenderPresent(self.ren)
        self.layer.presented()
        self.counts["presents"] += 1

    def _to_surface(self, r):
        """A rect on the layout canvas -> the same rect on the real surface."""
        sw, sh = self.surface
        if (sw, sh) == (self.w, self.h) or not self.w or not self.h:
            return r
        fx, fy = sw / float(self.w), sh / float(self.h)
        return self.sdl.SDL_FRect(r.x * fx, r.y * fy, r.w * fx, r.h * fy)

    def _to_layout(self, x, y):
        """A pointer position on the real surface -> the layout canvas."""
        sw, sh = self.surface
        if (sw, sh) == (self.w, self.h) or not sw or not sh:
            return x, y
        return x * self.w / float(sw), y * self.h / float(sh)

    def _video_visible(self):
        return bool(self.vtex and self.video_dst is not None and self.companion is not None
                    and self.companion.view.video_on and self.ui.showing == "companion")

    def _upload_video_frame(self):
        """UI thread: copy the video worker's newest frame into the video's own XRGB8888 texture.
        Never renders video itself.
        """
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
        """Posted by the video worker. Nothing to do here, the wake alone makes the loop render and
        upload the frame.
        """

    def _handle(self, ev):
        sdl = self.sdl
        t = sdl.event_type(ev)
        if t == self.post.ev_type:
            return                  # drained at the top of the loop
        if t in (sdl.EV_FINGER_DOWN, sdl.EV_FINGER_MOTION, sdl.EV_FINGER_UP,
                 sdl.EV_FINGER_CANCELED):
            f = sdl.TouchFinger.from_buffer(ev)
            if self.cc5 is not None and self.cc5.handle_event(t, f.windowID, ("f", f.fingerID)):
                return  # a touch on the corner handle's window
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
                return  # the corner handle's window
            self._activity()
            x, y = self._to_layout(m.x, m.y)
            if m.down:
                self._down(("m", 0), x, y, "pointer")
            else:
                self.router.up(("m", 0), x, y)
        elif t == sdl.EV_MOUSE_MOTION:
            m = sdl.MouseMotion.from_buffer(ev)
            if m.which != sdl.TOUCH_MOUSEID and m.state & sdl.BUTTON_LMASK:
                self.router.move(("m", 0), *self._to_layout(m.x, m.y))
        elif t == sdl.EV_WINDOW_EXPOSED:
            self.ui.root.damage_all()
        elif t == sdl.EV_QUIT:
            if self.stop_reason is None:
                self.stop_reason = "SDL_EVENT_QUIT"

    def _panel_is_ours_and_docked(self):
        """The backlight is DSI-1's, only act while the Command Center is on DSI-1 and ES is on the
        add-on. Undocked ES is on this panel and dims it itself.
        """
        if self.output != screen_map.CURRENT.bottom:
            return False
        try:
            c = sway_ipc.Ipc(sway_ipc.find_socket(), 1.0)
            try:
                outs = c.request(sway_ipc.GET_OUTPUTS)
            finally:
                c.close()
        except Exception:           # noqa: BLE001
            return False
        return any(o.get("name") == self.es_output and o.get("active") for o in outs)

    def _down(self, pid, x, y, kind):
        self.counts["input_down"] += 1
        # a touch on a dimmed panel only wakes it and doesnt press anything
        if self.screen_idle is not None and self.screen_idle.on_touch():
            self.last_input = {"kind": kind, "x": round(x), "y": round(y), "mode": self.mode,
                               "widget": "screen_idle.wake", "t": time.time()}
            self.state_dirty = True
            return
        w = self.router.down(pid, x, y)
        self.last_input = {"kind": kind, "x": round(x), "y": round(y), "mode": self.mode,
                           "widget": (w.name if w is not None else None), "t": time.time()}
        self.state_dirty = True

    # -- debug self-test -----------------------------------------------------
    def _debug_drag(self, v0=None, v1=0.60, dur=0.5, hz=60):
        """Pushes DOWN, MOTION x N, UP finger events for the volume slider into SDL's own queue, 1/hz
        apart, to test the real touch path in the running app (nothing reaches sway). The drag
        starts on the knob since the slider is relative.
        """
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
        # the YouTube TV strip's swipe up parks it and shows Home. pull.state is already
        # COMMAND_CENTER while BAR shows the strip, so handle_gesture()'s swipe up would be the
        # wrong move.
        if self.mode == BAR and self.ytauto is not None and self.ytauto.active and \
                name in ("swipe_up", "swipe_up_from_bottom"):
            self.on_tv_home()
        elif self.pull.state == summon.PullDownStateMachine.SETTINGS and \
                name in ("swipe_up", "swipe_down") and self.ui is not None:
            # a swipe up or down pages through Settings, a swipe up from the bottom edge still closes it
            self.ui.settings.change_page(1 if name == "swipe_up" else -1)
        else:
            self.pull.handle_gesture(name)
        self.state_dirty = True

    # -- the pull-down Command Center ---------------------------------------------
    def _activity(self):
        """Any touch while the Command Center is open restarts its auto-close countdown."""
        if self.pull is not None and self.pull.is_open():
            self.pull.rearm_timeout()
        if self.cc5 is not None:
            self.cc5.activity()  # the overlay's own countdown
        if self.ytauto is not None:
            # every touch that gets here while BAR shows the YouTube TV app is on the strip or its
            # handle, nothing else is mapped then
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
            self.cc5.after_pull(old, new, reason)  # closing it over a game goes back to HIDDEN
        if self.tabs is not None:
            self.tabs.after_pull(old, new, reason)  # refresh the tabs

    def _pull_open(self, reason):
        # The pull tab is always a way in, even with swipe-down off and no hardware button,
        # otherwise Settings could be unreachable.
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
            return  # HIDDEN opens it over the game, OVERLAY closes it
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
        if self.mode == BAR or self.charge_warning_on:
            # while Firefox or a video fills the panel the strip is all thats showing, and the Command
            # Center (say the YouTube results) has to still be there when the window closes, so restart
            # its countdown instead of closing it.
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
        if self.screen_idle is not None:
            try:
                self.screen_idle.on_es_event(ev.kind)
            except Exception:       # noqa: BLE001
                log.exception("screen idle: %s", ev.kind)
        if ev.kind == esevents.WAKE and self.rgb is not None:
            self.rgb.lights.on_es_wake()  # ROCKNIX may have just put its own colour back
        if ev.kind in (esevents.GAME_START, esevents.GAME_END):
            self.refresh_es_restart_row()
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
        self._probe_charge_limit()  # show live sysfs values, not a stale cache
        self._probe_brightness()  # the live backlight too

    def close_settings(self):
        self.pull.back_tap()

    def edit_home_tiles(self):
        """Settings > Command Center > "Edit buttons..." goes back to the Command Center's Home,
        straight into Edit tiles (drag to reorder, eye to hide).
        """
        self.pull.back_tap()                 # SETTINGS -> COMMAND_CENTER
        self.ui.close()                      # its Home, not a sheet
        self.ui.cc.home.enter_edit()
        self.state_dirty = True

    # -- Safe charge and brightness -------------------------------------------------
    BRIGHT_KEYS = (("screens", "bottom_brightness"), ("screens", "top_brightness"),
                   ("screens", "match_brightness"))

    def _apply_top_brightness(self):
        pct = config.get_value(self.cfg, ("screens", "top_brightness"))
        if pct < 100:
            self.top_dim.set_pct(pct)

    def _probe_brightness(self):
        """On Settings open the bottom slider shows the live backlight (ROCKNIX's own brightness
        control writes it too).
        """
        live = self.bottom_bl.get_pct()
        if live is None:
            return
        config.set_value(self.cfg, ("screens", "bottom_brightness"),
                         max(brightness.BOTTOM_MIN_PCT, live))
        self._refresh_rows(("screens", "bottom_brightness"))

    def _refresh_rows(self, *paths):
        settings = getattr(self.ui, "settings", None)
        if settings is None:
            return
        for p in paths:
            row = settings.rows.get(p)
            if row is not None:
                row.refresh_from_cfg()
        self.state_dirty = True

    def _on_brightness(self, key_path, value):
        g = lambda k: config.get_value(self.cfg, ("screens", k))
        if key_path == ("screens", "bottom_brightness"):
            self.bottom_bl.set_pct(value)
            if g("match_brightness"):
                top = brightness.matched_top(value, g("match_ratio"))
                config.set_value(self.cfg, ("screens", "top_brightness"), top)
                self.top_dim.set_pct(top)
                self._refresh_rows(("screens", "top_brightness"))
                self._schedule_save()
        elif key_path == ("screens", "top_brightness"):
            self.top_dim.set_pct(value)
            if g("match_brightness"):              # moving top while matched re-sets the ratio
                config.set_value(self.cfg, ("screens", "match_ratio"),
                                 brightness.match_ratio(value, g("bottom_brightness")))
        elif key_path == ("screens", "match_brightness") and value:
            live = self.bottom_bl.get_pct() or g("bottom_brightness")
            config.set_value(self.cfg, ("screens", "match_ratio"),
                             brightness.match_ratio(g("top_brightness"), live))
            log.info("brightness: match on, ratio %.2f (top %d%% / bottom %d%%)",
                     g("match_ratio"), g("top_brightness"), live)

    def _probe_charge_limit(self):
        """Every time Settings opens, read the real kernel thresholds (someone may have run
        battery-charge-limit by hand, or nothing's saved yet) so the rows show what the device is
        actually doing. With no charge limit sysfs at all the section shows disabled with a note.
        """
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
        """Both battery keys use this. Toggle off means no limit (end=100) whatever the slider says,
        on means the slider's value. Runs off the UI thread, and if the read-back doesnt match it
        shows an error on the % row instead of trusting the write.
        """
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
        """SettingsSheet already checked and stored the value. Save soon, apply live settings now,
        restart-flagged ones wait ("applies after restart").
        """
        key_path = tuple(key_path)
        self.settings_changes += 1
        f = config.field_for(key_path)
        log.info("setting %s = %r%s", ".".join(key_path), value,
                 " (applies after restart)" if f and f["restart"] else "")
        self._unsaved[key_path] = value
        if key_path[0] == "screens":
            # dual-screen-layout-and-power reads the file every poll, so write it now not in 0.8 s
            self.cancel(self.save_timer)
            self._save_now()
        else:
            self._schedule_save()
        if f is None or f["restart"]:
            return
        if key_path in self.BRIGHT_KEYS:
            self._on_brightness(key_path, value)
            return
        if key_path[0] in ("companion", "manuals", "steam"):
            self.companion.config_changed()
        elif key_path[0] == "appearance":
            # Any appearance key can change the theme, so apply_theme() rebuilds the whole THEME from
            # self.cfg every time instead of patching one key.
            palettes.apply_theme(self.cfg)
            if key_path[1] in ("theme_preset", "button_colours"):
                self._apply_button_colours()
            # Changing THEME isnt enough on its own, a widget only redraws when something damages it,
            # so the rest of Settings would keep the old colours. damage_all() repaints everything.
            if self.ui is not None:
                self.ui.root.damage_all()
        elif key_path == ("screens", "ui_resolution"):
            if self.surface[0] and self.win is not None:
                self._resize(*self.surface)
                self.ui.root.damage_all()
        elif key_path == ("command_center", "swipe_down_enabled"):
            self.pull.swipe_down_enabled = bool(value)
        elif key_path == ("command_center", "auto_close_timeout_s"):
            self.pull.auto_close_timeout_s = int(value or 0)
            self.pull.rearm_timeout()
            self._arm_cc_tick()
        elif key_path == ("youtube", "tv_bar_hide_s"):
            if self.ytauto is not None:
                self.ytauto.timeout_s = int(value or 0)  # 0 turns auto-hide off
        elif key_path == ("youtube", "tv_swipe_natural"):
            if self.ytapp is not None:
                self.ytapp.swipe_natural = bool(value)  # live, no restart needed
        elif key_path in (("battery", "safe_charge_enabled"), ("battery", "safe_charge_end_pct")):
            self._apply_charge_limit()
        elif key_path == ("command_center", "swipe_sensitivity"):
            kw = summon.swipe_recognizer_kwargs(value)
            self.gestures.edge, self.gestures.distance = kw["edge"], kw["distance"]
        elif key_path in (hidden_overlay.KEY_CORNER_HANDLE, hidden_overlay.KEY_OVERLAY_ON_HIDDEN):
            if self.cc5 is not None:
                self.cc5.config_changed()  # read from self.cfg, the handle follows right away
        # audio.show_volume_overlay is read on every level change. audio.volume_step_pct isnt used
        # yet (the hardware keys use /usr/bin/volume's own step of 5).
        self.state_dirty = True

    def _schedule_save(self):
        self.cancel(self.save_timer)
        self.save_timer = self.call_later(SAVE_DELAY, self._save_now)

    def _save_now(self):
        """Writes only the keys changed since the last save on top of the file as it is now. The
        game-screen overlay writes it too (its "Swap screens"), and saving our whole older copy
        would undo its swap.
        """
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
                # every app but YouTube TV keeps the fixed strip (bar_autohide.wants() decides)
                h, zone = self.ytauto.geometry(self.ui.cc.bar.app, BAR_H, BAR_H)
            return wl.ANCHOR_BOTTOM | wl.ANCHOR_LEFT | wl.ANCHOR_RIGHT, (0, h), zone
        return wl.ANCHOR_ALL, (0, 0), 0

    def _apply_bar_geometry(self):
        """For the auto-hide timer and touches, and on the YouTube TV app's first BAR entry, since
        geometry() ran before Bar.app turned "tv" and has to be redone. Only ever calls
        set_geometry() on an already-mapped surface.
        """
        if self.mode == HIDDEN:
            return
        anchor, size, zone = self.geometry(self.mode)
        self.layer.set_geometry(anchor, size, zone)
        self.ui.root.damage_all()

    def _refresh_bar_geometry(self):
        """geometry() runs before the apps flip Bar.app, so the strip can have been sized for the app that
        was there before (YouTube's auto-hide zone under Discord). Redo it if it now differs.
        """
        if self.mode != BAR or self.layer.hidden:
            return
        anchor, size, zone = self.geometry(BAR)
        if self.layer.geometry != (anchor, (int(size[0]), int(size[1])), int(zone)):
            self._apply_bar_geometry()

    def on_mode(self, mode, reason):
        if self.cc5 is not None and self.cc5.intercept_mode(mode, reason):
            return  # the overlay stays up over the game's window
        if mode == self.mode:
            if mode == BAR and reason != self.mode_reason:
                # still the bar, but a different window of ours is on it: tell the apps so the strip
                # controls the one that is showing (web buttons or YouTube's D-pad)
                self.mode_reason = reason
                log.info("BAR: now %s", reason)
                if self.web is not None:
                    self.web.on_mode(mode, mode, reason)
                if self.ytapp is not None:
                    self.ytapp.on_mode(mode, mode, reason)
                if self.ytauto is not None:
                    self.ytauto.on_mode(mode, mode, reason)
                self.state_dirty = True
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
                self.cc5.apply_layer(mode)  # OVERLAY goes on the overlay layer, the rest on TOP
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
            self.ytapp.on_mode(old, mode, reason)
        if self.ytauto is not None:
            self.ytauto.on_mode(old, mode, reason)  # arms or disarms the auto-hide timer
        self._refresh_bar_geometry()
        self._update_active()       # video only in FULL, on the companion
        self.state_dirty = True
        if self.cc5 is not None:
            self.cc5.after_mode(old, mode, reason)  # the corner handle
        if self.tabs is not None:
            self.tabs.after_mode(old, mode, reason)  # a tab's follow-up, and the tabs

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
        """Stick lights keeper, cheap unless ROCKNIX actually overwrote the sticks, and never does
        anything in mode "rocknix". Re-armed every time like the clock and battery ticks.
        """
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
            self.cc5.on_master(prev, m)  # the corner handle shows a changed level
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

    # Volume read-backs. Every live set during a drag makes pw-mon report a change, and reading
    # back on each one piled up on the audio worker and showed stale values after release. So:
    # one read in flight plus an "again" flag, no read while the finger is down or until the
    # commit lands, and a read that comes back in that window is dropped.
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
        """A live set from the throttle. If wpctl is slower than the throttle, sets would pile up
        ahead of the commit, so only one is queued and it sends the newest value.
        """
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
        self.commit_pending = True  # read-backs wait for it
        self.audio_worker.submit(guarded(self.backend.commit_master, self.faillog), v,
                                 done=self._commit_done)
        self.state_dirty = True

    def on_volume_cancel(self, v0, changed=False):
        """The press ended without a commit (turned into a swipe, the view closed under the finger,
        shutdown, or nothing changed). The slider's already back at v0, and a live change that
        already went out gets undone with one set back to v0.
        """
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
            # In dry-run nothing changed, so a re-read would just undo the UI. Only a change reported
            # meanwhile is worth reading.
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
        """Like on_volume_cancel for a mixer row: undo the live change and commit nothing (a stream
        has no saved volume, so the restore is the whole undo).
        """
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
            # BAR has no HUD on purpose. The surface is a 140 px strip and the rest of DSI-1 is the
            # rp5deck window (Firefox, YouTube). Showing it would mean re-anchoring full panel and back
            # for a sheet thats one close away in FULL.
            log.info("battery tapped in BAR mode: no HUD in BAR (see on_battery)")
            return
        self.open_hud()

    def open_hud(self):
        self.ui.open("hud")
        self.cancel(self.hud_timer)
        self.hud_timer = self.call_later(0, self._hud_tick)
        log.info("screen -> hud")
        self.state_dirty = True

    def open_notes(self):
        """The Notes tab. The notebook is read the first time its opened, not at startup, and while
        a game runs it opens that game's notebook instead.
        """
        notes = self.ui.cc.notes
        if not getattr(notes, "loaded", False):
            notes.load_current()        # the notebook open last (notes/.current)
            notes.loaded = True
            log.info("notes: %s (%s)", notes.book.load_note, notes.book.path)
            notes._refresh()
        running = getattr(getattr(self, "companion", None), "running", None)
        if running is not None and getattr(running, "rom_path", ""):
            notes.open_for_game(running.system, running.rom_path, running.name)
        else:
            notes.open_regular()
        log.info("notes: open %s", notes.book.path)
        self.ui.open("notes")
        log.info("screen -> notes")
        self.state_dirty = True

    def open_mixer(self):
        self.ui.open("mixer")
        self.ui.mixer.streams = "loading"
        self.ui.mixer.msg.set_text("Reading audio streams…")
        self.ui.mixer.msg2.set_text("")
        self._refresh_streams()
        log.info("screen -> mixer")
        self.state_dirty = True

    # -- Hotkey cheat sheet -----------------------------------------------------------
    def open_hotkeys(self):
        info = getattr(self.companion, "info", None)
        running_system = info.get("system") if info and info.get("running") else None
        contexts, order = hotkeys.load(running_system=running_system)
        self.ui.hotkeys.set_data(contexts, order)
        self.ui.open("hotkeys")
        log.info("screen -> hotkeys (running_system=%r)", running_system)
        self.state_dirty = True

    def open_steam_library(self):
        """The Steam tab: installed games as cover art, a tap starts one in the open Steam."""
        log.info("tab -> Steam library")
        self.steam_view.open()
        self.state_dirty = True

    # -- Clean state --------------------------------------------------------------------
    def open_clean_state(self):
        """The tile checks whats running and ES's health, then the confirm sheet. Every stop or
        restart is behind that confirm.
        """
        log.info("tile -> Clean state")
        self.clean.open()
        self.state_dirty = True

    # -- dual-screen settings guard ----------------------------------------------------
    def on_dualscreen_restore(self):
        """The Home banner's Restore button opens the confirm sheet naming the missing keys, restore
        only runs after that.
        """
        log.info("banner -> dual-screen settings")
        if os.environ.get("RP5DECK_DEMO_NOTICE"):
            log.info("demo notice: Restore does nothing")
            return
        self.dualscreen.open()

    def _replug_tick(self):
        home = getattr(getattr(self, "ui", None), "home", None)
        if home is not None:
            home.set_replug_notice(ADDON_REPLUG_TEXT if os.path.exists(ADDON_REPLUG_FLAG) else "")
        self.call_later(ADDON_REPLUG_POLL_S, self._replug_tick)

    def _ds_tick(self):
        if self._demo_notice("dualscreen"):
            pass
        elif self.dualscreen is not None:
            self.dualscreen.poll()
        self.ds_timer = self.call_later(DS_POLL_S, self._ds_tick)

    # -- demo banners for guide screenshots -------------------------------------------
    # RP5DECK_DEMO_NOTICE=charge|dualscreen (in $RP5DECK_HOME/env, then restart
    # command-center-app) shows that banner with sample text instead of polling, so the guide
    # can photograph it without breaking system.cfg or the charger. Its button does nothing.
    DEMO_NOTICE_TEXT = {
        "dualscreen": "Dual-screen settings missing: 3ds.screen_layout, wiiu.gamepad_enabled",
    }

    def _demo_notice(self, which):
        """True if RP5DECK_DEMO_NOTICE names `which`, then it shows that sample on Home."""
        if os.environ.get("RP5DECK_DEMO_NOTICE") != which:
            return False
        home = getattr(getattr(self, "ui", None), "home", None)
        if home is not None:
            getattr(home, "set_%s_notice" % which)(self.DEMO_NOTICE_TEXT[which])
        return True

    # -- "charger in but not charging" ------------------------------------------------
    def _charge_stuck_tick(self):
        if os.environ.get("RP5DECK_DEMO_NOTICE") == "charge":
            self.set_charge_warning(True)
        elif self.charge_stuck is not None:
            self.charge_stuck.poll()
        self.charge_stuck_timer = self.call_later(CHARGE_STUCK_POLL_S, self._charge_stuck_tick)

    def set_charge_warning(self, on):
        """The big charger warning. On: put it in front in whatever mode were in (every poll, so
        something that closed it gets it back). Off: take it down and close what it opened."""
        if not on:
            if self.charge_warning_on:
                self.charge_warning_on = False
                log.info("charge warning closed")
                if self.ui.sheet == "charge_warning":
                    self.ui.close()
                    if self.mode == hidden_overlay.OVERLAY:
                        self.cc5.dismiss("charge warning closed")
                    else:
                        self.pull.close("charge warning closed")
                    self.state_dirty = True
            return
        if not self.charge_warning_on:
            log.warning("charge warning up")
        self.charge_warning_on = True
        if self.mode == BAR:
            # park the app (it keeps running) so the warning gets the whole screen
            self.tabs.run(app_tabs.wsw.INTERNAL, lambda: self.set_charge_warning(True), "charge warning")
            return
        if self.mode == HIDDEN:
            self.cc5.open("charge_warning")
        elif self.mode == FULL:
            self.pull.open("charge_warning")
        if self.mode in (FULL, hidden_overlay.OVERLAY) and self.ui.sheet != "charge_warning":
            self.ui.open("charge_warning")
            self.ui.root.damage_all()
            self.state_dirty = True

    # -- stick lights -------------------------------------------------------------------
    def open_lights(self):
        log.info("tile -> Stick lights")
        self.rgb.open()

    # -- Appearance ---------------------------------------------------------------------
    def close_appearance(self):
        """Back from Custom colours goes to Settings, since thats where it opened from. Refresh the
        "Colour theme" row because opening the editor may have switched the preset to "custom"
        and Settings isnt rebuilt on its own.
        """
        self.ui.open("settings")
        row = self.ui.settings.rows.get(("appearance", "theme_preset"))
        if row is not None:
            row.refresh_from_cfg()
        self.state_dirty = True

    def reset_appearance(self):
        """Settings > Appearance > "Reset to default": every appearance key back to its default,
        applied and saved like any other setting.
        """
        for path in (("appearance", "theme_preset"), ("appearance", "custom_accent"),
                    ("appearance", "custom_bg"), ("appearance", "custom_text")):
            default = config.field_for(path)["default"]
            if config.set_value(self.cfg, path, default):
                self.on_setting(path, default)
        row = self.ui.settings.rows.get(("appearance", "theme_preset"))
        if row is not None:
            row.refresh_from_cfg()

    # -- Keyboard -----------------------------------------------------------------------
    def toggle_keyboard(self):
        """A toggle, not a sheet. Signals ROCKNIX's own wvkbd (same SIGRTMIN input_sense sends, it
        never starts or stops it). If that works close the Command Center so the keys reach the
        game or ES underneath. If nothing's running (the ES setting is off) stay open and show a
        hint for 2.5 s.
        """
        log.info("tile -> Keyboard")
        if rocknix_keyboard.toggle():
            self.close_command_center()
        elif self.ui.bar.show_hint("Turn on System Settings > Enable Touchscreen Keyboard in ES"):
            self.call_later(2.5, self.ui.bar.clear_hint)

    # -- Sleep tile -----------------------------------------------------------------------
    def open_sleep(self):
        """Shows a quick "Sleeping..." hint, then runs `systemctl suspend` off the UI thread after
        SLEEP_DELAY_S so the hint actually paints first. A second tap while its pending does
        nothing.
        """
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

    # -- Tile customisation (Home's Edit tiles) -------------------------------------------
    def on_tile_layout_changed(self, order, hidden):
        """Home calls this after every change (drag, hide/show, reset, Done). order is always every
        tile, hidden never has "home.settings". Saved right away so a crash cant lose it.
        """
        changes = {}
        if config.set_value(self.cfg, ("command_center", "tile_order"), order):
            changes[("command_center", "tile_order")] = order
        if config.set_value(self.cfg, ("command_center", "hidden_tiles"), hidden):
            changes[("command_center", "hidden_tiles")] = hidden
        if changes:
            config.save_changes(changes)
        self.state_dirty = True

    # -- Browser / Discord ----------------------------------------------------------------
    def open_browser(self):
        log.info("tile -> Browser")
        self._web_tile("Browser", None)
        self.state_dirty = True

    def open_discord(self):
        log.info("tile -> Discord")
        self._web_tile("Discord", browser.DISCORD_URL)
        self.state_dirty = True

    def _web_tile(self, label, url):
        """An emulator window on this screen gets parked first (else Firefox tiles next to it and
        the panel goes HIDDEN) and a parked Firefox comes back, then open_web.
        """
        if self.tabs is None:
            self.web.open_web(label, url)
            return
        self.tabs.run(app_tabs.wsw.WEB, lambda: self.web.open_web(label, url), "tile " + label)

    def open_ytapp(self):
        """The YouTube TV tile, same park-whats-shown-first as the Browser/Discord tiles."""
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
            self.ytapp.action(name)  # the D-pad, its own session
        else:
            self.web.action(name)
        self.state_dirty = True

    # -- the YouTube TV strip's Close and Home --------------------------------------------
    def on_tv_close(self):
        """Ends the YouTube App session. Its Firefox is asked to quit (Marionette:Quit, then
        SIGTERM), and once its window leaves DSI-1 sway reports FULL and the Command Center comes
        back through on_mode, nothing here forces it.
        """
        log.info("tv strip -> Close")
        if self.ytapp is not None:
            self.ytapp.close()
        if self.ytauto is not None:
            self.ytauto.stop()          # cancel the auto-hide timer right away

    def on_tv_home(self):
        """Parks the YouTube App (it keeps running) and opens the Command Center's Home in one tap.
        The tab strip still lists it so its easy to get back to.
        """
        log.info("tv strip -> Home")
        if self.tabs is not None:
            self.tabs.select(app_tabs.CC)
        elif self.pull is not None:
            self.pull.open("tv_home")
        if self.ytauto is not None:
            self.ytauto.stop()          # cancel the auto-hide timer right away

    def show_app_tabs(self):
        """The strip's Tabs button parks this app's window (it keeps running) and opens the Command
        Center with the tabs.
        """
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

    def manual_tool(self, name):
        """The manual viewer's PDF tools."""
        self.companion.manual_tool(name)
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
            # hud.read_running_game() never sees a running game (ES only sends "msg" when nothing
            # runs), so ask the companion, it knows from the hooks and the /runningGame poll.
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
                "size": [self.w, self.h],           # the layout canvas (targets are on it)
                "surface_size": list(self.surface),
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
            "tabs": self.tabs.state() if self.tabs is not None else None,
            "ytapp": self.ytapp.state() if self.ytapp is not None else None,
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
            "lights": self.rgb.lights.state.to_dict() if self.rgb else None,
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
        try:
            self.top_dim.stop()  # sway puts the add-on's gamma back
        except Exception:           # noqa: BLE001
            log.exception("top brightness at shutdown")
        try:
            if self.screen_idle is not None:
                self.screen_idle.shutdown()  # never exit with the panel dimmed
        except Exception:           # noqa: BLE001
            log.exception("screen idle at shutdown")
        # A drag in progress gets cancelled: the slider goes back, a live volume change is undone
        # (queued on the audio worker, which drains before it stops), nothing is committed.
        try:
            if getattr(self, "router", None):
                self.router.cancel_all()
        except Exception:           # noqa: BLE001
            log.exception("cancel_all at shutdown")
        # Save a setting changed less than SAVE_DELAY ago now, before the io worker stops.
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
                           ("ytapp", lambda: self.ytapp and self.ytapp.shutdown()),
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
                self.cc5.shutdown()  # the corner handle's surface and window
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
