"""Undocked (one screen): ES and games own the panel, and the Back button opens the Command
Center over them. There is no single-screen setting any more, and no floating button unless a
corner is picked in Settings."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import config  # noqa: E402
from hidden_overlay import HIDDEN, OVERLAY  # noqa: E402
from test_cc5_overlay import Harness  # noqa: E402

UNDOCKED = "undocked: DP-1 absent"
OLD_SETTING = ("command_center", "single_screen")


class Undocked(Harness):
    def app_undocked(self, cfg=None):
        app = self.make_cc5_app(cfg, reason=UNDOCKED)
        self.query_result = (HIDDEN, UNDOCKED)
        return app

    def test_notes_tab_opens_notes_without_moving_windows(self):
        app = self.app_undocked()
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)

        class RefusesUndocked:                  # what window_switcher answers with no DP-1
            def switch(self, target, *a, **k):
                return {"target": target, "ok": False, "refused": "undocked: DP-1 absent",
                        "error": None, "sent": None}
        app.tabs.switcher = RefusesUndocked()
        app.tabs.select("notes")
        app.post.drain()
        self.assertEqual(app.ui.sheet, "notes")
        self.assertEqual(app.tabs.refusals, 0)

    def test_back_opens_the_full_command_center_over_es(self):
        app = self.app_undocked()
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)
        t = app.ui.targets()
        self.assertIn("home.settings", t)          # the whole Command Center, not the game set
        self.assertNotIn("home.quitgame", t)

    def test_over_a_game_it_shows_the_game_tiles(self):
        app = self.app_undocked()
        app.companion.running = object()           # ES reported a running game
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)
        t = app.ui.targets()
        self.assertIn("home.quitgame", t)
        self.assertNotIn("home.settings", t)

    def test_no_floating_button_by_default(self):
        self.app_undocked()
        self.assertFalse(any(h.visible for h in self.handles))

    def test_a_chosen_corner_shows_the_button(self):
        self.app_undocked({"schema_version": 1, "command_center": {"corner_handle": "bottom-left"}})
        self.assertTrue(self.handles and self.handles[-1].visible)
        self.assertEqual(self.handles[-1].corner, "bottom-left")

    def test_a_saved_old_setting_changes_nothing(self):
        self.app_undocked({"schema_version": 1, "command_center": {"single_screen": True}})
        self.assertFalse(any(h.visible for h in self.handles))

    def test_the_setting_is_gone_from_settings(self):
        self.assertIsNone(config.field_for(OLD_SETTING))
        self.assertNotIn(OLD_SETTING, config.WIRED)

    def test_docked_emulator_window_still_works(self):
        app = self.make_cc5_app()                  # HIDDEN by an emulator's window
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)
        self.assertIn("home.quitgame", app.ui.targets())


if __name__ == "__main__":
    unittest.main()
