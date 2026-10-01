"""The Browser and Discord tiles, plus the YouTube TV tile.

Browser and Discord share one Firefox (browser.py, app_id rp5deck-web) and one profile.
Browser opens the start page, Discord opens https://discord.com/app, and login is by QR code
on your phone so nothing here ever sees a password. YouTube TV is its own Firefox
(YtAppSession below).

Display modes: the dual-screen daemon's for_window rules put the window on DSI-1, sway_ipc
reports BAR, the layer surface shrinks to the strip and the strip shows this app's controls.
When the window goes (FULL after BAR) the session ends and the processes stop. HIDDEN only
hides the on-screen keyboard. The app tabs can park the window on a workspace that's never
shown, and `parked` keeps the FULL that follows from ending the session, so the app keeps
running and its tab brings it back.

Nothing here sends a sway command. It only reads the tree to see if Firefox has focus and
where its window is. You focus Firefox by tapping it, and when it closes focus_guard hands
focus back to ES.

Every Browser and keyboard call runs on one FIFO worker, so a stop queued after a start
always runs after it. Results come back on the UI thread with a generation number, so a late
answer from a finished session gets dropped.
"""
import json
import logging
import os
import signal
import time

import browser as browser_mod
import osk
import sway_ipc

WEB = "web"
WEB_APP_ID = browser_mod.APP_ID
STARTING, RUNNING = "starting", "running"


def _reason_mentions(app_id, reason):
    """Does the mode-change reason ("rp5deck window on DSI-1: rp5deck-web, rp5deck-ytapp", the
    comma list after the last ": ") name app_id as one of the visible windows? None means no
    reason was given, act like before. A plain substring test isnt enough since "rp5deck-yt" is
    a prefix of "rp5deck-ytapp".
    """
    if reason is None:
        return True
    return app_id in reason.rsplit(": ", 1)[-1].split(", ")
WEB_POLL = 0.7  # Firefox alive? focused? text field? (drives the keyboard)
MAP_TIMEOUT = 15.0  # after a successful start with no window on DSI-1
HINT_SECONDS = 2.5

log = logging.getLogger("rp5deck.web")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _walk_views(node, output=None):
    """(view, output name) for every leaf window in a sway tree."""
    if node.get("type") == "output":
        output = node.get("name")
    if sway_ipc._is_view(node):
        yield node, output
        return
    for key in ("nodes", "floating_nodes"):
        for c in node.get(key) or []:
            yield from _walk_views(c, output)


def window_facts(tree, app_id):
    """What the web worker needs from sway's tree, read only: present (a window with app_id
    exists), output (which output it's on), focused (it has keyboard focus), focused_app (the
    app_id of whatever has focus).
    """
    facts = {"present": False, "output": None, "focused": False, "focused_app": None}
    for v, out in _walk_views(tree):
        aid = str(v.get("app_id") or "")
        if v.get("focused"):
            facts["focused_app"] = aid or sway_ipc.window_label(v)
        if aid == app_id:
            facts["present"] = True
            facts["output"] = out
            facts["focused"] = bool(v.get("focused"))
    return facts


def read_window_facts(app_id, timeout=1.0):
    """window_facts() of the live tree, or None if sway couldnt be read."""
    path = sway_ipc.find_socket()
    if not path:
        return None
    try:
        c = sway_ipc.Ipc(path, timeout)
        try:
            return window_facts(c.request(sway_ipc.GET_TREE), app_id)
        finally:
            c.close()
    except Exception:           # noqa: BLE001 - "unknown", the caller keeps its state
        return None


class ChildRegistry:
    """Keeps the pids rp5deck starts (Firefox, wvkbd) in a small JSON file so after a hard crash the
    next start can stop the orphans. A leftover wvkbd sits over the panel eating taps, a leftover
    Firefox holds DSI-1 in BAR with no controls. A pid only gets signalled if its cmdline still
    has the marker saved with it, so a reused pid is never touched.
    """

    MARKERS = {WEB: "rp5deck-web", "osk": "wvkbd", "ytapp": "rp5deck-ytapp"}

    def __init__(self, path, read_cmdline=None, kill=os.kill if hasattr(os, "kill") else None):
        self.path = path
        self.read_cmdline = read_cmdline or self._read_cmdline
        self.kill = kill

    @staticmethod
    def _read_cmdline(pid):
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return None

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("could not write %s", self.path)

    def record(self, kind, pid):
        if not pid:
            return
        d = self._load()
        d[kind] = {"pid": int(pid), "marker": self.MARKERS.get(kind, kind)}
        self._save(d)

    def forget(self, kind):
        d = self._load()
        if kind in d:
            del d[kind]
            self._save(d)

    def entries(self):
        return self._load()

    def reap_stale(self):
        """Stops every saved process whose cmdline still has its marker and forgets them all. Returns
        [(kind, pid)] signalled.
        """
        done = []
        for kind, e in sorted(self._load().items()):
            try:
                pid, marker = int(e["pid"]), str(e["marker"])
            except (KeyError, TypeError, ValueError):
                continue
            cmd = self.read_cmdline(pid)
            if cmd and marker in cmd and self.kill is not None:
                try:
                    self.kill(pid, signal.SIGTERM)
                    done.append((kind, pid))
                except OSError:
                    pass
        self._save({})
        return done


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------
class WebApps:
    """Drives the Browser/Discord tiles (one shared Firefox). host is main.App: ui, mode, master,
    call_later/cancel and state_dirty. submit runs a callable on a worker and posts done(result)
    back to the UI thread. Everything else can be swapped for tests. self.app is None or WEB.
    """

    def __init__(self, host, submit, submit_search=None, browser=None, keyboard=None,
                 osk_mode=osk.DEFAULT_MODE, facts=read_window_facts,
                 registry=None, home_url=None, internal=sway_ipc.INTERNAL,
                 clock=time.monotonic):
        self.host = host
        self.submit = submit
        self.submit_search = submit_search  # unused now, no search worker call left
        self.browser = browser if browser is not None else browser_mod.Browser()
        self.keyboard = keyboard if keyboard is not None else osk.Wvkbd(output=internal)
        self.policy = osk.OskPolicy(osk_mode)
        self.facts = facts
        self.registry = registry
        self.home_url = home_url or browser_mod.HOME_URL
        self.internal = internal
        self.clock = clock
        # session
        self.app = None             # None | WEB
        self.label = None           # "Browser" | "Discord"
        self.phase = None           # None | STARTING | RUNNING
        self.gen = 0                # bumps on every session start / end
        self.seen_bar = False       # the window reached DSI-1 this session
        self.started_at = None
        self.last_end = None
        self.error = None
        self.last_focused = False
        self.osk_on = False
        self.osk_error = None
        self.parked = False  # the app tabs parked the window, still running
        self.poll_timer = self.map_timer = self.hint_timer = None
        self.poll_inflight = False
        self.polls = 0

    # -- small utilities ----------------------------------------------------
    @property
    def ui(self):
        return self.host.ui

    def _dirty(self):
        self.host.state_dirty = True

    def _call(self, fn, *args, done=None, fail=None, search=False):
        """Runs fn(*args) on the web worker (or the search worker) and hands the result to done() on the
        UI thread. If fn raises it's logged and done() gets `fail`, so a starting state always
        resolves instead of leaving the poll stuck for the session.
        """
        name = getattr(fn, "__name__", repr(fn))

        def safe(*a):
            try:
                return fn(*a)
            except Exception:       # noqa: BLE001
                log.exception("web worker call %s failed", name)
                return fail
        (self.submit_search if search else self.submit)(safe, *args, done=done)

    def _record(self, kind, pid):
        if self.registry is not None and pid:
            self.registry.record(kind, pid)

    def _forget(self, kind):
        if self.registry is not None:
            self.registry.forget(kind)

    def _launch(self, big, small="", small2="", action=None, title=None):
        self.ui.launch.show(title or self.label or "", big, small, small2, action)
        if self.ui.sheet != "launch":
            self.ui.open("launch")

    def _hint(self, text):
        bar = self.ui.bar
        if not bar.show_hint(text):
            return
        self.host.cancel(self.hint_timer)
        self.hint_timer = self.host.call_later(HINT_SECONDS, self._unhint)

    def _unhint(self):
        self.hint_timer = None
        self.ui.bar.clear_hint()

    def _cancel_timers(self):
        for name in ("poll_timer", "map_timer"):
            self.host.cancel(getattr(self, name))
            setattr(self, name, None)

    # -- sessions -------------------------------------------------------------
    def _begin(self, app, label):
        self.gen += 1
        self.app, self.label, self.phase = app, label, STARTING
        self.seen_bar = False
        self.started_at = self.clock()
        self.error = None
        self.last_focused = False
        self.policy.reset()
        self._cancel_timers()
        log.info("%s: starting (session %d)", label, self.gen)
        self._dirty()

    def end_session(self, reason, stop=True, keep_sheet=False):
        """Forgets the running app and queues its process stop after anything already queued for it,
        so a start in flight gets stopped too.
        """
        app = self.app
        if app is None:
            return
        log.info("%s: session %d ends (%s)", self.label, self.gen, reason)
        self.gen += 1
        self.app, self.label, self.phase = None, None, None
        self.seen_bar = False
        self.parked = False
        self.last_end = reason
        self._cancel_timers()
        self.policy.reset()
        if self.osk_on:
            self._set_osk(False)
        if stop:
            # SIGTERM now (the worker may be stuck in a 30 s page load), then
            # the orderly stop, queued after whatever that worker is doing
            try:
                self.browser.terminate_now()
            except Exception:       # noqa: BLE001 - the queued stop still runs
                log.exception("terminate_now")
            self._call(self._w_stop, app)
        self.ui.cc.set_bar_app(None)
        self.ui.bar.set_keys_lit(False)
        self._unhint()
        if not keep_sheet and self.ui.sheet == "launch":
            self.ui.close()
        self._dirty()

    def close_app(self):
        """The strip's Close, or Cancel while starting."""
        self.end_session("closed by the owner")

    # -- web (Browser, Discord) -------------------------------------------------
    def open_web(self, label, url=None):
        url = url or self.home_url
        if self.app == WEB:
            # the same Firefox: just go there
            self.label = label
            self._call(self._w_navigate, self.gen, url, done=self._navigated,
                       fail=(self.gen, False))
            if self.host.mode != sway_ipc.BAR:
                self._launch("Opening %s…" % label, "In the Firefox that is already running.",
                             action="Close Firefox")
            self._dirty()
            return
        self._begin(WEB, label)
        self._launch("Opening %s…" % label,
                     "Firefox opens on this screen in a few seconds.",
                     "Tap the page to type; the keyboard comes up by itself.",
                     action="Cancel")
        self._call(self._w_open_web, self.gen, url, done=self._web_opened,
                   fail=(self.gen, False, None))

    def _w_open_web(self, gen, url):
        ok = bool(self.browser.open(url))
        pid = self.browser.pid() if ok else None
        self._record(WEB, pid)
        return gen, ok, pid

    def _web_opened(self, res):
        gen, ok, pid = res
        if gen != self.gen:
            return                  # ended meanwhile; its stop was queued after this start
        if not ok:
            label = self.label
            self.end_session("Firefox did not start", keep_sheet=True)
            self.error = "Firefox did not start"
            self._launch("Firefox did not start",
                         "Its control channel never answered (%s)." % browser_mod.FIREFOX_BIN,
                         "Nothing is left running. See the rp5deck log.", action="Close",
                         title=label)
            return
        self.phase = RUNNING
        log.info("%s: Firefox running, pid %s", self.label, pid)
        self._arm_map_watch()
        self._arm_poll(0.0)
        self._dirty()

    def _w_navigate(self, gen, url):
        return gen, bool(self.browser.navigate(url))

    def _navigated(self, res):
        gen, ok = res
        if gen == self.gen and not ok:
            log.warning("%s: navigation failed", self.label)
            if self.host.mode == sway_ipc.BAR:
                self._hint("Page did not load")

    # -- the strip ----------------------------------------------------------------
    def action(self, name):
        log.info("app action %s (%s, %s)", name, self.label, self.phase)
        g = self.gen
        if name == "launch.action":
            if self.app is not None and not self.seen_bar:
                self.close_app()
            self.ui.close()
        elif name == "web.close":
            self.close_app()
        elif self.app == WEB and name == "web.back":
            self._call(lambda: (g, bool(self.browser.back())), done=self._navigated,
                       fail=(g, False))
        elif self.app == WEB and name == "web.reload":
            self._call(lambda: (g, bool(self.browser.reload())), done=self._navigated,
                       fail=(g, False))
        elif self.app == WEB and name == "web.home":
            self._call(self._w_navigate, g, self.home_url, done=self._navigated, fail=(g, False))
        elif self.app == WEB and name == "web.keys":
            want, hint = self.policy.toggle(self.last_focused)
            self._set_osk(want)
            if hint:
                self._hint(hint)
        self._dirty()

    # -- polling ----------------------------------------------------------------
    def _arm_poll(self, delay=None):
        if self.app is None:
            return
        if delay is None:
            delay = WEB_POLL
        self.host.cancel(self.poll_timer)
        self.poll_timer = self.host.call_later(delay, self._poll)

    def _poll(self):
        self.poll_timer = None
        if self.app is None or self.phase != RUNNING:
            return
        if not self.poll_inflight:
            self.poll_inflight = True
            probe = self.host.mode == sway_ipc.BAR and self.policy.enabled()
            self._call(self._w_poll, self.gen, self.app, probe, done=self._polled,
                       fail={"gen": self.gen, "failed": True})
        self._arm_poll()

    def _w_poll(self, gen, app, probe):
        out = {"gen": gen, "app": app}
        out["alive"] = self.browser.is_running()
        focused = editable = None
        if out["alive"] and probe:
            f = self.facts(WEB_APP_ID)
            if f is not None:
                focused = bool(f["focused"])
                if focused:
                    st = self.browser.text_input_state()
                    editable = None if st is None else st["editable"]
                else:
                    editable = False
        out["focused"], out["editable"] = focused, editable
        return out

    def _polled(self, r):
        self.poll_inflight = False
        self.polls += 1
        if r.get("failed") or r.get("gen") != self.gen or self.app is None:
            return
        if not r.get("alive"):
            self.end_session("Firefox exited")
            return
        focused, editable = r.get("focused"), r.get("editable")
        if focused is not None:
            self.last_focused = focused
        if self.host.mode == sway_ipc.BAR and focused is not None and \
                not (focused and editable is None):
            self._set_osk(self.policy.update(focused, editable))
        self._dirty()

    # -- the on-screen keyboard -----------------------------------------------------
    def _set_osk(self, on):
        on = bool(on) and self.app == WEB and self.host.mode == sway_ipc.BAR
        # ROCKNIX's own touch keyboard service killalls the shared wvkbd-mobintl binary on its own
        # poll, which can take ours with it while osk_on never changes. So rerun _w_osk whenever we
        # think it's on but the process isnt visible anymore.
        if on == self.osk_on and (not on or self.keyboard.visible()):
            self.ui.bar.set_keys_lit(on)
            return
        self.osk_on = on
        self.ui.bar.set_keys_lit(on)
        self._call(self._w_osk, self.gen, on, done=self._osk_done,
                   fail=(self.gen, on, False, "keyboard call raised (see log)"))
        self._dirty()

    def _w_osk(self, gen, on):
        if on:
            ok = self.keyboard.show()
            if ok:
                self._record("osk", self.keyboard.pid())
            return gen, on, ok, self.keyboard.error
        self.keyboard.hide()
        self._forget("osk")
        return gen, on, True, None

    def _osk_done(self, res):
        gen, on, ok, err = res
        if not on:
            return
        if not ok:
            self.osk_error = err
            log.warning("on-screen keyboard: %s", err)
            if gen == self.gen:
                self.osk_on = False
                self.policy.reset()
                self.ui.bar.set_keys_lit(False)
                self._hint("No on-screen keyboard")
            return
        if gen == self.gen and self.app == WEB:
            # the window just got shorter: keep the text field in sight
            self.host.call_later(0.4, lambda: self._call(self._w_scroll, gen))

    def _w_scroll(self, gen):
        if gen == self.gen and self.app == WEB:
            self.browser.scroll_focused_into_view()

    # -- the map watchdog ---------------------------------------------------------
    def _arm_map_watch(self):
        self.host.cancel(self.map_timer)
        self.map_timer = self.host.call_later(MAP_TIMEOUT, self._map_check)

    def _map_check(self):
        self.map_timer = None
        if self.app is None or self.seen_bar:
            return
        g = self.gen
        self._call(lambda: (g, self.facts(WEB_APP_ID)), done=self._map_facts, fail=(g, None))

    def _map_facts(self, res):
        gen, f = res
        if gen != self.gen or self.seen_bar or self.app is None:
            return
        what = "Firefox"
        if f and f["present"] and f["output"] != self.internal:
            self.error = "window on %s" % f["output"]
            big = "%s opened on the other screen" % what
            small = "It is on %s: the rule that moves it to this screen (092, B15) is missing." \
                % f["output"]
        elif f and not f["present"]:
            self.error = "no window"
            big = "%s is running but has no window" % what
            small = "Waited %d s for it to appear." % MAP_TIMEOUT
        else:
            self.error = "sway unreadable"
            big = "%s did not appear on this screen" % what
            small = "sway's window tree could not be read."
        log.warning("%s: %s (%s)", self.label, big, small)
        self._launch(big, small, action="Close Firefox")
        self._dirty()

    # -- display modes ----------------------------------------------------------------
    def on_mode(self, old, new, reason=None):
        """`reason` (main.App.on_mode's mode-change reason, None for older callers) tells which
        recognised window caused BAR. YouTube TV is its own window that can be shown while this one
        sits parked with self.app still WEB, so self.app alone doesnt say whose window is on screen.
        """
        if new == sway_ipc.BAR:
            mine = _reason_mentions(WEB_APP_ID, reason)
            if self.app is not None and mine:
                self.seen_bar = True
                self.parked = False  # its window is on screen again
                self.host.cancel(self.map_timer)
                self.map_timer = None
                self.ui.cc.set_bar_app(self.app, keys_offered=self.policy.enabled())
                self._arm_poll(0.0)
            elif self.app is None:
                self.ui.cc.set_bar_app(None)
            # else some other recognised window (YouTube TV) is shown. YtAppSession.on_mode() sets the bar
            # app, and this session's parked/seen_bar state stays as it is.
            return
        if self.osk_on:
            self._set_osk(False)            # never over ES's panel or an emulator's screen
        if self.parked:
            return  # parked by the app tabs, not closed
        if new == sway_ipc.FULL and self.app is not None and self.seen_bar:
            self.end_session("its window closed")

    # -- lifecycle ----------------------------------------------------------------------
    def reap_stale(self):
        if self.registry is None:
            return []
        done = self.registry.reap_stale()
        for kind, pid in done:
            log.warning("stopped an orphaned %s (pid %d) left by a previous run", kind, pid)
        return done

    def shutdown(self):
        """Stops every child now from the UI thread, since the worker may be stuck in a 30 s page load.
        Each call has a bounded wait and a SIGKILL fallback, so running them together at exit is safe.
        """
        self._cancel_timers()
        for name, fn in (("keyboard", self.keyboard.hide), ("firefox", self.browser.close)):
            try:
                fn()
            except Exception:       # noqa: BLE001
                log.exception("stopping %s", name)
        if self.registry is not None:
            try:
                self.registry.reap_stale()
            except Exception:       # noqa: BLE001
                log.exception("clearing the child registry")

    def state(self):
        return {"app": self.app, "label": self.label, "phase": self.phase, "session": self.gen,
                "seen_bar": self.seen_bar, "error": self.error, "last_end": self.last_end,
                "polls": self.polls,
                "osk": {"mode": self.policy.mode, "on": self.osk_on,
                        "starts": getattr(self.keyboard, "starts", None),
                        "error": self.osk_error, "firefox_focused": self.last_focused},
                "parked": self.parked}

    # -- _w_stop -----------------------------------------------------------------------
    def _w_stop(self, app):
        try:
            self.keyboard.hide()
            self._forget("osk")
            self.browser.close()
        finally:
            # even if stopping raised, a stale children.json entry would name a dead (and later reused)
            # pid for the orphan sweep
            self._forget(app)


YTAPP = "ytapp"
YTAPP_APP_ID = browser_mod.TV_APP_ID


class YtAppSession:
    """The YouTube TV (leanback) tile. It has its own Firefox, profile and Marionette port
    (browser.TV_PROFILE_DIR / TV_APP_ID / TV_MARIONETTE_PORT) because the user agent override it
    needs is global and would change Browser/Discord's too (Mozilla bug 1513574).

    Much smaller than WebApps: no keyboard (leanback has no text field), one process, and
    self.browser.is_running() is the only state that matters. host is main.App, browser can be
    swapped so tests never start a real Firefox.
    """

    # How often the web worker polls window.__rp5swipes while this tile's window is shown, never
    # while parked, hidden or closed. up/down/left/right is the raw finger direction, and
    # swipe_natural decides which key each one sends (see _direction_to_key).
    SWIPE_POLL_S = 0.15
    _NATURAL_INVERT = {"up": "down", "down": "up", "left": "right", "right": "left"}

    def __init__(self, host, submit=None, browser=None, registry=None, swipe_natural=True):
        self.host = host
        self.submit = submit or (lambda fn, *a, done=None: host.io_worker.submit(fn, *a,
                                                                                 done=done))
        self.browser = browser if browser is not None else browser_mod.Browser(
            profile_dir=browser_mod.TV_PROFILE_DIR, app_id=browser_mod.TV_APP_ID,
            marionette_port=browser_mod.TV_MARIONETTE_PORT, home_url=browser_mod.YOUTUBE_TV_URL,
            profile_files=browser_mod.TV_PROFILE_FILES)
        self.registry = registry
        self.starting = False
        self.error = None
        self.swipe_natural = bool(swipe_natural)  # youtube.tv_swipe_natural, can change live
        self.swipe_timer = None

    @property
    def ui(self):
        return self.host.ui

    def is_running(self):
        return self.browser.is_running()

    # -- lifecycle ------------------------------------------------------------------
    def open(self):
        """Safe to call twice. A cold start launches Firefox. If it's already running, parked or shown,
        window_switcher's switch already did everything else.
        """
        if self.browser.is_running() or self.starting:
            return
        self.starting = True
        self.submit(self.browser.open, browser_mod.YOUTUBE_TV_URL, done=self._opened)

    def _opened(self, ok):
        self.starting = False
        if ok:
            if self.registry is not None:
                self.registry.record(YTAPP, self.browser.pid())
        else:
            self.error = "Firefox did not start"
            log.warning("ytapp: %s", self.error)
        self.host.state_dirty = True

    def close(self):
        """The tv strip's Close (main.App.on_tv_close). Clears the bar app right away like
        WebApps.end_session() does, the window can take a moment longer to go (Marionette:Quit then
        the SIGTERM/SIGKILL fallback) but the strip's D-pad shouldnt hang around after the tap.
        """
        if not self.browser.is_running():
            return
        self.ui.cc.set_bar_app(None)
        self._cancel_swipe_poll()
        self.submit(self.browser.close, done=lambda _r=None: self._closed())

    def _closed(self):
        if self.registry is not None:
            self.registry.forget(YTAPP)
        self.host.state_dirty = True

    def shutdown(self):
        """Stops the tile's Firefox from the UI thread at app exit, like WebApps.shutdown(): the web
        worker may be stopping or stuck in a slow page load, so browser.close() runs here with its
        bounded waits and SIGKILL fallback instead of being queued on the worker. Without this a
        running ytapp is orphaned on a clean exit and holds DSI-1 in BAR until the next start's
        reap_stale finds it.
        """
        self._cancel_swipe_poll()
        try:
            self.browser.close()
        finally:
            if self.registry is not None:
                self.registry.forget(YTAPP)

    # -- the strip's D-pad --------------------------------------------------------------
    def action(self, name):
        """Every "tv.<key>" action from the strip's tv buttons, sent as a bare key press (leanback takes
        keys, not clicks, and send_key() is the only thing that touches this page). tv.close and
        tv.home are handled by main.App first, the guard here keeps a direct call from sending them
        to the page as bogus keys.
        """
        if not name.startswith("tv.") or name in ("tv.close", "tv.home"):
            return
        key = name[len("tv."):]
        self.submit(self.browser.send_key, key, done=None)

    # -- swipe to navigate ---------------------------------------------------------
    def _direction_to_key(self, direction):
        """The raw finger direction (from browser.SWIPE_POLL_SCRIPT) to a WEBDRIVER_KEYS name. Natural
        (the default, like scrolling a phone): swipe left brings the next item in from the right so
        it sends "right", swipe up shows what's below so it sends "down". Off sends the raw direction
        as is. Neither has been tried with a real finger on leanback yet (patches/YT4-NOTES.md).
        """
        if self.swipe_natural:
            direction = self._NATURAL_INVERT.get(direction, direction)
        return direction if direction in ("up", "down", "left", "right") else None

    def _arm_swipe_poll(self):
        self.host.cancel(self.swipe_timer)
        self.swipe_timer = self.host.call_later(self.SWIPE_POLL_S, self._swipe_poll)

    def _cancel_swipe_poll(self):
        if self.swipe_timer is not None:
            self.host.cancel(self.swipe_timer)
            self.swipe_timer = None

    def _swipe_poll(self):
        self.swipe_timer = None
        if not self.browser.is_running():
            return
        self.submit(self.browser.poll_swipes, done=self._swiped)

    def _swiped(self, directions):
        # Rearm first no matter what. A None (the page didnt answer, mid navigation or a crashed
        # content process) just skips this round's key presses.
        self._arm_swipe_poll()
        if not directions:
            return
        for direction in directions:
            key = self._direction_to_key(direction)
            if key is not None:
                self.action("tv.%s" % key)

    # -- display modes ----------------------------------------------------------------
    def on_mode(self, old, new, reason=None):
        """Same reason check as WebApps.on_mode(): it tells "my window is what BAR shows" apart from
        "Browser/Discord is shown while mine is parked". The swipe poll runs only while this tile's
        own window is what BAR shows and stops the moment anything else is true, so it never runs
        against a page nobody is looking at.
        """
        mine = new == sway_ipc.BAR and self.browser.is_running() and \
            _reason_mentions(YTAPP_APP_ID, reason)
        if mine:
            self.ui.cc.set_bar_app("tv", keys_offered=False)
            self._arm_swipe_poll()
            return
        self._cancel_swipe_poll()
        # leaving BAR because of this tile closes nothing by itself. Parking keeps it running like
        # Browser/Discord, and Close is always a tap, never a mode change.

    def state(self):
        return {"running": self.browser.is_running(), "pid": self.browser.pid(),
                "starting": self.starting, "error": self.error}
