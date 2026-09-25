"""AH: bar_autohide.BarAutoHide - the YouTube TV app's auto-hidden strip.

FakeHost stands in for main.App: call_later/cancel are the same timer-heap
idiom main.App itself uses (never a real thread/clock - tests fire timers by
calling the queued function directly, like tests/test_summon.py's own fakes
for the pull-down auto-close countdown do). ui.cc.bar.app is the one piece
of main.App state BarAutoHide reads directly (screens.Bar.app)."""
import unittest

import bar_autohide
import screens
import sway_ipc

BAR, FULL, HIDDEN_MODE = sway_ipc.BAR, sway_ipc.FULL, sway_ipc.HIDDEN


class _Ns:
    """A bare attribute bag - main.App.ui.cc.bar.app, nothing more."""


class FakeHost:
    def __init__(self, app=None):
        self.ui = _Ns()
        self.ui.cc = _Ns()
        self.ui.cc.bar = _Ns()
        self.ui.cc.bar.app = app
        self.pending = []            # [fn, cancelled] - call_later() order
        self.geometry_calls = 0

    def call_later(self, delay, fn):
        h = [fn, False]
        self.pending.append(h)
        return h

    def cancel(self, h):
        if h is not None:
            h[1] = True

    def _apply_bar_geometry(self):
        self.geometry_calls += 1

    def set_app(self, app):
        self.ui.cc.bar.app = app

    def fire_due(self):
        """Run every un-cancelled call_later() queued so far, in order -
        main.App._run_timers() without the wall clock."""
        due, self.pending = self.pending, []
        for fn, cancelled in due:
            if not cancelled:
                fn()


class TestWants(unittest.TestCase):
    def test_only_tv_is_wanted(self):
        a = bar_autohide.BarAutoHide(FakeHost())
        self.assertTrue(a.wants("tv"))
        for app in (None, "web", "yt", "discord"):
            self.assertFalse(a.wants(app), app)


class TestGeometryPerState(unittest.TestCase):
    """"the geometry/zone in each state (shown-reserved never happens for
    tv; hidden = handle only, zone 0; shown-over = full strip, zone 0)"."""

    def test_non_tv_apps_get_the_default_reserved_zone_untouched(self):
        a = bar_autohide.BarAutoHide(FakeHost())
        for app in (None, "web", "yt"):
            self.assertEqual(a.geometry(app, screens.BAR_H, screens.BAR_H),
                             (screens.BAR_H, screens.BAR_H), app)

    def test_tv_shown_is_full_strip_zone_zero(self):
        a = bar_autohide.BarAutoHide(FakeHost())
        a.state = bar_autohide.SHOWN
        self.assertEqual(a.geometry("tv", screens.BAR_H, screens.BAR_H), (screens.BAR_H, 0))

    def test_tv_hidden_is_handle_only_zone_zero(self):
        a = bar_autohide.BarAutoHide(FakeHost(), handle_h=24)
        a.state = bar_autohide.HIDDEN
        self.assertEqual(a.geometry("tv", screens.BAR_H, screens.BAR_H), (24, 0))

    def test_tv_never_returns_the_full_reserved_zone_in_either_state(self):
        a = bar_autohide.BarAutoHide(FakeHost())
        for a.state in (bar_autohide.SHOWN, bar_autohide.HIDDEN):
            _, zone = a.geometry("tv", screens.BAR_H, screens.BAR_H)
            self.assertEqual(zone, 0, a.state)


class TestTimerHidesTheStrip(unittest.TestCase):
    def test_the_timer_hides_the_strip_after_timeout(self):
        host = FakeHost(app="tv")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-ytapp")
        self.assertEqual(a.state, bar_autohide.SHOWN)
        self.assertEqual(len(host.pending), 1)
        host.fire_due()
        self.assertEqual(a.state, bar_autohide.HIDDEN)
        self.assertGreaterEqual(host.geometry_calls, 1)

    def test_zero_timeout_disables_auto_hide(self):
        host = FakeHost(app="tv")
        a = bar_autohide.BarAutoHide(host, timeout_s=0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-ytapp")
        self.assertEqual(host.pending, [])
        self.assertEqual(a.state, bar_autohide.SHOWN)


class TestTouch(unittest.TestCase):
    """"handle tap -> shown; strip tap resets the timer"."""

    def make_hidden(self):
        host = FakeHost(app="tv")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-ytapp")
        host.fire_due()
        self.assertEqual(a.state, bar_autohide.HIDDEN)
        return host, a

    def test_a_tap_on_the_handle_shows_the_strip(self):
        host, a = self.make_hidden()
        calls_before = host.geometry_calls
        a.touch()
        self.assertEqual(a.state, bar_autohide.SHOWN)
        self.assertGreater(host.geometry_calls, calls_before)

    def test_a_tap_on_the_shown_strip_resets_the_timeout_without_reapplying_geometry(self):
        host = FakeHost(app="tv")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-ytapp")
        first_timer = host.pending[-1]
        calls_before = host.geometry_calls
        a.touch()
        self.assertEqual(a.state, bar_autohide.SHOWN)
        self.assertEqual(host.geometry_calls, calls_before)   # already shown: no-op visually
        self.assertTrue(first_timer[1])                        # the old timer was cancelled
        live = [h for h in host.pending if not h[1]]
        self.assertEqual(len(live), 1)                         # ... and a fresh one queued
        self.assertIsNot(live[0], first_timer)

    def test_touch_does_nothing_while_inactive(self):
        host = FakeHost(app="web")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.touch()
        self.assertEqual(host.pending, [])
        self.assertEqual(host.geometry_calls, 0)


class TestOtherAppsUnaffected(unittest.TestCase):
    """"Browser/Discord unaffected (still 140 px with a zone)"."""

    def test_entering_bar_with_web_never_activates_or_arms_a_timer(self):
        host = FakeHost(app="web")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-web")
        self.assertFalse(a.active)
        self.assertEqual(host.pending, [])
        self.assertEqual(a.geometry("web", screens.BAR_H, screens.BAR_H),
                         (screens.BAR_H, screens.BAR_H))


class TestLeavingRestoresNormalBehaviour(unittest.TestCase):
    """"leaving the YouTube App restores normal behaviour"."""

    def test_mode_leaving_bar_deactivates_and_cancels_the_timer(self):
        host = FakeHost(app="tv")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-ytapp")
        timer = host.pending[-1]
        a.on_mode(BAR, FULL, "no window on DSI-1")
        self.assertFalse(a.active)
        self.assertEqual(a.state, bar_autohide.SHOWN)   # reset for next time
        self.assertTrue(timer[1])
        host.fire_due()                                  # nothing left queued to fire
        self.assertEqual(a.geometry("tv", screens.BAR_H, screens.BAR_H),
                         (screens.BAR_H, 0))              # next open starts SHOWN again

    def test_switching_to_another_bar_app_without_leaving_bar_also_deactivates(self):
        """Defensive: real switches always cross a FULL blip (window_switcher
        moves windows between outputs - patches/AH-NOTES.md), but on_mode()
        must not depend on that to notice the app changed."""
        host = FakeHost(app="tv")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-ytapp")
        host.set_app("web")
        a.on_mode(BAR, BAR, "rp5deck window on DSI-1: rp5deck-web")
        self.assertFalse(a.active)


class TestTimerNeverFiresAfterSessionEnds(unittest.TestCase):
    def test_stop_cancels_the_pending_timer_for_good(self):
        host = FakeHost(app="tv")
        a = bar_autohide.BarAutoHide(host, timeout_s=4.0)
        a.on_mode(FULL, BAR, "rp5deck window on DSI-1: rp5deck-ytapp")
        self.assertEqual(len(host.pending), 1)
        a.stop()
        calls_before = host.geometry_calls
        host.fire_due()                     # the queued call is still there but cancelled
        self.assertEqual(a.state, bar_autohide.SHOWN)     # never flipped to HIDDEN
        self.assertEqual(host.geometry_calls, calls_before)
        self.assertFalse(a.active)


if __name__ == "__main__":
    unittest.main()
