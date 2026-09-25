#!/usr/bin/env python3
"""Behaviour of main.py + screens.py WITH patches/CC1-*.patch applied. I2 (24 Sep):
the patches are merged; tests/test_cc1_merged.py runs this module against
the working tree. The real main.App (no SDL),
the real DeckUI, the real CleanStateController and sheet; only the helper
(cleanstate.Helper: processes, commands) is a fake."""
import argparse
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import cleanstate  # noqa: E402
import cleanstate_view  # noqa: E402
import es_health  # noqa: E402
import main  # noqa: E402
import screens  # noqa: E402
from sway_ipc import FULL  # noqa: E402
from test_companion import AppCase, FakeLayer, FakeVideo, FakeWl, SyncWorker, W, H  # noqa: E402


class FakeHelper:
    def __init__(self, plan):
        self.plan = plan
        self.executed = []

    def discover(self, **kw):
        return self.plan

    def execute(self, plan, actions, confirmed=False, force_restart=False):
        self.executed.append((list(actions), confirmed, force_restart))
        return [cleanstate.Step(a, True, "did %s" % a) for a in actions]


def plan(items, verdict="ok", summary="ES is running and responding"):
    p = cleanstate.Plan()
    p.items = items
    p.health = es_health.Health(verdict, summary,
                                ["http", "stall"] if verdict == "frozen" else [], ["n1"])
    return p


GAME = cleanstate.Item(cleanstate.KILL_EMULATOR, "Game: Super Metroid (snes)", "ES's /emukill")


class Cases(AppCase):
    def build(self, the_plan):
        app = main.App(argparse.Namespace(seconds=0, output=None))
        app.title = "Test Device Command Center"
        app.layer = FakeLayer()
        app.wl = FakeWl()
        app.mode = FULL
        app.video = FakeVideo()
        for name in ("media_worker", "io_worker", "audio_worker", "search_worker",
                     "web_worker", "clean_worker"):
            setattr(app, name, SyncWorker(app.post))
        self.helper = FakeHelper(the_plan)
        app.clean_parts = {"helper_factory": lambda: self.helper}
        app.build_ui(W, H)
        app.post.drain()
        return app

    def open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "cc")

    def test_the_tile_is_on_home_and_not_over_a_game(self):
        app = self.build(plan([]))
        self.open_cc(app)
        names = [t.name for t in app.ui.home.tiles]
        self.assertIn("home.clean", names)
        # I2: every tile merged (9) no longer fits the old 4 x 2 grid; the
        # intent of the old "<= 8" check is that EVERY tile gets a real,
        # separate hit target on the panel - check exactly that.
        targets = app.ui.targets()
        home = app.ui.home.rect
        rects = []
        for n in names:
            self.assertIn(n, targets, n)
            x, y, w, h = targets[n]
            self.assertGreaterEqual(min(w, h), 120, (n, targets[n]))
            self.assertTrue(home[0] <= x and x + w <= home[0] + home[2] and
                            home[1] <= y and y + h <= home[1] + home[3], (n, targets[n], home))
            for other, (ox, oy, ow, oh) in rects:
                self.assertFalse(x < ox + ow and ox < x + w and y < oy + oh and oy < y + h,
                                 (n, other))
            rects.append((n, (x, y, w, h)))
        self.assertIn("home.clean", app.ui.targets())
        self.assertNotIn("home.clean", screens.OVERLAY_TILES)
        app.ui.set_overlay(True)                            # CC5 over an emulator's window
        self.assertNotIn("home.clean", app.ui.targets())
        app.ui.set_overlay(False)
        self.assertIn("home.clean", app.ui.targets())

    def test_tap_confirm_clean_done_back_to_companion(self):
        app = self.build(plan([GAME]))
        self.open_cc(app)
        self.tap(app, "home.clean")
        self.assertEqual(app.ui.sheet, "cleanstate")
        self.assertEqual(app.clean.phase, cleanstate_view.CONFIRM)
        t = app.ui.targets()
        for name in ("clean.primary", "clean.cancel", "clean.back", "bar.slider"):
            self.assertIn(name, t)                          # the volume strip stays on top
        sheet = app.clean.sheet
        self.assertEqual(sheet.rect, (0, screens.BAR_H, W, H - screens.BAR_H))
        self.assertIn("- Game: Super Metroid (snes) - ES's /emukill",
                      [l.text for l in sheet.lines])
        self.tap(app, "clean.primary")
        self.assertEqual(self.helper.executed, [([cleanstate.KILL_EMULATOR], True, False)])
        self.assertEqual(app.clean.phase, cleanstate_view.RESULT)
        self.tap(app, "clean.cancel")                       # "Done"
        self.assertIsNone(app.ui.sheet)
        self.assertEqual(app.ui.showing, "companion")       # the clean state's default view
        json.dumps(app.state(), default=str)
        self.assertEqual(app.state()["clean_state"]["phase"], None)
        self.assertEqual(app.state()["clean_state"]["runs"], 1)

    def test_frozen_es_restart_button(self):
        app = self.build(plan([], "frozen", "ES isn't responding (HTTP timeout, UI thread idle "
                                            "for 5 s)"))
        self.open_cc(app)
        self.tap(app, "home.clean")
        self.assertIn("clean.restart", app.ui.targets())
        self.assertNotIn("clean.primary", app.ui.targets())
        self.tap(app, "clean.restart")
        self.assertEqual(self.helper.executed, [([cleanstate.RESTART_ES], True, False)])

    def test_back_keeps_the_command_center(self):
        app = self.build(plan([GAME]))
        self.open_cc(app)
        self.tap(app, "home.clean")
        self.tap(app, "clean.back")
        self.assertIsNone(app.ui.sheet)
        self.assertEqual(app.ui.showing, "cc")
        self.assertEqual(self.helper.executed, [])

    def test_real_helper_is_the_default_factory(self):
        app = self.build(plan([]))
        app.clean_parts = {}
        c = cleanstate_view.CleanStateController(app, app._submit_clean)
        h = c.helper_factory()
        self.assertIsInstance(h, cleanstate.Helper)
        self.assertEqual(h.es_output, app.es_output)
        self.assertFalse(h.dry_run)

    def test_shutdown_stops_the_clean_worker(self):
        app = self.build(plan([]))
        stopped = []
        app.clean_worker = type("Wk", (), {"stop": lambda s, t=0: stopped.append(t),
                                           "submit": lambda s, *a, **k: None})()
        app.shutdown()
        self.assertEqual(len(stopped), 1)


if __name__ == "__main__":
    unittest.main()
