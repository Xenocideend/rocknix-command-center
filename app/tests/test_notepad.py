"""Batch 1: notepad.py - the Notes tab (finger drawing + typing), offline."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import app_tabs  # noqa: E402
import notepad  # noqa: E402
import ui  # noqa: E402


class TmpBook(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="notes-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, "sub", "notebook.json")

    def book(self):
        return notepad.Notebook(self.path)


class Model(TmpBook):
    def test_stroke_undo_clear(self):
        b = self.book()
        self.assertTrue(b.begin_stroke(0.1, 0.2))
        b.add_point(0.3, 0.4)
        self.assertEqual(b.page["strokes"][0]["points"], [[0.1, 0.2], [0.3, 0.4]])
        self.assertTrue(b.undo())
        self.assertFalse(b.undo())
        b.type("hi")
        self.assertTrue(b.clear())
        self.assertEqual(b.page, notepad._page())

    def test_points_are_clamped(self):
        b = self.book()
        b.begin_stroke(-1, 2)
        self.assertEqual(b.page["strokes"][0]["points"][0], [0.0, 1.0])

    def test_pages(self):
        b = self.book()
        b.type("one")
        b.new_page()
        b.type("two")
        self.assertEqual((b.index, len(b.pages)), (1, 2))
        self.assertTrue(b.go(-1))
        self.assertEqual(b.page["text"], "one")
        self.assertFalse(b.go(-1))            # already first

    def test_text_and_backspace(self):
        b = self.book()
        b.type("ab")
        b.type("\n")
        self.assertTrue(b.backspace())
        self.assertEqual(b.page["text"], "ab")

    def test_caps(self):
        b = self.book()
        b.begin_stroke(0, 0)
        for i in range(notepad.MAX_POINTS + 10):
            b.add_point(0.5, 0.5)
        self.assertEqual(len(b.page["strokes"][0]["points"]), notepad.MAX_POINTS)
        self.assertFalse(b.type("x" * (notepad.MAX_TEXT + 1)))

    def test_save_load_round_trip(self):
        b = self.book()
        b.begin_stroke(0.1, 0.1, "#ffd54a")
        b.add_point(0.2, 0.2)
        b.new_page()
        b.type("page two")
        self.assertTrue(b.save())
        c = self.book()
        self.assertTrue(c.load())
        self.assertEqual(c.pages, b.pages)
        self.assertEqual(c.index, 1)

    def test_missing_file_is_a_new_notebook(self):
        b = self.book()
        self.assertFalse(b.load())
        self.assertEqual(b.pages, [notepad._page()])

    def test_corrupt_file_is_kept_aside_not_overwritten(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        b = self.book()
        self.assertFalse(b.load())
        self.assertTrue(os.path.exists(self.path + ".bad"))
        self.assertIn(".bad", b.load_note)

    def test_invalid_strokes_are_dropped_on_load(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"pages": [{"strokes": [{"points": [[0.5, 0.5]]},
                                              {"points": [[5, 5]]}, "junk"], "text": 7}]}, f)
        b = self.book()
        b.load()
        self.assertEqual(len(b.page["strokes"]), 1)
        self.assertEqual(b.page["text"], "")


class Host:
    def __init__(self):
        self.timers = []

    def close_sheet(self):
        pass

    def call_later(self, s, fn):
        self.timers.append(fn)
        return fn

    def cancel(self, t):
        if t in self.timers:
            self.timers.remove(t)


class Sheet(TmpBook):
    def make(self):
        self.host = Host()
        s = notepad.NotesSheet(self.host, book=self.book())
        s.layout((0, 0, 1920, 810))
        self.router = ui.TouchRouter(s)
        return s

    def test_a_finger_drag_draws_and_saves(self):
        s = self.make()
        cx, cy, cw, ch = s.canvas.rect
        self.router.down(("f", 1), cx + 100, cy + 100)
        for i in range(1, 11):
            self.router.move(("f", 1), cx + 100 + i * 20, cy + 100 + i * 10)
        self.router.up(("f", 1), cx + 320, cy + 210)
        pts = s.book.page["strokes"][0]["points"]
        self.assertGreaterEqual(len(pts), 10)
        with open(self.path) as f:
            self.assertEqual(len(json.load(f)["pages"][0]["strokes"]), 1)

    def test_drawing_a_page_uses_numeric_colours(self):
        """Device, 25 Sep: a hex colour string reached gfx and crashed rp5deck on the
        first stroke. This fake refuses anything that is not numbers, like cairo."""
        class StrictG:
            calls = 0

            def _chk(self, col):
                assert isinstance(col, tuple) and all(isinstance(v, (int, float)) for v in col), col
                StrictG.calls += 1

            def round_rect(self, r, rad, col):
                self._chk(col)

            def line(self, x0, y0, x1, y1, col, w=3):
                self._chk(col)

            def circle(self, x, y, r, col):
                self._chk(col)

            def text(self, t, r, size, col, *a, **k):
                self._chk(col)

            def text_block(self, t, r, size, col, *a, **k):
                self._chk(col)
        s = self.make()
        for color in notepad.PEN_COLORS:
            s.book.begin_stroke(0.1, 0.1, color)
            s.book.add_point(0.5, 0.5)
        s.book.begin_stroke(0.2, 0.2, "not-a-colour")          # a hand-edited file
        s.book.type("text")
        s.canvas.draw(StrictG())
        self.assertGreater(StrictG.calls, len(notepad.PEN_COLORS))

    def test_tiny_moves_are_not_recorded(self):
        s = self.make()
        cx, cy, cw, ch = s.canvas.rect
        self.router.down(("f", 1), cx + 100, cy + 100)
        for i in range(1, 6):
            self.router.move(("f", 1), cx + 100 + i, cy + 100)     # 1 px steps
        self.router.up(("f", 1), cx + 105, cy + 100)
        self.assertLessEqual(len(s.book.page["strokes"][0]["points"]), 3)

    def gesture_router(self, s):
        fired = []
        g = ui.SwipeRecognizer(lambda: 810, wants=lambda name: True,
                               on_gesture=lambda name: fired.append(name))
        return ui.TouchRouter(s, gestures=g), fired

    def test_a_vertical_stroke_is_not_taken_by_a_swipe(self):
        s = self.make()
        router, fired = self.gesture_router(s)
        cx, cy, cw, ch = s.canvas.rect
        router.down(("f", 1), cx + 300, cy + 50)
        for i in range(1, 21):
            router.move(("f", 1), cx + 300, cy + 50 + i * 20)       # 400 px straight down
        router.up(("f", 1), cx + 300, cy + 450)
        self.assertEqual(fired, [])
        self.assertGreaterEqual(len(s.book.page["strokes"][0]["points"]), 20)

    def test_control_type_mode_still_lets_swipes_through(self):
        s = self.make()
        s.toggle_mode()                                             # type: the canvas does not draw
        router, fired = self.gesture_router(s)
        cx, cy, cw, ch = s.canvas.rect
        router.down(("f", 1), cx + 300, cy + 20)
        for i in range(1, 11):
            router.move(("f", 1), cx + 300, cy + 20 + i * 20)
        router.up(("f", 1), cx + 300, cy + 220)
        self.assertEqual(fired, ["swipe_down"])

    def test_type_mode_shows_the_keyboard_and_types(self):
        s = self.make()
        self.assertFalse(s.kb.visible)
        s.toggle_mode()
        self.assertTrue(s.kb.visible)
        self.assertGreater(s.canvas.rect[3], 150, "page too small with the keyboard up")
        s.type("hello")
        self.assertEqual(s.book.page["text"], "hello")
        # in type mode a drag does not draw
        cx, cy, cw, ch = s.canvas.rect
        self.router.down(("f", 1), cx + 50, cy + 50)
        self.router.up(("f", 1), cx + 150, cy + 60)
        self.assertEqual(s.book.page["strokes"], [])

    def test_clear_needs_two_taps(self):
        s = self.make()
        s.type("keep me?")
        s.clear()
        self.assertEqual(s.book.page["text"], "keep me?")
        self.assertEqual(s.clear_btn.text, "Tap again")
        s.clear()
        self.assertEqual(s.book.page["text"], "")

    def test_clear_arm_expires(self):
        s = self.make()
        s.type("x")
        s.clear()
        self.host.timers[0]()               # the arm timer fires
        self.assertFalse(s.clear_armed)
        s.clear()                           # arms again, does not clear
        self.assertEqual(s.book.page["text"], "x")

    def test_tools_are_in_the_header_and_big_enough(self):
        s = self.make()
        for w in (s.file_btn, s.mode_btn, s.undo_btn, s.redo_btn, s.clear_btn, s.prev_btn, s.next_btn,
                  s.new_btn):
            self.assertLess(w.rect[1] + w.rect[3], s.body.rect[1] + 1, w.name)
            self.assertGreaterEqual(w.rect[2], 120, w.name)
            self.assertGreaterEqual(w.rect[3], 110, w.name)


class Tabs(unittest.TestCase):
    def test_notes_tab_is_listed_and_routes_inside(self):
        tabs = app_tabs.build_tabs(None, {}, {}, {"cc_open": True, "sheet": "notes"})
        ids = [t["id"] for t in tabs]
        self.assertIn(app_tabs.NOTES, ids)
        self.assertEqual(ids.index(app_tabs.NOTES), ids.index(app_tabs.HUD) + 1)
        self.assertTrue(next(t for t in tabs if t["id"] == app_tabs.NOTES)["active"])
        self.assertEqual(app_tabs.route(app_tabs.NOTES, None, {})[1], "notes")
        self.assertEqual(app_tabs.active_tab(None, {}, {"cc_open": True, "sheet": "notes"}),
                         app_tabs.NOTES)


class AppOpen(unittest.TestCase):
    def test_open_notes_loads_once_and_shows_the_sheet(self):
        from test_companion import AppCase
        case = AppCase("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        d = tempfile.mkdtemp(prefix="notes-app-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "nb.json")
        with open(path, "w") as f:
            json.dump({"pages": [{"strokes": [], "text": "saved text"}]}, f)
        os.environ["RP5DECK_NOTES"] = path
        self.addCleanup(os.environ.pop, "RP5DECK_NOTES", None)
        app = case.make_app()
        app.ui.cc.notes.book.path = path       # the sheet was built before the env var
        app.open_notes()
        self.assertEqual(app.ui.sheet, "notes")
        self.assertEqual(app.ui.cc.notes.book.page["text"], "saved text")
        app.ui.cc.notes.book.path = os.path.join(d, "other.json")
        app.open_notes()                       # a second open does not reload
        self.assertEqual(app.ui.cc.notes.book.page["text"], "saved text")

    def test_open_notes_during_a_game_uses_the_games_notebook(self):
        from test_companion import AppCase
        import companion
        case = AppCase("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        d = tempfile.mkdtemp(prefix="notes-app-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "notebook.json")
        with open(path, "w") as f:
            json.dump({"pages": [{"strokes": [], "text": "everyday"}]}, f)
        app = case.make_app()
        app.ui.cc.notes.book.path = path
        app.companion.running = companion.Target("game", "nes", "/r/Duck Hunt.nes", "Duck Hunt", None, True)
        app.open_notes()
        self.assertEqual(app.ui.cc.notes.current_name(), "Duck Hunt (nes)")
        app.companion.running = None
        app.open_notes()
        self.assertEqual(app.ui.cc.notes.current_name(), "notebook")
        self.assertEqual(app.ui.cc.notes.book.page["text"], "everyday")


if __name__ == "__main__":
    unittest.main()
