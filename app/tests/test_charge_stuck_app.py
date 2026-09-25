#!/usr/bin/env python3
"""CHG: "charger connected but not charging" wired into the REAL main.App +
screens.py (no SDL) - proves the plumbing (io_worker reuse, Home banner
attribute names, the Dismiss button's on_click, and CHG-outranks-DS) rather
than re-testing charge_stuck_view.py's own poll/dismiss logic (tests/
test_charge_stuck_view.py) or the banner's own layout (tests/test_screens.py)."""
import argparse
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import main  # noqa: E402
from sway_ipc import FULL  # noqa: E402
from test_companion import AppCase, FakeLayer, FakeVideo, FakeWl, SyncWorker, W, H  # noqa: E402


class FakeSampler:
    def __init__(self, samples):
        self.samples = list(samples)

    def sample(self):
        return self.samples.pop(0)


def sample(t, plugged, current_ua=None, icl=None):
    return {"t": t, "plugged": plugged, "current_ua": current_ua, "icl": icl}


class Case(AppCase):
    def build(self, samples=None, ds_checker=None):
        app = main.App(argparse.Namespace(seconds=0, output=None))
        app.title = "Test Device Command Center"
        app.layer = FakeLayer()
        app.wl = FakeWl()
        app.mode = FULL
        app.video = FakeVideo()
        for name in ("media_worker", "io_worker", "audio_worker", "search_worker",
                     "web_worker", "clean_worker", "ds_worker"):
            setattr(app, name, SyncWorker(app.post))
        if samples is not None:
            app.charge_stuck_parts = {"sampler": FakeSampler(samples)}
        if ds_checker is not None:
            app.ds_parts = {"checker": ds_checker}
        app.build_ui(W, H)
        app.post.drain()
        return app

    def open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "cc")


class TestWiring(Case):
    def test_stuck_shows_the_home_banner(self):
        app = self.build(samples=[sample(i * 10.0, True, 700_000, 0x00) for i in range(6)])
        for _ in range(6):
            app.charge_stuck.poll()
            app.post.drain()
        self.open_cc(app)
        self.assertTrue(app.ui.home.banner_label.visible)
        self.assertIn("Not charging", app.ui.home.banner_label.text)
        self.assertEqual(app.ui.home.banner_action.text, "Dismiss")

    def test_good_never_shows_it(self):
        app = self.build(samples=[sample(i * 10.0, True, -300_000, 0x0C) for i in range(6)])
        for _ in range(6):
            app.charge_stuck.poll()
            app.post.drain()
        self.open_cc(app)
        self.assertFalse(app.ui.home.banner_label.visible)

    def test_tapping_dismiss_hides_it_through_the_real_button(self):
        app = self.build(samples=[sample(i * 10.0, True, 700_000, 0x00) for i in range(6)])
        for _ in range(6):
            app.charge_stuck.poll()
            app.post.drain()
        self.open_cc(app)
        self.tap(app, "home.banner_action")
        self.assertFalse(app.ui.home.banner_label.visible)
        self.assertTrue(app.charge_stuck.stuck)      # dismissed, not fixed

    def test_charge_outranks_a_live_dualscreen_notice(self):
        app = self.build(
            samples=[sample(i * 10.0, True, 700_000, 0x00) for i in range(6)],
            ds_checker=lambda: {"available": True, "missing": ["3ds.screen_layout"],
                                "error": None})
        app.dualscreen.poll()
        app.post.drain()
        for _ in range(6):
            app.charge_stuck.poll()
            app.post.drain()
        self.open_cc(app)
        self.assertIn("Not charging", app.ui.home.banner_label.text)
        # dismiss the charge notice: the dualscreen one takes the slot back.
        self.tap(app, "home.banner_action")
        self.assertIn("3ds.screen_layout", app.ui.home.banner_label.text)
        self.assertEqual(app.ui.home.banner_action.text, "Restore")


class TestDemoNotice(Case):
    """RP5DECK_DEMO_NOTICE (guide screenshots): shows the named banner with
    sample text instead of polling, and its button does nothing."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.environ.pop, "RP5DECK_DEMO_NOTICE", None)

    def test_charge_demo_shows_the_banner_without_a_poll(self):
        os.environ["RP5DECK_DEMO_NOTICE"] = "charge"
        # a good sampler: a real poll would never show the banner
        app = self.build(samples=[sample(i * 10.0, True, -300_000, 0x0C) for i in range(6)])
        app._charge_stuck_tick()
        self.open_cc(app)
        self.assertTrue(app.ui.home.banner_label.visible)
        self.assertIn("Not charging", app.ui.home.banner_label.text)
        self.tap(app, "home.banner_action")          # Dismiss: inert in demo
        self.assertTrue(app.ui.home.banner_label.visible)

    def test_dualscreen_demo_restore_never_opens_the_sheet(self):
        os.environ["RP5DECK_DEMO_NOTICE"] = "dualscreen"
        opened = []
        app = self.build(ds_checker=lambda: {"available": True, "missing": [], "error": None})
        app.dualscreen.open = lambda: opened.append(1)
        app._ds_tick()
        self.open_cc(app)
        self.assertIn("Dual-screen settings missing", app.ui.home.banner_label.text)
        self.tap(app, "home.banner_action")          # Restore: inert in demo
        self.assertEqual(opened, [])

    def test_unset_polls_normally(self):
        app = self.build(samples=[sample(i * 10.0, True, -300_000, 0x0C) for i in range(6)])
        app._charge_stuck_tick()
        app.post.drain()
        self.open_cc(app)
        self.assertFalse(app.ui.home.banner_label.visible)


if __name__ == "__main__":
    unittest.main()
