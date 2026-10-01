#!/usr/bin/env python3
"""focus_guard: gives the controller back to the game or EmulationStation after sway loses focus.

When a game launches ES closes its window, and when the game exits ES maps a new one. If a tap
on the bare bottom panel moved seat focus to its empty workspace in between, nothing refocuses
ES and the pad does nothing until you tap the top screen.

It has its own tiny sway IPC client, it doesnt use sway_ipc.Ipc and doesnt loosen that
module's read-only rule. The only commands it will ever send are

    ALLOWED_COMMAND = '[app_id="emulationstation"] focus'
    '[con_id=<digits>] focus'          (built by build_focus_con_command())

and enforce_allowed() fullmatches every command against those before a byte hits the socket.

The rule (decide() has the details): it never takes focus from a real window. It focuses ES
when ES exists and focus is on an empty workspace or nothing, or ES was just mapped. With no ES
window (ES closes its own while a game runs) it focuses the fullscreen game window by con_id.
The one exception to never stealing: rp5deck's own mpv or YouTube App window holding focus
while a game is running, since those are driven over IPC and never need keys.

None of this looks at output names, so it works the same whichever panel ES or the game is on.

The daemon listens for window and workspace events, waits DEBOUNCE seconds of quiet so a burst
(the close/new/focus on a game exit) is judged once, acts at most once per RATE_LIMIT seconds,
logs every action, and reconnects with backoff if sway restarts. --dry-run only logs.

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

# rp5deck's own windows, copied from sway_ipc.OWN_APP_IDS so this file never imports sway_ipc
WEB_APP_ID = "rp5deck-web"
YT_APP_ID = "rp5deck-yt"
# The YouTube App's Firefox (rp5deck-ytapp) gets the same hand back as mpv, it's driven over
# Marionette and touch so it never needs the keyboard while a game runs
YTAPP_APP_ID = "rp5deck-ytapp"
YOUTUBE_APP_IDS = (YT_APP_ID, YTAPP_APP_ID)
OWN_APP_IDS = {WEB_APP_ID, YT_APP_ID, YTAPP_APP_ID}

# ---------------------------------------------------------------------------
# The only two commands this module will ever send, checked in enforce_allowed()
# ---------------------------------------------------------------------------
ALLOWED_COMMAND = '[app_id="%s"] focus' % ES_APP_ID
CON_ID_COMMAND_RE = re.compile(r'\[con_id=(\d+)\] focus')


def build_focus_con_command(con_id):
    """The one place a '[con_id=...] focus' string gets built, so it cant drift from
    CON_ID_COMMAND_RE.
    """
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
    """A real leaf window: a con/floating_con with no children and an actual client (a copy of
    sway_ipc._is_view, so there's no import).
    """
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
    """True only for a workspace with no windows. The type check matters: a focused leaf window
    also has empty nodes, so without it a real window would count as empty and lose focus to ES
    (TestBreakRestoreNeverSteal proves this).
    """
    return n.get("type") == "workspace" and not n.get("nodes") and not n.get("floating_nodes")


def find_es(tree):
    """The EmulationStation window, or None. Matches by app_id alone since only ES's own con has it."""
    for n in _walk(tree):
        if str(n.get("app_id") or "") == ES_APP_ID:
            return n
    return None


# The app tabs park windows on this workspace, which is never shown. Focusing one would make
# sway show it over the game, so parked windows are never a target.
PARK_WS = "rp5deck-parked"


def _parked_ids(tree):
    for n in _walk(tree):
        if n.get("type") == "workspace" and n.get("name") == PARK_WS:
            return {v.get("id") for v in _walk(n) if _is_view(v)}
    return set()


def find_game_window(tree):
    """The first fullscreen real window that isnt ES or one of rp5deck's, so a game or standalone
    emulator. Matched by fullscreen_mode only, never app_id (no fixed emulator list) or output
    (the game can be on either panel).
    """
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
    """The one focused node, or None. Nothing focused happens for a moment between a window
    closing and the next one taking focus, which is exactly the gap this guard closes.
    """
    found = None
    for n in _walk(tree):
        if n.get("focused"):
            found = n
    return found


def _mpv_has_focus(node):
    """True when the focused node is rp5deck's mpv. Its own function so a test can patch it and
    prove the mpv rule depends on it.
    """
    return node is not None and str(node.get("app_id") or "") in YOUTUBE_APP_IDS


def decide(tree, just_mapped_es=False):
    """Returns (command, reason), pure, no IPC or clock.

    command is None (do nothing), ALLOWED_COMMAND (focus ES) or build_focus_con_command(con_id)
    (focus that window). None is falsy, so `should, reason = decide(...)` still works.

    Checked in this order:

    1. mpv or the YouTube App has focus and a game window exists: hand focus back to the game.
       no_focus only applies when mpv maps, so a later tap on the video would take it. With no
       game running this step does nothing.
    2. Any other real window has focus (a game, rp5deck-web, ES itself): leave it alone. This
       always wins over everything below.
    3. ES exists and focus is on an empty workspace, or ES was just mapped (just_mapped_es, set
       for one call after ES's window "new" event) with nothing focused: focus ES.
    4. No ES window at all and focus on an empty workspace or nothing: focus the game window by
       con_id.
    """
    node = focused_node(tree)

    if _mpv_has_focus(node):
        who = "mpv" if str(node.get("app_id") or "") == YT_APP_ID else "the YouTube App"
        game = find_game_window(tree)
        if game is not None:
            return (build_focus_con_command(game.get("id")),
                    "%s (%s) has focus while a game is running (%s): returning focus "
                    "to the game" % (who, node.get("app_id"), _label(game)))
        return None, "%s (%s) has focus but no game is running: never steal from it" % (
            who, node.get("app_id"))

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
        # After RetroArch closed, ES remapped unfocused and all its events landed in one debounce
        # window, so just_mapped_es was lost and nothing had focus for 15 s. With nothing focused
        # there's nothing to steal from, and with no game window ES isnt competing with anything.
        # The same exit can also leave workspace 1 itself focused instead of the ES window in it, and
        # a focused workspace is never what you meant.
        if find_game_window(tree) is None:
            if node is None:
                return ALLOWED_COMMAND, "nothing is focused and no game is running"
            if node.get("type") == "workspace":
                return ALLOWED_COMMAND, "workspace %s itself is focused (no window has input) " \
                    "and no game is running" % (node.get("name") or node.get("id"))
        return None, "no action needed (focused: %s)" % _label(node)

    # No ES window at all. Only act when focus is on an empty workspace or nothing, anything else
    # was already refused by the never-steal check above.
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
    """Raises CommandRefused unless command is exactly ALLOWED_COMMAND or fullmatches
    CON_ID_COMMAND_RE. This is the whole allow list, called before anything reaches the socket.
    """
    if command == ALLOWED_COMMAND:
        return
    if isinstance(command, str) and CON_ID_COMMAND_RE.fullmatch(command):
        return
    raise CommandRefused(
        "focus_guard refuses to send %r; the only commands it will ever "
        "send are %r or a '[con_id=<digits>] focus' built by "
        "build_focus_con_command()" % (command, ALLOWED_COMMAND))


# ---------------------------------------------------------------------------
# A small standalone sway IPC client. On purpose not sway_ipc.Ipc, which refuses RUN_COMMAND.
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
    """Just enough i3/sway IPC for this guard: SUBSCRIBE, GET_TREE and RUN_COMMAND, and
    RUN_COMMAND only ever carries an allowed command.
    """

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
        """The only method here that can send RUN_COMMAND. It refuses anything but the two allowed
        forms before touching the socket.
        """
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
    """Runs the guard against a live sway. start()/stop(), survives sway restarts and sits at
    about 0 CPU idle in select().

    act_fn is the test seam, called as act_fn(ipc, command) when decide() says act and the rate
    limit allows. Defaults to ipc.run_command(command). --dry-run never calls it.
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
        """A name for the log line: 'EmulationStation' for ALLOWED_COMMAND, 'con_id=<n>' for a con_id
        focus, otherwise the raw command.
        """
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
        """A window "new" for ES arms just_mapped_es for the next settled check. Every other event just
        restarts the debounce timer (the caller does that).
        """
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
