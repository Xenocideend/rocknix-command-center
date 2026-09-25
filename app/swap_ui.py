"""swap_ui - SW1 widgets: the "Swap screens" tile icon, a confirm sheet, and
the pull tab the Command Center overlay shows on the game/ES screen.

Kept out of screens.py (HF1 owns it this wave) so the screens.py side of SW1
is a few lines: patches/SW1-screens.patch imports this module, adds the tile
and registers the confirm sheet. ConfirmSheet is generic (title, message,
two buttons) so CC1's "Clean state" can reuse it.
"""
import ui
from ui import THEME, BODY_TEXT, Button, Container, Label, Sheet

SWAP_TILE_NAME = "home.swap"
CONFIRM_SHEET = "confirm"


def icon_swap(g, r, color):
    """Two stacked panels with arrows between them (up on the right, down on
    the left): "the two screens trade places"."""
    x, y, w, h = r
    t = max(3, w * 0.07)
    pw, ph = w * 0.62, h * 0.26
    px = x + (w - pw) / 2
    g.stroke_round_rect((px, y + h * 0.02, pw, ph), 4, color, t)
    g.stroke_round_rect((px, y + h * 0.72, pw, ph), 4, color, t)
    lx, rx = x + w * 0.12, x + w * 0.88
    top, bot = y + h * 0.18, y + h * 0.82
    g.line(lx, top, lx, bot, color, t)                          # left: down
    g.line(lx, bot, lx - w * 0.09, bot - h * 0.12, color, t)
    g.line(lx, bot, lx + w * 0.09, bot - h * 0.12, color, t)
    g.line(rx, bot, rx, top, color, t)                          # right: up
    g.line(rx, top, rx - w * 0.09, top + h * 0.12, color, t)
    g.line(rx, top, rx + w * 0.09, top + h * 0.12, color, t)


def icon_chevron_down(g, r, color):
    x, y, w, h = r
    t = max(4, w * 0.14)
    g.line(x + w * 0.15, y + h * 0.3, x + w * 0.5, y + h * 0.7, color, t)
    g.line(x + w * 0.5, y + h * 0.7, x + w * 0.85, y + h * 0.3, color, t)


def swap_tile(h):
    """The Command Center tile. `h` is the handler object (main.App)."""
    return ui.Tile("Swap screens", on_click=getattr(h, "ask_swap_screens", None),
                   name=SWAP_TILE_NAME, icon=icon_swap, subtitle="Game on the other panel")


def swap_prompt(es_screen, docked=True):
    """(title, message, yes label) for the confirm sheet, from the CURRENT
    es_screen value ("addon_top" = not swapped)."""
    if not docked:
        return ("Swap screens", "The add-on screen is not attached, so there is nothing to "
                "swap.", None)
    if es_screen == "builtin_bottom":
        return ("Swap screens back",
                "Games and EmulationStation move back to the add-on screen and the "
                "Command Center returns to the built-in screen. Controls go with the game. "
                "This takes a few seconds.",
                "Swap back")
    return ("Swap screens",
            "Games and EmulationStation move to the built-in screen and the Command "
            "Center moves to the add-on screen. Controls go with the game. "
            "This takes a few seconds.",
            "Swap")


class ConfirmSheet(Sheet):
    """Title, a message, and two big buttons. ask() arms the callbacks; each
    button fires its callback once. A None yes-label shows only the message
    and the No/Close button. The header's Back button is the same as No."""

    def __init__(self, h, name=CONFIRM_SHEET):
        Sheet.__init__(self, "", on_close=self._no, name=name)
        self.msg = self.body.add(Label("", size=BODY_TEXT + 4, name=name + ".msg"))
        self.yes = self.body.add(Button("Yes", name=name + ".yes", size=44,
                                        on_click=self._yes))
        self.no = self.body.add(Button("Cancel", name=name + ".no", size=44,
                                       on_click=self._no))
        self._on_yes = self._on_no = None
        self.asked = 0

    def ask(self, title, message, yes_label, on_yes, on_no, no_label="Cancel"):
        self.asked += 1
        self.title.set_text(title)
        self.msg.set_text(message)
        self._on_yes, self._on_no = on_yes, on_no
        self.yes.set_visible(bool(yes_label))
        if yes_label:
            self.yes.text = yes_label
            self.yes.invalidate()
        self.no.text = no_label if yes_label else "Close"
        self.no.invalidate()

    def _yes(self):
        cb, self._on_yes, self._on_no = self._on_yes, None, None
        if cb:
            cb()

    def _no(self):
        cb, self._on_yes, self._on_no = self._on_no, None, None
        if cb:
            cb()

    def layout(self, rect):
        Sheet.layout(self, rect)
        bx, by, bw, bh = self.body.rect
        self.msg.set_rect((bx + 80, by + 40, bw - 160, 200))
        bw2 = 460
        gap = 60
        y = by + max(260, int(bh * 0.45))
        self.yes.set_rect((bx + bw // 2 - gap // 2 - bw2, y, bw2, 150))
        self.no.set_rect((bx + bw // 2 + gap // 2, y, bw2, 150))

    def draw(self, g):
        Sheet.draw(self, g)
        # the message may be long: draw it wrapped instead of the Label's one line
        if self.msg.visible and self.msg.text:
            x, y, w, h = self.msg.rect
            g.fill_rect(self.msg.rect, THEME["bg"])
            g.text_block(self.msg.text, (x, y, w, h), BODY_TEXT + 4, THEME["text"], False, 4)


class PullTab(Container):
    """The Command Center overlay's closed state on the game/ES screen: one
    button, the whole surface (the overlay surface is only this big while
    closed, so nothing else of the game screen is covered)."""

    def __init__(self, on_open, name="overlay.tab"):
        Container.__init__(self, name=name + ".box", bg="bar")
        self.button = self.add(Button("Menu", name=name, size=36, icon=icon_chevron_down,
                                      on_click=on_open, radius=18))

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        self.button.set_rect((x + 6, y + 6, max(0, w - 12), max(0, h - 12)))
