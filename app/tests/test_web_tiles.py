#!/usr/bin/env python3
"""HF1 offline: the Browser / Discord / YouTube tiles (web_tiles.py) and
their wiring in main.App, built WITHOUT SDL (the test_companion pattern).
Firefox, mpv, wvkbd and sway are fakes; no process is launched except in
TestRegistryRealProcess (a python child, WSL / Linux only).

What is pinned down here:
  - tile -> launch sheet -> the app starts on the web worker; the window
    reaching DSI-1 (BAR) swaps the strip's battery / clock for the app's
    controls, volume stays
  - the session ends only when the window LEAVES DSI-1 after having been
    there (FULL after BAR), never while it is still starting
  - the on-screen keyboard: only while Firefox has focus; never outside BAR
  - one app at a time; late results of an ended session are dropped; a
    Cancel during a start still stops what that start launched
  - sway is only READ (GET_TREE); web_tiles never builds a sway command
"""
import argparse
import json
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import app_tabs  # noqa: E402
import bar_autohide  # noqa: E402
import browser  # noqa: E402
import config  # noqa: E402
import main  # noqa: E402
import screens  # noqa: E402
import sway_ipc  # noqa: E402
import ui  # noqa: E402
import web_tiles  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN  # noqa: E402
from test_companion import AppCase, FakeLayer, FakeVideo, FakeWl, SyncWorker, W, H  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
# expected warnings (a failed start, a missing keyboard) stay out of the test
# output; assertLogs still sees them
import logging  # noqa: E402
logging.getLogger("rp5deck.web").addHandler(logging.NullHandler())


def load_fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeBrowser:
    def __init__(self):
        self.calls = []
        self.running = False
        self.open_ok = True
        self.editable = {"editable": False, "focus": True}
        self.swipe_queue = []      # YT4: poll_swipes()'s own queue, a test pushes onto it

    def open(self, url=None):
        self.calls.append(("open", url))
        self.running = self.open_ok
        return self.open_ok

    def pid(self):
        return 4242 if self.running else None

    def is_running(self):
        return self.running

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return True

    def back(self):
        self.calls.append(("back",))
        return True

    def reload(self):
        self.calls.append(("reload",))
        return True

    def close(self):
        self.calls.append(("close",))
        self.running = False

    def terminate_now(self):
        if self.running:                    # the real one is a no-op without a process
            self.calls.append(("terminate_now",))
            self.running = False

    def text_input_state(self):
        self.calls.append(("probe",))
        return self.editable

    def scroll_focused_into_view(self):
        self.calls.append(("scroll",))
        return True

    def send_key(self, key):
        self.calls.append(("send_key", key))
        return True

    def poll_swipes(self):
        """YT4: each call returns (and clears) self.swipe_queue - a test
        pushes onto it directly, the same shape Browser.poll_swipes()
        returns (a list of direction strings, or None for "page did not
        answer")."""
        self.calls.append(("poll_swipes",))
        out, self.swipe_queue = self.swipe_queue, []
        return out

    def names(self):
        return [c[0] for c in self.calls]


class FakeKeyboard:
    def __init__(self):
        self.shown = False
        self.calls = []
        self.ok = True
        self.error = None
        self.starts = 0

    def show(self):
        self.calls.append("show")
        if not self.ok:
            self.error = "cannot start wvkbd-mobintl: missing"
            return False
        if not self.shown:
            self.starts += 1
        self.shown = True
        return True

    def hide(self):
        self.calls.append("hide")
        self.shown = False

    def pid(self):
        return 6161 if self.shown else None

    def visible(self):
        return self.shown

    def killed_externally(self):
        """W: simulate ROCKNIX's own keyboard service `killall`ing the
        shared wvkbd-mobintl binary without rp5deck's osk_on flag noticing -
        `shown` drops (a poll of the real process would too) but nothing
        calls hide()/show()."""
        self.shown = False


class Facts:
    """Stands in for read_window_facts(): what sway would say."""

    def __init__(self):
        self.focused = False
        self.present = True
        self.output = "DSI-1"
        self.calls = []

    def __call__(self, app_id):
        self.calls.append(app_id)
        return {"present": self.present, "output": self.output,
                "focused": self.focused, "focused_app": app_id if self.focused else None}


class DeferredWorker:
    """Queues calls; run() executes them in order (FIFO, like main.Worker)."""

    def __init__(self, post):
        self.post = post
        self.q = []

    def submit(self, fn, *args, done=None):
        self.q.append((fn, args, done))

    def run(self):
        while self.q:
            fn, args, done = self.q.pop(0)
            res = fn(*args)
            if done is not None:
                self.post(done, res)

    def stop(self, timeout=0):
        pass


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestWindowFacts(unittest.TestCase):
    def test_focused_firefox_on_dsi1(self):
        f = web_tiles.window_facts(
            load_fixture("sway-tree-rp5deck-web-focused-SYNTHETIC-from-real-capture.json"),
            "rp5deck-web")
        self.assertEqual(f, {"present": True, "output": "DSI-1", "focused": True,
                             "focused_app": "rp5deck-web"})

    def test_video_window_present_but_es_focused(self):
        f = web_tiles.window_facts(load_fixture("sway-tree-BAR-SYNTHETIC.json"), "rp5deck-yt")
        self.assertTrue(f["present"])
        self.assertEqual(f["output"], "DSI-1")
        self.assertFalse(f["focused"])
        self.assertEqual(f["focused_app"], "emulationstation")

    # test_absent removed: depended on
    # tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json, dropped
    # from the public release (see DROPPED-FIXTURES-list.txt).

    def test_live_read_sends_only_get_tree(self):
        sent = []
        tree = load_fixture("sway-tree-rp5deck-web-focused-SYNTHETIC-from-real-capture.json")

        class FakeIpc:
            def __init__(self, path, timeout=3.0):
                pass

            def request(self, mtype, payload=b""):
                sent.append(mtype)
                return tree

            def close(self):
                pass
        with mock.patch.object(sway_ipc, "find_socket", return_value="/x"), \
                mock.patch.object(sway_ipc, "Ipc", FakeIpc):
            f = web_tiles.read_window_facts("rp5deck-web")
        self.assertTrue(f["focused"])
        self.assertEqual(sent, [sway_ipc.GET_TREE])
        self.assertIn(sway_ipc.GET_TREE, sway_ipc.READ_ONLY_TYPES)

    def test_unreadable_sway_is_none(self):
        with mock.patch.object(sway_ipc, "find_socket", return_value=None):
            self.assertIsNone(web_tiles.read_window_facts("rp5deck-web"))
        with mock.patch.object(sway_ipc, "find_socket", return_value="/x"), \
                mock.patch.object(sway_ipc, "Ipc", side_effect=OSError("refused")):
            self.assertIsNone(web_tiles.read_window_facts("rp5deck-web"))

    def test_web_tiles_never_builds_a_sway_command(self):
        with open(web_tiles.__file__, encoding="utf-8") as f:
            src = f.read()
        for bad in ("RUN_COMMAND", "swaymsg", "focus_es", "run_command"):
            self.assertNotIn(bad, src)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="rp5deck-reg-")
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.cmd = {}
        self.killed = []
        self.reg = web_tiles.ChildRegistry(os.path.join(self.d, "run", "children.json"),
                                           read_cmdline=self.cmd.get,
                                           kill=lambda pid, sig: self.killed.append((pid, sig)))

    def test_orphans_with_their_marker_are_stopped(self):
        self.reg.record("web", 11)
        self.reg.record("osk", 12)
        self.reg.record("yt", 13)
        self.cmd[11] = "/storage/rp5deck-firefox/firefox/firefox --profile p --marionette " \
                       "--name rp5deck-web --no-remote"
        self.cmd[12] = "wvkbd-mobintl -L 360 --output DSI-1"
        # 13 is gone
        import signal
        done = self.reg.reap_stale()
        self.assertEqual(sorted(done), [("osk", 12), ("web", 11)])
        self.assertEqual(sorted(self.killed), [(11, signal.SIGTERM), (12, signal.SIGTERM)])
        self.assertEqual(self.reg.entries(), {})

    def test_a_reused_pid_is_never_signalled(self):
        self.reg.record("web", 21)
        self.cmd[21] = "/usr/bin/emulationstation"
        self.assertEqual(self.reg.reap_stale(), [])
        self.assertEqual(self.killed, [])

    def test_forget_and_bad_file(self):
        self.reg.record("yt", 31)
        self.reg.forget("yt")
        self.assertEqual(self.reg.entries(), {})
        with open(self.reg.path, "w") as f:
            f.write("{not json")
        self.assertEqual(self.reg.reap_stale(), [])
        self.reg.record("web", 0)                       # no pid: nothing recorded
        self.assertEqual(self.reg.entries(), {})


@unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc (WSL, the device)")
class TestRegistryRealProcess(unittest.TestCase):
    def test_reaps_a_real_orphan_by_marker(self):
        import subprocess
        import tempfile
        d = tempfile.mkdtemp(prefix="rp5deck-reg-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  "wvkbd-mobintl", "-L", "360"])
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        for p in (child, bystander):
            self.addCleanup(p.wait, 5)
            self.addCleanup(p.kill)
        reg = web_tiles.ChildRegistry(os.path.join(d, "children.json"))
        reg.record("osk", child.pid)
        reg.record("web", bystander.pid)                 # no rp5deck-web in its cmdline
        done = reg.reap_stale()
        self.assertEqual(done, [("osk", child.pid)])
        self.assertEqual(child.wait(5), -15)
        time.sleep(0.2)
        self.assertIsNone(bystander.poll())


# ---------------------------------------------------------------------------
# main.App with the tiles
# ---------------------------------------------------------------------------
class TilesCase(AppCase):
    def make_web_app(self, osk_mode="auto", deferred=False):
        app = main.App(argparse.Namespace(seconds=0, output=None))
        app.title = "Test Device Command Center"
        app.layer = FakeLayer()
        app.wl = FakeWl()
        app.mode = FULL
        app.video = FakeVideo()
        for name in ("media_worker", "io_worker", "audio_worker", "search_worker"):
            setattr(app, name, SyncWorker(app.post))
        app.web_worker = DeferredWorker(app.post) if deferred else SyncWorker(app.post)
        self.br, self.kb, self.facts = FakeBrowser(), FakeKeyboard(), Facts()
        app.web_parts = {"browser": self.br, "keyboard": self.kb,
                         "facts": self.facts, "osk_mode": osk_mode}
        # YT3: a fake Firefox for the YouTube App tile too, so tests can tap
        # "home.ytapp" / exercise the tv strip without launching a real
        # process (FakeBrowser's open/close/pid/send_key already match what
        # YtAppSession needs - same class TestYouTubeTvTile uses directly).
        self.ytbr = FakeBrowser()
        app.ytapp_parts = {"browser": self.ytbr}
        app.build_ui(W, H)
        app.post.drain()
        return app

    def open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "cc")

    def to_bar(self, app, app_id=None):
        # sway_ipc.compute_mode()'s real reason names the visible app_ids
        # (device log: "rp5deck window on DSI-1: rp5deck-web"); W2b's
        # on_mode() matches on that list, so the test must send it too
        if app_id is None:
            app_id = web_tiles.WEB_APP_ID
        app.on_mode(BAR, "rp5deck window on DSI-1: %s" % app_id)
        app.ui.set_size(W, screens.BAR_H)          # what the layer configure does
        app.post.drain()

    def to_full(self, app):
        app.on_mode(FULL, "no window on DSI-1")
        app.ui.set_size(W, H)
        app.post.drain()

    def poll(self, app):
        app.web._poll()
        app.post.drain()


class TestBrowserTile(TilesCase):
    def test_tiles_are_no_longer_coming_soon(self):
        app = self.make_web_app()
        subs = {t.name: t.subtitle for t in app.ui.home.tiles}
        for name in ("home.browser", "home.discord"):
            self.assertNotIn("soon", subs[name].lower())

    def test_browser_opens_firefox_and_the_strip_gets_its_controls(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.assertEqual(self.br.calls[0], ("open", browser.HOME_URL))
        self.assertEqual(app.ui.sheet, "launch")
        self.assertEqual(app.web.phase, web_tiles.RUNNING)
        self.assertEqual(app.web.registry.entries()["web"]["pid"], 4242)
        self.assertIn("launch.action", app.ui.targets())         # Cancel while it opens
        self.to_bar(app)
        t = app.ui.targets()
        for name in ("bar.mute", "bar.slider", "bar.web.back", "bar.web.reload", "bar.web.home",
                     "bar.web.keys", "bar.web.close"):
            self.assertIn(name, t)
        for name in ("bar.battery", "bar.yt.pause", "home.browser", "launch.action"):
            self.assertNotIn(name, t)
        # all inside the strip, none overlapping, all finger-sized
        rects = [v for k, v in t.items() if not k.endswith(".track")]
        for x, y, w, h in rects:
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y + h, screens.BAR_H)
            self.assertLessEqual(x + w, W)
        for name in ("bar.web.back", "bar.web.reload", "bar.web.home", "bar.web.keys",
                     "bar.web.close", "bar.mute"):
            self.assertGreaterEqual(min(t[name][2:]), 110, name)
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap = (a[0] < b[0] + b[2] and b[0] < a[0] + a[2])
                self.assertFalse(overlap, (a, b))
        self.assertGreaterEqual(t["bar.slider"][2], 600)          # volume stays usable
        # the layer geometry is still B1's strip (the keyboard stacks above it)
        self.assertEqual(app.layer.geometry[1], (0, screens.BAR_H))
        self.assertEqual(app.layer.geometry[2], screens.BAR_H)

    def test_back_reload_home_close(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        self.tap(app, "bar.web.back")
        self.tap(app, "bar.web.reload")
        self.tap(app, "bar.web.home")
        self.assertEqual(self.br.calls[1:], [("back",), ("reload",), ("navigate", browser.HOME_URL)])
        self.tap(app, "bar.web.close")
        self.assertEqual(self.br.calls[-1], ("close",))
        self.assertIsNone(app.web.app)
        self.assertNotIn("web", app.web.registry.entries())
        self.assertIn("bar.battery", app.ui.targets())            # the plain strip again

    def test_discord_uses_the_same_firefox(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.discord")
        self.assertEqual(self.br.calls, [("open", browser.DISCORD_URL)])
        self.assertEqual(app.web.label, "Discord")
        app.close_sheet()
        self.tap(app, "home.browser")                              # already running: navigate
        self.assertEqual(self.br.calls[-1], ("navigate", browser.HOME_URL))
        self.assertEqual(self.br.names().count("open"), 1)

    def test_session_ends_when_the_window_leaves_dsi1(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        self.to_full(app)
        self.assertIsNone(app.web.app)
        self.assertEqual(self.br.calls[-1], ("close",))
        self.assertEqual(app.web.last_end, "its window closed")
        self.assertNotEqual(app.ui.sheet, "launch")               # no stale "Opening…"

    def test_full_while_still_starting_does_not_end_it(self):
        # Firefox takes seconds to map its window; FULL before the first BAR
        # is "not there yet", not "closed" (killing it here would make the
        # Browser tile unusable).
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        app.on_mode(HIDDEN, "foreign")                            # anything but BAR ...
        self.to_full(app)                                         # ... then FULL again
        self.assertEqual(app.web.app, web_tiles.WEB)
        self.assertNotIn("close", self.br.names())

    def test_hidden_keeps_the_session(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        app.on_mode(HIDDEN, "foreign window on DSI-1")
        self.assertEqual(app.web.app, web_tiles.WEB)
        self.to_bar(app)
        self.assertIn("bar.web.close", app.ui.targets())

    def test_firefox_that_exits_by_itself_ends_the_session(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        self.br.running = False                                   # closed from its own UI
        self.poll(app)
        self.assertIsNone(app.web.app)
        self.assertEqual(app.web.last_end, "Firefox exited")

    def test_failed_start_says_so_and_leaves_nothing(self):
        app = self.make_web_app()
        self.br.open_ok = False
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.assertIsNone(app.web.app)
        self.assertEqual(app.ui.sheet, "launch")
        self.assertEqual(app.ui.launch.big.text, "Firefox did not start")
        self.assertEqual(app.ui.launch.title.text, "Browser")
        self.assertEqual(self.br.calls[-1], ("close",))
        self.tap(app, "launch.action")
        self.assertIsNone(app.ui.sheet)

    def test_window_on_the_wrong_screen_is_reported(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.facts.output = "DP-1"
        app.web._map_check()
        app.post.drain()
        self.assertEqual(app.ui.sheet, "launch")
        self.assertIn("other screen", app.ui.launch.big.text)
        self.assertIn("DP-1", app.ui.launch.small.text)
        self.tap(app, "launch.action")                            # Close Firefox
        self.assertEqual(self.br.calls[-1], ("close",))
        self.assertIsNone(app.web.app)

    def test_cancel_during_start_stops_what_the_start_launched(self):
        app = self.make_web_app(deferred=True)
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.assertEqual(self.br.calls, [])                       # still queued
        self.tap(app, "launch.action")                            # Cancel
        self.assertIsNone(app.web.app)
        app.web_worker.run()                                      # open runs, then close
        app.post.drain()
        self.assertEqual(self.br.names(), ["open", "close"])
        self.assertIsNone(app.web.app)                            # the late result was dropped
        self.assertFalse(self.br.running)

    def test_close_during_a_slow_page_load_signals_firefox_at_once(self):
        app = self.make_web_app(deferred=True)
        self.open_cc(app)
        self.tap(app, "home.browser")
        app.web_worker.run()
        app.post.drain()
        self.to_bar(app)
        self.tap(app, "bar.web.reload")          # stands for a load still in progress
        self.tap(app, "bar.web.close")
        # before the worker gets to anything queued: SIGTERM already went out
        self.assertEqual(self.br.names(), ["open", "terminate_now"])
        app.web_worker.run()
        app.post.drain()
        self.assertEqual(self.br.names(), ["open", "terminate_now", "reload", "close"])
        self.assertIsNone(app.web.app)

    def test_worker_exception_still_resolves_the_poll(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        self.br.is_running = lambda: 1 / 0
        with self.assertLogs("rp5deck.web", level="ERROR") as cm:
            self.poll(app)
        self.assertIn("ZeroDivisionError", " ".join(cm.output))
        self.assertFalse(app.web.poll_inflight)                   # not stuck for the session
        self.assertEqual(app.web.app, web_tiles.WEB)

    def test_state_json_has_the_apps_section(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        st = json.loads(json.dumps(app.state(), default=str))
        self.assertEqual(st["apps"]["app"], "web")
        self.assertEqual(st["apps"]["osk"]["mode"], "auto")

    def test_command_center_is_not_auto_closed_under_the_strip(self):
        app = self.make_web_app()
        app.pull.auto_close_timeout_s = 2
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.assertEqual(app.ui.sheet, "launch")
        self.to_bar(app)
        app.pull._deadline = time.monotonic() - 0.01
        app._cc_tick()
        self.assertTrue(app.pull.is_open())
        self.assertEqual(app.ui.sheet, "launch")

    def test_shutdown_stops_every_child(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        app.web.shutdown()
        self.assertEqual(self.br.calls[-1], ("close",))
        self.assertIn("hide", self.kb.calls)


class FakeYtAppCC:
    def __init__(self):
        self.app = None
        self.keys_offered = None

    def set_bar_app(self, app, keys_offered=None):
        self.app = app
        self.keys_offered = keys_offered


class FakeYtAppHost:
    """Stands in for main.App - just enough for YtAppSession: ui.cc,
    state_dirty, and (YT4) the call_later/cancel timer pair the swipe poll
    uses. No real worker thread: submit() is injected synchronously by each
    test instead (YtAppSession.__init__'s own submit= param); timers are
    fired manually (fire_timers()), never a real clock."""

    def __init__(self):
        self.ui = type("UI", (), {"cc": FakeYtAppCC()})()
        self.state_dirty = False
        self.timers = []          # [delay, fn, cancelled]

    def call_later(self, delay, fn):
        h = [delay, fn, False]
        self.timers.append(h)
        return h

    def cancel(self, h):
        if h is not None:
            h[2] = True

    def fire_timers(self):
        """Run every un-cancelled timer queued so far, in order - a swipe
        poll that re-arms itself queues a new one each time this is called,
        same idiom as bar_autohide's own FakeHost.fire_due()."""
        due, self.timers = self.timers, []
        for delay, fn, cancelled in due:
            if not cancelled:
                fn()


def sync_submit(fn, *a, done=None):
    result = fn(*a)
    if done is not None:
        done(result)


class TestReasonMentions(unittest.TestCase):
    """web_tiles._reason_mentions() - the on_mode() disambiguation both
    WebApps and YtAppSession use so a BAR transition caused by ONE
    recognised window is never mistaken for another's (test day's real
    device bug class: a Discord/Browser session left "parked" must not be
    reset just because the YouTube TV tile's window is what actually
    mapped)."""

    def test_none_reason_means_pre_w2b_caller_always_matches(self):
        self.assertTrue(web_tiles._reason_mentions(browser.APP_ID, None))
        self.assertTrue(web_tiles._reason_mentions(browser.TV_APP_ID, None))

    def test_matches_a_named_app_id(self):
        reason = "rp5deck window on DSI-1: rp5deck-web"
        self.assertTrue(web_tiles._reason_mentions("rp5deck-web", reason))

    def test_does_not_match_an_unrelated_app_id(self):
        reason = "rp5deck window on DSI-1: rp5deck-web"
        self.assertFalse(web_tiles._reason_mentions("rp5deck-yt", reason))

    def test_yt_app_id_is_a_prefix_of_tv_app_id_but_must_not_match_it(self):
        """rp5deck-yt is literally a prefix of rp5deck-ytapp - a naive
        substring test (`app_id in reason`) would wrongly say yes here."""
        reason = "rp5deck window on DSI-1: rp5deck-ytapp"
        self.assertFalse(web_tiles._reason_mentions("rp5deck-yt", reason))
        self.assertTrue(web_tiles._reason_mentions("rp5deck-ytapp", reason))

    def test_matches_one_of_several_comma_joined_windows(self):
        reason = "rp5deck window on DSI-1: rp5deck-web, rp5deck-ytapp"
        self.assertTrue(web_tiles._reason_mentions("rp5deck-web", reason))
        self.assertTrue(web_tiles._reason_mentions("rp5deck-ytapp", reason))
        self.assertFalse(web_tiles._reason_mentions("rp5deck-yt", reason))


class TestYouTubeTvTile(unittest.TestCase):
    """W2b (device failure, 24 Sep evening): the leanback/TV web app moved
    to its OWN Firefox instance/profile/app_id (web_tiles.YtAppSession),
    never a label under WebApps' shared "web" Firefox any more - a global
    user-agent override (the only kind Firefox still honours, see
    browser.py's own comment) cannot share a profile with Browser/Discord.
    Standalone unit tests (no main.App/screens.py needed) - patches/W2b-
    main.py wires a real YtAppSession into main.App.ytapp; patches/
    W2-screens.patch's tv_* Bar buttons are exercised end to end by a
    scratch-tree check script (see W2-NOTES.md), not here."""

    def make(self, browser_ok=True, swipe_natural=True):
        host = FakeYtAppHost()
        fb = FakeBrowser()
        fb.open_ok = browser_ok
        session = web_tiles.YtAppSession(host, submit=sync_submit, browser=fb,
                                         swipe_natural=swipe_natural)
        return host, fb, session

    def test_open_launches_the_dedicated_profile_url(self):
        host, fb, session = self.make()
        session.open()
        self.assertEqual(fb.calls[0], ("open", browser.YOUTUBE_TV_URL))
        self.assertTrue(fb.running)

    def test_open_is_a_no_op_while_already_running(self):
        host, fb, session = self.make()
        session.open()
        session.open()
        self.assertEqual(fb.names().count("open"), 1)

    def test_open_failure_is_reported_and_clears_starting(self):
        host, fb, session = self.make(browser_ok=False)
        session.open()
        self.assertFalse(session.starting)
        self.assertIn("did not start", session.error)

    def test_close_stops_a_running_instance_only(self):
        host, fb, session = self.make()
        session.close()
        self.assertNotIn("close", fb.names())          # never running: nothing to stop
        session.open()
        session.close()
        self.assertEqual(fb.calls[-1], ("close",))

    def test_dpad_sends_every_documented_key(self):
        host, fb, session = self.make()
        session.open()
        for key in ("left", "up", "right", "down", "ok", "back"):
            session.action("tv.%s" % key)
            self.assertEqual(fb.calls[-1], ("send_key", key))

    def test_dpad_ignores_non_tv_actions(self):
        host, fb, session = self.make()
        session.open()
        before = list(fb.calls)
        session.action("web.close")
        self.assertEqual(fb.calls, before)

    def test_dpad_ignores_close_and_home_even_if_called_directly(self):
        """YT3: main.App.on_app_action intercepts "tv.close"/"tv.home"
        before ever reaching here (they are not D-pad keys) - guarded again
        in this method itself so a direct call can never send them to the
        page as bogus key names."""
        host, fb, session = self.make()
        session.open()
        before = list(fb.calls)
        session.action("tv.close")
        session.action("tv.home")
        self.assertEqual(fb.calls, before)

    def test_close_clears_the_bar_app_immediately(self):
        """YT3: like WebApps.end_session(), the strip must stop showing the
        D-pad the moment Close is tapped, not only once Firefox has actually
        quit (which can take a moment - Marionette:Quit, then the SIGTERM/
        SIGKILL fallback)."""
        host, fb, session = self.make()
        session.open()
        host.ui.cc.app = "tv"
        session.close()
        self.assertIsNone(host.ui.cc.app)
        self.assertEqual(fb.calls[-1], ("close",))

    def test_bar_shows_tv_only_while_running_and_reason_says_so(self):
        host, fb, session = self.make()
        session.open()
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-ytapp")
        self.assertEqual(host.ui.cc.app, "tv")
        self.assertEqual(host.ui.cc.keys_offered, False)

    def test_bar_ignores_a_reason_naming_a_different_app(self):
        """Browser/Discord's window mapped, not this tile's - even though
        this session is also "running" (parked in the background),
        WebApps.on_mode() owns this transition, not YtAppSession."""
        host, fb, session = self.make()
        session.open()
        host.ui.cc.app = "unset"
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-web")
        self.assertEqual(host.ui.cc.app, "unset")

    def test_bar_does_nothing_while_not_running(self):
        host, fb, session = self.make()
        host.ui.cc.app = "unset"
        session.on_mode("FULL", sway_ipc.BAR, reason=None)
        self.assertEqual(host.ui.cc.app, "unset")

    def test_state_reports_liveness(self):
        host, fb, session = self.make()
        self.assertFalse(session.state()["running"])
        session.open()
        self.assertTrue(session.state()["running"])

    # -- YT4: swipe-to-navigate ----------------------------------------------------
    def test_mine_bar_entry_arms_the_swipe_poll(self):
        host, fb, session = self.make()
        session.open()
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-ytapp")
        self.assertEqual(len(host.timers), 1)

    def _live_timers(self, host):
        return [t for t in host.timers if not t[2]]

    def test_leaving_bar_cancels_the_swipe_poll(self):
        host, fb, session = self.make()
        session.open()
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-ytapp")
        session.on_mode(sway_ipc.BAR, "FULL", reason="no window on DSI-1")
        self.assertEqual(self._live_timers(host), [])

    def test_another_app_shown_in_bar_cancels_the_swipe_poll(self):
        """The window is parked (Browser/Discord shown instead) - mine is
        False even though this session is still "running" in the
        background; polling a page rp5deck is not looking at would be
        pointless (and would send key presses to a hidden tab)."""
        host, fb, session = self.make()
        session.open()
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-ytapp")
        session.on_mode(sway_ipc.BAR, sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-web")
        self.assertEqual(self._live_timers(host), [])

    def test_close_cancels_the_swipe_poll_immediately(self):
        host, fb, session = self.make()
        session.open()
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-ytapp")
        session.close()
        self.assertEqual(self._live_timers(host), [])

    def test_poll_reads_swipes_and_sends_the_dpad_keys(self):
        host, fb, session = self.make(swipe_natural=False)
        session.open()
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-ytapp")
        fb.swipe_queue = ["left", "up"]
        host.fire_timers()
        self.assertEqual([c for c in fb.calls if c[0] == "send_key"],
                         [("send_key", "left"), ("send_key", "up")])
        self.assertEqual(len(host.timers), 1)     # re-armed for the next poll

    def test_poll_keeps_polling_when_the_page_does_not_answer(self):
        host, fb, session = self.make()
        session.open()
        session.on_mode("FULL", sway_ipc.BAR, reason="rp5deck window on DSI-1: rp5deck-ytapp")
        fb.swipe_queue = None
        before = list(fb.calls)
        host.fire_timers()
        self.assertEqual([c for c in fb.calls if c[0] == "send_key"], [])
        self.assertGreater(len(fb.calls), len(before))    # still polled, just sent nothing
        self.assertEqual(len(host.timers), 1)

    def test_natural_direction_inverts_the_axis(self):
        host, fb, session = self.make(swipe_natural=True)
        pairs = [("up", "down"), ("down", "up"), ("left", "right"), ("right", "left")]
        for raw, key in pairs:
            self.assertEqual(session._direction_to_key(raw), key, raw)

    def test_literal_direction_is_unchanged(self):
        host, fb, session = self.make(swipe_natural=False)
        for d in ("up", "down", "left", "right"):
            self.assertEqual(session._direction_to_key(d), d)

    def test_unknown_direction_is_ignored(self):
        host, fb, session = self.make()
        self.assertIsNone(session._direction_to_key("diagonal"))


class TestYouTubeAppTvStrip(TilesCase):
    """YT3 (owner feedback, 24 Sep: "no way to kill the app or tab out of
    it"): the tv strip's Close/Home buttons (main.App.on_tv_close/
    on_tv_home) and its BAR-only swipe-up (main.App.on_gesture/
    gestures.wants), driven through the real main.App (a fake Firefox for
    the YouTube App tile - self.ytbr, wired by TilesCase.make_web_app)."""

    def to_tv_bar(self, app):
        app.on_mode(BAR, "rp5deck window on DSI-1: %s" % browser.TV_APP_ID)
        app.ui.set_size(W, screens.BAR_H)
        app.post.drain()

    def opened(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.ytapp")
        self.to_tv_bar(app)
        self.assertEqual(app.ui.bar.app, "tv")
        return app

    def test_close_ends_the_session_and_returns_to_full(self):
        app = self.opened()
        self.tap(app, "bar.tv.close")
        self.assertEqual(self.ytbr.calls[-1], ("close",))
        self.assertIsNone(app.ui.bar.app)          # immediate feedback (like WebApps' own Close)
        self.to_full(app)                          # sway reports the window is gone
        self.assertEqual(app.ui.showing, "cc")
        self.assertIsNone(app.ui.sheet)             # the normal, full Command Center home

    def test_home_parks_the_session_and_shows_the_command_center(self):
        app = self.opened()
        switches_before = app.tabs.switches
        self.tap(app, "bar.tv.home")
        # proves tabs.select(app_tabs.CC) actually ran a switch - not just
        # that the CC happened to already be open from self.open_cc() above
        self.assertGreater(app.tabs.switches, switches_before)
        self.assertEqual(app.tabs.last["target"], app_tabs.wsw.INTERNAL)
        self.to_full(app)                            # sway reports the window has left DSI-1
        self.assertTrue(self.ytbr.running)          # parked, not closed
        self.assertNotIn("close", self.ytbr.names())
        self.assertEqual(app.ui.showing, "cc")
        self.assertIsNone(app.ui.sheet)

    def test_swipe_up_on_the_tv_strip_opens_the_command_center(self):
        app = self.opened()
        self.assertTrue(app.gestures.wants("swipe_up"))
        self.assertTrue(app.gestures.wants("swipe_up_from_bottom"))
        app.on_gesture("swipe_up")
        app.post.drain()
        self.to_full(app)
        self.assertTrue(self.ytbr.running)           # parked, not closed
        self.assertNotIn("close", self.ytbr.names())
        self.assertEqual(app.ui.showing, "cc")

    def test_swipe_up_is_not_offered_for_other_bar_apps(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        self.assertFalse(app.gestures.wants("swipe_up"))
        self.assertFalse(app.gestures.wants("swipe_up_from_bottom"))

    def test_close_cancels_the_auto_hide_timer(self):
        app = self.opened()
        app.ytauto._on_timeout()                     # the strip auto-hid once
        self.assertEqual(app.ytauto.state, bar_autohide.HIDDEN)
        self.tap(app, "bar.tv.close")
        self.assertFalse(app.ytauto.active)           # stopped outright, not just re-armed

    def test_home_cancels_the_auto_hide_timer(self):
        app = self.opened()
        app.ytauto._on_timeout()
        self.assertEqual(app.ytauto.state, bar_autohide.HIDDEN)
        self.tap(app, "bar.tv.home")
        self.assertFalse(app.ytauto.active)

    def test_tv_tab_still_listed_after_home(self):
        app = self.opened()
        self.tap(app, "bar.tv.home")
        self.to_full(app)
        # sway is unreadable in this test environment (no real compositor),
        # so build_tabs() falls back to availability() alone; force "can
        # start" the way a real Firefox binary on the device would, so the
        # YouTube App tab's presence is what is actually being proven here.
        app.tabs.avail_fn = lambda: {"web": True}
        app.tabs.refresh()
        app.post.drain()
        self.assertIn(app_tabs.YOUTUBE_TV, [t["id"] for t in app.tabs.tabs()])


class TestKeyboardForFirefox(TilesCase):
    def browsing(self, osk_mode="auto"):
        app = self.make_web_app(osk_mode)
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        return app

    def test_shows_for_a_focused_text_field_and_scrolls_it_into_view(self):
        app = self.browsing()
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        self.assertTrue(self.kb.shown)
        self.assertTrue(app.ui.bar.web_keys.lit)
        self.assertEqual(app.web.registry.entries()["osk"]["pid"], 6161)
        app._run_timers(time.monotonic() + 0.5)                   # the scroll-into-view
        app.post.drain()
        self.assertIn(("scroll",), self.br.calls)
        self.br.editable = {"editable": False, "focus": True}
        self.poll(app)
        self.assertTrue(self.kb.shown)                            # one miss: stays
        self.poll(app)
        self.assertFalse(self.kb.shown)
        self.assertFalse(app.ui.bar.web_keys.lit)

    def test_never_shows_while_firefox_is_not_focused(self):
        app = self.browsing()
        self.facts.focused = False                                # ES / the game has focus
        self.br.editable = {"editable": True, "focus": False}
        for _ in range(4):
            self.poll(app)
        self.assertFalse(self.kb.shown)
        self.assertEqual(self.kb.calls, [])
        self.assertNotIn(("probe",), self.br.calls)               # not even asked

    def test_keyboard_button_needs_focus_first(self):
        app = self.browsing()
        self.tap(app, "bar.web.keys")
        self.assertFalse(self.kb.shown)
        self.assertTrue(app.ui.bar.hinting)
        self.assertEqual(app.ui.bar.hint.text, "Tap the page first")
        self.assertNotIn("bar.slider", app.ui.targets())          # the hint covers it briefly
        app.web._unhint()
        self.assertIn("bar.slider", app.ui.targets())
        self.facts.focused = True                                 # the owner taps the page
        self.poll(app)
        self.assertTrue(self.kb.shown)
        self.tap(app, "bar.web.keys")                             # and hides it again
        self.assertFalse(self.kb.shown)

    def test_leaving_bar_hides_it(self):
        app = self.browsing()
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        self.assertTrue(self.kb.shown)
        app.on_mode(HIDDEN, "an emulator's second screen")
        self.assertFalse(self.kb.shown)                           # never over its touch area
        self.poll(app)                                            # the poll does not re-show it
        self.assertFalse(self.kb.shown)

    def test_off_mode_offers_no_button_and_never_shows(self):
        app = self.browsing("off")
        self.assertNotIn("bar.web.keys", app.ui.targets())
        self.assertIn("bar.web.close", app.ui.targets())
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        self.assertEqual(self.kb.calls, [])

    def test_missing_keyboard_binary_is_reported(self):
        app = self.browsing()
        self.kb.ok = False
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        self.assertFalse(app.web.osk_on)
        self.assertFalse(app.ui.bar.web_keys.lit)
        self.assertEqual(app.ui.bar.hint.text, "No on-screen keyboard")
        self.assertIn("wvkbd", app.web.osk_error)

    def test_killed_externally_by_rocknix_reshows_on_the_next_poll(self):
        """W (heads-up from Main, 24 Sep): ROCKNIX's own touchscreen-keyboard
        service `killall`s the shared wvkbd-mobintl binary on its own loop,
        which can take rp5deck's instance with it while Firefox stays
        focused on the very same editable field the whole time (osk_on never
        sees a False -> True transition to notice by). _set_osk must re-run
        _w_osk whenever it THINKS the keyboard is on but it is not actually
        visible any more - not wait for the next fresh focus."""
        app = self.browsing()
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        self.assertTrue(self.kb.shown)
        self.assertEqual(self.kb.calls, ["show"])
        self.kb.killed_externally()                               # ROCKNIX's killall
        self.poll(app)                                            # same field, no transition
        self.assertTrue(app.web.osk_on)                           # rp5deck never thought it left
        self.assertTrue(self.kb.shown)                            # but it re-showed anyway
        self.assertEqual(self.kb.calls, ["show", "show"])

    def test_unknown_probe_changes_nothing(self):
        app = self.browsing()
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        self.br.editable = None                                   # Marionette did not answer
        for _ in range(3):
            self.poll(app)
        self.assertTrue(self.kb.shown)

    def test_closing_firefox_hides_the_keyboard(self):
        app = self.browsing()
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        self.tap(app, "bar.web.close")
        self.assertFalse(self.kb.shown)
        self.assertEqual(self.br.calls[-1], ("close",))


# ---------------------------------------------------------------------------
# Real drawing (WSL / the device): the new views paint, cover the panel,
# and RP5DECK_RENDER_DIR=<dir> writes them as PNGs for a human to look at.
# ---------------------------------------------------------------------------
def _gfx():
    try:
        import gfx
        return gfx
    except Exception:           # noqa: BLE001 - no cairo/pango here (the Windows PC)
        return None


@unittest.skipUnless(_gfx(), "needs libcairo + libpango (WSL, the device)")
class TestRealRenderingHF1(TilesCase):
    def setUp(self):
        TilesCase.setUp(self)
        import ctypes
        self.gfx, self.ct = _gfx(), ctypes

    def _write_png(self, canvas, path):
        f = self.gfx._cairo.cairo_surface_write_to_png
        f.argtypes = [self.ct.c_void_p, self.ct.c_char_p]
        canvas.pixels()
        self.assertEqual(f(canvas.surf, os.fsencode(path)), 0)

    def render(self, app, name):
        w, h = app.ui.root.rect[2], app.ui.root.rect[3]
        canvas = self.gfx.Canvas(w, h)
        self.addCleanup(canvas.free)
        with canvas.clipped((0, 0, w, h)):
            app.ui.root.paint(canvas)
        out = os.environ.get("RP5DECK_RENDER_DIR")
        if out:
            os.makedirs(out, exist_ok=True)
            self._write_png(canvas, os.path.join(out, "hf1-pc-render-%s.png" % name))
        data, stride = canvas.pixels()

        def px(x, y):
            return tuple(self.ct.string_at(data + int(y) * stride + int(x) * 4, 4))   # B,G,R,A
        return px, w, h

    def assert_opaque(self, px, w, h):
        for x in range(0, w, 97):
            for y in range(0, h, 53):
                self.assertEqual(px(x, y)[3], 255, (x, y))

    def test_command_center_tiles(self):
        app = self.make_web_app()
        app.apply_master({"state": "ok", "volume": 0.42, "muted": False,
                          "sink_description": "Speaker"})
        self.open_cc(app)
        px, w, h = self.render(app, "command-center-home")
        self.assert_opaque(px, w, h)

    def test_launch_sheet(self):
        app = self.make_web_app(deferred=True)
        self.open_cc(app)
        self.tap(app, "home.discord")
        px, w, h = self.render(app, "launch-opening-discord")
        self.assert_opaque(px, w, h)
        app.web_worker.q.clear()                         # Firefox "started" ...
        self.facts.output = "DP-1"                       # ... on the wrong screen
        app.web._map_facts((app.web.gen, self.facts("rp5deck-web")))
        px, w, h = self.render(app, "launch-window-on-wrong-screen")
        self.assert_opaque(px, w, h)

    def test_strips(self):
        app = self.make_web_app()
        app.apply_master({"state": "ok", "volume": 0.35, "muted": False})
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.to_bar(app)
        self.facts.focused = True
        self.br.editable = {"editable": True, "focus": True}
        self.poll(app)
        px, w, h = self.render(app, "bar-browser-keyboard-on")
        self.assertEqual((w, h), (W, screens.BAR_H))
        self.assert_opaque(px, w, h)
        kx, ky, kw, kh = app.ui.bar.web_keys.rect
        b, g, r, a = px(kx + 8, ky + kh / 2)
        accent = tuple(int(round(c * 255)) for c in ui.THEME["accent"][:3])
        self.assertEqual((r, g, b), accent)              # lit: the keyboard is up
        self.tap(app, "bar.web.close")
        self.to_full(app)

    def test_tv_strip_home_and_close(self):
        """YT3: the YouTube App's tv strip - Close/Home/D-pad all fit and
        draw, rendered both SHOWN and auto-hidden."""
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.ytapp")
        app.on_mode(BAR, "rp5deck window on DSI-1: %s" % browser.TV_APP_ID)
        app.ui.set_size(W, screens.BAR_H)
        app.post.drain()
        px, w, h = self.render(app, "yt3-tv-strip-shown")
        self.assertEqual((w, h), (W, screens.BAR_H))
        self.assert_opaque(px, w, h)
        app.ytauto._on_timeout()                          # AH: auto-hide fires
        app.ui.set_size(W, screens.HANDLE_H)
        px, w, h = self.render(app, "yt3-tv-strip-hidden-handle")
        self.assert_opaque(px, w, h)


class TestSleepTile(TilesCase):
    """YT4 (owner-approved): the Sleep tile - a brief "Sleeping..." hint,
    then system_sleep.suspend() off the UI thread after SLEEP_DELAY_S.
    app.sleep_parts injects a fake suspend() so nothing here ever actually
    calls systemctl."""

    def test_tap_shows_the_sleeping_hint(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.sleep")
        self.assertTrue(app.ui.bar.hinting)
        self.assertEqual(app.ui.bar.hint.text, "Sleeping…")
        self.assertTrue(app.sleep_pending)

    def test_double_tap_is_a_no_op_while_pending(self):
        app = self.make_web_app()
        self.open_cc(app)
        self.tap(app, "home.sleep")
        timer1 = app.sleep_timer
        self.tap(app, "home.sleep")
        self.assertIs(app.sleep_timer, timer1)

    def test_delay_then_suspend_off_the_ui_thread(self):
        calls = []
        app = self.make_web_app()
        app.sleep_parts = {"suspend": lambda: (calls.append(1), (True, ""))[1]}
        self.open_cc(app)
        self.tap(app, "home.sleep")
        self.assertEqual(calls, [])            # not yet - still in the delay
        self.run_timers(app, main.SLEEP_DELAY_S + 0.01)
        self.assertEqual(calls, [1])

    def test_success_clears_the_hint_and_the_pending_flag(self):
        app = self.make_web_app()
        app.sleep_parts = {"suspend": lambda: (True, "")}
        self.open_cc(app)
        self.tap(app, "home.sleep")
        self.run_timers(app, main.SLEEP_DELAY_S + 0.01)
        self.assertFalse(app.sleep_pending)
        self.assertFalse(app.ui.bar.hinting)

    def test_failure_shows_an_error_toast_and_clears_pending(self):
        app = self.make_web_app()
        app.sleep_parts = {"suspend": lambda: (False, "systemctl not found")}
        self.open_cc(app)
        self.tap(app, "home.sleep")
        self.run_timers(app, main.SLEEP_DELAY_S + 0.01)
        self.assertFalse(app.sleep_pending)
        self.assertTrue(app.ui.bar.hinting)
        self.assertIn("systemctl not found", app.ui.bar.hint.text)

    def test_a_second_tap_after_failure_can_sleep_again(self):
        app = self.make_web_app()
        calls = []
        app.sleep_parts = {"suspend": lambda: (calls.append(1), (False, "x"))[1]}
        self.open_cc(app)
        self.tap(app, "home.sleep")
        self.run_timers(app, main.SLEEP_DELAY_S + 0.01)
        self.assertFalse(app.sleep_pending)
        self.tap(app, "home.sleep")
        self.run_timers(app, main.SLEEP_DELAY_S + 0.01)
        self.assertEqual(calls, [1, 1])


class TestSafeChargeSettings(TilesCase):
    """YT4 (owner-approved): battery.safe_charge_enabled/safe_charge_end_pct
    - charge_limit.py's read_live()/apply() injected via app.charge_parts so
    nothing here ever touches real sysfs."""

    def test_open_settings_probes_live_values_into_the_fields(self):
        app = self.make_web_app()
        app.charge_parts = {"read_live": lambda: {"available": True, "end": 70, "start": 65}}
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        self.assertTrue(app.charge_available)
        self.assertTrue(config.get_value(app.cfg, ("battery", "safe_charge_enabled")))
        self.assertEqual(config.get_value(app.cfg, ("battery", "safe_charge_end_pct")), 70)

    def test_end_100_live_shows_the_toggle_off(self):
        app = self.make_web_app()
        app.charge_parts = {"read_live": lambda: {"available": True, "end": 100, "start": 95}}
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        self.assertFalse(config.get_value(app.cfg, ("battery", "safe_charge_enabled")))

    def test_unavailable_kernel_disables_the_controls_and_notes_it(self):
        app = self.make_web_app()
        app.charge_parts = {"read_live": lambda: {"available": False, "end": None, "start": None}}
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        self.assertFalse(app.charge_available)
        row = app.ui.settings.rows[("battery", "safe_charge_end_pct")]
        self.assertFalse(row.control.enabled)
        self.assertIn("Not supported", row.hint.text)

    def test_toggling_off_applies_end_100(self):
        app = self.make_web_app()
        calls = []

        def apply(end):
            calls.append(end)
            return {"ok": True, "end": end, "start": end - 5, "detail": ""}
        app.charge_parts = {"read_live": lambda: {"available": True, "end": 85, "start": 80},
                            "apply": apply}
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        config.set_value(app.cfg, ("battery", "safe_charge_enabled"), False)
        app.on_setting(("battery", "safe_charge_enabled"), False)
        self.assertEqual(calls, [100])

    def test_slider_change_applies_the_new_percentage(self):
        app = self.make_web_app()
        calls = []

        def apply(end):
            calls.append(end)
            return {"ok": True, "end": end, "start": end - 5, "detail": ""}
        app.charge_parts = {"read_live": lambda: {"available": True, "end": 85, "start": 80},
                            "apply": apply}
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        config.set_value(app.cfg, ("battery", "safe_charge_end_pct"), 60)
        app.on_setting(("battery", "safe_charge_end_pct"), 60)
        self.assertEqual(calls, [60])

    def test_disabled_toggle_never_applies(self):
        """battery.safe_charge_enabled off means "no limit" already applied
        (end=100) - a slider drag that follows must not re-apply a real
        percentage until the toggle is back on."""
        app = self.make_web_app()
        calls = []

        def apply(end):
            calls.append(end)
            return {"ok": True, "end": end, "start": end - 5, "detail": ""}
        app.charge_parts = {"read_live": lambda: {"available": True, "end": 100, "start": 95},
                            "apply": apply}
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        config.set_value(app.cfg, ("battery", "safe_charge_end_pct"), 60)
        app.on_setting(("battery", "safe_charge_end_pct"), 60)
        self.assertEqual(calls, [100])   # still off - the % change alone changes nothing real

    def test_mismatch_shows_a_note_on_the_percent_row(self):
        app = self.make_web_app()
        app.charge_parts = {
            "read_live": lambda: {"available": True, "end": 85, "start": 80},
            "apply": lambda end: {"ok": False, "end": end, "start": end - 5,
                                  "detail": "kernel holds end=60 start=55"},
        }
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        config.set_value(app.cfg, ("battery", "safe_charge_end_pct"), 60)
        app.on_setting(("battery", "safe_charge_end_pct"), 60)
        app.post.drain()
        row = app.ui.settings.rows[("battery", "safe_charge_end_pct")]
        self.assertIn("kernel holds", row.hint.text)
        self.run_timers(app, main.CHARGE_TOAST_S + 0.1)
        self.assertEqual(row.hint.text, row._restart_note)


if __name__ == "__main__":
    unittest.main()
