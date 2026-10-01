"""dualscreen_keys_view: the "dual-screen settings missing" warning and its one tap Restore.

Shaped like cleanstate_view.py, a small Sheet with CONFIRM / RUNNING / RESULT phases driven by
dualscreen_keys.check()/restore() on a worker, so the file read and the systemctl/chksysconfig
calls never block the UI thread. It isnt a tile. screens.Home shows a banner above the tile
grid (Home.set_dualscreen_notice) whenever a check finds a missing key, with a Restore button
that opens this sheet, so the tile grid never has to know about it.

poll() is the read-only half, run once at start and on a timer (main.DS_POLL_S). It only
updates the banner and never writes system.cfg, a background check only warns. open()/action()
are the write half and only run from your own tap on Restore, behind the sheet's confirm.
"""
import logging

import ui
from ui import Button, Label, Sheet

import dualscreen_keys

log = logging.getLogger("rp5deck.dualscreen_keys")

CONFIRM, RUNNING, RESULT = "confirm", "running", "result"


class DualScreenKeysSheet(Sheet):
    """Widgets only, DualScreenKeysController fills them. on_action(name) gets "ds.primary" or
    "ds.cancel".
    """

    MAX_LINES = 4

    def __init__(self, on_action):
        Sheet.__init__(self, "Dual-screen settings", on_close=lambda: on_action("ds.cancel"),
                       name="ds")
        self.head = self.body.add(Label("", size=48, bold=True, align="left", name="ds.head"))
        self.lines = [self.body.add(Label("", size=32, align="left", name="ds.line%d" % i))
                      for i in range(self.MAX_LINES)]
        self.primary = self.body.add(Button("Restore", name="ds.primary", size=44,
                                            on_click=lambda: on_action("ds.primary")))
        self.cancel = self.body.add(Button("Cancel", name="ds.cancel", size=44,
                                           on_click=lambda: on_action("ds.cancel")))

    def show(self, head, lines=(), primary=None, cancel="Cancel", colors=None):
        """primary / cancel: button text, or None to hide it."""
        self.head.set_text(head)
        colors = list(colors or [])
        for i, lab in enumerate(self.lines):
            text = lines[i] if i < len(lines) else ""
            col = colors[i] if i < len(colors) and colors[i] else "text"  # bare THEME key, a resolved tuple would never follow a theme change
            lab.set_text(text, col)
        for btn, text in ((self.primary, primary), (self.cancel, cancel)):
            if text:
                btn.text = text
                btn.invalidate()
            btn.set_visible(bool(text))
        if self.rect[2]:
            self.layout(self.rect)

    def layout(self, rect):
        Sheet.layout(self, rect)
        bx, by, bw, bh = self.body.rect
        self.head.set_rect((bx + 60, by + 16, bw - 120, 72))
        y = by + 96
        for lab in self.lines:
            lab.set_rect((bx + 60, y, bw - 120, 44))
            y += 50
        btn_h = 120
        shown = [b for b in (self.primary, self.cancel) if b.visible]
        if shown:
            cells = ui.grid((bx + 60, by + bh - btn_h - 12, bw - 120, btn_h), len(shown), 1, 30)
            for b, c in zip(shown, cells):
                b.set_rect(c)


class DualScreenKeysController:
    """host is main.App (ui, post, close_sheet, state_dirty). submit(fn, *args, done=cb) runs fn on a
    worker and posts cb(result) to the UI thread, since check()/restore() do real file and
    systemctl I/O. checker/restorer default to dualscreen_keys.check/restore (tests pass fakes,
    like cleanstate_view's helper_factory).
    """

    def __init__(self, host, submit, checker=None, restorer=None):
        self.host = host
        self.submit = submit
        self.checker = checker or dualscreen_keys.check
        self.restorer = restorer or dualscreen_keys.restore
        self.sheet = DualScreenKeysSheet(self.action)
        self.phase = None
        self.gen = 0
        self.missing = []          # the last successful check's result
        self.result = None
        self.check_inflight = False

    # -- read-only: app start + the periodic poll ----------------------------
    def poll(self):
        """Never called from a tap. A background check only warns, so this only calls
        dualscreen_keys.check() (read only) and updates the Home banner, never opens the sheet and never
        writes anything.
        """
        if self.check_inflight:
            return
        self.check_inflight = True
        self.submit(self.checker, done=self._checked)

    def _checked(self, res):
        self.check_inflight = False
        home = getattr(self.host.ui, "home", None)
        available = bool(res.get("available"))
        missing = list(res.get("missing") or []) if available else []
        self.missing = missing
        if home is None:
            return
        if not available:
            # cant check is never shown as missing, a corrupt or absent file is chksysconfig's problem to
            # notice at the next boot, not a sign that keys are gone
            home.set_dualscreen_notice("")
        elif missing:
            home.set_dualscreen_notice("Dual-screen settings missing: %s" % ", ".join(missing))
        else:
            home.set_dualscreen_notice("")
        self.host.state_dirty = True

    # -- the sheet: only from the banner's own Restore tap -------------------
    def open(self):
        if not self.missing:
            return
        self.gen += 1
        self.phase = CONFIRM
        self.result = None
        self._show_confirm()
        self.host.ui.open("dualscreen")
        self.host.state_dirty = True

    def _show_confirm(self):
        lines = ["Missing: " + ", ".join(self.missing),
                "EmulationStation will restart for a few seconds.",
                "Refused while a game is running."]
        self.sheet.show("Restore dual-screen settings?", lines, primary="Restore", cancel="Cancel")

    def action(self, name):
        log.info("dual-screen keys: %s (%s)", name, self.phase)
        if name == "ds.cancel":
            if self.phase == RUNNING:
                return  # cant be interrupted, the result says what happened
            self.gen += 1
            self.phase = None
            self.host.close_sheet()
            self.host.state_dirty = True
            return
        if name == "ds.primary" and self.phase == CONFIRM:
            self._run()

    def _run(self):
        self.phase = RUNNING
        self.gen += 1
        g = self.gen
        self.sheet.show("Working...", ["Stopping EmulationStation..."], cancel=None)
        self.submit(self.restorer, list(self.missing), done=lambda res: self._done(g, res))
        self.host.state_dirty = True

    def _done(self, gen, res):
        if gen != self.gen:
            return
        self.result = res
        self.phase = RESULT
        ok = bool(res.get("ok"))
        self.sheet.show("Done" if ok else "Could not restore", [res.get("detail", "")],
                        primary=None, cancel="Done")
        self.host.state_dirty = True
        if ok:
            self.poll()  # check again, a fixed file should clear the banner by itself

    def state(self):
        return {"phase": self.phase, "missing": list(self.missing), "result": self.result}
