#!/usr/bin/env python3
"""CC6 cases that need the PATCHED tree (patches/CC6-main / -screens /
-web_tiles / -focus_guard): the real main.App with its app tabs, driving a
simulated sway (tests/cc6_sway_sim.py) through the real window_switcher
allow-list, with rp5deck's mode fed back from sway_ipc.compute_mode() on the
simulated tree - what ModeWatcher does on the device.

I2 (24 Sep): the patches are merged into the real files; tests/test_cc6_merged.py
runs this module against the working tree.

RP5DECK_RENDER_DIR=<dir> also writes PC renders of the tab strip (cairo:
WSL / the device): cc6-pc-render-*.png.
"""
import argparse
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import app_tabs  # noqa: E402
import focus_guard  # noqa: E402
import main  # noqa: E402
import screens  # noqa: E402
import summon  # noqa: E402
import sway_ipc  # noqa: E402
import window_switcher as wsw  # noqa: E402
from cc6_sway_sim import Sim  # noqa: E402
from hidden_overlay import OVERLAY  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN  # noqa: E402
from test_cc5_overlay import FakeWl2, LayerRec  # noqa: E402
from test_companion import (AppCase, FakeResolver, FakeVideo, SyncWorker, INFO_A, INFO_B,  # noqa: E402
                            ROM_A, ROM_B, W, H, fake_load_image)
from test_web_tiles import FakeBrowser, FakeKeyboard, Facts  # noqa: E402

PULL = summon.PullDownStateMachine


class Case(AppCase):
    def make(self, sim, avail=None, cc="DSI-1", es="DP-1", connect=None):
        app = main.App(argparse.Namespace(seconds=0, output=None))
        app.title = "Retroid Pocket 5 Command Center"
        app.layer = LayerRec()
        app.wl = FakeWl2()
        app.mode = FULL
        app.video = FakeVideo()
        app.output, app.es_output = cc, es
        for name in ("media_worker", "io_worker", "audio_worker", "web_worker", "search_worker"):
            setattr(app, name, SyncWorker(app.post))
        self.sim = sim
        self.br, self.kb = FakeBrowser(), FakeKeyboard()
        app.web_parts = {"browser": self.br, "keyboard": self.kb, "facts": Facts()}
        self.ytbr = FakeBrowser()
        app.ytapp_parts = {"browser": self.ytbr}
        app.cc5_parts = {"query": lambda: sway_ipc.compute_mode(sim.tree(), internal=app.output,
                                                                external=app.es_output),
                         "peer_state": lambda: None}
        self.switcher = wsw.Switcher(connect=connect or sim.connection, log_fn=lambda m: None)
        app.tabs_parts = {"switcher": self.switcher,
                          "avail": lambda: dict(avail or {"web": True, "yt": True})}
        app.build_ui(W, H)
        app.companion.resolver = FakeResolver({ROM_A: INFO_A, ROM_B: INFO_B})
        app.companion.load_image = fake_load_image
        app.backend.get_master = lambda: {"state": "ok", "volume": 0.4, "muted": False,
                                          "sink_description": "Speaker"}
        app.summon_reader = object()
        app.apply_master(app.backend.get_master())
        app.post.drain()
        self.sync(app)
        return app

    def sync(self, app):
        """ModeWatcher + the layer configure: the mode sway's tree gives now."""
        m, why = sway_ipc.compute_mode(self.sim.tree(), internal=app.output,
                                       external=app.es_output)
        app.on_mode(m, why)
        app.ui.set_size(W, screens.BAR_H if app.mode == BAR else H)
        app.post.drain()
        return app.mode

    def back(self, app):
        app.on_summon_button(summon.SummonEvent("btn_back_f1", summon.KEYBOARD_NAME,
                                                "/dev/input/event8", summon.KEY_F1_CODE, 1,
                                                0.0, 0, 0))
        app.post.drain()

    def open_cc(self, app):
        app.open_command_center()
        app.post.drain()
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)

    def tabs(self, app):
        return {t["id"]: t for t in app.tabs.strip.tabs}

    def active(self, app):
        return [i for i, t in self.tabs(app).items() if t["active"]]

    def focused(self):
        return self.sim.focus[1] if self.sim.focus and self.sim.focus[0] == "con" else self.sim.focus


def ds_sim(**k):
    s = Sim(**k)
    w1 = s.map("DP-1", title="[w1] melonDS 1.1", fullscreen=True, focus=True)
    w2 = s.map("DSI-1", title="[w2] melonDS 1.1")
    return s, w1, w2


class TestStripInTheCommandCenter(Case):
    def test_strip_sits_under_the_volume_strip_and_moves_the_content_down(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        t = app.ui.targets()
        for name in ("tabs.companion", "tabs.hud", "tabs.browser", "tabs.discord",
                     "tabs.ytapp"):
            self.assertIn(name, t)
            self.assertEqual(t[name][1], screens.BAR_H + 5, name)      # the row under the bar
        self.assertGreaterEqual(app.ui.home.rect[1], screens.BAR_H + app_tabs.TABS_H)
        self.assertGreaterEqual(t["home.mixer"][1], screens.BAR_H + app_tabs.TABS_H)
        self.assertEqual(self.active(app), [])                          # home: nothing "shown"
        app.open_hud()
        app.tabs.rebuild()
        self.assertEqual(self.active(app), ["hud"])

    def test_hidden_in_the_settings_sheet_and_in_bar_mode(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        app.open_settings()
        app.post.drain()
        self.assertNotIn("tabs.hud", app.ui.targets())
        app.close_settings()
        self.assertIn("tabs.hud", app.ui.targets())
        self.tap(app, "home.browser")
        s.map("DSI-1", app_id="rp5deck-web")
        self.assertEqual(self.sync(app), BAR)
        t = app.ui.targets()
        self.assertNotIn("tabs.hud", t)
        self.assertIn("bar.tabs", t)                                    # the strip's Tabs button

    def test_strip_over_home_and_hud_only_other_sheets_keep_their_full_area(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        app.open_hud()
        self.assertIn("tabs.companion", app.ui.targets())
        self.assertEqual(app.ui.hud.rect[1], screens.BAR_H + app_tabs.TABS_H)
        app.close_sheet()
        app.open_mixer()
        app.post.drain()
        self.assertNotIn("tabs.companion", app.ui.targets())
        self.assertEqual(app.ui.mixer.rect[1], screens.BAR_H)          # as before CC6
    def test_tabs_only_for_what_exists_or_can_start(self):
        s, w1, w2 = ds_sim()
        app = self.make(s, avail={"web": False, "yt": False})
        app.tabs.refresh()
        app.post.drain()
        self.assertEqual(list(self.tabs(app)), ["companion", "emu:%d" % w2, "hud"])
        self.assertEqual(self.tabs(app)["emu:%d" % w2]["label"], "DS screen")
        s.close(w2)
        app.tabs.refresh()
        app.post.drain()
        self.assertEqual(list(self.tabs(app)), ["companion", "hud"])
        app2 = self.make(Sim(), avail={"web": True, "yt": False})
        self.assertEqual(list(self.tabs(app2)), ["companion", "hud", "browser", "discord", "ytapp"])


class TestEmulatorSecondWindow(Case):
    def test_ds_screen_to_hud_and_back_keeps_mode_and_focus_consistent(self):
        for k in ({}, {"focus_follows_source": True}, {"park_on": "source"}):
            with self.subTest(**k):
                s, w1, w2 = ds_sim(**k)
                app = self.make(s)
                self.assertEqual(app.mode, HIDDEN)
                self.back(app)                              # CC5: the overlay over the game
                self.assertEqual(app.mode, OVERLAY)
                self.assertEqual(self.active(app), ["emu:%d" % w2])
                self.tap(app, "tabs.hud")
                self.assertEqual(s.ws_name_of(w2), wsw.PARK_WS)
                self.assertEqual(self.sync(app), FULL)              # parked -> FULL
                self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)
                self.assertEqual(app.ui.sheet, "hud")               # the follow-up ran after
                self.assertFalse(app.layer.hidden)
                self.assertEqual(self.focused(), w1)                # the game kept focus
                self.assertEqual(self.active(app), ["hud"])
                self.assertIn("emu:%d" % w2, self.tabs(app))        # still there, parked
                self.tap(app, "tabs.emu:%d" % w2)
                self.assertEqual(s.ws_name_of(w2), "2")
                self.assertEqual(self.sync(app), HIDDEN)            # shown -> HIDDEN
                self.assertTrue(app.layer.hidden)
                self.assertEqual(app.pull.state, PULL.COMPANION)
                self.assertEqual(self.focused(), w1)
                self.assertEqual(s.ws_name_of(w1), "1")             # the game never moved

    def test_companion_tab_over_the_game(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.companion")
        self.assertEqual(self.sync(app), FULL)
        self.assertEqual(app.pull.state, PULL.COMPANION)
        self.assertEqual(app.ui.showing, "companion")

    def test_the_active_emulator_tab_in_the_overlay_just_closes_the_overlay(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.emu:%d" % w2)
        self.assertEqual(app.mode, HIDDEN)
        self.assertTrue(app.layer.hidden)
        self.assertEqual(s.received, [])                    # no sway command at all

    def test_touched_second_window_focus_goes_back_to_the_game(self):
        s, w1, w2 = ds_sim(focus_follows_source=True)
        s.focus = ("con", w2)
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.companion")
        self.assertEqual(self.focused(), w1)

    def test_focus_guard_never_picks_a_parked_window(self):
        s, w1, w2 = ds_sim()
        s.win[w2]["fullscreen"] = True              # an emulator that fullscreens window 2
        s.win[w1]["fullscreen"] = False
        s.command(wsw.build_park(w2))
        s.focus = ("ws", "2")                        # focus on the empty panel workspace
        cmd, why = focus_guard.decide(s.tree())
        self.assertNotEqual(cmd, focus_guard.build_focus_con_command(w2), why)


class TestFirefoxAndMpv(Case):
    def test_bar_tabs_parks_firefox_it_keeps_running_and_comes_back(self):
        s = Sim()
        es = s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.assertTrue(self.br.running)
        web = s.map("DSI-1", app_id="rp5deck-web", focus=True)          # the owner typed
        self.assertEqual(self.sync(app), BAR)
        self.tap(app, "bar.tabs")                                       # W: 1st tap - inline picker
        self.assertEqual(s.ws_name_of(web), "2")                        # nothing moved yet
        self.assertIn("tabs.browser", app.ui.targets())
        self.tap(app, "tabs.cc")                                        # W: the strip's Home pill
        self.assertEqual(s.ws_name_of(web), wsw.PARK_WS)                # is the escape hatch now
        self.assertEqual(self.focused(), es)                            # focus back to ES
        self.assertEqual(self.sync(app), FULL)
        self.assertTrue(self.br.running, self.br.calls)                 # NOT closed
        self.assertNotIn("close", self.br.names())
        self.assertEqual(app.web.app, "web")
        self.assertTrue(app.web.parked)
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)
        self.assertIn("tabs.browser", app.ui.targets())
        self.assertTrue(self.tabs(app)["browser"]["running"])
        self.tap(app, "tabs.browser")
        self.assertEqual(s.ws_name_of(web), "2")
        self.assertEqual(self.sync(app), BAR)
        self.assertFalse(app.web.parked)
        self.assertEqual(app.ui.bar.shown_app(), "web")
        self.assertEqual(self.focused(), es)                            # showing never focuses
        opens = [c for c in self.br.calls if c[0] in ("open", "navigate")]
        self.assertEqual(len(opens), 1, self.br.calls)                  # no reload on return

    def test_parked_flag_is_set_before_sway_reports_full(self):
        """The race: the mode watcher can report FULL before the switch's
        done() reaches the UI thread. web_tiles must already know."""
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        held = []

        def submit(fn, *a, done=None):
            res = fn(*a)                        # sway changes now...
            held.append((done, res))            # ...done() arrives later
        app = self.make(s)
        self.open_cc(app)
        self.tap(app, "home.browser")
        s.map("DSI-1", app_id="rp5deck-web")
        self.sync(app)
        app.tabs.submit = submit
        self.tap(app, "bar.tabs")                        # W: inline picker (no switch)
        self.tap(app, "tabs.cc")                          # W: the Home pill - the escape hatch
        self.assertEqual(self.sync(app), FULL)          # FULL first
        self.assertTrue(self.br.running, self.br.calls)
        for done, res in held:
            done(res)
        app.post.drain()
        self.assertTrue(self.br.running)
        self.assertTrue(app.web.parked)

    def test_discord_tab_on_the_same_firefox_navigates(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        self.tap(app, "home.browser")
        web = s.map("DSI-1", app_id="rp5deck-web")
        self.sync(app)
        self.tap(app, "bar.tabs")                        # W: the inline picker
        self.assertEqual(s.ws_name_of(web), "2")          # only a UI toggle: nothing moved yet
        self.assertNotIn("bar.mute", app.ui.targets())    # the normal bar controls are hidden
        self.sync(app)
        self.tap(app, "tabs.discord")                     # W: BAR -> BAR directly, no Home bounce
        self.assertEqual(self.sync(app), BAR)             # mode never touched FULL
        self.assertEqual(app.web.label, "Discord")
        self.assertIn(("navigate", "https://discord.com/app"), self.br.calls)
        self.assertEqual(s.ws_name_of(web), "2")
        self.assertNotIn("tabs.cc", self.tabs(app))       # the picker (and its Home pill) closed
        self.assertIn("bar.mute", app.ui.targets())       # the normal controls are back

    def test_browser_over_a_ds_game_then_back_to_the_ds_screen(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.browser")
        self.assertEqual(s.ws_name_of(w2), wsw.PARK_WS)
        self.assertEqual(self.sync(app), FULL)
        self.assertTrue(self.br.running)                        # started after the park
        self.assertEqual(app.ui.sheet, "launch")
        web = s.map("DSI-1", app_id="rp5deck-web")
        self.assertEqual(self.sync(app), BAR)
        self.tap(app, "bar.tabs")                        # W: inline picker (no switch yet)
        self.tap(app, "tabs.cc")                          # W: the Home pill - the escape hatch
        self.assertEqual(self.sync(app), FULL)
        self.assertEqual(set(self.tabs(app)), {"companion", "emu:%d" % w2, "hud", "browser",
                                               "discord", "ytapp"})
        self.tap(app, "tabs.emu:%d" % w2)
        self.assertEqual(self.sync(app), HIDDEN)
        self.assertEqual(s.ws_name_of(web), wsw.PARK_WS)
        self.assertTrue(self.br.running)                        # Firefox still runs, parked
        self.assertEqual(self.focused(), w1)

    def test_ytapp_tab_parks_and_returns(self):
        """YT3: the old mpv-based YouTube tab/play flow is gone; the
        YouTube App (ytapp, its own Firefox) uses the same park/return
        mechanics Browser/Discord already use."""
        s = Sim()
        es = s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        self.tap(app, "home.ytapp")
        app.post.drain()
        self.assertTrue(self.ytbr.running)
        yt = s.map("DSI-1", app_id="rp5deck-ytapp", title="Firefox")
        self.assertEqual(self.sync(app), BAR)
        self.tap(app, "bar.tv.home")                     # YT3: the tv strip's own Home button
        self.assertEqual(self.sync(app), FULL)
        self.assertTrue(self.ytbr.running)
        self.assertNotIn("close", self.ytbr.names())
        self.assertTrue(self.tabs(app)["ytapp"]["running"])
        self.tap(app, "tabs.ytapp")                       # back to it
        self.assertEqual(s.ws_name_of(yt), "2")
        self.assertEqual(self.sync(app), BAR)
        self.assertEqual(self.focused(), es)               # Firefox is never auto-focused


class TestBarTabsInlinePicker(Case):
    """W (test day, owner: "the tabs should be selectable in all the apps
    including discord, browser and youtube"): the BAR strip's Tabs button
    (screens.CommandCenter.toggle_bar_tabs / close_bar_tabs, patches/
    W-screens.patch) shows app_tabs.py's own strip inline instead of always
    parking + opening Home first."""

    def test_opening_the_picker_touches_nothing(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        self.tap(app, "home.browser")
        web = s.map("DSI-1", app_id="rp5deck-web")
        self.assertEqual(self.sync(app), BAR)
        before = list(s.received)
        self.tap(app, "bar.tabs")
        self.assertEqual(s.received, before)                    # a pure UI toggle
        self.assertEqual(s.ws_name_of(web), "2")                 # Firefox untouched, still shown
        self.assertEqual(app.mode, BAR)
        t = app.ui.targets()
        for name in ("tabs.companion", "tabs.hud", "tabs.browser", "tabs.discord", "tabs.cc"):
            self.assertIn(name, t)
            self.assertEqual(t[name][1], 5, name)                # BAR mode: the bar's own row
        for name in ("bar.mute", "bar.slider", "bar.web.back", "bar.web.close"):
            self.assertNotIn(name, t)                            # the normal controls are gone

    def test_browser_to_ds_screen_directly_from_bar_picker(self):
        """Two different window families (Firefox, an emulator's second
        window) already running side by side; the picker jumps straight
        from one to the other, past Companion/HUD, in one hop - HIDDEN, not
        FULL - exactly what request 1 asked for."""
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.browser")
        self.assertEqual(self.sync(app), FULL)                   # cold start: Firefox did not exist
        self.assertTrue(self.br.running)
        web = s.map("DSI-1", app_id="rp5deck-web")
        self.assertEqual(self.sync(app), BAR)
        self.assertEqual(s.ws_name_of(w2), wsw.PARK_WS)          # the DS window, parked meanwhile
        self.tap(app, "bar.tabs")
        self.assertIn("tabs.emu:%d" % w2, app.ui.targets())
        self.tap(app, "tabs.emu:%d" % w2)                        # already parked: no Home bounce
        self.assertEqual(self.sync(app), HIDDEN)                 # not FULL
        self.assertEqual(s.ws_name_of(web), wsw.PARK_WS)         # Firefox parked, still running
        self.assertTrue(self.br.running)
        self.assertEqual(s.ws_name_of(w2), "2")
        self.assertEqual(self.focused(), w1)                     # the game kept focus throughout
        self.assertNotIn("tabs.cc", self.tabs(app))              # the picker closed

    def test_home_pill_reaches_settings_and_back(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        self.tap(app, "home.browser")
        web = s.map("DSI-1", app_id="rp5deck-web")
        self.assertEqual(self.sync(app), BAR)
        self.tap(app, "bar.tabs")
        self.tap(app, "tabs.cc")
        self.assertEqual(self.sync(app), FULL)
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)
        self.assertEqual(app.ui.sheet, None)                     # Home, not stuck on a sheet
        app.open_settings()
        app.post.drain()
        self.assertEqual(app.ui.sheet, "settings")               # W: still reachable from BAR
        self.assertTrue(self.br.running)                         # Firefox kept running throughout
        self.assertEqual(s.ws_name_of(web), wsw.PARK_WS)

    def test_every_tab_target_at_least_120px_with_the_home_pill(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.browser")
        self.sync(app)
        web = s.map("DSI-1", app_id="rp5deck-web")
        self.assertEqual(self.sync(app), BAR)
        self.tap(app, "bar.tabs")
        t = app.ui.targets()
        rows = {n: r for n, r in t.items() if n.startswith("tabs.")}
        self.assertEqual(set(rows), {"tabs.companion", "tabs.emu:%d" % w2, "tabs.hud",
                                     "tabs.browser", "tabs.discord", "tabs.ytapp", "tabs.cc"})
        for name, r in rows.items():
            self.assertGreaterEqual(r[2], 120, name)
            self.assertGreaterEqual(r[3], 120, name)
        self.assertEqual(web, web)


class TestSafety(Case):
    def test_unknown_window_on_the_screen_refuses_and_sends_nothing(self):
        s = Sim()
        s.map("DP-1", title="Azahar", fullscreen=True, focus=True)
        s.map("DSI-1", app_id="rp5deck-test-other", title="foot")
        app = self.make(s)
        self.assertEqual(app.mode, HIDDEN)
        self.back(app)
        self.assertEqual(app.mode, OVERLAY)
        for tab in ("tabs.companion", "tabs.hud", "tabs.browser"):
            self.tap(app, tab)
            self.assertEqual(app.mode, OVERLAY)
        self.assertEqual(s.received, [])
        self.assertFalse(self.br.running)
        self.assertEqual(app.tabs.refusals, 3)
        self.assertIn("Can't switch", app.ui.bar.hint.text)

    def test_sway_unreadable_tiles_behave_as_before(self):
        def down():
            raise ConnectionError("no sway IPC socket")
        app = self.make(Sim(), connect=down)
        self.open_cc(app)
        self.tap(app, "home.browser")
        self.assertTrue(self.br.running)
        self.assertEqual(app.ui.sheet, "launch")
        app.open_ytapp()
        app.post.drain()
        self.assertTrue(self.ytbr.running)

    def test_swapped_screens_park_and_show_on_dp1(self):
        s = Sim()
        g = s.map("DSI-1", title="Azahar", fullscreen=True, focus=True)
        sec = s.map("DP-1", title="Secondary Window")
        app = self.make(s, cc="DP-1", es="DSI-1")
        self.assertEqual(app.mode, HIDDEN)
        self.back(app)
        self.tap(app, "tabs.companion")
        self.assertEqual(self.sync(app), FULL)
        self.open_cc(app)
        self.tap(app, "tabs.emu:%d" % sec)
        self.assertEqual(self.sync(app), HIDDEN)
        self.assertEqual(s.output_of(sec), "DP-1")
        self.assertEqual(self.focused(), g)
        for p in s.received:
            self.assertNotIn("output DSI-1", p)                 # never onto the game's screen

    def test_a_window_092_moves_back_is_parked_again_once(self):
        """092, screens swapped: a window mapping runs place_cc_windows,
        which moves EVERY matching window to the CC screen - a parked one too."""
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.browser")
        self.sync(app)
        web = s.map("DSI-1", app_id="rp5deck-web")
        s.command("[con_id=%d] move container to output DSI-1" % w2)     # 092's rule, not ours
        self.assertEqual(self.sync(app), HIDDEN)
        app.tabs.refresh()
        app.post.drain()
        self.assertEqual(s.ws_name_of(w2), wsw.PARK_WS)                  # parked again
        self.assertEqual(self.sync(app), BAR)
        s.command("[con_id=%d] move container to output DSI-1" % w2)     # and again: budget 1
        self.sync(app)
        app.tabs.refresh()
        app.post.drain()
        self.assertEqual(s.ws_name_of(w2), "2")
        self.assertEqual(app.mode, HIDDEN)
        self.assertEqual(web, web)

    def test_a_new_game_window_is_never_parked_by_the_conflict_rule(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.companion")
        self.sync(app)
        s.close(w2)
        s.close(w1)
        n1 = s.map("DP-1", title="[w1] melonDS 1.1", fullscreen=True, focus=True)
        n2 = s.map("DSI-1", title="[w2] melonDS 1.1")                   # the next game
        self.assertEqual(self.sync(app), HIDDEN)
        app.tabs.refresh()
        app.post.drain()
        self.assertEqual(s.ws_name_of(n2), "2")
        self.assertEqual(self.sync(app), HIDDEN)
        self.assertEqual(n1, n1)

    def test_state_json_has_the_tabs(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        st = app.state()
        self.assertIn("tabs", st)
        self.assertEqual(st["tabs"]["parking_workspace"], wsw.PARK_WS)


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
class TestRealRenderingCC6(Case):
    def render(self, app, w, h, name):
        import ctypes
        gfx = _gfx()
        canvas = gfx.Canvas(w, h)
        self.addCleanup(canvas.free)
        with canvas.clipped((0, 0, w, h)):
            app.ui.root.paint(canvas)
        out = os.environ.get("RP5DECK_RENDER_DIR")
        if out:
            os.makedirs(out, exist_ok=True)
            f = gfx._cairo.cairo_surface_write_to_png
            f.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            canvas.pixels()
            self.assertEqual(f(canvas.surf, os.fsencode(
                os.path.join(out, "cc6-pc-render-%s.png" % name))), 0)
        data, stride = canvas.pixels()

        def px(x, y):
            return tuple(ctypes.string_at(data + int(y) * stride + int(x) * 4, 4))
        return px

    def accent_at(self, px, rect):
        b, g, r, a = px(rect[0] + 30, rect[1] + rect[3] - 12)
        return (r, g, b) == (0x3D, 0x8B, 0xFD)

    def test_command_center_with_tabs(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        self.tap(app, "tabs.browser")
        self.sync(app)
        s.map("DSI-1", app_id="rp5deck-web")
        self.sync(app)
        self.tap(app, "bar.tabs")                        # W: inline picker
        self.tap(app, "tabs.cc")                          # W: the Home pill - the escape hatch
        self.sync(app)
        app.tabs.rebuild()
        px = self.render(app, W, H, "command-center-tabs")
        t = app.ui.targets()
        self.assertFalse(self.accent_at(px, t["tabs.browser"]))       # parked: not active
        app.open_hud()
        app.tabs.rebuild()
        px = self.render(app, W, H, "hud-tab-active")
        self.assertTrue(self.accent_at(px, app.ui.targets()["tabs.hud"]))
        rows = [r for n, r in app.ui.targets().items() if n.startswith("tabs.")]
        self.assertTrue(all(r[1] + r[3] <= app.ui.hud.rect[1] for r in rows))  # above the HUD

    def test_overlay_with_the_ds_tab_active(self):
        s, w1, w2 = ds_sim()
        app = self.make(s)
        self.back(app)
        px = self.render(app, W, H, "overlay-ds-tab")
        self.assertTrue(self.accent_at(px, app.ui.targets()["tabs.emu:%d" % w2]))

    def test_bar_strip_tabs_button(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        app = self.make(s)
        self.open_cc(app)
        self.tap(app, "home.browser")
        s.map("DSI-1", app_id="rp5deck-web")
        self.sync(app)
        self.render(app, W, screens.BAR_H, "bar-browser-tabs-button")
        t = app.ui.targets()
        self.assertLess(t["bar.tabs"][0], t["bar.web.back"][0])
        self.assertGreaterEqual(t["bar.slider"][2], 600)                # HF1's floor holds
        # YT3: the tv strip (YouTube App) - no Tabs button of its own any
        # more (Home/Close replace it), rendered here instead of the old
        # mpv "yt" strip this test used to exercise.
        app2 = self.make(Sim())
        self.open_cc(app2)
        self.tap(app2, "home.ytapp")
        app2.post.drain()
        self.sim.map("DSI-1", app_id="rp5deck-ytapp")
        self.sync(app2)
        self.render(app2, W, screens.BAR_H, "bar-youtube-tabs-button")
        t = app2.ui.targets()
        self.assertNotIn("bar.tabs", t)
        widths = [t[n][2] for n in ("bar.tv.left", "bar.tv.up", "bar.tv.down", "bar.tv.right",
                                    "bar.tv.ok", "bar.tv.back")]
        self.assertEqual(set(widths), {screens.Bar.TV_W})
        self.assertEqual(t["bar.tv.home"][2], screens.Bar.TV_ACTION_W)
        self.assertEqual(t["bar.tv.close"][2], screens.Bar.TV_ACTION_W)
        self.assertGreater(t["bar.slider"][2], 300)                     # the slider still fits


if __name__ == "__main__":
    unittest.main()
