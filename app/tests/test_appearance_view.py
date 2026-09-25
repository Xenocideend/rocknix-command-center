#!/usr/bin/env python3
"""appearance_view.py: the "Custom colours" sheet's shape (reuses
rgb_view.Swatch/PreviewCircle - not a new picker) and AppearanceController's
wiring - opening always switches to Custom, dragging applies live without
persisting, releasing/tapping persists, the low-contrast warning tracks the
current pick, and Back returns to Settings (not Home, unlike every other
add_sheet() sheet - see AppearanceController's own docstring)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appearance_view as av   # noqa: E402
import config                  # noqa: E402
import palettes                # noqa: E402
import rgb_leds                # noqa: E402
import ui                      # noqa: E402


class FakeUI:
    def __init__(self, outer):
        self.outer = outer
        self.settings = None

    def open(self, name):
        self.outer.opened.append(name)


class FakeHost:
    def __init__(self):
        self.opened = []
        self.closed = 0
        self.state_dirty = False
        self.ui = FakeUI(self)

    def close_appearance(self):
        self.closed += 1


def make_controller(save_calls=None, cfg=None):
    cfg = cfg if cfg is not None else config.defaults()
    host = FakeHost()
    save_calls = save_calls if save_calls is not None else []
    ctrl = av.AppearanceController(host, cfg, save_fn=lambda changes: save_calls.append(changes))
    return host, ctrl, save_calls, cfg


class ThemeIsolatedTestCase(unittest.TestCase):
    """AppearanceController's live-apply mutates the REAL ui.THEME (same
    object main.py's own widgets read - that is the whole point). Any test
    that drives it through open()/action() must restore ui.THEME afterwards
    so later tests (in this file or any other run in the same process) do
    not see a leftover custom/preset theme."""

    def tearDown(self):
        palettes.apply_theme(config.defaults())


class TestSheetShape(ThemeIsolatedTestCase):
    def test_widgets_exist(self):
        sheet = av.AppearanceSheet(lambda name, payload: None)
        self.assertEqual(set(sheet.slot_buttons), {"accent", "bg", "text"})
        self.assertEqual(len(sheet.swatches), len(rgb_leds.PRESETS))
        self.assertEqual(set(sheet.previews), {"accent", "bg", "text"})

    def test_reuses_rgb_view_widget_classes_not_new_ones(self):
        import rgb_view
        sheet = av.AppearanceSheet(lambda name, payload: None)
        for _pname, sw in sheet.swatches:
            self.assertIsInstance(sw, rgb_view.Swatch)
        for _slot, preview in sheet.previews.items():
            self.assertIsInstance(preview, rgb_view.PreviewCircle)

    def test_big_touch_targets(self):
        sheet = av.AppearanceSheet(lambda name, payload: None)
        sheet.layout((0, 0, 1920, 1080))
        for value, btn in sheet.slot_buttons.items():
            w, h = btn.rect[2], btn.rect[3]
            self.assertGreaterEqual(w, 120, value)
            self.assertGreaterEqual(h, 120, value)
        for lbl, slider in ((sheet.hue_label, sheet.hue), (sheet.sat_label, sheet.sat),
                            (sheet.light_label, sheet.light)):
            self.assertGreaterEqual(slider.rect[3], 120)

    def test_back_button_calls_close_action(self):
        seen = []
        sheet = av.AppearanceSheet(lambda name, payload: seen.append((name, payload)))
        sheet.back.clicked()
        self.assertEqual(seen, [("appearance.close", None)])


class TestOpenSwitchesToCustom(ThemeIsolatedTestCase):
    def test_open_sets_theme_preset_to_custom(self):
        host, ctrl, saves, cfg = make_controller()
        self.assertEqual(config.get_value(cfg, ("appearance", "theme_preset")), "default")
        ctrl.open()
        self.assertEqual(config.get_value(cfg, ("appearance", "theme_preset")), "custom")
        self.assertIn({("appearance", "theme_preset"): "custom"}, saves)
        self.assertEqual(host.opened, ["appearance_custom"])

    def test_open_is_a_no_op_on_the_preset_if_already_custom(self):
        cfg = config.defaults()
        config.set_value(cfg, ("appearance", "theme_preset"), "custom")
        host, ctrl, saves, cfg = make_controller(cfg=cfg)
        ctrl.open()
        self.assertEqual(saves, [])   # nothing to persist - already custom

    def test_open_applies_live(self):
        host, ctrl, saves, cfg = make_controller()
        ui.THEME.clear()
        ui.THEME.update(palettes.theme_rgba(palettes.PRESETS["high_contrast"]))
        try:
            ctrl.open()
            self.assertEqual(ui.THEME["bg"], ui.rgb(palettes._DEFAULT["bg"]))
        finally:
            palettes.apply_theme(config.defaults())


class TestSlotEditing(ThemeIsolatedTestCase):
    def test_slot_switch_updates_lit_state(self):
        host, ctrl, saves, cfg = make_controller()
        ctrl.action("appearance.slot", "bg")
        self.assertEqual(ctrl.active_slot, "bg")
        self.assertTrue(ctrl.sheet.slot_buttons["bg"].lit)
        self.assertFalse(ctrl.sheet.slot_buttons["accent"].lit)

    def test_drag_applies_live_but_does_not_persist(self):
        host, ctrl, saves, cfg = make_controller()
        ctrl.open()   # switches to "custom" - otherwise apply_theme() ignores custom_accent entirely
        ctrl.action("appearance.slot", "accent")
        before_saves = len(saves)
        before_accent = config.get_value(cfg, ("appearance", "custom_accent"))
        ctrl.action("appearance.hue", 10)
        after_accent = config.get_value(cfg, ("appearance", "custom_accent"))
        self.assertEqual(len(saves), before_saves)   # no persistence mid-drag
        self.assertNotEqual(after_accent, before_accent)   # cfg (and live THEME) DID change
        self.assertEqual(ui.THEME["accent"],
                         ui.rgb(palettes.hex_str_to_int(after_accent, 0)))

    def test_hue_commit_persists_the_final_value(self):
        host, ctrl, saves, cfg = make_controller()
        ctrl.action("appearance.slot", "accent")
        ctrl.action("appearance.hue", 10)
        ctrl.action("appearance.hue.commit", 10)
        stored = config.get_value(cfg, ("appearance", "custom_accent"))
        self.assertEqual(saves[-1], {("appearance", "custom_accent"): stored})

    def test_preset_swatch_tap_commits_immediately(self):
        host, ctrl, saves, cfg = make_controller()
        ctrl.action("appearance.slot", "text")
        rgb_val = rgb_leds.PRESET_BY_NAME["green"]
        ctrl.action("appearance.preset", rgb_val)
        self.assertEqual(config.get_value(cfg, ("appearance", "custom_text")),
                         "#%06x" % palettes.bytes_to_hex(rgb_val))
        self.assertEqual(saves[-1], {("appearance", "custom_text"):
                                     config.get_value(cfg, ("appearance", "custom_text"))})

    def test_sat_and_lightness_drag_and_commit(self):
        host, ctrl, saves, cfg = make_controller()
        ctrl.action("appearance.slot", "bg")
        ctrl.action("appearance.sat", 0.5)
        ctrl.action("appearance.light", 0.2)
        ctrl.action("appearance.light.commit", 0.2)
        _h, s, l = palettes.rgb_to_hsl(ctrl._hexval("bg"))
        self.assertAlmostEqual(l, 0.2, places=2)
        self.assertTrue(saves)


class TestWarningAndPreview(ThemeIsolatedTestCase):
    def test_warning_label_reflects_current_pick(self):
        host, ctrl, saves, cfg = make_controller()
        self.assertEqual(ctrl.sheet.warning.text, "")   # defaults are readable
        config.set_value(cfg, ("appearance", "custom_bg"), "#dddddd")
        config.set_value(cfg, ("appearance", "custom_text"), "#eeeeee")
        ctrl.refresh()
        self.assertIn("Low contrast", ctrl.sheet.warning.text)

    def test_previews_show_the_three_current_colours(self):
        host, ctrl, saves, cfg = make_controller()
        ctrl.refresh()
        for slot in ("accent", "bg", "text"):
            expected = palettes.hex_to_bytes(ctrl._hexval(slot))
            self.assertEqual(ctrl.sheet.previews[slot].rgb_val, expected)


class TestClose(ThemeIsolatedTestCase):
    def test_close_action_calls_host_close_appearance(self):
        host, ctrl, saves, cfg = make_controller()
        ctrl.action("appearance.close", None)
        self.assertEqual(host.closed, 1)


if __name__ == "__main__":
    unittest.main()
