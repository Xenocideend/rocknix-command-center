"""cleanstate_view: the Clean up tile's sheet and controller.

One sheet, four states:
  checking   "Checking what is running..." while cleanstate.Helper.discover() runs on a
             worker (the stop list scan plus es_health.check(), about 5 s, longer when ES's
             process or window is missing)
  confirm    the exact stop list the helper will act on, what ES's health check found, and
             what's never touched, with buttons:
               Clean up                 stop the listed apps (CLEAN_ACTIONS)
               Restart ES / Clean up + restart ES
                                        only when es_health offers a restart (NOT_RUNNING /
                                        NO_WINDOW / FROZEN), and this dialog saying what was
                                        found is the confirmation
               Restart anyway           SUSPECT only (one signal), opens a second confirm
               Cancel
  running    "Working..."
  result     one line per step (OK / failed), Done goes back to the companion

Every process and command action is cleanstate.Helper's, this only drives it. The live
Browser/YouTube session is ended through WebApps.end_session() on the UI thread before the
helper's registry stop, so the strip and the web worker agree with what's running.

discover() and execute() run on `submit` (a worker) and results come back through host.post
with a generation number, so an answer for a sheet you already left gets dropped.
"""
import logging

import cleanstate
import screen_map
import ui
from ui import Button, Label, Sheet

log = logging.getLogger("rp5deck.cleanstate")

CHECKING, CONFIRM, CONFIRM_FORCE, RUNNING, RESULT = (
    "checking", "confirm", "confirm_force", "running", "result")
MAX_LINES = 10         # game + 3 children + touch + notes + ES + 2 ES notes fits


class CleanStateSheet(Sheet):
    """Widgets only, CleanStateController fills them. on_action(name) gets "clean.primary",
    "clean.restart", "clean.cancel".
    """

    def __init__(self, on_action):
        Sheet.__init__(self, "Clean state", on_close=lambda: on_action("clean.cancel"),
                       name="clean")
        self.head = self.body.add(Label("", size=52, bold=True, align="left", name="clean.head"))
        self.lines = [self.body.add(Label("", size=32, align="left", name="clean.line%d" % i))
                      for i in range(MAX_LINES)]
        self.never = self.body.add(Label("", size=30, color="faint", align="left",
                                         name="clean.never"))
        self.primary = self.body.add(Button("Clean up", name="clean.primary", size=44,
                                            on_click=lambda: on_action("clean.primary")))
        self.restart = self.body.add(Button("Restart ES", name="clean.restart", size=44,
                                            on_click=lambda: on_action("clean.restart")))
        self.cancel = self.body.add(Button("Cancel", name="clean.cancel", size=44,
                                           on_click=lambda: on_action("clean.cancel")))

    def show(self, head, lines=(), never="", primary=None, restart=None, cancel="Cancel",
             colors=None):
        """primary / restart / cancel: button text, or None to hide it."""
        self.head.set_text(head)
        lines = list(lines)
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES - 1] + ["... and %d more (see the log)"
                                             % (len(lines) - MAX_LINES + 1)]
        colors = list(colors or [])
        for i, lab in enumerate(self.lines):
            text = lines[i] if i < len(lines) else ""
            col = colors[i] if i < len(colors) and colors[i] else "text"   # bare THEME key
            lab.set_text(text, col)
        self.never.set_text(never)
        for btn, text in ((self.primary, primary), (self.restart, restart),
                          (self.cancel, cancel)):
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
            y += 44
        btn_h = 120
        self.never.set_rect((bx + 60, by + bh - btn_h - 70, bw - 120, 50))
        shown = [b for b in (self.primary, self.restart, self.cancel) if b.visible]
        if shown:
            cells = ui.grid((bx + 60, by + bh - btn_h - 12, bw - 120, btn_h), len(shown), 1, 30)
            for b, c in zip(shown, cells):
                b.set_rect(c)


class CleanStateController:
    """host is main.App (ui, post, web, es_watcher, es_output, close_sheet, close_command_center,
    _activity, state_dirty). submit(fn, *args, done=cb) runs fn on a worker and posts cb(result)
    to the UI thread. helper_factory() -> cleanstate.Helper (tests pass fakes).
    """

    def __init__(self, host, submit, helper_factory=None):
        self.host = host
        self.submit = submit
        self.helper_factory = helper_factory or (
            lambda: cleanstate.Helper(es_output=getattr(host, "es_output", screen_map.CURRENT.top)))
        self.sheet = CleanStateSheet(self.action)
        self.phase = None
        self.gen = 0
        self.plan = None
        self.helper = None
        self.steps = None
        self.error = None
        self.runs = 0

    # -- helpers ------------------------------------------------------------
    def _dirty(self):
        self.host.state_dirty = True
        act = getattr(self.host, "_activity", None)
        if act:
            try:
                act()               # keep the Command Center from auto-closing meanwhile
            except Exception:       # noqa: BLE001
                log.exception("activity")

    def _events_seen(self):
        w = getattr(self.host, "es_watcher", None)
        return getattr(w, "events_seen", None)

    # -- open / check -------------------------------------------------------
    def open(self):
        self.gen += 1
        self.phase = CHECKING
        self.plan = self.steps = self.error = None
        self.sheet.show("Checking what is running...",
                        ["Looking for a game, the Browser / YouTube, and at ES itself.",
                         "This takes about 5 seconds."], cancel="Cancel")
        self.host.ui.open("cleanstate")
        g = self.gen
        self.submit(self._w_discover, g, done=self._discovered)
        self._dirty()

    def _w_discover(self, gen):
        try:
            helper = self.helper_factory()
            post = self.host.post
            plan = helper.discover(events_seen=self._events_seen,
                                   progress=lambda text: post(self._progress, gen, text))
            return gen, helper, plan, None
        except Exception as e:      # noqa: BLE001 - shown, never raised into the worker
            log.exception("clean state: discover failed")
            return gen, None, None, repr(e)

    def _progress(self, gen, text):
        if gen == self.gen and self.phase == CHECKING:
            self.sheet.lines[1].set_text(text)
            self._dirty()

    def _discovered(self, res):
        gen, helper, plan, err = res
        if gen != self.gen or self.phase != CHECKING:
            return
        if err is not None:
            self.error = err
            self._show_result([cleanstate.Step("discover", False, "Could not check: %s" % err)])
            return
        self.helper, self.plan = helper, plan
        self.phase = CONFIRM
        self._show_confirm()

    def _show_confirm(self):
        # Bare THEME keys here, not resolved tuples. show() -> Label.set_text(text, col) keeps whatever
        # colour it gets until show() runs again, which only a new discover/confirm/result triggers,
        # never a theme change (screens.py Bar.set_master() has the same story).
        p = self.plan
        lines, colors = [], []
        for line in p.stop_list():
            lines.append("- " + line)
            colors.append(None)
        for n in p.notes:
            lines.append("! " + n)
            colors.append("warn")
        h = p.health
        restart = None
        if h is not None:
            col = "ok" if h.verdict == "ok" else ("danger" if h.offer_restart else "warn")
            lines.append("ES: " + h.summary)
            colors.append(col)
            if h.offer_restart or h.allow_force:
                for n in h.notes[:2]:
                    lines.append("    " + n)
                    colors.append("dim")
            if h.offer_restart:
                restart = "Clean up + restart ES" if p.items else "Restart ES"
            elif h.allow_force:
                restart = "Restart anyway"
        if p.items:
            head = "Stop these?"
        elif restart:
            head = "Nothing else is running"
        else:
            head = "Nothing to stop"
        self.sheet.show(head, lines, cleanstate.NEVER_TOUCHED,
                        primary="Clean up" if p.items else None, restart=restart,
                        cancel="Cancel" if (p.items or restart) else "Close", colors=colors)
        self._dirty()

    # -- buttons ------------------------------------------------------------
    def action(self, name):
        log.info("clean state: %s (%s)", name, self.phase)
        if name == "clean.cancel":
            if self.phase == CONFIRM_FORCE:
                self.phase = CONFIRM
                self._show_confirm()
                return
            if self.phase == RUNNING:
                return  # cant be interrupted, the result says what happened
            done = self.phase == RESULT
            self.gen += 1
            self.phase = None
            self.host.close_sheet()
            if done:
                close = getattr(self.host, "close_command_center", None)
                if close:
                    close()  # clean state, back to the companion view
            self._dirty()
            return
        if self.plan is None:
            return
        if name == "clean.primary" and self.phase == CONFIRM and self.plan.items:
            self._execute(self.plan.actions(), force=False)
        elif name == "clean.primary" and self.phase == CONFIRM_FORCE:
            self._execute(self.plan.actions() + [cleanstate.RESTART_ES], force=True)
        elif name == "clean.restart" and self.phase == CONFIRM:
            h = self.plan.health
            if h is not None and h.offer_restart:
                self._execute(self.plan.actions() + [cleanstate.RESTART_ES], force=False)
            elif h is not None and h.allow_force:
                self.phase = CONFIRM_FORCE
                self.sheet.show("Restart EmulationStation anyway?",
                                ["Found: " + h.summary] + ["    " + n for n in h.notes[:4]]
                                + ["Only one sign of trouble was found, so this might be",
                                   "ES being busy. Restart only if the top screen is stuck.",
                                   "This restarts ES only (essway), never sway; a game ES "
                                   "started closes too."],
                                cleanstate.NEVER_TOUCHED, primary="Restart ES", restart=None,
                                cancel="Back")
                self._dirty()

    def _execute(self, actions, force):
        self.phase = RUNNING
        self.gen += 1
        self.runs += 1
        web = getattr(self.host, "web", None)
        if cleanstate.STOP_CHILDREN in actions and web is not None and web.app is not None:
            try:
                web.end_session("clean state")      # the UI-side stop (web_tiles)
            except Exception:       # noqa: BLE001
                log.exception("ending the web session")
        self.sheet.show("Working...", ["Stopping: %s" % ", ".join(actions)], cancel=None)
        g = self.gen
        self.submit(self._w_execute, g, list(actions), force, done=self._executed)
        self._dirty()

    def _w_execute(self, gen, actions, force):
        try:
            return gen, self.helper.execute(self.plan, actions, confirmed=True,
                                            force_restart=force), None
        except Exception as e:      # noqa: BLE001
            log.exception("clean state: execute failed")
            return gen, None, repr(e)

    def _executed(self, res):
        gen, steps, err = res
        if gen != self.gen:
            return
        if err is not None:
            steps = [cleanstate.Step("execute", False, "Failed: %s" % err)]
        self._show_result(steps)

    def _show_result(self, steps):
        self.phase = RESULT
        self.steps = steps
        ok = all(s.ok for s in steps)
        lines = [("OK  " if s.ok else "FAILED  ") + s.text for s in steps]
        colors = ["ok" if s.ok else "danger" for s in steps]  # bare THEME keys, see _show_confirm()
        if ok:
            lines.append("ES keeps (or gets back) the controls; this screen goes back "
                         "to the companion.")
            colors.append("dim")
        self.sheet.show("Done" if ok else "Some steps did not work", lines,
                        "Every step is in the rp5deck log and log/cleanstate.log.",
                        cancel="Done", colors=colors)
        self._dirty()

    def state(self):
        return {"phase": self.phase, "runs": self.runs, "error": self.error,
                "plan": self.plan.to_dict() if self.plan else None,
                "steps": [s.to_dict() for s in self.steps] if self.steps else None}
