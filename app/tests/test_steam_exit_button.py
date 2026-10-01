"""The Steam tab's Exit Steam button: the first tap arms it, a second tap within a few seconds closes Steam through
Clean state's exit-steam step (with or without a game running), and the arming wears off or is dropped on leaving."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import cleanstate  # noqa: E402
import steam_view  # noqa: E402
from test_steam_tab import FakeHost, FakeSteam, games  # noqa: E402


class TimerHost(FakeHost):
    def __init__(self):
        FakeHost.__init__(self)
        self.timers, self.cancelled, self.exits = [], [], 0

    def call_later(self, delay, fn):
        h = [delay, fn]
        self.timers.append(h)
        return h

    def cancel(self, h):
        self.cancelled.append(h)

    def exit_steam_now(self):
        self.exits += 1


class TestExitButton(unittest.TestCase):
    def setUp(self):
        self.host = TimerHost()
        run_now = lambda fn, *a, done=None: done(fn(*a)) if done else fn(*a)  # noqa: E731
        self.c = steam_view.SteamLibraryController(self.host, run_now, run_now, steam=FakeSteam(games(3)),
                                                   load_image=lambda *a: None, now=lambda: 1_800_000_000)
        self.c.sheet.layout((0, 0, 1920, 810))

    def test_the_button_is_in_the_sheet_and_named(self):
        self.assertEqual((self.c.sheet.exit.name, self.c.sheet.exit.text), ("steam.exit", "Exit Steam"))

    def test_the_header_buttons_do_not_overlap(self):
        s = self.c.sheet
        for a, b in ((s.back, s.sort), (s.sort, s.exit), (s.exit, s.title), (s.title, s.prev)):
            self.assertLessEqual(a.rect[0] + a.rect[2], b.rect[0], (a.name, b.name))

    def test_one_tap_only_arms_it(self):
        self.c.action("steam.exit")
        self.assertEqual(self.host.exits, 0)
        self.assertEqual(self.c.sheet.exit.text, "Tap again to exit")
        self.assertEqual(len(self.host.timers), 1)

    def test_the_second_tap_exits_steam_and_resets_the_button(self):
        self.c.action("steam.exit")
        self.c.action("steam.exit")
        self.assertEqual(self.host.exits, 1)
        self.assertEqual(self.c.sheet.exit.text, "Exit Steam")
        self.assertEqual(len(self.host.cancelled), 1)

    def test_the_arming_wears_off(self):
        self.c.action("steam.exit")
        self.host.timers[0][1]()
        self.assertEqual(self.c.sheet.exit.text, "Exit Steam")
        self.c.action("steam.exit")
        self.assertEqual(self.host.exits, 0)

    def test_leaving_the_sheet_drops_the_arming(self):
        self.c.action("steam.exit")
        self.c.close()
        self.assertEqual(self.c.sheet.exit.text, "Exit Steam")
        self.c.action("steam.exit")
        self.assertEqual(self.host.exits, 0)

    def test_it_works_with_no_game_running(self):
        self.c.running = None
        self.c.action("steam.exit")
        self.c.action("steam.exit")
        self.assertEqual(self.host.exits, 1)


class TestTheAppSide(unittest.TestCase):
    def setUp(self):
        from test_companion import AppCase

        class Case(AppCase):
            def runTest(self):
                pass
        self.case = Case()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.app = self.case.make_app()

    def test_it_runs_clean_states_exit_steam_step_confirmed(self):
        calls = []

        class Helper:
            def discover(self, check_health=True):
                calls.append(("discover", check_health))
                return "plan"

            def execute(self, plan, actions, confirmed=False, force_restart=False):
                calls.append(("execute", plan, tuple(actions), confirmed))
                return []

        self.app.clean = type("C", (), {"helper": Helper(), "helper_factory": staticmethod(lambda: None)})()
        self.app._submit_clean = lambda fn, done=None: done(fn())
        self.app.exit_steam_now()
        self.assertEqual(calls, [("discover", False), ("execute", "plan", (cleanstate.EXIT_STEAM,), True)])

    def test_the_result_text_follows_the_steps(self):
        shown = []
        self.app.ui.bar.show_hint = shown.append
        ok = type("S", (), {"ok": True, "text": "Steam closed"})()
        bad = type("S", (), {"ok": False, "text": "Steam is still running"})()
        self.app._exit_steam_done([ok])
        self.app._exit_steam_done([bad])
        self.app._exit_steam_done([])
        self.assertEqual(shown, ["Steam closed", "Steam is still running", "Steam is still running"])


if __name__ == "__main__":
    unittest.main()
