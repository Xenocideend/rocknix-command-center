"""notepad: the Command Center's Notes tab, a notebook of pages that take finger drawing and
typed text.

  Draw   a finger drag draws a stroke, and the tool strip picks the pen colour, the width
         (thin / medium / thick) or the eraser (removes strokes it touches)
  Type   rp5deck's own keyboard types into the page's text, and the tool strip picks the
         text colour and size
  Undo / Redo   the last stroke, and Clear needs two taps within CLEAR_ARM_S
  Prev / Next / New   page through the notebook
  File   notebooks: open another, start a new one, or delete one (two taps, it moves to
         notes/.deleted/ and is never erased). Every notebook saves itself and the one open
         last opens again (notes/.current).
While a game runs, Notes opens that game's own notebook, and it reopens next time you play.

Strokes are stored relative to the canvas (0..1) so a page survives any layout change. The
notebook saves atomically to NOTES_PATH after every stroke and text edit (it's small, a stroke
is capped at MAX_POINTS points and a page at MAX_STROKES strokes). The model (Notebook) has no
UI and no clock so it gets tested offline, NotesSheet is the widget.
"""
import json
import logging
import math
import os
import re

import ui
from ui import THEME, Button, Container, Keyboard, Label, Sheet, Widget

DEFAULT_NOTES_PATH = "/storage/rp5deck/notes/notebook.json"   # RP5DECK_NOTES overrides
PEN_COLORS = ("#ffffff", "#ffd54a", "#4ad7ff", "#ff5a5a", "#6be675")
PEN_WIDTHS = (("Thin", 3), ("Medium", 6), ("Thick", 14))
PEN_WIDTH = 6
ERASER_PX = 24  # the eraser removes strokes passing this close to the finger
TEXT_COLORS = ("", "#ffd54a", "#4ad7ff", "#ff5a5a", "#6be675")   # "" = the theme's text
TEXT_SIZES = (("Small", 28), ("Medium", 40), ("Large", 60))
TEXT_SIZE = 40
CURRENT_FILE = ".current"   # notes/.current: the notebook open last
DELETED_DIR = ".deleted"  # deleted notebooks go here and can be recovered
MIN_STEP = 3.0  # px between recorded points, keeps strokes small
MAX_POINTS = 4000
MAX_STROKES = 2000
MAX_TEXT = 20000
CLEAR_ARM_S = 3.0
TOOL_STRIP_H = 84
FILE_ROWS = 8               # notebooks per File panel page (two columns)
log = logging.getLogger("rp5deck.notepad")


def _page():
    return {"strokes": [], "text": "", "text_color": "", "text_size": TEXT_SIZE}


class Notebook:
    """Pages of strokes and text saved as JSON. No UI, no clock."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("RP5DECK_NOTES", DEFAULT_NOTES_PATH)
        self.pages = [_page()]
        self.index = 0
        self.load_note = ""
        self.redo_stack = []        # strokes undone on this page (memory only)

    # -- persistence -----------------------------------------------------------
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self.load_note = "new notebook"
            return False
        except (OSError, ValueError) as e:
            # never overwrite something we couldnt read, keep it aside
            self.load_note = "unreadable notebook kept as .bad (%s)" % e
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            return False
        pages = []
        for p in (data.get("pages") if isinstance(data, dict) else None) or []:
            if not isinstance(p, dict):
                continue
            strokes = [s for s in p.get("strokes") or [] if self._valid_stroke(s)][:MAX_STROKES]
            text = p.get("text") if isinstance(p.get("text"), str) else ""
            tcol = p.get("text_color") if p.get("text_color") in TEXT_COLORS else ""
            tsize = p.get("text_size") if p.get("text_size") in dict(TEXT_SIZES).values() else TEXT_SIZE
            pages.append({"strokes": strokes, "text": text[:MAX_TEXT],
                          "text_color": tcol, "text_size": tsize})
        self.pages = pages or [_page()]
        idx = data.get("index", 0) if isinstance(data, dict) else 0
        self.index = idx if isinstance(idx, int) and 0 <= idx < len(self.pages) else 0
        self.load_note = "loaded %d page(s)" % len(self.pages)
        return True

    @staticmethod
    def _valid_stroke(s):
        if not isinstance(s, dict) or not isinstance(s.get("points"), list):
            return False
        pts = s["points"]
        return all(isinstance(p, list) and len(p) == 2 and
                   all(isinstance(v, (int, float)) and 0.0 <= v <= 1.0 for v in p) for p in pts)

    def save(self):
        d = os.path.dirname(self.path)
        try:
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "index": self.index, "pages": self.pages}, f)
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            log.warning("notes: save failed (%s)", e)
            return False

    # -- pages -----------------------------------------------------------------
    @property
    def page(self):
        return self.pages[self.index]

    def go(self, delta):
        new = max(0, min(len(self.pages) - 1, self.index + delta))
        changed = new != self.index
        self.index = new
        if changed:
            self.redo_stack = []
        return changed

    def new_page(self):
        self.pages.insert(self.index + 1, _page())
        self.index += 1
        self.redo_stack = []

    # -- strokes ---------------------------------------------------------------
    def begin_stroke(self, x, y, color=PEN_COLORS[0], width=PEN_WIDTH):
        if len(self.page["strokes"]) >= MAX_STROKES:
            return False
        self.page["strokes"].append({"color": color, "width": width,
                                     "points": [[_clamp(x), _clamp(y)]]})
        self.redo_stack = []
        return True

    def add_point(self, x, y):
        st = self.page["strokes"]
        if not st or len(st[-1]["points"]) >= MAX_POINTS:
            return False
        st[-1]["points"].append([_clamp(x), _clamp(y)])
        return True

    def undo(self):
        if self.page["strokes"]:
            self.redo_stack.append(self.page["strokes"].pop())
            return True
        return False

    def redo(self):
        if self.redo_stack and len(self.page["strokes"]) < MAX_STROKES:
            self.page["strokes"].append(self.redo_stack.pop())
            return True
        return False

    def erase_near(self, x, y, rx, ry):
        """Removes every stroke passing within (rx, ry) of (x, y), in canvas units so the eraser is round
        on screen. Returns how many went.
        """
        keep, gone = [], 0
        for st in self.page["strokes"]:
            if any(((px - x) / rx) ** 2 + ((py - y) / ry) ** 2 <= 1.0 for px, py in st["points"]):
                gone += 1
            else:
                keep.append(st)
        if gone:
            self.page["strokes"] = keep
            self.redo_stack = []
        return gone

    def clear(self):
        had = bool(self.page["strokes"] or self.page["text"])
        keep = {k: self.page.get(k) for k in ("text_color", "text_size")}
        self.pages[self.index] = _page()
        self.pages[self.index].update(keep)
        self.redo_stack = []
        return had

    def set_text_style(self, color=None, size=None):
        if color is not None and color in TEXT_COLORS:
            self.page["text_color"] = color
        if size is not None and size in dict(TEXT_SIZES).values():
            self.page["text_size"] = size

    # -- text ------------------------------------------------------------------
    def type(self, s):
        t = self.page["text"]
        if len(t) + len(s) > MAX_TEXT:
            return False
        self.page["text"] = t + s
        return True

    def backspace(self):
        if self.page["text"]:
            self.page["text"] = self.page["text"][:-1]
            return True
        return False


def _swatch(hexcol):
    """A button icon: a dot in the chosen colour ("" = the theme's text colour)."""
    def icon(g, r, _fg):
        x, y, w, h = r
        col = _color(hexcol) if hexcol else THEME["text"]
        g.circle(x + w / 2.0, y + h / 2.0, min(w, h) * 0.38, col)
    return icon


_NOT_IN_FILENAMES = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def game_notebook_name(system, rom_path, name=None):
    """The notebook for one game: its name (else the ROM file's) and its system."""
    title = (name or os.path.splitext(os.path.basename(rom_path or ""))[0] or "game").strip()
    return " ".join(_NOT_IN_FILENAMES.sub(" ", "%s (%s)" % (title, system or "game")).split())[:120]


class NotesLibrary:
    """The notebooks, every *.json in the notes folder. .current names the one open last, and
    deleting moves a notebook into .deleted/ where it can be recovered.
    """

    def __init__(self, folder):
        self.folder = folder

    def names(self):
        try:
            files = [f for f in os.listdir(self.folder)
                     if f.endswith(".json") and not f.startswith(".")]
        except OSError:
            return []
        files.sort(key=lambda f: os.path.getmtime(os.path.join(self.folder, f)), reverse=True)
        return [f[:-5] for f in files]

    @staticmethod
    def title(name):
        return name[:1].upper() + name[1:]

    def path(self, name):
        return os.path.join(self.folder, name + ".json")

    def current(self):
        try:
            with open(os.path.join(self.folder, CURRENT_FILE), encoding="utf-8") as f:
                name = f.read().strip()
            return name if name and os.path.exists(self.path(name)) else None
        except OSError:
            return None

    def remember(self, name):
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(os.path.join(self.folder, CURRENT_FILE), "w", encoding="utf-8") as f:
                f.write(name)
        except OSError as e:
            log.warning("notes: could not remember %s (%s)", name, e)

    def new_name(self):
        taken = set(n.lower() for n in self.names())
        n = 1
        while True:
            name = "notebook" if n == 1 else "notebook %d" % n
            if name not in taken:
                return name
            n += 1

    def delete(self, name):
        src = self.path(name)
        dst_dir = os.path.join(self.folder, DELETED_DIR)
        try:
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, name + ".json")
            k = 2
            while os.path.exists(dst):
                dst = os.path.join(dst_dir, "%s (%d).json" % (name, k))
                k += 1
            os.replace(src, dst)
            return dst
        except OSError as e:
            log.warning("notes: delete %s failed (%s)", name, e)
            return None


def _color(c):
    """A saved stroke colour ("#rrggbb", portable JSON) -> the (r, g, b) tuple gfx draws with.
    Passing the hex string straight to gfx raised TypeError in set_source_rgba and crashed
    rp5deck on the first stroke.
    """
    try:
        if isinstance(c, str) and len(c) == 7 and c[0] == "#":
            return ui.rgb(int(c[1:], 16))
    except ValueError:
        pass
    return ui.rgb(0xFFFFFF)


def _clamp(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else float(v)


def icon_notes(g, r, color):
    """The Notes tab: a page with lines and a pen stroke."""
    x, y, w, h = r
    t = max(3, w * 0.07)
    g.stroke_round_rect((x + w * 0.16, y + h * 0.08, w * 0.62, h * 0.84), w * 0.06, color, t)
    for k in (0.32, 0.5, 0.68):
        g.line(x + w * 0.28, y + h * k, x + w * 0.62, y + h * k, color, t * 0.7)
    g.line(x + w * 0.58, y + h * 0.9, x + w * 0.92, y + h * 0.38, color, t * 1.2)


class NotesCanvas(Widget):
    """The page: draws strokes and text, and in Draw mode a finger drag adds a stroke."""
    interactive = True

    @property
    def owns_drag(self):
        """while drawing no swipe gesture can take the finger (ui.TouchRouter)"""
        return self.sheet.mode == "draw"

    def __init__(self, sheet, name="notes.canvas"):
        Widget.__init__(self, name=name)
        self.sheet = sheet
        self.pid = None
        self.last = None

    def _norm(self, x, y):
        cx, cy, cw, ch = self.rect
        return ((x - cx) / float(max(1, cw)), (y - cy) / float(max(1, ch)))

    def on_press(self, pid, x, y):
        if self.sheet.mode != "draw" or self.pid is not None:
            return
        nx, ny = self._norm(x, y)
        if self.sheet.eraser:
            self.pid, self.last = pid, (x, y)
            self._erase(nx, ny)
            return
        if self.sheet.book.begin_stroke(nx, ny, self.sheet.pen_color(), self.sheet.pen_width()):
            self.pid, self.last = pid, (x, y)
            self.invalidate()

    def on_move(self, pid, x, y):
        if pid != self.pid:
            return
        lx, ly = self.last
        if math.hypot(x - lx, y - ly) < MIN_STEP:
            return
        nx, ny = self._norm(x, y)
        if self.sheet.eraser:
            self.last = (x, y)
            self._erase(nx, ny)
            return
        if self.sheet.book.add_point(nx, ny):
            self.last = (x, y)
            self.invalidate()

    def _erase(self, nx, ny):
        _cx, _cy, cw, ch = self.rect
        if self.sheet.book.erase_near(nx, ny, ERASER_PX / float(max(1, cw)),
                                      ERASER_PX / float(max(1, ch))):
            self.invalidate()

    def on_release(self, pid, x, y):
        if pid != self.pid:
            return
        self.on_move(pid, x, y)
        self.pid = self.last = None
        self.sheet.changed()

    def on_cancel(self, pid):
        if pid == self.pid:  # a gesture took the finger, keep what was drawn
            self.pid = self.last = None
            self.sheet.changed()

    def draw(self, g):
        x, y, w, h = self.rect
        g.round_rect(self.rect, 16, THEME["tile"])
        page = self.sheet.book.page
        if page["text"]:
            size = page.get("text_size") or TEXT_SIZE
            col = _color(page["text_color"]) if page.get("text_color") else THEME["text"]
            g.text_block(page["text"], (x + 24, y + 16, w - 48, h - 32), size, col,
                         max_lines=max(1, int((h - 32) // int(size * 1.3))))
        for s in page["strokes"]:
            pts = s["points"]
            wd = s.get("width", PEN_WIDTH)
            col = _color(s.get("color", PEN_COLORS[0]))
            if len(pts) == 1:
                px, py = pts[0]
                g.circle(x + px * w, y + py * h, wd / 2.0, col)
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                g.line(x + ax * w, y + ay * h, x + bx * w, y + by * h, col, wd)
        if not page["text"] and not page["strokes"]:
            hint = ("Draw with your finger" if self.sheet.mode == "draw"
                    else "Type with the keyboard below")
            g.text(hint, (x, y + h / 2 - 30, w, 60), 36, THEME["dim"], False, "center")


class NotesSheet(Sheet):
    """The Notes sheet: toolbar, canvas, and the keyboard in Type mode."""

    def __init__(self, h, book=None, name="notes"):
        Sheet.__init__(self, "Notes", on_close=h.close_sheet, name=name)
        self.h = h
        self.book = book or Notebook()
        self.home_path = None               # the notebook used when none is remembered
        self.game = None                    # the game notebook open for a running game
        self.mode = "draw"
        self.pen = 0
        self.width_i = 1            # Medium
        self.eraser = False
        self.delete_armed = False
        self.clear_armed = False
        self._clear_timer = None
        # the tools sit in the header row beside Back (the title is hidden), so the page keeps its height
        # under the tab strip even with the keyboard up
        b = self
        self.file_btn = b.add(Button("File", name="notes.file", size=36, on_click=self.toggle_file))
        self.mode_btn = b.add(Button("Type", name="notes.mode", size=36, on_click=self.toggle_mode))
        self.undo_btn = b.add(Button("Undo", name="notes.undo", size=36, on_click=self.undo))
        self.redo_btn = b.add(Button("Redo", name="notes.redo", size=36, on_click=self.redo))
        self.clear_btn = b.add(Button("Clear", name="notes.clear", size=36, on_click=self.clear))
        self.prev_btn = b.add(Button("Prev", name="notes.prev", size=36,
                                     on_click=lambda: self.go(-1)))
        self.page_lbl = b.add(Label("", size=36, align="center", name="notes.page"))
        self.next_btn = b.add(Button("Next", name="notes.next", size=36,
                                     on_click=lambda: self.go(1)))
        self.new_btn = b.add(Button("New page", name="notes.new", size=36,
                                    on_click=self.new_page))
        # the tool strip above the page: Draw tools or Type tools, by mode
        self.pen_btn = self.body.add(Button("Pen", name="notes.pen", size=34, on_click=self.next_pen))
        self.width_btn = self.body.add(Button("Medium", name="notes.width", size=34,
                                              on_click=self.next_width))
        self.eraser_btn = self.body.add(Button("Eraser", name="notes.eraser", size=34,
                                               on_click=self.toggle_eraser))
        self.tcolor_btn = self.body.add(Button("Text colour", name="notes.text_color", size=34,
                                               on_click=self.next_text_color))
        self.tsize_btn = self.body.add(Button("Medium", name="notes.text_size", size=34,
                                              on_click=self.next_text_size))
        self.book_lbl = self.body.add(Label("", size=30, color="dim", align="right",
                                            name="notes.book"))
        self.canvas = self.body.add(NotesCanvas(self))
        self.kb = self.body.add(Keyboard(on_text=self.type, on_backspace=self.backspace,
                                 on_enter=lambda: self.type("\n"), enter_label="New line",
                                 name="notes.kb"))
        self.kb.set_visible(False)
        # the File panel over the page: notebooks to open, New, Delete (two taps)
        self.file_panel = self.body.add(Container(name="notes.files", bg="bg"))
        self.file_title = self.file_panel.add(Label("Notebooks", size=40, bold=True))
        self.file_btns = [self.file_panel.add(Button("", name="notes.open.%d" % i, size=36,
                                                     on_click=lambda i=i: self.open_slot(i)))
                          for i in range(FILE_ROWS)]
        self.file_offset = 0
        self.file_prev = self.file_panel.add(Button("Prev", name="notes.files.prev", size=34,
                                                    on_click=lambda: self.file_page(-1)))
        self.file_next = self.file_panel.add(Button("Next", name="notes.files.next", size=34,
                                                    on_click=lambda: self.file_page(1)))
        self.file_new = self.file_panel.add(Button("New notebook", name="notes.files.new", size=34,
                                                   on_click=self.new_notebook))
        self.file_delete = self.file_panel.add(Button("Delete this notebook", name="notes.files.delete",
                                                      size=34, on_click=self.delete_notebook))
        self.file_close = self.file_panel.add(Button("Close", name="notes.files.close", size=34,
                                                     on_click=self.toggle_file))
        self.file_panel.set_visible(False)
        self._refresh()

    # -- notebooks ---------------------------------------------------------------
    @property
    def lib(self):
        """The notebooks in the folder of the one open now."""
        return NotesLibrary(os.path.dirname(self.book.path) or ".")

    def current_name(self):
        return os.path.basename(self.book.path)[:-5] if self.book.path.endswith(".json") else "notebook"

    def load_current(self):
        """The notebook open last (notes/.current), else this sheet's default one."""
        if self.home_path is None:
            self.home_path = self.book.path
        name = self.lib.current()
        path = self.lib.path(name) if name else self.home_path
        if path != self.book.path:
            self.book = Notebook(path)
        self.book.load()
        self.lib.remember(self.current_name())
        self._refresh()
        return self.book.load_note

    def open_for_game(self, system, rom_path, name=None):
        """The running game's own notebook, made on first use. It isnt saved as the one open last, so
        the usual notebook comes back once no game runs.
        """
        gname = game_notebook_name(system, rom_path, name)
        if self.game == gname and self.current_name() == gname:
            return
        self._switch(gname, new=not os.path.exists(self.lib.path(gname)), remember=False)
        self.game = gname

    def open_regular(self):
        """Back from a game's notebook to the one open last outside a game."""
        if self.game is None:
            return
        self.game = None
        self.book.save()
        self.load_current()
        self.canvas.invalidate()

    def _switch(self, name, new=False, save_open=True, remember=True):
        if save_open:
            self.book.save()
        self.book = Notebook(self.lib.path(name))
        if new:
            self.book.save()
        else:
            self.book.load()
        self.game = None
        if remember:
            self.lib.remember(name)
        self.file_panel.set_visible(False)
        self.delete_armed = False
        self._refresh()
        self.canvas.invalidate()
        if self.rect[2]:
            self.layout(self.rect)

    def toggle_file(self):
        show = not self.file_panel.visible
        if show:
            self.book.save()
            self.file_offset = 0
        self.delete_armed = False
        self.file_panel.set_visible(show)
        self._refresh()
        if self.rect[2]:
            self.layout(self.rect)

    def file_page(self, d):
        n = len(self.lib.names())
        self.file_offset = max(0, min(max(0, n - FILE_ROWS), self.file_offset + d * FILE_ROWS))
        self._refresh()

    def open_slot(self, i):
        names = self.lib.names()
        k = self.file_offset + i
        if k < len(names):
            self._switch(names[k])

    def new_notebook(self):
        self._switch(self.lib.new_name(), new=True)

    def delete_notebook(self):
        """Two taps. The notebook moves to notes/.deleted/ (recoverable) and the newest other notebook
        opens, or a fresh one if it was the last.
        """
        if not self.delete_armed:
            self.delete_armed = True
            self._refresh()
            return
        gone = self.current_name()
        self.lib.delete(gone)
        rest = [n for n in self.lib.names() if n != gone]
        if rest:
            self._switch(rest[0], save_open=False)
        else:
            self._switch(self.lib.new_name(), new=True, save_open=False)

    # -- actions -------------------------------------------------------------
    def pen_color(self):
        return PEN_COLORS[self.pen % len(PEN_COLORS)]

    def pen_width(self):
        return PEN_WIDTHS[self.width_i % len(PEN_WIDTHS)][1]

    def next_width(self):
        self.width_i = (self.width_i + 1) % len(PEN_WIDTHS)
        self.eraser = False
        self._refresh()

    def toggle_eraser(self):
        self.eraser = not self.eraser
        self._refresh()

    def redo(self):
        if self.book.redo():
            self.changed()

    def next_text_color(self):
        cur = self.book.page.get("text_color", "")
        i = TEXT_COLORS.index(cur) if cur in TEXT_COLORS else 0
        self.book.set_text_style(color=TEXT_COLORS[(i + 1) % len(TEXT_COLORS)])
        self.changed()

    def next_text_size(self):
        sizes = [v for _n, v in TEXT_SIZES]
        cur = self.book.page.get("text_size", TEXT_SIZE)
        i = sizes.index(cur) if cur in sizes else 1
        self.book.set_text_style(size=sizes[(i + 1) % len(sizes)])
        self.changed()

    def toggle_mode(self):
        self.mode = "type" if self.mode == "draw" else "draw"
        self.kb.set_visible(self.mode == "type")
        self._refresh()
        if self.rect[2]:
            self.layout(self.rect)

    def undo(self):
        if self.book.undo():
            self.changed()

    def clear(self):
        if not self.clear_armed:
            self.clear_armed = True
            self._refresh()
            call_later = getattr(self.h, "call_later", None)
            if call_later is not None:
                self._clear_timer = call_later(CLEAR_ARM_S, self._disarm)
            return
        self._disarm()
        if self.book.clear():
            self.changed()

    def _disarm(self):
        self.clear_armed = False
        cancel = getattr(self.h, "cancel", None)
        if self._clear_timer is not None and cancel is not None:
            cancel(self._clear_timer)
        self._clear_timer = None
        self._refresh()

    def next_pen(self):
        self.pen = (self.pen + 1) % len(PEN_COLORS)
        self.eraser = False
        self._refresh()

    def go(self, delta):
        if self.book.go(delta):
            self.changed()

    def new_page(self):
        self.book.new_page()
        self.changed()

    def type(self, s):
        if self.book.type(s):
            self.changed()

    def backspace(self):
        if self.book.backspace():
            self.changed()

    def changed(self):
        self.book.save()
        self._refresh()
        self.canvas.invalidate()

    def _refresh(self):
        self.mode_btn.text = "Draw" if self.mode == "type" else "Type"
        self.clear_btn.text = "Tap again" if self.clear_armed else "Clear"
        self.pen_btn.text = "Pen %d" % (self.pen + 1)
        self.pen_btn.icon = _swatch(self.pen_color())
        self.width_btn.text = PEN_WIDTHS[self.width_i % len(PEN_WIDTHS)][0]
        self.eraser_btn.text = "Eraser: on" if self.eraser else "Eraser"
        tcol = self.book.page.get("text_color", "")
        self.tcolor_btn.text = "Text colour"
        self.tcolor_btn.icon = _swatch(tcol)
        self.tsize_btn.text = dict((v, n) for n, v in TEXT_SIZES).get(
            self.book.page.get("text_size", TEXT_SIZE), "Medium")
        self.book_lbl.set_text(NotesLibrary.title(self.current_name()))
        self.redo_btn.set_enabled(bool(self.book.redo_stack))
        drawing = self.mode == "draw"
        for w in (self.pen_btn, self.width_btn, self.eraser_btn):
            w.set_visible(drawing)
        for w in (self.tcolor_btn, self.tsize_btn):
            w.set_visible(not drawing)
        names = self.lib.names()
        for i, btn in enumerate(self.file_btns):
            k = self.file_offset + i
            btn.set_visible(k < len(names))
            if k < len(names):
                title = NotesLibrary.title(names[k])
                btn.text = ("> " + title) if names[k] == self.current_name() else title
                btn.invalidate()
        self.file_prev.set_enabled(self.file_offset > 0)
        self.file_next.set_enabled(self.file_offset + FILE_ROWS < len(names))
        self.file_delete.text = "Tap again to delete" if self.delete_armed else "Delete this notebook"
        for w in (self.width_btn, self.eraser_btn, self.tcolor_btn, self.tsize_btn,
                  self.file_delete, self.redo_btn):
            w.invalidate()
        self.page_lbl.set_text("Page %d of %d" % (self.book.index + 1, len(self.book.pages)))
        self.prev_btn.set_enabled(self.book.index > 0)
        self.next_btn.set_enabled(self.book.index < len(self.book.pages) - 1)
        self.undo_btn.set_enabled(bool(self.book.page["strokes"]))
        for w in (self.mode_btn, self.clear_btn, self.pen_btn):
            w.invalidate()

    # -- layout ----------------------------------------------------------------
    def layout(self, rect):
        Sheet.layout(self, rect)
        x, y, w, _h = rect
        self.title.set_visible(False)
        tools = ui.hsplit((x + 320, y + 10, w - 340, self.HEADER_H - 20),
                          [140, 150, 140, 140, 180, 130, None, 130, 190], gap=10)
        for wdg, r in zip((self.file_btn, self.mode_btn, self.undo_btn, self.redo_btn,
                           self.clear_btn, self.prev_btn, self.page_lbl, self.next_btn,
                           self.new_btn), tools):
            wdg.set_rect(r)
        bx, by, bw, bh = self.body.rect
        strip = (bx + 20, by + 12, bw - 40, TOOL_STRIP_H)
        cells = ui.hsplit(strip, [240, 240, 240, None], gap=16)
        draw = (self.pen_btn, self.width_btn, self.eraser_btn)
        typ = (self.tcolor_btn, self.tsize_btn)
        for wdg, r in zip(draw, cells):
            wdg.set_rect(r)
        for wdg, r in zip(typ, cells):
            wdg.set_rect(r)
        self.book_lbl.set_rect(cells[3])
        top = by + 12 + TOOL_STRIP_H + 12
        kb_h = min(360, max(0, bh - 300)) if self.mode == "type" else 0
        self.canvas.set_rect((bx + 20, top, bw - 40, max(120, by + bh - top - kb_h - 20)))
        if kb_h:
            self.kb.layout((bx, by + bh - kb_h, bw, kb_h))
        if self.file_panel.visible:
            px, py, pw, ph = bx + 20, top, bw - 40, max(200, by + bh - top - 20)
            self.file_panel.set_rect((px, py, pw, ph))
            self.file_title.set_rect((px + 20, py + 10, pw - 40, 60))
            row_h = max(56, min(90, (ph - 60 - 110) // (FILE_ROWS + 1)))
            cols = ui.hsplit((px + 20, py + 80, pw - 40, row_h * ((FILE_ROWS + 1) // 2)),
                             [None, None], gap=16)
            for i, btn in enumerate(self.file_btns):
                cx, cy, cw, _ch = cols[i % 2]
                btn.set_rect((cx, cy + (i // 2) * row_h, cw, row_h - 10))
            foot = ui.hsplit((px + 20, py + ph - 100, pw - 40, 88),
                             [150, 150, None, 360, 180], gap=16)
            for wdg, r in zip((self.file_prev, self.file_next, None, self.file_delete,
                               self.file_close), foot):
                if wdg is not None:
                    wdg.set_rect(r)
            self.file_new.set_rect(foot[2])
