"""Batch 1 (25 Sep): the Top screen and Performance tiles, and Settings > Command Center >
"Edit buttons...". The real main.App / DeckUI (no SDL), driven by taps."""
import os
import sys
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import main  # noqa: E402
import perf_profile  # noqa: E402
import summon  # noqa: E402
from test_companion import AppCase  # noqa: E402

PULL = summon.PullDownStateMachine


class Batch1Tiles(AppCase):
    def setUp(self):
        AppCase.setUp(self)
        d = tempfile.mkdtemp(prefix="b1-perf-")
        self.addCleanup(shutil.rmtree, d, True)
        self.mode_path = os.path.join(d, "perf-mode")
        orig = perf_profile.MODE_PATH
        perf_profile.MODE_PATH = self.mode_path
        self.addCleanup(setattr, perf_profile, "MODE_PATH", orig)
        self.popen = []
        orig_popen = main.subprocess.Popen
        main.subprocess.Popen = lambda argv, **kw: self.popen.append(argv)
        self.addCleanup(setattr, main.subprocess, "Popen", orig_popen)

    def open_cc(self, app):
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "cc")

    def perf_subtitle(self, app):
        return app.ui.cc.home._tiles_by_name["home.perf"].subtitle

    def test_new_tiles_are_on_home(self):
        app = self.make_app()
        self.open_cc(app)
        t = app.ui.targets()
        self.assertIn("home.topscreen", t)
        self.assertIn("home.perf", t)

    def test_performance_cycles_auto_max_saver(self):
        app = self.make_app()
        self.open_cc(app)
        self.assertIn("Auto", self.perf_subtitle(app))
        seen = []
        for _ in range(3):
            self.tap(app, "home.perf")
            seen.append(open(self.mode_path).read().strip())
        self.assertEqual(seen, ["max", "saver", "auto"])
        self.assertIn("Auto", self.perf_subtitle(app))

    def test_performance_tile_shows_the_saved_mode_at_start(self):
        with open(self.mode_path, "w") as f:
            f.write("saver\n")
        app = self.make_app()
        self.assertIn("Saver", self.perf_subtitle(app))

    def test_top_screen_asks_first_then_runs_the_script(self):
        app = self.make_app()
        self.open_cc(app)
        self.tap(app, "home.topscreen")
        self.assertEqual(self.popen, [], "turned off without asking")
        app.top_screen_off_now()          # the confirm sheet's Yes
        self.assertEqual(self.popen, [["sh", os.path.join(main.HERE, "top-screen.sh"), "off"]])

    def test_top_screen_no_does_nothing(self):
        app = self.make_app()
        self.open_cc(app)
        self.tap(app, "home.topscreen")
        app.close_sheet()
        self.assertEqual(self.popen, [])

    def test_settings_edit_buttons_opens_edit_mode(self):
        app = self.make_app()
        self.open_cc(app)
        app.open_settings()
        self.assertEqual(app.pull.state, PULL.SETTINGS)
        sheet = app.ui.settings if hasattr(app.ui, "settings") else None
        row = getattr(sheet, "command_center_extra", None)
        if row is None:                   # find the sheet holding the row
            for w in (getattr(app.ui, n, None) for n in dir(app.ui)):
                if hasattr(w, "command_center_extra"):
                    row = w.command_center_extra
                    break
        self.assertIsNotNone(row, "no Edit buttons row in Settings")
        row.edit_btn.on_click()
        self.assertEqual(app.pull.state, PULL.COMMAND_CENTER)
        self.assertTrue(app.ui.cc.home.edit_mode)


class PowerAndQuit(Batch1Tiles):
    """Batch 1: the Power sheet (Sleep / Restart / Shut down / Hibernate) and the
    overlay's Quit game (Clean state's own game stop)."""

    def ran(self, app):
        calls = []
        app.sleep_parts["power_run"] = lambda cmd: calls.append(list(cmd))
        return calls

    def test_power_tile_opens_the_sheet_with_hibernate_disabled(self):
        app = self.make_app()
        self.open_cc(app)
        self.tap(app, "home.power")
        self.assertEqual(app.ui.sheet, "power")
        t = app.ui.targets()
        for n in ("power.sleep", "power.restart", "power.shutdown"):
            self.assertIn(n, t)
        self.assertNotIn("power.hibernate", t)          # disabled: not a target
        self.assertIn("work in progress", app.ui.cc.power.note.text)
        wip = app.ui.cc.power.wip
        self.assertEqual(wip.text, "Work in progress")
        hx, hy, hw, hh = app.ui.cc.power.hibernate.rect
        self.assertEqual((wip.rect[0], wip.rect[2]), (hx, hw))       # under Hibernate
        self.assertGreaterEqual(wip.rect[1], hy + hh)

    def test_restart_and_shutdown_ask_first(self):
        for name, cmd in (("restart", ["systemctl", "reboot"]),
                          ("shutdown", ["systemctl", "poweroff"])):
            app = self.make_app()
            calls = self.ran(app)
            self.open_cc(app)
            self.tap(app, "home.power")
            self.tap(app, "power." + name)
            self.assertEqual(calls, [], "%s ran without asking" % name)
            self.assertEqual(app.ui.sheet, "confirm")
            self.tap(app, "confirm.yes")
            app.post.drain()
            self.assertEqual(calls, [cmd])

    def test_cancel_does_nothing(self):
        app = self.make_app()
        calls = self.ran(app)
        self.open_cc(app)
        self.tap(app, "home.power")
        self.tap(app, "power.shutdown")
        self.tap(app, "confirm.no")
        self.assertEqual(calls, [])

    def test_sleep_goes_through_the_existing_sleep_flow(self):
        app = self.make_app()
        seen = []
        app.open_sleep = lambda: seen.append("sleep")
        self.open_cc(app)
        self.tap(app, "home.power")
        self.tap(app, "power.sleep")
        self.assertEqual(seen, ["sleep"])

    def test_quit_game_asks_then_uses_the_clean_state_stop(self):
        import cleanstate
        app = self.make_app()
        done = []

        class FakeHelper:
            def discover(self, **kw):
                done.append(("discover", kw))
                return "plan"

            def execute(self, plan, actions, confirmed=False, force_restart=False):
                done.append(("execute", plan, list(actions), confirmed))
                return [cleanstate.Step(cleanstate.KILL_EMULATOR, True, "Game closed")]
        app.clean.helper_factory = FakeHelper
        app._submit_clean = lambda fn, *a, done=None: done(fn(*a))
        app.ask_quit_game()
        self.assertEqual(app.ui.sheet, "confirm")
        self.assertEqual(done, [], "quit without asking")
        app.quit_game_now()
        self.assertEqual(done[-1], ("execute", "plan", [cleanstate.KILL_EMULATOR], True))

    def test_overlay_has_hotkeys_performance_and_quit(self):
        import screens
        import cc_overlay
        for tiles in (screens.OVERLAY_TILES, cc_overlay.OVERLAY_TILES):
            for n in ("home.hotkeys", "home.perf", "home.quitgame", "home.mixer", "home.hud"):
                self.assertIn(n, tiles)
        app = self.make_app()
        home = app.ui.cc.home
        home.set_overlay(True)
        shown = [t.name for t in home.overlay_shown_tiles()]
        for n in ("home.hotkeys", "home.perf", "home.quitgame"):
            self.assertIn(n, shown)
        home.set_overlay(False)
        self.assertNotIn("home.quitgame", [t.name for t in home.visible_tiles()])

    def test_overlay_notes_tile_opens_the_games_notebook(self):
        import companion
        import tempfile
        import shutil
        d = tempfile.mkdtemp(prefix="notes-overlay-")
        self.addCleanup(shutil.rmtree, d, True)
        app = self.make_app()
        app.ui.cc.notes.book.path = os.path.join(d, "notebook.json")
        home = app.ui.cc.home
        home.set_overlay(True)
        tile = next(t for t in home.overlay_shown_tiles() if t.name == "home.notes")
        app.companion.running = companion.Target("game", "psx", "/r/Vib-Ribbon.chd", "Vib-Ribbon", None, True)
        tile.on_click()
        self.assertEqual(app.ui.sheet, "notes")
        self.assertEqual(app.ui.cc.notes.current_name(), "Vib-Ribbon (psx)")
        home.set_overlay(False)
        self.assertNotIn("home.notes", [t.name for t in home.visible_tiles()])

    def test_saved_sleep_slot_becomes_power(self):
        import config
        import json
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "config.json")
        order = list(reversed([k.replace("home.power", "home.sleep") for k in config.HOME_TILE_KEYS]))
        with open(p, "w") as f:
            json.dump({"schema_version": 1, "command_center": {"tile_order": order,
                                                               "hidden_tiles": ["home.sleep"]}}, f)
        cfg, note = config.load(p)
        got = config.get_value(cfg, ("command_center", "tile_order"))
        self.assertEqual(got, [k.replace("home.sleep", "home.power") for k in order])
        self.assertEqual(config.get_value(cfg, ("command_center", "hidden_tiles")), ["home.power"])


if __name__ == "__main__":
    unittest.main()
