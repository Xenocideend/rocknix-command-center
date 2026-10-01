"""sway_ipc: works out FULL / BAR / HIDDEN from sway's window tree.

Talks the i3/sway IPC protocol on the socket directly (stdlib only). The client is read only
by construction, Ipc.send() refuses every message type except GET_WORKSPACES, SUBSCRIBE,
GET_OUTPUTS, GET_TREE and GET_VERSION, so this app cant send an output, input, workspace or
focus command even by mistake (the layout daemon owns layout, and we never move ES).

Mode rules:
  HIDDEN  DSI-1 is missing, or DP-1 is missing (undocked, ES moves to DSI-1)
  FULL    no toplevel window on DSI-1's visible workspace
  BAR     every window there has an app_id starting "rp5deck-" (ours, "rp5deck-test-other"
          is saved to mean foreign in tests)
  HIDDEN  any other window there (an emulator's second screen, for DS/3DS the game's own
          touchscreen, we have to get out of its way completely)

compute_mode() is pure and tested against get_tree fixtures. ModeWatcher is the thread: it
subscribes to window/output/workspace events, rereads the tree after a quiet spell, and only
reports a new mode once it's come out the same twice DEBOUNCE apart.
"""
import json
import logging
import os
import select
import socket
import struct
import threading
import time

import screen_map

FULL, BAR, HIDDEN = "FULL", "BAR", "HIDDEN"
INTERNAL = screen_map.CURRENT.bottom
EXTERNAL = screen_map.CURRENT.top
# The app_ids that belong to rp5deck. Any other id, rp5deck-test-other and other rp5deck-* ids
# included, counts as foreign.
OWN_APP_IDS = {"rp5deck-web", "rp5deck-yt", "rp5deck-ytapp"}  # the YouTube TV tile

MAGIC = b"i3-ipc"
_HDR = struct.Struct("=6sII")
RUN_COMMAND, GET_WORKSPACES, SUBSCRIBE, GET_OUTPUTS, GET_TREE = 0, 1, 2, 3, 4
GET_VERSION = 7
READ_ONLY_TYPES = frozenset({GET_WORKSPACES, SUBSCRIBE, GET_OUTPUTS, GET_TREE,
                             GET_VERSION})
EVENT_BIT = 0x80000000
SUBSCRIBE_EVENTS = ["window", "output", "workspace"]

log = logging.getLogger("rp5deck.sway")


# ---------------------------------------------------------------------------
# Pure mode computation
# ---------------------------------------------------------------------------
def outputs_in_tree(tree):
    return {n.get("name"): n for n in (tree.get("nodes") or [])
            if n.get("type") == "output" and not str(n.get("name", "")).startswith("__")}


def _is_view(n):
    if n.get("type") not in ("con", "floating_con"):
        return False
    if n.get("nodes") or n.get("floating_nodes"):
        return False            # a split container, not a window
    return (n.get("pid") is not None or n.get("app_id") is not None
            or n.get("window") is not None or n.get("shell") is not None)


def _views(node):
    if _is_view(node):
        yield node
        return
    for key in ("nodes", "floating_nodes"):
        for c in node.get(key) or []:
            yield from _views(c)


def visible_views(output_node):
    """Windows on the output's visible workspace (all of them if the node doesnt say which one is
    current).
    """
    cur = output_node.get("current_workspace")
    for ws in output_node.get("nodes") or []:
        if ws.get("type") != "workspace":
            continue
        if cur is not None and ws.get("name") != cur:
            continue
        yield from _views(ws)


def window_label(v):
    props = v.get("window_properties") or {}
    return v.get("app_id") or props.get("class") or v.get("name") or "con#%s" % v.get("id")


def is_own_window(v):
    aid = str(v.get("app_id") or "")
    return aid in OWN_APP_IDS


def compute_mode(tree, internal=INTERNAL, external=EXTERNAL):
    """Returns (mode, reason) for a get_tree reply."""
    outs = outputs_in_tree(tree)
    if internal not in outs:
        return HIDDEN, "%s absent" % internal
    views = list(visible_views(outs[internal]))
    if external not in outs:
        # one screen with ES fullscreen on it: one of our own windows on top (a web app on a
        # workspace of its own) turns the Command Center into the bar, anything else hides it
        if views and all(is_own_window(v) for v in views):
            return BAR, "rp5deck window on %s (undocked): %s" % (
                internal, ", ".join(window_label(v) for v in views))
        return HIDDEN, "undocked: %s absent" % external
    if not views:
        return FULL, "no window on %s" % internal
    foreign = [v for v in views if not is_own_window(v)]
    if foreign:
        return HIDDEN, "foreign window on %s: %s" % (
            internal, ", ".join(window_label(v) for v in foreign))
    return BAR, "rp5deck window on %s: %s" % (
        internal, ", ".join(window_label(v) for v in views))


# ---------------------------------------------------------------------------
# IPC client
# ---------------------------------------------------------------------------
class CommandRefused(Exception):
    pass


def find_socket():
    p = os.environ.get("SWAYSOCK")
    if p and os.path.exists(p):
        return p
    for base in ("/run", "/tmp"):
        for dirpath, dirnames, filenames in _walk(base, 3):
            for f in filenames:
                if f.startswith("sway-ipc.") and f.endswith(".sock"):
                    return os.path.join(dirpath, f)
    return None


def _walk(base, depth):
    try:
        entries = list(os.scandir(base))
    except OSError:
        return
    files = [e.name for e in entries if not e.is_dir(follow_symlinks=False)]
    yield base, None, files
    if depth > 0:
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                yield from _walk(e.path, depth - 1)


class Ipc:
    def __init__(self, path, timeout=3.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, mtype, payload=b""):
        if mtype not in READ_ONLY_TYPES:
            raise CommandRefused("sway IPC message type %d is not read-only; "
                                 "rp5deck never sends commands" % mtype)
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
        self.send(mtype, payload)
        while True:
            t, data = self.recv()
            if t == mtype:
                return json.loads(data.decode("utf-8", "replace"))
            # an event mixed in on a subscribed socket, skip it


def query_mode(timeout=2.0, internal=INTERNAL, external=EXTERNAL):
    """One-shot check. Returns (mode, reason) or (None, error text)."""
    path = find_socket()
    if not path:
        return None, "no sway IPC socket"
    try:
        c = Ipc(path, timeout)
        try:
            return compute_mode(c.request(GET_TREE), internal, external)
        finally:
            c.close()
    except Exception as e:      # noqa: BLE001 - reported, caller decides
        return None, "sway IPC failed: %s" % e


class ModeWatcher:
    """Background thread, calls callback(mode, reason) when the debounced mode changes (and once at
    start with the first mode).

    Every event restarts the quiet period, so a steady stream of events (an FPS readout or a page
    title changing a few times a second) could push the recheck back forever and leave rp5deck
    FULL over a DS/3DS emulator's touchscreen. `max_delay` is a hard ceiling that later events
    dont push back, so a recompute happens at least every `max_delay` seconds however fast events
    come.
    """

    def __init__(self, callback, debounce=0.3, resync=60.0, initial=None,
                 internal=INTERNAL, external=EXTERNAL, max_delay=1.0):
        self.callback = callback
        self.internal = internal
        self.external = external
        self.debounce = debounce
        self.max_delay = max_delay
        self.resync = resync
        self.current = initial
        self.last_reason = None
        self.events_seen = 0
        self._stop = threading.Event()
        self._rp, self._wp = os.pipe()
        self._thread = threading.Thread(target=self._run, name="rp5deck-sway",
                                        daemon=True)

    def start(self):
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
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

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001
                log.warning("sway IPC session ended: %s (retry in %.0fs)", e, backoff)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)

    def _emit(self, mode, reason):
        # BAR to BAR with a different reason is another of our windows coming on screen (YouTube picked from
        # the Discord strip, one screen): the strip has to follow it, so that is announced too.
        same_bar = mode == BAR and mode == self.current and reason != self.last_reason
        if mode != self.current or same_bar:
            log.info("mode %s -> %s (%s)", self.current, mode, reason)
            self.current = mode
            self.last_reason = reason
            try:
                self.callback(mode, reason)
            except Exception:       # noqa: BLE001
                log.exception("mode callback failed")
        else:
            self.last_reason = reason

    def _session(self):
        path = find_socket()
        if not path:
            raise ConnectionError("no sway IPC socket")
        sub = Ipc(path, timeout=5.0)
        cmd = Ipc(path, timeout=5.0)
        try:
            reply = sub.request(SUBSCRIBE, json.dumps(SUBSCRIBE_EVENTS))
            if not reply.get("success"):
                raise ConnectionError("subscribe refused: %r" % reply)
            log.info("subscribed to sway %s events on %s", SUBSCRIBE_EVENTS, path)
            mode, reason = compute_mode(cmd.request(GET_TREE), self.internal, self.external)
            self._emit(mode, reason)
            deadline = None  # when to check next (quiet period)
            # The hard ceiling on how long a burst can hold off a recheck. Armed on the first event of a
            # burst and never pushed back by the rest (unlike `deadline`, which every event restarts). It gets
            # checked after draining an event as well as by the select() timeout, since a socket
            # that's always readable would always win select() and the deadline would never fire.
            force_deadline = None
            candidate = None
            while not self._stop.is_set():
                now = time.monotonic()
                pending = [d for d in (deadline, force_deadline) if d is not None]
                timeout = self.resync if not pending else max(0.0, min(pending) - now)
                r, _, _ = select.select([sub.sock, self._rp], [], [], timeout)
                if self._rp in r:
                    return
                recompute = False
                if sub.sock in r:
                    t, data = sub.recv()
                    if t & EVENT_BIT:
                        self.events_seen += 1
                    now = time.monotonic()
                    deadline = now + self.debounce   # restart the quiet period
                    if force_deadline is None:
                        force_deadline = now + self.max_delay
                    elif now >= force_deadline:
                        recompute = True
                else:
                    recompute = True    # deadline or force_deadline elapsed
                if not recompute:
                    continue
                deadline = None
                force_deadline = None
                mode, reason = compute_mode(cmd.request(GET_TREE), self.internal, self.external)
                # BAR counts together with whose window it is: another of our windows on screen is news
                key = (mode, reason) if mode == BAR else mode
                shown = (self.current, self.last_reason) if self.current == BAR else self.current
                if key == shown:
                    candidate = None
                elif candidate == key:
                    self._emit(mode, reason)
                    candidate = None
                else:
                    # first sighting, confirm one debounce later
                    candidate = key
                    deadline = time.monotonic() + self.debounce
                    force_deadline = time.monotonic() + self.max_delay
        finally:
            sub.close()
            cmd.close()
