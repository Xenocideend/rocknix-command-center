#!/usr/bin/env python3
"""CC6 app_tabs, the parts that need no patched main.py: the tab model
(which tabs exist, which one is active), routing a tab to a switch target,
and the strip widget. The controller inside the real main.App is covered by
tests/cc6_patched_cases.py (run by tests/test_cc6_merged.py)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import app_tabs  # noqa: E402
import window_switcher as wsw  # noqa: E402
from cc6_sway_sim import Sim  # noqa: E402

ALL = {"web": True, "yt": True}
NONE = {"web": False, "yt": False}


def snap_of(sim, cc="DSI-1", es="DP-1"):
    return wsw.scan(sim.tree(), cc, es).to_dict()


def ids(tabs):
    return [t["id"] for t in tabs]


def active(tabs):
    return [t["id"] for t in tabs if t["active"]]


class TestWhichTabsExist(unittest.TestCase):
    def test_nothing_running_nothing_installed(self):
        tabs = app_tabs.build_tabs(snap_of(Sim()), {}, NONE, {"mode": "FULL"})
        self.assertEqual(ids(tabs), ["companion", "hud"])

    def test_installed_apps_can_start(self):
        tabs = app_tabs.build_tabs(snap_of(Sim()), {}, ALL, {"mode": "FULL"})
        self.assertEqual(ids(tabs), ["companion", "hud", "browser", "discord", "ytapp"])
        self.assertFalse(any(t["running"] for t in tabs if t["id"] in ("browser", "ytapp")))

    def test_a_running_app_has_its_tab_even_when_the_binary_check_says_no(self):
        s = Sim()
        s.map("DSI-1", app_id="rp5deck-web")
        tabs = app_tabs.build_tabs(snap_of(s), {"app": "web", "label": "Discord"}, NONE,
                                   {"mode": "BAR"})
        self.assertEqual(ids(tabs), ["companion", "hud", "browser", "discord"])
        by = {t["id"]: t for t in tabs}
        self.assertTrue(by["discord"]["running"])
        self.assertFalse(by["browser"]["running"])
        self.assertEqual(active(tabs), ["discord"])

    def test_youtube_tv_is_its_own_window_kind_not_a_web_label(self):
        """W2b: a global user-agent override cannot share the Browser/
        Discord Firefox/profile - this tile is its own window kind
        (wsw.YTAPP, browser.TV_APP_ID), never a "web" label any more."""
        s = Sim()
        s.map("DSI-1", app_id="rp5deck-ytapp")
        tabs = app_tabs.build_tabs(snap_of(s), {}, NONE, {"mode": "BAR"})
        self.assertEqual(ids(tabs), ["companion", "hud", "ytapp"])
        by = {t["id"]: t for t in tabs}
        self.assertTrue(by["ytapp"]["running"])
        self.assertEqual(active(tabs), ["ytapp"])

    def test_youtube_tv_tab_can_start_even_when_not_running(self):
        tabs = app_tabs.build_tabs(snap_of(Sim()), {}, {"web": True, "yt": False},
                                   {"mode": "FULL"})
        self.assertIn("ytapp", ids(tabs))
        self.assertFalse({t["id"]: t for t in tabs}["ytapp"]["running"])

    def test_emulator_tab_only_while_its_second_window_exists(self):
        s = Sim()
        s.map("DP-1", title="[w1] melonDS", fullscreen=True, focus=True)
        w2 = s.map("DSI-1", title="[w2] melonDS")
        tabs = app_tabs.build_tabs(snap_of(s), {}, NONE, {"mode": "HIDDEN"})
        self.assertEqual(ids(tabs), ["companion", "emu:%d" % w2, "hud"])
        self.assertEqual(active(tabs), ["emu:%d" % w2])
        s.command(wsw.build_park(w2))
        tabs = app_tabs.build_tabs(snap_of(s), {}, NONE, {"mode": "FULL", "cc_open": True})
        self.assertIn("emu:%d" % w2, ids(tabs))                 # parked: still a tab
        self.assertEqual(active(tabs), [])
        s.close(w2)
        tabs = app_tabs.build_tabs(snap_of(s), {}, NONE, {"mode": "FULL"})
        self.assertEqual(ids(tabs), ["companion", "hud"])

    def test_an_unknown_window_never_gets_a_tab(self):
        s = Sim()
        s.map("DSI-1", app_id="rp5deck-test-other", title="foot")
        s.map("DSI-1", app_id="emulationstation")
        tabs = app_tabs.build_tabs(snap_of(s), {}, NONE, {"mode": "HIDDEN"})
        self.assertEqual(ids(tabs), ["companion", "hud"])


class TestActiveTab(unittest.TestCase):
    def test_follows_what_is_on_screen(self):
        cases = [({"mode": "FULL", "cc_open": False}, "companion"),
                 ({"mode": "FULL", "cc_open": True, "sheet": "hud"}, "hud"),
                 ({"mode": "FULL", "cc_open": True, "sheet": None}, None),
                 ({"mode": "FULL", "cc_open": True, "sheet": "mixer"}, None)]
        for view, want in cases:
            self.assertEqual(app_tabs.active_tab(snap_of(Sim()), {}, view), want, view)

    def test_sway_unreadable_falls_back_to_the_mode(self):
        self.assertEqual(app_tabs.active_tab(None, {"app": "web", "label": "Browser"},
                                             {"mode": "BAR"}), "browser")

    def test_youtube_tv_window_wins_over_a_shown_web_window(self):
        """Cannot both be SHOWN at once in practice (window_switcher parks
        one before showing the other), but active_tab() must still pick
        deterministically if it ever happened."""
        s = Sim()
        s.map("DSI-1", app_id="rp5deck-ytapp")
        self.assertEqual(app_tabs.active_tab(snap_of(s), {}, {"mode": "BAR"}), "ytapp")


class TestRoute(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(app_tabs.route("companion", None, {}), ("internal", "companion"))
        self.assertEqual(app_tabs.route("hud", None, {}), ("internal", "hud"))
        self.assertEqual(app_tabs.route("cc", None, {}), ("internal", "cc"))
        self.assertEqual(app_tabs.route("browser", None, {}), ("web", "browser"))
        self.assertEqual(app_tabs.route("discord", None, {}), ("web", "discord"))
        self.assertEqual(app_tabs.route("ytapp", None, {}), ("ytapp", "ytapp"))
        self.assertEqual(app_tabs.route("emu:12", None, {}), ("emu:12", "emu"))
        with self.assertRaises(ValueError):
            app_tabs.route("settings", None, {})
        with self.assertRaises(ValueError):
            app_tabs.route("youtube", None, {})    # YT3: the old mpv tab id no longer exists
        for tab in ("companion", "hud", "cc", "browser", "discord", "ytapp", "emu:12"):
            target, _ = app_tabs.route(tab, None, {})
            wsw.parse_target(target)                            # every target is one it knows


class TestStrip(unittest.TestCase):
    def tabs(self, n, act=0):
        return [{"id": "t%d" % i, "label": "Tab %d" % i, "running": i % 2 == 0,
                 "active": i == act} for i in range(n)]

    def test_layout_fits_the_panel_and_targets_stay_big(self):
        for n in range(1, 9):
            strip = app_tabs.TabStrip(lambda i: None)
            strip.set_tabs(self.tabs(n))
            strip.layout((0, 140, 1920, app_tabs.TABS_H))
            rects = [b.rect for b in strip.buttons]
            self.assertEqual(len(rects), n)
            self.assertLessEqual(rects[-1][0] + rects[-1][2], 1920 - 19)
            for a, b in zip(rects, rects[1:]):
                self.assertLessEqual(a[0] + a[2], b[0])           # no overlap
            for r in rects:
                self.assertGreaterEqual(r[3], 120)
                self.assertGreaterEqual(r[2], 200)                 # 8 tabs still >= 200 px wide

    def test_a_tap_reports_the_tab_id_and_unchanged_tabs_do_not_rebuild(self):
        got = []
        strip = app_tabs.TabStrip(got.append)
        self.assertTrue(strip.set_tabs(self.tabs(3)))
        strip.layout((0, 0, 1920, 130))
        b = strip.buttons[1]
        x, y, w, h = b.rect
        b.on_press(1, x + 5, y + 5)
        b.on_release(1, x + 5, y + 5)
        self.assertEqual(got, ["t1"])
        before = list(strip.buttons)
        self.assertFalse(strip.set_tabs(self.tabs(3)))
        self.assertEqual(strip.buttons, before)
        self.assertTrue(strip.set_tabs(self.tabs(3, act=2)))

    def test_active_tab_is_drawn_in_the_accent_colour(self):
        strip = app_tabs.TabStrip(lambda i: None)
        strip.set_tabs(self.tabs(2, act=1))
        self.assertEqual(strip.buttons[1].colors()[0], app_tabs.THEME["accent"])
        self.assertEqual(strip.buttons[0].colors()[0], app_tabs.THEME["tile"])


# ---------------------------------------------------------------------------
# W: the BAR strip's inline picker hook (TabsController.bar_tabs() / rebuild()
# / _close_bar_tabs(), screens.CommandCenter.toggle_bar_tabs / close_bar_tabs
# in patches/W-screens.patch) - a light-weight fake app/ui, no real main.App
# or screens.py needed, unlike the full integration in
# tests/cc6_patched_cases.py::TestBarTabsInlinePicker.
# ---------------------------------------------------------------------------
class FakeBarTabsCC:
    """Stands in for screens.CommandCenter's two new pieces."""

    def __init__(self):
        self.bar_tabs_open = False
        self.closes = 0

    def close_bar_tabs(self):
        self.closes += 1
        self.bar_tabs_open = False


class FakeBarTabsUI:
    def __init__(self, cc):
        self.cc = cc
        self.sheet = None


class FakeBarTabsApp:
    def __init__(self):
        self.mode = "FULL"
        self.pull = None
        self.output, self.es_output = "DSI-1", "DP-1"
        self.ui = FakeBarTabsUI(FakeBarTabsCC())
        self.state_dirty = False


def _controller(app=None):
    app = app or FakeBarTabsApp()
    c = app_tabs.TabsController(app, submit=lambda fn, *a, done=None: None,
                                switcher=object(), avail=lambda: dict(NONE))
    return app, c


class TestBarTabsInlinePickerHook(unittest.TestCase):
    def test_bar_tabs_is_the_normal_list_plus_a_home_pill(self):
        app, c = _controller()
        ids = [t["id"] for t in c.tabs()]
        bar_ids = [t["id"] for t in c.bar_tabs()]
        self.assertEqual(bar_ids, ids + [app_tabs.CC])
        home = [t for t in c.bar_tabs() if t["id"] == app_tabs.CC][0]
        self.assertEqual(home["label"], "Home")
        self.assertFalse(home["active"])
        self.assertFalse(home["running"])

    def test_rebuild_shows_the_home_pill_only_while_the_picker_is_open(self):
        app, c = _controller()
        self.assertNotIn(app_tabs.CC, [t["id"] for t in c.strip.tabs])
        app.ui.cc.bar_tabs_open = True
        c.rebuild()
        self.assertIn(app_tabs.CC, [t["id"] for t in c.strip.tabs])
        app.ui.cc.bar_tabs_open = False
        c.rebuild()
        self.assertNotIn(app_tabs.CC, [t["id"] for t in c.strip.tabs])

    def test_a_tap_always_closes_the_picker(self):
        app, c = _controller()
        app.ui.cc.bar_tabs_open = True
        c.select(app_tabs.COMPANION)
        self.assertEqual(app.ui.cc.closes, 1)
        self.assertFalse(app.ui.cc.bar_tabs_open)

    def test_select_is_safe_with_no_cc_attribute_at_all(self):
        app, c = _controller()
        app.ui = type("PlainUI", (), {"sheet": None})()   # e.g. an older/unpatched screens.py
        c.select(app_tabs.COMPANION)                      # must not raise


if __name__ == "__main__":
    unittest.main()
