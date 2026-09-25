#!/usr/bin/env python3
"""focus_guard - fix FOC1: after a game exits, EmulationStation's new window
does not always get keyboard focus back, so the owner has to tap the top
screen before the controller works again.

Root cause (TASKS.md FOC1, found by Main on the device 24 Sep 2026): ES
CLOSES its window when a game launches and MAPS A NEW ONE when the game
exits (`close con com.libretro.RetroArch` -> `new con emulationstation` ->
usually `focus con emulationstation`). A tap on bare DSI-1 (the bottom
panel, when rp5deck's layer surface is not covering it - e.g. before install,
or if 094 is down) moves sway's seat focus to DSI-1's empty workspace 2. When
that happens, the new ES window is not the thing sway would refocus, and
nothing re-focuses it for the owner.

This module is a small, standalone daemon (also importable for tests) with
its OWN minimal sway IPC client - it does NOT import sway_ipc.Ipc, and it
does not loosen sway_ipc.py's read-only guarantee in any way. The only
commands this client will EVER send, to anything, are exactly

    ALLOWED_COMMAND = '[app_id="emulationstation"] focus'
    '[con_id=<digits>] focus'          (built by build_focus_con_command())

enforced in code by enforce_allowed() before a single byte reaches the
socket (see CommandRefused, and TestCommandAllowList in
tests/test_focus_guard.py). Nothing else is ever accepted - both forms are
checked with a strict fullmatch, no trailing text, no second command.

Decision (decide(), pure - see its docstring for the exact rule): the base
rule (FOC1) acts only when EmulationStation exists in the tree, AND nothing
that looks like a real window currently holds focus, AND (the focused node
is an empty workspace, or ES was JUST mapped while nothing is focused).
"The main screen keeps control focus unless overwritten by an emulator" (the
owner's words) - a RetroArch window, an rp5deck-launched window (rp5deck-web,
rp5deck-yt), or any other real window keeps it: this guard never steals
from one.

FOC2 adds two more cases HF1 found (TASKS.md FOC2), both still governed by
the same "never steal from a real window" principle:

  gap 1: ES closes its OWN window while a game runs (FOC1's root cause). If
  Firefox (rp5deck-web) also closes at that moment, seat focus lands on the
  now-empty workspace with no ES window for `[app_id="emulationstation"]
  focus` to find at all - find_es() returns None. decide() then looks for a
  fullscreen game/emulator window directly (find_game_window(), matched by
  fullscreen_mode alone, never by output - SW1) and focuses it BY CON_ID,
  since there is no app_id to match on for a window that might be
  "com.libretro.RetroArch", "melonDS", a Cemu window, etc.

  gap 2: mpv (rp5deck-yt) taking focus. `no_focus` (092) only applies when a
  window MAPS, not on a later click, so tapping the video re-focuses mpv
  like any ordinary window - which pauses RetroArch / starves an SDL game of
  input exactly like FOC1, except nothing ever needed keyboard focus in mpv
  in the first place (rp5deck drives it over IPC). So this is the one
  exception carved OUT of "never steal from a real window": if mpv has
  focus and a game/emulator window exists anywhere, decide() hands focus
  straight back to the game (again by con_id). With no game running, this is
  skipped entirely and ES's own focus handling is left alone.

SW1 (screen swap): ES may live on either panel now (DSI-1 when the owner
swaps the screens). Nothing here depends on which: decide() reads only the
tree's focus, app_ids and fullscreen_mode, never an output name, and both
commands act on whatever they match wherever it is - so the controls follow
ES (or the game) to whichever screen it is on without a second command
(tests/test_focus_guard.py TestSwappedScreensSW1, including a mirror test
that renames DP-1/DSI-1 into each other and requires identical decisions).

The daemon (FocusGuard): subscribes to sway's "window" and "workspace"
events, waits DEBOUNCE seconds of quiet after the last event before
re-reading the tree (so a burst of events - the close/new/focus triple on a
game exit - is judged once, on the settled state), rate-limits itself to at
most one action per RATE_LIMIT seconds, logs every action taken (or that a
dry run would have taken) with its reason, and reconnects with exponential
backoff if the sway socket goes away (a sway restart). --dry-run logs
without ever calling run_command().

    python3 focus_guard.py [--dry-run] [--debounce S] [--rate-limit S]
                           [--sway-socket PATH] [--log-level LEVEL]
"""
import argparse
import json
import logging
import os
import re
import select
import socket
import struct
import sys
import threading
import time

log = logging.getLogger("rp5deck.focus_guard")

ES_APP_ID = "emulationstation"

# FOC2: rp5deck's own launched windows (duplicated from sway_ipc.OWN_APP_IDS
# - see the module docstring: this file has no runtime dependency on
# sway_ipc.py at all, by design).
WEB_APP_ID = "rp5deck-web"
YT_APP_ID = "rp5deck-yt"
OWN_APP_IDS = {WEB_APP_ID, YT_APP_ID}

# ---------------------------------------------------------------------------
# The ONLY two commands this module will ever send. Nothing else reads
# ALLOWED_COMMAND or CON_ID_COMMAND_RE to build a command string outside of
# build_focus_con_command() below - every command is compared/matched
# verbatim in enforce_allowed() before a byte reaches the socket.
# ---------------------------------------------------------------------------
ALLOWED_COMMAND = '[app_id="%s"] focus' % ES_APP_ID
CON_ID_COMMAND_RE = re.compile(r'\[con_id=(\d+)\] focus')


def build_focus_con_command(con_id):
    """The one place a '[con_id=...] focus' string is ever built, so it can
    never drift from CON_ID_COMMAND_RE (enforce_allowed's own check)."""
    return "[con_id=%d] focus" % int(con_id)


DEFAULT_DEBOUNCE = 0.15
DEFAULT_RATE_LIMIT = 1.0
DEFAULT_RESYNC = 60.0


# ---------------------------------------------------------------------------
# Pure tree logic
# ---------------------------------------------------------------------------
def _walk(node):
    """Every node in the tree, depth-first (root included)."""
    yield node
    for key in ("nodes", "floating_nodes"):
        for c in node.get(key) or []:
            yield from _walk(c)


def _is_view(n):
    """A real (leaf) window: a con/floating_con with no children of its own
    and something identifying an actual client (mirrors sway_ipc._is_view -
    duplicated, not imported, so this module has no runtime dependency on
    sway_ipc.py at all)."""
    if n.get("type") not in ("con", "floating_con"):
        return False
    if n.get("nodes") or n.get("floating_nodes"):
        return False
    return (n.get("pid") is not None or n.get("app_id") is not None
            or n.get("window") is not None or n.get("shell") is not None)


def _label(n):
    if n is None:
        return "nothing"
    props = n.get("window_properties") or {}
    return (n.get("app_id") or props.get("class") or n.get("name")
            or "%s#%s" % (n.get("type", "node"), n.get("id")))


def is_empty_workspace(n):
    """True only for a workspace with no windows at all. The `type ==
    "workspace"` check is load-bearing, not decorative: every LEAF window
    (a real, focused con - RetroArch, rp5deck-web, ...) also has empty
    `nodes` and `floating_nodes` (it has no children of its own), so
    dropping the type check would make this function call a focused real
    window an "empty workspace" and hand it ES's focus (see
    TestBreakRestoreNeverSteal in tests/test_focus_guard.py, which
    monkeypatches exactly this function to prove the suite catches it)."""
    return n.get("type") == "workspace" and not n.get("nodes") and not n.get("floating_nodes")


def find_es(tree):
    """The EmulationStation window node, or None. Matches by app_id alone
    (not _is_view too): ES's app_id only ever appears on its own con, so
    this needs no extra filter, and staying independent of _is_view keeps
    that function's only job "is the focused node a real window" (see
    TestBreakRestoreNeverSteal in tests/test_focus_guard.py)."""
    for n in _walk(tree):
        if str(n.get("app_id") or "") == ES_APP_ID:
            return n
    return None


# CC6 (window_switcher.py): the app tabs park windows on this workspace,
# which is never displayed. Focusing a window there would make sway show
# the workspace over the game, so a parked window is never a focus target.
PARK_WS = "rp5deck-parked"


def _parked_ids(tree):
    for n in _walk(tree):
        if n.get("type") == "workspace" and n.get("name") == PARK_WS:
            return {v.get("id") for v in _walk(n) if _is_view(v)}
    return set()


def find_game_window(tree):
    """FOC2 gap 1: the first fullscreen real window that is neither
    EmulationStation nor one of rp5deck's own launched windows (rp5deck-web,
    rp5deck-yt) - i.e. a game or a standalone emulator (RetroArch, melonDS,
    Cemu, ...). Matched by fullscreen_mode alone (DESIGN.md: FULL - filling
    the whole panel - is required for anything real on a screen rp5deck
    might otherwise cover), never by app_id (there is no fixed list of
    emulators) and never by output name (SW1: the game can be on either
    physical panel - see TestSwappedScreensSW1)."""
    parked = _parked_ids(tree)
    for n in _walk(tree):
        if not _is_view(n) or n.get("id") in parked:
            continue
        aid = str(n.get("app_id") or "")
        if aid == ES_APP_ID or aid in OWN_APP_IDS:
            continue
        if n.get("fullscreen_mode") == 1:
            return n
    return None


def focused_node(tree):
    """The one node with focused == true, or None if nothing has focus right
    now (can happen transiently between a window closing and the next one
    taking focus - exactly the gap this guard exists to close)."""
    found = None
    for n in _walk(tree):
        if n.get("focused"):
            found = n
    return found


def _mpv_has_focus(node):
    """True when `node` (the tree's one focused node, or None) is rp5deck's
    own mpv (rp5deck-yt). Isolated into its own function purely so a test
    can monkeypatch this one check and prove FOC2 gap 2 actually depends on
    it (TestDecideFOC2Gap2Mpv.test_break_restore_mpv_rule_is_load_bearing),
    the same way TestBreakRestoreNeverSteal does for _is_view /
    is_empty_workspace."""
    return node is not None and str(node.get("app_id") or "") == YT_APP_ID


def decide(tree, just_mapped_es=False):
    """Return (command, reason). Pure - no IPC, no clock.

    `command` is one of:
      * None                             - do nothing
      * ALLOWED_COMMAND                  - focus EmulationStation
      * build_focus_con_command(con_id)  - focus a specific window directly

    (a non-empty string is truthy and None is falsy, so callers that only
    care "should it act" can still do `should, reason = decide(...)`.)

    Checked in this order:

    1. FOC2 gap 2 - mpv (rp5deck-yt) has focus. `no_focus` (092) only
       applies when mpv's window MAPS, so a later tap re-focuses it like any
       ordinary window, which would pause/starve whatever game is running.
       Nothing needs keyboard focus in mpv (rp5deck drives it over IPC), so
       if a game/emulator window exists anywhere, hand focus straight back
       to it. With no game running this step does nothing - ES's own focus
       handling is left alone.
    2. The "never steal from a real window" rule (FOC1, unchanged): any
       OTHER real window with focus - a game, rp5deck-web, ES itself -
       is left alone. Checked before anything below, so it always wins.
    3. FOC1's original rule, when EmulationStation exists in the tree: the
       focused node is a workspace with no windows at all (tiling or
       floating), or ES was JUST mapped (the caller's `just_mapped_es`, set
       for one decide() call after a window "new" event for ES) with
       nothing focused -> focus ES.
    4. FOC2 gap 1 - EmulationStation has NO window in the tree at all (it
       closes its own window while a game runs - FOC1's root cause), and
       focus is on an empty workspace or nothing: find_es() has nothing to
       match, so look for the game/emulator window directly
       (find_game_window()) and focus it by con_id.
    """
    node = focused_node(tree)

    if _mpv_has_focus(node):
        game = find_game_window(tree)
        if game is not None:
            return (build_focus_con_command(game.get("id")),
                    "mpv (%s) has focus while a game is running (%s): returning focus "
                    "to the game" % (YT_APP_ID, _label(game)))
        return None, "mpv (%s) has focus but no game is running: never steal from it" % YT_APP_ID

    es = find_es(tree)

    if node is not None and _is_view(node):
        if es is not None and str(node.get("app_id") or "") == ES_APP_ID:
            return None, "EmulationStation is already focused"
        return None, "a real window has focus (%s): never steal from it" % _label(node)

    if es is not None:
        if node is not None and is_empty_workspace(node):
            return ALLOWED_COMMAND, "an empty workspace is focused (%s)" % (
                node.get("name") or node.get("id"))
        if node is None and just_mapped_es:
            return ALLOWED_COMMAND, "EmulationStation just mapped and nothing is focused"
        # Test day (24 Sep 16:00:45): after RetroArch closed, ES re-mapped
        # its window unfocused and its new/title/fullscreen/move events all
        # fell inside one debounce window, so the one-shot just_mapped_es
        # was lost and NOTHING had focus for 15 s (pad dead until a tap).
        # With nothing focused there is nothing to steal from; with no game
        # window there is nothing else ES could be competing with (a game
        # mid-launch maps and takes focus itself, and while one exists this
        # stays hands-off).
        # ... and (16:05:52) the same exit left WORKSPACE 1 itself focused -
        # the workspace container, not the ES window inside it - so no window
        # had input either. A focused workspace is never a user's target.
        if find_game_window(tree) is None:
            if node is None:
                return ALLOWED_COMMAND, "nothing is focused and no game is running"
            if node.get("type") == "workspace":
                return ALLOWED_COMMAND, "workspace %s itself is focused (no window has input) " \
                    "and no game is running" % (node.get("name") or node.get("id"))
        return None, "no action needed (focused: %s)" % _label(node)

    # No EmulationStation window at all (FOC2 gap 1). Only act when focus is
    # on an empty workspace or nothing at all - anything else was already
    # handled (and refused) by the never-steal check above.
    if node is not None and not is_empty_workspace(node):
        return None, "no action needed (focused: %s)" % _label(node)
    game = find_game_window(tree)
    if game is None:
        return None, "no EmulationStation window and no game window found"
    return (build_focus_con_command(game.get("id")),
            "no EmulationStation window: refocusing the game window (%s)" % _label(game))


# ---------------------------------------------------------------------------
# The command allow-list
# ---------------------------------------------------------------------------
class CommandRefused(Exception):
    pass


def enforce_allowed(command):
    """Raise CommandRefused unless `command` is EXACTLY ALLOWED_COMMAND, or
    fullmatches CON_ID_COMMAND_RE ('[con_id=<digits>] focus', no other text).
    This is the whole allow-list: called before a single byte reaches the
    sway socket, from the one place in this module that can send a
    command."""
    if command == ALLOWED_COMMAND:
        return
    if isinstance(command, str) and CON_ID_COMMAND_RE.fullmatch(command):
        return
    raise CommandRefused(
        "focus_guard refuses to send %r; the only commands it will ever "
        "send are %r or a '[con_id=<digits>] focus' built by "
        "build_focus_con_command()" % (command, ALLOWED_COMMAND))


# ---------------------------------------------------------------------------
# A minimal, standalone sway IPC client (own protocol code - see module doc:
# this is deliberately NOT sway_ipc.Ipc, which refuses RUN_COMMAND outright)
# ---------------------------------------------------------------------------
MAGIC = b"i3-ipc"
_HDR = struct.Struct("=6sII")
RUN_COMMAND, SUBSCRIBE, GET_TREE = 0, 2, 4
EVENT_BIT = 0x80000000
SUBSCRIBE_EVENTS = ["window", "workspace"]


def find_socket():
    p = os.environ.get("SWAYSOCK")
    if p and os.path.exists(p):
        return p
    for base in ("/run", "/tmp"):
        try:
            for entry in os.scandir(base):
                if entry.name.startswith("sway-ipc.") and entry.name.endswith(".sock"):
                    return entry.path
        except OSError:
            continue
    return None


class Ipc:
    """Speaks just enough of the i3/sway IPC wire protocol for this guard:
    SUBSCRIBE, GET_TREE, and RUN_COMMAND - but RUN_COMMAND only ever carries
    ALLOWED_COMMAND or a '[con_id=<digits>] focus' (enforce_allowed()
    below)."""

    def __init__(self, path, timeout=3.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send_raw(self, mtype, payload=b""):
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

    def recv(self):
        magic, length, mtype = _HDR.unpack(self._read(_HDR.size))
        if magic != MAGIC:
            raise ConnectionError("bad IPC magic %r" % magic)
        return mtype, self._read(length)

    def request(self, mtype, payload=b""):
        self._send_raw(mtype, payload)
        while True:
            t, data = self.recv()
            if t == mtype:
                return json.loads(data.decode("utf-8", "replace"))
            # an event interleaved on this socket: skip it, keep waiting

    def subscribe(self, events=SUBSCRIBE_EVENTS):
        reply = self.request(SUBSCRIBE, json.dumps(events))
        if not reply.get("success"):
            raise ConnectionError("sway refused SUBSCRIBE: %r" % reply)
        return reply

    def get_tree(self):
        return self.request(GET_TREE)

    def run_command(self, command):
        """The ONLY method in this module that can send RUN_COMMAND, and it
        refuses everything except ALLOWED_COMMAND / a well-formed
        '[con_id=<digits>] focus' before touching the socket."""
        enforce_allowed(command)
        self._send_raw(RUN_COMMAND, command)
        t, data = self.recv()
        return json.loads(data.decode("utf-8", "replace"))

    def focus_es(self):
        return self.run_command(ALLOWED_COMMAND)

    def focus_con(self, con_id):
        return self.run_command(build_focus_con_command(con_id))


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------
class FocusGuard:
    """Runs the guard against a live sway. start()/stop(); survives sway
    restarts (reconnects with backoff); ~0 CPU idle (blocks in select()
    between events).

    act_fn (test seam): called as act_fn(ipc, command) whenever decide()
    returns a truthy command and the rate limit allows it. Defaults to
    `lambda ipc, command: ipc.run_command(command)`. In --dry-run mode
    act_fn is never called; the guard only logs what it would have done.
    """

    def __init__(self, dry_run=False, debounce=DEFAULT_DEBOUNCE,
                 rate_limit=DEFAULT_RATE_LIMIT, resync=DEFAULT_RESYNC,
                 log_fn=None, act_fn=None, now=time.monotonic,
                 backoff_start=1.0, backoff_cap=30.0):
        self.dry_run = dry_run
        self.debounce = debounce
        self.rate_limit = rate_limit
        self.resync = resync
        self._log = log_fn or log.info
        self._act = act_fn
        self._now = now
        self.backoff_start = backoff_start
        self.backoff_cap = backoff_cap
        self._stop = threading.Event()
        self._rp = self._wp = None                  # opened lazily in start()
        self._thread = threading.Thread(target=self._run, name="rp5deck-focus-guard",
                                        daemon=True)
        self.actions_taken = 0
        self.actions_skipped_rate_limited = 0
        self.events_seen = 0
        self._last_action = None
        self._just_mapped_es = False

    def start(self):
        self._rp, self._wp = os.pipe()
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._wp is None:
            return                                   # never started: nothing to join/close
        try:
            os.write(self._wp, b"x")
        except OSError:
            pass
        self._thread.join(timeout)
        for fd in (self._rp, self._wp):
            try:
                os.close(fd)
            except OSError:
                pass
        self._rp = self._wp = None

    # -- act, with the rate limit -----------------------------------------
    @staticmethod
    def _describe(command):
        """A human-readable name for a log line: 'EmulationStation' for
        ALLOWED_COMMAND (keeps the FOC1 log wording exactly as before),
        'con_id=<n>' for a '[con_id=<n>] focus', else the raw command."""
        if command == ALLOWED_COMMAND:
            return "EmulationStation"
        m = CON_ID_COMMAND_RE.fullmatch(command or "")
        if m:
            return "con_id=%s" % m.group(1)
        return command

    def _maybe_act(self, ipc, command, reason):
        now = self._now()
        if self._last_action is not None and now - self._last_action < self.rate_limit:
            self.actions_skipped_rate_limited += 1
            self._log("focus_guard: SKIPPED (rate-limited, %.2fs since last action): %s"
                      % (now - self._last_action, reason))
            return False
        target = self._describe(command)
        if self.dry_run:
            self._log("focus_guard: [dry-run] would focus %s: %s" % (target, reason))
        else:
            act = self._act or (lambda c, cmd: c.run_command(cmd))
            act(ipc, command)
            self._log("focus_guard: focused %s: %s" % (target, reason))
        self._last_action = now
        self.actions_taken += 1
        return True

    # -- the reconnect-with-backoff loop -----------------------------------
    def _run(self):
        backoff = self.backoff_start
        while not self._stop.is_set():
            try:
                self._session()
                backoff = self.backoff_start
            except Exception as e:              # noqa: BLE001 - reconnect, never crash
                self._log("focus_guard: sway IPC session ended: %s (retry in %.1fs)"
                          % (e, backoff))
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.backoff_cap)

    def _session(self):
        path = find_socket()
        if not path:
            raise ConnectionError("no sway IPC socket")
        sub = Ipc(path, timeout=5.0)
        cmd = Ipc(path, timeout=5.0)
        try:
            sub.subscribe(SUBSCRIBE_EVENTS)
            self._log("focus_guard: subscribed to sway %s events on %s (dry_run=%s)"
                      % (SUBSCRIBE_EVENTS, path, self.dry_run))
            deadline = None
            while not self._stop.is_set():
                now = time.monotonic()
                timeout = self.resync if deadline is None else max(0.0, deadline - now)
                r, _, _ = select.select([sub.sock, self._rp], [], [], timeout)
                if self._rp in r:
                    return
                if sub.sock in r:
                    t, data = sub.recv()
                    if t & EVENT_BIT:
                        self.events_seen += 1
                        self._note_event(t & ~EVENT_BIT, data)
                    deadline = time.monotonic() + self.debounce
                    continue
                # quiet period elapsed (or the resync timeout fired): judge
                # the settled tree once
                just_mapped = self._just_mapped_es
                self._just_mapped_es = False
                try:
                    tree = cmd.get_tree()
                except OSError as e:
                    raise ConnectionError("get_tree failed: %s" % e) from e
                command, reason = decide(tree, just_mapped_es=just_mapped)
                if command:
                    self._maybe_act(cmd, command, reason)
                deadline = None
        finally:
            sub.close()
            cmd.close()

    def _note_event(self, mtype, data):
        """window "new" for EmulationStation arms the just-mapped flag for
        the NEXT settled-tree check; every other window/workspace event just
        restarts the debounce timer (handled by the caller)."""
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return
        if payload.get("change") == "new":
            container = payload.get("container") or {}
            if str(container.get("app_id") or "") == ES_APP_ID:
                self._just_mapped_es = True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="log what would be done; never sends the focus command")
    ap.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE)
    ap.add_argument("--rate-limit", type=float, default=DEFAULT_RATE_LIMIT)
    ap.add_argument("--resync", type=float, default=DEFAULT_RESYNC)
    ap.add_argument("--sway-socket", metavar="PATH",
                    help="override $SWAYSOCK / auto-discovery")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, a.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.sway_socket:
        os.environ["SWAYSOCK"] = a.sway_socket
    if not find_socket():
        log.error("focus_guard: no sway IPC socket found (set SWAYSOCK or --sway-socket)")
        return 1
    guard = FocusGuard(dry_run=a.dry_run, debounce=a.debounce, rate_limit=a.rate_limit,
                       resync=a.resync, log_fn=log.info)
    guard.start()
    log.info("focus_guard: running (dry_run=%s, debounce=%.2fs, rate_limit=%.2fs)",
             a.dry_run, a.debounce, a.rate_limit)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        guard.stop()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(_main(sys.argv[1:]))
