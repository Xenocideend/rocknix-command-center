#!/usr/bin/env python3
"""Tile customisation, end to end through the real main.App (build_ui(),
config.json persistence) - reuses test_companion.AppCase (the same real-App
harness cc6_patched_cases.py/sw1_patched_cases.py already build on) rather
than a second one, per the module's own note that its regression suites are
"ordinary modules of the full run"."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config  # noqa: E402
from test_companion import AppCase, W, H  # noqa: E402


class TestDefaultsAndStartup(AppCase):
    def test_home_starts_with_config_defaults(self):
        app = self.make_app()
        self.assertEqual(app.ui.cc.home.order, list(config.HOME_TILE_KEYS))
        self.assertEqual(app.ui.cc.home.hidden, set())

    def test_a_stored_order_and_hidden_set_are_applied_at_startup(self):
        stored = list(reversed(config.HOME_TILE_KEYS))
        app = self.make_app(cfg_patch={"schema_version": 1,
                                       "command_center": {"tile_order": stored,
                                                          "hidden_tiles": ["home.mixer"]}})
        self.assertEqual(app.ui.cc.home.order, stored)
        self.assertEqual(app.ui.cc.home.hidden, {"home.mixer"})


class TestDragPersists(AppCase):
    def _open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)         # AppCase.swipe: opens the Command Center

    def _drag_far(self, app, name):
        t = app.ui.targets()[name]
        x, y, w, h = t
        pid = ("f", 1)
        app.router.down(pid, x + w / 2, y + h / 2)
        app.router.move(pid, x + w / 2 + 1400, y + h / 2)
        app.router.up(pid, x + w / 2 + 1400, y + h / 2)

    def test_a_drag_drop_writes_tile_order_to_config_json(self):
        app = self.make_app()
        self._open_cc(app)
        app.ui.cc.home.enter_edit()
        self._drag_far(app, "home.mixer")
        with open(os.environ["RP5DECK_CONFIG"]) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["command_center"]["tile_order"], app.ui.cc.home.order)
        self.assertNotEqual(on_disk["command_center"]["tile_order"][0], "home.mixer")

    def test_a_fresh_load_after_a_drag_sees_the_new_order(self):
        app = self.make_app()
        self._open_cc(app)
        app.ui.cc.home.enter_edit()
        self._drag_far(app, "home.hud")
        wanted = list(app.ui.cc.home.order)
        cfg2, _note = config.load(path=os.environ["RP5DECK_CONFIG"])
        self.assertEqual(config.get_value(cfg2, ("command_center", "tile_order")), wanted)


class TestHidePersists(AppCase):
    def _open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)

    def test_hiding_a_tile_persists_and_it_leaves_the_normal_grid(self):
        app = self.make_app()
        self._open_cc(app)
        app.ui.cc.home.enter_edit()
        app.ui.cc.home._toggle_hidden("home.browser")
        app.ui.cc.home.exit_edit()
        self.assertNotIn("home.browser", app.ui.targets())
        cfg2, _note = config.load(path=os.environ["RP5DECK_CONFIG"])
        self.assertEqual(config.get_value(cfg2, ("command_center", "hidden_tiles")),
                         ["home.browser"])

    def test_settings_tile_cannot_be_hidden_end_to_end(self):
        app = self.make_app()
        self._open_cc(app)
        app.ui.cc.home.enter_edit()
        self.assertNotIn("home.settings", app.ui.cc.home.badges)
        app.ui.cc.home._toggle_hidden("home.settings")
        app.ui.cc.home.exit_edit()
        self.assertIn("home.settings", app.ui.targets())

    def test_reset_tile_layout_persists_the_defaults(self):
        app = self.make_app()
        self._open_cc(app)
        app.ui.cc.home.enter_edit()
        app.ui.cc.home._toggle_hidden("home.discord")
        app.ui.cc.home.reset_layout()
        cfg2, _note = config.load(path=os.environ["RP5DECK_CONFIG"])
        self.assertEqual(config.get_value(cfg2, ("command_center", "tile_order")),
                         list(config.HOME_TILE_KEYS))
        self.assertEqual(config.get_value(cfg2, ("command_center", "hidden_tiles")), [])


if __name__ == "__main__":
    unittest.main()
