"""hotkeys_view - CC7: the Hotkey cheat sheet's UI.

HotkeysSheet is a screens.Sheet (Back + title, same shape as HudSheet /
MixerSheet in screens.py) that this module owns; main.py/screens.py only get
a small patch (patches/CC7-*.patch) to build one and hand it to
screens.CommandCenter as an extra sheet, the same way settings_view's
SettingsSheet is threaded through today.

Touch-first per DESIGN.md and the CC7 brief: body text 36-44 px, paging
(Prev/Next) instead of scrolling - same widgets and the same Prev/Next
pattern as screens.MixerSheet/YouTubeSheet, so no new interaction idiom is
introduced. Rows are not tappable (there is nothing to tap them INTO - this
is a reference sheet, not a settings editor), so MIN_TARGET only binds the
Back/Prev/Next controls, which screens.Sheet/this module already size at
>=120 px tall.

Paging model: every (context, page-within-context) pair the context's combo
list needs becomes one PAGE in a single flat sequence; Prev/Next walk that
sequence like a book, and the header shows "melonDS (NDS)  2/3" (the
per-context page) plus set_data()'s caller controls which context comes
first (hotkeys.ordered_context_ids - context-aware ordering when a game is
running)."""
import ui
from ui import Button, Label, Sheet

import hotkeys

ROW_H = 128
MAX_ROWS = 5


class HotkeysSheet(Sheet):
    def __init__(self, h):
        Sheet.__init__(self, "Hotkeys", on_close=h.close_sheet, name="hotkeys")
        self.prev = self.add(Button("Prev", on_click=lambda: self.turn(-1), name="hotkeys.prev",
                                    size=40))
        self.next = self.add(Button("Next", on_click=lambda: self.turn(1), name="hotkeys.next",
                                    size=40))
        self.msg = self.body.add(Label("", size=44, bold=True, align="center",
                                       name="hotkeys.message"))
        self.rows = []               # [(combo Label, action Label)]
        self.contexts = {}
        self.order = []
        self.pages = []              # [(Context, [Combo,...], page_index, page_count)]
        self.page = 0
        self.set_data(*hotkeys.load())

    # -- data --------------------------------------------------------------
    def set_data(self, contexts, order):
        """contexts: id -> hotkeys.Context. order: id sequence, already
        reordered for context-awareness by the caller (main.py passes
        hotkeys.load(running_system) straight through)."""
        self.contexts = contexts
        self.order = list(order)
        self._build_pages()
        self.page = 0
        self._apply()

    def _build_pages(self):
        self.pages = []
        for cid in self.order:
            ctx = self.contexts.get(cid)
            if ctx is None or not ctx.combos:
                continue
            combos = ctx.combos
            n = max(1, (len(combos) + MAX_ROWS - 1) // MAX_ROWS)
            for i in range(n):
                self.pages.append((ctx, combos[i * MAX_ROWS:(i + 1) * MAX_ROWS], i, n))

    # -- paging --------------------------------------------------------------
    def turn(self, d):
        new = self.page + d
        if 0 <= new < len(self.pages):
            self.page = new
            self._apply()

    def _clear_rows(self):
        for combo_lbl, action_lbl in self.rows:
            self.body.remove(combo_lbl)
            self.body.remove(action_lbl)
        self.rows = []

    def _apply(self):
        self._clear_rows()
        if not self.pages:
            self.title.set_text("Hotkeys")
            self.msg.set_text("No hotkeys could be sourced (see the report's "
                              "unconfirmed list).")
            self.prev.set_visible(False)
            self.next.set_visible(False)
            if self.rect[2]:
                self.layout(self.rect)
            return
        ctx, combos, i, n = self.pages[self.page]
        self.title.set_text(ctx.title if n == 1 else "%s  (%d/%d)" % (ctx.title, i + 1, n))
        self.msg.set_text("")
        for c in combos:
            tag = "device" if c.verified_on_device else "source"
            action_text = c.action
            if c.note:
                action_text += "  -  " + c.note
            action_text += "   · " + tag
            combo_lbl = self.body.add(Label(hotkeys.format_combo(c), size=40, bold=True))
            action_lbl = self.body.add(Label(action_text, size=36, color="dim"))
            self.rows.append((combo_lbl, action_lbl))
        total = len(self.pages)
        self.prev.set_visible(total > 1)
        self.next.set_visible(total > 1)
        self.prev.set_enabled(self.page > 0)
        self.next.set_enabled(self.page < total - 1)
        if self.rect[2]:
            self.layout(self.rect)

    # -- layout --------------------------------------------------------------
    def layout(self, rect):
        Sheet.layout(self, rect)
        x, y, w, h = rect
        self.title.set_rect((x + 320, y, max(0, w - 980), self.HEADER_H))
        self.prev.set_rect((x + w - 640, y + 10, 300, self.HEADER_H - 20))
        self.next.set_rect((x + w - 320, y + 10, 300, self.HEADER_H - 20))
        bx, by, bw, bh = self.body.rect
        self.msg.set_rect((bx, by + bh * 0.4, bw, 80))
        for idx, (combo_lbl, action_lbl) in enumerate(self.rows):
            ry = by + 16 + idx * ROW_H
            combo_lbl.set_rect((bx + 40, ry, bw - 80, 52))
            action_lbl.set_rect((bx + 40, ry + 56, bw - 80, ROW_H - 64))
