#!/usr/bin/env python3
"""Appearance wired into the REAL main.App / cc_overlay.OverlayApp (not just
appearance_view.py's own unit tests) - same harness test_companion.py's
TestAppSettings already uses (AppCase: a real App, fakes only for
device I/O/SDL). Proves:
  * Settings > Appearance has a "Colour theme" cycle button and picking a
    preset applies live (ui.THEME) with no restart, and is saved.
  * "Edit custom colours..." opens the dedicated sheet, switches to Custom,
    and its own Back returns to Settings (not Home - the one sheet in this
    app that does that, see main.App.close_appearance()'s docstring).
  * "Reset to default" puts every appearance.* key back and re-applies.
  * cc_overlay.OverlayApp is a SEPARATE process in production (its own
    ui.THEME) - here it shares the test process's `ui` module with the
    main App, so this proves what production can't in one process: that
    OverlayApp._config_poll() itself calls palettes.apply_theme() when it
    notices config.json changed (SW1's existing "the overlay re-reads
    config.json every CONFIG_POLL seconds" mechanism, now covering
    Appearance too)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config      # noqa: E402
import palettes    # noqa: E402
import ui          # noqa: E402
from test_companion import AppCase, RecordingCanvas  # noqa: E402
from sw1_patched_cases import NORMAL, OverlayCase  # noqa: E402


class ThemeRestoringAppCase(AppCase):
    """Every test here drives a real App, which calls palettes.apply_theme()
    against the real ui.THEME (module-level, shared for the lifetime of the
    test process) - restore it so other test files are never handed a
    leftover preset. AppCase itself cleans up via addCleanup(), not
    tearDown(), so overriding tearDown() here does not skip any of that."""

    def tearDown(self):
        palettes.apply_theme(config.defaults())


class TestSettingsAppearanceTab(ThemeRestoringAppCase):
    def test_appearance_tab_reachable_and_preset_cycles_live(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.tap(app, "settings.tab.appearance")
        key = "settings.row.appearance.theme_preset.control"
        self.assertIn(key, app.ui.targets())
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "default")
        self.tap(app, key)   # CycleButton: default -> high_contrast (PRESET_ORDER[1])
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "high_contrast")
        self.assertEqual(ui.THEME["bg"], ui.rgb(palettes.PRESETS["high_contrast"]["bg"]))
        # persisted (SAVE_DELAY-scheduled, like every other setting)
        app._save_now()
        on_disk = config.load(os.environ["RP5DECK_CONFIG"])[0]
        self.assertEqual(config.get_value(on_disk, ("appearance", "theme_preset")), "high_contrast")

    def test_edit_custom_colours_opens_sheet_and_switches_to_custom(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.tap(app, "settings.tab.appearance")
        self.tap(app, "settings.appearance.extra.edit")
        self.assertEqual(app.ui.sheet, "appearance_custom")
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "custom")

    def test_back_from_custom_colours_returns_to_settings_not_home(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.tap(app, "settings.tab.appearance")
        self.tap(app, "settings.appearance.extra.edit")
        self.assertEqual(app.ui.sheet, "appearance_custom")
        self.tap(app, "appearance_custom.back")
        self.assertEqual(app.ui.sheet, "settings")
        # the "Colour theme" row noticed the flip to Custom that happened
        # behind Settings' back (AppearanceController.open())
        self.assertEqual(app.ui.settings.controls[("appearance", "theme_preset")].value, "custom")

    def test_reset_to_default_restores_every_appearance_key(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.tap(app, "settings.tab.appearance")
        self.tap(app, "settings.appearance.extra.edit")   # -> custom
        self.tap(app, "appearance_custom.back")            # -> back to settings
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "custom")
        self.tap(app, "settings.appearance.extra.reset")
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "default")
        self.assertEqual(config.get_value(app.cfg, ("appearance", "custom_accent")),
                         config.field_for(("appearance", "custom_accent"))["default"])
        self.assertEqual(ui.THEME["bg"], ui.rgb(palettes.PRESETS["default"]["bg"]))


class TestFullRepaintAfterHomeRoundTrip(ThemeRestoringAppCase):
    """Device field test 25 Sep (build i10): after Settings.back -> Home ->
    home.settings -> settings.tab.appearance and a FURTHER preset change,
    only the cycle button itself kept updating - the rest of the Settings
    page (background, tabs, other rows, Back, footer) froze at whichever
    preset was showing right before the round trip. ui.THEME WAS correctly
    updated (the log showed every `setting appearance.theme_preset = ...`
    and config.json ended at the right value) - what was missing is
    reproduced here exactly the way the real render loop consumes it
    (main.App._render(): `dmg = self.ui.root.take_damage(); ... with
    self.canvas.clipped(dmg): self.ui.root.paint(self.canvas)` - a widget
    whose rect does not intersect `dmg` never even has draw() called, so a
    correctly-updated THEME cannot save it). Root cause: main.App.on_setting()
    mutated ui.THEME but never marked anything beyond the CycleButton
    itself (which invalidates on tap) as damaged - see its own "appearance"
    branch comment for the fix (self.ui.root.damage_all(), the same idiom
    on_mode()/_rebind()/_output_changed() already use)."""

    def test_a_second_change_after_a_home_round_trip_repaints_everything(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.tap(app, "settings.tab.appearance")
        key = "settings.row.appearance.theme_preset.control"

        self.tap(app, key)                       # default -> high_contrast, BEFORE the round trip
        app.ui.root.take_damage()                # consumed by "the render loop" (not modelled further)

        self.tap(app, "settings.back")           # -> Home
        self.tap(app, "home.settings")           # -> Settings
        self.tap(app, "settings.tab.appearance")  # "reopen Settings > Appearance"
        app.ui.root.take_damage()                # the round trip's own frame, already rendered

        self.tap(app, key)                       # high_contrast -> rp5_black: "change again"
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "rp5_black")

        dmg = app.ui.root.take_damage()
        self.assertEqual(dmg, app.ui.root.rect,
                         "expected the WHOLE surface marked damaged, not just the cycle button")

        # The recording graphics fake: paint exactly what the real render
        # loop would (clipped to `dmg`) and confirm a widget OTHER than the
        # cycle button - Settings' own Back button - actually drew itself
        # with the NEW theme's colour, not a frozen one.
        g = RecordingCanvas()
        g.clip_rect = dmg
        app.ui.root.paint(g)
        back_rect = app.ui.settings.back.rect
        back_fills = [op for op in g.ops if op[0] == "round_rect" and op[1] == back_rect]
        self.assertTrue(back_fills, "Back button did not draw at all - still clipped out")
        self.assertEqual(back_fills[0][3], ui.THEME["tile"])
        self.assertNotEqual(back_fills[0][3], ui.rgb(palettes.PRESETS["high_contrast"]["tile"]))


class TestOverlayFollowsAppearance(OverlayCase):
    def tearDown(self):
        palettes.apply_theme(config.defaults())

    def test_config_poll_applies_a_theme_change_made_elsewhere(self):
        app = self.make_overlay(placement=NORMAL)
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "default")
        # "made elsewhere": the main panel process saves a change; the
        # overlay only ever learns about it by re-reading config.json,
        # same as every other setting _config_poll follows (see its
        # docstring) - never by talking to the other process directly.
        changes = {("appearance", "theme_preset"): "rp5_gc"}
        config.save_changes(changes, path=os.environ["RP5DECK_CONFIG"])
        app.cfg_sig = None   # force _config_poll to notice the file changed
        app._config_poll()
        self.assertEqual(config.get_value(app.cfg, ("appearance", "theme_preset")), "rp5_gc")
        self.assertEqual(ui.THEME["accent"], ui.rgb(palettes.PRESETS["rp5_gc"]["accent"]))


if __name__ == "__main__":
    unittest.main()
