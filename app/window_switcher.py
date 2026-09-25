#!/usr/bin/env python3
"""window_switcher - CC6: the ONE place rp5deck moves a window, for the app
tabs (app_tabs.py): hide ("park") and show the windows that can share the
Command Center's screen - Firefox (rp5deck-web: Browser, Discord), mpv
(rp5deck-yt: YouTube) and an emulator's second window (DS / 3DS / Wii U).

sway_ipc.py stays read-only (it refuses RUN_COMMAND outright). This module
has its OWN minimal sway IPC client, like focus_guard.py, and a strict
allow-list, enforce_allowed(), that every payload passes before a byte
reaches the socket.

THE ALLOW-LIST - a payload is one or more of these parts joined by "; ",
each a full match, nothing else:

    [con_id=<digits>] move container to workspace rp5deck-parked    (park)
    [con_id=<digits>] move container to output <DSI-1|DP-1>         (show)
    [con_id=<digits>] focus                                         (last part only)

and every con_id is checked against a tree read in the same call:
  * park: a window this module RECOGNISES (recognise()) that is shown on
    the Command Center's screen right now;
  * show: a recognised window that is on the parking workspace, and the
    output is the Command Center's screen - present, one of DSI-1 / DP-1,
    and never the output EmulationStation / the game is on;
  * focus: a visible window that is not being parked in the same payload
    and is not mpv (rp5deck-yt is no_focus by design).
Recognised = app_id exactly rp5deck-web or rp5deck-yt, or a title (Cemu's " - FPS: n"
suffix removed) that contains a match for
092's second-window rule (SECOND_WINDOW_TITLE, kept identical to
rocknix-config/092-dual-screen-persist by a test). EmulationStation, the
game's own window, any other rp5deck-* id and every unknown window are never
recognised, so they can never be moved.

WHY A HIDDEN WORKSPACE, NOT THE SCRATCHPAD (the brief suggested the
scratchpad; read from memory of sway's source, NOT verified on the device -
the device checklist step C1 checks it before anything else):
  * showing a scratchpad window again goes through root_scratchpad_show(),
    which disables fullscreen on the FOCUSED workspace first - that is the
    game's (or ES's) workspace on the other screen, so every "show" would
    un-fullscreen the game, and nothing on the allow-list could fix it;
  * `scratchpad show` also floats the window on the focused (game) screen
    and focuses it;
  * `move scratchpad` returns focus to the window's old workspace even when
    the window was not focused (root_scratchpad_add_container), i.e. it
    takes focus from the game on every hide.
A named workspace that is never displayed has none of that: `move container
to workspace` / `move container to output` are the moves 092 itself uses;
a moved window keeps its tiling / fullscreen state; focus only changes if
the moved window had it (sway restores focus to the old workspace then, and
the payload's trailing focus part hands it straight back - see plan()).
sway creates the workspace on first use (on the focused output), never
switches an output to it, and destroys it when its last window leaves.
compute_mode() only counts an output's CURRENT workspace, so a parked
window is invisible to rp5deck's mode (FULL after parking) exactly like a
closed one.

FOCUS: showing never focuses anything (the owner taps Firefox to type, as
before, HF1). After any park/show the payload ends with one focus part:
the window that had focus before, if it stays visible, else the "home"
window (EmulationStation, or with no ES window the fullscreen game), so a
window that had focus and was parked never leaves the game unfocused.
Focusing the already-focused window is a no-op in sway.

EMULATOR SECOND WINDOWS can only be SHOWN or HIDDEN, never closed (there is
no close command at all). Hiding one while its game runs: the game keeps
running and keeps focus on the other screen; its touch screen is gone until
the tab shows it again (touches land on rp5deck); and a window that is not
displayed gets no frame callbacks, so an emulator that waits on vsync for
that window may stall until it is shown again (device checklist C4-C6:
melonDS, Azahar, Cemu). Showing it again resumes it; nothing is lost.

    python3 window_switcher.py list   [--cc DSI-1] [--es DP-1]
    python3 window_switcher.py switch TARGET [--cc ..] [--es ..] [--apply]
        TARGET: internal | web | yt | emu:<con_id>; dry run unless --apply
"""
import argparse
import json
import logging
import os
import re
import socket
import struct
import sys

log = logging.getLogger("rp5deck.switcher")

ES_APP_ID = "emulationstation"
WEB_APP_ID = "rp5deck-web"
YT_APP_ID = "rp5deck-yt"
# W2b: the YouTube TV (leanback) tile's own dedicated Firefox instance/
# profile (browser.TV_APP_ID) - a window kind of its own, never the same
# window as WEB (browser.APP_ID/PROFILE_DIR), so a global user-agent
# override in its profile cannot leak into Browser/Discord.
YTAPP_APP_ID = "rp5deck-ytapp"
OWN_APP_IDS = (WEB_APP_ID, YT_APP_ID, YTAPP_APP_ID)
OUTPUTS = ("DSI-1", "DP-1")
PARK_WS = "rp5deck-parked"
MAX_PARTS = 12

# rocknix-config/092-dual-screen-persist, ensure_rp5deck_rules: the title
# rule that moves an emulator's second window to the Command Center's
# screen (sway PCRE). tests/test_window_switcher.py reads 092 and requires
# this exact text, so the two cannot drift apart. Not anchored at the front
# since test day (24 Sep): Azahar titles it "Azahar <build> | <game> | Secondary Window".
SECOND_WINDOW_TITLE = r"(^|\| )(Secondary Window|DSperate \(Bottom\)|GamePad View)$|\[w2\]"
_SECOND_RE = re.compile(SECOND_WINDOW_TITLE)
# Cemu appends its frame rate once running ("GamePad View - FPS: 30.03" on the
# device, test day); 092's rule fires when the window maps, before that, so
# only this recogniser needs to see past it.
_FPS_SUFFIX = re.compile(r" - FPS: [0-9.]+$")


def _second_title(title):
    return _FPS_SUFFIX.sub("", str(title or ""))

WEB, YT, EMU, YTAPP = "web", "yt", "emu", "ytapp"
INTERNAL = "internal"
SHOWN, PARKED, ELSEWHERE = "shown", "parked", "elsewhere"
FULL, BAR, HIDDEN = "FULL", "BAR", "HIDDEN"      # sway_ipc's names (not imported: see doc)

# ---------------------------------------------------------------------------
# The three part shapes. Commands are only ever BUILT by the three
# build_*() functions and only ever SENT after enforce_allowed().
# ---------------------------------------------------------------------------
PARK_RE = re.compile(r"\[con_id=(\d+)\] move container to workspace " + re.escape(PARK_WS))
SHOW_RE = re.compile(r"\[con_id=(\d+)\] move container to output (DSI-1|DP-1)")
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


class CommandRefused(Exception):
    pass


# ---------------------------------------------------------------------------
# Pure tree logic
# ---------------------------------------------------------------------------
def _is_view(n):
    """A leaf window (mirrors sway_ipc._is_view; duplicated so this module
    has no runtime dependency on sway_ipc.py, like focus_guard.py)."""
    if n.get("type") not in ("con", "floating_con"):
        return False
    if n.get("nodes") or n.get("floating_nodes"):
        return False
    return (n.get("pid") is not None or n.get("app_id") is not None
            or n.get("window") is not None or n.get("shell") is not None)


def _walk_views(tree):
    """(view, output name, workspace name, visible, floating) for every
    window; visible = on its output's current workspace, output not __i3."""
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
        return None                     # rp5deck-test-other etc.: foreign, never moved
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
        es = game = es_screen_view = None
        for n, out, ws, vis, floating in _walk_views(tree):
            kind = recognise(n)
            if kind is not None:
                if ws == PARK_WS:
                    where = PARKED
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
            if kind is None and ws != PARK_WS and vis:
                aid = str(n.get("app_id") or "")
                if aid == ES_APP_ID and es is None:
                    es = int(n.get("id"))
                elif aid not in OWN_APP_IDS and not aid.startswith("rp5deck"):
                    if n.get("fullscreen_mode") == 1 and game is None:
                        game = int(n.get("id"))
                    if out == es_output and es_screen_view is None:
                        es_screen_view = int(n.get("id"))
        # the window focus goes back to when the focused one is parked:
        # EmulationStation, else the fullscreen game, else whatever is on
        # the ES screen (never a recognised window: those are the ones moved)
        self.home = es if es is not None else (game if game is not None else es_screen_view)
        self.problem = None
        if cc_output not in OUTPUTS:
            self.problem = "the Command Center's screen %r is not DSI-1 or DP-1" % (cc_output,)
        elif cc_output not in outs:
            self.problem = "%s is not connected" % cc_output
        elif cc_output == es_output:
            self.problem = "the Command Center and EmulationStation are on the same screen"
        elif es_output not in outs:
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

    @property
    def parts(self):
        if self.refused or not (self.park or self.show):
            return []
        out = [build_park(w.con_id) for w in self.park]
        out += [build_show(w.con_id, w.output_target) for w in self.show]
        if self.focus is not None:
            out.append(build_focus(self.focus))
        return out

    def payload(self):
        return SEP.join(self.parts)

    def to_dict(self):
        return {"target": self.target, "park": [w.con_id for w in self.park],
                "show": [w.con_id for w in self.show], "focus": self.focus,
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


def plan(snap, target):
    """What to park / show for `target`, from one Snapshot. Never plans a
    move of an unrecognised window (Snapshot.windows holds only recognised
    ones), and refuses rather than half-works:
      * the screen layout is not a docked, two-screen one (Snapshot.problem);
      * an UNKNOWN window is on the Command Center's screen - the result
        could not be what the tab promises, and it is not ours to move.
    Invariant: afterwards at most one KIND of recognised window is shown on
    the Command Center's screen, so rp5deck's mode is `expect`."""
    kind, con_id = parse_target(target)
    p = Plan(target)
    if snap.problem:
        p.refused = snap.problem
        return p
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
        p.focus = f[0]                  # it keeps focus: restore it if sway moved it
    else:
        p.focus = snap.home             # it was parked, or mpv / nothing / a workspace had it
    return p


# ---------------------------------------------------------------------------
# The allow-list
# ---------------------------------------------------------------------------
def enforce_allowed(payload, tree, cc_output, es_output):
    """Raise CommandRefused unless `payload` is built only of the three
    allowed part shapes AND every con_id in it is allowed for its part in
    `tree` (a fresh get_tree). Called from Ipc.run_command(), the one place
    that can send RUN_COMMAND."""
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
    for i, part in enumerate(parts):
        if ";" in part:
            raise CommandRefused("part %r: bad separator" % part)
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
            if target is None or not target[3] or target[2] == PARK_WS:
                raise CommandRefused("focus %d: not a visible window" % cid)
            if str(target[0].get("app_id") or "") == YT_APP_ID:
                raise CommandRefused("focus %d: mpv (%s) is never focused" % (cid, YT_APP_ID))
            continue
        raise CommandRefused("part %r is not on the allow-list" % part)
    if not moved:
        raise CommandRefused("payload moves nothing: %r" % payload)


# ---------------------------------------------------------------------------
# A minimal sway IPC client of its own (sway_ipc.Ipc refuses RUN_COMMAND)
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
        """The ONLY way this module sends RUN_COMMAND: enforce_allowed()
        first, against the tree the payload was planned from."""
        enforce_allowed(payload, tree, cc_output, es_output)
        self._send(RUN_COMMAND, payload)
        return self._reply(RUN_COMMAND)


def open_ipc(timeout=2.0):
    path = find_socket()
    if not path:
        raise ConnectionError("no sway IPC socket")
    return Ipc(path, timeout)


# ---------------------------------------------------------------------------
# The switcher (call from a worker thread: it blocks on the socket)
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
        """A read-only Snapshot of the live tree, or None if sway could not
        be read (the tabs then keep what they had)."""
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
        """Plan from a fresh tree, check, send, re-read and verify. Returns
        a plain dict (see the keys below); never raises."""
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
    """What did not end up where the plan put it (empty list = all good)."""
    wrong = []
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
# CLI (device checklist C1-C3: list first, then a dry run, then --apply)
# ---------------------------------------------------------------------------
def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "switch"):
        s = sub.add_parser(name)
        s.add_argument("--cc", default="DSI-1", help="the Command Center's screen")
        s.add_argument("--es", default="DP-1", help="EmulationStation's screen")
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
