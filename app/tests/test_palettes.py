#!/usr/bin/env python3
"""palettes.py: every preset defines every THEME key, Default is
byte-identical to ui.THEME, WCAG contrast thresholds hold for every
shipped preset (text-on-background >= 4.5:1, accent-text-on-accent
>= 3:1, plus this module's own extra danger/ok/warn-vs-background >=
3:1 check - see palettes.py's module docstring for why that third check
exists), Custom derivation, ensure_contrast(), live apply (THEME mutated
in place, not rebound), and the corrupt/unknown-preset fallback to
Default."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config     # noqa: E402
import palettes   # noqa: E402
import ui         # noqa: E402


class TestThemeKeys(unittest.TestCase):
    def test_theme_keys_matches_ui_theme(self):
        self.assertEqual(set(palettes.THEME_KEYS), set(ui.THEME.keys()))

    def test_every_preset_defines_every_key(self):
        for name, p in palettes.PRESETS.items():
            self.assertEqual(set(p.keys()), set(palettes.THEME_KEYS), name)

    def test_every_preset_value_is_a_plain_int(self):
        for name, p in palettes.PRESETS.items():
            for k, v in p.items():
                self.assertIsInstance(v, int, "%s.%s" % (name, k))
                self.assertTrue(0 <= v <= 0xFFFFFF, "%s.%s" % (name, k))

    def test_preset_order_matches_config_schema_values(self):
        # config.py deliberately hand-copies these (stays import-free of
        # palettes.py/ui.py) - this is the test that keeps them in sync.
        self.assertEqual(config.THEME_PRESET_VALUES,
                         palettes.PRESET_ORDER + (palettes.CUSTOM_PRESET,))

    def test_every_preset_has_a_label(self):
        for name in palettes.PRESET_ORDER:
            self.assertIn(name, palettes.PRESET_LABELS)
            self.assertTrue(palettes.PRESET_LABELS[name])

    def test_custom_has_a_label_too(self):
        # Not in PRESET_ORDER/PRESETS (no fixed colours of its own) but IS
        # a config.THEME_PRESET_VALUES enum value, so display_label() needs
        # to handle it.
        self.assertEqual(palettes.PRESET_LABELS[palettes.CUSTOM_PRESET], "Custom")


class TestDisplayLabel(unittest.TestCase):
    """settings_view.py's CycleButton and `palettes.py --list` must show
    the same text for the same preset (device field test 25 Sep found them
    disagreeing - see settings_view.py's _ENUM_DISPLAY_OVERRIDES entry for
    ("appearance", "theme_preset"))."""

    def test_classic_console_presets_get_a_bracketed_console_hint(self):
        for name, console in palettes.PRESET_CONSOLE_HINT.items():
            label = palettes.display_label(name)
            self.assertEqual(label, "%s (%s)" % (palettes.PRESET_LABELS[name], console))

    def test_rp5_and_default_presets_get_no_hint(self):
        for name in ("default", "high_contrast", "rp5_black", "rp5_white",
                    "rp5_16bit", "rp5_gc", "rp5_yellow", "rp5_turquoise"):
            self.assertEqual(palettes.display_label(name), palettes.PRESET_LABELS[name])

    def test_every_preset_order_name_has_a_display_label(self):
        for name in palettes.PRESET_ORDER + (palettes.CUSTOM_PRESET,):
            self.assertTrue(palettes.display_label(name))

    def test_unknown_name_falls_back_to_itself(self):
        self.assertEqual(palettes.display_label("not-a-preset"), "not-a-preset")


class TestDefaultMatchesUiTheme(unittest.TestCase):
    def test_default_preset_equals_ui_theme_exactly(self):
        """The whole point of "default": an existing user sees NO change.
        Compared post-theme_rgba() (the same ui.rgb() conversion ui.py's
        own THEME literals went through), key for key, value for value."""
        self.assertEqual(palettes.theme_rgba(palettes.PRESETS["default"]), ui.THEME)

    def test_default_is_not_built_by_derive_full(self):
        """Insurance against a future refactor routing "default" through
        _derive_full() "for consistency" and drifting off ui.THEME's exact
        literals by a rounding step - this test does not care HOW
        PRESETS["default"] is built, only that today's algorithm is not
        involved: _DEFAULT is a plain module-level dict copied verbatim."""
        self.assertEqual(palettes.PRESETS["default"], palettes._DEFAULT)


class TestContrastMath(unittest.TestCase):
    def test_contrast_white_on_black_is_21_to_1(self):
        self.assertAlmostEqual(palettes.contrast_ratio(0xFFFFFF, 0x000000), 21.0, places=1)

    def test_contrast_is_symmetric(self):
        a, b = 0x123456, 0xABCDEF
        self.assertAlmostEqual(palettes.contrast_ratio(a, b), palettes.contrast_ratio(b, a))

    def test_contrast_same_colour_is_1_to_1(self):
        self.assertAlmostEqual(palettes.contrast_ratio(0x445566, 0x445566), 1.0, places=6)

    def test_meets_body_and_large_thresholds(self):
        self.assertTrue(palettes.meets_body(0xFFFFFF, 0x000000))
        self.assertFalse(palettes.meets_body(0x777777, 0x666666))
        self.assertTrue(palettes.meets_large(0x3D8BFD, 0x12141C))


class TestEnsureContrast(unittest.TestCase):
    def test_already_readable_colour_is_returned_unchanged(self):
        self.assertEqual(palettes.ensure_contrast(0xFFFFFF, 0x000000), 0xFFFFFF)

    def test_unreadable_colour_on_dark_bg_is_pushed_towards_white(self):
        fixed = palettes.ensure_contrast(0x1A1A1A, 0x000000)
        self.assertGreaterEqual(palettes.contrast_ratio(fixed, 0x000000), palettes.LARGE_MIN_RATIO)

    def test_unreadable_colour_on_light_bg_is_pushed_towards_black(self):
        fixed = palettes.ensure_contrast(0xEEEEEE, 0xFFFFFF)
        self.assertGreaterEqual(palettes.contrast_ratio(fixed, 0xFFFFFF), palettes.LARGE_MIN_RATIO)

    def test_hue_stays_recognisable_a_nudged_red_is_still_reddish(self):
        fixed = palettes.ensure_contrast(0xFF6666, 0xFFFFFF)
        r, g, b = palettes.hex_to_bytes(fixed)
        self.assertGreater(r, g)
        self.assertGreater(r, b)


class TestShippedPresetContrast(unittest.TestCase):
    """The two thresholds the brief requires, for every shipped preset, PLUS
    the extra danger/ok/warn-vs-background check this module's own
    docstring documents as the practical reading of "keep semantic colours
    meaningful... readable on that preset's background"."""

    def test_text_on_background_meets_body_threshold(self):
        for name, p in palettes.PRESETS.items():
            ratio = palettes.contrast_ratio(p["text"], p["bg"])
            self.assertGreaterEqual(ratio, palettes.BODY_MIN_RATIO,
                                    "%s: text/bg %.2f:1" % (name, ratio))

    def test_text_on_accent_meets_large_threshold(self):
        for name, p in palettes.PRESETS.items():
            ratio = palettes.contrast_ratio(p["text"], p["accent"])
            self.assertGreaterEqual(ratio, palettes.LARGE_MIN_RATIO,
                                    "%s: text/accent %.2f:1" % (name, ratio))

    def test_danger_ok_warn_readable_on_background(self):
        for name, p in palettes.PRESETS.items():
            for key in ("danger", "ok", "warn"):
                ratio = palettes.contrast_ratio(p[key], p["bg"])
                self.assertGreaterEqual(ratio, palettes.LARGE_MIN_RATIO,
                                        "%s: %s/bg %.2f:1" % (name, key, ratio))


class TestDeriveCustom(unittest.TestCase):
    def test_derive_custom_has_every_key(self):
        th = palettes.derive_custom(0x3D8BFD, 0x12141C, 0xF4F5F7)
        self.assertEqual(set(th.keys()), set(palettes.THEME_KEYS))

    def test_derive_custom_keeps_the_three_picked_colours(self):
        th = palettes.derive_custom(0x3D8BFD, 0x12141C, 0xF4F5F7)
        self.assertEqual((th["accent"], th["bg"], th["text"]), (0x3D8BFD, 0x12141C, 0xF4F5F7))

    def test_derive_custom_danger_ok_warn_stay_readable_on_extreme_backgrounds(self):
        for bg in (0x000000, 0xFFFFFF, 0x808080):
            th = palettes.derive_custom(0x3D8BFD, bg, 0xF4F5F7 if bg != 0xFFFFFF else 0x0A0A0A)
            for key in ("danger", "ok", "warn"):
                ratio = palettes.contrast_ratio(th[key], bg)
                self.assertGreaterEqual(ratio, palettes.LARGE_MIN_RATIO,
                                        "bg=%06X %s %.2f:1" % (bg, key, ratio))

    def test_custom_contrast_warning_none_when_readable(self):
        self.assertIsNone(palettes.custom_contrast_warning(0x3D8BFD, 0x12141C, 0xF4F5F7))

    def test_custom_contrast_warning_present_when_not_readable(self):
        warning = palettes.custom_contrast_warning(0xEEEEEE, 0xDDDDDD, 0xEEEEEE)
        self.assertIsNotNone(warning)
        self.assertIn("Low contrast", warning)

    def test_custom_contrast_warning_names_which_pair_is_bad(self):
        # readable text-on-bg, unreadable text-on-accent
        warning = palettes.custom_contrast_warning(0xF0F0F0, 0x101010, 0xF4F5F7)
        self.assertIn("text on accent", warning)
        self.assertNotIn("text on background", warning)


class TestHsl(unittest.TestCase):
    def test_round_trips_for_a_spread_of_colours(self):
        for hexv in (0x12141C, 0xF4F5F7, 0x3D8BFD, 0x000000, 0xFFFFFF, 0x808080, 0xED3736):
            h, s, l = palettes.rgb_to_hsl(hexv)
            self.assertEqual(palettes.hsl_to_rgb(h, s, l), hexv)

    def test_pure_red_hue_is_zero(self):
        h, s, l = palettes.rgb_to_hsl(0xFF0000)
        self.assertAlmostEqual(h, 0.0, places=1)
        self.assertAlmostEqual(s, 1.0, places=2)
        self.assertAlmostEqual(l, 0.5, places=2)


class TestResolveAndApply(unittest.TestCase):
    def test_resolve_unknown_preset_falls_back_to_default(self):
        self.assertEqual(palettes.resolve_theme_hex("no-such-preset"), palettes.PRESETS["default"])

    def test_resolve_custom_with_garbage_hex_falls_back_to_default_colours(self):
        th = palettes.resolve_theme_hex("custom", "not-a-colour", None, 12345)
        expected = palettes.derive_custom(palettes._DEFAULT["accent"], palettes._DEFAULT["bg"],
                                          palettes._DEFAULT["text"])
        self.assertEqual(th, expected)

    def test_resolve_custom_with_good_hex_strings(self):
        th = palettes.resolve_theme_hex("custom", "#ff0000", "#000000", "#ffffff")
        self.assertEqual((th["accent"], th["bg"], th["text"]), (0xFF0000, 0x000000, 0xFFFFFF))

    def test_theme_for_config_reads_appearance_group(self):
        cfg = config.defaults()
        config.set_value(cfg, ("appearance", "theme_preset"), "high_contrast")
        th = palettes.theme_for_config(cfg)
        self.assertEqual(th, palettes.PRESETS["high_contrast"])

    def test_theme_for_config_missing_appearance_group_is_default(self):
        cfg = {"schema_version": 1}   # a config predating the "appearance" group
        th = palettes.theme_for_config(cfg)
        self.assertEqual(th, palettes.PRESETS["default"])

    def test_apply_theme_mutates_the_given_dict_in_place_not_rebinding(self):
        """The exact bug apply_theme()'s docstring warns about: several
        modules do `from ui import THEME`, binding their OWN name to the
        SAME dict object - a rebind (`d = {...}`) would strand them on the
        old dict. Proven here by keeping our own alias and checking IT
        changed too, not just the object apply_theme() was handed."""
        fake_theme = dict(ui.THEME)
        alias = fake_theme
        cfg = config.defaults()
        config.set_value(cfg, ("appearance", "theme_preset"), "high_contrast")
        palettes.apply_theme(cfg, theme=fake_theme)
        self.assertIs(alias, fake_theme)
        self.assertEqual(fake_theme["bg"], ui.rgb(palettes.PRESETS["high_contrast"]["bg"]))

    def test_apply_theme_a_redraw_uses_the_new_value(self):
        """item 4's "live apply updates THEME and a redraw uses it": a
        Label with the live default colour (see ui.Label's own docstring)
        picks up a just-applied theme with no reconstruction."""
        import ui as ui_mod
        label = ui_mod.Label("hi")   # color=None -> live "text" key
        before = label.color_value()
        cfg = config.defaults()
        config.set_value(cfg, ("appearance", "theme_preset"), "high_contrast")
        try:
            palettes.apply_theme(cfg)
            after = label.color_value()
            self.assertNotEqual(before, after)
            self.assertEqual(after, ui_mod.THEME["text"])
        finally:
            palettes.apply_theme(config.defaults())   # restore ui.THEME for later tests


class TestConfigRoundTrip(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_preset_choice_round_trips_through_save_and_load(self):
        cfg, _ = config.load(self.p)
        config.set_value(cfg, ("appearance", "theme_preset"), "rp5_gc")
        config.save(cfg, self.p)
        reloaded, _ = config.load(self.p)
        self.assertEqual(config.get_value(reloaded, ("appearance", "theme_preset")), "rp5_gc")
        self.assertEqual(palettes.theme_for_config(reloaded), palettes.PRESETS["rp5_gc"])

    def test_custom_colours_round_trip(self):
        cfg, _ = config.load(self.p)
        config.set_value(cfg, ("appearance", "theme_preset"), "custom")
        config.set_value(cfg, ("appearance", "custom_accent"), "#ff8800")
        config.save(cfg, self.p)
        reloaded, _ = config.load(self.p)
        th = palettes.theme_for_config(reloaded)
        self.assertEqual(th["accent"], 0xFF8800)

    def test_corrupt_json_falls_back_to_default_theme(self):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        cfg, note = config.load(self.p)
        self.assertTrue(note)
        self.assertEqual(palettes.theme_for_config(cfg), palettes.PRESETS["default"])

    def test_corrupt_appearance_section_falls_back_to_default_theme(self):
        with open(self.p, "w", encoding="utf-8") as f:
            json.dump({"appearance": {"theme_preset": "not-a-real-preset",
                                      "custom_accent": 12345}}, f)
        cfg, notes = config.load(self.p)
        self.assertTrue(notes)
        self.assertEqual(palettes.theme_for_config(cfg), palettes.PRESETS["default"])


class TestCli(unittest.TestCase):
    def test_list_prints_every_preset_name(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            palettes._cli_list()
        out = buf.getvalue()
        for name in palettes.PRESET_ORDER:
            self.assertIn(name, out)


if __name__ == "__main__":
    unittest.main()
