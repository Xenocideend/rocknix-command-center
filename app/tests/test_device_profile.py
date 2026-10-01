"""The device profile (screen_map): the RP5 keeps everything, the handhelds with two built-in panels have no swap or add-on
tiles, the backlight comes from the profile or the first one the kernel lists, and no module other than screen_map spells
out an output name."""
import ast
import glob
import json
import os
import subprocess
import sys
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import screen_map  # noqa: E402

# model -> (bottom, top): the Command Center's panel is the one ROCKNIX calls second_con and gives its bottom-screen UI to
BUILT_IN_MODELS = {"AYN Thor": ("DSI-1", "DSI-2"), "AYN Thor Lite": ("DSI-2", "DSI-1"),
                   "AYANEO Pocket DS": ("DSI-2", "DSI-1"), "Anbernic RG DS": ("DSI-1", "DSI-2"),
                   "Anbernic RG DS Plus": ("DSI-1", "DSI-2"), "Retroid Pocket Duo": ("DSI-2", "DSI-1")}


class Profiles(unittest.TestCase):
    def test_the_rp5_keeps_every_feature_and_is_verified(self):
        s = screen_map.for_device("Retroid Pocket 5", env={})
        self.assertEqual(s.features, screen_map.FEATURES)
        self.assertTrue(s.verified)
        self.assertEqual(s.backlight, "/sys/class/backlight/ae94000.dsi.0")
        self.assertEqual(s.unsupported_tiles(), [])

    def test_built_in_dual_screen_models_have_no_swap_or_add_on(self):
        for model, (bottom, top) in BUILT_IN_MODELS.items():
            s = screen_map.for_device(model, env={})
            self.assertEqual((s.bottom, s.top, s.top_is_addon), (bottom, top, False), model)
            self.assertEqual(screen_map.kind(s), "builtin", model)
            self.assertFalse(s.has("swap") or s.has("addon"), model)
            self.assertFalse(s.verified, model)
            self.assertEqual(s.unsupported_tiles(), ["home.lights", "home.perf", "home.swap", "home.topscreen"], model)

    def test_a_plus_model_is_not_taken_for_the_plain_one_and_both_are_built_in(self):
        self.assertIs(screen_map.for_device("Anbernic RG DS Plus", env={}), screen_map.RG_DS)
        self.assertIs(screen_map.for_device("Anbernic RG DS", env={}), screen_map.RG_DS)

    def test_the_thor_lite_is_not_taken_for_the_thor(self):
        self.assertIs(screen_map.for_device("AYN Thor Lite", env={}), screen_map.THOR_LITE)
        self.assertIs(screen_map.for_device("AYN Thor", env={}), screen_map.THOR)
        self.assertNotEqual(screen_map.THOR.bottom, screen_map.THOR_LITE.bottom)

    def test_the_touch_names_are_the_ones_rocknix_maps(self):
        self.assertEqual((screen_map.THOR.touch_bottom, screen_map.THOR.touch_top),
                         ("0:0:bottom_touchscreen", "0:0:top_touchscreen"))
        self.assertEqual(screen_map.POCKET_DS.touch_bottom, "1046:967:Goodix_Capacitive_TouchScreen")
        self.assertEqual(screen_map.POCKET_DS.touch_top, "0:0:generic_ft5x06_(44)")
        self.assertIsNone(screen_map.RG_DS.touch_bottom)

    def test_the_env_text_names_what_each_kind_of_daemon_reads(self):
        rp5 = screen_map.env_lines(screen_map.RP5)
        thor = screen_map.env_lines(screen_map.THOR)
        self.assertIn("INTERNAL='DSI-1'\n", rp5)
        self.assertNotIn("BOTTOM_OUTPUT", rp5)
        self.assertIn("BOTTOM_OUTPUT='DSI-1'\nTOP_OUTPUT='DSI-2'\n", thor)
        self.assertNotIn("INTERNAL", thor)
        self.assertNotIn("EXTERNAL", thor)

    def test_the_rp5_needs_the_add_on_daemon_and_the_others_the_built_in_one(self):
        self.assertEqual(screen_map.kind(screen_map.RP5), "addon")
        for s in (screen_map.THOR, screen_map.THOR_LITE, screen_map.POCKET_DS, screen_map.RG_DS, screen_map.DUO):
            self.assertEqual(screen_map.kind(s), "builtin")

    def test_an_unknown_model_keeps_the_rp5_profile(self):
        self.assertIs(screen_map.for_device("Some Other Handheld", env={}), screen_map.RP5)

    def test_an_override_to_two_built_in_outputs_drops_swap_and_add_on(self):
        s = screen_map.for_device("Retroid Pocket 5", env={"RP5DECK_SCREENS": "DSI-2,DSI-1"})
        self.assertFalse(s.has("swap") or s.has("addon"))
        self.assertTrue(s.has("rgb_leds"))
        self.assertFalse(s.verified)

    def test_an_override_to_an_add_on_output_keeps_the_features(self):
        s = screen_map.for_device("Retroid Pocket 5", env={"RP5DECK_SCREENS": "DSI-1,DP-2"})
        self.assertTrue(s.has("swap") and s.has("addon"))

    def test_every_tile_that_needs_a_feature_names_a_real_feature(self):
        self.assertTrue(set(screen_map.TILE_NEEDS.values()) <= screen_map.FEATURES)
        self.assertTrue(set(screen_map.GROUP_NEEDS.values()) <= screen_map.FEATURES)

    def test_settings_groups_follow_the_features(self):
        self.assertEqual(screen_map.RP5.unsupported_groups(), [])
        self.assertEqual(screen_map.BUILTIN_DUAL.unsupported_groups(), ["battery", "dualscreen", "lights"])

    def test_the_device_name_comes_from_the_override_first(self):
        s = screen_map.for_device(None, env={"RP5DECK_DEVICE": "AYN Thor"})
        self.assertIs(s, screen_map.THOR)


class Backlight(unittest.TestCase):
    def test_the_profiles_own_path_wins(self):
        self.assertEqual(screen_map.backlight_path(screen_map.RP5, listdir=lambda p: ["x"]),
                         "/sys/class/backlight/ae94000.dsi.0")

    B = "/sys/class/backlight/"

    def paths(self, screens, names, env=None):
        return screen_map.backlight_paths(screens, listdir=lambda p: list(names), env=env or {})

    def test_a_single_backlight_is_the_bottom_ones_and_the_top_has_none(self):
        self.assertEqual(self.paths(screen_map.THOR, ["only"]), (self.B + "only", None))

    def test_the_rp5_keeps_its_own_bottom_backlight_and_has_no_top_one_whatever_is_listed(self):
        self.assertEqual(self.paths(screen_map.RP5, ["a", "b"]), ("/sys/class/backlight/ae94000.dsi.0", None))

    def test_two_panels_and_two_backlights_follow_the_output_numbers(self):
        # DSI-N takes the Nth backlight in name order
        self.assertEqual(self.paths(screen_map.THOR, ["b", "a"]), (self.B + "a", self.B + "b"))             # bottom DSI-1
        self.assertEqual(self.paths(screen_map.THOR_LITE, ["b", "a"]), (self.B + "b", self.B + "a"))        # bottom DSI-2

    def test_two_backlights_for_outputs_not_named_dsi_n_go_in_name_order(self):
        s = screen_map.THOR._replace(bottom="eDP-1", top="eDP-2")
        self.assertEqual(self.paths(s, ["b", "a"]), (self.B + "a", self.B + "b"))

    def test_three_backlights_are_not_guessed_for(self):
        self.assertEqual(self.paths(screen_map.THOR, ["a", "b", "c"]), (self.B + "a", None))

    def test_the_profile_wins_over_the_guess(self):
        s = screen_map.THOR._replace(backlight="/x/bottom", backlight_top="/x/top")
        self.assertEqual(self.paths(s, ["a", "b"]), ("/x/bottom", "/x/top"))
        s = screen_map.THOR._replace(backlight_top="/x/top")
        self.assertEqual(self.paths(s, ["a", "b"]), (self.B + "a", "/x/top"))

    def test_the_environment_override_wins_over_everything(self):
        self.assertEqual(self.paths(screen_map.THOR, ["a", "b"], env={"RP5DECK_BACKLIGHTS": "/p/1, /p/2"}), ("/p/1", "/p/2"))
        self.assertEqual(self.paths(screen_map.RP5, ["a"], env={"RP5DECK_BACKLIGHTS": "/p/1,/p/2"}), ("/p/1", "/p/2"))

    def test_a_broken_override_is_ignored(self):
        for raw in ("/p/1", "/p/1,", ",/p/2", "a,b,c", ""):
            self.assertEqual(self.paths(screen_map.THOR, ["a", "b"], env={"RP5DECK_BACKLIGHTS": raw}),
                             (self.B + "a", self.B + "b"), raw)

    def test_the_wrappers_return_one_half_each(self):
        self.assertEqual(screen_map.backlight_path(screen_map.THOR, listdir=lambda p: ["a", "b"], env={}), self.B + "a")
        self.assertEqual(screen_map.top_backlight_path(screen_map.THOR, listdir=lambda p: ["a", "b"], env={}), self.B + "b")
        self.assertIsNone(screen_map.top_backlight_path(screen_map.RP5, listdir=lambda p: ["a", "b"], env={}))

    def test_nothing_listed_falls_back_to_the_rp5_path(self):
        self.assertEqual(screen_map.backlight_path(screen_map.BUILTIN_DUAL, listdir=lambda p: []),
                         "/sys/class/backlight/ae94000.dsi.0")

        def boom(p):
            raise OSError("no such dir")
        self.assertEqual(screen_map.backlight_path(screen_map.BUILTIN_DUAL, listdir=boom),
                         "/sys/class/backlight/ae94000.dsi.0")


class Wiring(unittest.TestCase):
    def run_with(self, screens, code, device_name=""):
        env = dict(os.environ, RP5DECK_SCREENS=screens, RP5DECK_DEVICE=device_name)
        r = subprocess.run([sys.executable, "-B", "-c", code], cwd=APP, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    STUB = ("import json, screens\n"
            "class Stub:\n"
            "    def __getattr__(self, n):\n"
            "        return lambda *a, **k: None\n")

    def test_home_leaves_out_the_tiles_the_device_cannot_use(self):
        code = (self.STUB +
                "h = screens.Home(Stub())\n"
                "h.set_rect((0, 0, 1920, 810))\n"
                "print(json.dumps([[t.name for t in h.visible_tiles()], sorted(h.unavailable)]))")
        names, off = self.run_with("DSI-2,DSI-1", code)
        self.assertEqual(off, ["home.swap", "home.topscreen"])      # an override keeps the RP5's other hardware
        for gone in ("home.swap", "home.topscreen"):
            self.assertNotIn(gone, names)
        self.assertIn("home.settings", names)
        names_rp5, off_rp5 = self.run_with("DSI-1,DP-1", code)
        for kept in ("home.swap", "home.topscreen", "home.lights", "home.perf"):
            self.assertIn(kept, names_rp5)
        self.assertFalse(off_rp5)

    def test_an_unavailable_tile_stays_out_in_edit_mode(self):
        code = (self.STUB +
                "h = screens.Home(Stub())\n"
                "h.edit_mode = True\n"
                "print(json.dumps([t.name for t in h.visible_tiles()]))")
        self.assertNotIn("home.swap", self.run_with("DSI-2,DSI-1", code))

    def test_settings_leave_out_the_groups_the_device_cannot_use(self):
        code = "import json, settings_view\nprint(json.dumps(settings_view.GROUP_ORDER))"
        thor = self.run_with("", code, "AYN Thor")
        rp5 = self.run_with("", code, "Retroid Pocket 5")
        self.assertIn("battery", rp5)       # lights and dualscreen have no settings rows of their own today
        for g in ("lights", "battery", "dualscreen"):
            self.assertNotIn(g, thor)
        for g in ("screens", "audio", "about"):
            self.assertIn(g, thor)

    def test_the_device_override_picks_that_models_profile(self):
        code = "import json, screen_map\nprint(json.dumps([screen_map.CURRENT.bottom, screen_map.CURRENT.top]))"
        self.assertEqual(self.run_with("", code, "AYN Thor"), ["DSI-1", "DSI-2"])
        self.assertEqual(self.run_with("", code, "Retroid Pocket 5"), ["DSI-1", "DP-1"])

    def test_the_window_switcher_pattern_follows_the_outputs(self):
        code = ("import json, window_switcher as w\n"
                "print(json.dumps([bool(w.SHOW_RE.search('[con_id=5] move container to output DSI-2')),"
                " bool(w.SHOW_RE.search('[con_id=5] move container to output DP-1'))]))")
        self.assertEqual(self.run_with("DSI-2,DSI-1", code), [True, False])
        self.assertEqual(self.run_with("DSI-1,DP-1", code), [False, True])

    def test_the_backlight_modules_read_the_profile(self):
        code = ("import json, brightness, screen_idle, screen_map\n"
                "print(json.dumps([brightness.BACKLIGHT, screen_idle.BACKLIGHT, screen_map.backlight_path()]))")
        a, b, c = self.run_with("DSI-1,DP-1", code)
        self.assertEqual((a, b), (c, c))

    def test_the_layer_surface_default_follows_the_bottom_output(self):
        src = open(os.path.join(APP, "wl_layer.py"), encoding="utf-8").read()
        self.assertIn("output_name=screen_map.CURRENT.bottom", src)


class NoHardCodedOutputs(unittest.TestCase):
    """Only screen_map names an output. A string anywhere else that holds DSI-1, DSI-2 or DP-1 would break a device whose
    outputs are called something else (docstrings and comments are fine)."""
    NAMES = ("DSI-1", "DSI-2", "DP-1", "ae94000")      # the last is the RP5's backlight

    def literals(self, path):
        tree = ast.parse(open(path, encoding="utf-8").read())
        docs = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                b = n.body
                if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant):
                    docs.add(id(b[0].value))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                if any(x in n.value for x in self.NAMES):
                    yield n.lineno, n.value

    def test_no_module_but_screen_map_spells_out_an_output(self):
        found = []
        for p in sorted(glob.glob(os.path.join(APP, "*.py"))):
            if os.path.basename(p) == "screen_map.py":
                continue
            found += ["%s:%d %r" % (os.path.basename(p), ln, v[:60]) for ln, v in self.literals(p)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
