#!/usr/bin/env python3
"""osk.py offline: the show/hide policy for the Firefox keyboard and the
wvkbd process wrapper (Popen faked; nothing is launched).

The rule that matters most: the keyboard is only shown while FIREFOX has
focus. wvkbd types into whatever is focused, so showing it while ES or a
game is focused would send the owner's keystrokes to them.
"""
import os
import signal
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import osk  # noqa: E402


class TestResolveMode(unittest.TestCase):
    def test_config_wins_then_env_then_default(self):
        self.assertEqual(osk.resolve_mode("button", {"RP5DECK_OSK": "off"}), "button")
        self.assertEqual(osk.resolve_mode(None, {"RP5DECK_OSK": "OFF "}), "off")
        self.assertEqual(osk.resolve_mode("bogus", {"RP5DECK_OSK": "nope"}), "auto")
        self.assertEqual(osk.resolve_mode(None, {}), "auto")
        self.assertEqual(osk.resolve_mode(3, {}), "auto")


class TestAutoPolicy(unittest.TestCase):
    def setUp(self):
        self.p = osk.OskPolicy("auto")

    def test_shows_for_a_focused_text_field(self):
        self.assertFalse(self.p.update(True, False))
        self.assertTrue(self.p.update(True, True))

    def test_never_shows_while_firefox_is_not_focused(self):
        # an editable element in an UNFOCUSED Firefox: the keys would go to ES
        for _ in range(5):
            self.assertFalse(self.p.update(False, True))

    def test_hide_needs_two_misses_in_a_row(self):
        self.p.update(True, True)
        self.assertTrue(self.p.update(True, False))      # one miss: still up
        self.assertTrue(self.p.update(True, True))       # back in a field: counter reset
        self.assertTrue(self.p.update(True, False))
        self.assertFalse(self.p.update(True, False))     # second miss in a row: down

    def test_losing_focus_hides_it(self):
        self.p.update(True, True)
        self.p.update(False, True)
        self.assertFalse(self.p.update(False, True))

    def test_owner_hide_lasts_until_a_new_text_field(self):
        self.p.update(True, True)
        self.assertEqual(self.p.toggle(True), (False, None))
        self.assertFalse(self.p.update(True, True))      # same field: stays hidden
        self.assertFalse(self.p.update(True, False))     # left the field
        self.assertTrue(self.p.update(True, True))       # a new field: shows again

    def test_owner_can_ask_for_it_without_a_text_field(self):
        # e.g. Firefox's own address bar, which the page probe cannot see
        self.assertEqual(self.p.toggle(True), (True, None))
        self.assertTrue(self.p.update(True, False))
        self.assertTrue(self.p.update(True, False))

    def test_owner_requested_keyboard_still_needs_focus(self):
        # asked for by the Keyboard button, then the owner taps the top screen:
        # ES / the game has focus now, so the keyboard must go
        self.assertEqual(self.p.toggle(True), (True, None))
        self.p.update(False, False)
        self.assertFalse(self.p.update(False, False))
        bp = osk.OskPolicy("button")
        bp.toggle(True)
        bp.update(False, False)
        self.assertFalse(bp.update(False, False))

    def test_toggle_while_unfocused_arms_it_with_a_hint(self):
        want, hint = self.p.toggle(False)
        self.assertFalse(want)
        self.assertEqual(hint, "Tap the page first")
        self.assertFalse(self.p.update(False, False))
        self.assertTrue(self.p.update(True, False))      # the owner tapped the page

    def test_reset_forgets_everything(self):
        self.p.toggle(True)
        self.p.reset()
        self.assertFalse(self.p.update(True, False))


class TestButtonAndOffPolicies(unittest.TestCase):
    def test_button_mode_ignores_text_fields(self):
        p = osk.OskPolicy("button")
        self.assertFalse(p.update(True, True))
        self.assertEqual(p.toggle(True), (True, None))
        self.assertTrue(p.update(True, False))
        self.assertEqual(p.toggle(True), (False, None))
        self.assertFalse(p.update(True, True))

    def test_off_mode_never_shows(self):
        p = osk.OskPolicy("off")
        self.assertFalse(p.enabled())
        self.assertEqual(p.toggle(True), (False, None))
        self.assertFalse(p.update(True, True))

    def test_unknown_mode_falls_back_to_auto(self):
        self.assertEqual(osk.OskPolicy("sometimes").mode, "auto")


class FakeProc:
    def __init__(self, pid=77, stubborn=False):
        self.pid = pid
        self.returncode = None
        self.signals = []
        self.stubborn = stubborn
        self.killed = False

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if not self.stubborn:
            self.returncode = -sig

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("wvkbd", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakePopen:
    def __init__(self, stubborn=False, fail=False):
        self.calls = []
        self.procs = []
        self.stubborn = stubborn
        self.fail = fail

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.fail:
            raise FileNotFoundError(cmd[0])
        p = FakeProc(pid=100 + len(self.procs), stubborn=self.stubborn)
        self.procs.append(p)
        return p


class TestWvkbd(unittest.TestCase):
    def test_command_uses_only_the_flags_e1b_proved(self):
        k = osk.Wvkbd(output="DSI-1", height=360, popen=FakePopen())
        self.assertEqual(k.build_command(), ["wvkbd-mobintl", "-L", "360", "--output", "DSI-1"])

    def test_show_starts_once_and_hide_stops_it(self):
        fp = FakePopen()
        k = osk.Wvkbd(popen=fp)
        self.assertTrue(k.show())
        self.assertTrue(k.show())                        # idempotent
        self.assertEqual(len(fp.calls), 1)
        self.assertTrue(k.visible())
        self.assertEqual(k.pid(), 100)
        env = fp.calls[0][1]["env"]
        self.assertIn("WAYLAND_DISPLAY", env)
        self.assertIs(fp.calls[0][1]["stdin"], subprocess.DEVNULL)
        k.hide()
        self.assertEqual(fp.procs[0].signals, [signal.SIGTERM])
        self.assertFalse(k.visible())
        self.assertIsNone(k.pid())
        self.assertTrue(k.show())                        # a fresh process next time
        self.assertEqual(len(fp.calls), 2)

    def test_hide_escalates_to_kill(self):
        fp = FakePopen(stubborn=True)
        k = osk.Wvkbd(popen=fp)
        k.show()
        k.hide()
        self.assertTrue(fp.procs[0].killed)
        self.assertFalse(k.visible())

    def test_missing_binary_is_an_error_not_a_crash(self):
        k = osk.Wvkbd(popen=FakePopen(fail=True))
        self.assertFalse(k.show())
        self.assertIn("wvkbd-mobintl", k.error)
        self.assertFalse(k.visible())
        k.hide()                                         # harmless

    def test_a_keyboard_that_died_reads_hidden_and_restarts(self):
        fp = FakePopen()
        k = osk.Wvkbd(popen=fp)
        k.show()
        fp.procs[0].returncode = 1                       # crashed
        self.assertFalse(k.visible())
        k.show()
        self.assertEqual(len(fp.calls), 2)


if __name__ == "__main__":
    unittest.main()
