#!/usr/bin/env python3
"""CC2 + CC3's settings, against the REAL merged config.py / settings_view.py
(I2, 24 Sep: patches/CC23-*.patch are applied; this replaces
tests/test_cc23_patches.py, which applied them to a temp copy and ran the
same checks in a subprocess)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config  # noqa: E402
import settings_view  # noqa: E402

KEY = ("companion", "in_game_display")


class TestCC23Settings(unittest.TestCase):
    def test_in_game_display_is_wired_and_defaults_to_art(self):
        # CC2: "art" = the pre-CC2 behaviour for anyone who never opens Settings
        self.assertEqual(config.get_value(config.defaults(), KEY), "art")
        self.assertIn(KEY, config.WIRED)
        self.assertEqual(set(config.field_for(KEY)["values"]),
                         {"art", "dim", "off", "manual", "hud", "clock", "slideshow"})

    def test_cc3_media_kinds_are_valid_and_in_the_default_priority(self):
        self.assertIn("cartridge", config.MEDIA_VALUES)
        self.assertIn("boxback", config.MEDIA_VALUES)
        prio = config.field_for(("companion", "media_priority"))["default"]
        self.assertEqual(sorted(config.MEDIA_VALUES), sorted(prio))

    def test_playtime_is_wired_and_off_by_default(self):
        k = ("companion", "show_metadata", "playtime")
        self.assertIn(k, config.WIRED)
        self.assertFalse(config.get_value(config.defaults(), k))

    def test_the_sheet_has_a_control_for_every_wired_key_and_readable_labels(self):
        sheet = settings_view.SettingsSheet(config.defaults(), on_change=lambda *a: None,
                                            on_close=lambda: None)
        self.assertEqual(sorted(".".join(k) for k in sheet.controls),
                         sorted(".".join(k) for k in config.WIRED))
        control = sheet.controls[KEY]
        labels = {}
        for v in config.field_for(KEY)["values"]:
            control.set_value(v)
            labels[v] = control.text
        self.assertTrue(all(isinstance(t, str) and t for t in labels.values()), labels)
        self.assertEqual(labels["hud"], "HUD (device stats)")
        self.assertEqual(labels["off"], "Off (blank)")


if __name__ == "__main__":
    unittest.main()
