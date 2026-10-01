#!/usr/bin/env python3
"""CC5: the Command Center over an emulator's second screen (hidden_overlay.py).

The owner: the Dual Screen Add-on's shell covers the volume keys, so while a
DS / 3DS / Wii U game owns the panel (rp5deck HIDDEN) the Command Center must
still open, for volume. What is proved here, on the REAL main.App (no SDL:
the headless harness from test_companion, audio in dry-run):

  * summon works in HIDDEN: Back opens OVERLAY - the panel's own surface,
    full panel, moved to the OVERLAY layer BEFORE it is mapped (above a
    fullscreen emulator window), showing the volume strip first;
  * OVERLAY never steals focus: no new surface, no keyboard interactivity
    other than NONE anywhere (the handle too), no sway command;
  * every dismiss (Close, Back again, swipe up, timeout) goes back to HIDDEN
    through layer.hide(), i.e. the emulator gets its touch screen back;
  * only an emulator window's HIDDEN counts (undocked / stale reasons are
    refused), and the reasons come from sway_ipc.compute_mode() itself;
  * the FX-A slider semantics hold in OVERLAY (no jump, cancel on swipe,
    one commit on release);
  * the optional corner handle: mapped only in HIDDEN-by-window, keyboard
    NONE, OVERLAY layer, exclusive zone 0; a tap opens the overlay.

wl_layer.set_layer is checked on the wire (recording fake of marshal, like
test_wl_layer_rebind) where libwayland-client loads (WSL / the device).
RP5DECK_RENDER_DIR=<dir> writes PC renders (cairo: WSL / the device).
"""
import argparse
import json
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import config  # noqa: E402
import hidden_overlay  # noqa: E402
import main  # noqa: E402
import screens  # noqa: E402
import summon  # noqa: E402
import sway_ipc  # noqa: E402
from hidden_overlay import OVERLAY  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN  # noqa: E402
from test_companion import (AppCase, FakeLayer, FakeResolver, FakeVideo, SyncWorker,  # noqa: E402
                            INFO_A, INFO_B, ROM_A, ROM_B, W, H, fake_load_image)

PULL = summon.PullDownStateMachine
LAYER_TOP, LAYER_OVERLAY, KEYBOARD_NONE = 2, 3, 0
WINDOW_REASON = "foreign window on DSI-1: [w2] melonDS"


# ---------------------------------------------------------------------------
# sway trees -> the reasons compute_mode() really produces
# ---------------------------------------------------------------------------
def tree(outputs):
    """outputs: {name: [app_id or title, ...]} -> a minimal get_tree reply."""
    nodes = []
    for i, (name, wins) in enumerate(outputs.items()):
        views = [{"type": "con", "id": 100 * i + k, "pid": 10 + k,
                  "app_id": w if not w.startswith("[") else None, "name": w, "nodes": []}
                 for k, w in enumerate(wins)]
        ws = {"type": "workspace", "name": str(i + 1), "nodes": views, "floating_nodes": []}
        nodes.append({"type": "output", "name": name, "current_workspace": str(i + 1),
                      "nodes": [ws]})
    return {"type": "root", "nodes": nodes}


def reason_of(outputs):
    return sway_ipc.compute_mode(tree(outputs), internal="DSI-1", external="DP-1")


class TestHiddenCause(unittest.TestCase):
    """The overlay keys off compute_mode()'s reason text. These reasons are
    produced by compute_mode itself, so a reworded reason turns this red
    instead of silently disabling (or wrongly enabling) the overlay."""

    def test_an_emulator_window_on_the_panel_is_window(self):
        mode, why = reason_of({"DP-1": ["[w1] melonDS"], "DSI-1": ["[w2] melonDS"]})
        self.assertEqual(mode, HIDDEN)
        self.assertEqual(hidden_overlay.hidden_cause(why), "window")

    def test_undocked_and_absent_are_not_window(self):
        mode, why = reason_of({"DSI-1": ["[w2] melonDS"]})
        self.assertEqual((mode, hidden_overlay.hidden_cause(why)), (HIDDEN, "undocked"))
        mode, why = reason_of({"DP-1": ["emulationstation"]})
        self.assertEqual((mode, hidden_overlay.hidden_cause(why)), (HIDDEN, "absent"))

    def test_full_and_bar_reasons_have_no_hidden_cause(self):
        for outs in ({"DP-1": [], "DSI-1": []}, {"DP-1": [], "DSI-1": ["rp5deck-web"]}):
            mode, why = reason_of(outs)
            self.assertIn(mode, (FULL, BAR))
            self.assertIsNone(hidden_overlay.hidden_cause(why))


class TestPureHelpers(unittest.TestCase):
    def test_settings_default_when_absent_or_invalid(self):
        cfg = config.defaults()
        self.assertTrue(hidden_overlay.setting_overlay_on_hidden(cfg))
        self.assertEqual(hidden_overlay.setting_corner_handle(cfg), "off")
        cfg["command_center"]["overlay_on_hidden"] = "yes"
        cfg["command_center"]["corner_handle"] = "middle"
        self.assertTrue(hidden_overlay.setting_overlay_on_hidden(cfg))
        self.assertEqual(hidden_overlay.setting_corner_handle(cfg), "off")
        cfg["command_center"]["overlay_on_hidden"] = False
        cfg["command_center"]["corner_handle"] = "bottom-left"
        self.assertFalse(hidden_overlay.setting_overlay_on_hidden(cfg))
        self.assertEqual(hidden_overlay.setting_corner_handle(cfg), "bottom-left")

    def test_corner_anchor(self):
        wl = FakeWl2()
        self.assertEqual(hidden_overlay.corner_anchor("top-left", wl), 1 | 4)
        self.assertEqual(hidden_overlay.corner_anchor("bottom-right", wl), 2 | 8)
        self.assertIsNone(hidden_overlay.corner_anchor("off", wl))

    def test_level_changed(self):
        a = {"state": "ok", "volume": 0.4, "muted": False}
        self.assertFalse(hidden_overlay.level_changed(None, a))
        self.assertFalse(hidden_overlay.level_changed(a, dict(a)))
        self.assertTrue(hidden_overlay.level_changed(a, dict(a, volume=0.45)))
        self.assertTrue(hidden_overlay.level_changed(a, dict(a, muted=True)))
        self.assertFalse(hidden_overlay.level_changed(a, {"state": "error"}))


class TestPullOpen(unittest.TestCase):
    def test_open_ignores_the_swipe_and_button_settings(self):
        seen = []
        p = PULL(on_change=lambda *a: seen.append(a), swipe_down_enabled=False,
                 hardware_button="none")
        p.summon_button()
        p.swipe_down_from_top()
        self.assertEqual(p.state, PULL.COMPANION)
        p.open("cc5")
        self.assertEqual(seen, [(PULL.COMPANION, PULL.COMMAND_CENTER, "cc5")])
        p.open("again")
        self.assertEqual(len(seen), 1)

    def test_overlay_mode_does_not_reset_the_pull_down(self):
        p = PULL()
        p.open("x")
        p.mode_changed(OVERLAY)
        self.assertEqual(p.state, PULL.COMMAND_CENTER)
        p.mode_changed(HIDDEN)
        self.assertEqual(p.state, PULL.COMPANION)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeWl2:
    ANCHOR_TOP, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT, ANCHOR_ALL = 1, 2, 4, 8, 15
    LAYER_TOP, LAYER_OVERLAY = LAYER_TOP, LAYER_OVERLAY
    KEYBOARD_NONE = KEYBOARD_NONE


class LayerRec(FakeLayer):
    """FakeLayer + set_layer, and the keyboard interactivity the real
    LayerSurface re-asserts on every show/set_geometry."""

    def __init__(self):
        FakeLayer.__init__(self)
        self.layer = LAYER_TOP
        self.keyboard = KEYBOARD_NONE
        self.maps = []                  # (layer, keyboard, anchor) at every show/set_geometry

    def set_layer(self, layer):
        self.calls.append(("set_layer", layer))
        self.layer = layer

    def show(self, anchor, size, zone):
        FakeLayer.show(self, anchor, size, zone)
        self.maps.append((self.layer, self.keyboard, anchor))

    def set_geometry(self, anchor, size, zone):
        FakeLayer.set_geometry(self, anchor, size, zone)
        self.maps.append((self.layer, self.keyboard, anchor))


class FakeHandle:
    window_id = 77

    def __init__(self, app, output):
        self.output = output
        self.visible = False
        self.corner = None
        self.level = None
        self.calls = []

    def show(self, corner, output):
        self.visible, self.corner, self.output = True, corner, output
        self.calls.append(("show", corner, output))

    def hide(self):
        self.visible = False
        self.calls.append("hide")

    def flash(self, level):
        self.level = level

    def on_input(self, etype, fid):
        return etype == "up" and self.visible

    def render(self):
        pass

    def state(self):
        return {"corner": self.corner}

    def destroy(self):
        self.calls.append("destroy")


class Harness(AppCase):
    def make_cc5_app(self, cfg_patch=None, query=None, peer=None, handle=True, mode=HIDDEN,
                     reason=WINDOW_REASON):
        if cfg_patch:
            with open(os.environ["RP5DECK_CONFIG"], "w") as f:
                json.dump(cfg_patch, f)
        app = main.App(argparse.Namespace(seconds=0, output=None))
        app.title = "Test Device Command Center"
        app.layer = LayerRec()
        app.wl = FakeWl2()
        app.mode = FULL
        app.video = FakeVideo()
        for name in ("media_worker", "io_worker", "audio_worker", "web_worker", "search_worker"):
            setattr(app, name, SyncWorker(app.post))
        self.queries = []
        self.query_result = (HIDDEN, reason)

        def q():
            self.queries.append(1)
            if isinstance(self.query_result, Exception):
                raise self.query_result
            return self.query_result
        self.handles = []

        def factory(a, output):
            h = FakeHandle(a, output)
            self.handles.append(h)
            return h
        self.peer = peer
        app.cc5_parts = {"query": query or q, "peer_state": lambda: self.peer}
        if handle:
            app.cc5_parts["handle_factory"] = factory
        app.build_ui(W, H)
        app.companion.resolver = FakeResolver({ROM_A: INFO_A, ROM_B: INFO_B})
        app.companion.load_image = fake_load_image
        app.backend.get_master = lambda: {"state": "ok", "volume": 0.40, "muted": False,
                                          "sink_description": "Speaker"}
        app.summon_reader = object()        # a reader is running (binding btn_back_f1)
        app.apply_master(app.backend.get_master())
        app.post.drain()
        if mode != FULL:
            app.on_mode(mode, reason)
            app.post.drain()
        return app

    def back(self, app):
        app.on_summon_button(summon.SummonEvent("btn_back_f1", summon.KEYBOARD_NAME,
                                                "/dev/input/event8", summon.KEY_F1_CODE, 1,
                                                0.0, 0, 0))
        app.post.drain()

    def open_overlay(self, app):
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)

    def assert_hidden_unmapped(self, app):
        self.assertEqual(app.mode, HIDDEN)
        self.assertTrue(app.layer.hidden)
        self.assertEqual(app.layer.calls[-1], "hide")
        self.assertEqual(app.pull.state, PULL.COMPANION)
        self.assertFalse(app.ui.home.overlay)


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------
class TestSummonInHidden(Harness):
    def test_back_in_hidden_opens_the_overlay_volume_first(self):
        app = self.make_cc5_app()
        self.assertTrue(app.layer.hidden)
        n = len(app.layer.calls)
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)
        self.assertEqual(self.queries, [1])             # the tree was re-read once
        # the SAME surface, full panel, on the OVERLAY layer before it is mapped
        new = app.layer.calls[n:]
        self.assertEqual(new[0], ("set_layer", LAYER_OVERLAY))
        self.assertEqual(new[1], ("show", 15, (0, 0), 0))
        self.assertEqual(app.layer.maps[-1], (LAYER_OVERLAY, KEYBOARD_NONE, 15))
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)
        self.assertEqual(app.ui.showing, "cc")
        t = app.ui.targets()
        for name in ("bar.slider", "bar.mute", "home.mixer", "home.hud", "home.overlay_close",
                     "home.close"):
            self.assertIn(name, t)
        for name in ("home.browser", "home.youtube", "home.discord", "home.settings"):
            self.assertNotIn(name, t)
        # volume first: the strip is the top row
        self.assertEqual(t["bar.slider"][1] < t["home.mixer"][1], True)
        self.assertLess(t["bar.slider"][1], 140)
        self.assertEqual(app.ui.bar.slider.value, 0.40)
        self.assertFalse(app.companion.active)          # no video under the overlay
        json.dumps(app.state(), default=str)
        st = app.state()["cc5"]
        self.assertTrue(st["open"])
        self.assertTrue(st["takes_summon"])
        self.assertEqual(app.state()["mode"], OVERLAY)

    def test_the_overlay_opens_no_surface_and_asks_for_no_keyboard(self):
        app = self.make_cc5_app(handle=False)
        self.open_overlay(app)
        self.assertEqual(app.state()["cc5"]["handle"]["visible"], False)
        for layer, keyboard, anchor in app.layer.maps:
            self.assertEqual(keyboard, KEYBOARD_NONE)
        # nothing in the overlay path can talk to sway except the read-only query
        self.assertTrue(sway_ipc.RUN_COMMAND not in sway_ipc.READ_ONLY_TYPES)

    def test_undocked_hidden_opens_the_overlay(self):
        app = self.make_cc5_app()
        self.query_result = (HIDDEN, "undocked: DP-1 absent")
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)

    def test_panel_output_absent_is_refused(self):
        app = self.make_cc5_app()
        self.query_result = (HIDDEN, "DSI-1 absent")
        self.back(app)
        self.assertEqual(app.mode, HIDDEN)
        self.assertTrue(app.layer.hidden)
        self.assertNotIn(("set_layer", LAYER_OVERLAY), app.layer.calls)
        self.assertIn("not an emulator window", app.cc5.last_refusal)

    def test_a_stale_watcher_reason_is_rechecked(self):
        # the watcher said "window" when the game started; the panel's output
        # went away since, so no new reason was ever reported
        app = self.make_cc5_app()
        self.query_result = (HIDDEN, "DSI-1 absent")
        self.back(app)
        self.assertEqual(app.mode, HIDDEN)
        self.assertEqual(app.mode_reason, WINDOW_REASON)

    def test_a_failed_tree_read_falls_back_to_the_watchers_reason(self):
        app = self.make_cc5_app()
        self.query_result = RuntimeError("no sway socket")
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)

    def test_setting_off_leaves_hidden_alone(self):
        app = self.make_cc5_app({"command_center": {"overlay_on_hidden": False}})
        self.assertFalse(app.cc5.takes_summon())
        self.back(app)
        self.assertEqual(app.mode, HIDDEN)
        self.assertEqual(self.queries, [])
        self.assertIn("overlay_on_hidden", app.cc5.last_refusal)

    def test_the_game_screen_overlay_being_open_wins(self):
        app = self.make_cc5_app()
        self.peer = {"running": True, "pulldown": {"state": "command_center"}}
        self.back(app)
        self.assertEqual(app.mode, HIDDEN)
        self.assertIn("game-screen", app.cc5.last_refusal)

    def test_bar_and_full_keep_their_behaviour(self):
        app = self.make_cc5_app(mode=BAR, reason="rp5deck window on DSI-1: rp5deck-web")
        self.back(app)
        self.assertEqual(app.mode, BAR)
        self.assertEqual(app.pull.state, PULL.COMPANION)
        app2 = self.make_cc5_app(mode=FULL)
        self.back(app2)
        self.assertEqual((app2.mode, app2.pull.state), (FULL, PULL.COMMAND_CENTER))
        self.back(app2)
        self.assertEqual((app2.mode, app2.pull.state), (FULL, PULL.COMPANION))
        self.assertNotIn(("set_layer", LAYER_OVERLAY), app2.layer.calls)


# ---------------------------------------------------------------------------
# Closing: every way back is HIDDEN with the surface unmapped
# ---------------------------------------------------------------------------
class TestDismiss(Harness):
    def test_close_tile(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        self.tap(app, "home.overlay_close")
        self.assert_hidden_unmapped(app)
        self.assertEqual(app.cc5.dismissals, 1)
        self.assertNotIn("bar.slider", app.state()["targets"])

    def test_small_close_button(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        self.tap(app, "home.close")
        self.assert_hidden_unmapped(app)

    def test_back_again(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        self.back(app)
        self.assert_hidden_unmapped(app)

    def test_swipe_up(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        self.swipe(app, 960, 900, 960, 500)
        app.post.drain()
        self.assert_hidden_unmapped(app)

    def test_timeout_and_touch_restarts_it(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        self.assertEqual(app.cc5.timeout_s(), hidden_overlay.OVERLAY_TIMEOUT_S)
        self.run_timers(app, hidden_overlay.OVERLAY_TIMEOUT_S - 5)
        app._activity()                                  # a touch
        self.run_timers(app, hidden_overlay.OVERLAY_TIMEOUT_S - 2)
        self.assertEqual(app.mode, OVERLAY)
        self.run_timers(app, hidden_overlay.OVERLAY_TIMEOUT_S + 1)
        self.assert_hidden_unmapped(app)

    def test_timeout_waits_for_a_finger_on_the_slider(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        s = app.ui.bar.slider
        app.router.down(("f", 3), s.x_for(s.value), s.rect[1] + s.rect[3] / 2)
        self.run_timers(app, hidden_overlay.OVERLAY_TIMEOUT_S + 1)
        self.assertEqual(app.mode, OVERLAY)
        app.router.up(("f", 3), s.x_for(s.value), s.rect[1] + s.rect[3] / 2)
        self.run_timers(app, 2 * hidden_overlay.OVERLAY_TIMEOUT_S + 2)
        self.assertEqual(app.mode, HIDDEN)

    def test_the_command_center_timeout_setting_is_used_when_set(self):
        app = self.make_cc5_app({"command_center": {"auto_close_timeout_s": 10}})
        self.open_overlay(app)
        self.assertIsNone(app.cc5.timeout_s())
        for _ in range(12):
            app._cc_tick()
        app.pull.tick(time.monotonic() + 11)
        app.post.drain()
        self.assert_hidden_unmapped(app)

    def test_reopen_after_close(self):
        app = self.make_cc5_app()
        for _ in range(3):
            self.open_overlay(app)
            self.back(app)
            self.assert_hidden_unmapped(app)
        self.assertEqual((app.cc5.opens, app.cc5.dismissals), (3, 3))


class TestModeChangesWhileOpen(Harness):
    def test_the_same_game_window_keeps_it_open(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        app.on_mode(HIDDEN, "foreign window on DSI-1: [w2] melonDS, Secondary")
        self.assertEqual(app.mode, OVERLAY)
        self.assertEqual(app.cc5.hidden_reason, "foreign window on DSI-1: [w2] melonDS, Secondary")

    def test_game_exit_goes_full_on_the_top_layer_with_the_companion(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        n = len(app.layer.calls)
        app.on_mode(FULL, "no window on DSI-1")
        self.assertEqual(app.mode, FULL)
        new = app.layer.calls[n:]
        self.assertEqual(new[0], ("set_layer", LAYER_TOP))
        self.assertEqual(new[1][0], "set_geometry")
        self.assertEqual(app.layer.maps[-1][:2], (LAYER_TOP, KEYBOARD_NONE))
        self.assertEqual(app.pull.state, PULL.COMPANION)
        self.assertEqual(app.ui.showing, "companion")
        self.assertFalse(app.ui.home.overlay)
        self.assertIn("home.settings", [t.name for t in app.ui.home.tiles if t.visible])

    def test_undock_while_open_hides(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        app.on_mode(HIDDEN, "undocked: DP-1 absent")
        self.assert_hidden_unmapped(app)
        self.assertEqual(app.cc5.ended, 1)

    def test_a_later_full_command_center_is_the_normal_one(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        self.back(app)
        app.on_mode(FULL, "no window on DSI-1")
        self.swipe(app, 700, 20, 700, 400)
        t = app.ui.targets()
        self.assertIn("home.hotkeys", t)            # the normal grid (batch 1: no Browser tile)
        self.assertNotIn("home.overlay_close", t)


# ---------------------------------------------------------------------------
# FX-A slider semantics in OVERLAY, through the existing audio path
# ---------------------------------------------------------------------------
class TestVolumeInOverlay(Harness):
    def calls(self, app, n):
        return list(app.backend.calls)[n:]

    def test_touch_down_does_not_jump_and_a_drag_commits_once(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        s = app.ui.bar.slider
        n = len(app.backend.calls)
        span = s.track()[1] - s.track()[0]
        x0, cy = s.x_for(0.9), s.rect[1] + s.rect[3] / 2   # far from the knob at 0.40
        app.router.down(("f", 1), x0, cy)
        self.assertEqual(s.value, 0.40)
        for k in range(1, 6):
            app.router.move(("f", 1), x0 + k * 0.04 * span, cy)
        app.router.up(("f", 1), x0 + 0.20 * span, cy)
        app.post.drain()
        names = [c[0] for c in self.calls(app, n)]
        self.assertEqual(names.count("commit_master"), 1)
        self.assertEqual(names[-1], "commit_master")
        self.assertAlmostEqual(app.last_commit, 0.60, places=6)
        self.assertEqual(app.mode, OVERLAY)

    def test_a_drag_that_becomes_a_swipe_restores_and_closes(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        s = app.ui.bar.slider
        n = len(app.backend.calls)
        x0, cy = s.x_for(0.40), s.rect[1] + s.rect[3] - 5
        app.router.down(("f", 2), x0, cy)
        app.router.move(("f", 2), x0 + 60, cy)            # engaged: one live set
        app.router.move(("f", 2), x0 + 70, cy - 300)      # ...then it becomes a swipe up
        self.assertEqual(app.mode, HIDDEN)                # the swipe closed the overlay
        app.router.up(("f", 2), x0 + 70, cy - 300)
        app.post.drain()
        calls = self.calls(app, n)
        self.assertNotIn("commit_master", [c[0] for c in calls])
        self.assertEqual(calls[-1], ("set_master", "0.4000"))  # put back, live only
        self.assertEqual(s.value, 0.40)

    def test_closing_with_a_finger_on_the_slider_commits_nothing(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        s = app.ui.bar.slider
        n = len(app.backend.calls)
        x0, cy = s.x_for(0.40), s.rect[1] + s.rect[3] / 2
        app.router.down(("f", 5), x0, cy)
        app.router.move(("f", 5), x0 + 200, cy)
        self.back(app)                                    # Back while dragging
        calls = self.calls(app, n)
        self.assertNotIn("commit_master", [c[0] for c in calls])
        self.assertEqual(calls[-1], ("set_master", "0.4000"))
        self.assert_hidden_unmapped(app)

    def test_mute_and_mixer(self):
        app = self.make_cc5_app()
        app.backend.list_streams = lambda: [{"id": 41, "display_name": "melonDS",
                                             "volume": 0.5, "muted": False}]
        self.open_overlay(app)
        n = len(app.backend.calls)
        self.tap(app, "bar.mute")
        self.assertEqual(self.calls(app, n), [("toggle_master_mute", "")])
        self.tap(app, "home.mixer")
        self.assertEqual(app.ui.sheet, "mixer")
        self.assertIn("mixer.slider.41", app.ui.targets())
        self.tap(app, "mixer.back")
        self.assertEqual(app.ui.sheet, None)
        self.assertEqual(app.mode, OVERLAY)


# ---------------------------------------------------------------------------
# The corner handle
# ---------------------------------------------------------------------------
class TestCornerHandle(Harness):
    def test_off_by_default_no_handle_is_made(self):
        app = self.make_cc5_app()
        self.assertEqual(self.handles, [])
        self.assertIsNone(app.cc5.handle)

    def test_shown_in_hidden_by_a_window_or_undocked(self):
        app = self.make_cc5_app({"command_center": {"corner_handle": "top-right"}})
        self.assertEqual(len(self.handles), 1)
        h = self.handles[0]
        self.assertTrue(h.visible)
        self.assertEqual((h.corner, h.output), ("top-right", "DSI-1"))
        self.open_overlay(app)
        self.assertFalse(h.visible)                     # never over the overlay's own buttons
        self.back(app)
        self.assertTrue(h.visible)
        app.on_mode(FULL, "no window on DSI-1")
        self.assertFalse(h.visible)
        app.on_mode(HIDDEN, "undocked: DP-1 absent")
        self.assertTrue(h.visible)

    def test_a_tap_opens_the_overlay_even_with_the_button_path_off(self):
        app = self.make_cc5_app({"command_center": {"corner_handle": "bottom-left",
                                                    "overlay_on_hidden": False}})
        self.assertTrue(app.cc5.handle_event("down", 77, ("f", 1)))
        self.assertTrue(app.cc5.handle_event("up", 77, ("f", 1)))
        app.post.drain()
        self.assertEqual(app.mode, OVERLAY)
        self.assertFalse(app.cc5.handle_event("up", 5, ("f", 1)))   # another window: not ours

    def test_recheck_hides_it_when_the_panel_output_goes(self):
        app = self.make_cc5_app({"command_center": {"corner_handle": "top-left"}})
        h = self.handles[0]
        self.query_result = (HIDDEN, "DSI-1 absent")
        self.run_timers(app, hidden_overlay.HANDLE_RECHECK_S + 0.1)
        self.assertFalse(h.visible)
        self.assertIn("absent", app.cc5.handle_blocked)
        app.on_mode(FULL, "no window on DSI-1")
        app.on_mode(HIDDEN, WINDOW_REASON)             # the next game: back again
        self.assertTrue(h.visible)

    def test_volume_change_flashes_the_level(self):
        app = self.make_cc5_app({"command_center": {"corner_handle": "top-right"}})
        h = self.handles[0]
        app.apply_master({"state": "ok", "volume": 0.45, "muted": False})
        self.assertEqual(h.level, (0.45, False))
        self.run_timers(app, hidden_overlay.FLASH_S + 0.1)
        self.assertIsNone(h.level)

    def test_setting_it_live(self):
        # the schema entry arrives with patches/CC5-config.patch (live, not
        # restart); emulate it here - test_cc5_merged runs the real one
        app = self.make_cc5_app()
        self.assertEqual(self.handles, [])
        app.cfg["command_center"]["corner_handle"] = "top-left"
        real = config.field_for
        fake = {"key_path": hidden_overlay.KEY_CORNER_HANDLE, "restart": False,
                "default": "off"}
        with mock.patch.object(config, "field_for",
                               lambda k: fake if tuple(k) == fake["key_path"] else real(k)):
            app.on_setting(hidden_overlay.KEY_CORNER_HANDLE, "top-left")
        self.assertEqual(len(self.handles), 1)
        self.assertTrue(self.handles[0].visible)

    def test_a_handle_that_cannot_be_made_costs_nothing_else(self):
        app = self.make_cc5_app()

        def broken(a, output):
            raise RuntimeError("no second window")
        app.cc5.handle_factory = broken
        app.cfg["command_center"]["corner_handle"] = "top-left"
        app.cc5.config_changed()
        self.assertIn("no second window", app.cc5.handle_error)
        self.open_overlay(app)                           # Back still works


# ---------------------------------------------------------------------------
# SdlHandle against fake SDL / wl / gfx: the handle's surface parameters
# ---------------------------------------------------------------------------
class FakeSdl:
    EV_FINGER_DOWN, EV_FINGER_UP, EV_FINGER_MOTION, EV_FINGER_CANCELED = 0x700, 0x701, 0x702, 0x703
    EV_MOUSE_DOWN, EV_MOUSE_UP = 0x401, 0x402
    PIXELFORMAT_ARGB8888, TEXTUREACCESS_STREAMING = 1, 1

    class _Lib:
        class SDL_GetWindowID:
            restype = argtypes = None

            def __new__(cls, win):
                return 77
    lib = _Lib()

    def __init__(self):
        self.calls = []
        self.props = {}

    def __getattr__(self, name):
        def f(*a):
            self.calls.append((name,) + a)
            if name == "CreateWindowWithProperties":
                return 1001
            if name in ("CreateRenderer", "CreateTexture", "CreateProperties",
                        "GetWindowProperties", "GetPointerProperty"):
                return 5
            if name.startswith("Set") and name.endswith("Property"):
                self.props[a[1]] = a[2]
            return True
        return f

    def error(self):
        return ""


class FakeLayerSurface:
    made = []

    def __init__(self, display, surface, **kw):
        self.kw = kw
        self.hidden = False
        self.can_present = True
        self.geometry = (kw["anchor"], kw["size"], kw["exclusive_zone"])
        self.calls = []
        FakeLayerSurface.made.append(self)

    def create(self):
        self.calls.append("create")
        return self.kw["size"]

    def show(self, anchor, size, zone):
        self.hidden = False
        self.geometry = (anchor, size, zone)
        self.calls.append(("show", anchor, size, zone))

    def hide(self):
        self.hidden = True
        self.calls.append("hide")

    def presented(self):
        self.calls.append("presented")

    def rebind(self, out):
        self.calls.append(("rebind", out))

    def destroy(self):
        self.calls.append("destroy")


class FakeWlModule(FakeWl2):
    LayerSurface = FakeLayerSurface


class RecCanvas:
    def __init__(self, w, h):
        self.clip_rect = (0, 0, w, h)
        self.ops = []

    def clipped(self, r):
        import contextlib
        return contextlib.nullcontext()

    def __getattr__(self, name):
        return lambda *a, **k: self.ops.append(name)

    def pixels(self):
        return 0, 4 * hidden_overlay.HANDLE_PX

    def free(self):
        pass


class TestSdlHandle(unittest.TestCase):
    def make(self):
        class App:
            pass
        app = App()
        app.sdl, app.wl = FakeSdl(), FakeWlModule()
        app.gfx = type("G", (), {"Canvas": RecCanvas})
        app.layer = type("L", (), {"display": 99})()
        FakeLayerSurface.made = []
        h = hidden_overlay.SdlHandle(app, "DSI-1")
        return app, h, FakeLayerSurface.made[0]

    def test_surface_parameters_never_take_focus(self):
        app, h, ls = self.make()
        self.assertEqual(ls.kw["keyboard"], KEYBOARD_NONE)
        self.assertEqual(ls.kw["layer"], LAYER_OVERLAY)
        self.assertEqual(ls.kw["exclusive_zone"], 0)
        self.assertEqual(ls.kw["size"], (hidden_overlay.HANDLE_PX, hidden_overlay.HANDLE_PX))
        self.assertEqual(ls.kw["output_name"], "DSI-1")
        self.assertNotIn("Bottom", app.sdl.props[b"SDL.window.create.title"].decode())
        self.assertTrue(ls.hidden)                      # nothing mapped until it is wanted
        self.assertEqual(h.window_id, 77)

    def test_show_render_tap_hide_destroy(self):
        app, h, ls = self.make()
        h.show("bottom-right", "DSI-1")
        self.assertEqual(ls.calls[-1], ("show", 2 | 8, (96, 96), 0))
        h.render()
        self.assertEqual(ls.calls[-1], "presented")
        h.render()                                      # nothing changed: no present
        self.assertEqual(h.presents, 1)
        h.flash((0.5, False))
        h.render()
        self.assertEqual(h.presents, 2)
        self.assertFalse(h.on_input(app.sdl.EV_FINGER_UP, ("f", 1)))   # no down first
        h.on_input(app.sdl.EV_FINGER_DOWN, ("f", 1))
        self.assertTrue(h.on_input(app.sdl.EV_FINGER_UP, ("f", 1)))
        h.show("bottom-right", "DP-1")                  # SW1 moved the panel
        self.assertIn(("rebind", "DP-1"), ls.calls)
        h.hide()
        self.assertEqual(ls.calls[-1], "hide")
        h.destroy()
        self.assertIn("destroy", ls.calls)
        names = [c[0] for c in app.sdl.calls]
        self.assertLess(ls.calls.index("destroy"), len(ls.calls))
        self.assertEqual(names[-1], "DestroyWindow")    # the role went first, the window last


# ---------------------------------------------------------------------------
# wl_layer.set_layer on the wire (needs libwayland-client: WSL / the device)
# ---------------------------------------------------------------------------
try:
    import wl_layer  # noqa: E402
except OSError as e:          # no libwayland-client (Windows)
    wl_layer = None
    WHY = str(e)
else:
    WHY = ""


@unittest.skipIf(wl_layer is None, "libwayland-client not loadable here: %s" % WHY)
class TestSetLayerWire(unittest.TestCase):
    SURFACE, SHELL, PROXY = 11, 22, 33

    def setUp(self):
        self.calls = []
        self.saved = (wl_layer.marshal, wl_layer._wl, wl_layer._Listener)
        self.addCleanup(self._restore)
        test = self

        def marshal(proxy, opcode, args=(), new_iface=None, version=None, destroy=False):
            test.calls.append((proxy, opcode, list(args)))
            return 44 if new_iface is not None else None

        class FakeWl:
            def wl_display_roundtrip(self, d):
                if test.ls.proxy:
                    test.ls._on_configure(None, test.ls.proxy, 5, 1920, 1080)
                return 0

            def wl_proxy_get_version(self, p):
                return 4

            def wl_display_flush(self, d):
                return 0

            def wl_display_get_error(self, d):
                return 0
        wl_layer.marshal = marshal
        wl_layer._wl = FakeWl()
        wl_layer._Listener = lambda proxy, funcs: ("listener", proxy)
        ls = wl_layer.LayerSurface(display=1, surface=self.SURFACE, log=lambda m: None)
        ls.shell, ls.shell_version, ls.proxy = self.SHELL, 4, self.PROXY
        ls.configured, ls.has_buffer = True, True
        o = wl_layer._Output(1, 101, 4)
        o.name = "DSI-1"
        ls.outputs = {1: o}
        ls.output = o
        self.ls = ls

    def _restore(self):
        wl_layer.marshal, wl_layer._wl, wl_layer._Listener = self.saved

    def test_set_layer_then_show_maps_on_overlay_with_keyboard_none(self):
        ls = self.ls
        ls.hide()
        self.calls.clear()
        self.assertTrue(ls.set_layer(wl_layer.LAYER_OVERLAY))
        ls.show(wl_layer.ANCHOR_ALL, (0, 0), 0)
        ops = [(p, op, a) for p, op, a in self.calls]
        self.assertEqual(ops[0], (self.PROXY, 8, [('u', wl_layer.LAYER_OVERLAY)]))
        kb = [a for p, op, a in ops if p == self.PROXY and op == 4]
        self.assertEqual(kb, [[('u', wl_layer.KEYBOARD_NONE)]])
        commit = ops.index((self.SURFACE, 6, []))
        self.assertGreater(commit, 0)                   # set_layer precedes the commit
        self.assertTrue(ls.can_present)

    def test_same_layer_is_a_no_op_and_v1_refuses_without_recording(self):
        ls = self.ls
        self.assertFalse(ls.set_layer(wl_layer.LAYER_TOP))
        self.assertEqual(self.calls, [])
        ls.shell_version = 1
        self.assertFalse(ls.set_layer(wl_layer.LAYER_OVERLAY))
        self.assertEqual(self.calls, [])
        self.assertEqual(ls.layer, wl_layer.LAYER_TOP)

    def test_rebind_keeps_the_layer(self):
        ls = self.ls
        o2 = wl_layer._Output(2, 202, 4)
        o2.name = "DP-1"
        ls.outputs[2] = o2
        ls.set_layer(wl_layer.LAYER_OVERLAY)
        self.calls.clear()
        ls.rebind("DP-1")
        get = [a for p, op, a in self.calls if p == self.SHELL and op == 0]
        self.assertEqual(get[0][3], ('u', wl_layer.LAYER_OVERLAY))


# ---------------------------------------------------------------------------
# PC renders (cairo + pango: WSL / the device)
# ---------------------------------------------------------------------------
def _gfx():
    try:
        import gfx
        return gfx
    except Exception:           # noqa: BLE001 - no cairo/pango here (the Windows PC)
        return None


@unittest.skipUnless(_gfx(), "needs libcairo + libpango (WSL, the device)")
class TestRealRenderingCC5(Harness):
    def render(self, root_or_fn, w, h, name):
        import ctypes
        gfx = _gfx()
        canvas = gfx.Canvas(w, h)
        self.addCleanup(canvas.free)
        with canvas.clipped((0, 0, w, h)):
            root_or_fn(canvas)
        out = os.environ.get("RP5DECK_RENDER_DIR")
        if out:
            os.makedirs(out, exist_ok=True)
            f = gfx._cairo.cairo_surface_write_to_png
            f.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            canvas.pixels()
            self.assertEqual(f(canvas.surf, os.fsencode(
                os.path.join(out, "cc5-pc-render-%s.png" % name))), 0)
        data, stride = canvas.pixels()

        def px(x, y):
            return tuple(ctypes.string_at(data + int(y) * stride + int(x) * 4, 4))
        return px

    def test_overlay_home(self):
        app = self.make_cc5_app()
        self.open_overlay(app)
        px = self.render(app.ui.root.paint, W, H, "overlay-home")
        for x in range(0, W, 97):
            for y in range(0, H, 53):
                self.assertEqual(px(x, y)[3], 255, (x, y))       # opaque: covers the game
        s = app.ui.bar.slider
        kx, cy = s.x_for(s.value), s.rect[1] + s.rect[3] / 2
        self.assertGreater(sum(px(kx, cy)[:3]), 600)             # the white knob is drawn

    def test_overlay_mixer(self):
        app = self.make_cc5_app()
        app.backend.list_streams = lambda: [
            {"id": 41, "display_name": "melonDS", "volume": 0.8, "muted": False},
            {"id": 42, "display_name": "rp5deck companion", "volume": 0.3, "muted": True}]
        self.open_overlay(app)
        self.tap(app, "home.mixer")
        self.render(app.ui.root.paint, W, H, "overlay-mixer")

    def test_handle_icon_and_level(self):
        n = hidden_overlay.HANDLE_PX
        px = self.render(lambda g: hidden_overlay.paint_handle(g, n, n), n, n, "handle-icon")
        self.assertEqual(px(n / 2, n / 2)[3], 255)
        self.render(lambda g: hidden_overlay.paint_handle(g, n, n, (0.45, False)), n, n,
                    "handle-level")


if __name__ == "__main__":
    unittest.main()
