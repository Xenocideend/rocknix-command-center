"""app_tabs - CC6: the app tabs. A strip in the Command Center (under the
volume strip) that switches what the bottom screen shows among what is
running or can start:

    Companion | <DS screen / 3DS screen / GamePad> | HUD | Browser | Discord | YouTube App

  * Companion and HUD are rp5deck's own views: no sway command, unless a
    window has to get out of the way first.
  * Browser / Discord (the same Firefox, rp5deck-web), the YouTube App
    (its own Firefox instance, rp5deck-ytapp - web_tiles.YtAppSession) and
    an emulator's second window are real WINDOWS: they are hidden ("parked")
    and shown by window_switcher.py, the one module that moves windows,
    with its own allow-list. sway_ipc.py stays read-only.
  * A tab exists only for what exists or can start: an emulator tab only
    while its second window exists (shown or parked); Browser / Discord /
    the YouTube App while Firefox runs or its binary is there. Companion and
    HUD always.
  * The ACTIVE tab is derived, never stored: a recognised window shown on
    the Command Center's screen wins (the tree says so); otherwise the open
    sheet (HUD) or, with the Command Center closed, Companion. So the strip
    cannot drift from what is really on screen.

Mode consistency (sway_ipc.compute_mode is unchanged): parking the window
that owns the screen makes it FULL (rp5deck shows Companion / HUD), showing
an emulator window makes it HIDDEN again, showing Firefox makes it BAR. At
most one kind of recognised window is shown at a time (window_switcher.plan).
What a tab does after the switch (open the HUD, start Firefox, ...) runs
once the mode the switch promised has arrived, or after SETTLE_S at the
latest - so CC5's overlay closing on FULL cannot undo it.

Over an emulator's second window (CC5 OVERLAY) the strip is there too: the
emulator's tab is the active one and tapping it closes the overlay (the
game gets its touch screen back); any other tab parks the emulator window
(the game keeps running and keeps focus; see window_switcher's doc for the
stall risk of a hidden emulator window).

In BAR mode (Firefox fills the screen above a 140 px strip) the strip
cannot fit, so the BAR strip gets one Tabs button (screens.icon_tabs, in
patches/CC6-screens.patch): it parks the window and opens the Command
Center, whose tab strip then shows that app as running. YT3: the YouTube
App's own tv strip replaces this with dedicated Home/Close buttons instead
(screens.Bar.tv_home/tv_close, main.App.on_tv_home/on_tv_close) - the owner
never recognised the bare Tabs icon.

When sway cannot be read, nothing is moved and a tab / tile does what it
did before CC6 (open the sheet, start Firefox); a refusal (an unknown
window owns the screen, undocked, ...) does nothing and says why in the
volume strip.

YT3: the old mpv-based "YouTube" quick-search tile/tab (HF1's "one of
Firefox / mpv at a time" rule) is gone - Browser/Discord/the YouTube App all
share one Firefox-family rule instead (opening any one of them ends
whichever other is running; a parked one stays running, web_tiles' `parked`,
patches/CC6-web_tiles.patch).
"""
import logging
import os
import time

import browser
import screens
import summon
import ui
import window_switcher as wsw
from ui import THEME

log = logging.getLogger("rp5deck.tabs")

PULL = summon.PullDownStateMachine
FULL, BAR, HIDDEN, OVERLAY = "FULL", "BAR", "HIDDEN", "OVERLAY"

TABS_H = 130                # the strip's height in the Command Center (MIN_TARGET is 130)
TAB_MAX_W = 300
SETTLE_S = 3.0              # run a tab's follow-up at the latest this long after its switch
REFRESH_S = 2.0             # re-read sway's tree this often while the strip is on screen
INTENT_S = 30.0             # how long a choice defends itself against a rule moving a window back

# YT3: the old mpv-based "YouTube" quick-search tab (YOUTUBE = "youtube")
# is gone along with the tile/sheet/BAR strip that were its only callers -
# never a tab id any tree can produce any more (wsw.YT never matches a real
# window: nothing launches rp5deck-yt now). YOUTUBE_TV ("ytapp", the
# leanback/sign-in tile) is unrelated and unaffected.
COMPANION, HUD, BROWSER, DISCORD, YOUTUBE_TV, CC = (
    "companion", "hud", "browser", "discord", "ytapp", "cc")
EMU_PREFIX = "emu:"
DISCORD_LABEL = "Discord"                        # web_tiles.Web.label when app == "web"
YOUTUBE_TV_LABEL = browser.YOUTUBE_TV_LABEL       # ditto - one source of truth, browser.py
LABELS = {COMPANION: "Companion", HUD: "HUD", BROWSER: "Browser", DISCORD: DISCORD_LABEL,
          YOUTUBE_TV: YOUTUBE_TV_LABEL}


# ---------------------------------------------------------------------------
# Icons (g, rect, color)
# ---------------------------------------------------------------------------
def icon_companion(g, r, color):
    """A picture frame (the companion shows the game's art)."""
    x, y, w, h = r
    t = max(4, w * 0.08)
    g.stroke_round_rect((x + t, y + h * 0.15, w - 2 * t, h * 0.7), 6, color, t)
    g.polygon([(x + w * 0.2, y + h * 0.75), (x + w * 0.45, y + h * 0.45),
               (x + w * 0.62, y + h * 0.62), (x + w * 0.72, y + h * 0.52),
               (x + w * 0.85, y + h * 0.75)], color)


def icon_dual(g, r, color):
    """Two stacked screens (an emulator's second window)."""
    x, y, w, h = r
    t = max(4, w * 0.07)
    g.stroke_round_rect((x + w * 0.15, y + h * 0.04, w * 0.7, h * 0.4), 5, color, t)
    g.round_rect((x + w * 0.15, y + h * 0.54, w * 0.7, h * 0.42), 5, color)


def icon_tv(g, r, color):
    """W2: a TV screen (leanback) - a wide rounded rect with a little stand."""
    x, y, w, h = r
    t = max(4, w * 0.07)
    g.stroke_round_rect((x + w * 0.1, y + h * 0.08, w * 0.8, h * 0.58), 6, color, t)
    g.fill_rect((x + w * 0.42, y + h * 0.7, w * 0.16, h * 0.14), color)
    g.fill_rect((x + w * 0.28, y + h * 0.86, w * 0.44, h * 0.08), color)


ICONS = {COMPANION: icon_companion, HUD: screens.icon_gauge, BROWSER: screens.icon_globe,
         DISCORD: screens.icon_chat, YOUTUBE_TV: icon_tv}


# ---------------------------------------------------------------------------
# The tab model (pure)
# ---------------------------------------------------------------------------
def availability():
    """What can START (checked when the strip is refreshed; cheap stats).
    YT3: the old mpv-based "YouTube" tab's "yt" key is gone with it - nothing
    reads it any more."""
    import browser
    return {"web": os.access(browser.FIREFOX_BIN, os.X_OK)}


def _wins(snap, kind, where=None):
    if not snap:
        return []
    return [w for w in snap.get("windows") or []
            if w["kind"] == kind and (where is None or w["where"] == where)]


def build_tabs(snap, web, avail, view):
    """The strip's tabs, left to right.

    snap   window_switcher.Snapshot.to_dict() of a recent tree, or None
    web    {"app": None | "web", "label": "Browser" | "Discord" | None}
    avail  {"web": bool}: can Firefox start
    view   {"mode": ..., "cc_open": bool, "sheet": name or None}

    -> [{"id", "label", "running", "active"}]; "running" (the green dot)
    means an app or window exists behind the tab - never set for rp5deck's
    own views."""
    web = web or {}
    avail = avail or {}
    view = view or {}
    tabs = [{"id": COMPANION, "label": LABELS[COMPANION], "running": False}]
    for w in _wins(snap, wsw.EMU):
        if w["where"] in (wsw.SHOWN, wsw.PARKED):
            tabs.append({"id": EMU_PREFIX + str(w["con_id"]), "label": w["label"],
                         "running": True, "family": w["family"]})
    tabs.append({"id": HUD, "label": LABELS[HUD], "running": False})
    web_running = web.get("app") == wsw.WEB or bool(_wins(snap, wsw.WEB))
    if web_running or avail.get("web"):
        label = web.get("label") if web.get("app") == wsw.WEB else None
        tabs.append({"id": BROWSER, "label": LABELS[BROWSER],
                     "running": web_running and label != DISCORD_LABEL})
        tabs.append({"id": DISCORD, "label": LABELS[DISCORD],
                     "running": web_running and label == DISCORD_LABEL})
    # W2b: its own window kind/Firefox instance (browser.TV_APP_ID), not a
    # "web" label any more - a global user-agent override cannot share the
    # Browser/Discord profile without leaking into it (see browser.py).
    ytapp_wins = _wins(snap, wsw.YTAPP)
    if ytapp_wins or avail.get("web"):
        tabs.append({"id": YOUTUBE_TV, "label": LABELS[YOUTUBE_TV], "running": bool(ytapp_wins)})
    act = active_tab(snap, web, view)
    for t in tabs:
        t["active"] = t["id"] == act
    return tabs


def active_tab(snap, web, view):
    """What the bottom screen shows right now, as a tab id (or None)."""
    web = web or {}
    view = view or {}
    shown = [w for w in (snap or {}).get("windows") or [] if w["where"] == wsw.SHOWN]
    for w in shown:
        if w["kind"] == wsw.EMU:
            return EMU_PREFIX + str(w["con_id"])
        if w["kind"] == wsw.YTAPP:
            return YOUTUBE_TV
    kinds = set(w["kind"] for w in shown)
    if snap is None and view.get("mode") == BAR and web.get("app"):
        kinds = {web["app"]}                # sway unreadable: BAR means our app is up
    if wsw.WEB in kinds:
        return DISCORD if web.get("label") == DISCORD_LABEL else BROWSER
    if view.get("mode") in (HIDDEN,) and not snap:
        return None
    if not view.get("cc_open"):
        return COMPANION
    sheet = view.get("sheet")
    if sheet == "hud":
        return HUD
    return None                             # the Command Center's home / another sheet


def route(tab_id, snap, web):
    """(window_switcher target, follow-up name) for a tapped tab."""
    web = web or {}
    if tab_id.startswith(EMU_PREFIX):
        return "emu:" + tab_id[len(EMU_PREFIX):], "emu"
    if tab_id in (COMPANION, HUD, CC):
        return wsw.INTERNAL, tab_id
    if tab_id in (BROWSER, DISCORD):
        return wsw.WEB, tab_id
    if tab_id == YOUTUBE_TV:
        return wsw.YTAPP, tab_id
    raise ValueError("unknown tab %r" % tab_id)


# ---------------------------------------------------------------------------
# The strip (widgets)
# ---------------------------------------------------------------------------
class TabButton(ui.Button):
    """One tab: a small icon and the label (the icon goes when the tab is
    too narrow for both), accent fill when active, a green dot when an app
    or window behind it is running but not on screen."""
    TEXT = 32
    ICON = 48
    ICON_MIN_W = 280

    def __init__(self, tab, on_click):
        ui.Button.__init__(self, tab["label"], on_click=on_click, name="tabs." + tab["id"],
                           size=self.TEXT, icon=ICONS.get(tab["id"], icon_dual), radius=20)
        self.tab = tab

    def colors(self):
        if self.enabled and self.tab.get("active") and not self.pressed:
            return THEME["accent"], THEME["text"]
        return ui.Button.colors(self)

    def draw(self, g):
        bg, fg = self.colors()
        g.round_rect(self.rect, self.radius, bg)
        x, y, w, h = self.rect
        if w >= self.ICON_MIN_W:
            isz = self.ICON
            self.icon(g, (x + 16, y + (h - isz) / 2.0, isz, isz), fg)
            g.text(self.text, (x + 16 + isz + 12, y, w - isz - 40, h), self.size, fg, True)
        else:
            g.text(self.text, (x + 8, y, w - 16, h), self.size, fg, True, "center")
        if self.tab.get("running") and not self.tab.get("active"):
            g.circle(x + w - 18, y + 18, 8, THEME["ok"])


class TabStrip(ui.Container):
    """The tabs in one row. set_tabs() rebuilds the buttons only when the
    tabs changed, so a periodic refresh does not redraw an unchanged strip.

    on_open (W): screens.CommandCenter.toggle_bar_tabs / close_bar_tabs call
    this (if set) right after flipping bar_tabs_open, so the ONE shared strip
    picks up TabsController.bar_tabs()'s extra Home pill before it is laid
    out inline in the BAR strip's own row - and drops it again once closed."""

    GAP = 16
    HEIGHT = TABS_H             # screens.CommandCenter (patched) lays the strip out this tall

    def __init__(self, on_tab, on_open=None):
        ui.Container.__init__(self, name="tabs", bg="panel")
        self.on_tab = on_tab
        self.on_open = on_open
        self.tabs = []
        self.buttons = []

    def set_tabs(self, tabs):
        key = [(t["id"], t["label"], bool(t.get("running")), bool(t.get("active")))
               for t in tabs]
        if key == [(t["id"], t["label"], bool(t.get("running")), bool(t.get("active")))
                   for t in self.tabs]:
            return False
        self.tabs = [dict(t) for t in tabs]
        for b in self.buttons:
            self.remove(b)
        self.buttons = [self.add(TabButton(t, (lambda i=t["id"]: self.on_tab(i))))
                        for t in self.tabs]
        if self.rect[2]:
            self.layout(self.rect)
        self.invalidate()
        return True

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        n = len(self.buttons)
        if not n:
            return
        inner_w = w - 40
        bw = min(TAB_MAX_W, (inner_w - self.GAP * (n - 1)) / float(n))
        for i, b in enumerate(self.buttons):
            b.set_rect((x + 20 + i * (bw + self.GAP), y + 5, bw, h - 10))

    def draw(self, g):
        ui.Container.draw(self, g)
        x, y, w, h = self.rect
        g.fill_rect((x, y + h - 2, w, 2), THEME["line"])


# ---------------------------------------------------------------------------
# The controller (UI thread; the switcher runs on a worker)
# ---------------------------------------------------------------------------
class TabsController:
    """Owns the tabs for one main.App (the panel; cc_overlay's game-screen
    Command Center has none). main.App calls:

      select(tab_id)                 a tab, or the tiles that route through it
      run(target, then, why)         a switch followed by `then` (e.g. play)
      after_mode(old, new, reason)   end of on_mode
      after_pull(old, new, reason)   end of on_pull
      state()

    submit(fn, *args, done=) runs fn on a worker and posts done(result) to
    the UI thread (main.App's io worker). switcher: window_switcher.Switcher
    (tests: one bound to a simulated sway). avail() -> {"web", "yt"}."""

    def __init__(self, app, submit=None, switcher=None, avail=None, clock=time.monotonic):
        self.app = app
        self.submit = submit or (lambda fn, *a, done=None: app.io_worker.submit(fn, *a,
                                                                                done=done))
        self.switcher = switcher or wsw.Switcher()
        self.avail_fn = avail or availability
        self.clock = clock
        self.strip = TabStrip(self.select, on_open=self.rebuild)
        self.snap = None                # last Snapshot.to_dict()
        self.avail = {"web": False, "yt": False}
        self.busy = False
        self.queued = None              # (target, then, why) while a switch runs
        self.pending = None             # {"expect", "then", "timer", "target"}
        self.intent = None              # {"target", "until", "budget"}
        self.scan_inflight = False
        self.scan_again = False
        self.refresh_timer = None
        self.switches = self.refusals = self.errors = self.scans = 0
        self.last = None                # the last switch result, trimmed
        self.events = []
        self._refresh_avail()
        self.rebuild()

    # -- state -----------------------------------------------------------------
    def _web(self):
        web = getattr(self.app, "web", None)
        if web is None:
            return {}
        return {"app": web.app, "label": web.label, "phase": web.phase}

    def _view(self):
        app = self.app
        pull = getattr(app, "pull", None)
        return {"mode": app.mode, "cc_open": bool(pull is not None and pull.is_open()),
                "sheet": app.ui.sheet if app.ui is not None else None}

    def tabs(self):
        return build_tabs(self.snap, self._web(), self.avail, self._view())

    def bar_tabs(self):
        """W: the tab list shown by the BAR strip's inline picker (screens.
        CommandCenter.toggle_bar_tabs) - the normal tabs plus a Home pill
        (routed by `route()` like the old "cc" target from show_app_tabs:
        park whatever is shown and open the Command Center) so Settings /
        Mixer stay reachable without a button that would itself be hidden
        once the picker replaces the bar's own row."""
        return self.tabs() + [{"id": CC, "label": "Home", "running": False, "active": False}]

    def _bar_tabs_open(self):
        return bool(getattr(getattr(self.app.ui, "cc", None), "bar_tabs_open", False))

    def rebuild(self):
        tabs = self.bar_tabs() if self._bar_tabs_open() else self.tabs()
        if self.strip.set_tabs(tabs):
            self.app.state_dirty = True

    def _refresh_avail(self):
        try:
            self.avail = dict(self.avail_fn())
        except Exception:               # noqa: BLE001 - a missing module means "cannot start"
            log.exception("tabs: availability check failed")
            self.avail = {"web": False, "yt": False}

    def _note(self, what, detail):
        self.events = (self.events + [[what, detail, round(time.time(), 3)]])[-12:]
        self.app.state_dirty = True

    # -- reading the tree (read-only) --------------------------------------------
    def strip_visible(self):
        app = self.app
        return (app.mode in (FULL, OVERLAY) and app.pull is not None and app.pull.is_open()
                and app.ui is not None and not app.ui.compact)

    def refresh(self):
        if self.scan_inflight:
            self.scan_again = True
            return
        self.scan_inflight = True
        self.submit(self._w_scan, self.app.output, self.app.es_output, done=self._scanned)

    def _w_scan(self, cc, es):
        try:
            snap = self.switcher.snapshot(cc, es)
        except Exception:               # noqa: BLE001
            log.exception("tabs: snapshot")
            snap = None
        return snap.to_dict() if snap is not None else None

    def _scanned(self, snap):
        self.scan_inflight = False
        self.scans += 1
        if snap is not None:
            self.snap = snap
        self._refresh_avail()
        self.rebuild()
        self._resolve_conflict()
        if self.scan_again:
            self.scan_again = False
            self.refresh()

    def _arm_refresh(self):
        if self.refresh_timer is None and self.strip_visible():
            self.refresh_timer = self.app.call_later(REFRESH_S, self._tick)

    def _tick(self):
        self.refresh_timer = None
        if self.strip_visible():
            self.refresh()
            self._arm_refresh()

    def _resolve_conflict(self):
        """092 (screens swapped) moves EVERY matching window to the Command
        Center's screen whenever Firefox / mpv / a second window maps, which
        can bring a window this choice parked back onto the screen. Such a
        window (by con_id - a NEW window is never touched) is parked again,
        once, for INTENT_S after the choice."""
        it = self.intent
        if it is None or self.busy or not self.snap or not it.get("parked"):
            return
        if self.clock() > it["until"] or it["budget"] <= 0:
            self.intent = None
            return
        back = [w for w in self.snap.get("windows") or []
                if w["where"] == wsw.SHOWN and w["con_id"] in it["parked"]]
        if back:
            it["budget"] -= 1
            log.info("tabs: %s came back after %s; parking it again",
                     [w["con_id"] for w in back], it["target"])
            self._note("conflict", [w["con_id"] for w in back])
            self._start(it["target"], None, "conflict")

    # -- selecting -----------------------------------------------------------------
    def select(self, tab_id):
        """A tap on a tab (and the tiles routed here). Maps the tab to a
        switch target and what to do once the screen is clear."""
        snap, web = self.snap, self._web()
        target, follow = route(tab_id, snap, web)
        log.info("tab %s -> switch %s, then %s (mode %s)", tab_id, target, follow, self.app.mode)
        self._note("tab", tab_id)
        self._close_bar_tabs()
        if follow == "emu" and self.app.mode == OVERLAY and \
                active_tab(snap, web, self._view()) == tab_id:
            # the overlay over this very window: closing it IS the switch
            self.app.cc5.dismiss("tab")
            return
        self.run(target, getattr(self, "_then_" + follow), "tab " + tab_id)

    def _close_bar_tabs(self):
        """BAR mode's Tabs button shows this same strip inline in the bar's
        own row instead of the normal controls (screens.CommandCenter.
        toggle_bar_tabs / close_bar_tabs, patches/W-screens.patch). A tap
        always closes it, whether or not the switch changes `mode` (Discord
        -> Browser stays BAR throughout, so nothing else would ever close
        it). Defensive: a no-op against a fake ui / an unpatched CommandCenter
        that has no close_bar_tabs (older screens.py, tests)."""
        cc = getattr(self.app.ui, "cc", None)
        close = getattr(cc, "close_bar_tabs", None)
        if close is not None:
            close()

    def run(self, target, then=None, why="", wait=True):
        """Switch (worker), then call then() on the UI thread once the mode
        the switch promises has arrived (at the latest SETTLE_S later), or
        right after the switch with wait=False (a follow-up that does not
        touch the UI, e.g. mpv's play: CC5's overlay closing cannot undo it)."""
        self.intent = {"target": target, "until": self.clock() + INTENT_S, "budget": 1,
                       "parked": set()}
        self._start(target, then, why, wait)

    def _start(self, target, then, why, wait=True):
        if self.busy:
            self.queued = (target, then, why, wait)
            return
        app = self.app
        if target == wsw.INTERNAL and app.mode == FULL:
            # FULL: nothing is shown on this screen, so nothing needs to move
            self._done_local(target, then)
            return
        self.busy = True
        self._park_flag(target)
        self.submit(self.switcher.switch, target, app.output, app.es_output,
                    done=lambda res: self._switched(res, then, wait))

    def _done_local(self, target, then):
        self.last = {"target": target, "ok": True, "sent": None, "local": True}
        if then is not None:
            then()
        self.rebuild()

    def _park_flag(self, target):
        """Tell web_tiles its window is being parked BEFORE the switch, so the
        FULL that follows is not read as "the window closed" (which ends the
        session and stops Firefox / mpv)."""
        web = getattr(self.app, "web", None)
        if web is None or web.app is None or not hasattr(web, "parked"):
            self._parked_before = None
            return
        self._parked_before = web.parked
        if target != web.app:
            web.parked = True

    def _sync_parked(self, res):
        web = getattr(self.app, "web", None)
        if web is None or web.app is None or not hasattr(web, "parked"):
            return
        after = res.get("after")
        if not after:
            if self._parked_before is not None:
                web.parked = self._parked_before     # nothing was read: nothing moved
            return
        mine = _wins(after, web.app)
        web.parked = bool(mine) and all(w["where"] == wsw.PARKED for w in mine)

    def _switched(self, res, then, wait=True):
        self.busy = False
        res = res or {"ok": False, "error": "no result"}
        self.switches += 1
        self.last = {k: res.get(k) for k in ("target", "ok", "refused", "error", "sent",
                                              "expect")}
        if res.get("after"):
            self.snap = res["after"]
        self._sync_parked(res)
        if res.get("refused"):
            self.refusals += 1
            self._hint("Can't switch: " + res["refused"])
            self._note("refused", res["refused"])
        elif not res.get("ok"):
            self.errors += 1
            self._note("error", res.get("error"))
            log.warning("tabs: switch %s failed: %s", res.get("target"), res.get("error"))
            if res.get("sent") is None and then is not None:
                # nothing reached sway (it could not be read): behave as
                # before CC6 - start / open what was asked, move nothing
                then()
            else:
                self._hint("Switch failed (see log)")
        else:
            self._note("switched", res.get("sent"))
            it = self.intent
            if it is not None and it["target"] == res.get("target"):
                it["parked"] |= set((res.get("plan") or {}).get("park") or ())
            self._follow_up(res.get("expect") if wait else None, then, res.get("target"))
        self.rebuild()
        if self.queued is not None:
            q, self.queued = self.queued, None
            self._start(*q)

    def _follow_up(self, expect, then, target):
        if then is None:
            return
        app = self.app
        if expect is None or app.mode == expect or (expect == HIDDEN and app.mode == OVERLAY):
            then()
            return
        self._cancel_pending()
        timer = app.call_later(SETTLE_S, lambda: self._settle("timeout"))
        self.pending = {"expect": expect, "then": then, "timer": timer, "target": target}

    def _cancel_pending(self):
        p, self.pending = self.pending, None
        if p is not None:
            self.app.cancel(p["timer"])

    def _settle(self, why):
        p, self.pending = self.pending, None
        if p is None:
            return
        self.app.cancel(p["timer"])
        if why == "timeout" and self.app.mode == HIDDEN and p["expect"] != HIDDEN:
            log.info("tabs: follow-up for %s dropped: still HIDDEN after %.0fs", p["target"],
                     SETTLE_S)
            self._note("dropped", p["target"])
            return
        log.info("tabs: follow-up for %s (%s; mode %s)", p["target"], why, self.app.mode)
        p["then"]()
        self.rebuild()

    def _hint(self, text):
        bar = getattr(self.app.ui, "bar", None)
        if bar is not None and bar.show_hint(text[:60]):
            self.app.call_later(2.5, bar.clear_hint)

    # -- follow-ups (UI thread, after the switch) ----------------------------------------
    def _open_cc(self):
        pull = self.app.pull
        if pull is not None and not pull.is_open():
            pull.open("tab")

    def _then_companion(self):
        pull = self.app.pull
        if pull is not None and pull.is_open():
            pull.close("tab")

    def _then_cc(self):
        self._open_cc()
        self.app.ui.close()

    def _then_hud(self):
        self._open_cc()
        self.app.open_hud()

    def _then_emu(self):
        pass                            # HIDDEN (or OVERLAY) closes the Command Center itself

    def _then_browser(self):
        self._open_web("Browser", None)

    def _then_discord(self):
        import browser
        self._open_web("Discord", browser.DISCORD_URL)

    def _then_ytapp(self):
        """W2b: its own Firefox instance/profile (browser.TV_APP_ID), not
        _open_web() - window_switcher's YTAPP kind already parked whatever
        else was shown and shown this tile's window if it already existed;
        this starts it fresh the first time (main.App.ytapp, patches/
        W2b-main.patch)."""
        self.app.ytapp.open()

    def _open_web(self, label, url):
        web = self.app.web
        if web.app != wsw.WEB:
            self._open_cc()             # the launch sheet ("Opening ...") lives in it
        if web.app == wsw.WEB and web.label == label and self.app.mode == BAR:
            return                      # already on that page's tab
        web.open_web(label, url)

    # -- main.App hooks --------------------------------------------------------------------
    def after_mode(self, old, new, reason):
        p = self.pending
        if p is not None and (new == p["expect"] or (p["expect"] == HIDDEN and new == OVERLAY)):
            self._settle("mode %s" % new)
        if new in (FULL, BAR, OVERLAY, HIDDEN):
            self.refresh()
        self.rebuild()
        self._arm_refresh()

    def after_pull(self, old, new, reason):
        if new == PULL.COMMAND_CENTER:
            self.refresh()
        self.rebuild()
        self._arm_refresh()

    def state(self):
        return {"tabs": [[t["id"], bool(t.get("running")), bool(t.get("active"))]
                         for t in self.strip.tabs],
                "busy": self.busy, "pending": self.pending["target"] if self.pending else None,
                "switches": self.switches, "refusals": self.refusals, "errors": self.errors,
                "scans": self.scans, "last": self.last, "events": self.events[-6:],
                "parking_workspace": wsw.PARK_WS}
