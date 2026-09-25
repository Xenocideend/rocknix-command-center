#!/usr/bin/env python3
"""CC7's Hotkeys tile + sheet, against the REAL merged main.py / screens.py
(I2, 24 Sep: patches/CC7-*.patch are applied; this replaces
tests/test_cc7_patches.py, which applied them to a temp copy).

Same proof as before, now in-process on the shipped files: a real headless
main.App (test_companion.AppCase), the real widget tree and touch router -
the tile exists only once the Command Center is open, a tap opens the
sheet with rows, Back returns home, and a running Wii U game (through the
real es-event -> companion.info path) puts Cemu's page first."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import esevents  # noqa: E402
import test_companion as tc  # noqa: E402


class TestHotkeysSheetEndToEnd(tc.AppCase):
    def test_tile_opens_the_sheet_and_the_running_system_goes_first(self):
        app = self.make_app()
        # 1. the tile exists only once the Command Center is open
        self.assertNotIn("home.hotkeys", app.ui.targets())
        self.swipe(app, 700, 20, 700, 400)
        self.assertIn("home.hotkeys", app.ui.targets())
        # 2. a real tap (through app.router) opens it
        self.tap(app, "home.hotkeys")
        self.assertEqual(app.ui.screen, "hotkeys")
        self.assertGreater(len(app.ui.hotkeys.rows), 0)
        self.assertEqual(app.ui.hotkeys.pages[0][0].id, "global")
        # 3. Back (the generic close_sheet) returns home
        self.tap(app, "hotkeys.back")
        self.assertEqual(app.ui.screen, "home")
        # 4. context-aware ordering through the real ES event path
        ev = esevents.EsEvent(esevents.GAME_START, "wiiu", "/storage/roms/wiiu/Game.wux",
                              "Game", (), "hook", 0.0, None)
        self.es(app, ev)
        self.assertEqual(app.companion.info.get("system"), "wiiu")
        self.tap(app, "home.hotkeys")
        self.assertEqual(app.ui.hotkeys.pages[0][0].id, "cemu")


if __name__ == "__main__":
    unittest.main()
