#!/usr/bin/env python3
"""DS: dual-screen settings guard wired into the REAL main.App + screens.py
(no SDL) - proves the plumbing itself (worker assignment, add_sheet name,
Home banner attribute names, the Restore button's on_click) rather than
re-testing dualscreen_keys_view.py's own phase logic (tests/
test_dualscreen_keys_view.py) or Home.set_dualscreen_notice's layout
(tests/test_screens.py). Same shape as tests/cc1_patched_cases.py for CC1."""
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


class Case(AppCase):
    def build(self, checker=None, restorer=None):
        app = main.App(argparse.Namespace(seconds=0, output=None))
        app.title = "Test Device Command Center"
        app.layer = FakeLayer()
        app.wl = FakeWl()
        app.mode = FULL
        app.video = FakeVideo()
        for name in ("media_worker", "io_worker", "audio_worker", "search_worker",
                     "web_worker", "clean_worker", "ds_worker"):
            setattr(app, name, SyncWorker(app.post))
        if checker is not None or restorer is not None:
            app.ds_parts = {}
            if checker is not None:
                app.ds_parts["checker"] = checker
            if restorer is not None:
                app.ds_parts["restorer"] = restorer
        app.build_ui(W, H)
        app.post.drain()
        return app

    def open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "cc")


class TestWiring(Case):
    def test_dualscreen_sheet_is_registered(self):
        app = self.build()
        self.assertIn("dualscreen", app.ui.sheets)
        self.assertIs(app.ui.sheets["dualscreen"], app.dualscreen.sheet)

    def test_poll_with_missing_keys_shows_the_home_banner(self):
        app = self.build(checker=lambda: {"available": True,
                                          "missing": ["3ds.screen_layout"], "error": None})
        app.dualscreen.poll()
        app.post.drain()
        self.open_cc(app)
        self.assertTrue(app.ui.home.banner_label.visible)
        self.assertIn("3ds.screen_layout", app.ui.home.banner_label.text)
        self.assertTrue(app.ui.home.banner_action.visible)

    def test_poll_with_nothing_missing_never_shows_it(self):
        app = self.build(checker=lambda: {"available": True, "missing": [], "error": None})
        app.dualscreen.poll()
        app.post.drain()
        self.open_cc(app)
        self.assertFalse(app.ui.home.banner_label.visible)

    def test_tapping_restore_opens_the_confirm_sheet(self):
        app = self.build(checker=lambda: {"available": True,
                                          "missing": ["wiiu.gamepad_enabled"], "error": None})
        app.dualscreen.poll()
        app.post.drain()
        self.open_cc(app)
        self.tap(app, "home.banner_action")
        self.assertEqual(app.ui.screen, "dualscreen")
        self.assertIn("wiiu.gamepad_enabled", " ".join(l.text for l in app.dualscreen.sheet.lines))

    def test_confirmed_restore_runs_through_the_real_sheet(self):
        calls = []

        def restorer(missing):
            calls.append(list(missing))
            return {"ok": True, "detail": "restored: %s" % ", ".join(missing),
                    "restored": list(missing), "backup": "/x"}

        checks = [{"available": True, "missing": ["3ds.screen_layout"], "error": None},
                 {"available": True, "missing": [], "error": None}]
        app = self.build(checker=lambda: checks.pop(0), restorer=restorer)
        app.dualscreen.poll()
        app.post.drain()
        self.open_cc(app)
        self.tap(app, "home.banner_action")
        self.tap(app, "ds.primary")
        self.assertEqual(calls, [["3ds.screen_layout"]])
        self.assertEqual(app.dualscreen.phase, "result")
        self.assertFalse(app.ui.home.banner_label.visible)   # the re-poll cleared it


if __name__ == "__main__":
    unittest.main()
