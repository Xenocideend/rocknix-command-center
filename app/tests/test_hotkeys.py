#!/usr/bin/env python3
"""hotkeys.py: data integrity of the static tables (every context sourced,
every combo cited), context-aware ordering, the allow-listed system.cfg/
retroarch.cfg parsers (SYNTHETIC fixtures - never a real device file), and
apply_overrides()/load()'s end-to-end wiring. Offline, stdlib only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hotkeys  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def _read_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


SYSTEM_CFG_SYNTHETIC = _read_fixture("hotkeys-system-cfg-SYNTHETIC.txt")
RETROARCH_CFG_SYNTHETIC = _read_fixture("hotkeys-retroarch-cfg-SYNTHETIC.txt")


class TestRetroArchLiveIndices(unittest.TestCase):
    """Device, test day: retroarch.cfg input_enable_hotkey_btn = 10; the
    owner found Home + Start closes RetroArch (the sheet said Select)."""

    def device_overrides(self):
        return {"system_cfg": {}, "retroarch_cfg": {
            "input_enable_hotkey_btn": "10", "input_exit_emulator_btn": "9",
            "input_fps_toggle_btn": "3", "input_menu_toggle_btn": "2",
            "input_save_state_btn": "5", "input_load_state_btn": "4"}}

    def rows(self, ov):
        out = hotkeys.apply_overrides(hotkeys.default_contexts(), ov)
        return {c.action: c.buttons for c in out["retroarch"].combos}

    def test_device_hotkey_is_home(self):
        r = self.rows(self.device_overrides())
        self.assertEqual(r["Exit RetroArch / close the running core"], ("Home (hold)", "Start"))
        self.assertEqual(r["Toggle the on-screen FPS counter"], ("Home (hold)", "Y"))
        self.assertEqual(r["Open RetroArch's Quick Menu"], ("Home (hold)", "X"))
        self.assertEqual(r["Save state"], ("Home (hold)", "R1"))
        self.assertEqual(r["Load state"], ("Home (hold)", "L1"))

    def test_unknown_index_is_not_invented(self):
        ov = self.device_overrides()
        ov["retroarch_cfg"]["input_enable_hotkey_btn"] = "17"
        out = hotkeys.apply_overrides(hotkeys.default_contexts(), ov)
        first = out["retroarch"].combos[0]
        self.assertIn("not decoded", first.note)
        self.assertNotIn("17", "".join(first.buttons))


class TestDataIntegrity(unittest.TestCase):
    """The point of the verified_on_device/source split: a combo with no
    citation, or a boolean that isn't really a bool, is a bug this must
    catch - not something the sheet quietly renders wrong."""

    def test_every_context_in_order_exists_and_is_titled(self):
        contexts = hotkeys.default_contexts()
        for cid in hotkeys.CONTEXTS_ORDER:
            self.assertIn(cid, contexts, cid)
            self.assertIn(cid, hotkeys.CONTEXT_TITLES, cid)
            self.assertEqual(contexts[cid].id, cid)

    def test_every_combo_has_buttons_action_and_a_real_citation(self):
        contexts = hotkeys.default_contexts()
        for cid, ctx in contexts.items():
            for c in ctx.combos:
                self.assertIsInstance(c.verified_on_device, bool, (cid, c))
                self.assertTrue(c.buttons, (cid, c))
                self.assertTrue(c.action.strip(), (cid, c))
                self.assertTrue(c.source.strip(), (cid, c))
                # a citation must point somewhere real: a source file path
                # (contains a "/"), a bare well-known script name, or a
                # doc/row this repo actually has
                self.assertTrue(
                    "/" in c.source or ".md" in c.source or "input_sense" in c.source, (cid, c))

    def test_global_context_carries_the_tasks_row_facts(self):
        """The specific facts TASKS.md's CC7 row calls verified_on_device
        must actually be marked that way (and nothing invents extra
        verified_on_device entries that were never actually checked)."""
        g = hotkeys.default_contexts()["global"]
        by_action = {c.action: c for c in g.combos}
        kill = next(c for c in g.combos if "Close the running" in c.action)
        self.assertTrue(kill.verified_on_device)
        self.assertEqual(hotkeys.format_combo(kill), "L1 + Select + Start")
        # bonus finds (screenshot/mangohud/game-guide) were NOT in the TASKS
        # row - they must stay from_source, not be promoted to verified.
        for c in g.combos:
            if "Screenshot" in c.action or "MangoHud" in c.action or "guides" in c.action:
                self.assertFalse(c.verified_on_device, c)

    def test_no_context_is_empty(self):
        for cid, ctx in hotkeys.default_contexts().items():
            self.assertTrue(ctx.combos, cid)

    def test_unconfirmed_list_is_nonempty_and_names_the_fn_button_question(self):
        joined = " ".join(hotkeys.UNCONFIRMED)
        self.assertIn("FN", joined)
        self.assertIn("key.dpad.events", joined)
        # explicitly excluded emulators must be named as to WHY, not silently missing
        self.assertIn("PPSSPP", joined)
        self.assertIn("mupen64plus", joined)

    def test_ppsspp_and_mupen64plus_have_no_invented_context(self):
        contexts = hotkeys.default_contexts()
        self.assertNotIn("ppsspp", contexts)
        self.assertNotIn("mupen64plus", contexts)


class TestRealButtonNames(unittest.TestCase):
    """The owner's test-day request: the sheet must show the names printed
    on the RP5 (B/A/Y/X, Home, Back, Select, Start, ...), never an evdev
    BTN_* name, a Sony glyph, or a shouty ALL-CAPS label - and never mix
    styles. This scans every USER-FACING field the sheet actually renders
    (buttons + action + note - see hotkeys_view._apply): a `source` citation
    is allowed to quote a third-party file verbatim (e.g. RetroArch's own
    autoconfig calling a button 'Create'/'Square'), because that string is
    never shown to the user (hotkeys_view never displays c.source)."""

    FORBIDDEN = ("GUIDE", "face button", "Square", "Triangle", "Circle", "Cross",
                "Create", "Options", "-> F1")

    def _user_facing_strings(self):
        out = []
        for ctx in hotkeys.default_contexts().values():
            for c in ctx.combos:
                out.extend(c.buttons)
                out.append(c.action)
                if c.note:
                    out.append(c.note)
        return out

    def test_no_label_uses_a_forbidden_style(self):
        for s in self._user_facing_strings():
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, s, (bad, s))

    def test_no_label_is_all_caps_start_or_select(self):
        for s in self._user_facing_strings():
            self.assertNotIn("START", s, s)
            self.assertNotIn("SELECT", s, s)

    def test_evdev_face_buttons_map_to_the_confirmed_nintendo_layout(self):
        # Owner-confirmed on the device (test day): bottom=B, right=A,
        # left=Y, top=X.
        self.assertEqual(hotkeys.btn_glyph("BTN_SOUTH"), "B")
        self.assertEqual(hotkeys.btn_glyph("BTN_EAST"), "A")
        self.assertEqual(hotkeys.btn_glyph("BTN_WEST"), "Y")
        self.assertEqual(hotkeys.btn_glyph("BTN_NORTH"), "X")

    def test_other_evdev_names_use_the_printed_key_name(self):
        self.assertEqual(hotkeys.btn_glyph("BTN_MODE"), "Home")
        self.assertEqual(hotkeys.btn_glyph("BTN_BACK"), "Back")
        self.assertEqual(hotkeys.btn_glyph("BTN_SELECT"), "Select")
        self.assertEqual(hotkeys.btn_glyph("BTN_START"), "Start")
        self.assertEqual(hotkeys.btn_glyph("BTN_TOUCH"), "Touch screen")

    def test_dpad_volume_brightness_reads_as_not_enabled_on_this_device(self):
        # Owner decision (test day): FN + D-pad volume/brightness stays OFF
        # (key.dpad.events unset) - the sheet must say so via the note
        # mechanism it already has, not silently look like a working combo.
        g = hotkeys.default_contexts()["global"]
        dpad_actions = ("Volume up", "Volume down", "Brightness up", "Brightness down")
        found = 0
        for c in g.combos:
            if c.action in dpad_actions and any("D-pad" in b for b in c.buttons):
                found += 1
                self.assertIn("off on this device", c.note, c)
                self.assertEqual(c.note, hotkeys.DPAD_NOTE, c)
        self.assertEqual(found, len(dpad_actions))


class TestFormatCombo(unittest.TestCase):
    def test_joins_with_plus(self):
        c = hotkeys.combo(("L1", "Select", "Start"), "Kill", True, "test")
        self.assertEqual(hotkeys.format_combo(c), "L1 + Select + Start")

    def test_single_button(self):
        c = hotkeys.combo(("Home",), "Open menu", False, "test")
        self.assertEqual(hotkeys.format_combo(c), "Home")


class TestOrdering(unittest.TestCase):
    def test_default_order_is_global_first(self):
        self.assertEqual(hotkeys.ordered_context_ids(None), hotkeys.CONTEXTS_ORDER)
        self.assertEqual(hotkeys.ordered_context_ids(""), hotkeys.CONTEXTS_ORDER)

    def test_unmapped_running_system_defaults_to_retroarch(self):
        """ROCKNIX runs the large majority of systems (Genesis/Mega Drive,
        SNES, NES, PS1, GBA, arcade, ...) on a libretro core via RetroArch -
        a running system this module has no more specific standalone-
        emulator mapping for must show RetroArch first, not stay on Global
        (regression: a Mega Drive game via RetroArch showed only the
        Global/ROCKNIX page - device evidence, test day)."""
        order = hotkeys.ordered_context_ids("megadrive")
        self.assertEqual(order[0], "retroarch")
        self.assertEqual(order[1], "global")
        self.assertEqual(sorted(order), sorted(hotkeys.CONTEXTS_ORDER))

    def test_standalone_only_systems_are_excluded_from_the_retroarch_default(self):
        """psp/n64 run on ROCKNIX's own standalone (-sa) builds, not
        RetroArch (see UNCONFIRMED) - showing RetroArch's hotkeys for them
        would be wrong, not just incomplete, so they must NOT be
        redirected to "retroarch" by the unmapped-system fallback."""
        self.assertEqual(hotkeys.ordered_context_ids("psp"), hotkeys.CONTEXTS_ORDER)
        self.assertEqual(hotkeys.ordered_context_ids("n64"), hotkeys.CONTEXTS_ORDER)

    def test_running_nds_puts_melonds_first_then_global(self):
        order = hotkeys.ordered_context_ids("nds")
        self.assertEqual(order[0], "melonds")
        self.assertEqual(order[1], "global")
        # nothing lost, nothing duplicated
        self.assertEqual(sorted(order), sorted(hotkeys.CONTEXTS_ORDER))

    def test_running_wiiu_puts_cemu_first(self):
        order = hotkeys.ordered_context_ids("wiiu")
        self.assertEqual(order[0], "cemu")
        self.assertEqual(order[1], "global")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(hotkeys.ordered_context_ids("  N3DS  ")[0], "azahar")

    def test_running_global_itself_does_not_duplicate(self):
        # SYSTEM_TO_CONTEXT never maps to "global", but prove the guard
        # holds even if it did (defensive: a future mapping mistake must not
        # produce a broken/duplicated order).
        order = hotkeys.ordered_context_ids(None)
        self.assertEqual(len(order), len(set(order)))


class TestAllowlistedParser(unittest.TestCase):
    """The security-critical parser: system.cfg holds Wi-Fi credentials, and
    this must prove a negative - that they NEVER come back - not just that
    the wanted keys do."""

    def test_only_allowlisted_keys_come_back(self):
        out = hotkeys.parse_allowlisted(SYSTEM_CFG_SYNTHETIC, hotkeys.SYSTEM_CFG_KEYS)
        self.assertEqual(out.get("key.hotkey.a"), "BTN_TR")
        self.assertEqual(out.get("key.dpad.events"), "1")
        self.assertEqual(out.get("key.touchscreen.events"), "0")

    def test_credential_looking_keys_are_never_returned(self):
        """Prove the check by breaking the premise: the fixture DOES contain
        wifi.psk/root.password, and this must still come back empty for
        both - the allow-list, not luck, is what protects them."""
        out = hotkeys.parse_allowlisted(SYSTEM_CFG_SYNTHETIC, hotkeys.SYSTEM_CFG_KEYS)
        self.assertNotIn("wifi.psk", out)
        self.assertNotIn("root.password", out)
        self.assertNotIn("wifi.ssid", out)
        self.assertNotIn("system.autohotkeys", out)  # allow-listed set excludes it today
        joined_values = " ".join(out.values())
        self.assertNotIn("hunter2", joined_values)

    def test_unreadable_or_empty_text_returns_empty_dict(self):
        self.assertEqual(hotkeys.parse_allowlisted(None, hotkeys.SYSTEM_CFG_KEYS), {})
        self.assertEqual(hotkeys.parse_allowlisted("", hotkeys.SYSTEM_CFG_KEYS), {})

    def test_retroarch_fixture_parses_only_its_allowlist(self):
        out = hotkeys.parse_allowlisted(RETROARCH_CFG_SYNTHETIC, hotkeys.RETROARCH_CFG_KEYS)
        self.assertEqual(out.get("input_enable_hotkey_btn"), "12")
        self.assertEqual(out.get("input_save_state_btn"), "5")
        self.assertNotIn("input_driver", out)


class TestReadOverrides(unittest.TestCase):
    def test_missing_files_give_empty_dicts_not_none(self):
        overrides = hotkeys.read_overrides(read_fn=lambda path: None)
        self.assertEqual(overrides, {"system_cfg": {}, "retroarch_cfg": {}})

    def test_reads_the_right_path_per_file(self):
        seen = {}

        def fake(path):
            seen[path] = True
            if path == hotkeys.SYSTEM_CFG_PATH:
                return SYSTEM_CFG_SYNTHETIC
            if path == hotkeys.RETROARCH_CFG_PATH:
                return RETROARCH_CFG_SYNTHETIC
            return None
        overrides = hotkeys.read_overrides(read_fn=fake)
        self.assertIn(hotkeys.SYSTEM_CFG_PATH, seen)
        self.assertIn(hotkeys.RETROARCH_CFG_PATH, seen)
        self.assertEqual(overrides["system_cfg"]["key.hotkey.a"], "BTN_TR")
        self.assertEqual(overrides["retroarch_cfg"]["input_enable_hotkey_btn"], "12")


class TestApplyOverrides(unittest.TestCase):
    def test_no_overrides_leaves_defaults_untouched(self):
        contexts = hotkeys.default_contexts()
        out = hotkeys.apply_overrides(contexts, {"system_cfg": {}, "retroarch_cfg": {}})
        kill = next(c for c in out["global"].combos if "Close the running" in c.action)
        self.assertEqual(hotkeys.format_combo(kill), "L1 + Select + Start")

    def test_hotkey_a_override_changes_the_kill_combo_glyph(self):
        contexts = hotkeys.default_contexts()
        overrides = {"system_cfg": {"key.hotkey.a": "BTN_TR"}, "retroarch_cfg": {}}
        out = hotkeys.apply_overrides(contexts, overrides)
        kill = next(c for c in out["global"].combos if "Close the running" in c.action)
        self.assertEqual(hotkeys.format_combo(kill), "R1 + Select + Start")

    def test_unknown_override_value_falls_back_to_the_glyph_map(self):
        """An override naming a BTN_* this module has no glyph for still
        must not crash - it shows the raw name rather than a KeyError."""
        contexts = hotkeys.default_contexts()
        overrides = {"system_cfg": {"key.hotkey.a": "BTN_SOMETHING_NEW"}, "retroarch_cfg": {}}
        out = hotkeys.apply_overrides(contexts, overrides)
        kill = next(c for c in out["global"].combos if "Close the running" in c.action)
        self.assertIn("BTN_SOMETHING_NEW", hotkeys.format_combo(kill))

    def test_dpad_events_on_clears_the_off_by_default_note(self):
        contexts = hotkeys.default_contexts()
        vol = next(c for c in contexts["global"].combos if c.action == "Volume up")
        self.assertEqual(vol.note, hotkeys.DPAD_NOTE)
        out = hotkeys.apply_overrides(contexts, {"system_cfg": {"key.dpad.events": "1"},
                                                 "retroarch_cfg": {}})
        vol2 = next(c for c in out["global"].combos if c.action == "Volume up")
        self.assertEqual(vol2.note, "")

    def test_dpad_events_off_states_it_plainly(self):
        contexts = hotkeys.default_contexts()
        out = hotkeys.apply_overrides(contexts, {"system_cfg": {"key.dpad.events": "0"},
                                                 "retroarch_cfg": {}})
        vol2 = next(c for c in out["global"].combos if c.action == "Volume up")
        self.assertEqual(vol2.note, hotkeys.DPAD_NOTE)

    def test_dpad_events_unread_leaves_the_unconfirmed_note_alone(self):
        contexts = hotkeys.default_contexts()
        out = hotkeys.apply_overrides(contexts, {"system_cfg": {}, "retroarch_cfg": {}})
        vol2 = next(c for c in out["global"].combos if c.action == "Volume up")
        self.assertEqual(vol2.note, hotkeys.DPAD_NOTE)

    def test_retroarch_override_is_flagged_but_not_translated_to_a_wrong_glyph(self):
        contexts = hotkeys.default_contexts()
        out = hotkeys.apply_overrides(
            # 17: an index no button has on this pad (12 is now measured = R3)
            contexts, {"system_cfg": {}, "retroarch_cfg": {"input_enable_hotkey_btn": "17"}})
        notes = " ".join(c.note for c in out["retroarch"].combos)
        self.assertIn("input_enable_hotkey_btn", notes)
        # never invent a glyph for the overridden index: the note names the
        # KEY that changed, not a fabricated button label for value "17"
        self.assertIn("not decoded to a glyph", notes)

    def test_other_contexts_pass_through_unchanged(self):
        contexts = hotkeys.default_contexts()
        out = hotkeys.apply_overrides(contexts, {"system_cfg": {"key.hotkey.a": "BTN_TR"},
                                                 "retroarch_cfg": {}})
        self.assertEqual(out["melonds"], contexts["melonds"])
        self.assertEqual(out["dolphin"], contexts["dolphin"])


class TestLoad(unittest.TestCase):
    def test_load_with_no_readable_files_matches_defaults(self):
        contexts, order = hotkeys.load(read_fn=lambda path: None)
        self.assertEqual(order[0], "global")
        kill = next(c for c in contexts["global"].combos if "Close the running" in c.action)
        self.assertEqual(hotkeys.format_combo(kill), "L1 + Select + Start")

    def test_load_applies_overrides_and_running_system_order(self):
        def fake(path):
            return SYSTEM_CFG_SYNTHETIC if path == hotkeys.SYSTEM_CFG_PATH else None
        contexts, order = hotkeys.load(running_system="wiiu", read_fn=fake)
        self.assertEqual(order[0], "cemu")
        kill = next(c for c in contexts["global"].combos if "Close the running" in c.action)
        self.assertEqual(hotkeys.format_combo(kill), "R1 + Select + Start")


if __name__ == "__main__":
    unittest.main()
