"""Owner, 26 Sep: "Add standard notepad features to the note pad like saving, loading, text
color and size". Notebooks (open / new / delete-to-.deleted, the last one reopens), pen
width, eraser, redo, and per-page text colour and size."""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import notepad  # noqa: E402
import ui  # noqa: E402
from test_notepad import Host, TmpBook  # noqa: E402


class StrictG:
    """Refuses any colour that is not a tuple of numbers, like cairo (25 Sep crash)."""
    calls = 0

    def _chk(self, col):
        assert isinstance(col, tuple) and all(isinstance(v, (int, float)) for v in col), col
        StrictG.calls += 1

    def round_rect(self, r, rad, col):
        self._chk(col)

    def stroke_round_rect(self, r, rad, col, w=3):
        self._chk(col)

    def line(self, x0, y0, x1, y1, col, w=3):
        self._chk(col)

    def circle(self, x, y, r, col):
        self._chk(col)

    def text(self, t, r, size, col, *a, **k):
        self._chk(col)

    def text_block(self, t, r, size, col, *a, **k):
        self._chk(col)


class Model(TmpBook):
    def test_redo_brings_back_what_undo_took_and_a_new_stroke_clears_it(self):
        b = self.book()
        b.begin_stroke(0.1, 0.1)
        b.begin_stroke(0.2, 0.2)
        self.assertTrue(b.undo())
        self.assertEqual(len(b.page["strokes"]), 1)
        self.assertTrue(b.redo())
        self.assertEqual(len(b.page["strokes"]), 2)
        b.undo()
        b.begin_stroke(0.3, 0.3)
        self.assertFalse(b.redo())

    def test_eraser_removes_only_the_strokes_it_touches(self):
        b = self.book()
        b.begin_stroke(0.1, 0.1)
        b.add_point(0.2, 0.1)
        b.begin_stroke(0.8, 0.8)
        self.assertEqual(b.erase_near(0.2, 0.1, 0.02, 0.02), 1)
        self.assertEqual([s["points"][0] for s in b.page["strokes"]], [[0.8, 0.8]])
        self.assertEqual(b.erase_near(0.5, 0.5, 0.02, 0.02), 0)

    def test_text_style_is_per_page_saved_and_checked_on_load(self):
        b = self.book()
        b.set_text_style(color="#ff5a5a", size=60)
        b.set_text_style(color="purple", size=999)                     # not offered: ignored
        self.assertEqual((b.page["text_color"], b.page["text_size"]), ("#ff5a5a", 60))
        b.new_page()
        self.assertEqual((b.page["text_color"], b.page["text_size"]), ("", notepad.TEXT_SIZE))
        b.save()
        with open(self.path) as f:
            data = json.load(f)
        data["pages"][0]["text_size"] = 7                              # hand-edited
        with open(self.path, "w") as f:
            json.dump(data, f)
        b2 = self.book()
        b2.load()
        self.assertEqual(b2.pages[0]["text_color"], "#ff5a5a")
        self.assertEqual(b2.pages[0]["text_size"], notepad.TEXT_SIZE)

    def test_clear_keeps_the_text_style(self):
        b = self.book()
        b.set_text_style(color="#6be675", size=28)
        b.type("hello")
        b.clear()
        self.assertEqual((b.page["text"], b.page["text_color"], b.page["text_size"]),
                         ("", "#6be675", 28))


class Library(TmpBook):
    def lib(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        return notepad.NotesLibrary(os.path.dirname(self.path))

    def write(self, name, age):
        p = os.path.join(os.path.dirname(self.path), name + ".json")
        with open(p, "w") as f:
            json.dump({"pages": [{"strokes": [], "text": name}]}, f)
        t = time.time() - age
        os.utime(p, (t, t))

    def test_names_newest_first_and_hidden_files_left_out(self):
        lib = self.lib()
        self.write("old", 100)
        self.write("new", 1)
        self.write(".current", 0)
        self.assertEqual(lib.names(), ["new", "old"])

    def test_new_name_does_not_take_an_existing_one(self):
        lib = self.lib()
        self.assertEqual(lib.new_name(), "notebook")
        self.write("notebook", 1)
        self.write("notebook 2", 1)
        self.assertEqual(lib.new_name(), "notebook 3")

    def test_delete_moves_to_deleted_and_never_overwrites(self):
        lib = self.lib()
        for _ in range(2):
            self.write("shopping", 1)
            lib.delete("shopping")
        gone = os.path.join(os.path.dirname(self.path), notepad.DELETED_DIR)
        self.assertEqual(sorted(os.listdir(gone)), ["shopping (2).json", "shopping.json"])
        self.assertNotIn("shopping", lib.names())

    def test_current_is_remembered(self):
        lib = self.lib()
        self.write("ideas", 1)
        self.assertIsNone(lib.current())
        lib.remember("ideas")
        self.assertEqual(lib.current(), "ideas")
        lib.remember("missing")
        self.assertIsNone(lib.current())


class Sheet(TmpBook):
    def make(self):
        self.host = Host()
        s = notepad.NotesSheet(self.host, book=self.book())
        s.load_current()
        s.layout((0, 0, 1920, 810))
        self.router = ui.TouchRouter(s)
        return s

    def tap(self, w):
        x, y, ww, hh = w.rect
        self.assertTrue(w.visible and w.enabled, w.name)
        self.router.down(("f", 1), x + ww / 2, y + hh / 2)
        self.router.up(("f", 1), x + ww / 2, y + hh / 2)

    def drag(self, s, x0, y0, x1, y1, n=10):
        cx, cy, _w, _h = s.canvas.rect
        self.router.down(("f", 2), cx + x0, cy + y0)
        for i in range(1, n + 1):
            self.router.move(("f", 2), cx + x0 + (x1 - x0) * i / n, cy + y0 + (y1 - y0) * i / n)
        self.router.up(("f", 2), cx + x1, cy + y1)

    def test_tool_strip_follows_the_mode(self):
        s = self.make()
        self.assertTrue(s.pen_btn.visible and s.width_btn.visible and s.eraser_btn.visible)
        self.assertFalse(s.tcolor_btn.visible or s.tsize_btn.visible)
        self.tap(s.mode_btn)
        self.assertTrue(s.tcolor_btn.visible and s.tsize_btn.visible)
        self.assertFalse(s.pen_btn.visible)
        for w in (s.pen_btn, s.tcolor_btn):                       # strip above the page
            self.assertLessEqual(w.rect[1] + w.rect[3], s.canvas.rect[1])

    def test_pen_width_applies_to_new_strokes(self):
        s = self.make()
        self.tap(s.width_btn)                                     # Medium -> Thick
        self.drag(s, 50, 50, 300, 120)
        self.assertEqual(s.book.page["strokes"][-1]["width"], 14)

    def test_eraser_drag_removes_a_stroke(self):
        s = self.make()
        self.drag(s, 100, 100, 400, 100)
        self.drag(s, 100, 300, 400, 300)
        self.tap(s.eraser_btn)
        self.drag(s, 250, 60, 250, 140)                           # across the first stroke
        self.assertEqual(len(s.book.page["strokes"]), 1)
        with open(self.path) as f:
            self.assertEqual(len(json.load(f)["pages"][0]["strokes"]), 1)

    def test_an_eraser_tap_removes_the_stroke_and_draws_nothing(self):
        s = self.make()
        self.drag(s, 100, 100, 400, 100)
        self.tap(s.eraser_btn)
        cx, cy, _w, _h = s.canvas.rect
        self.router.down(("f", 3), cx + 250, cy + 100)
        self.router.up(("f", 3), cx + 250, cy + 100)
        self.assertEqual(s.book.page["strokes"], [])

    def test_undo_redo_buttons(self):
        s = self.make()
        self.drag(s, 100, 100, 400, 100)
        self.assertFalse(s.redo_btn.enabled)
        self.tap(s.undo_btn)
        self.tap(s.redo_btn)
        self.assertEqual(len(s.book.page["strokes"]), 1)

    def test_text_colour_and_size_cycle_and_save(self):
        s = self.make()
        self.tap(s.mode_btn)
        self.tap(s.tcolor_btn)
        self.tap(s.tsize_btn)
        with open(self.path) as f:
            page = json.load(f)["pages"][0]
        self.assertEqual(page["text_color"], notepad.TEXT_COLORS[1])
        self.assertEqual(page["text_size"], 60)
        self.assertEqual(s.tsize_btn.text, "Large")

    def test_page_and_buttons_draw_with_numeric_colours(self):
        s = self.make()
        s.book.set_text_style(color="#ffd54a", size=60)
        s.book.type("coloured text")
        s.canvas.draw(StrictG())
        for btn in (s.pen_btn, s.tcolor_btn):
            btn.icon(StrictG(), (0, 0, 60, 60), (1, 1, 1, 1))
        s.book.set_text_style(color="")                           # theme colour
        s._refresh()
        s.tcolor_btn.icon(StrictG(), (0, 0, 60, 60), (1, 1, 1, 1))
        s.canvas.draw(StrictG())

    def test_file_panel_new_open_and_the_last_one_reopens(self):
        s = self.make()
        s.book.type("first")
        s.changed()
        self.tap(s.file_btn)
        self.assertTrue(s.file_panel.visible)
        self.tap(s.file_new)
        self.assertEqual(s.current_name(), "notebook 2")
        s.book.type("second")
        s.changed()
        restart = notepad.NotesSheet(Host(), book=notepad.Notebook(self.path))
        restart.load_current()                                    # the new one reopens
        self.assertEqual(restart.current_name(), "notebook 2")
        self.tap(s.file_btn)
        names = [b.text for b in s.file_btns if b.visible]
        self.assertEqual(names, ["> Notebook 2", "Notebook"])
        self.tap(s.file_btns[1])                                  # open "Notebook"
        self.assertEqual(s.book.page["text"], "first")
        s2 = notepad.NotesSheet(Host(), book=notepad.Notebook(self.path.replace("notebook.json",
                                                                                  "other.json")))
        s2.load_current()                                         # a restart
        self.assertEqual(s2.current_name(), "notebook")
        self.assertEqual(s2.book.page["text"], "first")

    def test_delete_needs_two_taps_and_is_recoverable(self):
        s = self.make()
        s.book.type("keep me")
        s.changed()
        self.tap(s.file_btn)
        self.tap(s.file_new)
        self.tap(s.file_btn)
        self.tap(s.file_delete)
        self.assertEqual(s.current_name(), "notebook 2")          # first tap only arms
        self.tap(s.file_delete)
        self.assertEqual(s.current_name(), "notebook")            # the other one opened
        self.assertEqual(s.book.page["text"], "keep me")
        gone = os.path.join(os.path.dirname(self.path), notepad.DELETED_DIR)
        self.assertEqual(os.listdir(gone), ["notebook 2.json"])
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.path), "notebook 2.json")),
                         "the deleted notebook must not be saved back")

    def test_a_running_game_opens_its_own_notebook(self):
        s = self.make()
        s.book.type("everyday")
        s.changed()
        s.open_for_game("nes", "/storage/roms/nes/10-Yard Fight (USA, Europe).nes", "10-Yard Fight")
        self.assertEqual(s.current_name(), "10-Yard Fight (nes)")
        self.assertEqual(s.book.page["text"], "")                 # made on first use
        s.book.type("kick on 3rd down")
        s.changed()
        folder = os.path.dirname(self.path)
        self.assertTrue(os.path.exists(os.path.join(folder, "10-Yard Fight (nes).json")))
        self.assertEqual(s.lib.current(), "notebook")              # not the one open last

    def test_after_the_game_the_usual_notebook_comes_back(self):
        s = self.make()
        s.book.type("everyday")
        s.changed()
        s.open_for_game("snes", "/r/Zelda.sfc", "Zelda")
        s.book.type("dungeon 3 key")
        s.changed()
        s.open_regular()
        self.assertEqual(s.current_name(), "notebook")
        self.assertEqual(s.book.page["text"], "everyday")

    def test_the_next_play_reopens_that_games_notes(self):
        s = self.make()
        s.open_for_game("snes", "/r/Zelda.sfc", "Zelda")
        s.book.type("dungeon 3 key")
        s.changed()
        s.open_regular()
        restart = notepad.NotesSheet(Host(), book=notepad.Notebook(self.path))
        restart.load_current()
        restart.open_for_game("snes", "/r/Zelda.sfc", "Zelda")
        self.assertEqual(restart.book.page["text"], "dungeon 3 key")
        restart.open_for_game("nes", "/r/Other.nes", "Other")       # another game: its own
        self.assertEqual(restart.book.page["text"], "")

    def test_game_notebook_names_are_safe_file_names(self):
        self.assertEqual(notepad.game_notebook_name("snes", "/r/Link?.sfc"), "Link (snes)")
        self.assertEqual(notepad.game_notebook_name("gc", "/r/x.iso", 'A: "B"/C'), "A B C (gc)")


if __name__ == "__main__":
    unittest.main()
