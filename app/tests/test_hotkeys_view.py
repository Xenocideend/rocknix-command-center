#!/usr/bin/env python3
"""hotkeys_view.HotkeysSheet: paging across every context (book-style, one
flat page sequence), the header's per-context page indicator, that Back/
Prev/Next stay >=120 px tall (DESIGN.md's touch-target rule), and that
set_data() (main.py's context-aware reload) rebuilds the page list and
resets to page 0. Offline, stdlib only - same pattern as
tests/test_settings_view.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hotkeys       # noqa: E402
import hotkeys_view  # noqa: E402
import ui            # noqa: E402

SCREEN_W, SCREEN_H = 1920, 1080


class Handlers:
    def __init__(self):
        self.closed = 0

    def close_sheet(self):
        self.closed += 1


def make_sheet():
    h = Handlers()
    sheet = hotkeys_view.HotkeysSheet(h)
    root = ui.Root(SCREEN_W, SCREEN_H)
    root.add(sheet)
    sheet.layout((0, 0, SCREEN_W, SCREEN_H))
    return sheet, h, root


class TestInitialLoad(unittest.TestCase):
    def test_loads_real_hotkeys_data_by_default(self):
        sheet, h, root = make_sheet()
        self.assertTrue(sheet.pages)
        ctx, combos, i, n = sheet.pages[0]
        self.assertEqual(ctx.id, "global")
        self.assertEqual(len(sheet.rows), len(combos))

    def test_back_button_calls_close_sheet(self):
        sheet, h, root = make_sheet()
        sheet.back.clicked()
        self.assertEqual(h.closed, 1)


class TestPaging(unittest.TestCase):
    def test_pages_cover_every_combo_in_every_context_exactly_once(self):
        sheet, h, root = make_sheet()
        seen = []
        for ctx, combos, i, n in sheet.pages:
            seen.extend((ctx.id, c) for c in combos)
        contexts = hotkeys.default_contexts()
        expected = []
        for cid in hotkeys.CONTEXTS_ORDER:
            expected.extend((cid, c) for c in contexts[cid].combos)
        self.assertEqual(seen, expected)

    def test_a_context_longer_than_one_page_shows_its_own_counter(self):
        sheet, h, root = make_sheet()
        # dsperate has 13 combos, MAX_ROWS=5 -> 3 pages for that context alone
        idx = next(i for i, (ctx, combos, pi, n) in enumerate(sheet.pages) if ctx.id == "dsperate")
        sheet.page = idx
        sheet._apply()
        self.assertIn("dsperate", sheet.title.text.lower() + sheet.title.text)
        self.assertIn("(1/", sheet.title.text)

    def test_turn_advances_and_clamps_at_both_ends(self):
        sheet, h, root = make_sheet()
        total = len(sheet.pages)
        self.assertGreater(total, 1)
        sheet.turn(-1)
        self.assertEqual(sheet.page, 0)          # clamped, not negative
        sheet.turn(1)
        self.assertEqual(sheet.page, 1)
        for _ in range(total + 5):
            sheet.turn(1)
        self.assertEqual(sheet.page, total - 1)  # clamped at the end

    def test_prev_next_enabled_state_matches_position(self):
        sheet, h, root = make_sheet()
        self.assertFalse(sheet.prev.enabled)
        self.assertTrue(sheet.next.enabled)
        sheet.turn(1)
        self.assertTrue(sheet.prev.enabled)

    def test_single_page_context_hides_the_within_context_counter(self):
        sheet, h, root = make_sheet()
        idx = next(i for i, (ctx, combos, pi, n) in enumerate(sheet.pages) if ctx.id == "azahar")
        sheet.page = idx
        sheet._apply()
        self.assertEqual(sheet.title.text, hotkeys.CONTEXT_TITLES["azahar"])


class TestRowContent(unittest.TestCase):
    def test_row_text_matches_format_combo_and_carries_the_source_tag(self):
        sheet, h, root = make_sheet()
        ctx, combos, i, n = sheet.pages[0]
        combo_lbl, action_lbl = sheet.rows[0]
        self.assertEqual(combo_lbl.text, hotkeys.format_combo(combos[0]))
        tag = "device" if combos[0].verified_on_device else "source"
        self.assertIn(tag, action_lbl.text)

    def test_a_note_is_appended_to_the_action_text(self):
        sheet, h, root = make_sheet()
        idx = next(i for i, (ctx, combos, pi, n) in enumerate(sheet.pages)
                  if ctx.id == "global" and any(c.note for c in combos))
        sheet.page = idx
        sheet._apply()
        ctx, combos, i, n = sheet.pages[idx]
        noted = next(j for j, c in enumerate(combos) if c.note)
        self.assertIn(combos[noted].note, sheet.rows[noted][1].text)


class TestSetData(unittest.TestCase):
    def test_set_data_reorders_pages_and_resets_to_page_zero(self):
        sheet, h, root = make_sheet()
        sheet.turn(1)
        self.assertNotEqual(sheet.page, 0)
        contexts, order = hotkeys.load(running_system="wiiu")
        sheet.set_data(contexts, order)
        self.assertEqual(sheet.page, 0)
        ctx, combos, i, n = sheet.pages[0]
        self.assertEqual(ctx.id, "cemu")

    def test_set_data_with_a_running_retroarch_game_shows_retroarch_first(self):
        """Regression: a Genesis/Mega Drive game running under RetroArch
        (running_system='megadrive', not in hotkeys.SYSTEM_TO_CONTEXT) opened
        the sheet on Global only, with no RetroArch page visible without
        paging forward - device evidence, test day. hotkeys.load() now
        defaults an unmapped-but-running system to the RetroArch page (see
        hotkeys.ordered_context_ids), and this proves the view actually
        picks that up end-to-end via set_data(), the same path main.py's
        open_hotkeys() uses."""
        sheet, h, root = make_sheet()
        contexts, order = hotkeys.load(running_system="megadrive")
        sheet.set_data(contexts, order)
        self.assertEqual(sheet.page, 0)
        ctx, combos, i, n = sheet.pages[0]
        self.assertEqual(ctx.id, "retroarch")

    def test_set_data_with_empty_contexts_shows_a_message_not_a_crash(self):
        sheet, h, root = make_sheet()
        sheet.set_data({}, [])
        self.assertEqual(sheet.rows, [])
        self.assertFalse(sheet.prev.visible)
        self.assertFalse(sheet.next.visible)
        self.assertTrue(sheet.msg.text)


class TestTouchTargets(unittest.TestCase):
    """DESIGN.md / the CC7 brief: hit targets >=120 px. Rows are read-only
    (nothing to tap them into), so only Back/Prev/Next are interactive."""

    def test_back_prev_next_are_at_least_120px_tall(self):
        sheet, h, root = make_sheet()
        for w in (sheet.back, sheet.prev, sheet.next):
            self.assertGreaterEqual(w.rect[3], 120, w.name)

    def test_body_text_sizes_are_at_least_36px(self):
        sheet, h, root = make_sheet()
        for combo_lbl, action_lbl in sheet.rows:
            self.assertGreaterEqual(combo_lbl.size, 36)
            self.assertGreaterEqual(action_lbl.size, 36)


if __name__ == "__main__":
    unittest.main()
