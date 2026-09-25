#!/usr/bin/env python3
"""screens.Home tile-customisation mode (owner: "add a customisation mode
with drag and drop tiles"): entering it via a long-press, dragging a tile
to reorder (following the finger, reflowing the rest live), hiding/showing
a tile (Settings can never be hidden), Reset tile layout, and that every
interactive control - tile, hide badge, Done, Reset - is >= 120 px both
ways. Pure ui.py widgets, no SDL/cairo; a FakeHost stands in for main.App's
call_later()/cancel()/on_tile_layout_changed() and the tile-open handlers.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import screens  # noqa: E402
import ui       # noqa: E402

W, H = 1920, 1080
HOME_TILE_NAMES = ("home.mixer", "home.hud", "home.browser",
                   "home.discord", "home.ytapp", "home.hotkeys", "home.clean", "home.settings",
                   "home.swap", "home.lights", "home.keyboard", "home.sleep")


class FakeHost:
    """Enough of main.App for screens.Home: the timer pair its long-press
    uses (the same call_at/call_later/cancel idiom main.App itself uses
    for the auto-close countdown and the RGB keeper poll), every tile
    open_*/toggle_* handler (as no-op recorders), and
    on_tile_layout_changed() to capture what Home persists."""

    def __init__(self):
        self.now = 0.0
        self.timers = []          # [deadline, fn, cancelled]
        self.opened = []
        self.layout_changes = []

    def call_later(self, delay, fn):
        h = [self.now + delay, fn, False]
        self.timers.append(h)
        return h

    @staticmethod
    def cancel(h):
        if h is not None:
            h[2] = True

    def tick(self, dt):
        """Advance the fake clock and run any timers now due, oldest first
        (matches main.App._run_timers's behaviour closely enough for these
        tests: nothing here relies on heap tie-breaking order)."""
        self.now += dt
        due = [h for h in self.timers if not h[2] and h[0] <= self.now]
        self.timers = [h for h in self.timers if h not in due]
        for h in due:
            if not h[2]:
                h[1]()

    def on_tile_layout_changed(self, order, hidden):
        self.layout_changes.append((list(order), list(hidden)))

    def __getattr__(self, name):
        # open_mixer, open_hud, open_browser, open_youtube, open_discord,
        # open_hotkeys, open_clean_state, open_settings, ask_swap_screens,
        # open_lights, toggle_keyboard, close_command_center - every tile's
        # on_click, recorded rather than special-cased one by one.
        def _record():
            self.opened.append(name)
        return _record


def make_home():
    h = FakeHost()
    home = screens.Home(h)
    home.layout((0, 0, W, H))
    return home, h


def targets(home):
    """name -> rect for every visible, interactive widget under Home -
    mirrors main.py App.state()'s own ui.targets() shape closely enough
    for the >=120px check below."""
    out = {}

    def walk(w):
        if not w.visible:
            return
        if getattr(w, "interactive", False) and w.name:
            out[w.name] = w.rect
        for c in getattr(w, "children", ()):
            walk(c)
    walk(home)
    return out


def tap(router, home, name):
    t = targets(home)
    x, y, w, h = t[name]
    pid = ("f", id(name))
    router.down(pid, x + w / 2, y + h / 2)
    router.up(pid, x + w / 2, y + h / 2)


class TestConstruction(unittest.TestCase):
    def test_twelve_tiles_in_the_expected_order(self):
        home, _h = make_home()
        self.assertEqual(tuple(t.name for t in home.tiles), HOME_TILE_NAMES)
        self.assertEqual(home.order, list(HOME_TILE_NAMES))
        self.assertEqual(home.hidden, set())

    def test_settings_has_no_hide_badge_everyone_else_does(self):
        home, _h = make_home()
        self.assertNotIn("home.settings", home.badges)
        for name in HOME_TILE_NAMES:
            if name != "home.settings":
                self.assertIn(name, home.badges, name)

    def test_grid_is_6_cols_for_12_tiles(self):
        home, _h = make_home()
        self.assertEqual(home.grid_cols(), 6)


class TestNormalModeTapping(unittest.TestCase):
    def setUp(self):
        self.home, self.h = make_home()
        self.router = ui.TouchRouter(self.home)

    def test_a_plain_tap_opens_the_tile(self):
        tap(self.router, self.home, "home.mixer")
        self.assertEqual(self.h.opened, ["open_mixer"])
        self.assertFalse(self.home.edit_mode)

    def test_release_before_the_long_press_threshold_still_opens_it(self):
        t = self.home._tiles_by_name["home.hud"]
        x, y, w, h = t.rect
        pid = ("f", 1)
        self.router.down(pid, x + w / 2, y + h / 2)
        self.h.tick(screens.EDIT_LONG_PRESS_S - 0.1)
        self.router.up(pid, x + w / 2, y + h / 2)
        self.assertEqual(self.h.opened, ["open_hud"])
        self.assertFalse(self.home.edit_mode)


class TestLongPressEntersEdit(unittest.TestCase):
    def setUp(self):
        self.home, self.h = make_home()
        self.router = ui.TouchRouter(self.home)

    def _press_and_hold(self, name, dt, pid=("f", 1)):
        t = self.home._tiles_by_name[name]
        x, y, w, h = t.rect
        self.router.down(pid, x + w / 2, y + h / 2)
        self.h.tick(dt)
        return x + w / 2, y + h / 2

    def test_holding_past_the_threshold_enters_edit_mode(self):
        cx, cy = self._press_and_hold("home.browser", screens.EDIT_LONG_PRESS_S + 0.05)
        self.assertTrue(self.home.edit_mode)
        self.router.up(("f", 1), cx, cy)
        self.assertEqual(self.h.opened, [])          # the same touch never opens Browser

    def test_moving_off_the_tile_before_the_threshold_cancels_it(self):
        t = self.home._tiles_by_name["home.browser"]
        x, y, w, h = t.rect
        pid = ("f", 1)
        self.router.down(pid, x + w / 2, y + h / 2)
        self.router.move(pid, x + w + 500, y + h + 500)     # well outside the tile
        self.h.tick(screens.EDIT_LONG_PRESS_S + 0.05)
        self.assertFalse(self.home.edit_mode)

    def test_a_second_finger_elsewhere_does_not_also_arm(self):
        self._press_and_hold("home.discord", 0.1, pid=("f", 1))
        t2 = self.home._tiles_by_name["home.hotkeys"]
        x2, y2, w2, h2 = t2.rect
        self.router.down(("f", 2), x2 + w2 / 2, y2 + h2 / 2)
        self.router.up(("f", 2), x2 + w2 / 2, y2 + h2 / 2)
        self.assertEqual(self.h.opened, ["open_hotkeys"])   # a plain tap, unaffected
        self.assertFalse(self.home.edit_mode)

    def test_overlay_mode_refuses_to_enter_edit(self):
        self.home.set_overlay(True)
        self._press_and_hold("home.mixer", screens.EDIT_LONG_PRESS_S + 0.05)
        self.assertFalse(self.home.edit_mode)


class TestEditMode(unittest.TestCase):
    def setUp(self):
        self.home, self.h = make_home()
        self.router = ui.TouchRouter(self.home)
        self.home.enter_edit()

    def test_done_and_reset_appear_close_and_sink_hide(self):
        self.assertTrue(self.home.edit_done.visible)
        self.assertTrue(self.home.edit_reset.visible)
        self.assertFalse(self.home.close.visible)
        self.assertFalse(self.home.sub.visible)

    def test_every_tile_has_a_visible_hide_badge_except_settings(self):
        for name, b in self.home.badges.items():
            self.assertTrue(b.visible, name)
        self.assertNotIn("home.settings", self.home.badges)

    def test_drag_reorders_and_persists(self):
        t = self.home._tiles_by_name["home.mixer"]
        x, y, w, h = t.rect
        pid = ("f", 1)
        self.router.down(pid, x + w / 2, y + h / 2)
        self.router.move(pid, x + w / 2 + 1200, y + h / 2)
        self.router.up(pid, x + w / 2 + 1200, y + h / 2)
        self.assertNotEqual(self.home.order[0], "home.mixer")
        self.assertIn("home.mixer", self.home.order)
        self.assertEqual(sorted(self.home.order), sorted(HOME_TILE_NAMES))
        self.assertTrue(self.h.layout_changes)
        order, hidden = self.h.layout_changes[-1]
        self.assertEqual(sorted(order), sorted(HOME_TILE_NAMES))
        self.assertEqual(hidden, [])

    def test_drag_follows_the_finger_during_the_move(self):
        t = self.home._tiles_by_name["home.hud"]
        x0, y0, w, h = t.rect
        pid = ("f", 1)
        self.router.down(pid, x0 + w / 2, y0 + h / 2)
        self.router.move(pid, x0 + w / 2 + 40, y0 + h / 2 + 10)
        self.assertAlmostEqual(t.rect[0], x0 + 40, delta=1)
        self.assertAlmostEqual(t.rect[1], y0 + 10, delta=1)
        self.router.up(pid, x0 + w / 2 + 40, y0 + h / 2 + 10)

    def test_a_second_finger_cannot_steal_a_tile_already_dragging(self):
        t1 = self.home._tiles_by_name["home.mixer"]
        x1, y1, w1, h1 = t1.rect
        self.router.down(("f", 1), x1 + w1 / 2, y1 + h1 / 2)
        t2 = self.home._tiles_by_name["home.hud"]
        x2, y2, w2, h2 = t2.rect
        self.router.down(("f", 2), x2 + w2 / 2, y2 + h2 / 2)
        self.assertIs(self.home.dragging_tile, t1)
        self.router.up(("f", 1), x1 + w1 / 2, y1 + h1 / 2)
        self.router.up(("f", 2), x2 + w2 / 2, y2 + h2 / 2)

    def test_hide_badge_hides_a_tile_and_it_disappears_in_normal_mode(self):
        tap(self.router, self.home, "home.browser.hide")
        self.assertIn("home.browser", self.home.hidden)
        self.home.exit_edit()
        self.assertNotIn(self.home._tiles_by_name["home.browser"], self.home.visible_tiles())
        self.assertTrue(self.home._tiles_by_name["home.browser"].visible is False)

    def test_hidden_tile_still_shown_and_toggleable_in_edit_mode(self):
        self.home._toggle_hidden("home.browser")
        self.assertIn(self.home._tiles_by_name["home.browser"], self.home.visible_tiles())
        tap(self.router, self.home, "home.browser.hide")
        self.assertNotIn("home.browser", self.home.hidden)

    def test_settings_cannot_be_hidden_even_by_direct_call(self):
        self.home._toggle_hidden("home.settings")
        self.assertNotIn("home.settings", self.home.hidden)

    def test_grid_shrinks_in_normal_mode_when_tiles_are_hidden(self):
        for name in ("home.browser", "home.discord", "home.ytapp", "home.sleep"):
            self.home._toggle_hidden(name)
        self.home.exit_edit()
        self.assertEqual(home_visible_count(self.home), 8)
        self.assertEqual(self.home.grid_cols(), 4)

    def test_reset_tile_layout_restores_defaults(self):
        t = self.home._tiles_by_name["home.mixer"]
        x, y, w, h = t.rect
        pid = ("f", 1)
        self.router.down(pid, x + w / 2, y + h / 2)
        self.router.move(pid, x + w / 2 + 1200, y + h / 2)
        self.router.up(pid, x + w / 2 + 1200, y + h / 2)
        self.home._toggle_hidden("home.browser")
        self.assertNotEqual(self.home.order, list(HOME_TILE_NAMES))
        self.home.reset_layout()
        self.assertEqual(self.home.order, list(HOME_TILE_NAMES))
        self.assertEqual(self.home.hidden, set())
        order, hidden = self.h.layout_changes[-1]
        self.assertEqual(order, list(HOME_TILE_NAMES))
        self.assertEqual(hidden, [])

    def test_done_exits_edit_mode_and_notifies(self):
        self.home.exit_edit()
        self.assertFalse(self.home.edit_mode)
        self.assertTrue(self.home.edit_done.visible is False)
        self.assertTrue(self.h.layout_changes)


def home_visible_count(home):
    return sum(1 for t in home.tiles if t.visible)


class TestPersistedStateIn(unittest.TestCase):
    def test_set_order_and_set_hidden_from_config(self):
        home, _h = make_home()
        stored = ["home.settings", "home.mixer", "home.hud", "home.browser", "home.youtube",
                 "home.discord", "home.ytapp", "home.hotkeys", "home.clean", "home.swap",
                 "home.lights", "home.keyboard"]
        home.set_order(stored)
        # YT3: a stored order saved before the old "YouTube" (mpv) tile was
        # removed - "home.youtube" is dropped silently (unknown to this Home
        # instance now), every real tile stays, nothing crashes. YT4: this
        # same stored order predates the Sleep tile too - a genuinely new
        # tile is appended, never dropped (set_order()'s other own rule).
        expected = [n for n in stored if n != "home.youtube"] + ["home.sleep"]
        home.set_hidden(["home.mixer"])
        self.assertEqual(home.order, expected)
        self.assertEqual(set(home.order), set(HOME_TILE_NAMES))
        self.assertNotIn("home.youtube", home.order)
        self.assertEqual(home.hidden, {"home.mixer"})
        self.assertNotIn(home._tiles_by_name["home.mixer"], home.visible_tiles())

    def test_set_order_tolerates_unknown_and_missing_names(self):
        home, _h = make_home()
        home.set_order(["home.gone", "home.settings"])       # a stale/foreign name + a partial list
        self.assertEqual(home.order[0], "home.settings")
        self.assertEqual(set(home.order), set(HOME_TILE_NAMES))  # every real tile still reachable

    def test_set_hidden_refuses_settings_and_unknown_names(self):
        home, _h = make_home()
        home.set_hidden(["home.settings", "home.gone", "home.mixer"])
        self.assertEqual(home.hidden, {"home.mixer"})


class TestTouchTargets(unittest.TestCase):
    """DESIGN: every interactive control >= 120 px both ways (RGBSheet and
    CC1's own tile-grid tests use the same "grid stuck too small -> red"
    style proof)."""

    def test_tiles_badges_and_edit_buttons(self):
        home, _h = make_home()
        home.enter_edit()
        t = targets(home)
        checked = 0
        for name, (x, y, w, h) in t.items():
            if name.startswith("home."):
                self.assertGreaterEqual(w, 120, name)
                self.assertGreaterEqual(h, 120, name)
                checked += 1
        self.assertGreater(checked, 10)

    def test_badges_do_not_overlap_home_close_at_construction_size(self):
        home, _h = make_home()
        home.enter_edit()
        for name, b in home.badges.items():
            self.assertEqual(b.rect[2], screens.TILE_BADGE, name)
            self.assertEqual(b.rect[3], screens.TILE_BADGE, name)


class TestDualScreenNotice(unittest.TestCase):
    """screens.Home.set_dualscreen_notice (DS): the shared banner above the
    tile grid, never a tile - dualscreen_keys_view.py drives it the same way
    CC1 drives a sheet, but this is not a sheet: it must be visible without
    any tap. (The banner is shared with CHG - see TestBannerPriority for the
    "both active at once" rule.)"""

    def test_hidden_by_default_and_grid_unchanged(self):
        home, _h = make_home()
        self.assertFalse(home.banner_label.visible)
        self.assertFalse(home.banner_action.visible)
        before = home._grid_rect()
        home.set_dualscreen_notice("")     # explicit clear is still a no-op
        self.assertEqual(home._grid_rect(), before)

    def test_setting_text_shows_the_banner_and_shrinks_the_grid(self):
        home, _h = make_home()
        before = home._grid_rect()
        home.set_dualscreen_notice("Dual-screen settings missing: 3ds.screen_layout")
        self.assertTrue(home.banner_label.visible)
        self.assertTrue(home.banner_action.visible)
        self.assertEqual(home.banner_action.text, "Restore")
        self.assertIn("3ds.screen_layout", home.banner_label.text)
        after = home._grid_rect()
        self.assertGreater(after[1], before[1])          # grid starts lower
        self.assertLess(after[3], before[3])              # and is shorter

    def test_restore_button_meets_the_touch_target_floor(self):
        home, _h = make_home()
        home.set_dualscreen_notice("Dual-screen settings missing: wiiu.gamepad_enabled")
        _, _, w, h = home.banner_action.rect
        self.assertGreaterEqual(min(w, h), 120)

    def test_clearing_hides_it_again_and_restores_grid_size(self):
        home, _h = make_home()
        before = home._grid_rect()
        home.set_dualscreen_notice("Dual-screen settings missing: wiiu.gamepad_enabled")
        home.set_dualscreen_notice("")
        self.assertFalse(home.banner_label.visible)
        self.assertFalse(home.banner_action.visible)
        self.assertEqual(home._grid_rect(), before)

    def test_tapping_restore_calls_the_host(self):
        home, h = make_home()
        home.set_dualscreen_notice("Dual-screen settings missing: 3ds.screen_layout")
        home.banner_action.clicked()
        self.assertIn("on_dualscreen_restore", h.opened)

    def test_hidden_during_cc5_overlay_even_if_active(self):
        home, _h = make_home()
        home.set_dualscreen_notice("Dual-screen settings missing: 3ds.screen_layout")
        home.set_overlay(True)
        self.assertFalse(home.banner_label.visible)
        self.assertFalse(home.banner_action.visible)
        home.set_overlay(False)
        self.assertTrue(home.banner_label.visible)
        self.assertTrue(home.banner_action.visible)


class TestChargeNotice(unittest.TestCase):
    """screens.Home.set_charge_notice (CHG): same shared banner, but its own
    action label (Dismiss) and its own CC5 OVERLAY rule (stays up)."""

    def test_setting_text_shows_dismiss(self):
        home, _h = make_home()
        home.set_charge_notice("Charger connected but not charging.")
        self.assertTrue(home.banner_label.visible)
        self.assertEqual(home.banner_action.text, "Dismiss")
        self.assertIn("not charging", home.banner_label.text)

    def test_tapping_dismiss_calls_the_host(self):
        home, h = make_home()
        home.set_charge_notice("Charger connected but not charging.")
        home.banner_action.clicked()
        self.assertIn("on_charge_notice_dismiss", h.opened)

    def test_clearing_hides_it(self):
        home, _h = make_home()
        home.set_charge_notice("Charger connected but not charging.")
        home.set_charge_notice("")
        self.assertFalse(home.banner_label.visible)

    def test_stays_visible_during_cc5_overlay(self):
        home, _h = make_home()
        home.set_charge_notice("Charger connected but not charging.")
        home.set_overlay(True)
        self.assertTrue(home.banner_label.visible)
        self.assertTrue(home.banner_action.visible)
        self.assertEqual(home.banner_action.text, "Dismiss")


class TestBannerPriority(unittest.TestCase):
    """Both notices active at once: CHG (charge) always outranks DS
    (dualscreen) for the one shared banner slot - the owner's own rule."""

    def test_charge_wins_when_both_are_active(self):
        home, _h = make_home()
        home.set_dualscreen_notice("Dual-screen settings missing: 3ds.screen_layout")
        home.set_charge_notice("Charger connected but not charging.")
        self.assertIn("not charging", home.banner_label.text)
        self.assertEqual(home.banner_action.text, "Dismiss")

    def test_dualscreen_reappears_once_charge_clears(self):
        home, _h = make_home()
        home.set_dualscreen_notice("Dual-screen settings missing: 3ds.screen_layout")
        home.set_charge_notice("Charger connected but not charging.")
        home.set_charge_notice("")
        self.assertIn("3ds.screen_layout", home.banner_label.text)
        self.assertEqual(home.banner_action.text, "Restore")

    def test_order_of_arrival_does_not_matter(self):
        home, _h = make_home()
        home.set_charge_notice("Charger connected but not charging.")
        home.set_dualscreen_notice("Dual-screen settings missing: wiiu.gamepad_enabled")
        self.assertIn("not charging", home.banner_label.text)

    def test_neither_active_hides_the_banner(self):
        home, _h = make_home()
        home.set_dualscreen_notice("Dual-screen settings missing: 3ds.screen_layout")
        home.set_charge_notice("Charger connected but not charging.")
        home.set_dualscreen_notice("")
        home.set_charge_notice("")
        self.assertFalse(home.banner_label.visible)
        self.assertFalse(home.banner_action.visible)


if __name__ == "__main__":
    unittest.main()
