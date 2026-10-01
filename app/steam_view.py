"""steam_view: the Steam tab, a grid of the installed games with their cover art.

Tapping a card asks the running Steam client to start that game (steam_library.launch) and closes the
Command Center so the game shows. The tab is only offered while Steam is open (app_tabs), so a launch
never starts a second client. The library and the pictures are read on workers and posted back, a
page of covers is decoded once and kept until the library changes.

Cards are named steam.card0 .. steam.card11, the page buttons steam.prev and steam.next.
"""
import logging
import time

import media
import steam_library
import ui
from ui import THEME, Button, Label, Sheet

log = logging.getLogger("rp5deck.steam")

COLS, ROWS = 4, 3
PER_PAGE = COLS * ROWS
GAP = 20
NAME_H = 54
CHECK_DESC = "Steam is not open"
SORT_LABELS = {"recent": "Recent", "az": "A-Z", "played": "Most played"}
EXIT_LABEL, EXIT_ARMED_LABEL, EXIT_ARM_S = "Exit Steam", "Tap again to exit", 4.0


class GameCard(Button):
    """One game: its picture with the name and a short line over a dark strip. The running game gets an
    outline."""

    def __init__(self, on_click, name):
        Button.__init__(self, "", on_click=on_click, name=name, radius=18)
        self.game = None
        self.image = None
        self.playing = False

    def set_game(self, game, image, playing):
        self.game, self.image, self.playing = game, image, playing
        self.set_visible(game is not None)
        self.invalidate()

    def draw(self, g):
        if self.game is None:
            return
        x, y, w, h = self.rect
        bg, fg = self.colors()
        g.round_rect(self.rect, self.radius, bg)
        if self.image is not None:
            g.image(self.image, x + (w - self.image.w) / 2.0, y + (h - NAME_H - self.image.h) / 2.0)
        strip = (x, y + h - NAME_H, w, NAME_H)
        g.round_rect(strip, self.radius, THEME["panel"])
        g.fill_rect((x, y + h - NAME_H, w, 14), THEME["panel"])
        line = self.game.get("line") or ""
        name_w = w - 28
        g.text(self.game["name"], (x + 14, y + h - NAME_H, name_w, NAME_H * 0.6), 26, fg, True)
        if line:
            g.text(line, (x + 14, y + h - NAME_H * 0.42, name_w, NAME_H * 0.4), 18, THEME["dim"])
        if self.playing:
            g.stroke_round_rect(self.rect, self.radius, THEME["accent"], 6)
            g.round_rect((x + w - 150, y + 10, 140, 44), 22, THEME["panel"])
            g.text("Playing", (x + w - 150, y + 10, 140, 44), 26, THEME["ok"], True, "center")


class SteamLibrarySheet(Sheet):
    """Widgets only, SteamLibraryController fills them."""

    def __init__(self, on_action, on_close):
        Sheet.__init__(self, "Steam library", on_close=on_close, name="steam")
        self.cards = [self.body.add(GameCard((lambda i=i: on_action("steam.card%d" % i)), "steam.card%d" % i))
                      for i in range(PER_PAGE)]
        self.sort = self.add(Button("Recent", name="steam.sort", size=40, on_click=lambda: on_action("steam.sort")))
        self.exit = self.add(Button(EXIT_LABEL, name="steam.exit", size=40, on_click=lambda: on_action("steam.exit")))
        self.prev = self.add(Button("<", name="steam.prev", size=52, on_click=lambda: on_action("steam.prev")))
        self.next = self.add(Button(">", name="steam.next", size=52, on_click=lambda: on_action("steam.next")))
        self.note = self.body.add(Label("", size=40, color="dim", align="center", name="steam.note"))

    def layout(self, rect):
        Sheet.layout(self, rect)
        x, y, w, h = rect
        self.sort.set_rect((x + 320, y + 20, 260, self.HEADER_H - 40))
        self.exit.set_rect((x + 600, y + 20, 360, self.HEADER_H - 40))
        self.title.set_rect((x + 980, y, w - 980 - 340, self.HEADER_H))
        self.prev.set_rect((x + w - 320, y + 20, 140, self.HEADER_H - 40))
        self.next.set_rect((x + w - 160, y + 20, 140, self.HEADER_H - 40))
        bx, by, bw, bh = self.body.rect
        cw = (bw - GAP * (COLS + 1)) / float(COLS)
        ch = (bh - GAP * (ROWS + 1)) / float(ROWS)
        for i, c in enumerate(self.cards):
            r, col = divmod(i, COLS)
            c.set_rect((bx + GAP + col * (cw + GAP), by + GAP + r * (ch + GAP), cw, ch))
        self.note.set_rect((bx, by, bw, bh))

    def card_size(self):
        r = self.cards[0].rect
        return int(r[2]), int(r[3] - NAME_H)


class SteamLibraryController:
    """host is main.App: host.pull (closed after a launch), host.ui.open(), host.state_dirty. submit_io and
    submit_media run fn on a worker and post done(result) back to the UI thread."""

    def __init__(self, host, submit_io, submit_media, steam=steam_library, load_image=media.load_image,
                 root=None, now=time.time):
        self.host = host
        self.submit_io, self.submit_media = submit_io, submit_media
        self.steam, self.load_image, self.root, self.now = steam, load_image, root, now
        self.sheet = SteamLibrarySheet(self.action, self.close)
        self.library = []               # as read, sorted for show in self.games
        self.games = []
        self.sort_mode = steam_library.SORTS[0]
        self.page = 0
        self.gen = 0
        self.images = {}                # appid -> Image, for the games already decoded at this size
        self.size = None
        self.running = None
        self.message = ""
        self.exit_timer = None

    # -- opening ---------------------------------------------------------------------------
    def open(self):
        self.gen += 1
        self.page = 0
        self.library, self.games, self.message = [], [], "Loading your library..."
        self._fill()
        self.host.ui.open("steam")
        gen = self.gen
        self.submit_io(self._read_library, done=lambda res: self._library(gen, res))

    def close(self):
        self.gen += 1
        self._disarm_exit()
        self.host.close_sheet()

    def _root(self):
        return self.root or self.steam.default_root()

    def _read_library(self):
        root = self._root()
        return self.steam.library(root, self.now()), self.steam.running_appid()

    def _library(self, gen, res):
        if gen != self.gen:
            return
        self.library, self.running = res
        self.games = self.steam.sort_games(self.library, self.sort_mode)
        self.message = "" if self.games else "No installed games found"
        self._fill()

    # -- the page --------------------------------------------------------------------------
    def pages(self):
        return max(1, -(-len(self.games) // PER_PAGE))

    def _page_games(self):
        return self.games[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]

    def _fill(self):
        size = self.sheet.card_size()
        if size != self.size:
            self.size, self.images = size, {}
        shown = self._page_games()
        for i, c in enumerate(self.sheet.cards):
            g = shown[i] if i < len(shown) else None
            c.set_game(g, self.images.get(g["appid"]) if g else None, bool(g and g["appid"] == self.running))
        self.sheet.title.set_text("Steam library" if self.pages() == 1
                                  else "Steam library  %d/%d" % (self.page + 1, self.pages()))
        self.sheet.sort.text = SORT_LABELS[self.sort_mode]
        self.sheet.sort.invalidate()
        self.sheet.prev.set_enabled(self.page > 0)
        self.sheet.next.set_enabled(self.page < self.pages() - 1)
        self.sheet.note.set_text(self.message)
        self.sheet.invalidate()
        missing = [g for g in shown if g["appid"] not in self.images and g["card"]]
        if missing and size[0] > 0:
            gen = self.gen
            self.submit_media(self._decode, missing, size, done=lambda res: self._decoded(gen, size, res))
        self.host.state_dirty = True

    def _decode(self, games, size):
        out = {}
        for g in games:
            try:
                out[g["appid"]] = self.load_image(g["card"], size[0], size[1], media.FILL)
            except Exception:           # noqa: BLE001 - a bad picture leaves the name card
                log.warning("steam: cannot decode %s", g["card"])
                out[g["appid"]] = None  # remembered, so the page is not decoded again and again
        return out

    def _decoded(self, gen, size, images):
        if gen != self.gen or size != self.size:
            return
        self.images.update(images)
        self._fill()

    # -- taps ------------------------------------------------------------------------------
    def action(self, name):
        if name == "steam.prev" and self.page > 0:
            self.page -= 1
            self._fill()
        elif name == "steam.next" and self.page < self.pages() - 1:
            self.page += 1
            self._fill()
        elif name == "steam.sort":
            modes = steam_library.SORTS
            self.sort_mode = modes[(modes.index(self.sort_mode) + 1) % len(modes)]
            log.info("steam: sort %s", self.sort_mode)
            self.games = self.steam.sort_games(self.library, self.sort_mode)
            self.page = 0
            self._fill()
        elif name == "steam.exit":
            self._exit_tapped()
        elif name.startswith("steam.card"):
            shown = self._page_games()
            i = int(name[len("steam.card"):])
            if i < len(shown):
                self.launch(shown[i])

    def _exit_tapped(self):
        """Closing Steam takes two taps, the second within a few seconds, so a stray touch does not end a game."""
        if self.exit_timer is None:
            self.sheet.exit.text = EXIT_ARMED_LABEL
            self.sheet.exit.invalidate()
            self.exit_timer = self.host.call_later(EXIT_ARM_S, self._disarm_exit)
            return
        self._disarm_exit()
        log.info("steam: exit Steam")
        self.host.exit_steam_now()

    def _disarm_exit(self):
        if self.exit_timer is not None:
            self.host.cancel(self.exit_timer)
            self.exit_timer = None
        self.sheet.exit.text = EXIT_LABEL
        self.sheet.exit.invalidate()

    def launch(self, game):
        log.info("steam: launch %s (%d)", game["name"], game["appid"])
        self.submit_io(self.steam.launch, game["appid"], done=lambda ok: self._launched(game, ok))

    def _launched(self, game, ok):
        if not ok:
            self.message = CHECK_DESC
            self._fill()
            return
        pull = getattr(self.host, "pull", None)
        if pull is not None and pull.is_open():
            pull.close("steam launch")
        self.host.state_dirty = True
