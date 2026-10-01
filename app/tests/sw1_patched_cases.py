#!/usr/bin/env python3
"""SW1 cases that need the PATCHED main.py / screens.py / command-center-app.

I2 (24 Sep): patches/SW1-*.patch are merged into the real files;
tests/test_sw1_merged.py runs this module against the working tree.

Covers: the main panel follows ES (rebind, web apps' output, ModeWatcher
outputs, forced --output, a failed rebind -> exit 4, undocked), the "Swap
screens" tile + confirm, the swap saved at once and only changed keys saved,
the Back button handed to an open overlay; the game-screen overlay
(cc_overlay.py): tab / open / close geometry on the OVERLAY layer, hidden
tiles, the tab setting, Back only when the panel cannot show it, following
ES, undocked; the 094 overlay loop (WSL/Linux).
"""
import argparse
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cc_overlay  # noqa: E402
import config  # noqa: E402
import hidden_overlay  # noqa: E402
import main  # noqa: E402
import screen_swap  # noqa: E402
import summon  # noqa: E402
import sway_ipc  # noqa: E402
import swap_ui  # noqa: E402
from screen_swap import Placement  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN  # noqa: E402
from test_companion import AppCase, FakeLayer, FakeVideo, SyncWorker, W, H  # noqa: E402
from test_web_tiles import FakeBrowser, Facts  # noqa: E402

PULL = summon.PullDownStateMachine
ES_KEY = ("screens", "es_screen")
NORMAL = Placement(True, "DP-1", "DSI-1", "observed-es")
SWAPPED = Placement(True, "DSI-1", "DP-1", "observed-es")
UNDOCKED = Placement(False, "DP-1", "DSI-1", "undocked")


class RebindLayer(FakeLayer):
    def __init__(self, fail=False):
        FakeLayer.__init__(self)
        self.fail = fail
        self.bound = []

    def rebind(self, output):
        if self.fail:
            raise RuntimeError("protocol error (test)")
        self.bound.append(output)
        self.calls.append(("rebind", output))
        return (1920, 1080)


class Wl:
    ANCHOR_TOP, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT, ANCHOR_ALL = 1, 2, 4, 8, 15
    LAYER_TOP, LAYER_OVERLAY = 2, 3


class FakeModeWatcher:
    def __init__(self, internal, external):
        self.internal, self.external, self.current = internal, external, None
        self.events_seen = 0


def summon_ev():
    return summon.SummonEvent("btn_back_f1", "InputPlumber Keyboard", "/dev/input/event5",
                              0x3b, 1, 0.0, 0, 0)


class Base(AppCase):
    def setUp(self):
        AppCase.setUp(self)
        self._orig_query = sway_ipc.query_mode
        sway_ipc.query_mode = lambda **kw: (FULL, "test: no window on %s" % kw.get("internal"))
        self.addCleanup(setattr, sway_ipc, "query_mode", self._orig_query)
        self._orig_alive = screen_swap.pid_alive
        screen_swap.pid_alive = lambda pid: True
        self.addCleanup(setattr, screen_swap, "pid_alive", self._orig_alive)

    def cfg_path(self):
        return os.environ["RP5DECK_CONFIG"]

    def write_cfg(self, obj):
        with open(self.cfg_path(), "w") as f:
            json.dump(obj, f)

    def raw(self):
        with open(self.cfg_path(), encoding="utf-8") as f:
            return f.read()

    def disk(self):
        cfg, _ = config.load(self.cfg_path())
        return cfg

    def write_peer(self, rel, state):
        p = os.path.join(os.environ["RP5DECK_RUN_DIR"], rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(dict({"running": True, "pid": 4242}, **state), f)


# ---------------------------------------------------------------------------
# the main panel
# ---------------------------------------------------------------------------
class PanelCase(Base):
    def make_panel(self, output=None, layer=None, cfg=None):
        if cfg is not None:
            self.write_cfg(cfg)
        app = main.App(argparse.Namespace(seconds=0, output=output))
        app.title = "Test Device Command Center"
        app.layer = layer or RebindLayer()
        app.wl = Wl()
        app.mode = FULL
        app.video = FakeVideo()
        for name in ("media_worker", "io_worker", "audio_worker", "search_worker",
                     "web_worker"):
            setattr(app, name, SyncWorker(app.post))
        app.web_parts = {"browser": FakeBrowser(), "facts": Facts()}
        app.build_ui(W, H)
        app.watcher = FakeModeWatcher(app.output, app.es_output)
        app.post.drain()
        return app


class TestPanelFollowsES(PanelCase):
    def test_initial_placement_binds_opposite_es(self):
        app = self.make_panel()
        orig = screen_swap.query_placement
        screen_swap.query_placement = lambda setting=None, timeout=2.0: (SWAPPED, None)
        self.addCleanup(setattr, screen_swap, "query_placement", orig)
        app._initial_placement()
        self.assertEqual((app.output, app.es_output), ("DP-1", "DSI-1"))

    def test_initial_placement_without_sway_trusts_the_setting(self):
        app = self.make_panel(cfg={"schema_version": 1, "screens": {"es_screen": "builtin_bottom"}})
        orig = screen_swap.query_placement
        screen_swap.query_placement = lambda setting=None, timeout=2.0: (None, "no socket")
        self.addCleanup(setattr, screen_swap, "query_placement", orig)
        app._initial_placement()
        self.assertEqual((app.output, app.es_output), ("DP-1", "DSI-1"))

    def test_swap_rebinds_and_moves_everything_that_names_the_output(self):
        app = self.make_panel()
        self.assertEqual(app.output, "DSI-1")
        app.on_placement(SWAPPED)
        self.assertEqual(app.layer.bound, ["DP-1"])
        self.assertEqual((app.output, app.es_output), ("DP-1", "DSI-1"))
        self.assertEqual((app.watcher.internal, app.watcher.external), ("DP-1", "DSI-1"))
        self.assertEqual(app.web.internal, "DP-1")
        self.assertEqual(app.web.keyboard.output, "DP-1")
        self.assertEqual(app.rebinds, 1)
        app.on_placement(NORMAL)
        self.assertEqual(app.layer.bound, ["DP-1", "DSI-1"])
        self.assertEqual(app.web.keyboard.output, "DSI-1")
        st = app.state()["screens"]
        self.assertEqual((st["output"], st["rebinds"], st["role"]), ("DSI-1", 2, "panel"))

    def test_same_screens_do_not_rebind(self):
        app = self.make_panel()
        app.on_placement(NORMAL)
        app.on_placement(Placement(True, "DP-1", "DSI-1", "observed-workspace"))
        self.assertEqual(app.layer.bound, [])

    def test_a_mode_change_follows_the_rebind(self):
        app = self.make_panel()
        app.on_mode(HIDDEN, "foreign window on DSI-1")
        sway_ipc.query_mode = lambda **kw: (FULL, "no window on %s" % kw["internal"])
        app.on_placement(SWAPPED)
        self.assertEqual(app.mode, FULL)
        self.assertIn("DP-1", app.mode_reason)

    def test_failed_rebind_exits_4_for_a_fresh_surface(self):
        app = self.make_panel(layer=RebindLayer(fail=True))
        app.on_placement(SWAPPED)
        self.assertEqual(app.exit_code, 4)
        self.assertIn("rebind", app.stop_reason)
        self.assertEqual(app.output, "DSI-1")

    def test_forced_output_never_moves(self):
        app = self.make_panel(output="DSI-1")
        app.on_placement(SWAPPED)
        self.assertEqual(app.layer.bound, [])
        self.assertEqual(app.output, "DSI-1")

    def test_undocked_binds_the_builtin_panel(self):
        app = self.make_panel()
        app.on_placement(SWAPPED)
        app.on_placement(UNDOCKED)
        self.assertEqual(app.output, "DSI-1")
        self.assertEqual(app.es_output, "DP-1")        # compute_mode then says "undocked"

    def test_setting_changed_elsewhere_refreshes_the_toggle(self):
        app = self.make_panel()
        self.write_cfg({"schema_version": 1, "screens": {"es_screen": "builtin_bottom"}})
        app.on_placement(NORMAL)
        self.assertEqual(config.get_value(app.cfg, ES_KEY), "builtin_bottom")
        self.assertTrue(app.ui.settings.rows[ES_KEY].control.state)
        self.assertEqual(app._unsaved, {})             # a refresh is not a change


def _es_tree(es_output):
    """A minimal sway get_tree with ES's window on `es_output`'s workspace 1
    and nothing else - enough for both sway_ipc.compute_mode() and
    screen_swap.observed_es_output()."""
    def out(name, has_es):
        ws = {"type": "workspace", "name": "1", "floating_nodes": [],
             "nodes": ([{"type": "con", "app_id": "emulationstation",
                        "name": "EmulationStation", "pid": 1, "nodes": [],
                        "floating_nodes": []}] if has_es else [])}
        return {"type": "output", "name": name, "current_workspace": "1", "nodes": [ws]}
    return {"type": "root", "nodes": [out("DP-1", es_output == "DP-1"),
                                      out("DSI-1", es_output == "DSI-1")]}


class TestModeRaceFix(PanelCase):
    """SW2 (24 Sep): the exact incident from the real device log in the bug
    report. After a game exits, ES briefly re-maps its window on the OTHER
    output (a known ES quirk); dual-screen-layout-and-power moves it back at its
    next poll (~3s). A background sway_ipc.ModeWatcher event computed
    against the OLD output pairing can still be sitting in main.App's post
    queue when main.on_placement's own (fresh, correct) mode recompute for
    the NEW pairing has already run in the SAME drain() pass - applying that
    stale event then silently undoes the fix.

    Reproduced by direct calls in the EXACT order the real log showed - no
    threads or wall-clock timing needed: the bug is about which of two
    QUEUED events a single drain() pass applies LAST, not about how fast
    either was computed. tests/test_screen_swap.py's TestHold covers the
    companion fix that stops the SURFACE from ever chasing this blip."""

    def test_the_stale_event_is_dropped_not_applied(self):
        app = self.make_panel()                      # NORMAL: es DP-1, panel on DSI-1
        trees = {"now": _es_tree("DP-1")}
        sway_ipc.query_mode = lambda **kw: sway_ipc.compute_mode(
            trees["now"], internal=kw["internal"], external=kw["external"])

        # Cycle 1 (the blip): in the real log the STALE event happened to be
        # processed FIRST - harmless, on_placement's own fresh recompute
        # corrects it a moment later.
        self.assertEqual((app.output, app.es_output), ("DSI-1", "DP-1"))
        app._on_watched_mode(HIDDEN, "foreign window on DSI-1: emulationstation",
                            app.output, app.es_output)
        self.assertEqual(app.mode, HIDDEN)
        trees["now"] = _es_tree("DSI-1")               # ES really did blip to DSI-1
        app.on_placement(SWAPPED)                      # rebind DSI-1 -> DP-1; fresh recompute
        self.assertEqual(app.mode, FULL)
        self.assertEqual(app.layer.bound, ["DP-1"])

        # Cycle 2 (092 moves ES back): on_placement is processed FIRST this
        # time (the real, UNLUCKY order that caused the bug) - its own
        # recompute is silently a no-op (mode is already FULL). THEN the
        # watcher's stale event arrives, computed for the OLD pairing (DP-1
        # internal / DSI-1 external) from BEFORE this rebind.
        trees["now"] = _es_tree("DP-1")                 # ES really is back on DP-1
        app.on_placement(NORMAL)                        # rebind DP-1 -> DSI-1
        self.assertEqual(app.mode, FULL)
        self.assertEqual(app.layer.bound, ["DP-1", "DSI-1"])
        app._on_watched_mode(HIDDEN, "foreign window on DP-1: emulationstation",
                            "DP-1", "DSI-1")             # the stale (pre-rebind) computation
        self.assertEqual(app.mode, FULL,
                         "a mode event computed for an output pairing this surface has "
                         "since moved away from must be dropped, not applied")


class TestReconcileWatchdog(PanelCase):
    """SW2 (24 Sep, Main's follow-up): a backstop for "stuck" - main.App.
    _reconcile recomputes mode from a fresh tree every RECONCILE_PERIOD and
    applies it after two consecutive confirming checks."""

    def stuck(self, es_output="DP-1"):
        app = self.make_panel()
        trees = {"now": _es_tree(es_output)}
        sway_ipc.query_mode = lambda **kw: sway_ipc.compute_mode(
            trees["now"], internal=kw["internal"], external=kw["external"])
        return app, trees

    def test_the_stuck_bug_self_heals_via_the_watchdog_alone(self):
        # Exactly the bug's OUTCOME (real tree says FULL, self.mode is
        # wrongly HIDDEN) with _on_watched_mode's own fix not involved at
        # all - the watchdog is the ONLY thing that can recover here.
        app, trees = self.stuck("DP-1")           # ES really is on DP-1: panel (DSI-1) is FULL
        app.mode = HIDDEN
        app.mode_reason = "foreign window on DSI-1: emulationstation (stuck)"
        now = 1000.0
        app.reconcile_next = now
        app._reconcile(now)                        # first sighting: not applied yet
        self.assertEqual(app.mode, HIDDEN)
        self.assertEqual(app.reconcile_corrections, 0)
        app._reconcile(now + main.RECONCILE_PERIOD)  # second, confirming sighting
        self.assertEqual(app.mode, FULL)
        self.assertEqual(app.reconcile_corrections, 1)

    def test_a_single_sighting_is_not_enough(self):
        app, trees = self.stuck("DP-1")
        app.mode = HIDDEN
        now = 1000.0
        app.reconcile_next = now
        app._reconcile(now)
        self.assertEqual(app.mode, HIDDEN)
        # reality flips back to agreeing before the SECOND check: never confirmed
        trees["now"] = _es_tree("DSI-1")
        app._reconcile(now + main.RECONCILE_PERIOD)
        self.assertEqual(app.mode, HIDDEN)
        self.assertEqual(app.reconcile_corrections, 0)

    def test_skips_during_overlay(self):
        app, trees = self.stuck("DP-1")
        app.mode = hidden_overlay.OVERLAY
        now = 1000.0
        app.reconcile_next = now
        app._reconcile(now)
        app._reconcile(now + main.RECONCILE_PERIOD)
        self.assertEqual(app.mode, hidden_overlay.OVERLAY, "OVERLAY must never be touched")

    def test_skips_while_the_command_center_is_open(self):
        app, trees = self.stuck("DP-1")
        app.mode = HIDDEN
        app.pull._goto(PULL.COMMAND_CENTER, "test")
        now = 1000.0
        app.reconcile_next = now
        app._reconcile(now)
        app._reconcile(now + main.RECONCILE_PERIOD)
        self.assertEqual(app.mode, HIDDEN, "an open Command Center must not be yanked away")
        self.assertEqual(app.reconcile_corrections, 0)

    def test_skips_while_a_configure_is_pending(self):
        app, trees = self.stuck("DP-1")
        app.mode = HIDDEN
        app.layer.configured = False               # a rebind's configure has not landed yet
        now = 1000.0
        app.reconcile_next = now
        app._reconcile(now)
        app._reconcile(now + main.RECONCILE_PERIOD)
        self.assertEqual(app.mode, HIDDEN)
        self.assertEqual(app.reconcile_corrections, 0)

    def test_rate_limited(self):
        app, trees = self.stuck("DP-1")
        app.mode = HIDDEN
        app.reconcile_next = 1000.0
        app._reconcile(1000.0)                       # first sighting
        app.reconcile_next = 1005.0
        app._reconcile(1005.0)                        # confirmed: correction #1
        self.assertEqual(app.reconcile_corrections, 1)
        self.assertEqual(app.reconcile_last, 1005.0)
        # immediately gets stuck again (a second, unrelated bad event), well
        # inside RECONCILE_MIN_GAP of the last correction
        app.mode = HIDDEN
        app.reconcile_next = 1006.0
        app._reconcile(1006.0)                        # first sighting
        app.reconcile_next = 1007.0
        app._reconcile(1007.0)                         # confirmed, but rate-limited
        self.assertEqual(app.reconcile_corrections, 1, "rate limit must have held it back")
        self.assertGreater(app.reconcile_skips, 0)

    def test_the_overlay_process_has_no_watchdog(self):
        app = cc_overlay.OverlayApp(argparse.Namespace(seconds=0, output=None))
        self.assertIsNone(app.watcher)
        app.mode = HIDDEN
        app.reconcile_next = 0.0
        app._reconcile(1000.0)                       # must not raise, must not touch mode
        self.assertEqual(app.mode, HIDDEN)


class TestHeartbeat(PanelCase):
    """SW2: main.py's main loop touches its heartbeat file about every
    HEARTBEAT_PERIOD seconds; command-center-app's hang detector restarts a child
    whose pid is alive but whose heartbeat has gone stale."""

    def test_panel_heartbeat_path_and_write(self):
        app = self.make_panel()
        want = os.path.join(os.environ["RP5DECK_RUN_DIR"], "heartbeat")
        self.assertEqual(app.heartbeat_path(), want)
        app._touch_heartbeat(1000.0)
        with open(want) as f:
            t0 = float(f.read())
        self.assertAlmostEqual(t0, time.time(), delta=5)
        # throttled: a second call inside HEARTBEAT_PERIOD does not rewrite
        os.utime(want, (0, 0))
        app._touch_heartbeat(1000.5)
        self.assertEqual(os.stat(want).st_mtime, 0)
        app._touch_heartbeat(1000.0 + main.HEARTBEAT_PERIOD)
        self.assertNotEqual(os.stat(want).st_mtime, 0)

    def test_overlay_heartbeat_is_a_separate_flat_file(self):
        app = cc_overlay.OverlayApp(argparse.Namespace(seconds=0, output=None))
        want = os.path.join(os.environ["RP5DECK_RUN_DIR"], "heartbeat-overlay")
        self.assertEqual(app.heartbeat_path(), want)
        self.assertNotEqual(app.heartbeat_path(),
                            os.path.join(os.environ["RP5DECK_RUN_DIR"], "overlay", "heartbeat"))
        app._touch_heartbeat(1000.0)
        self.assertTrue(os.path.exists(want))


class TestSwapTile(PanelCase):
    def open_cc(self, app):
        app.open_command_center()
        app.post.drain()
        self.assertEqual(app.ui.showing, "cc")

    def test_tile_confirm_swap_writes_the_file_and_closes(self):
        app = self.make_panel()
        self.open_cc(app)
        self.tap(app, swap_ui.SWAP_TILE_NAME)
        self.assertEqual(app.ui.sheet, swap_ui.CONFIRM_SHEET)
        self.assertEqual(app.ui.confirm.title.text, "Swap screens")
        self.tap(app, "confirm.yes")
        self.assertEqual(self.disk()["screens"]["es_screen"], "builtin_bottom")
        self.assertEqual(self.disk()["output"], "DP-1")
        self.assertEqual(app.pull.state, PULL.COMPANION)
        self.assertEqual(config.get_value(app.cfg, ES_KEY), "builtin_bottom")

    def test_swapped_prompt_says_back_and_cancel_writes_nothing(self):
        self.write_cfg({"schema_version": 1, "screens": {"es_screen": "builtin_bottom"}})
        app = self.make_panel()
        self.open_cc(app)
        before = self.raw()
        self.tap(app, swap_ui.SWAP_TILE_NAME)
        self.assertEqual(app.ui.confirm.title.text, "Swap screens back")
        self.tap(app, "confirm.no")
        self.assertEqual(self.raw(), before)
        self.assertEqual(app.ui.sheet, None)
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)

    def test_the_flip_reads_the_file_not_a_stale_copy(self):
        app = self.make_panel()                                     # loaded unswapped
        self.write_cfg({"schema_version": 1, "screens": {"es_screen": "builtin_bottom"}})
        app.swap_screens_now()                                      # overlay swapped meanwhile
        self.assertEqual(self.disk()["screens"]["es_screen"], "addon_top")

    def test_settings_toggle_is_saved_without_waiting(self):
        app = self.make_panel()
        config.set_value(app.cfg, ES_KEY, "builtin_bottom")
        app.on_setting(ES_KEY, "builtin_bottom")                    # no timers run
        self.assertEqual(self.disk()["screens"]["es_screen"], "builtin_bottom")

    def test_only_changed_keys_are_written(self):
        app = self.make_panel()
        self.write_cfg({"schema_version": 1, "screens": {"es_screen": "builtin_bottom"}})
        config.set_value(app.cfg, ("companion", "play_video"), False)
        app.on_setting(("companion", "play_video"), False)
        self.run_timers(app, 5)
        d = self.disk()
        self.assertIs(d["companion"]["play_video"], False)
        self.assertEqual(d["screens"]["es_screen"], "builtin_bottom")   # not put back


class TestPanelBackButton(PanelCase):
    def test_back_opens_the_panel_normally(self):
        app = self.make_panel()
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)

    def test_back_is_left_to_an_open_overlay(self):
        app = self.make_panel()
        self.write_peer("overlay/state.json", {"pulldown": {"state": "command_center"}})
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMPANION)

    def test_a_dead_overlay_does_not_swallow_back(self):
        app = self.make_panel()
        self.write_peer("overlay/state.json", {"pulldown": {"state": "command_center"}})
        screen_swap.pid_alive = lambda pid: False
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)


# ---------------------------------------------------------------------------
# the game-screen overlay
# ---------------------------------------------------------------------------
class OverlayCase(Base):
    def make_overlay(self, cfg=None, placement=NORMAL, layer=None):
        if cfg is not None:
            self.write_cfg(cfg)
        app = cc_overlay.OverlayApp(argparse.Namespace(seconds=0, output=None))
        app.title = "Test Device Command Center"
        app.layer = layer or RebindLayer()
        app.wl = Wl()
        app.io_worker = SyncWorker(app.post)
        app.audio_worker = SyncWorker(app.post)
        app.placement = placement
        app.output = app.placement_output(placement)
        app.build_ui(W, H)
        mode0, why0 = app.initial_mode()
        app.mode = FULL                         # what setup() does before on_mode(mode0)
        app.on_mode(mode0, why0)
        self.size(app)
        app.post.drain()
        return app

    def size(self, app):
        """What the layer's configure does: resize to the geometry."""
        if app.mode == cc_overlay.TAB:
            app.w, app.h = cc_overlay.TAB_W, cc_overlay.TAB_H
        else:
            app.w, app.h = W, H
        app.ui.set_size(app.w, app.h)


class TestOverlay(OverlayCase):
    TAB_GEOM = (Wl.ANCHOR_TOP | Wl.ANCHOR_RIGHT, (cc_overlay.TAB_W, cc_overlay.TAB_H), 0)

    def test_overlay_layer_on_the_es_screen(self):
        app = self.make_overlay()
        self.assertEqual(app.layer_level(), Wl.LAYER_OVERLAY)
        self.assertEqual(app.output, "DP-1")
        self.assertTrue(app.run_dir.endswith("overlay"))

    def test_starts_as_a_small_tab(self):
        app = self.make_overlay()
        self.assertEqual(app.mode, cc_overlay.TAB)
        self.assertEqual(app.layer.geometry, self.TAB_GEOM)
        self.assertEqual(app.ui.showing, "tab")
        t = app.ui.targets()
        self.assertEqual(sorted(t), ["overlay.tab"])
        x, y, w, h = t["overlay.tab"]
        self.assertLessEqual(x + w, cc_overlay.TAB_W)
        self.assertLessEqual(y + h, cc_overlay.TAB_H)

    def test_tap_opens_full_with_only_game_screen_tiles(self):
        app = self.make_overlay()
        self.tap(app, "overlay.tab")
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)
        self.assertEqual(app.mode, FULL)
        self.assertEqual(app.layer.geometry, (Wl.ANCHOR_ALL, (0, 0), 0))
        self.size(app)
        t = app.ui.targets()
        for name in ("home.mixer", "home.hud", swap_ui.SWAP_TILE_NAME, "bar.slider"):
            self.assertIn(name, t)
        for name in ("home.browser", "home.youtube", "home.discord", "home.settings"):
            self.assertNotIn(name, t)
        self.tap(app, "home.close")
        self.assertEqual(app.mode, cc_overlay.TAB)
        self.assertEqual(app.layer.geometry, self.TAB_GEOM)

    def test_a_short_drag_on_the_tab_opens_it(self):
        app = self.make_overlay()
        x, y, w, h = app.ui.targets()["overlay.tab"]
        self.swipe(app, x + w / 2, 10, x + w / 2, 10 + 40)
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)

    def test_tab_off_means_nothing_mapped_while_closed(self):
        app = self.make_overlay(cfg={"schema_version": 1,
                                     "command_center": {"game_screen_tab": False}})
        self.assertEqual(app.mode, HIDDEN)
        self.assertTrue(app.layer.hidden)

    def test_tab_setting_followed_live(self):
        app = self.make_overlay()
        self.write_cfg({"schema_version": 1, "command_center": {"game_screen_tab": False}})
        app.cfg_sig = ("force", 0, 0)
        app._config_poll()
        self.assertEqual(app.mode, HIDDEN)
        self.write_cfg({"schema_version": 1, "command_center": {"game_screen_tab": True}})
        app.cfg_sig = ("force", 1, 0)
        app._config_poll()
        self.assertEqual(app.mode, cc_overlay.TAB)

    def test_back_opens_here_only_when_the_panel_cannot(self):
        app = self.make_overlay()
        self.write_peer("state.json", {"mode": FULL})
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMPANION, "panel is FULL: its own CC opens")
        self.write_peer("state.json", {"mode": HIDDEN})
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER, "DS game owns the panel's screen")
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMPANION, "Back closes it again")

    def test_back_opens_here_when_the_panel_is_not_running(self):
        app = self.make_overlay()
        app.on_summon_button(summon_ev())                       # no state file at all
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)
        app.on_summon_button(summon_ev())
        self.write_peer("state.json", {"mode": FULL})
        screen_swap.pid_alive = lambda pid: False               # crashed, stale file
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)

    def test_follows_es_to_the_other_panel(self):
        app = self.make_overlay()
        app.on_placement(SWAPPED)
        self.assertEqual(app.layer.bound, ["DSI-1"])
        self.assertEqual(app.output, "DSI-1")
        self.assertEqual(app.mode, cc_overlay.TAB)

    def test_undocked_hides_and_redock_brings_the_tab_back(self):
        app = self.make_overlay()
        self.tap(app, "overlay.tab")
        app.on_placement(UNDOCKED)
        self.assertEqual(app.output, "DSI-1")
        self.assertEqual(app.pull.state, PULL.COMPANION)
        self.assertEqual(app.mode, HIDDEN)
        app.on_summon_button(summon_ev())
        self.assertEqual(app.pull.state, PULL.COMPANION)         # nothing to open undocked
        app.on_placement(NORMAL)
        self.assertEqual(app.output, "DP-1")
        self.assertEqual(app.mode, cc_overlay.TAB)

    def test_swap_from_the_game_screen(self):
        app = self.make_overlay()
        self.tap(app, "overlay.tab")
        self.size(app)
        self.tap(app, swap_ui.SWAP_TILE_NAME)
        self.tap(app, "confirm.yes")
        self.assertEqual(self.disk()["screens"]["es_screen"], "builtin_bottom")
        self.assertEqual(app.mode, cc_overlay.TAB)

    def test_state_file_is_its_own(self):
        app = self.make_overlay()
        app.write_state()
        p = os.path.join(os.environ["RP5DECK_RUN_DIR"], "overlay", "state.json")
        with open(p) as f:
            st = json.load(f)
        self.assertEqual(st["screens"]["role"], "overlay")
        self.assertFalse(os.path.exists(os.path.join(os.environ["RP5DECK_RUN_DIR"],
                                                     "state.json")))


# ---------------------------------------------------------------------------
# the 094 overlay loop (real processes: WSL / Linux)
# ---------------------------------------------------------------------------
import test_supervisor as tsup  # noqa: E402


def tearDownModule():
    # these classes run from test_sw1_merged/test_cc5_merged, but unittest
    # looks the module fixture up on each class's own module: this one
    tsup.assert_no_leftovers()


class TestSupervisorOverlayLoop(unittest.TestCase):
    def setUp(self):
        if not tsup.LINUX and not tsup._which_wsl():
            self.skipTest("no WSL and not on Linux")

    def harness(self, body, timeout=40, overlay=True):
        sup = tsup.wslify(os.path.join(os.path.dirname(HERE), "command-center-app"))
        fix = lambda n: tsup.wslify(os.path.join(tsup.FIX, n))
        ov = 'export RP5DECK_OVERLAY="$D/home/overlay_stub.sh"' if overlay else \
            'export RP5DECK_OVERLAY="$D/home/nope.py"'
        script = r"""
set -u
# teardown (group kill + leak check) is tsup.run_harness's; see test_supervisor
D=$(mktemp -d "/tmp/$RP5DECK_TEST_TAG.XXXXXX")
mkdir -p "$D/bin" "$D/home" "$D/log"
cp '%(py3)s' "$D/bin/python3"; chmod +x "$D/bin/python3"
cp '%(sway)s' "$D/bin/swaymsg"; chmod +x "$D/bin/swaymsg"
for n in app guard overlay; do cp '%(child)s' "$D/home/${n}_stub.sh"; chmod +x "$D/home/${n}_stub.sh"; done
export PATH="$D/bin:$PATH"
touch "$D/sway-ipc.999.sock"; export SWAYSOCK="$D/sway-ipc.999.sock"
# I2: only THIS harness's sockets count (094 would otherwise search all of /run /tmp)
export RP5DECK_SWAY_SOCK_DIRS="$D"
export RP5DECK_HOME="$D/home" RP5DECK_MAIN="$D/home/app_stub.sh" RP5DECK_GUARD="$D/home/guard_stub.sh"
%(ov)s
export RP5DECK_LOG_DIR="$D/log" RP5DECK_LOCK="$D/094.pid" RP5DECK_DISABLE="$D/.disable-rp5deck"
export RP5DECK_GUARD_DISABLE="$D/.disable-guard" RP5DECK_OVERLAY_DISABLE="$D/.disable-overlay"
export RP5DECK_FLAG_FILE="$D/flag"
export RP5DECK_RAPID_SECS=1 RP5DECK_RAPID_MAX=3 RP5DECK_BACKOFF_MAX=1
APP_STUB="$D/home/app_stub.sh"; OV_STUB="$D/home/overlay_stub.sh"
LOCK="$RP5DECK_LOCK"; LOG="$D/log/command-center-app.log"
starts() { cat "$1.starts" 2>/dev/null || echo 0; }
wait_for() { i=0; while [ "$i" -lt "$2" ]; do if eval "$1"; then return 0; fi; i=$((i + 1)); sleep 0.1; done; return 1; }
kill_stub_now() { p=$(pgrep -f "$1" | head -1); [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; }
rm -f "$LOCK"
bash '%(sup)s' >"$D/stdout.log" 2>&1 &
wait_for '[ -s "$LOCK" ]' 100 || echo "LAUNCH_FAILED=yes"
SUPPID=$(cat "$LOCK")
%(body)s
""" % {"py3": fix("supervisor-stub-python3-SYNTHETIC.sh"),
       "sway": fix("supervisor-stub-swaymsg-SYNTHETIC.sh"),
       "child": fix("supervisor-stub-child-SYNTHETIC.sh"), "sup": sup, "ov": ov, "body": body}
        r = tsup.run_harness(self, script, timeout=timeout)
        if r is None:
            self.skipTest("no WSL")
        return r

    def test_overlay_starts_with_the_app(self):
        r = self.harness(r"""
wait_for '[ "$(starts "$OV_STUB")" -ge 1 ] && [ "$(starts "$APP_STUB")" -ge 1 ]' 100
echo "OV=$(starts "$OV_STUB") APP=$(starts "$APP_STUB")"
""")
        self.assertIn("OV=1 APP=1", r.stdout, r.stdout + r.stderr)

    def test_overlay_giving_up_keeps_the_app(self):
        r = self.harness(r"""
last=0; n=0
while [ "$n" -lt 15 ]; do
    n=$((n + 1))
    wait_for "[ \"\$(starts \"\$OV_STUB\")\" -gt $last ]" 50 || break
    last=$(starts "$OV_STUB"); kill_stub_now "$OV_STUB"
    grep -q "giving up on the game-screen Command Center" "$LOG" 2>/dev/null && break
done
wait_for 'grep -q "giving up on the game-screen" "$LOG" 2>/dev/null' 50
echo "WARNED=$(grep -q "WARNING: giving up on the game-screen Command Center" "$LOG" && echo yes || echo no)"
echo "SUP_ALIVE=$(kill -0 "$SUPPID" 2>/dev/null && echo yes || echo no)"
echo "APP_ALIVE=$(pgrep -f "$APP_STUB" >/dev/null && echo yes || echo no)"
""", timeout=60)
        self.assertIn("WARNED=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("SUP_ALIVE=yes", r.stdout)
        self.assertIn("APP_ALIVE=yes", r.stdout)

    def test_clean_stop_takes_the_overlay_down(self):
        r = self.harness(r"""
wait_for '[ "$(starts "$OV_STUB")" -ge 1 ]' 100
OV_PID=$(pgrep -f "$OV_STUB" | head -1)
kill -TERM "$SUPPID"
wait_for '! kill -0 "$OV_PID" 2>/dev/null' 100
echo "OV_GONE=$(kill -0 "$OV_PID" 2>/dev/null && echo no || echo yes)"
""")
        self.assertIn("OV_GONE=yes", r.stdout, r.stdout + r.stderr)

    def test_overlay_kill_switch_stops_only_the_overlay(self):
        r = self.harness(r"""
wait_for '[ "$(starts "$OV_STUB")" -ge 1 ]' 100
: > "$RP5DECK_OVERLAY_DISABLE"
kill_stub_now "$OV_STUB"
wait_for 'grep -q "overlay: kill switch present (overlay only)" "$LOG" 2>/dev/null' 50
sleep 1
echo "OV_STARTS=$(starts "$OV_STUB")"
echo "APP_ALIVE=$(pgrep -f "$APP_STUB" >/dev/null && echo yes || echo no)"
""")
        self.assertIn("OV_STARTS=1", r.stdout, r.stdout + r.stderr)
        self.assertIn("APP_ALIVE=yes", r.stdout)

    def test_no_overlay_file_no_overlay_loop(self):
        r = self.harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ]' 100
sleep 0.5
grep -q "no Command Center on the game screen" "$LOG" && echo "NOTED=yes"
echo "LOOP=$(pgrep -f -- "--overlay-loo[p]" >/dev/null && echo yes || echo no)"
""", overlay=False)
        self.assertIn("NOTED=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("LOOP=no", r.stdout)


if __name__ == "__main__":
    unittest.main()
