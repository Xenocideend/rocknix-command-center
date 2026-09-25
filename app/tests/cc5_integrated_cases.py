#!/usr/bin/env python3
"""CC5 cases that need the PATCHED tree: SW1's main.py / screens.py patches
plus CC5's config.py / settings_view.py / cc_overlay.py patches.

I2 (24 Sep): the patches are merged into the real files; tests/test_cc5_merged.py
runs this module against the working tree.

Proves there is ONE overlay per Back press (Main's reconcile request): the
panel (main.App with CC5, SW1-patched) and the game-screen overlay
(cc_overlay.OverlayApp, CC5-patched) both receive the same press; the
overlay reads the panel's REAL state.json (written by the panel's own
write_state), in either order of arrival.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cc_overlay  # noqa: E402
import config  # noqa: E402
import hidden_overlay  # noqa: E402
import settings_view  # noqa: E402
import summon  # noqa: E402
from hidden_overlay import OVERLAY  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN  # noqa: E402
from sw1_patched_cases import OverlayCase, PanelCase, summon_ev  # noqa: E402

PULL = summon.PullDownStateMachine
WINDOW = "foreign window on DSI-1: [w2] melonDS"


class TestSchema(unittest.TestCase):
    def test_keys_defaults_wired_and_controls(self):
        d = config.defaults()
        self.assertIs(config.get_value(d, hidden_overlay.KEY_OVERLAY_ON_HIDDEN), True)
        self.assertEqual(config.get_value(d, hidden_overlay.KEY_CORNER_HANDLE), "off")
        self.assertIn(hidden_overlay.KEY_OVERLAY_ON_HIDDEN, config.WIRED)
        self.assertIn(hidden_overlay.KEY_CORNER_HANDLE, config.WIRED)
        f = config.field_for(hidden_overlay.KEY_CORNER_HANDLE)
        self.assertEqual(tuple(f["values"]), hidden_overlay.CORNERS)
        self.assertFalse(f["restart"])
        self.assertFalse(config.field_for(hidden_overlay.KEY_OVERLAY_ON_HIDDEN)["restart"])
        cfg = config.defaults()
        self.assertFalse(config.set_value(cfg, hidden_overlay.KEY_CORNER_HANDLE, "middle"))
        self.assertTrue(config.set_value(cfg, hidden_overlay.KEY_CORNER_HANDLE, "top-left"))
        self.assertEqual(hidden_overlay.setting_corner_handle(cfg), "top-left")
        sheet = settings_view.SettingsSheet(config.defaults(), lambda *a: None, lambda: None)
        self.assertEqual(set(sheet.controls), set(config.WIRED))
        c = sheet.controls[hidden_overlay.KEY_CORNER_HANDLE]
        labels = []
        for v in hidden_overlay.CORNERS:
            c.set_value(v)
            labels.append(c.text)
        self.assertEqual(labels, ["Off", "Top left", "Top right", "Bottom left",
                                  "Bottom right"])


class Both(PanelCase, OverlayCase):
    def make_both(self, cfg=None, panel_mode=HIDDEN, reason=WINDOW):
        panel = self.make_panel(cfg=cfg)
        panel.summon_reader = object()          # the button reader is running
        panel.backend.get_master = lambda: {"state": "ok", "volume": 0.5, "muted": False}
        panel.cc5.query = lambda: (HIDDEN, reason)
        if panel_mode != FULL:
            panel.on_mode(panel_mode, reason)
        panel.post.drain()
        panel.write_state()
        overlay = self.make_overlay()
        return panel, overlay

    def press(self, first, second):
        for app in (first, second):
            app.on_summon_button(summon_ev())
            app.post.drain()
            if hasattr(app, "cc5"):
                app.write_state()


class TestOneOverlayPerPress(Both):
    def test_panel_hidden_by_a_game_opens_only_the_panel_overlay_either_order(self):
        for order in ("overlay-first", "panel-first"):
            with self.subTest(order):
                panel, overlay = self.make_both()
                pair = (overlay, panel) if order == "overlay-first" else (panel, overlay)
                self.press(*pair)
                self.assertEqual(panel.mode, OVERLAY)
                self.assertEqual(overlay.pull.state, PULL.COMPANION)
                self.assertNotEqual(overlay.mode, FULL)
                # the next press closes the panel's and still opens nothing up there
                self.press(*pair)
                self.assertEqual(panel.mode, HIDDEN)
                self.assertTrue(panel.layer.hidden)
                self.assertEqual(overlay.pull.state, PULL.COMPANION)

    def test_setting_off_gives_the_press_back_to_the_game_screen(self):
        panel, overlay = self.make_both(cfg={"schema_version": 1, "command_center":
                                             {"overlay_on_hidden": False}})
        self.assertFalse(panel.state()["cc5"]["takes_summon"])
        self.press(panel, overlay)
        self.assertEqual(panel.mode, HIDDEN)
        self.assertEqual(overlay.pull.state, PULL.COMMAND_CENTER)

    def test_no_reader_no_claim(self):
        panel, overlay = self.make_both()
        panel.summon_reader = None
        panel.write_state()
        overlay.on_summon_button(summon_ev())
        self.assertEqual(overlay.pull.state, PULL.COMMAND_CENTER)

    def test_full_and_bar_keep_sw1s_rules(self):
        panel, overlay = self.make_both(panel_mode=FULL)
        self.press(overlay, panel)
        self.assertEqual(panel.pull.state, PULL.COMMAND_CENTER)
        self.assertEqual(overlay.pull.state, PULL.COMPANION)
        panel, overlay = self.make_both(panel_mode=BAR, reason="rp5deck window on DSI-1: x")
        self.press(overlay, panel)
        self.assertEqual(panel.mode, BAR)
        self.assertEqual(overlay.pull.state, PULL.COMMAND_CENTER)

    def test_panel_takes_summon_truth_table(self):
        t = cc_overlay.panel_takes_summon
        self.assertFalse(t(None))
        self.assertFalse(t({"mode": HIDDEN}))
        self.assertFalse(t({"mode": FULL, "cc5": {"takes_summon": True}}))
        self.assertFalse(t({"mode": BAR, "cc5": {"takes_summon": True}}))
        self.assertFalse(t({"mode": HIDDEN, "cc5": {"takes_summon": False}}))
        self.assertTrue(t({"mode": HIDDEN, "cc5": {"takes_summon": True}}))
        self.assertTrue(t({"mode": OVERLAY, "cc5": {"takes_summon": True}}))


if __name__ == "__main__":
    unittest.main()
