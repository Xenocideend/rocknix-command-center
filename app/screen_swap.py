"""screen_swap: which screen EmulationStation (and the game) is on, which screen the Command
Center belongs on, and a read-only watcher that follows both live.

Swap the top and bottom screens, with the controls staying on the screen the game or ES is
on, and the Command Center usable on both.

The layout daemon (dual-screen-layout-and-power) reads screens.es_screen from config.json
every poll and puts ES and workspace 1 on that output (config.es_screen_from_raw() is what
both sides follow). rp5deck never moves anything, its sway client stays read only. The main
panel binds its layer surface to the output ES isnt on, and the Command Center overlay
(cc_overlay.py) to the one it is on.

Where ES is comes from the sway tree, not the setting: the ES window's output, or while a game
runs (ES has no window then) the output holding workspace 1, which the daemon keeps with ES,
and only if neither exists the setting. That order prevents a lockout. If the panel followed
the setting and ES didnt move (an old daemon, a stopped one, a kill switch), the panel would
bind under fullscreen ES with the other screen bare, no Command Center anywhere and no way to
switch the setting back. Following ES keeps the panel on the other screen, so the worst a
setting the daemon ignores can do is nothing.

Undocked (no DP-1) the swap is ignored like the daemon does: ES is on the built-in panel and
the main panel reports HIDDEN as always.

    placement = plan(tree, es_screen)       # pure
    watcher = ScreenWatcher(callback)        # thread, callback(placement) on change
"""
import collections
import json
import logging
import os
import select
import threading
import time

import config
import sway_ipc

log = logging.getLogger("rp5deck.screens")

ADDON = sway_ipc.EXTERNAL  # "DP-1": the Dual Screen add-on (top)
BUILTIN = sway_ipc.INTERNAL  # "DSI-1": the handheld's own panel (bottom)
OUTPUTS = (ADDON, BUILTIN)
ES_APP_ID = "emulationstation"
ES_WORKSPACE = "1"  # the daemon puts workspace 1 on ES's output
# An ES output that disagrees with screens.es_screen gets held this long
# (ScreenWatcher._apply_hold) before it's followed, so a blip (ES briefly remapping its window
# on the other output right after a game exits, a known ES quirk) reverts before the panel
# rebinds. Longer than the layout daemon's own poll (~3 s) so it always gets a chance to move
# ES back or confirm the move first.
HOLD_S = 5.0

Placement = collections.namedtuple("Placement", "docked es_output cc_output source")
Placement.__doc__ = """docked: both panels exist. es_output: where ES / the game is.
cc_output: where the main panel binds (always the other one when docked).
source: "observed-es", "observed-workspace", "setting" or "undocked"."""


def es_output_for_setting(es_screen):
    return BUILTIN if es_screen == "builtin_bottom" else ADDON


def same_screens(a, b):
    """Equal as far as binding goes. `source` is only for diagnostics, ES closing its window for a
    game flips it without anything moving.
    """
    return a is not None and b is not None and tuple(a[:3]) == tuple(b[:3])


def other_output(output):
    return ADDON if output == BUILTIN else BUILTIN


# ---------------------------------------------------------------------------
# Pure tree reading
# ---------------------------------------------------------------------------
def _walk(node, output=None, workspace=None):
    """(node, its output name, its workspace name) for every node."""
    if node.get("type") == "output":
        output = node.get("name")
    elif node.get("type") == "workspace":
        workspace = node.get("name")
    yield node, output, workspace
    for key in ("nodes", "floating_nodes"):
        for c in node.get(key) or []:
            yield from _walk(c, output, workspace)


def es_window_output(tree):
    for n, out, _ws in _walk(tree):
        if str(n.get("app_id") or "") == ES_APP_ID:
            return out
    return None


def workspace_output(tree, name):
    for n, out, _ws in _walk(tree):
        if n.get("type") == "workspace" and str(n.get("name")) == name:
            return out
    return None


def observed_es_output(tree):
    """(output, source) of ES as sway shows it right now, or (None, None)."""
    out = es_window_output(tree)
    if out in OUTPUTS:
        return out, "observed-es"
    out = workspace_output(tree, ES_WORKSPACE)
    if out in OUTPUTS:
        return out, "observed-workspace"
    return None, None


def plan(tree, es_screen):
    """Placement for a get_tree reply and the stored es_screen. Docked means cc_output != es_output
    and both are real panels (tests/test_screen_swap.py checks). Undocked gives cc on the built-in
    panel and "es" on the missing add-on, so sway_ipc.compute_mode() keeps answering HIDDEN
    "undocked".
    """
    outs = sway_ipc.outputs_in_tree(tree or {})
    if ADDON not in outs or BUILTIN not in outs:
        return Placement(False, ADDON, BUILTIN, "undocked")
    es, source = observed_es_output(tree)
    if es is None:
        es, source = es_output_for_setting(es_screen), "setting"
    return Placement(True, es, other_output(es), source)


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------
def read_setting(path=None, env=None):
    """screens.es_screen the way the daemon reads the file (config.es_screen_from_raw), read from
    disk every call since the other rp5deck process or a hand edit may have changed it.
    """
    path = path or config.config_path(env)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    return config.es_screen_from_raw(raw)


def swapped_value(es_screen):
    """the value "Swap screens" flips to"""
    return config.other_screen(es_screen if es_screen in config.SCREEN_VALUES else "addon_top")


def query_placement(es_screen=None, timeout=2.0):
    """One shot: (Placement, None) or (None, error text). Read-only IPC."""
    if es_screen is None:
        es_screen = read_setting()
    path = sway_ipc.find_socket()
    if not path:
        return None, "no sway IPC socket"
    try:
        c = sway_ipc.Ipc(path, timeout)
        try:
            return plan(c.request(sway_ipc.GET_TREE), es_screen), None
        finally:
            c.close()
    except Exception as e:      # noqa: BLE001 - reported, caller decides
        return None, "sway IPC failed: %s" % e


def fallback_placement(es_screen):
    """When sway cant be asked at start: assume docked and trust the setting. The first watcher
    reading corrects it within a second if ES is somewhere else or the add-on isnt there.
    """
    es = es_output_for_setting(es_screen)
    return Placement(True, es, other_output(es), "setting (sway not answering)")


# ---------------------------------------------------------------------------
# The other rp5deck process (main panel <-> game-screen overlay)
# ---------------------------------------------------------------------------
def pid_alive(pid):
    """True if process `pid` exists (Linux /proc). Never signals anything."""
    try:
        return os.path.isdir("/proc/%d" % int(pid))
    except (TypeError, ValueError):
        return False


def read_peer_state(path, alive=None):
    """The other process's state.json as a dict, or None when it isnt running (no file,
    unreadable, "running": false, or its pid is gone, since a crash leaves "running": true).
    """
    alive = alive or pid_alive
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or not st.get("running") or not alive(st.get("pid")):
        return None
    return st


def peer_mode(st):
    return st.get("mode") if st else None


def peer_open(st):
    """True if that process's Command Center is pulled down right now."""
    if not st:
        return False
    return (st.get("pulldown") or {}).get("state") not in (None, "companion")


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------
class ScreenWatcher:
    """Background thread: subscribes to sway window/workspace/output events (sway_ipc.Ipc, read only
    by construction), rereads the tree after a quiet spell and calls callback(placement) when the
    placement changes, only after seeing the new one twice DEBOUNCE apart (ES moves in a burst:
    container move, focus, workspace switch). Also calls it once at start.

    setting_fn() -> es_screen is checked on every evaluation (one small file read) so a changed
    setting gets picked up while ES isnt there.
    """

    def __init__(self, callback, setting_fn=read_setting, debounce=0.3, resync=10.0,
                 initial=None, ipc_factory=None, find_socket=None, hold_s=HOLD_S):
        self.callback = callback
        self.setting_fn = setting_fn
        self.debounce = debounce
        self.resync = resync
        self.current = initial
        self.hold_s = hold_s
        self._disagree_since = None  # monotonic time the current disagreement started
        self._disagree_output = None  # the observed (disagreeing) es_output being held
        self.events_seen = 0
        self.evaluations = 0
        self._ipc_factory = ipc_factory or (lambda p: sway_ipc.Ipc(p, timeout=5.0))
        self._find_socket = find_socket or sway_ipc.find_socket
        self._stop = threading.Event()
        self._rp, self._wp = os.pipe()
        self._thread = threading.Thread(target=self._run, name="rp5deck-screens", daemon=True)

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

    def evaluate(self, tree):
        self.evaluations += 1
        try:
            setting = self.setting_fn()
        except Exception:           # noqa: BLE001 - a bad read must not stop the watcher
            setting = "addon_top"
        return self._apply_hold(plan(tree, setting), setting)

    def _apply_hold(self, raw, setting):
        """Follows `raw` right away when it agrees with es_screen (a real swap writes es_screen first, so
        by the time ES shows up on the new output the setting already says so) or when there's no
        confirmed placement yet (self.current is None, trust the tree over the setting, so a cold
        start doesnt wait either).

        A disagreeing reading is held for self.hold_s seconds, reporting the last confirmed placement
        (self.current) instead of `raw`, never one made up from the setting, which could put the panel
        back under fullscreen ES whenever the daemon is old, stopped or the setting was hand edited.
        The hold timer resets the moment the reading stops disagreeing (a revert) or disagrees a new
        way.
        """
        if not raw.docked or self.current is None:
            self._disagree_since = None
            return raw
        configured = es_output_for_setting(setting)
        if raw.es_output == configured:
            self._disagree_since = None
            return raw
        now = time.monotonic()
        if self._disagree_since is None or self._disagree_output != raw.es_output:
            self._disagree_since = now
            self._disagree_output = raw.es_output
        if now - self._disagree_since >= self.hold_s:
            return raw
        return self.current

    def _emit(self, placement):
        if not same_screens(placement, self.current):
            log.info("placement %s -> %s", self.current, placement)
            self.current = placement
            try:
                self.callback(placement)
            except Exception:       # noqa: BLE001
                log.exception("placement callback failed")

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001
                log.warning("screen watcher: sway IPC session ended: %s (retry in %.0fs)",
                            e, backoff)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)

    def _session(self):
        path = self._find_socket()
        if not path:
            raise ConnectionError("no sway IPC socket")
        sub = self._ipc_factory(path)
        cmd = self._ipc_factory(path)
        try:
            reply = sub.request(sway_ipc.SUBSCRIBE, json.dumps(["window", "workspace", "output"]))
            if not reply.get("success"):
                raise ConnectionError("subscribe refused: %r" % reply)
            self._emit(self.evaluate(cmd.request(sway_ipc.GET_TREE)))
            deadline = None
            candidate = None
            while not self._stop.is_set():
                now = time.monotonic()
                # resync also catches a setting change while nothing moves
                timeout = self.resync if deadline is None else max(0.0, deadline - now)
                r, _, _ = select.select([sub.sock, self._rp], [], [], timeout)
                if self._rp in r:
                    return
                if sub.sock in r:
                    t, _data = sub.recv()
                    if t & sway_ipc.EVENT_BIT:
                        self.events_seen += 1
                    deadline = time.monotonic() + self.debounce
                    continue
                p = self.evaluate(cmd.request(sway_ipc.GET_TREE))
                if same_screens(p, self.current):
                    candidate, deadline = None, None
                elif same_screens(p, candidate):
                    self._emit(p)
                    candidate, deadline = None, None
                else:
                    candidate = p
                    deadline = time.monotonic() + self.debounce
        finally:
            sub.close()
            cmd.close()
