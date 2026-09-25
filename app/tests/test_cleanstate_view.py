#!/usr/bin/env python3
"""cleanstate_view.py (CC1): the confirm / result sheet and its controller,
against a fake host and a fake helper (no process, no command). The patched
main.App wiring is tested in tests/cc1_patched_cases.py (run by test_cc1_merged)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import cleanstate  # noqa: E402
import cleanstate_view  # noqa: E402
import es_health  # noqa: E402
from cleanstate_view import CHECKING, CONFIRM, CONFIRM_FORCE, RESULT, RUNNING  # noqa: E402


class FakeUi:
    def __init__(self):
        self.sheet = None
        self.opened = []

    def open(self, name):
        self.sheet = name
        self.opened.append(name)

    def close(self):
        self.sheet = None


class FakeWeb:
    def __init__(self, app="web"):
        self.app = app
        self.ended = []

    def end_session(self, reason, **kw):
        self.ended.append(reason)
        self.app = None


class Host:
    def __init__(self, web=None):
        self.ui = FakeUi()
        self.web = web
        self.es_output = "DP-1"
        self.state_dirty = False
        self.closed_cc = 0
        self.activity = 0
        self.es_watcher = type("W", (), {"events_seen": 7})()

    def post(self, fn, *a):
        fn(*a)

    def close_sheet(self):
        self.ui.close()

    def close_command_center(self):
        self.closed_cc += 1

    def _activity(self):
        self.activity += 1


class Deferred:
    """A worker the test drives: submit() queues, run() executes one."""

    def __init__(self, host):
        self.host = host
        self.q = []

    def __call__(self, fn, *args, done=None):
        self.q.append((fn, args, done))

    def run(self):
        fn, args, done = self.q.pop(0)
        res = fn(*args)
        if done:
            self.host.post(done, res)


def plan_with(items=(), health=None, notes=()):
    p = cleanstate.Plan()
    p.items = list(items)
    p.health = health
    p.notes = list(notes)
    return p


class FakeHelper:
    def __init__(self, plan, steps=None):
        self.plan = plan
        self.steps = steps
        self.executed = []
        self.discover_kw = None

    def discover(self, **kw):
        self.discover_kw = kw
        kw["progress"]("ES's window is missing; watching up to 30 s")
        return self.plan

    def execute(self, plan, actions, confirmed=False, force_restart=False):
        self.executed.append((list(actions), confirmed, force_restart))
        return self.steps or [cleanstate.Step(a, True, "did %s" % a) for a in actions]


GAME = cleanstate.Item(cleanstate.KILL_EMULATOR, "Game: Super Metroid (snes)",
                       "ES's /emukill first, then ROCKNIX's kill target retroarch (SIGTERM)")
WEB = cleanstate.Item(cleanstate.STOP_CHILDREN, "Browser / Discord: Firefox (rp5deck-web)",
                      "pid 4000")


def health(verdict, summary):
    sig = {"frozen": ["http", "stall", "state"], "suspect": ["stall"]}.get(verdict, [])
    return es_health.Health(verdict, summary, sig, ["HTTP API: timeout twice",
                                                    "main thread: no progress for 5 s"])


class Case(unittest.TestCase):
    def make(self, plan, steps=None, web=None):
        self.host = Host(web)
        self.worker = Deferred(self.host)
        self.helper = FakeHelper(plan, steps)
        self.c = cleanstate_view.CleanStateController(self.host, self.worker,
                                                      helper_factory=lambda: self.helper)
        self.c.sheet.layout((0, 140, 1920, 940))
        return self.c

    def texts(self):
        s = self.c.sheet
        return [s.head.text] + [l.text for l in s.lines if l.text] + [s.never.text]

    def buttons(self):
        s = self.c.sheet
        return {b.name: b.text for b in (s.primary, s.restart, s.cancel) if b.visible}


class TestFlow(Case):
    def test_checking_then_confirm_shows_the_exact_stop_list(self):
        c = self.make(plan_with([GAME, WEB], health("ok", "ES is running and responding")),
                      web=FakeWeb())
        c.open()
        self.assertEqual(self.host.ui.sheet, "cleanstate")
        self.assertEqual(c.phase, CHECKING)
        self.assertEqual(self.buttons(), {"clean.cancel": "Cancel"})
        self.worker.run()
        self.assertEqual(c.phase, CONFIRM)
        t = self.texts()
        self.assertEqual(t[0], "Stop these?")
        self.assertIn("- Game: Super Metroid (snes) - ES's /emukill first, then ROCKNIX's kill "
                      "target retroarch (SIGTERM)", t)
        self.assertIn("- Browser / Discord: Firefox (rp5deck-web) - pid 4000", t)
        self.assertIn("ES: ES is running and responding", t)
        self.assertIn(cleanstate.NEVER_TOUCHED, t)
        self.assertEqual(self.buttons(), {"clean.primary": "Clean up", "clean.cancel": "Cancel"})
        self.assertIsNotNone(self.helper.discover_kw["events_seen"])
        self.assertEqual(self.helper.discover_kw["events_seen"](), 7)

    def test_clean_up_runs_the_plans_actions_and_ends_the_web_session_first(self):
        web = FakeWeb()
        c = self.make(plan_with([GAME, WEB], health("ok", "fine")), web=web)
        c.open()
        self.worker.run()
        c.action("clean.primary")
        self.assertEqual(c.phase, RUNNING)
        self.assertEqual(web.ended, ["clean state"])
        self.assertEqual(self.buttons(), {})              # nothing to press while it runs
        c.action("clean.cancel")                          # ignored while running
        self.assertEqual(c.phase, RUNNING)
        self.worker.run()
        self.assertEqual(self.helper.executed,
                         [([cleanstate.KILL_EMULATOR, cleanstate.STOP_CHILDREN], True, False)])
        self.assertEqual(c.phase, RESULT)
        self.assertEqual(self.texts()[0], "Done")
        self.assertEqual(self.buttons(), {"clean.cancel": "Done"})
        c.action("clean.cancel")
        self.assertIsNone(self.host.ui.sheet)
        self.assertEqual(self.host.closed_cc, 1)          # back to the companion view

    def test_nothing_to_stop(self):
        c = self.make(plan_with([], health("ok", "ES is running and responding")))
        c.open()
        self.worker.run()
        self.assertEqual(self.texts()[0], "Nothing to stop")
        self.assertEqual(self.buttons(), {"clean.cancel": "Close"})
        c.action("clean.primary")                         # hidden button: no-op
        self.assertEqual(self.helper.executed, [])

    def test_frozen_es_offers_restart_and_says_what_was_found(self):
        h = health("frozen", "ES isn't responding (HTTP timeout, UI thread idle for 5 s, "
                             "process stopped)")
        c = self.make(plan_with([], h))
        c.open()
        self.worker.run()
        t = self.texts()
        self.assertIn("ES: ES isn't responding (HTTP timeout, UI thread idle for 5 s, "
                      "process stopped)", t)
        self.assertIn("    HTTP API: timeout twice", t)
        self.assertEqual(self.buttons(), {"clean.restart": "Restart ES", "clean.cancel": "Cancel"})
        c.action("clean.restart")
        self.worker.run()
        self.assertEqual(self.helper.executed, [([cleanstate.RESTART_ES], True, False)])

    def test_frozen_with_apps_cleans_and_restarts(self):
        c = self.make(plan_with([WEB], health("frozen", "ES isn't responding (x)")),
                      web=FakeWeb())
        c.open()
        self.worker.run()
        self.assertEqual(self.buttons()["clean.restart"], "Clean up + restart ES")
        c.action("clean.restart")
        self.worker.run()
        self.assertEqual(self.helper.executed,
                         [([cleanstate.STOP_CHILDREN, cleanstate.RESTART_ES], True, False)])

    def test_suspect_needs_a_second_confirm_and_forces(self):
        c = self.make(plan_with([], health("suspect", "ES might be stuck (HTTP ok, UI thread "
                                                      "idle for 5 s)")))
        c.open()
        self.worker.run()
        self.assertEqual(self.buttons(), {"clean.restart": "Restart anyway",
                                          "clean.cancel": "Cancel"})
        c.action("clean.restart")
        self.assertEqual(c.phase, CONFIRM_FORCE)
        self.assertEqual(self.helper.executed, [])
        self.assertEqual(self.texts()[0], "Restart EmulationStation anyway?")
        self.assertEqual(self.buttons(), {"clean.primary": "Restart ES", "clean.cancel": "Back"})
        c.action("clean.cancel")                          # Back -> the first confirm
        self.assertEqual(c.phase, CONFIRM)
        c.action("clean.restart")
        c.action("clean.primary")
        self.worker.run()
        self.assertEqual(self.helper.executed, [([cleanstate.RESTART_ES], True, True)])

    def test_game_verdict_offers_no_restart(self):
        c = self.make(plan_with([GAME], health("game", "A game is running (retroarch)")))
        c.open()
        self.worker.run()
        self.assertNotIn("clean.restart", self.buttons())

    def test_cancel_while_checking_drops_the_late_answer(self):
        c = self.make(plan_with([GAME], health("ok", "fine")))
        c.open()
        c.action("clean.cancel")
        self.assertIsNone(self.host.ui.sheet)
        self.assertEqual(self.host.closed_cc, 0)          # Cancel keeps the Command Center open
        self.worker.run()                                 # the discover answer arrives late
        self.assertIsNone(c.phase)
        self.assertIsNone(c.plan)

    def test_progress_text_reaches_the_sheet(self):
        c = self.make(plan_with([], health("ok", "fine")))
        c.open()
        fn, args, done = self.worker.q[0]
        res = fn(*args)                                   # progress posted meanwhile
        self.assertEqual(c.sheet.lines[1].text, "ES's window is missing; watching up to 30 s")
        done(res)
        self.assertGreater(self.host.activity, 0)         # keeps the CC from auto-closing

    def test_failed_step_is_shown_red(self):
        steps = [cleanstate.Step(cleanstate.KILL_EMULATOR, False,
                                 "The game is still running (try L1+SELECT+START)")]
        c = self.make(plan_with([GAME], health("ok", "fine")), steps=steps)
        c.open()
        self.worker.run()
        c.action("clean.primary")
        self.worker.run()
        self.assertEqual(self.texts()[0], "Some steps did not work")
        self.assertIn("FAILED  The game is still running (try L1+SELECT+START)", self.texts())

    def test_a_helper_crash_is_shown_not_raised(self):
        c = self.make(plan_with([], None))
        self.helper.discover = lambda **kw: 1 / 0
        c.open()
        self.worker.run()
        self.assertEqual(c.phase, RESULT)
        self.assertIn("Could not check", " ".join(self.texts()))

    def test_long_lists_are_cut_with_a_count(self):
        items = [cleanstate.Item(cleanstate.STOP_CHILDREN, "thing %d" % i) for i in range(15)]
        c = self.make(plan_with(items, health("ok", "fine")))
        c.open()
        self.worker.run()
        # 15 items + the ES line = 16 lines; 9 fit, then the count of the rest
        self.assertEqual(c.sheet.lines[-1].text, "... and 7 more (see the log)")
        self.assertEqual(c.sheet.lines[0].text, "- thing 0")

    def test_state_is_json_able(self):
        import json
        c = self.make(plan_with([GAME], health("frozen", "x")))
        c.open()
        self.worker.run()
        json.dumps(c.state(), default=str)


if __name__ == "__main__":
    unittest.main()
