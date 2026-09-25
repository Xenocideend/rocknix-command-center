#!/usr/bin/env python3
"""main.App's "Keyboard" tile end to end: toggling ROCKNIX's on-screen
keyboard (rocknix_keyboard.toggle(), monkeypatched here - never real /proc
or a real signal) closes the Command Center on success, hand-focus back to
the game/ES the same way the Close button does; on failure (nothing
running) it shows the "enable it in ES" hint instead and leaves the panel
open. Reuses test_companion.AppCase like tests/test_tile_customisation.py."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config  # noqa: E402
import main    # noqa: E402
import rocknix_keyboard  # noqa: E402
from test_companion import AppCase  # noqa: E402


class TestKeyboardTile(AppCase):
    def _open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)

    def setUp(self):
        AppCase.setUp(self)
        self._orig_toggle = rocknix_keyboard.toggle
        self.addCleanup(setattr, main.rocknix_keyboard, "toggle", self._orig_toggle)

    def test_present_in_the_tile_customisation_set(self):
        self.assertIn("home.keyboard", config.HOME_TILE_KEYS)

    def test_success_closes_the_command_center(self):
        main.rocknix_keyboard.toggle = lambda: True
        app = self.make_app()
        self._open_cc(app)
        self.assertEqual(app.ui.showing, "cc")
        self.tap(app, "home.keyboard")
        self.assertNotEqual(app.ui.showing, "cc")

    def test_failure_shows_a_hint_and_leaves_the_panel_open(self):
        main.rocknix_keyboard.toggle = lambda: False
        app = self.make_app()
        self._open_cc(app)
        self.tap(app, "home.keyboard")
        self.assertEqual(app.ui.showing, "cc")
        self.assertTrue(app.ui.bar.hinting)
        self.assertIn("Enable Touchscreen Keyboard", app.ui.bar.hint.text)

    def test_hint_clears_itself_after_2_5s(self):
        main.rocknix_keyboard.toggle = lambda: False
        app = self.make_app()
        self._open_cc(app)
        self.tap(app, "home.keyboard")
        self.assertTrue(app.ui.bar.hinting)
        self.run_timers(app, 2.6)
        self.assertFalse(app.ui.bar.hinting)

    def test_never_calls_the_real_proc_scan_in_this_test(self):
        # Confirms the monkeypatch above is what the tile actually calls -
        # not a coincidence of find_pid() legitimately finding nothing.
        calls = []
        main.rocknix_keyboard.toggle = lambda: (calls.append(1), True)[1]
        app = self.make_app()
        self._open_cc(app)
        self.tap(app, "home.keyboard")
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
