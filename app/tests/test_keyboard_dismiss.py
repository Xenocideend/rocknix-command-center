#!/usr/bin/env python3
"""The keyboard's dismiss arrow. ROCKNIX's wvkbd reserves its height at the bottom of the screen, so the panel gets a
shorter surface (1920x360 under a 720 px keyboard) with nothing on it that reaches the Keyboard tile. main.App shows
an arrow on top of everything while that is the case, and tapping it sends the keyboard its toggle signal. The keyboard
process, sway and the signal are replaced here, like test_keyboard_tile."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import main  # noqa: E402
import rocknix_keyboard  # noqa: E402
import sway_ipc  # noqa: E402
from test_companion import AppCase  # noqa: E402

FULL_H = 1080


class TestKeyboardDismissArrow(AppCase):
    def setUp(self):
        AppCase.setUp(self)
        for mod, name in ((rocknix_keyboard, "find_pid"), (rocknix_keyboard, "toggle"),
                          (sway_ipc, "output_height")):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
        self.signals = []
        rocknix_keyboard.find_pid = lambda *a, **k: 4242
        rocknix_keyboard.toggle = lambda *a, **k: self.signals.append("toggle") or True
        sway_ipc.output_height = lambda name, timeout=1.0: FULL_H

    def app_with(self, surface_h, mode=None):
        app = self.make_app()
        app.mode = mode or main.FULL
        app.surface = (1920, surface_h)
        app.ui.set_size(1920, surface_h)            # what _resize does before it asks for the arrow
        app._update_keyboard_dismiss()
        return app

    def arrow_shown(self, app):
        return "kb.dismiss" in app.ui.targets()

    def test_a_panel_squeezed_by_the_keyboard_gets_the_arrow(self):
        app = self.app_with(360)
        self.assertTrue(app.keyboard_up)
        self.assertTrue(self.arrow_shown(app))

    def test_a_full_size_panel_does_not(self):
        app = self.app_with(FULL_H)
        self.assertFalse(app.keyboard_up)
        self.assertFalse(self.arrow_shown(app))

    def test_the_arrow_needs_the_keyboard_to_be_running(self):
        rocknix_keyboard.find_pid = lambda *a, **k: None
        app = self.app_with(360)
        self.assertFalse(self.arrow_shown(app))

    def test_the_140_pixel_strip_is_not_mistaken_for_a_keyboard(self):
        app = self.app_with(140, mode=main.BAR)
        self.assertFalse(self.arrow_shown(app))

    def test_an_unreadable_sway_shows_no_arrow(self):
        sway_ipc.output_height = lambda name, timeout=1.0: None
        app = self.app_with(360)
        self.assertFalse(self.arrow_shown(app))

    def test_a_little_shorter_is_not_a_keyboard(self):
        app = self.app_with(FULL_H - 100)
        self.assertFalse(self.arrow_shown(app))

    def test_tapping_the_arrow_sends_the_toggle_and_the_arrow_goes(self):
        app = self.app_with(360)
        self.tap(app, "kb.dismiss")
        self.assertEqual(self.signals, ["toggle"])
        self.assertFalse(app.keyboard_up)
        self.assertFalse(self.arrow_shown(app))

    def test_a_failed_signal_leaves_the_arrow_up(self):
        rocknix_keyboard.toggle = lambda *a, **k: False
        app = self.app_with(360)
        self.tap(app, "kb.dismiss")
        self.assertTrue(self.arrow_shown(app))

    def test_the_arrow_is_on_top_and_inside_the_squeezed_surface(self):
        app = self.app_with(360)
        x, y, w, h = app.ui.targets()["kb.dismiss"]
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + h, 360)
        self.assertLessEqual(x + w, 1920)
        self.assertEqual(app.ui.root.hit(x + w // 2, y + h // 2).name, "kb.dismiss")
        self.assertIs(app.ui.root.children[-1], app.ui.kb_dismiss)     # drawn last, hit first

    def test_when_the_surface_grows_back_the_arrow_goes(self):
        app = self.app_with(360)
        app.surface = (1920, FULL_H)
        app.ui.set_size(1920, FULL_H)
        app._update_keyboard_dismiss()
        self.assertFalse(self.arrow_shown(app))

    def test_a_resize_asks_for_the_arrow_after_the_ui_has_its_new_size(self):
        import inspect
        src = inspect.getsource(main.App._resize)
        self.assertIn("self._update_keyboard_dismiss()", src)
        self.assertLess(src.index("self.ui.set_size(w, h)"), src.index("self._update_keyboard_dismiss()"))


if __name__ == "__main__":
    unittest.main()
