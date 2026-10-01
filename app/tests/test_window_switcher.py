#!/usr/bin/env python3
"""CC6 window_switcher: the allow-list, what it will and will not move, and
that rp5deck's mode (sway_ipc.compute_mode, the real one) is what each tab
promises after a switch - against a stateful simulated sway
(tests/cc6_sway_sim.py) under every knob where real sway is uncertain.

Break proofs: tools/cc6_break_tests.py mutates the safety logic and requires
these to go red.
"""
import itertools
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
import sway_ipc  # noqa: E402
import window_switcher as ws  # noqa: E402
from cc6_sway_sim import Sim, serve  # noqa: E402

REPO = os.path.dirname(os.path.dirname(APP))
F092 = os.path.join(os.path.dirname(APP), "scripts", "dual-screen-layout-and-power")
KNOBS = [dict(park_on=p, focus_follows_source=f)
         for p, f in itertools.product(("focused", "source"), (False, True))]


def mode(sim, cc="DSI-1", es="DP-1"):
    return sway_ipc.compute_mode(sim.tree(), internal=cc, external=es)[0]


def ds_game(**knobs):
    """A melonDS game: ES closed its window (it does while a game runs),
    [w1] fullscreen + focused on DP-1, [w2] on DSI-1."""
    s = Sim(**knobs)
    w1 = s.map("DP-1", title="[w1] melonDS 1.1", fullscreen=True, focus=True)
    w2 = s.map("DSI-1", title="[w2] melonDS 1.1")
    return s, w1, w2


def browsing(**knobs):
    """ES on DP-1, Firefox (rp5deck-web) on DSI-1 with focus (the owner typed)."""
    s = Sim(**knobs)
    es = s.map("DP-1", app_id="emulationstation", title="EmulationStation", fullscreen=True)
    web = s.map("DSI-1", app_id="rp5deck-web", title="Discord - Mozilla Firefox", focus=True)
    return s, es, web


def switch(sim, target, cc="DSI-1", es="DP-1", wire=False, dry=False):
    conn = (lambda: serve(sim)) if wire else sim.connection
    return ws.Switcher(connect=conn, dry_run=dry, log_fn=lambda m: None).switch(target, cc, es)


def focused(sim):
    return sim.focus[1] if sim.focus and sim.focus[0] == "con" else sim.focus


class TestRealDeviceTitles(unittest.TestCase):
    """The titles sway reported on the device, test day 24 Sep."""
    YES = {
        "Azahar fbd3fb0 | Ocarina of Time 3D | Secondary Window": ("Azahar", "3DS screen"),
        "GamePad View - FPS: 30.03": ("Cemu", "GamePad"),
        "GamePad View": ("Cemu", "GamePad"),
        "[w2] [60/60] melonDS 1.1": ("melonDS", "DS screen"),
        "Secondary Window": ("Azahar", "3DS screen"),
        "DSperate (Bottom)": ("DSperate", "DS screen"),
    }
    NO = ["Azahar fbd3fb0 | Ocarina of Time 3D",
          "Cemu 6f6c129 - FPS: 30.03 [Vulkan] [Generic] [TitleId: 00050000-10143500] The Wind Waker HD [US v0]",
          "[w1] [60/60] melonDS 1.1", "EmulationStation", "Secondary Window 2", "DSperate (Top)"]

    def node(self, title, app_id=None):
        return {"type": "con", "pid": 4242, "name": title, "app_id": app_id,
                "id": 7, "nodes": [], "floating_nodes": []}

    def test_second_windows_are_recognised_and_named(self):
        for t, fam in self.YES.items():
            self.assertEqual(ws.recognise(self.node(t)), ws.EMU, t)
            self.assertEqual(ws.emu_family(t), fam, t)

    def test_main_windows_are_not(self):
        for t in self.NO:
            self.assertIsNone(ws.recognise(self.node(t)), t)


class TestRecognise(unittest.TestCase):
    def v(self, app_id=None, title=""):
        return {"type": "con", "id": 7, "app_id": app_id, "name": title, "pid": 1, "nodes": []}

    def test_only_our_apps_and_the_092_second_windows(self):
        yes = {("rp5deck-web", ""): ws.WEB, ("rp5deck-yt", "x"): ws.YT,
               (None, "[w2] melonDS 1.1"): ws.EMU, (None, "Secondary Window"): ws.EMU,
               ("org.azahar_emu.Azahar", "Secondary Window"): ws.EMU,
               (None, "GamePad View"): ws.EMU, (None, "DSperate (Bottom)"): ws.EMU}
        for (aid, title), kind in yes.items():
            self.assertEqual(ws.recognise(self.v(aid, title)), kind, (aid, title))

    def test_never_es_the_game_or_anything_unknown(self):
        no = [("emulationstation", "EmulationStation"),
              ("emulationstation", "[w2] trick"),          # ES is never moved, whatever its title
              ("com.libretro.RetroArch", "RetroArch"), (None, "[w1] melonDS 1.1"),
              ("rp5deck-test-other", "foot"), ("rp5deck-webx", ""), ("rp5deck", "rp5deck"),
              ("rp5deck-test-bar", "[w2] x"),             # other rp5deck-* ids stay foreign
              (None, "Secondary Window 2"), (None, "DSperate (Top)"), ("foot", "foot"),
              (None, "Cemu 2.0 - GamePad View"), (None, "")]
        for aid, title in no:
            self.assertIsNone(ws.recognise(self.v(aid, title)), (aid, title))

    def test_a_split_container_is_not_a_window(self):
        n = self.v("rp5deck-web")
        n["nodes"] = [self.v("rp5deck-web")]
        self.assertIsNone(ws.recognise(n))

    def test_title_rule_is_the_one_in_092(self):
        """Read 092: every title rule there must be SECOND_WINDOW_TITLE,
        except the Azahar game-window rule (title + Azahar's app_id, 25 Sep;
        tests/test_092_azahar_rule.py owns that one)."""
        if not os.path.exists(F092):
            self.skipTest("092 is not beside this copy of the app (%s)" % F092)
        with open(F092, encoding="utf-8") as f:
            src = f.read()
        found, game = [], []
        for chunk in src.split('[title=\\"')[1:]:
            crit = chunk.split('\\"]')[0]
            title, _, app_id = crit.partition('\\" app_id=\\"')
            title = title.replace("\\\\", "\\").replace("\\$", "$")
            if app_id:
                self.assertEqual(app_id, "^org\\\\.azahar_emu\\\\.Azahar\\$", crit)
                self.assertTrue(title.startswith("^Azahar "), crit)
                game.append(title)
            else:
                found.append(title)
        self.assertGreaterEqual(len(found), 3, "092's title rules not found")
        for pat in found:
            self.assertEqual(pat, ws.SECOND_WINDOW_TITLE)
        self.assertEqual(len(game), 3, "the Azahar game-window rule: static, swap, handler")

    def test_family_labels(self):
        self.assertEqual(ws.emu_family("[w2] melonDS 1.1"), ("melonDS", "DS screen"))
        self.assertEqual(ws.emu_family("Secondary Window"), ("Azahar", "3DS screen"))
        self.assertEqual(ws.emu_family("GamePad View"), ("Cemu", "GamePad"))
        self.assertEqual(ws.emu_family("DSperate (Bottom)"), ("DSperate", "DS screen"))


class TestAllowList(unittest.TestCase):
    """enforce_allowed() on a real-shaped tree: exactly three shapes, each
    con_id checked against the tree."""

    def setUp(self):
        self.s, self.es_id, self.web = browsing()
        self.game = self.s.map("DP-1", app_id="com.libretro.RetroArch", title="RetroArch",
                               fullscreen=True)
        self.yt = None
        self.tree = self.s.tree()

    def ok(self, payload, cc="DSI-1", es="DP-1", tree=None):
        ws.enforce_allowed(payload, tree or self.tree, cc, es)

    def bad(self, payload, cc="DSI-1", es="DP-1", tree=None):
        with self.assertRaises(ws.CommandRefused, msg=payload):
            ws.enforce_allowed(payload, tree or self.tree, cc, es)

    def test_the_three_shapes_pass(self):
        self.ok(ws.build_park(self.web))
        self.ok(ws.build_park(self.web) + "; " + ws.build_focus(self.es_id))
        s, w1, w2 = ds_game()
        s.command(ws.build_park(w2))
        t = s.tree()
        self.ok(ws.build_show(w2, "DSI-1") + "; " + ws.build_focus(w1), tree=t)

    def test_arbitrary_commands_are_refused(self):
        for p in ("exit", "reload", "kill", "[con_id=%d] kill" % self.web,
                  "[con_id=%d] fullscreen enable" % self.web,
                  "[con_id=%d] move scratchpad" % self.web, "scratchpad show",
                  "[con_id=%d] floating disable" % self.web,
                  "[app_id=\"rp5deck-web\"] move container to workspace rp5deck-parked",
                  "[con_id=%d] move container to workspace 1" % self.web,
                  "[con_id=%d] move container to workspace rp5deck-parked2" % self.web,
                  "move workspace to output DP-1", "workspace rp5deck-parked",
                  "output DSI-1 disable", "input * events disabled", "exec foot",
                  "[con_id=%d] move container to workspace rp5deck-parked, kill" % self.web,
                  "[con_id=%d] move container to workspace rp5deck-parked;exit" % self.web,
                  "[con_id=%d] move container to workspace rp5deck-parked; exit" % self.web,
                  "[con_id=%d] move container to workspace rp5deck-parked\nexit" % self.web,
                  " [con_id=%d] move container to workspace rp5deck-parked" % self.web,
                  "[con_id=%d] move container to workspace rp5deck-parked " % self.web,
                  "[con_id=%d]  focus" % self.es_id, "", "; "):
            self.bad(p)

    def test_never_moves_es_the_game_or_an_unknown_window(self):
        other = self.s.map("DSI-1", app_id="rp5deck-test-other", title="foot")
        t = self.s.tree()
        for cid in (self.es_id, self.game, other):
            self.bad(ws.build_park(cid), tree=t)
            self.bad(ws.build_show(cid, "DSI-1"), tree=t)

    def test_park_only_what_is_shown_on_the_cc_screen(self):
        s = Sim()
        s.map("DP-1", app_id="emulationstation", fullscreen=True, focus=True)
        stray = s.map("DP-1", title="[w2] melonDS")          # rule failed: on the ES screen
        self.bad(ws.build_park(stray), tree=s.tree())

    def test_show_only_from_the_parking_workspace_to_the_cc_screen(self):
        s, w1, w2 = ds_game()
        t = s.tree()
        self.bad(ws.build_show(w2, "DSI-1"), tree=t)           # not parked: already there
        s.command(ws.build_park(w2))
        t = s.tree()
        self.ok(ws.build_show(w2, "DSI-1"), tree=t)
        self.bad(ws.build_show(w2, "DP-1"), tree=t)            # the game's screen: never
        self.bad("[con_id=%d] move container to output HDMI-A-1" % w2, tree=t)
        with self.assertRaises(ws.CommandRefused):
            ws.build_show(w2, "HDMI-A-1")
        # swapped (SW1): the CC is on DP-1, ES on DSI-1 -> only DP-1 is allowed
        self.bad(ws.build_show(w2, "DSI-1"), cc="DP-1", es="DSI-1", tree=t)
        self.ok(ws.build_show(w2, "DP-1"), cc="DP-1", es="DSI-1", tree=t)
        # the CC screen and ES's screen must differ (a layout 092 has not finished)
        self.bad(ws.build_show(w2, "DSI-1"), cc="DSI-1", es="DSI-1", tree=t)

    def test_focus_rules(self):
        yt = self.s.map("DSI-1", app_id="rp5deck-yt", title="mpv")
        t = self.s.tree()
        park = ws.build_park(self.web)
        self.bad(park + "; " + ws.build_focus(yt), tree=t)            # mpv: never
        self.bad(park + "; " + ws.build_focus(self.web), tree=t)      # the one being parked
        self.bad(ws.build_focus(self.es_id) + "; " + park, tree=t)    # focus not last
        self.bad(park + "; " + ws.build_focus(self.es_id) + "; " + ws.build_focus(self.game),
                 tree=t)
        self.bad(ws.build_focus(self.es_id), tree=t)                  # moves nothing
        # focusing a PARKED window would make its workspace visible over the game
        s2, w1b, w2b = ds_game()
        s2.command(ws.build_park(w2b))
        web = s2.map("DSI-1", app_id="rp5deck-web")
        s2.command(ws.build_park(web))
        t2 = s2.tree()
        self.bad(ws.build_show(web, "DSI-1") + "; " + ws.build_focus(w2b), tree=t2)

    def test_no_double_moves_and_a_part_limit(self):
        park = ws.build_park(self.web)
        self.bad(park + "; " + park)
        many = "; ".join([park] + [ws.build_focus(self.es_id)] * 12)
        self.bad(many)


class TestSwitchInTheSim(unittest.TestCase):
    """plan -> allow-list -> the simulated sway -> compute_mode, under every
    knob: afterwards rp5deck's mode is what the tab promised, the game (or
    ES) has focus, and nothing but recognised windows moved."""

    def check(self, res, sim, expect_mode, focus, cc="DSI-1", es="DP-1"):
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["expect"], expect_mode, res)
        self.assertEqual(mode(sim, cc, es), expect_mode, sim.tree())
        self.assertEqual(focused(sim), focus, (sim.focus, res["sent"]))
        for name, w in sim.ws.items():                  # the parking ws is never displayed
            if name == ws.PARK_WS:
                self.assertNotEqual(sim.current[w["output"]], ws.PARK_WS)

    def test_ds_game_hide_and_show_the_second_window(self):
        for k in KNOBS:
            with self.subTest(**k):
                s, w1, w2 = ds_game(**k)
                self.assertEqual(mode(s), sway_ipc.HIDDEN)
                self.check(switch(s, "internal"), s, sway_ipc.FULL, w1)
                self.assertEqual(s.ws_name_of(w2), ws.PARK_WS)
                self.assertEqual(s.ws_name_of(w1), "1")          # the game never moved
                self.check(switch(s, "emu:%d" % w2), s, sway_ipc.HIDDEN, w1)
                self.assertEqual(s.ws_name_of(w2), "2")
                self.assertNotIn(ws.PARK_WS, s.ws)              # emptied -> destroyed

    def test_touched_second_window_had_focus_then_focus_goes_to_the_game(self):
        for k in KNOBS:
            with self.subTest(**k):
                s, w1, w2 = ds_game(**k)
                s.focus = ("con", w2)                   # the owner tapped the DS screen
                self.check(switch(s, "internal"), s, sway_ipc.FULL, w1)

    def test_firefox_had_focus_parking_gives_it_back_to_es(self):
        for k in KNOBS:
            with self.subTest(**k):
                s, es_id, web = browsing(**k)
                self.check(switch(s, "internal"), s, sway_ipc.FULL, es_id)
                self.check(switch(s, "web"), s, sway_ipc.BAR, es_id)   # shown, NOT focused

    def test_browser_over_a_ds_game(self):
        for k in KNOBS:
            with self.subTest(**k):
                s, w1, w2 = ds_game(**k)
                res = switch(s, "web")                  # Firefox not running yet
                self.check(res, s, sway_ipc.FULL, w1)   # the app starts after this
                web = s.map("DSI-1", app_id="rp5deck-web")      # 092's rule puts it there
                self.assertEqual(mode(s), sway_ipc.BAR)
                self.check(switch(s, "emu:%d" % w2), s, sway_ipc.HIDDEN, w1)
                self.assertEqual(s.ws_name_of(web), ws.PARK_WS)
                self.check(switch(s, "web"), s, sway_ipc.BAR, w1)
                self.assertEqual(s.ws_name_of(w2), ws.PARK_WS)

    def test_youtube_and_mpv_never_gets_focus(self):
        for k in KNOBS:
            with self.subTest(**k):
                s, es_id, web = browsing(**k)
                s.close(web)
                yt = s.map("DSI-1", app_id="rp5deck-yt", title="mpv", focus=True)   # tapped
                self.check(switch(s, "internal"), s, sway_ipc.FULL, es_id)
                self.check(switch(s, "yt"), s, sway_ipc.BAR, es_id)
                self.assertEqual(s.ws_name_of(yt), "2")

    def test_popup_windows_move_together(self):
        s, es_id, web = browsing()
        pop = s.map("DSI-1", app_id="rp5deck-web", title="Log in")
        self.check(switch(s, "internal"), s, sway_ipc.FULL, es_id)
        self.assertEqual({s.ws_name_of(web), s.ws_name_of(pop)}, {ws.PARK_WS})
        self.check(switch(s, "web"), s, sway_ipc.BAR, es_id)

    def test_swapped_screens(self):
        for k in KNOBS:
            with self.subTest(**k):
                s = Sim(**k)
                es_id = s.map("DSI-1", app_id="emulationstation", fullscreen=True)
                g = s.map("DSI-1", title="Azahar", fullscreen=True, focus=True)
                sec = s.map("DP-1", title="Secondary Window")
                self.check(switch(s, "internal", cc="DP-1", es="DSI-1"), s, sway_ipc.FULL, g,
                           cc="DP-1", es="DSI-1")
                self.check(switch(s, "emu:%d" % sec, cc="DP-1", es="DSI-1"), s,
                           sway_ipc.HIDDEN, g, cc="DP-1", es="DSI-1")
                self.assertEqual(s.output_of(sec), "DP-1")

    def test_already_there_sends_nothing(self):
        s, w1, w2 = ds_game()
        res = switch(s, "emu:%d" % w2)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["sent"])
        self.assertEqual(s.received, [])

    def test_unknown_window_on_the_cc_screen_refuses_everything_and_sends_nothing(self):
        s, w1, w2 = ds_game()
        s.map("DSI-1", app_id="rp5deck-test-other", title="foot")
        for target in ("internal", "web", "yt", "emu:%d" % w2):
            res = switch(s, target)
            self.assertFalse(res["ok"])
            self.assertIn("never moves it", res["refused"])
        self.assertEqual(s.received, [])

    def test_undocked_or_bad_layout_refuses(self):
        s, w1, w2 = ds_game()
        s.undock("DP-1")
        res = switch(s, "emu:%d" % w2)
        self.assertIn("add-on", res["refused"])
        self.assertEqual(s.received, [])
        s, w1, w2 = ds_game()
        self.assertIn("same screen", switch(s, "internal", cc="DP-1", es="DP-1")["refused"])
        self.assertIn("not DSI-1 or DP-1", switch(s, "internal", cc="HDMI-A-1")["refused"])
        self.assertEqual(s.received, [])

    def test_gone_or_misplaced_emulator_window(self):
        s, w1, w2 = ds_game()
        self.assertIn("gone", switch(s, "emu:99999")["refused"])
        self.assertIn("gone", switch(s, "emu:%d" % w1)["refused"])      # the game: not ours
        s2 = Sim()
        s2.map("DP-1", title="[w1] melonDS", fullscreen=True, focus=True)
        stray = s2.map("DP-1", title="[w2] melonDS")
        self.assertIn("other screen", switch(s2, "emu:%d" % stray)["refused"])

    def test_sway_saying_no_or_lying_is_an_error_not_ok(self):
        s, w1, w2 = ds_game()
        s.refuse = True
        res = switch(s, "internal")
        self.assertFalse(res["ok"])
        self.assertIn("sway refused", res["error"])
        s.refuse, s.lie = False, True
        res = switch(s, "internal")
        self.assertFalse(res["ok"])
        self.assertIn("not parked", res["error"])

    def test_dry_run_sends_nothing(self):
        s, w1, w2 = ds_game()
        res = switch(s, "internal", dry=True)
        self.assertTrue(res["ok"])
        self.assertIn(ws.PARK_WS, res["sent"])
        self.assertEqual(s.received, [])
        self.assertEqual(s.ws_name_of(w2), "2")

    def test_no_es_and_no_fullscreen_game_still_restores_a_window_on_the_es_screen(self):
        s = Sim()
        g = s.map("DP-1", title="Cemu 2.0", fullscreen=False)
        pad = s.map("DSI-1", title="GamePad View", focus=True)
        self.check(switch(s, "internal"), s, sway_ipc.FULL, g)
        self.assertEqual(s.ws_name_of(pad), ws.PARK_WS)


def undocked_es(**knobs):
    """One screen: ES fullscreen and focused on DSI-1's workspace 1, nothing else."""
    s = Sim(outputs=("DSI-1",), **knobs)
    es_id = s.map("DSI-1", app_id="emulationstation", title="EmulationStation", fullscreen=True,
                  focus=True)
    return s, es_id


class TestUndockedWebApps(unittest.TestCase):
    """One screen, ES fullscreen on it: a web app is shown by switching to a workspace of its own and
    hidden by switching back to ES's. ES is never moved."""

    def test_es_alone_is_hidden_and_a_web_window_on_top_is_the_bar(self):
        s, es_id = undocked_es()
        self.assertEqual(mode(s), sway_ipc.HIDDEN)
        web = s.map("DSI-1", app_id="rp5deck-web", title="Discord")
        self.assertEqual(mode(s), sway_ipc.HIDDEN)              # behind ES on workspace 1 still
        switch(s, "web")
        self.assertEqual(mode(s), sway_ipc.BAR)
        self.assertEqual(s.ws_name_of(web), ws.UNDOCKED_WS[ws.WEB])
        self.assertEqual(s.ws_name_of(es_id), "1")              # ES never moved

    def test_show_hide_show_again(self):
        for k in KNOBS:
            with self.subTest(**k):
                s, es_id = undocked_es(**k)
                web = s.map("DSI-1", app_id="rp5deck-web", title="Discord")
                res = switch(s, "web")
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["expect"], sway_ipc.BAR)
                self.assertEqual(mode(s), sway_ipc.BAR)
                self.assertEqual(focused(s), web)
                res = switch(s, "internal")
                self.assertTrue(res["ok"], res)
                self.assertEqual(mode(s), sway_ipc.HIDDEN)
                self.assertEqual(focused(s), es_id)
                self.assertEqual(s.ws_name_of(web), ws.UNDOCKED_WS[ws.WEB])  # kept, parked
                snap = ws.Switcher(connect=s.connection, log_fn=lambda m: None).snapshot("DSI-1", "DP-1")
                self.assertEqual(snap.by_id(web).where, ws.PARKED)
                res = switch(s, "web")                          # no move this time, only the switch
                self.assertTrue(res["ok"], res)
                self.assertEqual(s.received[-1], "workspace %s; %s" % (
                    ws.UNDOCKED_WS[ws.WEB], ws.build_focus(web)))
                self.assertEqual(mode(s), sway_ipc.BAR)

    def test_no_window_yet_sends_nothing_and_promises_hidden(self):
        s, es_id = undocked_es()
        res = switch(s, "web")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["expect"], sway_ipc.HIDDEN)
        self.assertIsNone(res["sent"])
        self.assertEqual(s.received, [])

    def test_home_with_nothing_showing_sends_nothing(self):
        s, es_id = undocked_es()
        res = switch(s, "internal")
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["sent"])

    def test_the_youtube_app_has_its_own_workspace(self):
        s, es_id = undocked_es()
        web = s.map("DSI-1", app_id="rp5deck-web", title="Discord")
        switch(s, "web")
        yt = s.map("DSI-1", app_id="rp5deck-ytapp", title="YouTube on TV")
        res = switch(s, "ytapp")
        self.assertTrue(res["ok"], res)
        self.assertEqual(s.ws_name_of(yt), ws.UNDOCKED_WS[ws.YTAPP])
        self.assertEqual(s.ws_name_of(web), ws.UNDOCKED_WS[ws.WEB])
        self.assertEqual(mode(s), sway_ipc.BAR)
        self.assertEqual(s.current["DSI-1"], ws.UNDOCKED_WS[ws.YTAPP])

    def test_the_add_on_only_targets_are_refused(self):
        s, es_id = undocked_es()
        s.map("DSI-1", app_id="rp5deck-yt", title="mpv")
        s.map("DSI-1", title="Secondary Window")
        for target in ("yt", "emu:%d" % 101):
            res = switch(s, target)
            self.assertFalse(res["ok"])
            self.assertIn("add-on", res["refused"])
        self.assertEqual(s.received, [])

    def test_a_game_on_the_one_screen_is_the_home_window(self):
        s = Sim(outputs=("DSI-1",))
        game = s.map("DSI-1", app_id="com.libretro.RetroArch", title="RetroArch", fullscreen=True,
                     focus=True)
        web = s.map("DSI-1", app_id="rp5deck-web", title="Discord")
        self.assertTrue(switch(s, "web")["ok"])
        self.assertTrue(switch(s, "internal")["ok"])
        self.assertEqual(focused(s), game)
        self.assertEqual(s.ws_name_of(game), "1")

    def test_sway_lying_is_an_error(self):
        s, es_id = undocked_es()
        s.map("DSI-1", app_id="rp5deck-web", title="Discord")
        s.lie = True
        res = switch(s, "web")
        self.assertFalse(res["ok"])
        self.assertIn("sway said yes but", res["error"])


class TestUndockedAllowList(unittest.TestCase):
    """The one-screen commands are allowed only undocked, only for web windows, only to places the tree
    backs up."""

    def bad(self, payload, sim):
        ipc = sim.connection()
        with self.assertRaises(ws.CommandRefused, msg=payload):
            ipc.run_command(payload, ipc.get_tree(), "DSI-1", "DP-1")
        self.assertEqual(sim.received, [])

    def test_refused_while_docked(self):
        s, es_id, web = browsing()
        self.bad(ws.build_undocked_move(web, ws.WEB), s)
        self.bad("workspace %s" % ws.UNDOCKED_WS[ws.WEB], s)
        self.bad("workspace number 1", s)

    def test_only_web_windows_and_only_their_own_workspace(self):
        s, es_id = undocked_es()
        web = s.map("DSI-1", app_id="rp5deck-web", title="Discord")
        yt = s.map("DSI-1", app_id="rp5deck-ytapp", title="YouTube")
        self.bad(ws.build_undocked_move(es_id, ws.WEB), s)           # ES is never moved
        self.bad(ws.build_undocked_move(web, ws.YTAPP), s)           # the wrong kind's workspace
        self.bad(ws.build_undocked_move(web, ws.WEB) + "; " + ws.build_undocked_move(web, ws.WEB) +
                 "; workspace %s" % ws.UNDOCKED_WS[ws.WEB], s)       # moved twice
        self.assertEqual(ws.build_undocked_move(yt, ws.YTAPP),
                         "[con_id=%d] move container to workspace rp5deck-undocked-ytapp" % yt)

    def test_workspace_switches_need_a_window_or_the_home_workspace(self):
        s, es_id = undocked_es()
        self.bad("workspace %s" % ws.UNDOCKED_WS[ws.WEB], s)         # no web window yet
        self.bad("workspace number 7", s)                           # ES is on 1
        self.bad("workspace nonsense", s)
        self.bad("workspace number 1; exit", s)

    def test_focus_after_a_switch_only_for_web_windows_or_home(self):
        s, es_id = undocked_es()
        web = s.map("DSI-1", app_id="rp5deck-web", title="Discord")
        other = s.map("DSI-1", app_id="foot", title="foot")
        s.add_ws("9", "DSI-1")
        s.command("[con_id=%d] move container to workspace 9" % other)      # a window nobody sees
        self.bad("workspace %s; %s" % (ws.UNDOCKED_WS[ws.WEB], ws.build_focus(other)), s)
        self.bad(ws.build_focus(web), s)                            # no switch: moves nothing


class TestRealWire(unittest.TestCase):
    """The same switch through the real Ipc framing over a socketpair."""

    def test_switch_over_the_wire(self):
        s, w1, w2 = ds_game(focus_follows_source=True)
        res = switch(s, "internal", wire=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(mode(s), sway_ipc.FULL)
        self.assertEqual(focused(s), w1)
        self.assertEqual(s.received, [ws.build_park(w2) + "; " + ws.build_focus(w1)])

    def test_run_command_refuses_before_a_byte_is_sent(self):
        s, w1, w2 = ds_game()
        ipc = serve(s)
        tree = ipc.get_tree()
        for p in ("exit", "[con_id=%d] move container to workspace %s" % (w1, ws.PARK_WS)):
            with self.assertRaises(ws.CommandRefused):
                ipc.run_command(p, tree, "DSI-1", "DP-1")
        ipc.get_tree()                                  # the connection still works
        ipc.close()
        self.assertEqual(s.received, [])


if __name__ == "__main__":
    unittest.main()
