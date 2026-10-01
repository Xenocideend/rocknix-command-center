#!/usr/bin/env python3
"""FX-A regression tests: volume and slider input (Review R).

  RV1-M1  a stroke that starts on a slider and becomes a swipe (or is
          cancelled for any other reason) restores the value it had at
          pointer-down, and issues no set and no commit; a slider never
          jumps to the finger on touch-down.
  RV2-M1  volume read-backs are coalesced (at most one in flight), held
          while the finger is on the slider and until the commit has
          landed, so the slider never moves back to a stale value.
  RV2-m3  a setting changed just before shutdown is still saved.
  RV2-m6  one exception in the HUD sampler or the mixer's stream list does
          not wedge that screen for the session.
  RV5b    swallowed exceptions on the volume / signal paths are logged,
          once, not per event.

Offline, stdlib only. The app-level tests reuse the suite's headless App
harness (tests/test_companion.AppCase), audio in dry-run.
"""
import json
import logging
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import audio  # noqa: E402
import config  # noqa: E402
import main  # noqa: E402
import summon  # noqa: E402
import ui  # noqa: E402
from test_companion import AppCase  # noqa: E402

PULL = summon.PullDownStateMachine


def audio_calls(app, since):
    return [c for c in list(app.backend.calls)[since:]]


# ---------------------------------------------------------------------------
# ui.Slider on its own
# ---------------------------------------------------------------------------
class TestSliderGestureSafety(unittest.TestCase):
    def setUp(self):
        self.changes, self.releases, self.cancels = [], [], []
        # rect x 100..1100, knob_r 50 -> track 150..1050 (900 px)
        self.s = ui.Slider(rect=(100, 0, 1000, 120), knob_r=50, step=0.01, value=0.2,
                           on_change=self.changes.append, on_release=self.releases.append,
                           on_cancel=lambda v, changed: self.cancels.append((v, changed)))

    def test_touch_down_does_not_move_the_knob(self):
        s = self.s
        s.on_press("p", s.x_for(0.9), 60)
        self.assertEqual(s.value, 0.2)
        self.assertEqual(self.changes, [])
        self.assertTrue(s.dragging)             # external updates are still held off

    def test_drag_is_relative_to_where_the_finger_landed(self):
        s = self.s
        x0 = s.x_for(0.9)                       # far from the knob at 0.2
        s.on_press("p", x0, 60)
        s.on_move("p", x0 + 0.10 * 900, 62)     # 10 % of the track to the right
        self.assertEqual(s.value, 0.3)
        s.on_release("p", x0 + 0.10 * 900, 62)
        self.assertEqual(self.releases, [0.3])
        self.assertEqual(self.cancels, [])

    def test_cancel_after_a_drag_restores_the_press_value_and_commits_nothing(self):
        s = self.s
        x0 = s.x_for(0.2)
        s.on_press("p", x0, 60)
        s.on_move("p", x0 + 0.3 * 900, 60)
        self.assertEqual(s.value, 0.5)
        s.on_cancel("p")
        s.on_cancel("p")                        # a second cancel is a no-op
        self.assertEqual(s.value, 0.2)
        self.assertEqual(self.releases, [])
        self.assertEqual(self.cancels, [(0.2, True)])
        self.assertFalse(s.dragging)

    def test_vertical_stroke_changes_nothing_even_on_release(self):
        s = self.s
        x0 = s.x_for(0.8)
        s.on_press("p", x0, 100)
        s.on_move("p", x0 + 5, 40)
        s.on_move("p", x0 + 60, 0)              # sideways only after going vertical
        s.on_release("p", x0 + 60, 0)
        self.assertEqual(s.value, 0.2)
        self.assertEqual(self.changes, [])
        self.assertEqual(self.releases, [])
        self.assertEqual(self.cancels, [(0.2, False)])

    def test_a_clean_tap_sets_on_release_not_on_press(self):
        s = self.s
        s.on_press("p", s.x_for(0.6), 60)
        self.assertEqual(s.value, 0.2)
        s.on_release("p", s.x_for(0.6) + 3, 61)
        self.assertEqual(s.value, 0.6)
        self.assertEqual(self.releases, [0.6])

    def test_without_an_on_cancel_the_readout_is_restored_through_on_change(self):
        changes, releases = [], []
        s = ui.Slider(rect=(100, 0, 1000, 120), knob_r=50, value=0.4,
                      on_change=changes.append, on_release=releases.append)
        x0 = s.x_for(0.4)
        s.on_press("p", x0, 60)
        s.on_move("p", x0 + 0.2 * 900, 60)
        s.on_cancel("p")
        self.assertEqual(changes, [0.6, 0.4])
        self.assertEqual(releases, [])
        self.assertEqual(s.value, 0.4)


class TestSwipeFromASlider(unittest.TestCase):
    def setUp(self):
        self.root = ui.Root(1920, 1080)
        self.changes, self.rel, self.got = [], [], []
        self.sl = self.root.add(ui.Slider(rect=(400, 900, 1000, 120), knob_r=50, value=0.05,
                                          on_change=self.changes.append,
                                          on_release=self.rel.append))
        self.g = ui.SwipeRecognizer(lambda: 1080, edge=60, distance=120,
                                    wants=lambda n: n == "swipe_up",
                                    on_gesture=self.got.append)
        self.r = ui.TouchRouter(self.root, gestures=self.g)

    def test_swipe_up_from_the_slider_leaves_the_value_alone(self):
        x = self.sl.x_for(0.8)
        self.r.down(("f", 1), x, 1000)
        for y in (960, 920, 880, 840):
            self.r.move(("f", 1), x, y)
        self.r.up(("f", 1), x, 700)
        self.assertEqual(self.got, ["swipe_up"])
        self.assertEqual(self.sl.value, 0.05)
        self.assertEqual(self.changes, [])
        self.assertEqual(self.rel, [])

    def test_sideways_then_claimed_restores(self):
        x = self.sl.x_for(0.05)
        self.r.down(("f", 1), x, 1000)
        self.r.move(("f", 1), x + 40, 1000)             # engaged: live value moves
        self.assertGreater(self.sl.value, 0.05)
        self.r.move(("f", 1), x + 50, 840)              # ...then claimed as swipe_up
        self.assertEqual(self.got, ["swipe_up"])
        self.assertEqual(self.sl.value, 0.05)
        self.assertEqual(self.rel, [])
        self.assertEqual(self.changes[-1], 0.05)        # the readout was put back


# ---------------------------------------------------------------------------
# The app: master, mixer and settings sliders
# ---------------------------------------------------------------------------
class TestAppSwipeCancelsSliders(AppCase):
    def open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)

    def test_swipe_up_from_the_master_slider_sets_and_commits_nothing(self):
        # RV1-M1's reproduction: finger lands at 80 % of the slider, moves up
        app = self.make_app()
        self.open_cc(app)
        before_v = app.ui.bar.slider.value
        x, y, w, h = app.ui.targets()["bar.slider"]
        n = len(app.backend.calls)
        fx, fy = x + 0.8 * w, y + h - 5
        self.swipe(app, fx, fy, fx, max(0, fy - 200))
        app.post.drain()
        self.assertEqual(app.pull.state, PULL.COMPANION)       # the gesture still worked
        self.assertEqual(audio_calls(app, n), [])
        self.assertEqual(app.ui.bar.slider.value, before_v)
        self.assertIsNone(app.last_set)
        self.assertIsNone(app.last_commit)

    def test_swipe_up_from_a_mixer_row_sets_nothing(self):
        app = self.make_app()
        self.open_cc(app)
        app.open_mixer()
        app.post.drain()
        app.ui.mixer.set_streams([{"id": 77, "display_name": "RetroArch", "volume": 0.2,
                                   "muted": False}])
        x, y, w, h = app.ui.targets()["mixer.slider.77"]
        n = len(app.backend.calls)
        fx, fy = x + 0.9 * w, y + h / 2
        self.swipe(app, fx, fy, fx, fy - 300)
        app.post.drain()
        self.assertEqual(app.pull.state, PULL.COMPANION)
        self.assertEqual(audio_calls(app, n), [])

    def test_a_drag_that_turns_into_a_swipe_restores_live_and_never_commits(self):
        app = self.make_app()
        app.apply_master({"state": "ok", "volume": 0.05, "muted": False})
        self.open_cc(app)
        s = app.ui.bar.slider
        x0, cy = s.x_for(0.05), s.rect[1] + s.rect[3] - 5
        n = len(app.backend.calls)
        pid = ("f", 9)
        app.router.down(pid, x0, cy)
        app.router.move(pid, x0 + 60, cy)               # engaged: one live set
        self.assertEqual([c[0] for c in audio_calls(app, n)], ["set_master"])
        app.router.move(pid, x0 + 70, cy - 300)          # ...then it becomes a swipe up
        self.assertEqual(app.pull.state, PULL.COMPANION)
        app.router.up(pid, x0 + 70, cy - 300)
        app.post.drain()
        calls = audio_calls(app, n)
        self.assertNotIn("commit_master", [c[0] for c in calls])
        self.assertEqual(calls[-1], ("set_master", "0.0500"))  # put back, live only
        self.assertEqual(s.value, 0.05)
        self.assertIsNone(app.last_commit)

    def test_swipe_up_from_a_settings_slider_changes_no_setting(self):
        app = self.make_app()
        self.open_cc(app)
        app.open_settings()
        self.assertEqual(app.pull.state, PULL.SETTINGS)
        sliders = [w for w in app.ui.settings.walk()
                   if isinstance(w, ui.Slider) and w.shown() and w.enabled]
        self.assertTrue(sliders, "no visible settings slider to test with")
        s = sliders[-1]
        cfg_before = json.dumps(app.cfg, sort_keys=True)
        v_before = s.value
        fx, fy = s.x_for(0.9 if v_before < 0.5 else 0.1), s.rect[1] + s.rect[3] / 2
        self.swipe(app, fx, fy, fx, fy - 400)
        app.post.drain()
        # the swipe was claimed as a gesture (in Settings a swipe up turns the page, 26 Sep)
        self.assertEqual((app.last_gesture or {}).get("name"), "swipe_up")
        self.assertEqual(app.settings_changes, 0)
        self.assertEqual(json.dumps(app.cfg, sort_keys=True), cfg_before)
        self.assertEqual(s.value, v_before)

    def test_a_horizontal_drag_still_sets_live_and_commits_once(self):
        app = self.make_app()
        app.apply_master({"state": "ok", "volume": 0.30, "muted": False})
        self.open_cc(app)
        s = app.ui.bar.slider
        x0, cy = s.x_for(0.30), s.rect[1] + s.rect[3] / 2
        n = len(app.backend.calls)
        pid = ("f", 4)
        app.router.down(pid, x0, cy)
        for k in range(1, 11):
            app.router.move(pid, x0 + k * 0.03 * (s.track()[1] - s.track()[0]), cy)
        app.router.up(pid, x0 + 0.30 * (s.track()[1] - s.track()[0]), cy)
        app.post.drain()
        names = [c[0] for c in audio_calls(app, n)]
        self.assertEqual(names.count("commit_master"), 1)
        self.assertEqual(names[-1], "commit_master")
        self.assertIn("set_master", names)
        self.assertAlmostEqual(app.last_commit, 0.60, places=6)


# ---------------------------------------------------------------------------
# RV2-M1: volume read-backs
# ---------------------------------------------------------------------------
class ManualWorker:
    """Runs submitted jobs only when told to, like a busy audio worker."""

    def __init__(self, post):
        self.post = post
        self.jobs = []

    def submit(self, fn, *args, done=None):
        self.jobs.append((fn, args, done))

    def run_one(self):
        fn, args, done = self.jobs.pop(0)
        try:
            res = fn(*args)
        except Exception:           # noqa: BLE001  (main.Worker's behaviour)
            return
        if done is not None:
            self.post(done, res)

    def names(self):
        return [getattr(fn, "__name__", repr(fn)) for fn, _a, _d in self.jobs]

    def stop(self, timeout=0):
        pass


class FakeAudio:
    """Backend stand-in: a live volume that set/commit change."""

    def __init__(self, v=0.30):
        self.v = v
        self.dryrun = False
        self.calls = []
        self.reads = 0

    def get_master(self):
        self.reads += 1
        return {"state": "ok", "volume": self.v, "muted": False, "sink_description": "Speaker"}

    def set_master(self, v):
        self.calls.append(("set_master", round(v, 4)))
        self.v = round(v, 4)
        return True

    def commit_master(self, v):
        self.calls.append(("commit_master", round(v, 4)))
        self.v = round(v, 4)
        return True

    def toggle_master_mute(self):
        return True


class TestMasterReadBacks(AppCase):
    def make(self, v=0.30):
        app = self.make_app()
        app.backend = FakeAudio(v)
        app.audio_worker = ManualWorker(app.post)
        app.apply_master(app.backend.get_master())
        self.swipe(app, 700, 20, 700, 400)
        return app

    def drain_worker(self, app, limit=50):
        while app.audio_worker.jobs and limit:
            app.audio_worker.run_one()
            app.post.drain()
            limit -= 1

    def get_master_jobs(self, app):
        return sum(1 for fn, _a, _d in app.audio_worker.jobs
                   if "get_master" in getattr(fn, "__name__", "") or
                   getattr(fn, "__wrapped__", None) is app.backend.get_master or
                   fn == app.backend.get_master)

    def test_events_coalesce_to_one_read_in_flight(self):
        app = self.make()
        for _ in range(8):
            app.on_audio_event("master")
        self.assertEqual(self.get_master_jobs(app), 1)
        self.drain_worker(app)
        # the events that arrived while the first read was pending cost ONE more read
        self.assertLessEqual(app.backend.reads, 1 + 2)

    def test_no_reads_while_dragging_and_none_stale_after_release(self):
        app = self.make(0.30)
        s = app.ui.bar.slider
        track = s.track()[1] - s.track()[0]
        x0, cy = s.x_for(0.30), s.rect[1] + s.rect[3] / 2
        pid = ("f", 5)
        app.router.down(pid, x0, cy)
        applied = []
        orig = app.apply_master
        app.apply_master = lambda m: (applied.append((m["volume"], s.dragging)), orig(m))
        for k in range(1, 31):
            app.router.move(pid, x0 + k * 0.02 * track, cy)
            app.post.drain()
            app.on_audio_event("master")                 # pw-mon reports each set
            app.post.drain()
            if k % 3 == 0 and app.audio_worker.jobs:     # the worker runs, but slowly
                app.audio_worker.run_one()
                app.post.drain()
        self.assertEqual(self.get_master_jobs(app), 0, app.audio_worker.names())
        app.router.up(pid, x0 + 0.60 * track, cy)
        app.post.drain()
        self.assertAlmostEqual(app.last_commit, 0.90, places=6)
        app.on_audio_event("master")
        self.drain_worker(app)
        stale = [v for v, _d in applied if abs(v - 0.90) > 1e-6]
        self.assertEqual(stale, [], applied)
        self.assertEqual(s.value, 0.90)

    def test_a_read_in_flight_before_the_press_is_not_applied_after_release(self):
        app = self.make(0.30)
        app.on_audio_event("master")                     # read queued, not yet run
        s = app.ui.bar.slider
        track = s.track()[1] - s.track()[0]
        x0, cy = s.x_for(0.30), s.rect[1] + s.rect[3] / 2
        pid = ("f", 6)
        app.router.down(pid, x0, cy)
        app.router.move(pid, x0 + 0.5 * track, cy)
        app.router.up(pid, x0 + 0.5 * track, cy)
        app.post.drain()
        app.audio_worker.run_one()                      # the old read (0.30) lands now,
        app.post.drain()                                 # before the commit has run
        self.assertEqual(s.value, 0.80)
        self.drain_worker(app)
        self.assertEqual(s.value, 0.80)
        self.assertEqual(app.backend.v, 0.80)

    def test_live_sets_do_not_pile_up_behind_a_slow_worker(self):
        app = self.make(0.30)
        for v in (0.31, 0.32, 0.33, 0.34):
            app._send_master(v)                          # the throttle firing, worker busy
        self.assertEqual(len(app.audio_worker.jobs), 1)
        app.audio_worker.run_one()
        self.assertEqual(app.backend.calls, [("set_master", 0.34)])   # newest value wins
        app._send_master(0.35)
        self.assertEqual(len(app.audio_worker.jobs), 1)  # a new one queues after it ran

    def test_a_failing_read_or_commit_does_not_wedge_the_reads(self):
        app = self.make(0.30)
        boom = mock.Mock(side_effect=OSError("wpctl vanished"))
        good = app.backend.get_master
        app.backend.get_master = boom
        with self.assertLogs("rp5deck", logging.ERROR):
            app.on_audio_event("master")
            self.drain_worker(app)
        app.backend.get_master = good
        app.backend.v = 0.55
        app.on_audio_event("master")
        self.drain_worker(app)
        self.assertEqual(app.ui.bar.slider.value, 0.55)
        # a commit that raises must not hold the reads off forever
        app.backend.commit_master = mock.Mock(side_effect=OSError("volume script failed"))
        app.on_volume_release(0.7)
        with self.assertLogs("rp5deck", logging.ERROR):
            self.drain_worker(app)
        app.backend.v = 0.40
        app.on_audio_event("master")
        self.drain_worker(app)
        self.assertEqual(app.ui.bar.slider.value, 0.40)


# ---------------------------------------------------------------------------
# RV2 minors: shutdown save, HUD / mixer survive one exception
# ---------------------------------------------------------------------------
class TestShutdownFlushesSave(AppCase):
    def test_setting_changed_just_before_shutdown_is_saved(self):
        app = self.make_app()
        path = os.environ["RP5DECK_CONFIG"]
        config.set_value(app.cfg, ("companion", "play_video"), False)
        app.on_setting(("companion", "play_video"), False)
        app.stop_reason = "SIGTERM"
        app.shutdown()
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            on_disk = json.load(f)
        self.assertIs(config.get_value(on_disk, ("companion", "play_video")), False)


class TestRefreshSurvivesOneException(AppCase):
    def test_hud_keeps_sampling_after_one_failure(self):
        app = self.make_app()
        from test_companion import SyncWorker
        app.hud_worker = SyncWorker(app.post)
        n = {"k": 0}

        def flaky():
            n["k"] += 1
            if n["k"] == 1:
                raise OSError("transient sysfs read")
            return {"battery_percent": 50}
        with mock.patch("hud.sample", flaky), self.assertLogs("rp5deck", logging.ERROR):
            app.open_hud()
            app._run_timers(time.monotonic())           # the first tick (delay 0)
            app.post.drain()
            for _ in range(2):                          # the next ticks, without waiting 1 s
                app._hud_tick()
                app.post.drain()
        self.assertGreaterEqual(n["k"], 2)
        self.assertFalse(app.hud_inflight)
        self.assertGreaterEqual(app.ui.hud.samples, 1)

    def test_mixer_recovers_after_one_failure(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        n = {"k": 0}

        def flaky():
            n["k"] += 1
            if n["k"] == 1:
                raise ValueError("pw-dump shape changed")
            return [{"id": 5, "display_name": "Game", "volume": 0.5, "muted": False}]
        app.backend.list_streams = flaky
        with self.assertLogs("rp5deck", logging.ERROR):
            app.open_mixer()
            app.post.drain()
        self.assertFalse(app.mixer_inflight)
        self.assertIsNone(app.ui.mixer.streams)             # shown as a failure, not empty
        app.on_audio_event("streams")
        app.post.drain()
        self.assertEqual(len(app.ui.mixer.streams), 1)


# ---------------------------------------------------------------------------
# RV5b: log what used to vanish
# ---------------------------------------------------------------------------
class TestLoggedNotSwallowed(unittest.TestCase):
    def make_sub(self, cb):
        sub = audio.Subscriber(cb)
        self.addCleanup(lambda: [os.close(fd) for fd in (sub._stop_r, sub._stop_w)])
        return sub

    def test_subscriber_callback_exception_is_logged_once_per_burst(self):
        def bad(kind):
            raise RuntimeError("UI handler bug")
        sub = self.make_sub(bad)
        with self.assertLogs("rp5deck.audio", logging.ERROR) as cm:
            for _ in range(5):
                sub._pending.add("master")
                sub._flush()
        self.assertEqual(len(cm.records), 1)            # rate-limited, not per event
        self.assertIn("UI handler bug", cm.output[0])

    def test_signal_thread_death_is_logged(self):
        app = main.App.__new__(main.App)
        app._sig_r = -1
        app.post = lambda *a: None
        with mock.patch("main.os.read", side_effect=OSError(9, "Bad file descriptor")), \
                self.assertLogs("rp5deck", logging.ERROR):
            app._sig_thread()

    def test_worker_logs_repeated_failures_once(self):
        posted = []
        wk = main.Worker("t-worker", lambda fn, *a: posted.append(fn))

        def bad():
            raise OSError("same failure every tick")
        with self.assertLogs("rp5deck", logging.ERROR) as cm:
            for _ in range(5):
                wk.submit(bad)
            wk.stop(2.0)
        self.assertEqual(len(cm.records), 1)


class TestPwMonKill(unittest.TestCase):
    def test_pwmon_that_ignores_sigterm_is_killed(self):
        import subprocess

        class Stubborn:
            def __init__(self):
                self.killed = False
                self.stdout = None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired("pw-mon", timeout)
                return -9

            def kill(self):
                self.killed = True
        sub = audio.Subscriber(lambda k: None)
        self.addCleanup(lambda: [os.close(fd) for fd in (sub._stop_r, sub._stop_w)])
        p = Stubborn()
        sub._pwmon_proc = p
        with self.assertLogs("rp5deck.audio", logging.WARNING):
            sub._kill_pwmon()
        self.assertTrue(p.killed)
        self.assertIsNone(sub._pwmon_proc)


@unittest.skipUnless(sys.platform.startswith("linux"), "inotify is Linux-only")
class TestInotifyFd(unittest.TestCase):
    def test_fd_is_cloexec_and_nonblocking_and_signatures_are_pinned(self):
        import ctypes
        import fcntl
        import tempfile
        d = tempfile.mkdtemp(prefix="rp5deck-ino-")
        self.addCleanup(os.rmdir, d)
        w = audio._InotifyWatch.create(d)
        self.assertIsNotNone(w)
        self.addCleanup(w.close)
        self.assertTrue(fcntl.fcntl(w.fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        self.assertTrue(fcntl.fcntl(w.fd, fcntl.F_GETFL) & os.O_NONBLOCK)
        libc = audio._inotify_libc()
        self.assertEqual(libc.inotify_init1.restype, ctypes.c_int)
        self.assertEqual(list(libc.inotify_init1.argtypes), [ctypes.c_int])
        self.assertEqual(list(libc.inotify_add_watch.argtypes),
                         [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32])


if __name__ == "__main__":
    unittest.main()
