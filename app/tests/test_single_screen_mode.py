#!/usr/bin/env python3
"""The Command Center with one screen (add-on unplugged): Browser, Discord and the YouTube App each open,
go Home, come back, switch between each other and close, over ES, over Steam's window or over a game. Against
the simulated sway (tests/cc6_sway_sim.py) with the real planner, allow-list, mode computation and tab model.

What the dual-screen daemon does when a web window maps (move it to its workspace and show it) is stood in
for by place(); the real shell handler has its own tests (test_092_undocked_web_window.py).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import app_tabs  # noqa: E402
import sway_ipc  # noqa: E402
import window_switcher as ws  # noqa: E402
from cc6_sway_sim import Sim  # noqa: E402

CC, ES_OUT = "DSI-1", "DP-1"
AVAIL = {"web": True, "yt": True}
APPS = (("Browser", ws.WEB, "rp5deck-web", "browser"),
        ("Discord", ws.WEB, "rp5deck-web", "discord"),
        ("YouTube App", ws.YTAPP, "rp5deck-ytapp", "ytapp"))
HOMES = ("es", "steam", "game")


def scene(home="es"):
    """One screen. `home` is what is underneath: ES fullscreen, Steam's gamescope window (no app id, not
    fullscreen), or a fullscreen game. Returns (sim, the home window's id)."""
    s = Sim(outputs=(CC,))
    if home == "es":
        h = s.map(CC, app_id="emulationstation", title="EmulationStation", fullscreen=True, focus=True)
    elif home == "steam":
        h = s.map(CC, app_id=None, title="Steam Big Picture Mode", fullscreen=False, focus=True)
    else:
        h = s.map(CC, app_id="com.libretro.RetroArch", title="RetroArch", fullscreen=True, focus=True)
    return s, h


def mode(s):
    return sway_ipc.compute_mode(s.tree(), internal=CC, external=ES_OUT)[0]


def focused(s):
    return s.focus[1] if s.focus and s.focus[0] == "con" else s.focus


def switch(s, target):
    return ws.Switcher(connect=s.connection, log_fn=lambda m: None).switch(target, CC, ES_OUT)


def snap(s):
    return ws.scan(s.tree(), CC, ES_OUT)


def open_app(s, app_id, kind):
    """A web window maps (behind whatever is fullscreen) and the daemon puts it on its workspace and shows it."""
    w = s.map(CC, app_id=app_id, title="Firefox")
    name = ws.UNDOCKED_WS[kind]
    s.command("[con_id=%d] move container to workspace %s" % (w, name))
    s.command("workspace %s" % name)
    return w


def tabs(s, label="Browser"):
    sn = snap(s).to_dict()
    web = {"app": ws.WEB, "label": label} if sn["windows"] else {}
    return app_tabs.build_tabs(sn, web, AVAIL, {"mode": mode(s)})


def active_ids(s, label="Browser"):
    return [t["id"] for t in tabs(s, label) if t["active"]]


def each_home(test):
    def run(self):
        for home in HOMES:
            with self.subTest(home=home):
                test(self, home)
    run.__name__ = test.__name__
    return run


class TestOpenHomeReopen(unittest.TestCase):
    @each_home
    def test_the_first_open_of_each_app(self, home):
        for label, kind, app_id, tab in APPS:
            s, h = scene(home)
            self.assertEqual(mode(s), sway_ipc.HIDDEN)
            res = switch(s, kind)                      # before the app exists: nothing to do yet
            self.assertTrue(res["ok"], res)
            self.assertIsNone(res["sent"])
            self.assertEqual(res["expect"], sway_ipc.HIDDEN)
            w = open_app(s, app_id, kind)
            self.assertEqual(mode(s), sway_ipc.BAR, label)
            self.assertEqual(focused(s), w, label)
            self.assertEqual(s.ws_name_of(h), "1", "the window underneath never moves")
            self.assertEqual(active_ids(s, label), [tab], label)

    @each_home
    def test_home_goes_back_to_what_was_underneath_and_keeps_the_app(self, home):
        for label, kind, app_id, tab in APPS:
            s, h = scene(home)
            w = open_app(s, app_id, kind)
            res = switch(s, "internal")
            self.assertTrue(res["ok"], (label, res))
            self.assertEqual(mode(s), sway_ipc.HIDDEN, label)
            self.assertEqual(focused(s), h, label)
            self.assertEqual(s.ws_name_of(w), ws.UNDOCKED_WS[kind], "the app is parked, not closed")
            by = {x.con_id: x for x in snap(s).windows}
            self.assertEqual(by[w].where, ws.PARKED)
            self.assertEqual(active_ids(s, label), ["companion"], "no app tab is the shown one at home")
            self.assertTrue(any(t["id"] == tab and t["running"] for t in tabs(s, label)), label)

    @each_home
    def test_coming_back_moves_nothing_and_restores_the_bar(self, home):
        for label, kind, app_id, tab in APPS:
            s, h = scene(home)
            w = open_app(s, app_id, kind)
            switch(s, "internal")
            res = switch(s, kind)
            self.assertTrue(res["ok"], (label, res))
            self.assertEqual(s.received[-1], "workspace %s; %s" % (ws.UNDOCKED_WS[kind], ws.build_focus(w)))
            self.assertEqual(mode(s), sway_ipc.BAR, label)
            self.assertEqual(focused(s), w, label)

    @each_home
    def test_home_twice_and_home_with_nothing_open_send_nothing_more(self, home):
        s, h = scene(home)
        self.assertIsNone(switch(s, "internal")["sent"])
        open_app(s, "rp5deck-web", ws.WEB)
        switch(s, "internal")
        n = len(s.received)
        res = switch(s, "internal")
        self.assertTrue(res["ok"])
        self.assertIsNone(res["sent"])
        self.assertEqual(len(s.received), n)


class TestSwitchingBetweenApps(unittest.TestCase):
    @each_home
    def test_each_app_has_its_own_screen_and_none_is_lost(self, home):
        s, h = scene(home)
        web = open_app(s, "rp5deck-web", ws.WEB)
        yt = open_app(s, "rp5deck-ytapp", ws.YTAPP)
        self.assertEqual(mode(s), sway_ipc.BAR)
        self.assertEqual(s.current[CC], ws.UNDOCKED_WS[ws.YTAPP])
        self.assertEqual(active_ids(s), ["ytapp"])
        self.assertTrue(switch(s, ws.WEB)["ok"])
        self.assertEqual(s.current[CC], ws.UNDOCKED_WS[ws.WEB])
        self.assertEqual(focused(s), web)
        self.assertEqual(active_ids(s), ["browser"])
        self.assertTrue(switch(s, ws.YTAPP)["ok"])
        self.assertEqual(focused(s), yt)
        self.assertTrue(switch(s, "internal")["ok"])
        self.assertEqual(focused(s), h)
        for w in (web, yt):
            self.assertIn(s.ws_name_of(w), ws.UNDOCKED_WS.values())

    @each_home
    def test_browser_and_discord_are_one_window_so_switching_between_them_is_the_app_not_the_screen(self, home):
        s, h = scene(home)
        open_app(s, "rp5deck-web", ws.WEB)
        n = len(s.received)
        res = switch(s, ws.WEB)                         # Discord tapped while the Browser window is up
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["sent"])
        self.assertEqual(len(s.received), n)
        self.assertEqual(mode(s), sway_ipc.BAR)
        self.assertEqual(active_ids(s, "Browser"), ["browser"])
        self.assertEqual(active_ids(s, "Discord"), ["discord"])

    @each_home
    def test_the_other_screens_targets_are_refused_and_nothing_is_sent(self, home):
        s, h = scene(home)
        open_app(s, "rp5deck-web", ws.WEB)
        n = len(s.received)
        s.map(CC, app_id="rp5deck-yt", title="mpv")
        s.map(CC, title="Secondary Window")
        for target in ("yt", "emu:%d" % 102):
            res = switch(s, target)
            self.assertFalse(res["ok"], target)
            self.assertIn("add-on", res["refused"])
        self.assertEqual(len(s.received), n)


class TestClosing(unittest.TestCase):
    @each_home
    def test_closing_the_shown_app_lands_on_what_was_underneath(self, home):
        for label, kind, app_id, tab in APPS:
            s, h = scene(home)
            w = open_app(s, app_id, kind)
            s.close(w)
            self.assertEqual(mode(s), sway_ipc.HIDDEN, label)
            self.assertEqual(s.current[CC], "1", label)
            self.assertEqual(focused(s), h, label)
            self.assertNotIn(ws.UNDOCKED_WS[kind], s.ws, "the emptied workspace is gone")
            self.assertEqual([t["id"] for t in tabs(s) if t["running"]], [])

    @each_home
    def test_open_close_open_close_again(self, home):
        s, h = scene(home)
        for _ in range(3):
            w = open_app(s, "rp5deck-web", ws.WEB)
            self.assertEqual(mode(s), sway_ipc.BAR)
            s.close(w)
            self.assertEqual(mode(s), sway_ipc.HIDDEN)
            self.assertEqual(focused(s), h)

    @each_home
    def test_closing_a_parked_app_changes_nothing_on_screen(self, home):
        s, h = scene(home)
        w = open_app(s, "rp5deck-ytapp", ws.YTAPP)
        switch(s, "internal")
        s.close(w)
        self.assertEqual(mode(s), sway_ipc.HIDDEN)
        self.assertEqual(focused(s), h)
        self.assertEqual(s.current[CC], "1")
        self.assertNotIn(ws.UNDOCKED_WS[ws.YTAPP], s.ws)

    @each_home
    def test_closing_one_app_leaves_the_other_one_usable(self, home):
        s, h = scene(home)
        web = open_app(s, "rp5deck-web", ws.WEB)
        yt = open_app(s, "rp5deck-ytapp", ws.YTAPP)
        s.close(yt)
        self.assertEqual(s.current[CC], ws.UNDOCKED_WS[ws.WEB], "back to the app shown before")
        self.assertEqual(mode(s), sway_ipc.BAR)
        self.assertTrue(switch(s, "internal")["ok"])
        self.assertTrue(switch(s, ws.WEB)["ok"])
        self.assertEqual(focused(s), web)


class TestPopupsAndOddities(unittest.TestCase):
    @each_home
    def test_a_login_popup_travels_with_its_app(self, home):
        s, h = scene(home)
        web = open_app(s, "rp5deck-web", ws.WEB)
        pop = open_app(s, "rp5deck-web", ws.WEB)
        self.assertEqual({s.ws_name_of(web), s.ws_name_of(pop)}, {ws.UNDOCKED_WS[ws.WEB]})
        self.assertEqual(mode(s), sway_ipc.BAR)
        switch(s, "internal")
        self.assertEqual(mode(s), sway_ipc.HIDDEN)
        self.assertTrue(switch(s, ws.WEB)["ok"])
        self.assertEqual(mode(s), sway_ipc.BAR)

    def test_a_foreign_window_on_the_apps_screen_is_not_the_bar(self):
        s, h = scene("es")
        open_app(s, "rp5deck-web", ws.WEB)
        s.map(CC, app_id="foot", title="foot")          # something else lands on the same workspace
        self.assertEqual(mode(s), sway_ipc.HIDDEN)

    def test_a_home_workspace_with_a_name_that_is_not_a_number_is_refused_not_guessed(self):
        s = Sim(outputs=(CC,))
        s.add_ws("main", CC, current=True)
        h = s.map(CC, app_id="emulationstation", title="EmulationStation", fullscreen=True, focus=True)
        open_app(s, "rp5deck-web", ws.WEB)
        n = len(s.received)
        res = switch(s, "internal")
        self.assertFalse(res["ok"])
        self.assertIn("not a plain number", res["refused"])
        self.assertEqual(len(s.received), n)
        self.assertEqual(s.current[CC], ws.UNDOCKED_WS[ws.WEB])

    def test_a_game_that_mapped_hidden_behind_es_changes_nothing_about_the_apps(self):
        s, h = scene("es")
        w = open_app(s, "rp5deck-web", ws.WEB)
        switch(s, "internal")
        game = s.map(CC, app_id="com.libretro.RetroArch", title="RetroArch", fullscreen=True)
        self.assertTrue(switch(s, ws.WEB)["ok"])
        self.assertEqual(focused(s), w)
        self.assertEqual(s.ws_name_of(game), "1")


class TestTheStripFollowsTheShownApp(unittest.TestCase):
    """YouTube picked from the Discord strip, one screen: the mode stays BAR, so only the reason changes, and the
    strip has to switch to YouTube's controls."""

    def test_the_watcher_announces_a_different_window_while_still_bar(self):
        seen = []
        w = sway_ipc.ModeWatcher(lambda m, r: seen.append((m, r)))
        web = "rp5deck window on DSI-1 (undocked): rp5deck-web"
        yt = "rp5deck window on DSI-1 (undocked): rp5deck-ytapp"
        w._emit(sway_ipc.HIDDEN, "undocked")
        w._emit(sway_ipc.BAR, web)
        w._emit(sway_ipc.BAR, web)                       # nothing changed
        w._emit(sway_ipc.BAR, yt)                        # another of our windows is on screen
        w._emit(sway_ipc.BAR, yt)
        w._emit(sway_ipc.HIDDEN, "undocked")
        w._emit(sway_ipc.HIDDEN, "foreign window")       # a changed reason is only news in BAR
        self.assertEqual(seen, [(sway_ipc.HIDDEN, "undocked"), (sway_ipc.BAR, web), (sway_ipc.BAR, yt),
                                (sway_ipc.HIDDEN, "undocked")])

    @unittest.skipUnless(sys.platform.startswith("linux"), "the watcher stop pipe needs POSIX select")
    def test_the_real_watcher_loop_announces_it_from_sway_events(self):
        """ModeWatcher's own loop, with a fake sway socket: a tree where YouTube replaced Discord on screen."""
        import json
        import socket
        import threading
        import time

        s, h = scene("es")
        trees = [s.tree()]                                  # ES alone: HIDDEN
        open_app(s, "rp5deck-web", ws.WEB)
        trees.append(s.tree())                              # Discord on screen: BAR
        open_app(s, "rp5deck-ytapp", ws.YTAPP)
        trees.append(s.tree())                              # YouTube on screen: still BAR
        step = {"i": 0}
        made = []

        class FakeIpc:
            def __init__(self, path, timeout=5.0):
                self.sock, self.peer = socket.socketpair()
                made.append(self)

            def request(self, mtype, payload=""):
                if mtype == sway_ipc.SUBSCRIBE:
                    return {"success": True}
                return trees[step["i"]]

            def recv(self):
                self.sock.recv(1)
                return sway_ipc.EVENT_BIT | 3, b"{}"

            def close(self):
                pass
        seen, lock = [], threading.Lock()

        def cb(m, r):
            with lock:
                seen.append((m, r))
        real_ipc, real_find = sway_ipc.Ipc, sway_ipc.find_socket
        sway_ipc.Ipc, sway_ipc.find_socket = FakeIpc, lambda: "/fake"
        w = sway_ipc.ModeWatcher(cb, debounce=0.05, resync=5.0, internal=CC, external=ES_OUT, max_delay=0.3)
        try:
            w.start()

            def wait_for(n):
                end = time.time() + 5
                while time.time() < end:
                    with lock:
                        if len(seen) >= n:
                            return
                    time.sleep(0.02)
                self.fail("announcements: %r (wanted %d)" % (seen, n))
            wait_for(1)
            for i in (1, 2):
                step["i"] = i
                made[0].peer.send(b"x")                     # a sway event on the subscription socket
                wait_for(i + 1)
        finally:
            w.stop()
            sway_ipc.Ipc, sway_ipc.find_socket = real_ipc, real_find
        self.assertEqual([m for m, _ in seen], [sway_ipc.HIDDEN, sway_ipc.BAR, sway_ipc.BAR])
        self.assertIn("rp5deck-web", seen[1][1])
        self.assertIn("rp5deck-ytapp", seen[2][1])

    def app(self):
        from test_companion import AppCase

        class Case(AppCase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        self.addCleanup(case.tearDown)
        app = case.make_app()
        app.web_calls, app.yt_calls = [], []
        app.web.on_mode = lambda old, new, reason: app.web_calls.append((old, new, reason))
        app.ytapp.on_mode = lambda old, new, reason: app.yt_calls.append((old, new, reason))
        return app

    def test_the_app_tells_both_tiles_when_the_shown_window_changes_within_bar(self):
        app = self.app()
        web = "rp5deck window on DSI-1 (undocked): rp5deck-web"
        yt = "rp5deck window on DSI-1 (undocked): rp5deck-ytapp"
        app.on_mode(sway_ipc.BAR, web)
        self.assertEqual(app.mode, sway_ipc.BAR)
        n_web, n_yt = len(app.web_calls), len(app.yt_calls)
        app.on_mode(sway_ipc.BAR, web)                   # the same again: nothing
        self.assertEqual((len(app.web_calls), len(app.yt_calls)), (n_web, n_yt))
        app.on_mode(sway_ipc.BAR, yt)
        self.assertEqual(app.web_calls[-1], (sway_ipc.BAR, sway_ipc.BAR, yt))
        self.assertEqual(app.yt_calls[-1], (sway_ipc.BAR, sway_ipc.BAR, yt))
        self.assertEqual(app.mode_reason, yt)

    def test_the_strip_reserves_its_space_again_when_a_web_app_follows_youtube(self):
        """YouTube's auto-hide strip reserves nothing; Discord opened after it has to get the full 140 px zone
        even though the geometry is first worked out while the bar still says "tv"."""
        import main
        app = self.app()
        bar = app.ui.cc.bar
        self.assertIsNotNone(app.ytauto)
        web = "rp5deck window on DSI-1 (undocked): rp5deck-web"
        yt = "rp5deck window on DSI-1 (undocked): rp5deck-ytapp"

        def flip(name, key):
            return lambda old, new, reason: setattr(bar, "app", name) if key in (reason or "") else None
        app.ytapp.on_mode = flip("tv", "ytapp")
        app.on_mode(sway_ipc.BAR, yt)
        self.assertEqual(app.layer.geometry[2], 0)
        app.on_mode(sway_ipc.HIDDEN, "undocked: DP-1 absent")
        app.web.on_mode = flip("web", "rp5deck-web")
        app.on_mode(sway_ipc.BAR, web)
        self.assertEqual(app.layer.geometry[2], main.BAR_H)
        # and the same going from Discord straight to YouTube, then back, inside the bar
        app.on_mode(sway_ipc.BAR, yt)
        self.assertEqual(app.layer.geometry[2], 0)
        app.on_mode(sway_ipc.BAR, web)
        self.assertEqual(app.layer.geometry[2], main.BAR_H)

    def test_other_modes_still_only_react_to_a_change_of_mode(self):
        app = self.app()
        app.on_mode(sway_ipc.HIDDEN, "undocked: DP-1 absent")
        n = len(app.web_calls)
        app.on_mode(sway_ipc.HIDDEN, "foreign window on DSI-1: foot")
        self.assertEqual(len(app.web_calls), n)

    @each_home
    def test_switching_to_an_app_that_is_not_open_yet_expects_the_bar_while_another_is_up(self, home):
        s, h = scene(home)
        open_app(s, "rp5deck-web", ws.WEB)
        res = switch(s, ws.YTAPP)                        # YouTube is started after this
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["sent"])
        self.assertEqual(res["expect"], sway_ipc.BAR)    # not HIDDEN: nothing to wait three seconds for

    @each_home
    def test_with_nothing_up_it_still_expects_the_screen_underneath(self, home):
        s, h = scene(home)
        self.assertEqual(switch(s, ws.YTAPP)["expect"], sway_ipc.HIDDEN)


class TestSettingsOverSteam(unittest.TestCase):
    def shown(self, system):
        import companion
        from test_companion import AppCase

        class Case(AppCase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        self.addCleanup(case.tearDown)
        app = case.make_app()
        app.companion.running = companion.Target("game", system, "/roms/x", "X", None, True)
        app.on_mode(sway_ipc.HIDDEN, "undocked: DP-1 absent")
        self.assertTrue(app.cc5.open("summon_button", "undocked: DP-1 absent"))
        return [t.name for t in app.ui.cc.home.overlay_shown_tiles() if t.visible]

    def test_settings_is_offered_over_steam(self):
        self.assertIn("home.settings", self.shown("steam"))

    def test_settings_stays_out_over_any_other_game(self):
        self.assertNotIn("home.settings", self.shown("snes"))

    def test_clean_state_stays_out_over_steam(self):
        self.assertNotIn("home.clean", self.shown("steam"))


class TestSteamAsTheHomeWindow(unittest.TestCase):
    def test_the_gamescope_window_is_the_home_even_with_no_app_id(self):
        s, h = scene("steam")
        open_app(s, "rp5deck-web", ws.WEB)
        sn = snap(s)
        self.assertEqual(sn.home, h)
        self.assertEqual(sn.home_ws, "1")

    def test_home_hands_the_focus_back_to_steam_and_the_payload_says_so(self):
        s, h = scene("steam")
        open_app(s, "rp5deck-ytapp", ws.YTAPP)
        switch(s, "internal")
        self.assertEqual(s.received[-1], "workspace number 1; %s" % ws.build_focus(h))
        self.assertEqual(focused(s), h)

    def test_steam_closing_while_an_app_is_up_leaves_the_app_and_a_way_home(self):
        s, h = scene("steam")
        web = open_app(s, "rp5deck-web", ws.WEB)
        s.close(h)                                      # Steam exits while the Browser is on screen
        self.assertEqual(mode(s), sway_ipc.BAR)
        self.assertEqual(snap(s).home, None)
        res = switch(s, "internal")                     # nothing to go home to: no command, no crash
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["sent"])
        s.close(web)
        self.assertEqual(mode(s), sway_ipc.HIDDEN)


if __name__ == "__main__":
    unittest.main()
