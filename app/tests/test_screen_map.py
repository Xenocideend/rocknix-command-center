"""screen_map: which output is which screen, per device and by override."""
import json
import os
import subprocess
import sys
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import screen_map  # noqa: E402


class Profiles(unittest.TestCase):
    def test_rp5_is_the_add_on_layout(self):
        s = screen_map.for_device("Retroid Pocket 5", env={})
        self.assertEqual((s.bottom, s.top, s.top_is_addon), ("DSI-1", "DP-1", True))

    def test_the_duo_uses_two_built_in_panels(self):
        s = screen_map.for_device("Retroid Pocket Duo", env={})
        self.assertEqual((s.bottom, s.top, s.top_is_addon), ("DSI-2", "DSI-1", False))

    def test_the_duo_lite_is_not_taken_for_the_duo(self):
        s = screen_map.for_device("Retroid Pocket Duo Lite", env={})
        self.assertEqual(s, screen_map.DEFAULT)

    def test_an_unknown_device_gets_the_rp5_layout(self):
        self.assertEqual(screen_map.for_device("Handheld", env={}), screen_map.RP5)

    def test_the_override_wins(self):
        s = screen_map.for_device("Retroid Pocket 5", env={"RP5DECK_SCREENS": "DSI-2,DSI-1"})
        self.assertEqual((s.bottom, s.top, s.top_is_addon), ("DSI-2", "DSI-1", False))

    def test_an_override_to_a_dp_output_counts_as_an_add_on(self):
        s = screen_map.for_device("x", env={"RP5DECK_SCREENS": "DSI-1,DP-2"})
        self.assertTrue(s.top_is_addon)

    def test_a_broken_override_is_ignored(self):
        for raw in ("DSI-1", "DSI-1,DSI-1", ",DP-1", "a,b,c"):
            self.assertEqual(screen_map.for_device("Retroid Pocket 5", env={"RP5DECK_SCREENS": raw}),
                             screen_map.RP5, raw)


class Wiring(unittest.TestCase):
    def values_with(self, screens):
        env = dict(os.environ, RP5DECK_SCREENS=screens)
        code = ("import json, sway_ipc, config, window_switcher, es_health, osk, brightness, theme_colour;"
                "print(json.dumps([sway_ipc.INTERNAL, sway_ipc.EXTERNAL, config.SCREEN_TO_OUTPUT,"
                " list(window_switcher.OUTPUTS), es_health.check.__defaults__[1],"
                " brightness.TopDim(spawn=lambda *a, **k: None).output, osk.Wvkbd(popen=None).output,"
                " list(theme_colour.DEFAULT_GRIM_CMD)]))")
        r = subprocess.run([sys.executable, "-B", "-c", code], cwd=APP, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_every_module_follows_the_map(self):
        internal, external, s2o, outputs, es_default, topdim, keyboard, grim = self.values_with("DSI-2,DSI-1")
        self.assertEqual((internal, external), ("DSI-2", "DSI-1"))
        self.assertEqual(s2o, {"builtin_bottom": "DSI-2", "addon_top": "DSI-1"})
        self.assertEqual(outputs, ["DSI-2", "DSI-1"])
        self.assertEqual(es_default, "DSI-1")
        self.assertEqual(topdim, "DSI-1")
        self.assertEqual(keyboard, "DSI-2")
        self.assertIn("DSI-1", grim)
