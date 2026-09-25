#!/usr/bin/env python3
"""dualscreen_keys_view.py (DS): the confirm/result sheet and its controller,
against a fake host and fake checker()/restorer() (no file, no process). The
Home banner wiring (screens.Home.set_dualscreen_notice) is exercised through
a fake ui.home stand-in here; the real Home is covered by
tests/test_screens.py's TestDualScreenNotice."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dualscreen_keys_view as dkv  # noqa: E402
from dualscreen_keys_view import CONFIRM, RESULT, RUNNING  # noqa: E402


class FakeHome:
    def __init__(self):
        self.notice = None

    def set_dualscreen_notice(self, text):
        self.notice = text


class FakeUi:
    def __init__(self):
        self.home = FakeHome()
        self.sheet = None
        self.opened = []

    def open(self, name):
        self.sheet = name
        self.opened.append(name)

    def close(self):
        self.sheet = None


class Host:
    def __init__(self):
        self.ui = FakeUi()
        self.state_dirty = False

    def post(self, fn, *a):
        fn(*a)

    def close_sheet(self):
        self.ui.close()


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


def ok(missing=(), error=None):
    return {"available": error is None, "missing": list(missing) if error is None else None,
            "error": error}


class Case(unittest.TestCase):
    def make(self, check_results=(), restore_results=()):
        self.host = Host()
        self.worker = Deferred(self.host)
        self.checks = list(check_results)
        self.restores = list(restore_results)
        self.checked = []
        self.restored = []

        def checker():
            self.checked.append(1)
            return self.checks.pop(0)

        def restorer(missing):
            self.restored.append(list(missing))
            return self.restores.pop(0)

        self.c = dkv.DualScreenKeysController(self.host, self.worker, checker=checker,
                                              restorer=restorer)
        self.c.sheet.layout((0, 140, 1920, 940))
        return self.c

    def texts(self):
        s = self.c.sheet
        return [s.head.text] + [l.text for l in s.lines if l.text]

    def buttons(self):
        s = self.c.sheet
        return {b.name: b.text for b in (s.primary, s.cancel) if b.visible}


class TestPoll(Case):
    def test_missing_keys_set_the_banner(self):
        c = self.make(check_results=[ok(["3ds.screen_layout", "wiiu.gamepad_enabled"])])
        c.poll()
        self.worker.run()
        self.assertIn("3ds.screen_layout", self.host.ui.home.notice)
        self.assertIn("wiiu.gamepad_enabled", self.host.ui.home.notice)
        self.assertEqual(c.missing, ["3ds.screen_layout", "wiiu.gamepad_enabled"])

    def test_nothing_missing_clears_the_banner(self):
        c = self.make(check_results=[ok([])])
        self.host.ui.home.notice = "stale"
        c.poll()
        self.worker.run()
        self.assertEqual(self.host.ui.home.notice, "")

    def test_cannot_check_is_never_shown_as_missing(self):
        c = self.make(check_results=[ok(error="cannot check: [Errno 2] no such file")])
        self.host.ui.home.notice = "stale"
        c.poll()
        self.worker.run()
        self.assertEqual(self.host.ui.home.notice, "")
        self.assertEqual(c.missing, [])

    def test_a_poll_already_inflight_is_not_duplicated(self):
        c = self.make(check_results=[ok([]), ok([])])
        c.poll()
        c.poll()                    # ignored: the first is still inflight
        self.assertEqual(len(self.worker.q), 1)
        self.worker.run()
        self.assertEqual(self.checked, [1])


class TestSheetFlow(Case):
    def test_open_without_missing_keys_is_a_no_op(self):
        c = self.make()
        c.open()
        self.assertIsNone(self.host.ui.sheet)
        self.assertIsNone(c.phase)

    def test_open_shows_the_missing_keys_and_a_confirm(self):
        c = self.make(check_results=[ok(["3ds.screen_layout"])])
        c.poll()
        self.worker.run()
        c.open()
        self.assertEqual(self.host.ui.sheet, "dualscreen")
        self.assertEqual(c.phase, CONFIRM)
        t = self.texts()
        self.assertEqual(t[0], "Restore dual-screen settings?")
        self.assertIn("Missing: 3ds.screen_layout", t)
        self.assertIn("EmulationStation will restart for a few seconds.", t)
        self.assertEqual(self.buttons(), {"ds.primary": "Restore", "ds.cancel": "Cancel"})

    def test_cancel_before_running_closes_without_restoring(self):
        c = self.make(check_results=[ok(["3ds.screen_layout"])])
        c.poll()
        self.worker.run()
        c.open()
        c.action("ds.cancel")
        self.assertIsNone(self.host.ui.sheet)
        self.assertIsNone(c.phase)
        self.assertEqual(self.restored, [])

    def test_restore_runs_and_shows_success(self):
        c = self.make(check_results=[ok(["3ds.screen_layout"]), ok([])],
                      restore_results=[{"ok": True, "detail": "restored: 3ds.screen_layout",
                                        "restored": ["3ds.screen_layout"], "backup": "/x"}])
        c.poll()
        self.worker.run()
        c.open()
        c.action("ds.primary")
        self.assertEqual(c.phase, RUNNING)
        self.assertEqual(self.buttons(), {})           # nothing to press while it runs
        c.action("ds.cancel")                           # ignored while running
        self.assertEqual(c.phase, RUNNING)
        self.worker.run()
        self.assertEqual(self.restored, [["3ds.screen_layout"]])
        self.assertEqual(c.phase, RESULT)
        self.assertEqual(self.texts()[0], "Done")
        self.assertIn("restored: 3ds.screen_layout", self.texts())
        self.assertEqual(self.buttons(), {"ds.cancel": "Done"})
        # a successful restore re-polls, clearing the banner once the file
        # really does have the keys.
        self.worker.run()
        self.assertEqual(self.host.ui.home.notice, "")
        c.action("ds.cancel")
        self.assertIsNone(self.host.ui.sheet)

    def test_restore_failure_is_shown_and_does_not_repoll(self):
        c = self.make(check_results=[ok(["3ds.screen_layout"])],
                      restore_results=[{"ok": False, "detail": "refused: a game is running",
                                        "restored": [], "backup": None}])
        c.poll()
        self.worker.run()
        c.open()
        c.action("ds.primary")
        self.worker.run()
        self.assertEqual(c.phase, RESULT)
        self.assertEqual(self.texts()[0], "Could not restore")
        self.assertIn("refused: a game is running", self.texts())
        self.assertEqual(self.worker.q, [])             # no re-poll queued

    def test_a_stale_generations_answer_is_dropped(self):
        # same generation guard as cleanstate_view's own CleanStateController
        # (never trust an answer that started before the sheet moved on).
        c = self.make(check_results=[ok(["3ds.screen_layout"])],
                      restore_results=[{"ok": True, "detail": "restored", "restored": [],
                                        "backup": None}])
        c.poll()
        self.worker.run()
        c.open()
        c.action("ds.primary")
        fn, args, done = self.worker.q[0]
        c.gen += 1                          # e.g. the sheet was reset from elsewhere meanwhile
        res = fn(*args)
        done(res)
        self.assertEqual(c.phase, RUNNING)   # the stale answer never overwrote the live phase

    def test_state_is_json_able(self):
        import json
        c = self.make(check_results=[ok(["3ds.screen_layout"])])
        c.poll()
        self.worker.run()
        c.open()
        json.dumps(c.state(), default=str)


if __name__ == "__main__":
    unittest.main()
