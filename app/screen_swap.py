"""screen_swap - SW1: which screen EmulationStation (and the game) is on, which
screen the Command Center belongs on, and a read-only watcher that follows
both live.

The owner's words: "swap the top and bottom screen with controls staying with
the screen that the game is on or ES is on. command center should be able to
be used on both screens."

Who does what:
  * 092-dual-screen-persist (the layout daemon) reads screens.es_screen from
    config.json every poll and puts ES + workspace 1 on that output
    (config.es_screen_from_raw() is the contract both sides implement).
  * rp5deck never moves anything (its sway client stays read-only). The main
    panel binds its layer surface to the output ES is NOT on, and the
    Command Center overlay (cc_overlay.py) to the output ES IS on.

Where ES is comes from the SWAY TREE, not from the setting: the ES window's
output; while a game runs (ES has no window then) the output holding
workspace 1, which 092 keeps with ES; only if neither exists, the setting.
That ordering is the lockout fix (RV1-M4). If the panel followed the setting
and ES did not move - an older 092, a 092 that is stopped, a kill switch -
the panel would bind UNDER fullscreen ES while the other screen sat bare:
no Command Center anywhere, no way to switch the setting back. Following ES
means the panel is always on the screen ES is not on; the worst case of a
setting that 092 ignores is "the swap did nothing".

Undocked (DP-1 absent) the swap is ignored, as in 092: ES is on the built-in
panel and the main panel reports HIDDEN exactly as before SW1.

    placement = plan(tree, es_screen)       # pure
    watcher = ScreenWatcher(callback)        # thread; callback(placement) on change
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

ADDON = sway_ipc.EXTERNAL       # "DP-1": the Retroid Dual Screen Add-on (top)
BUILTIN = sway_ipc.INTERNAL     # "DSI-1": the handheld's own panel (bottom)
OUTPUTS = (ADDON, BUILTIN)
ES_APP_ID = "emulationstation"
ES_WORKSPACE = "1"              # 092 assigns workspace 1 to ES's output
# SW2 (24 Sep): an observed ES output that DISAGREES with the configured
# screens.es_screen is held this long (ScreenWatcher._apply_hold) before
# being followed, so a transient blip - ES briefly re-mapping its window on
# the other output right after a game exits, a known ES quirk - self-reverts
# before the panel's surface ever rebinds. Longer than 092-dual-screen-
# persist's own poll period (~3 s in the bug report's own log) so 092 always
# gets a chance to move ES back (or confirm the move) first.
HOLD_S = 5.0

Placement = collections.namedtuple("Placement", "docked es_output cc_output source")
Placement.__doc__ = """docked: both panels exist. es_output: where ES / the game is.
cc_output: where the main panel binds (always the other one when docked).
source: "observed-es", "observed-workspace", "setting" or "undocked"."""


def es_output_for_setting(es_screen):
    return BUILTIN if es_screen == "builtin_bottom" else ADDON


def same_screens(a, b):
    """Equal as far as binding goes (the `source` field is diagnostics only:
    ES closing its window for a game flips it without moving anything)."""
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
    """Placement for a sway get_tree reply and the stored es_screen value.
    Invariant (tests/test_screen_swap.py): docked => cc_output != es_output,
    and both are real panels; undocked => the pre-SW1 pair (cc on the
    built-in panel, "es" on the absent add-on) so sway_ipc.compute_mode()
    keeps answering HIDDEN "undocked" exactly as before."""
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
    """screens.es_screen as 092 sees the file (config.es_screen_from_raw):
    re-read from disk every call, because the other rp5deck process or a
    hand edit may have changed it since this process loaded its config."""
    path = path or config.config_path(env)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    return config.es_screen_from_raw(raw)


def swapped_value(es_screen):
    """The value "Swap screens" flips to."""
    return config.other_screen(es_screen if es_screen in config.SCREEN_VALUES else "addon_top")


def query_placement(es_screen=None, timeout=2.0):
    """One-shot: (Placement, None) or (None, error text). Read-only IPC."""
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
    """When sway cannot be asked at start-up: assume docked (094 only starts
    rp5deck once DP-1 exists) and trust the setting. The first watcher
    reading corrects it within a second if ES is elsewhere."""
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
    """The other process's state.json as a dict, or None when it is not
    running (no file, unreadable, "running": false, or its pid is gone - a
    crash leaves "running": true behind)."""
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
    """Background thread: subscribes to sway window/workspace/output events
    (sway_ipc.Ipc - read-only by construction), re-reads the tree after a
    quiet period and calls callback(placement) when the placement changes,
    only after seeing the new one twice DEBOUNCE apart (ES moves in a burst:
    container move, focus, workspace switch). Also calls it once at start.

    setting_fn() -> es_screen is consulted on every evaluation (cheap: one
    small file read) so a changed setting is picked up while ES is absent."""

    def __init__(self, callback, setting_fn=read_setting, debounce=0.3, resync=10.0,
                 initial=None, ipc_factory=None, find_socket=None, hold_s=HOLD_S):
        self.callback = callback
        self.setting_fn = setting_fn
        self.debounce = debounce
        self.resync = resync
        self.current = initial
        self.hold_s = hold_s
        self._disagree_since = None      # SW2: monotonic time the current disagreement started
        self._disagree_output = None     # the observed (disagreeing) es_output being held
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
        """SW2: follow `raw` immediately when it agrees with the configured
        es_screen (a real swap: swap_screens_now writes es_screen FIRST, so
        by the time ES is actually observed on the new output the setting
        already says so - always immediate) or when there is no confirmed
        placement yet to fall back on (self.current is None: RV1-M4 says
        trust the sway tree over the setting, always, so a cold start must
        not wait before reporting reality either).

        A DISAGREEING observation is held for self.hold_s seconds, reporting
        the last CONFIRMED placement (self.current) instead of `raw` - never
        a placement synthesized from the setting, which could put the panel
        right back under a fullscreen ES whenever 092 is old, stopped, or the
        setting was hand-edited (exactly the RV1-M4 lockout this module
        exists to avoid). The hold timer resets the moment the observation
        stops disagreeing (a revert) or starts disagreeing a new way."""
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
