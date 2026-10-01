"""Batch 1: the manual viewer's PDF tools (companion.manual_tool / ManualSheet):
1 or 2 pages, zoom (re-rendered sharper), Fit, First / Last, pan, swipe to turn."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import companion  # noqa: E402
from test_companion import FakeManuals, ROM_A, game_ev, make_controller  # noqa: E402


class PageManuals(FakeManuals):
    """FakeManuals plus render_page (single-page mode)."""

    def __init__(self, pages=6):
        FakeManuals.__init__(self, pages)
        self.singles = []

    def render_page(self, pdf, page, height=1080):
        self.singles.append((page, height))
        return "/c/p%d.png" % page


class Tools(unittest.TestCase):
    def setUp(self):
        self.c, self.view, self.timers, _v, _s, _cfg = make_controller()
        self.c.manuals = PageManuals()
        self.c.set_active(True)
        self.c.on_es_event(game_ev(ROM_A, "Alpha"))
        self.timers.advance(0.6)
        self.c.open_manual()
        self.mv = self.view.manual

    def paths(self):
        return [p[0].path for p in self.mv.pages]

    def test_single_page_mode(self):
        self.c.manual_tool("single")
        self.assertEqual(self.paths(), ["/c/p1.png"])
        self.assertEqual(self.c.manuals.singles[-1][0], 1)
        self.assertIn("1 of 6", self.mv.title.text)
        self.assertEqual(self.mv.pages_btn.text, "2 pages")
        self.c.manual_page(1)
        self.assertEqual(self.paths(), ["/c/p2.png"])       # one page per step
        self.assertEqual(self.c.manuals.prerenders[-1], [3])

    def test_back_to_spreads_starts_on_an_odd_page(self):
        self.c.manual_tool("single")
        self.c.manual_page(1)                               # page 2
        self.c.manual_tool("single")
        self.assertEqual(self.paths(), ["/c/p1.png", "/c/p2.png"])

    def test_zoom_renders_bigger_and_sharper(self):
        ph = self.mv.page_height()
        self.c.manual_tool("zoom_in")
        self.assertEqual(self.c.manuals.spreads[-1], (1, int(ph * 1.5)))
        (img, x, y) = self.mv.pages[0]
        bx, by, bw, bh = self.mv.body_rect()
        self.assertLess(y, by, "a zoomed page is taller than the area")
        self.assertEqual(self.mv.zoom_lbl.text, "150%")
        self.assertTrue(self.mv.fit_btn.enabled)
        for _ in range(5):
            self.c.manual_tool("zoom_in")
        self.assertEqual(self.c.manual_state["zoom"], companion.MANUAL_ZOOMS[-1])
        self.assertFalse(self.mv.zoom_in.enabled)
        self.c.manual_tool("fit")
        self.assertEqual(self.c.manual_state["zoom"], 1.0)
        self.assertFalse(self.mv.zoom_out.enabled)

    def test_first_and_last(self):
        self.c.manual_tool("last")
        self.assertEqual(self.paths(), ["/c/p5.png", "/c/p6.png"])
        self.c.manual_tool("first")
        self.assertEqual(self.paths(), ["/c/p1.png", "/c/p2.png"])
        self.c.manual_tool("single")
        self.c.manual_tool("last")
        self.assertEqual(self.paths(), ["/c/p6.png"])

    def test_pan_only_when_zoomed_and_clamped(self):
        self.mv.pan_by(300, 300)
        self.assertEqual(self.mv.offset, [0, 0])            # 100%: the spread fits
        self.c.manual_tool("zoom_in")
        self.c.manual_tool("zoom_in")                       # 200%
        self.mv.pan_by(0, 10000)
        top = min(py for _i, _px, py in self.mv.pages)
        by = self.mv.body_rect()[1]
        self.assertEqual(top + self.mv.offset[1], by, "panned past the top edge")
        self.mv.pan_by(0, -100000)
        bottom = max(py + i.h for i, _px, py in self.mv.pages)
        ay, ah = self.mv.body_rect()[1], self.mv.body_rect()[3]
        self.assertEqual(bottom + self.mv.offset[1], ay + ah, "panned past the bottom edge")

    def test_swipe_turns_pages_at_100_percent(self):
        calls = []
        self.mv.pan.h = type("H", (), {"manual_page": lambda _s, d: calls.append(d)})()
        a = self.mv.pan
        a.on_press(1, 1200, 500)
        a.on_move(1, 900, 510)
        a.on_release(1, 900, 510)                            # right-to-left: forward
        a.on_press(2, 600, 500)
        a.on_release(2, 1000, 520)                           # left-to-right: back
        a.on_press(3, 600, 500)
        a.on_release(3, 660, 500)                            # too short: nothing
        self.assertEqual(calls, [1, -1])

    def test_tools_do_not_overlap_the_pages(self):
        bx, by, bw, bh = self.mv.body_rect()
        for w in (self.mv.pages_btn, self.mv.zoom_in, self.mv.last_btn):
            self.assertGreaterEqual(w.rect[1], by + bh, w.name)
            self.assertGreaterEqual(w.rect[3], 120, w.name)


if __name__ == "__main__":
    unittest.main()
