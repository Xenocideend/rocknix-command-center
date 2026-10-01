#!/usr/bin/env python3
"""window_switcher: the one place rp5deck moves a window. For the app tabs (app_tabs.py) it parks
and shows the windows that can share the Command Center's screen: Firefox (rp5deck-web for
Browser and Discord, rp5deck-ytapp for the YouTube App), mpv (rp5deck-yt) and an emulator's
second window (DS, 3DS, Wii U).

sway_ipc.py stays read only (it refuses RUN_COMMAND). This has its own small sway IPC client,
like focus_guard.py, and a strict allow list, enforce_allowed(), that every payload goes
through before a byte reaches the socket.

The allow list. A payload is one or more of these parts joined by "; ", each a full match,
nothing else:

    [con_id=<digits>] move container to workspace rp5deck-parked    (park)
    [con_id=<digits>] move container to output <DSI-1|DP-1>         (show)
    [con_id=<digits>] focus                                         (last part only)

and every con_id is checked against a tree read in the same call. Park needs a recognised
window (recognise()) shown on the Command Center's screen right now. Show needs a recognised
window on the parking workspace, and the output has to be the Command Center's screen (there,
DSI-1 or DP-1, and never the output ES or the game is on). Focus needs a visible window that
isnt being parked in the same payload and isnt mpv (rp5deck-yt is no_focus on purpose).

Recognised means app_id exactly one of rp5deck's own window ids, or a title (with Cemu's
" - FPS: n" suffix removed) matching the dual-screen daemon's second window rule
(SECOND_WINDOW_TITLE, kept identical to rocknix-config/dual-screen-layout-and-power by a
test). ES, the game's own window, any other rp5deck-* id and every unknown window are never
recognised, so they can never be moved.

Why a hidden workspace and not the scratchpad (from reading sway's source):
showing a scratchpad window goes through root_scratchpad_show(), which turns off fullscreen on
the focused workspace first. That's the game's (or ES's) workspace on the other screen, so every
show would unfullscreen the game. `scratchpad show` also floats the window on the focused
screen and focuses it, and `move scratchpad` sends focus back to the window's old workspace
even when it wasnt focused, so every hide would take focus from the game.

A named workspace that's never shown has none of that. `move container to workspace` and
`move container to output` are the same moves the dual-screen daemon uses, a moved window
keeps its tiling or fullscreen state, and focus only changes if the moved window had it (sway
puts focus on the old workspace and the payload's last focus part hands it straight back, see
plan()). sway makes the workspace on first use, never shows it on an output, and removes it
when its last window leaves. compute_mode() only looks at an output's current workspace, so a
parked window counts the same as a closed one (FULL after parking).

Focus: showing never focuses anything, you tap Firefox to type like before. After any park or
show the payload ends with one focus part, the window that had focus before if it stays
visible, else the home window (ES, or the fullscreen game when there's no ES window), so
parking the focused window never leaves the game without focus. Focusing the window that
already has focus does nothing in sway.

An emulator's second window can only be shown or hidden, never closed (there's no close
command). Hiding one while its game runs keeps the game running with focus on the other
screen, but its touch screen is gone until the tab shows it again (touches land on rp5deck).
A window that isnt shown gets no frame callbacks, so an emulator that waits on vsync for it
may stall until it's shown again. Showing it resumes it, nothing is lost.

    python3 window_switcher.py list   [--cc DSI-1] [--es DP-1]
    python3 window_switcher.py switch TARGET [--cc ..] [--es ..] [--apply]
        TARGET: internal | web | yt | emu:<con_id>, a dry run unless --apply
"""
import argparse
import json
import logging
import os
import re
import socket
import struct
import sys

import screen_map

log = logging.getLogger("rp5deck.switcher")

ES_APP_ID = "emulationstation"
WEB_APP_ID = "rp5deck-web"
YT_APP_ID = "rp5deck-yt"
# The YouTube App's own Firefox and profile (browser.TV_APP_ID), a window kind of its own and
# never the same window as WEB, so its global user agent override cant leak into
# Browser/Discord.
YTAPP_APP_ID = "rp5deck-ytapp"
OWN_APP_IDS = (WEB_APP_ID, YT_APP_ID, YTAPP_APP_ID)
OUTPUTS = (screen_map.CURRENT.bottom, screen_map.CURRENT.top)
PARK_WS = "rp5deck-parked"
MAX_PARTS = 12

# The title rule rocknix-config/dual-screen-layout-and-power (ensure_rp5deck_rules) uses to
# move an emulator's second window to the Command Center's screen (sway PCRE).
# tests/test_window_switcher.py reads that file and requires this exact text so the two cant
# drift. Not anchored at the front since Azahar titles it "Azahar <build> | <game> | Secondary
# Window".
SECOND_WINDOW_TITLE = r"(^|\| )(Secondary Window|DSperate \(Bottom\)|GamePad View)( - FPS: [0-9.]+)?$|\[w2\]"
_SECOND_RE = re.compile(SECOND_WINDOW_TITLE)
# Cemu adds its frame rate once running ("GamePad View - FPS: 30.03"). The daemon's rule fires
# when the window maps, before that, so only this recogniser needs to see past it.
_FPS_SUFFIX = re.compile(r" - FPS: [0-9.]+$")


def _second_title(title):
    return _FPS_SUFFIX.sub("", str(title or ""))

WEB, YT, EMU, YTAPP = "web", "yt", "emu", "ytapp"
INTERNAL = "internal"
SHOWN, PARKED, ELSEWHERE = "shown", "parked", "elsewhere"
FULL, BAR, HIDDEN = "FULL", "BAR", "HIDDEN"  # sway_ipc's names, not imported (see the doc)

# Undocked there is one screen and ES is fullscreen on it, so a web app opens hidden behind ES. Each
# kind gets a workspace of its own on that screen: showing it is a workspace switch, hiding it is a
# switch back to ES's workspace. The dual-screen daemon moves a newly mapped window there.
UNDOCKED_WS = {WEB: "rp5deck-undocked-web", YTAPP: "rp5deck-undocked-ytapp"}
_UNDOCKED_KINDS = "|".join(UNDOCKED_WS)
UNDOCKED_MOVE_RE = re.compile(r"\[con_id=(\d+)\] move container to workspace rp5deck-undocked-(" +
                              _UNDOCKED_KINDS + ")")
UNDOCKED_WS_RE = re.compile(r"workspace rp5deck-undocked-(" + _UNDOCKED_KINDS + ")")
HOME_WS_RE = re.compile(r"workspace number (\d+)")

# ---------------------------------------------------------------------------
# The three part shapes. Commands only get built by the three build_*() functions and only get
# sent after enforce_allowed().
# ---------------------------------------------------------------------------
PARK_RE = re.compile(r"\[con_id=(\d+)\] move container to workspace " + re.escape(PARK_WS))
SHOW_RE = re.compile(r"\[con_id=(\d+)\] move container to output (%s)" % "|".join(re.escape(o) for o in OUTPUTS))
FOCUS_RE = re.compile(r"\[con_id=(\d+)\] focus")
SEP = "; "


def build_park(con_id):
    return "[con_id=%d] move container to workspace %s" % (int(con_id), PARK_WS)


def build_show(con_id, output):
    if output not in OUTPUTS:
        raise CommandRefused("show: output %r is not one of %s" % (output, OUTPUTS))
    return "[con_id=%d] move container to output %s" % (int(con_id), output)


def build_focus(con_id):
    return "[con_id=%d] focus" % int(con_id)


def build_undocked_move(con_id, kind):
    if kind not in UNDOCKED_WS:
        raise CommandRefused("undocked move: %r has no workspace of its own" % (kind,))
    return "[con_id=%d] move container to workspace %s" % (int(con_id), UNDOCKED_WS[kind])


def build_workspace_of_kind(kind):
    if kind not in UNDOCKED_WS:
        raise CommandRefused("undocked workspace: %r has none" % (kind,))
    return "workspace %s" % UNDOCKED_WS[kind]


def build_home_workspace(name):
    if not re.fullmatch(r"\d+", str(name or "")):
        raise CommandRefused("home workspace %r is not a plain number" % (name,))
    return "workspace number %s" % name


class CommandRefused(Exception):
    pass


# ---------------------------------------------------------------------------
# Pure tree logic
# ---------------------------------------------------------------------------
def _is_view(n):
    """A leaf window (a copy of sway_ipc._is_view so there's no import, like focus_guard.py)."""
    if n.get("type") not in ("con", "floating_con"):
        return False
    if n.get("nodes") or n.get("floating_nodes"):
        return False
    return (n.get("pid") is not None or n.get("app_id") is not None
            or n.get("window") is not None or n.get("shell") is not None)


def _walk_views(tree):
    """(view, output name, workspace name, visible, floating) for every window. visible means on its
    output's current workspace and the output isnt __i3.
    """
    def rec(n, out, ws, vis, floating):
        t = n.get("type")
        if t == "output":
            out = n.get("name")
            cur = n.get("current_workspace")
            for key in ("nodes", "floating_nodes"):
                for c in n.get(key) or []:
                    name = c.get("name")
                    v = (not str(out or "").startswith("__")
                         and (cur is None or name == cur))
                    yield from rec(c, out, name, v, key == "floating_nodes")
            return
        if t == "workspace":
            for key in ("nodes", "floating_nodes"):
                for c in n.get(key) or []:
                    yield from rec(c, out, ws, vis, key == "floating_nodes")
            return
        if _is_view(n):
            yield n, out, ws, vis, floating
            return
        for key in ("nodes", "floating_nodes"):
            for c in n.get(key) or []:
                yield from rec(c, out, ws, vis, floating or key == "floating_nodes")
    yield from rec(tree, None, None, False, False)


def _outputs(tree):
    return [n.get("name") for n in (tree.get("nodes") or [])
            if n.get("type") == "output" and not str(n.get("name", "")).startswith("__")]


def recognise(node):
    """WEB / YT / EMU / YTAPP for a window this module may move, else None."""
    if not _is_view(node):
        return None
    aid = str(node.get("app_id") or "")
    if aid == ES_APP_ID:
        return None
    if aid == WEB_APP_ID:
        return WEB
    if aid == YT_APP_ID:
        return YT
    if aid == YTAPP_APP_ID:
        return YTAPP
    if aid.startswith("rp5deck"):
        return None  # rp5deck-test-other and the like: foreign, never moved
    if _SECOND_RE.search(_second_title(node.get("name"))):
        return EMU
    return None


def emu_family(title):
    """(emulator, tab label) for a second window's title."""
    t = _second_title(title)
    if "[w2]" in t:
        return "melonDS", "DS screen"
    if t.endswith("DSperate (Bottom)"):
        return "DSperate", "DS screen"
    if t.endswith("Secondary Window"):
        return "Azahar", "3DS screen"
    if t.endswith("GamePad View"):
        return "Cemu", "GamePad"
    return "emulator", "Game screen 2"


def _label(n):
    props = n.get("window_properties") or {}
    return n.get("app_id") or props.get("class") or n.get("name") or "con#%s" % n.get("id")


class Win:
    __slots__ = ("con_id", "kind", "title", "app_id", "where", "output", "workspace",
                 "focused", "fullscreen", "floating", "family", "label", "output_target")

    def __init__(self, node, kind, where, output, workspace, floating):
        self.con_id = int(node.get("id"))
        self.kind = kind
        self.title = str(node.get("name") or "")
        self.app_id = str(node.get("app_id") or "")
        self.where = where
        self.output = output
        self.workspace = workspace
        self.focused = bool(node.get("focused"))
        self.fullscreen = node.get("fullscreen_mode") == 1
        self.floating = bool(floating)
        self.output_target = None       # set by plan() for a window it shows
        if kind == EMU:
            self.family, self.label = emu_family(self.title)
        elif kind == WEB:
            self.family, self.label = "Firefox", "Browser"
        elif kind == YTAPP:
            self.family, self.label = "Firefox", "YouTube App"  # browser.YOUTUBE_TV_LABEL
        else:
            self.family, self.label = "mpv", "YouTube"

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class Snapshot:
    """What the switcher knows about one get_tree reply."""

    def __init__(self, tree, cc_output, es_output):
        self.cc_output, self.es_output = cc_output, es_output
        outs = _outputs(tree)
        self.outputs = outs
        self.windows = []
        self.foreign_shown = []         # labels of unknown windows on the CC screen
        self.focused = None             # (con_id, label, visible, kind, app_id) of the focused view
        self.focus_on_view = False
        # one screen, the Command Center's: web apps live on workspaces of their own (UNDOCKED_WS)
        self.undocked = cc_output in outs and es_output not in outs and cc_output != es_output
        self.home_ws = None             # the workspace of `home` (where ES, or the game, is)
        es = game = es_screen_view = None
        ws_of = {}
        for n, out, ws, vis, floating in _walk_views(tree):
            kind = recognise(n)
            if kind is not None:
                if ws == PARK_WS:
                    where = PARKED
                elif self.undocked and kind in UNDOCKED_WS:
                    # only the kind's own workspace counts, anywhere else it is behind ES
                    if ws == UNDOCKED_WS[kind]:
                        where = SHOWN if vis else PARKED
                    else:
                        where = ELSEWHERE
                elif vis and out == cc_output:
                    where = SHOWN
                else:
                    where = ELSEWHERE
                self.windows.append(Win(n, kind, where, out, ws, floating))
            elif vis and out == cc_output:
                self.foreign_shown.append(_label(n))
            if n.get("focused"):
                self.focused = (int(n.get("id")), _label(n), bool(vis and ws != PARK_WS), kind,
                                str(n.get("app_id") or ""))
            # undocked ES (or the game) sits on a workspace that isnt shown while a web app is
            if kind is None and ws != PARK_WS and (vis or self.undocked):
                aid = str(n.get("app_id") or "")
                ws_of[int(n.get("id"))] = ws
                if aid == ES_APP_ID and es is None:
                    es = int(n.get("id"))
                elif aid not in OWN_APP_IDS and not aid.startswith("rp5deck"):
                    if n.get("fullscreen_mode") == 1 and game is None:
                        game = int(n.get("id"))
                    # undocked, whatever is on the one screen stands in for the ES screen
                    if (out == es_output or self.undocked) and es_screen_view is None:
                        es_screen_view = int(n.get("id"))
        # where focus goes back to when the focused window is parked: ES, else the fullscreen game,
        # else whatever is on the ES screen (never a recognised window, those are what get moved)
        self.home = es if es is not None else (game if game is not None else es_screen_view)
        self.home_ws = ws_of.get(self.home)
        self.problem = None
        if cc_output not in OUTPUTS:
            self.problem = "the Command Center's screen %r is not %s" % (cc_output, " or ".join(OUTPUTS))
        elif cc_output not in outs:
            self.problem = "%s is not connected" % cc_output
        elif cc_output == es_output:
            self.problem = "the Command Center and EmulationStation are on the same screen"
        elif es_output not in outs and not self.undocked:
            self.problem = "undocked: %s absent" % es_output

    def of_kind(self, kind, where=None):
        return [w for w in self.windows if w.kind == kind and (where is None or w.where == where)]

    def by_id(self, con_id):
        for w in self.windows:
            if w.con_id == con_id:
                return w
        return None

    def shown(self):
        return [w for w in self.windows if w.where == SHOWN]

    def to_dict(self):
        return {"cc_output": self.cc_output, "es_output": self.es_output,
                "windows": [w.to_dict() for w in self.windows],
                "foreign_shown": list(self.foreign_shown), "home": self.home,
                "focused": list(self.focused) if self.focused else None,
                "problem": self.problem}


def scan(tree, cc_output, es_output):
    return Snapshot(tree, cc_output, es_output)


# ---------------------------------------------------------------------------
# Planning (pure)
# ---------------------------------------------------------------------------
class Plan:
    def __init__(self, target):
        self.target = target
        self.park = []                  # Win
        self.show = []                  # Win
        self.focus = None               # con_id
        self.expect = None              # FULL / BAR / HIDDEN once sway has applied it
        self.refused = None
        self.undocked_move = []         # Win, to its kind's undocked workspace (one screen only)
        self.workspace = None           # the workspace to switch to (one screen only)
        self.undocked_show = None       # Win, the window that ends up showing (one screen only)

    @property
    def parts(self):
        if self.refused or not (self.park or self.show or self.undocked_move or self.workspace):
            return []
        out = [build_park(w.con_id) for w in self.park]
        out += [build_show(w.con_id, w.output_target) for w in self.show]
        out += [build_undocked_move(w.con_id, w.kind) for w in self.undocked_move]
        if self.workspace is not None:
            out.append(self.workspace)
        if self.focus is not None:
            out.append(build_focus(self.focus))
        return out

    def payload(self):
        return SEP.join(self.parts)

    def to_dict(self):
        return {"target": self.target, "park": [w.con_id for w in self.park],
                "show": [w.con_id for w in self.show], "focus": self.focus,
                "undocked_move": [w.con_id for w in self.undocked_move], "workspace": self.workspace,
                "expect": self.expect, "refused": self.refused, "payload": self.payload()}


def parse_target(target):
    """"internal" | "web" | "yt" | "ytapp" | "emu:<con_id>" -> (kind, con_id or None)."""
    t = str(target or "")
    if t in (INTERNAL, WEB, YT, YTAPP):
        return t, None
    m = re.fullmatch(r"emu:(\d+)", t)
    if m:
        return EMU, int(m.group(1))
    raise ValueError("unknown switch target %r" % (target,))


def _plan_undocked(snap, p, kind):
    """One screen, ES (or a game) fullscreen on it. A web app is shown by switching to the workspace
    of its kind (moving its window there first if the daemon hasnt), and hidden again by switching
    back to the workspace ES is on. Nothing else is ever moved. The second windows of emulators and
    the mpv player need the add-on's screen.
    """
    if kind not in UNDOCKED_WS and kind != INTERNAL:
        p.refused = "undocked: that needs the add-on's screen"
        return p
    shown = [w for w in snap.windows if w.where == SHOWN and w.kind in UNDOCKED_WS]
    if kind == INTERNAL:
        if shown and snap.home is not None and snap.home_ws is not None:
            try:
                p.workspace = build_home_workspace(snap.home_ws)
            except CommandRefused as e:
                p.refused = str(e)
                return p
            p.focus = snap.home
        p.expect = HIDDEN
        return p
    mine = snap.of_kind(kind)
    if not mine:
        # nothing to show yet, the app is started after this. The screen stays as it is meanwhile: the bar
        # if another app is up, else ES.
        p.expect = BAR if shown else HIDDEN
        return p
    w = mine[0]
    if w.where == SHOWN:
        p.expect = BAR          # already on screen: nothing to send (Discord tapped over the Browser)
        return p
    if w.workspace != UNDOCKED_WS[kind]:
        p.undocked_move = [w]
    p.workspace = build_workspace_of_kind(kind)
    p.undocked_show = w
    p.focus = w.con_id
    p.expect = BAR
    return p


def plan(snap, target):
    """What to park and show for `target`, from one Snapshot. Never plans moving an unrecognised
    window (Snapshot.windows only holds recognised ones), and refuses instead of half working
    when the screen layout isnt docked with two screens (Snapshot.problem), or when an unknown
    window is on the Command Center's screen (the result couldnt be what the tab promises, and
    it isnt ours to move). Afterwards at most one kind of recognised window is shown on the
    Command Center's screen, so rp5deck's mode is `expect`.
    """
    kind, con_id = parse_target(target)
    p = Plan(target)
    if snap.problem:
        p.refused = snap.problem
        return p
    if snap.undocked:
        return _plan_undocked(snap, p, kind)
    if snap.foreign_shown:
        p.refused = ("another app's window is on this screen (%s); rp5deck never moves it"
                     % ", ".join(snap.foreign_shown))
        return p
    shown = snap.shown()
    if kind == INTERNAL:
        p.park = shown
        p.expect = FULL
    elif kind == EMU:
        w = snap.by_id(con_id)
        if w is None or w.kind != EMU:
            p.refused = "that emulator window is gone"
            return p
        if w.where == ELSEWHERE:
            p.refused = "%s is on the other screen (%s), not parked here" % (w.label, w.output)
            return p
        p.park = [s for s in shown if s.con_id != w.con_id]
        p.show = [w] if w.where == PARKED else []
        p.expect = HIDDEN
    else:
        p.park = [s for s in shown if s.kind != kind]
        p.show = snap.of_kind(kind, PARKED)
        p.expect = BAR if (p.show or snap.of_kind(kind, SHOWN)) else FULL
    for w in p.show:
        w.output_target = snap.cc_output
    parked = set(w.con_id for w in p.park)
    f = snap.focused
    if f is not None and f[2] and f[0] not in parked and f[4] != YT_APP_ID:
        p.focus = f[0]  # it keeps focus, restore it if sway moved it
    else:
        p.focus = snap.home  # it was parked, or mpv, nothing or a workspace had it
    return p


# ---------------------------------------------------------------------------
# The allow-list
# ---------------------------------------------------------------------------
def enforce_allowed(payload, tree, cc_output, es_output):
    """Raises CommandRefused unless `payload` is only made of the three allowed part shapes and every
    con_id in it is allowed for its part in `tree` (a fresh get_tree). Called from
    Ipc.run_command(), the one place that can send RUN_COMMAND.
    """
    if not isinstance(payload, str) or not payload:
        raise CommandRefused("empty payload")
    if any(ch in payload for ch in "\n\r\0,"):
        raise CommandRefused("payload %r contains a forbidden character" % payload)
    parts = payload.split(SEP)
    if len(parts) > MAX_PARTS:
        raise CommandRefused("payload has %d parts (max %d)" % (len(parts), MAX_PARTS))
    snap = Snapshot(tree, cc_output, es_output)
    moved = set()
    parked = set()
    switched = False
    for i, part in enumerate(parts):
        if ";" in part:
            raise CommandRefused("part %r: bad separator" % part)
        m = UNDOCKED_MOVE_RE.fullmatch(part)
        if m:
            cid, kind = int(m.group(1)), m.group(2)
            w = snap.by_id(cid)
            if not snap.undocked:
                raise CommandRefused("move %d: the one-screen workspaces are for undocked only" % cid)
            if w is None or w.kind != kind:
                raise CommandRefused("move %d: not a recognised %s window" % (cid, kind))
            if cid in moved:
                raise CommandRefused("con %d moved twice" % cid)
            moved.add(cid)
            continue
        m = UNDOCKED_WS_RE.fullmatch(part)
        if m:
            kind = m.group(1)
            if not snap.undocked:
                raise CommandRefused("workspace %s: undocked only" % kind)
            if not snap.of_kind(kind):
                raise CommandRefused("workspace %s: no %s window to show" % (kind, kind))
            switched = True
            continue
        m = HOME_WS_RE.fullmatch(part)
        if m:
            if not snap.undocked:
                raise CommandRefused("workspace number %s: undocked only" % m.group(1))
            if snap.home_ws != m.group(1):
                raise CommandRefused("workspace number %s: ES (or the game) is on %r, not there"
                                     % (m.group(1), snap.home_ws))
            switched = True
            continue
        m = PARK_RE.fullmatch(part)
        if m:
            cid = int(m.group(1))
            w = snap.by_id(cid)
            if w is None or w.where != SHOWN:
                raise CommandRefused("park %d: not a recognised window shown on %s"
                                     % (cid, cc_output))
            if cid in moved:
                raise CommandRefused("con %d moved twice" % cid)
            moved.add(cid)
            parked.add(cid)
            continue
        m = SHOW_RE.fullmatch(part)
        if m:
            cid, out = int(m.group(1)), m.group(2)
            if snap.problem:
                raise CommandRefused("show %d: %s" % (cid, snap.problem))
            if out != cc_output or out == es_output:
                raise CommandRefused("show %d: output %s is not the Command Center's screen "
                                     "(%s; ES is on %s)" % (cid, out, cc_output, es_output))
            w = snap.by_id(cid)
            if w is None or w.where != PARKED:
                raise CommandRefused("show %d: not a recognised window on %s" % (cid, PARK_WS))
            if cid in moved:
                raise CommandRefused("con %d moved twice" % cid)
            moved.add(cid)
            continue
        m = FOCUS_RE.fullmatch(part)
        if m:
            cid = int(m.group(1))
            if i != len(parts) - 1:
                raise CommandRefused("focus must be the last part")
            if cid in parked:
                raise CommandRefused("focus %d: it is parked by this payload" % cid)
            target = None
            for n, out, ws, vis, floating in _walk_views(tree):
                if int(n.get("id", -1)) == cid:
                    target = (n, out, ws, vis)
                    break
            # undocked the window being focused is on the workspace this very payload switches to,
            # so it isnt visible yet in the tree the payload was planned from
            coming = snap.undocked and switched and target is not None and (
                cid == snap.home or (snap.by_id(cid) is not None
                                     and snap.by_id(cid).kind in UNDOCKED_WS))
            if target is None or (not coming and (not target[3] or target[2] == PARK_WS)):
                raise CommandRefused("focus %d: not a visible window" % cid)
            if str(target[0].get("app_id") or "") == YT_APP_ID:
                raise CommandRefused("focus %d: mpv (%s) is never focused" % (cid, YT_APP_ID))
            continue
        raise CommandRefused("part %r is not on the allow-list" % part)
    if not moved and not switched:
        raise CommandRefused("payload moves nothing: %r" % payload)


# ---------------------------------------------------------------------------
# A small sway IPC client of its own (sway_ipc.Ipc refuses RUN_COMMAND)
# ---------------------------------------------------------------------------
MAGIC = b"i3-ipc"
_HDR = struct.Struct("=6sII")
RUN_COMMAND, GET_TREE = 0, 4


def find_socket():
    p = os.environ.get("SWAYSOCK")
    if p and os.path.exists(p):
        return p
    for base in ("/run", "/tmp"):
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("sway-ipc.") and e.name.endswith(".sock"):
                return e.path
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    for f in os.scandir(e.path):
                        if f.name.startswith("sway-ipc.") and f.name.endswith(".sock"):
                            return f.path
            except OSError:
                continue
    return None


class Ipc:
    def __init__(self, path, timeout=2.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, mtype, payload=b""):
        if isinstance(payload, str):
            payload = payload.encode()
        self.sock.sendall(_HDR.pack(MAGIC, len(payload), mtype) + payload)

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("sway IPC socket closed")
            buf += chunk
        return buf

    def _reply(self, mtype):
        while True:
            magic, length, t = _HDR.unpack(self._read(_HDR.size))
            if magic != MAGIC:
                raise ConnectionError("bad IPC magic %r" % magic)
            data = self._read(length)
            if t == mtype:
                return json.loads(data.decode("utf-8", "replace"))

    def get_tree(self):
        self._send(GET_TREE)
        return self._reply(GET_TREE)

    def run_command(self, payload, tree, cc_output, es_output):
        """The only way this module sends RUN_COMMAND, enforce_allowed() first against the tree the
        payload was planned from.
        """
        enforce_allowed(payload, tree, cc_output, es_output)
        self._send(RUN_COMMAND, payload)
        return self._reply(RUN_COMMAND)


def open_ipc(timeout=2.0):
    path = find_socket()
    if not path:
        raise ConnectionError("no sway IPC socket")
    return Ipc(path, timeout)


# ---------------------------------------------------------------------------
# The switcher (call it from a worker thread, it blocks on the socket)
# ---------------------------------------------------------------------------
class Switcher:
    """connect() -> an object with get_tree() and run_command(payload, tree,
    cc_output, es_output) (tests pass a simulated sway)."""

    def __init__(self, connect=open_ipc, dry_run=False, log_fn=None):
        self.connect = connect
        self.dry_run = dry_run
        self._log = log_fn or log.info
        self.sent = 0

    def snapshot(self, cc_output, es_output):
        """A read-only Snapshot of the live tree, or None if sway couldnt be read (the tabs keep what
        they had).
        """
        try:
            c = self.connect()
        except Exception as e:          # noqa: BLE001 - "unknown", never a crash
            self._log("switcher: sway unreadable: %s" % e)
            return None
        try:
            return Snapshot(c.get_tree(), cc_output, es_output)
        except Exception as e:          # noqa: BLE001
            self._log("switcher: get_tree failed: %s" % e)
            return None
        finally:
            _close(c)

    def switch(self, target, cc_output, es_output):
        """Plans from a fresh tree, checks, sends, rereads and verifies. Returns a plain dict (keys
        below), never raises.
        """
        res = {"target": target, "ok": False, "refused": None, "error": None, "sent": None,
               "replies": None, "expect": None, "plan": None, "before": None, "after": None,
               "dry_run": self.dry_run}
        try:
            c = self.connect()
        except Exception as e:          # noqa: BLE001
            res["error"] = "sway unreadable: %s" % e
            return res
        try:
            tree = c.get_tree()
            snap = Snapshot(tree, cc_output, es_output)
            p = plan(snap, target)
            res.update(plan=p.to_dict(), expect=p.expect, before=snap.to_dict())
            if p.refused:
                res["refused"] = p.refused
                self._log("switcher: %s refused: %s" % (target, p.refused))
                return res
            payload = p.payload()
            if not payload:
                res["ok"] = True
                res["after"] = res["before"]
                return res
            if self.dry_run:
                enforce_allowed(payload, tree, cc_output, es_output)
                self._log("switcher: [dry-run] would send: %s" % payload)
                res.update(ok=True, sent=payload, after=res["before"])
                return res
            replies = c.run_command(payload, tree, cc_output, es_output)
            self.sent += 1
            res["sent"], res["replies"] = payload, replies
            self._log("switcher: %s -> sent %r, replies %s" % (target, payload, replies))
            after = Snapshot(c.get_tree(), cc_output, es_output)
            res["after"] = after.to_dict()
            bad = [r for r in (replies or []) if not (isinstance(r, dict) and r.get("success"))]
            wrong = verify(p, after)
            if bad:
                res["error"] = "sway refused: %s" % "; ".join(
                    str(r.get("error") if isinstance(r, dict) else r) for r in bad)
            elif wrong:
                res["error"] = "sway said yes but: %s" % "; ".join(wrong)
            else:
                res["ok"] = True
            return res
        except CommandRefused as e:
            res["refused"] = "allow-list: %s" % e
            self._log("switcher: REFUSED by the allow-list: %s" % e)
            return res
        except Exception as e:          # noqa: BLE001 - reported, the caller decides
            res["error"] = "%s: %s" % (type(e).__name__, e)
            self._log("switcher: %s failed: %s" % (target, res["error"]))
            return res
        finally:
            _close(c)


def verify(p, after):
    """what didnt end up where the plan put it (an empty list means all good)"""
    wrong = []
    if after.undocked:
        w = p.undocked_show
        if w is not None:
            a = after.by_id(w.con_id)
            if a is None:
                wrong.append("%s (%d) is gone" % (w.label, w.con_id))
            elif a.where != SHOWN:
                wrong.append("%s (%d) is %s, not shown" % (a.label, a.con_id, a.where))
        elif p.target == INTERNAL and any(a.where == SHOWN for a in after.windows
                                          if a.kind in UNDOCKED_WS):
            wrong.append("a web app is still showing")
        return wrong
    for w in p.park:
        a = after.by_id(w.con_id)
        if a is not None and a.where != PARKED:
            wrong.append("%s (%d) is %s, not parked" % (w.label, w.con_id, a.where))
    for w in p.show:
        a = after.by_id(w.con_id)
        if a is None:
            wrong.append("%s (%d) is gone" % (w.label, w.con_id))
        elif a.where != SHOWN:
            wrong.append("%s (%d) is %s, not shown" % (w.label, w.con_id, a.where))
    return wrong


def _close(c):
    close = getattr(c, "close", None)
    if close is not None:
        try:
            close()
        except Exception:               # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# CLI: list first, then a dry run, then --apply
# ---------------------------------------------------------------------------
def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "switch"):
        s = sub.add_parser(name)
        s.add_argument("--cc", default=screen_map.CURRENT.bottom, help="the Command Center's screen")
        s.add_argument("--es", default=screen_map.CURRENT.top, help="EmulationStation's screen")
        if name == "switch":
            s.add_argument("target", help="internal | web | yt | emu:<con_id>")
            s.add_argument("--apply", action="store_true", help="really send (default: dry run)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "list":
        snap = Switcher().snapshot(a.cc, a.es)
        if snap is None:
            return 1
        print(json.dumps(snap.to_dict(), indent=1))
        return 0
    res = Switcher(dry_run=not a.apply).switch(a.target, a.cc, a.es)
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
