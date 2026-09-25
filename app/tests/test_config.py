#!/usr/bin/env python3
"""config.load()/save(): schema v1 defaults, every validation branch,
migration from the pre-schema {"output", "es_output"} shape, the
opposite-screens rule, atomic save, unknown-key preservation, and the exact
way main.py consumes the result."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, data):
        with open(self.p, "w", encoding="utf-8") as f:
            json.dump(data, f)


class TestDefaults(ConfigTestCase):
    def test_missing_file_gives_every_schema_default(self):
        cfg, note = config.load(self.p)
        self.assertIn("defaults", note)
        for f in config.SCHEMA:
            self.assertEqual(config.get_value(cfg, f["key_path"]), f["default"], f["key_path"])

    def test_legacy_output_keys_derived_from_screen_defaults(self):
        cfg, _ = config.load(self.p)
        # R6 defaults: ES on the add-on (top, DP-1), Command Center on the
        # built-in bottom panel (DSI-1) - matches today's config.py DEFAULTS.
        self.assertEqual(cfg["es_output"], "DP-1")
        self.assertEqual(cfg["output"], "DSI-1")

    def test_defaults_helper_is_a_fresh_copy_each_time(self):
        a, b = config.defaults(), config.defaults()
        a["companion"]["media_priority"].append("x")
        self.assertNotIn("x", b["companion"]["media_priority"])

    def test_schema_has_no_duplicate_key_paths(self):
        paths = [f["key_path"] for f in config.SCHEMA]
        self.assertEqual(len(paths), len(set(paths)))

    def test_every_field_has_a_group_and_label(self):
        for f in config.SCHEMA:
            self.assertIn(f["group"], config.GROUPS, f["key_path"])
            self.assertTrue(f["label"])

    def test_owner_approved_changes_over_r6(self):
        # manuals.viewer gains "native" and defaults to it (R7: poppler/pdftoppm present)
        viewer = config.field_for(("manuals", "viewer"))
        self.assertEqual(set(viewer["values"]), {"native", "firefox_pdfjs"})
        self.assertEqual(viewer["default"], "native")

    def test_hardware_button_defaults_to_back_f1_not_a_paddle(self):
        # Device-verified 23 Sep: the RP5 has no rear paddles at all (0
        # evdev events for either paddle code in 120 s; BTN_BACK/F1 fired
        # reliably) - the default must never regress to a paddle value.
        btn = config.field_for(("command_center", "hardware_button"))
        self.assertEqual(btn["default"], "btn_back_f1")
        self.assertNotIn(btn["default"], config.PADDLE_HARDWARE_BUTTONS)
        # the paddle values must still be valid choices (some other device
        # might have real paddles) - only the DEFAULT changed
        self.assertIn("btn_c_paddle", btn["values"])
        self.assertIn("btn_z_paddle", btn["values"])


class TestWired(unittest.TestCase):
    """RV4-M2 / RV5b check 12: four schema keys had a working Settings
    control and zero effect. WIRED names every key the app actually
    consumes; settings_view.py is only allowed to build a control for a
    WIRED key (tests/test_settings_view.py::TestCoverage asserts that)."""

    def test_wired_is_a_subset_of_schema(self):
        all_paths = set(f["key_path"] for f in config.SCHEMA)
        self.assertTrue(config.WIRED <= all_paths)

    def test_known_dead_settings_are_excluded(self):
        for path in [("companion", "brightness_pct"),
                     ("companion", "background_blur"),
                     ("manuals", "viewer"),
                     ("audio", "volume_step_pct")]:
            self.assertNotIn(path, config.WIRED, path)

    def test_swap_is_wired_and_the_derived_screen_is_not(self):
        # SW1: es_screen is the "Swap screens" toggle (092 + screen_swap.py
        # consume it); command_center_screen is always its opposite and must
        # never get a control of its own (the RV1-M4 "dangerous combo").
        self.assertIn(("screens", "es_screen"), config.WIRED)
        self.assertNotIn(("screens", "command_center_screen"), config.WIRED)
        self.assertFalse(config.field_for(("screens", "es_screen"))["restart"])

    def test_game_screen_tab_is_wired_and_on_by_default(self):
        self.assertIn(("command_center", "game_screen_tab"), config.WIRED)
        self.assertIs(config.field_for(("command_center", "game_screen_tab"))["default"], True)


class TestHardwareButtonPaddleNote(ConfigTestCase):
    """A stored command_center.hardware_button of "btn_c_paddle"/
    "btn_z_paddle" (e.g. from a different device, or from before the
    default changed) must still load AS-IS - only a note is added, the
    value itself is never overridden the way screens.* is."""

    def test_stored_paddle_value_loads_as_is_with_a_note(self):
        self.write({"schema_version": 1,
                   "command_center": {"hardware_button": "btn_c_paddle"}})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["command_center"]["hardware_button"], "btn_c_paddle")
        self.assertIn("hardware_button", note)
        self.assertIn("no rear", note)

    def test_other_paddle_value_also_noted(self):
        self.write({"schema_version": 1,
                   "command_center": {"hardware_button": "btn_z_paddle"}})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["command_center"]["hardware_button"], "btn_z_paddle")
        self.assertIn("no rear", note)

    def test_default_load_has_no_paddle_note(self):
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["command_center"]["hardware_button"], "btn_back_f1")
        self.assertNotIn("no rear", note)

    def test_non_paddle_stored_value_has_no_paddle_note(self):
        self.write({"schema_version": 1,
                   "command_center": {"hardware_button": "none"}})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["command_center"]["hardware_button"], "none")
        self.assertNotIn("no rear", note)


class TestInvalidJSON(ConfigTestCase):
    def test_missing_file_note(self):
        cfg, note = config.load(self.p)
        self.assertIn("no %s" % self.p, note)

    def test_invalid_json_gives_defaults_and_says_so(self):
        with open(self.p, "w") as f:
            f.write("{output: DSI-1")
        cfg, note = config.load(self.p)
        self.assertEqual(cfg, config.defaults())
        self.assertIn("IGNORED", note)

    def test_top_level_not_an_object_gives_defaults(self):
        with open(self.p, "w") as f:
            json.dump([1, 2, 3], f)
        cfg, note = config.load(self.p)
        self.assertEqual(cfg, config.defaults())
        self.assertIn("IGNORED", note)
        self.assertIn("not an object", note)


class TestValidationBranches(ConfigTestCase):
    """Every SCHEMA type, hit with a wrong-type / out-of-range / unknown-enum
    value: that key alone must fall back to its own default, with a note -
    never a crash, never contaminating a sibling key."""

    def _load_with(self, patch):
        data = {"schema_version": 1}
        data.update(patch)
        self.write(data)
        return config.load(self.p)

    def test_bool_wrong_type(self):
        cfg, note = self._load_with({"companion": {"play_video": "yes"}})
        self.assertEqual(cfg["companion"]["play_video"], True)
        self.assertIn("invalid companion.play_video", note)

    def test_int_wrong_type(self):
        cfg, note = self._load_with({"companion": {"brightness_pct": "80"}})
        self.assertEqual(cfg["companion"]["brightness_pct"], 80)
        self.assertIn("invalid companion.brightness_pct", note)

    def test_int_out_of_range_low(self):
        cfg, note = self._load_with({"companion": {"brightness_pct": 0}})
        self.assertEqual(cfg["companion"]["brightness_pct"], 80)
        self.assertIn("invalid companion.brightness_pct", note)

    def test_int_out_of_range_high(self):
        cfg, note = self._load_with({"audio": {"volume_step_pct": 21}})
        self.assertEqual(cfg["audio"]["volume_step_pct"], 5)

    def test_bool_disguised_as_int_is_rejected(self):
        # isinstance(True, int) is True in Python: guard against a bool
        # silently passing an int field's validation.
        cfg, note = self._load_with({"companion": {"video_start_delay_ms": True}})
        self.assertEqual(cfg["companion"]["video_start_delay_ms"], 500)

    def test_float_out_of_range(self):
        cfg, note = self._load_with({"companion": {"background_dim": 1.5}})
        self.assertEqual(cfg["companion"]["background_dim"], 0.15)

    def test_float_accepts_an_int_json_value(self):
        cfg, note = self._load_with({"companion": {"background_dim": 1}})
        self.assertEqual(cfg["companion"]["background_dim"], 1.0)
        self.assertNotIn("invalid", note)

    def test_enum_unknown_value(self):
        cfg, note = self._load_with({"companion": {"image_fit": "stretch"}})
        self.assertEqual(cfg["companion"]["image_fit"], "fit")
        self.assertIn("invalid companion.image_fit", note)

    def test_enum_wrong_type(self):
        cfg, note = self._load_with({"manuals": {"open_mode": 1}})
        self.assertEqual(cfg["manuals"]["open_mode"], "on_tap")

    def test_array_enum_wrong_type(self):
        cfg, note = self._load_with({"companion": {"media_priority": "video"}})
        self.assertEqual(cfg["companion"]["media_priority"], list(config.MEDIA_VALUES))
        self.assertIn("invalid companion.media_priority", note)

    def test_array_enum_unknown_member(self):
        bad = list(config.MEDIA_VALUES)
        bad[0] = "boxart"
        cfg, note = self._load_with({"companion": {"media_priority": bad}})
        self.assertEqual(cfg["companion"]["media_priority"], list(config.MEDIA_VALUES))

    def test_array_enum_duplicate_member(self):
        bad = list(config.MEDIA_VALUES[:-1]) + [config.MEDIA_VALUES[0]]
        cfg, note = self._load_with({"companion": {"media_priority": bad}})
        self.assertEqual(cfg["companion"]["media_priority"], list(config.MEDIA_VALUES))

    def test_array_enum_missing_member_is_incomplete(self):
        bad = list(config.MEDIA_VALUES[:-1])       # dropped one value
        cfg, note = self._load_with({"companion": {"media_priority": bad}})
        self.assertEqual(cfg["companion"]["media_priority"], list(config.MEDIA_VALUES))

    def test_array_enum_valid_reorder_is_kept(self):
        reordered = list(reversed(config.MEDIA_VALUES))
        cfg, note = self._load_with({"companion": {"media_priority": reordered}})
        self.assertEqual(cfg["companion"]["media_priority"], reordered)
        self.assertNotIn("invalid", note)

    def test_nested_show_metadata_field(self):
        cfg, note = self._load_with({"companion": {"show_metadata": {"rating": "maybe"}}})
        self.assertEqual(cfg["companion"]["show_metadata"]["rating"], False)
        self.assertIn("invalid companion.show_metadata.rating", note)

    def test_one_bad_key_does_not_touch_its_siblings(self):
        cfg, note = self._load_with({"companion": {"play_video": "yes", "image_fit": "crop"}})
        self.assertEqual(cfg["companion"]["play_video"], True)     # fell back
        self.assertEqual(cfg["companion"]["image_fit"], "crop")    # kept

    def test_valid_values_all_round_trip(self):
        patch = {
            "companion": {"play_video": False, "video_audio": "muted",
                          "video_start_delay_ms": 1200, "background_dim": 0.4},
            "manuals": {"open_mode": "auto_on_game_start", "viewer": "firefox_pdfjs"},
            "command_center": {"hardware_button": "btn_z_paddle", "auto_close_timeout_s": 30},
            "audio": {"volume_step_pct": 10, "show_volume_overlay": False},
        }
        cfg, note = self._load_with(patch)
        self.assertEqual(cfg["companion"]["video_audio"], "muted")
        self.assertEqual(cfg["manuals"]["viewer"], "firefox_pdfjs")
        self.assertEqual(cfg["command_center"]["hardware_button"], "btn_z_paddle")
        self.assertEqual(cfg["audio"]["volume_step_pct"], 10)
        self.assertNotIn("invalid", note)


class TestMigration(ConfigTestCase):
    def test_old_shape_defaults_migrate_to_the_same_screens(self):
        self.write({"output": "DSI-1", "es_output": "DP-1"})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["screens"]["command_center_screen"], "builtin_bottom")
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
        self.assertEqual(cfg["output"], "DSI-1")
        self.assertEqual(cfg["es_output"], "DP-1")
        self.assertIn("migrated", note)

    def test_old_shape_swapped_screens_follow_what_092_will_do(self):
        # Old-shape output=DP-1/es_output=DSI-1 migrates to the SWAPPED
        # screens combo - but 092 reads only screens.es_screen, which this
        # file does not have, so 092 keeps ES on the add-on. load() must say
        # the same (SW1 shared contract), or rp5deck and 092 would disagree
        # about where ES is.
        self.write({"output": "DP-1", "es_output": "DSI-1"})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["screens"]["command_center_screen"], "builtin_bottom")
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
        self.assertEqual(cfg["output"], "DSI-1")
        self.assertEqual(cfg["es_output"], "DP-1")
        self.assertIn("migrated", note)
        self.assertIn("not in the file as 092 reads it", note)

    def test_old_shape_with_unknown_key_keeps_it(self):
        self.write({"output": "DP-1", "es_output": "DSI-1", "future": {"x": 1}})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["future"], {"x": 1})

    def test_unrecognized_legacy_output_cannot_migrate_falls_back(self):
        self.write({"output": "HDMI-A-1", "es_output": "DP-1"})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["screens"]["command_center_screen"],
                         config.field_for(("screens", "command_center_screen"))["default"])
        self.assertIn("could not migrate", note)
        # es_output DID recognize -> still migrated for that key
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")

    def test_schema_version_present_skips_migration(self):
        # Screens values here are the shipped default so this test isolates
        # ONE thing: schema_version present -> the stray legacy "output" key
        # is never re-migrated.
        self.write({"schema_version": 1, "output": "HDMI-A-1",
                   "screens": {"es_screen": "addon_top",
                               "command_center_screen": "builtin_bottom"}})
        cfg, note = config.load(self.p)
        # the already-versioned screens.* values win; the derived legacy
        # keys reflect them, not the stray "output" string in the file
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
        self.assertEqual(cfg["output"], "DSI-1")
        self.assertNotIn("migrated", note)
        self.assertNotIn("092 reads", note)


class TestOppositeScreensRule(ConfigTestCase):
    def test_equal_screens_in_file_are_fixed_up_on_load(self):
        self.write({"schema_version": 1,
                   "screens": {"es_screen": "addon_top", "command_center_screen": "addon_top"}})
        cfg, note = config.load(self.p)
        self.assertNotEqual(cfg["screens"]["es_screen"], cfg["screens"]["command_center_screen"])
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
        self.assertIn("were equal", note)

    def test_set_value_picking_one_flips_the_other(self):
        cfg = config.defaults()
        self.assertTrue(config.set_value(cfg, ("screens", "es_screen"), "builtin_bottom"))
        self.assertEqual(cfg["screens"]["command_center_screen"], "addon_top")
        self.assertEqual(cfg["es_output"], "DSI-1")
        self.assertEqual(cfg["output"], "DP-1")

    def test_save_fixes_up_a_cfg_hand_mutated_to_equal_screens(self):
        cfg = config.defaults()
        cfg["screens"]["command_center_screen"] = "addon_top"    # now equal to es_screen
        out, notes = config.save(cfg, self.p)
        self.assertNotEqual(out["screens"]["es_screen"], out["screens"]["command_center_screen"])
        self.assertTrue(any("were equal" in n for n in notes))


class TestScreensFollowTheSharedContract(ConfigTestCase):
    """SW1 replaced the RV1-M4 lockout guard (which ignored every stored
    non-default combination) with ONE contract shared with 092:
    config.es_screen_from_raw(). A stored swap is honoured now - the
    Command Center can no longer be hidden by it, because rp5deck binds to
    whichever panel ES is really not on (screen_swap.py) - and
    command_center_screen is always derived as the opposite."""

    def test_a_stored_swap_is_honoured(self):
        self.write({"schema_version": 1,
                   "screens": {"es_screen": "builtin_bottom",
                               "command_center_screen": "addon_top"}})
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["screens"]["es_screen"], "builtin_bottom")
        self.assertEqual(cfg["screens"]["command_center_screen"], "addon_top")
        self.assertEqual(cfg["output"], "DP-1")
        self.assertEqual(cfg["es_output"], "DSI-1")
        self.assertNotIn("092 reads", note)

    def test_es_screen_alone_decides_even_if_cc_disagrees(self):
        # a hand edit that only flips command_center_screen: 092 ignores
        # that key, so ES stays on the add-on, and a Command Center there
        # would sit under ES - load() re-derives it from es_screen instead.
        self.write({"schema_version": 1,
                   "screens": {"es_screen": "addon_top",
                               "command_center_screen": "addon_top"}})
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
        self.assertEqual(cfg["screens"]["command_center_screen"], "builtin_bottom")

    def test_cc_only_file_is_unswapped(self):
        self.write({"schema_version": 1, "screens": {"command_center_screen": "addon_top"}})
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
        self.assertEqual(cfg["screens"]["command_center_screen"], "builtin_bottom")

    def test_invalid_es_screen_is_unswapped(self):
        for bad in ("BUILTIN_BOTTOM", 1, None, ["builtin_bottom"], "DSI-1"):
            with self.subTest(bad=bad):
                self.write({"schema_version": 1, "screens": {"es_screen": bad}})
                cfg, _ = config.load(self.p)
                self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
                self.assertEqual(cfg["output"], "DSI-1")

    def test_contract_function_directly(self):
        f = config.es_screen_from_raw
        self.assertEqual(f({"screens": {"es_screen": "builtin_bottom"}}), "builtin_bottom")
        for raw in (None, [], "x", {}, {"screens": []}, {"screens": {"es_screen": "addon_top"}},
                    {"output": "DP-1", "es_output": "DSI-1"}):
            self.assertEqual(f(raw), "addon_top", raw)

    def test_the_two_screens_are_never_equal_for_any_stored_value(self):
        vals = ["addon_top", "builtin_bottom", "x", None, 3]
        for es in vals:
            for cc in vals:
                with self.subTest(es=es, cc=cc):
                    self.write({"schema_version": 1,
                               "screens": {"es_screen": es, "command_center_screen": cc}})
                    cfg, _ = config.load(self.p)
                    self.assertNotEqual(cfg["output"], cfg["es_output"])
                    self.assertIn(cfg["output"], ("DP-1", "DSI-1"))


class TestSaveChanges(ConfigTestCase):
    """SW1: two processes (main panel + game-screen overlay) write this
    file. save_changes() writes only the keys it is given, on top of the
    file as it is now, so neither can put the other's change back."""

    def test_two_writers_keep_both_changes(self):
        config.save(config.defaults(), self.p)
        panel_cfg, _ = config.load(self.p)          # both load the same file
        # the overlay swaps the screens ...
        config.save_changes({("screens", "es_screen"): "builtin_bottom"}, self.p)
        # ... then the panel (holding a stale copy) changes something else
        config.set_value(panel_cfg, ("companion", "play_video"), False)
        config.save_changes({("companion", "play_video"): False}, self.p)
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["screens"]["es_screen"], "builtin_bottom")
        self.assertEqual(cfg["screens"]["command_center_screen"], "addon_top")
        self.assertIs(cfg["companion"]["play_video"], False)

    def test_whole_snapshot_save_would_have_lost_the_swap(self):
        # the control for the test above: the old way (save the stale
        # in-memory snapshot) undoes the other process's swap
        config.save(config.defaults(), self.p)
        panel_cfg, _ = config.load(self.p)
        config.save_changes({("screens", "es_screen"): "builtin_bottom"}, self.p)
        config.set_value(panel_cfg, ("companion", "play_video"), False)
        config.save(panel_cfg, self.p)
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")

    def test_invalid_change_is_refused_not_written(self):
        config.save(config.defaults(), self.p)
        out, notes = config.save_changes({("companion", "background_dim"): 7.0,
                                          ("companion", "idle_mode"): "blank"}, self.p)
        self.assertTrue(any("refused companion.background_dim" in n for n in notes))
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["companion"]["background_dim"], 0.15)
        self.assertEqual(cfg["companion"]["idle_mode"], "blank")

    def test_missing_file_is_created(self):
        config.save_changes({("screens", "es_screen"): "builtin_bottom"}, self.p)
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["es_output"], "DSI-1")


class TestUnknownKeysPreserved(ConfigTestCase):
    def test_top_level_unknown_key_round_trips(self):
        self.write({"schema_version": 1, "future_feature": {"enabled": True}})
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["future_feature"], {"enabled": True})
        out, _ = config.save(cfg, self.p)
        self.assertEqual(out["future_feature"], {"enabled": True})
        cfg2, _ = config.load(self.p)
        self.assertEqual(cfg2["future_feature"], {"enabled": True})

    def test_unknown_key_inside_a_known_group_is_preserved(self):
        self.write({"schema_version": 1, "companion": {"new_setting_v2": 7}})
        cfg, _ = config.load(self.p)
        self.assertEqual(cfg["companion"]["new_setting_v2"], 7)
        self.assertEqual(cfg["companion"]["play_video"], True)   # known sibling untouched
        out, _ = config.save(cfg, self.p)
        self.assertEqual(out["companion"]["new_setting_v2"], 7)


class TestAtomicSave(ConfigTestCase):
    def test_round_trip(self):
        cfg = config.defaults()
        config.set_value(cfg, ("companion", "brightness_pct"), 45)
        out, notes = config.save(cfg, self.p)
        self.assertEqual(notes, [])
        cfg2, note = config.load(self.p)
        self.assertEqual(cfg2["companion"]["brightness_pct"], 45)
        self.assertIn("loaded", note)

    def test_save_does_not_persist_legacy_keys(self):
        cfg = config.defaults()
        config.save(cfg, self.p)
        with open(self.p, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertNotIn("output", on_disk)
        self.assertNotIn("es_output", on_disk)

    def test_failure_mid_write_leaves_old_file_intact(self):
        good = config.defaults()
        config.set_value(good, ("companion", "brightness_pct"), 33)
        config.save(good, self.p)
        with open(self.p, encoding="utf-8") as f:
            before = f.read()

        broken = config.defaults()
        config.set_value(broken, ("companion", "brightness_pct"), 99)
        with mock.patch("config.os.replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                config.save(broken, self.p)

        with open(self.p, encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)                         # untouched
        cfg, note = config.load(self.p)
        self.assertEqual(cfg["companion"]["brightness_pct"], 33)  # still the good value
        leftover_tmp = [n for n in os.listdir(self.d) if n != "config.json"]
        self.assertEqual(leftover_tmp, [])                       # temp file cleaned up

    def test_write_is_actually_atomic_not_merely_untested(self):
        # Prove the guard can fail: a NON-atomic save (write straight to the
        # target) would corrupt the file on the same simulated failure.
        with open(self.p, "w", encoding="utf-8") as f:
            f.write('{"schema_version": 1}\n')
        with open(self.p, encoding="utf-8") as f:
            before = f.read()
        with self.assertRaises(OSError):
            with open(self.p, "w", encoding="utf-8") as f:
                f.write("{completely broken")
                raise OSError("simulated failure, mid-write, direct to the target")
        with open(self.p, encoding="utf-8") as f:
            after = f.read()
        self.assertNotEqual(before, after)      # <- this is what atomic save must NOT do


class TestGetSetValue(ConfigTestCase):
    def test_get_value_default_for_absent_key(self):
        cfg = {"schema_version": 1}
        self.assertEqual(config.get_value(cfg, ("audio", "volume_step_pct")), 5)

    def test_set_value_rejects_invalid_and_leaves_cfg_unchanged(self):
        cfg = config.defaults()
        ok = config.set_value(cfg, ("companion", "brightness_pct"), 1000)
        self.assertFalse(ok)
        self.assertEqual(cfg["companion"]["brightness_pct"], 80)

    def test_set_value_unknown_key_path_refused(self):
        cfg = config.defaults()
        self.assertFalse(config.set_value(cfg, ("companion", "no_such_key"), 1))


class TestTileCustomisationSchema(ConfigTestCase):
    """command_center.tile_order / hidden_tiles (screens.Home's "Edit
    tiles" mode): validated at the config layer independently of
    screens.py's own guard, so a bug there is not the only thing standing
    between the owner and locking themselves out of Settings."""

    def test_home_tile_keys_excludes_settings_from_hideable(self):
        self.assertIn("home.settings", config.HOME_TILE_KEYS)
        self.assertNotIn("home.settings", config.HIDEABLE_TILE_KEYS)
        self.assertEqual(len(config.HIDEABLE_TILE_KEYS), len(config.HOME_TILE_KEYS) - 1)

    def test_tile_order_default_is_the_full_set_in_order(self):
        cfg = config.defaults()
        self.assertEqual(config.get_value(cfg, ("command_center", "tile_order")),
                         list(config.HOME_TILE_KEYS))

    def test_tile_order_rejects_a_partial_list(self):
        cfg = config.defaults()
        partial = list(config.HOME_TILE_KEYS)[:-1]
        self.assertFalse(config.set_value(cfg, ("command_center", "tile_order"), partial))
        self.assertEqual(config.get_value(cfg, ("command_center", "tile_order")),
                         list(config.HOME_TILE_KEYS))

    def test_tile_order_rejects_a_duplicate(self):
        cfg = config.defaults()
        dup = list(config.HOME_TILE_KEYS)[:-1] + [config.HOME_TILE_KEYS[0]]
        self.assertFalse(config.set_value(cfg, ("command_center", "tile_order"), dup))

    def test_tile_order_accepts_a_full_permutation(self):
        cfg = config.defaults()
        reordered = list(reversed(config.HOME_TILE_KEYS))
        self.assertTrue(config.set_value(cfg, ("command_center", "tile_order"), reordered))
        self.assertEqual(config.get_value(cfg, ("command_center", "tile_order")), reordered)

    def test_a_saved_order_with_the_removed_youtube_tile_loads_without_it(self):
        """YT3: a config.json written before the old mpv-based "YouTube"
        tile was removed still names it in tile_order/hidden_tiles. Loading
        it must never crash and must never show "home.youtube" - the
        array_enum/tile_set validators refuse the whole stale list (it is
        no longer a full permutation of / subset of HOME_TILE_KEYS) and
        fall back to today's default, which does not contain it."""
        stale_order = ["home.settings", "home.mixer", "home.hud", "home.browser",
                       "home.youtube", "home.discord", "home.ytapp", "home.hotkeys",
                       "home.clean", "home.swap", "home.lights", "home.keyboard"]
        d = tempfile.mkdtemp(prefix="rp5deck-cfg-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"command_center": {"tile_order": stale_order,
                                          "hidden_tiles": ["home.youtube"]}}, f)
        cfg, note = config.load(path)
        order = config.get_value(cfg, ("command_center", "tile_order"))
        hidden = config.get_value(cfg, ("command_center", "hidden_tiles"))
        self.assertNotIn("home.youtube", order)
        self.assertNotIn("home.youtube", hidden)
        self.assertEqual(set(order), set(config.HOME_TILE_KEYS))
        self.assertIn("invalid command_center.tile_order", note)
        self.assertIn("invalid command_center.hidden_tiles", note)

    def test_hidden_tiles_default_is_empty(self):
        cfg = config.defaults()
        self.assertEqual(config.get_value(cfg, ("command_center", "hidden_tiles")), [])

    def test_hidden_tiles_accepts_a_subset(self):
        cfg = config.defaults()
        self.assertTrue(config.set_value(cfg, ("command_center", "hidden_tiles"),
                                         ["home.mixer", "home.hud"]))
        self.assertEqual(config.get_value(cfg, ("command_center", "hidden_tiles")),
                         ["home.mixer", "home.hud"])

    def test_hidden_tiles_refuses_home_settings_even_directly(self):
        cfg = config.defaults()
        ok = config.set_value(cfg, ("command_center", "hidden_tiles"), ["home.settings"])
        self.assertFalse(ok)
        self.assertEqual(config.get_value(cfg, ("command_center", "hidden_tiles")), [])

    def test_hidden_tiles_refuses_a_duplicate(self):
        cfg = config.defaults()
        ok = config.set_value(cfg, ("command_center", "hidden_tiles"),
                              ["home.mixer", "home.mixer"])
        self.assertFalse(ok)

    def test_hidden_tiles_refuses_an_unknown_key(self):
        cfg = config.defaults()
        ok = config.set_value(cfg, ("command_center", "hidden_tiles"), ["home.nonexistent"])
        self.assertFalse(ok)

    def test_tile_order_and_hidden_tiles_are_not_generically_wired(self):
        # settings_view.py has no renderer for "tile_set", and a generic
        # array_enum reorder page would duplicate the dedicated drag-and-
        # drop UI - see config.py's WIRED comment.
        self.assertNotIn(("command_center", "tile_order"), config.WIRED)
        self.assertNotIn(("command_center", "hidden_tiles"), config.WIRED)


class TestEnvPath(unittest.TestCase):
    def test_env_path(self):
        p = os.path.join(tempfile.gettempdir(), "rp5deck-test-config.json")
        self.assertEqual(config.config_path({"RP5DECK_CONFIG": p}), p)
        self.assertEqual(config.config_path({}), config.DEFAULT_PATH)


class TestMainPyCompatibility(ConfigTestCase):
    """main.py (UNCHANGED, not touched by this task) does exactly this:

        self.cfg, self.cfg_note = config.load()
        ...
        self.output = self.cfg["output"]
        self.es_output = self.cfg["es_output"]

    Prove that keeps working against a config.json written by the NEW
    schema-aware save(), and against a config.json still in the OLD shape."""

    def test_against_a_freshly_saved_schema_v1_file(self):
        # SW1: a saved swap round-trips (the lockout guard is gone; see
        # TestScreensFollowTheSharedContract).
        cfg = config.defaults()
        config.set_value(cfg, ("screens", "es_screen"), "builtin_bottom")
        config.save(cfg, self.p)

        loaded_cfg, cfg_note = config.load(self.p)   # exactly main.py's call
        output = loaded_cfg["output"]
        es_output = loaded_cfg["es_output"]
        self.assertEqual(output, "DP-1")
        self.assertEqual(es_output, "DSI-1")
        self.assertIn("loaded", cfg_note)

    def test_against_an_old_shape_file_never_touched_by_this_app_yet(self):
        self.write({"output": "DP-1", "es_output": "DSI-1"})
        loaded_cfg, cfg_note = config.load(self.p)
        # migrating this old shape lands on the swapped combo, but 092
        # cannot see legacy keys, so the shared contract keeps the default.
        self.assertEqual(loaded_cfg["output"], "DSI-1")
        self.assertEqual(loaded_cfg["es_output"], "DP-1")

    def test_against_a_missing_file(self):
        loaded_cfg, cfg_note = config.load(self.p)
        self.assertEqual(loaded_cfg["output"], "DSI-1")
        self.assertEqual(loaded_cfg["es_output"], "DP-1")


if __name__ == "__main__":
    unittest.main()
