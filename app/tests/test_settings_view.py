#!/usr/bin/env python3
"""settings_view.SettingsSheet: full schema coverage, paging across every
group, touch interaction for every control type (toggle / slider / cycle
enum / media-priority reorder / the opposite-screens flip), the restart
hint, the read-only About page, and that every visible control sits inside
1920x1080 with both dimensions >= 120 px. Offline, stdlib only - no display,
same pattern as tests/test_ui.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config          # noqa: E402
import device          # noqa: E402
import palettes        # noqa: E402
import settings_view as sv  # noqa: E402
import ui              # noqa: E402

SCREEN_W, SCREEN_H = 1920, 1080


def make_sheet(device_name="Test Device", app_version="1.2.3"):
    cfg = config.defaults()
    changes = []
    closes = []
    sheet = sv.SettingsSheet(cfg, lambda k, v: changes.append((k, v)), lambda: closes.append(1),
                             device_name=device_name, app_version=app_version)
    root = ui.Root(SCREEN_W, SCREEN_H)
    root.add(sheet)
    sheet.layout((0, 0, SCREEN_W, SCREEN_H))
    router = ui.TouchRouter(root)
    return sheet, cfg, changes, closes, root, router


def goto(sheet, key_path):
    """Select whichever group/page currently shows key_path's row, and
    re-layout so its rect is valid."""
    group = key_path[0]
    pages = sheet.pages[group]
    for pi, page in enumerate(pages):
        if sheet.rows[key_path] in page:
            sheet.group, sheet.page_index = group, pi
            sheet._apply_visibility()
            sheet.layout((0, 0, SCREEN_W, SCREEN_H))
            return
    raise AssertionError("%r not found on any page" % (key_path,))


def tap(router, widget):
    x, y, w, h = widget.rect
    pid = ("f", id(widget) & 0xFFFF)
    router.down(pid, x + w / 2, y + h / 2)
    router.up(pid, x + w / 2, y + h / 2)


class TestCoverage(unittest.TestCase):
    """The point of generating the UI from SCHEMA: a WIRED key with no
    control is a bug, and so is a control for a key that ISN'T WIRED (RV4-M2:
    four settings looked like working controls and did nothing at all).
    This must be able to fail, or it is not testing anything."""

    def test_every_wired_key_has_a_control_and_no_other_key_does(self):
        sheet, *_ = make_sheet()
        self.assertEqual(set(sheet.controls.keys()), config.WIRED)
        self.assertEqual(set(sheet.rows.keys()), config.WIRED)

    def test_unwired_schema_keys_have_no_control(self):
        # RV4-M2's four dead settings, named directly: a schema key can
        # exist (so old config.json files round-trip it) without ever
        # being shown as a live control.
        sheet, *_ = make_sheet()
        for path in [("companion", "brightness_pct"),
                     ("companion", "background_blur"),
                     ("manuals", "viewer"),
                     ("audio", "volume_step_pct"),
                     ("screens", "command_center_screen")]:
            self.assertNotIn(path, sheet.controls, path)
            self.assertNotIn(path, sheet.rows, path)

    def test_coverage_check_actually_catches_a_missing_key(self):
        """Prove the assertion above is a real check: with a key missing
        from `controls` (as a future SCHEMA addition might do if its author
        forgot to wire up a row), the same comparison must fail - shown
        here directly rather than trusted on faith."""
        sheet, *_ = make_sheet()
        want = set(config.WIRED)
        stripped = dict(sheet.controls)
        del stripped[("audio", "show_volume_overlay")]
        self.assertNotEqual(set(stripped.keys()), want)
        with self.assertRaises(AssertionError):
            self.assertEqual(set(stripped.keys()), want)

    def test_coverage_check_actually_catches_an_extra_unwired_control(self):
        """The other direction: a control built for a key that ISN'T in
        WIRED (the exact shape of the RV4-M2 bug) must also fail the
        comparison, not just a missing one."""
        sheet, *_ = make_sheet()
        want = set(config.WIRED)
        extra = dict(sheet.controls)
        extra[("companion", "brightness_pct")] = object()
        self.assertNotEqual(set(extra.keys()), want)
        with self.assertRaises(AssertionError):
            self.assertEqual(set(extra.keys()), want)

    def test_reorder_field_control_is_the_first_subpage_not_the_last(self):
        # Regression: building all sub-pages of media_priority once
        # overwrote self.rows/self.controls on every iteration, so the
        # entry ended up pointing at the LAST sub-page (offset != 0) - a
        # lookup by key_path found a widget that was invisible on the
        # first page a user actually lands on.
        sheet, *_ = make_sheet()
        key = ("companion", "media_priority")
        self.assertEqual(sheet.rows[key].offset, 0)
        self.assertEqual(sheet.controls[key].offset, 0)


class TestGeometry(unittest.TestCase):
    def test_every_control_on_every_page_fits_and_is_big_enough(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        checked = 0
        for g in sv.GROUP_ORDER:
            pages = sheet.pages.get(g) or []
            for pi in range(len(pages)):
                sheet.group, sheet.page_index = g, pi
                sheet._apply_visibility()
                sheet.layout((0, 0, SCREEN_W, SCREEN_H))
                for w in root.walk():
                    if w.interactive and w.shown():
                        checked += 1
                        x, y, ww, hh = w.rect
                        self.assertGreaterEqual(x, 0, w.name)
                        self.assertGreaterEqual(y, 0, w.name)
                        self.assertLessEqual(x + ww, SCREEN_W, w.name)
                        self.assertLessEqual(y + hh, SCREEN_H, w.name)
                        self.assertGreaterEqual(min(ww, hh), 120, w.name)
        self.assertGreater(checked, 30)     # every field plus tabs/footer/back, more than once

    def test_body_text_sizes_are_36_to_44(self):
        sheet, *_ = make_sheet()
        for row in sheet.rows.values():
            if isinstance(row, sv.FieldRow):
                self.assertGreaterEqual(row.label.size, 36)
                self.assertLessEqual(row.label.size, 44)


class TestPaging(unittest.TestCase):
    def test_group_tabs_reach_every_group(self):
        sheet, *_ = make_sheet()
        for g in sv.GROUP_ORDER:
            sheet._select_group(g)
            self.assertEqual(sheet.group, g)
            self.assertEqual(sheet.page_index, 0)
            self.assertTrue(sheet._current_page_widgets(), g)

    def test_next_prev_walks_every_page_of_a_multi_page_group(self):
        sheet, *_ = make_sheet()
        sheet._select_group("companion")
        total = len(sheet.pages["companion"])
        self.assertGreater(total, 1)
        seen = set()
        for _ in range(total):
            seen.add(sheet.page_index)
            sheet._change_page(1)
        self.assertEqual(seen, set(range(total)))
        self.assertEqual(sheet.page_index, total - 1)   # clamped, did not run off the end
        sheet._change_page(1)
        self.assertEqual(sheet.page_index, total - 1)
        for _ in range(total):
            sheet._change_page(-1)
        self.assertEqual(sheet.page_index, 0)

    def test_footer_buttons_disabled_at_the_ends(self):
        sheet, *_ = make_sheet()
        sheet._select_group("manuals")           # a single-page group
        self.assertFalse(sheet.prev_btn.enabled)
        self.assertFalse(sheet.next_btn.enabled)
        sheet._select_group("companion")
        self.assertFalse(sheet.prev_btn.enabled)
        self.assertTrue(sheet.next_btn.enabled)


class TestBackButton(unittest.TestCase):
    def test_back_button_calls_on_close_and_is_big_enough(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        x, y, w, h = sheet.back.rect
        self.assertGreaterEqual(min(w, h), 120)
        tap(router, sheet.back)
        self.assertEqual(closes, [1])


class TestToggleControl(unittest.TestCase):
    def test_tap_toggles_value_and_calls_on_change(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "play_video")
        goto(sheet, key)
        before = cfg["companion"]["play_video"]
        tap(router, sheet.rows[key].control)
        self.assertEqual(cfg["companion"]["play_video"], not before)
        self.assertEqual(changes[-1], (key, not before))

    def test_restart_hint_shown_only_for_restart_flagged_fields(self):
        sheet, *_ = make_sheet()
        for f in config.SCHEMA:
            if f["type"] == "array_enum" or f["key_path"] not in config.WIRED:
                continue
            r = sheet.rows[f["key_path"]]
            if f["restart"]:
                self.assertIn("restart", r.hint.text.lower())
            else:
                self.assertEqual(r.hint.text, "")


class TestSliderControl(unittest.TestCase):
    def test_drag_release_commits_and_notifies(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "idle_slideshow_interval_s")
        goto(sheet, key)
        slider = sheet.rows[key].control
        tx = slider.x_for(0.5)
        cy = slider.rect[1] + slider.rect[3] / 2
        pid = ("f", 1)
        router.down(pid, tx, cy)
        router.up(pid, tx, cy)
        self.assertEqual(cfg["companion"]["idle_slideshow_interval_s"], config.get_value(cfg, key))
        self.assertGreaterEqual(cfg["companion"]["idle_slideshow_interval_s"], 5)
        self.assertLessEqual(cfg["companion"]["idle_slideshow_interval_s"], 60)
        self.assertEqual(changes[-1][0], key)
        self.assertEqual(changes[-1][1], cfg["companion"]["idle_slideshow_interval_s"])

    def test_auto_close_timeout_zero_reads_as_never_not_0(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("command_center", "auto_close_timeout_s")
        goto(sheet, key)
        row = sheet.rows[key]
        self.assertEqual(cfg["command_center"]["auto_close_timeout_s"], 0)  # the shipped default
        self.assertEqual(row.readout.text, "Never")

    def test_readout_tracks_drag_before_release(self):
        # Note: touch-down alone never moves the knob any more (RV1-M1, a
        # concurrent ui.py fix owned by FX-A: a swipe that merely starts on
        # a slider must not change the value) - a real move past SLOP is
        # needed to engage the drag, matching Slider's own documented
        # press/move/release contract.
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "video_start_delay_ms")
        goto(sheet, key)
        row = sheet.rows[key]
        slider = row.control
        pid = ("f", 2)
        x0 = slider.x_for(slider.value)
        cy = slider.rect[1] + slider.rect[3] / 2
        tx = slider.x_for(0.9)
        router.down(pid, x0, cy)
        router.move(pid, tx, cy)
        self.assertNotEqual(changes and changes[-1][0], key)     # no commit yet mid-drag
        self.assertNotEqual(row.readout.text, "500")              # label moved off the default
        router.up(pid, tx, cy)
        self.assertEqual(changes[-1][0], key)


class TestEnumControl(unittest.TestCase):
    def test_tap_cycles_to_the_next_value_and_wraps(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "image_fit")
        goto(sheet, key)
        control = sheet.rows[key].control
        values = config.field_for(key)["values"]
        start = values.index(cfg["companion"]["image_fit"])
        expected_sequence = [values[(start + i) % len(values)] for i in range(1, len(values) + 1)]
        for expected in expected_sequence:
            tap(router, control)
            self.assertEqual(cfg["companion"]["image_fit"], expected)
            self.assertEqual(changes[-1], (key, expected))


class TestMediaPriorityReorder(unittest.TestCase):
    def test_down_swaps_with_the_next_rank(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "media_priority")
        goto(sheet, key)
        page = sheet.rows[key]
        before = list(cfg["companion"]["media_priority"])
        _, up0, down0 = page.rows[0]
        tap(router, down0)
        after = cfg["companion"]["media_priority"]
        self.assertEqual(after[0], before[1])
        self.assertEqual(after[1], before[0])
        self.assertEqual(changes[-1], (key, after))

    def test_top_rank_cannot_move_up(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "media_priority")
        goto(sheet, key)
        page = sheet.rows[key]
        lbl, up0, down0 = page.rows[0]
        self.assertFalse(up0.enabled)
        before = list(cfg["companion"]["media_priority"])
        up0._set_pressed(True)
        up0.clicked() if up0.enabled else None   # a disabled control must not act even if forced
        self.assertEqual(cfg["companion"]["media_priority"], before)

    def test_move_crosses_into_the_next_subpage(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "media_priority")
        goto(sheet, key)
        first_page = sheet.reorder_pages[key][0]
        self.assertIs(sheet.rows[key], first_page)
        self.assertEqual(first_page.count, sv.REORDER_PAGE_SIZE)
        before = list(cfg["companion"]["media_priority"])
        last_local = first_page.count - 1                # last row shown on THIS sub-page
        _, up, down = first_page.rows[last_local]
        self.assertTrue(down.enabled)                     # its partner lives on the next sub-page
        tap(router, down)
        after = cfg["companion"]["media_priority"]
        gi = first_page.offset + last_local
        self.assertEqual(after[gi], before[gi + 1])
        self.assertEqual(after[gi + 1], before[gi])

    def test_bottom_rank_cannot_move_down(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "media_priority")
        goto(sheet, key)
        last_page = sheet.reorder_pages[key][-1]
        n = len(cfg["companion"]["media_priority"])
        last_local = n - 1 - last_page.offset
        lbl, up, down = last_page.rows[last_local]
        self.assertFalse(down.enabled)

    def test_every_subpage_has_a_labelled_header_with_its_range(self):
        """CM bug 2 (test day): the owner paged through Settings ->
        Companion (Next x7, some Prev) and reported cartridge/boxback
        "not there". A PC render of every Companion page
        (screenshots/cm-pc-render-media-page-*.png) showed both DO appear,
        correctly, with working Up/Down, on the second reorder sub-page -
        the real gap was that a reorder sub-page showed no label at all
        (unlike a normal FieldRow, which always shows field["label"]), so
        nothing on screen said "this is the media order setting", let
        alone that it continues onto another page. Every sub-page must now
        show the field's label and which ranks it covers."""
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "media_priority")
        pages = sheet.reorder_pages[key]
        self.assertGreaterEqual(len(pages), 2)         # 8 values / 4 per page
        n = len(cfg["companion"]["media_priority"])
        for page in pages:
            self.assertIn("Preferred media order", page.header.text)
            lo = page.offset + 1
            hi = min(page.offset + page.count, n)
            self.assertIn("%d-%d of %d" % (lo, hi, n), page.header.text)

    def test_cartridge_and_boxback_are_on_the_second_subpage_with_working_buttons(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        key = ("companion", "media_priority")
        pages = sheet.reorder_pages[key]
        last = pages[-1]
        labels = [lbl.text for lbl, up, down in last.rows if lbl.visible]
        self.assertTrue(any("Cartridge" in t for t in labels), labels)
        self.assertTrue(any("Boxback" in t for t in labels), labels)


class TestSwapScreensToggle(unittest.TestCase):
    """SW1: the Screens group is back, as ONE control - "Swap screens", a
    Toggle over screens.es_screen - and nothing for the derived
    command_center_screen (RV1-M4's "dangerous combo" cannot be built)."""

    KEY = ("screens", "es_screen")
    CC = ("screens", "command_center_screen")

    def test_screens_tab_has_exactly_the_swap_toggle(self):
        sheet, *_ = make_sheet()
        self.assertIn("screens", sheet.tabs)
        self.assertIn("screens", sv.GROUP_ORDER)
        self.assertIsInstance(sheet.controls[self.KEY], ui.Toggle)
        self.assertEqual(sheet.rows[self.KEY].label.text, "Swap screens")
        self.assertNotIn(self.CC, sheet.rows)
        self.assertNotIn(self.CC, sheet.controls)
        rows_on_screens_pages = [w for page in sheet.pages["screens"] for w in page]
        self.assertEqual(rows_on_screens_pages, [sheet.rows[self.KEY]])

    def test_default_is_off_and_live(self):
        sheet, cfg, *_ = make_sheet()
        self.assertFalse(sheet.controls[self.KEY].state)
        self.assertEqual(sheet.rows[self.KEY].hint.text, "")      # no "after restart"

    def test_tap_swaps_both_screens_and_reports_both(self):
        sheet, cfg, changes, closes, root, router = make_sheet()
        goto(sheet, self.KEY)
        tap(router, sheet.controls[self.KEY])
        self.assertTrue(sheet.controls[self.KEY].state)
        self.assertEqual(cfg["screens"]["es_screen"], "builtin_bottom")
        self.assertEqual(cfg["screens"]["command_center_screen"], "addon_top")
        self.assertEqual(cfg["output"], "DP-1")
        self.assertEqual(cfg["es_output"], "DSI-1")
        self.assertIn((self.KEY, "builtin_bottom"), changes)
        self.assertIn((self.CC, "addon_top"), changes)
        tap(router, sheet.controls[self.KEY])                       # and back
        self.assertEqual(cfg["screens"]["es_screen"], "addon_top")
        self.assertEqual(cfg["output"], "DSI-1")
        self.assertFalse(sheet.controls[self.KEY].state)

    def test_refresh_shows_a_swap_made_elsewhere(self):
        # e.g. the Command Center's "Swap screens" tile changed cfg
        sheet, cfg, *_ = make_sheet()
        config.set_value(cfg, self.KEY, "builtin_bottom")
        goto(sheet, self.KEY)          # _apply_visibility refreshes visible rows
        self.assertTrue(sheet.controls[self.KEY].state)

    def test_toggle_is_touch_sized_and_on_screen(self):
        sheet, *_ = make_sheet()
        goto(sheet, self.KEY)
        x, y, w, h = sheet.controls[self.KEY].rect
        self.assertGreaterEqual(w, 120)
        self.assertGreaterEqual(h, 120)
        self.assertLessEqual(x + w, SCREEN_W)
        self.assertLessEqual(y + h, SCREEN_H)


class TestPolish(unittest.TestCase):
    """I1 review polish: the restart hint and the Command Center tab label
    were both ellipsized off-screen at their old widths/text, and an
    auto-close timeout of 0 read as a bare "0" instead of "Never" (the
    latter has its own dedicated test in TestSliderControl)."""

    def test_restart_hint_text_is_short_enough_to_fit(self):
        sheet, *_ = make_sheet()
        restart_field = next(f for f in config.SCHEMA
                             if f["restart"] and f["key_path"] in config.WIRED)
        row = sheet.rows[restart_field["key_path"]]
        self.assertEqual(row.hint.text, "after restart")
        self.assertLess(len(row.hint.text), len("applies after restart"))

    def test_command_center_tab_label_is_shortened(self):
        sheet, *_ = make_sheet()
        self.assertEqual(sv.TAB_TITLES["command_center"], "Command")
        self.assertLess(len(sv.TAB_TITLES["command_center"]), len("Command Center"))
        # the full name is unaffected anywhere else it's used
        self.assertEqual(sv.GROUP_TITLES["command_center"], "Command Center")


class TestHardwareButtonDefault(unittest.TestCase):
    """Device-verified 23 Sep: the RP5 has no rear paddles at all - the
    default must be btn_back_f1, and the paddle choices must read as
    unsafe-on-this-device rather than as ordinary working options."""

    def test_default_control_shows_back_f1(self):
        sheet, cfg, *_ = make_sheet()
        key = ("command_center", "hardware_button")
        self.assertEqual(cfg["command_center"]["hardware_button"], "btn_back_f1")
        self.assertEqual(sheet.rows[key].control.value, "btn_back_f1")

    def test_paddle_values_are_labeled_no_rear_paddles(self):
        sheet, *_ = make_sheet()
        key = ("command_center", "hardware_button")
        goto(sheet, key)
        control = sheet.rows[key].control
        for _ in range(len(control.values)):
            if control.value in config.PADDLE_HARDWARE_BUTTONS:
                self.assertIn("no rear paddles", control.text)
            else:
                self.assertNotIn("no rear paddles", control.text)
            control.clicked()


class TestAboutPage(unittest.TestCase):
    def test_device_name_and_version_shown_read_only(self):
        sheet, *_ = make_sheet(device_name="Retroid Pocket 5", app_version="9.9.9")
        texts = [r.text for r in sheet.about_rows]
        self.assertIn("Retroid Pocket 5", texts)
        self.assertIn("9.9.9", texts)
        for r in sheet.about_rows:
            self.assertFalse(r.interactive)

    def test_default_device_name_comes_from_the_device_module(self):
        sheet, *_ = make_sheet(device_name=None)
        self.assertEqual(sheet.about_rows[1].text, device.device_name())

    def test_about_has_no_schema_keys(self):
        # About is explicitly NOT settings - nothing in it should appear in
        # the coverage map.
        sheet, *_ = make_sheet()
        about_names = {r.name for r in sheet.about_rows if r.name}
        self.assertFalse(about_names & set(sheet.controls))


class TestAppearanceGroup(unittest.TestCase):
    """The "Appearance" tab is built the same data-driven way every other
    group is (config.GROUPS + config.WIRED - appearance.theme_preset is the
    one WIRED field, so it gets an ordinary FieldRow/CycleButton like any
    enum), PLUS one hand-built row (AppearanceExtraRow) for its two buttons,
    which are not schema fields at all - see settings_view.py's own
    docstring on SettingsSheet.__init__'s on_edit_colours/on_reset_appearance
    params for why."""

    def make(self):
        cfg = config.defaults()
        changes = []
        edits = []
        resets = []
        sheet = sv.SettingsSheet(cfg, lambda k, v: changes.append((k, v)), lambda: None,
                                 on_edit_colours=lambda: edits.append(1),
                                 on_reset_appearance=lambda: resets.append(1))
        root = ui.Root(SCREEN_W, SCREEN_H)
        root.add(sheet)
        sheet.layout((0, 0, SCREEN_W, SCREEN_H))
        router = ui.TouchRouter(root)
        return sheet, cfg, changes, edits, resets, router

    def test_appearance_tab_exists(self):
        sheet, *_ = self.make()
        self.assertIn("appearance", sv.GROUP_ORDER)
        self.assertIn("appearance", sheet.tabs)

    def test_theme_preset_has_an_ordinary_cycle_button(self):
        sheet, cfg, *_ = self.make()
        self.assertIn(("appearance", "theme_preset"), sheet.controls)
        ctrl = sheet.controls[("appearance", "theme_preset")]
        self.assertIsInstance(ctrl, sv.CycleButton)
        self.assertEqual(ctrl.value, "default")

    def test_custom_colour_fields_have_no_generic_control(self):
        # Same reasoning as lights.left/right (config.py's WIRED comment):
        # a hex_color has no FieldRow renderer and none should be built.
        sheet, *_ = self.make()
        for leaf in ("custom_accent", "custom_bg", "custom_text"):
            self.assertNotIn(("appearance", leaf), sheet.controls)

    def test_cycle_button_shows_the_same_descriptive_label_as_the_cli(self):
        """Device field test 25 Sep: the button showed "Dreamcast" while
        `palettes.py --list` showed "White/orange swirl" - the generic
        _display() was splitting the raw preset NAME on underscores
        instead of using palettes.PRESET_LABELS at all. Fixed via
        _ENUM_DISPLAY_OVERRIDES -> palettes.display_label; every classic-
        console preset's button text must now match --list's own label,
        plus the console name in brackets."""
        sheet, cfg, *_ = self.make()
        ctrl = sheet.controls[("appearance", "theme_preset")]
        for name in ("gameboy", "nes", "genesis", "playstation", "n64", "dreamcast"):
            ctrl.set_value(name)
            self.assertIn(palettes.PRESET_LABELS[name], ctrl.text)
            self.assertIn(palettes.PRESET_CONSOLE_HINT[name], ctrl.text)
        # The RP5 editions/Default/High contrast need no bracketed hint -
        # PRESET_LABELS already IS the console/device name for those.
        for name in ("default", "high_contrast", "rp5_black", "rp5_white",
                    "rp5_16bit", "rp5_gc", "rp5_yellow", "rp5_turquoise"):
            ctrl.set_value(name)
            self.assertEqual(ctrl.text, palettes.PRESET_LABELS[name])

    def test_extra_row_is_on_the_appearance_page_and_is_touch_sized(self):
        sheet, *_ = self.make()
        goto(sheet, ("appearance", "theme_preset"))
        extra = sheet.appearance_extra
        self.assertIn(extra, sheet.pages["appearance"][sheet.page_index])
        for btn in (extra.edit_btn, extra.reset_btn):
            self.assertGreaterEqual(btn.rect[2], 120)
            self.assertGreaterEqual(btn.rect[3], 120)

    def test_edit_button_calls_on_edit_colours(self):
        sheet, cfg, changes, edits, resets, router = self.make()
        goto(sheet, ("appearance", "theme_preset"))
        tap(router, sheet.appearance_extra.edit_btn)
        self.assertEqual(edits, [1])
        self.assertEqual(resets, [])

    def test_reset_button_calls_on_reset_appearance(self):
        sheet, cfg, changes, edits, resets, router = self.make()
        goto(sheet, ("appearance", "theme_preset"))
        tap(router, sheet.appearance_extra.reset_btn)
        self.assertEqual(resets, [1])
        self.assertEqual(edits, [])

    def test_missing_callbacks_default_to_harmless_no_ops(self):
        sheet, cfg, changes, closes, root, router = make_sheet()   # no callbacks passed at all
        goto(sheet, ("appearance", "theme_preset"))
        tap(router, sheet.appearance_extra.edit_btn)   # must not raise
        tap(router, sheet.appearance_extra.reset_btn)  # must not raise


if __name__ == "__main__":
    unittest.main()
