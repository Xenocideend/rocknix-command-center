"""tests for rocknix_keyboard.py (owner: a Command Center control to "type
into emulator settings" - toggle ROCKNIX's own touchkeyboard.service
wvkbd-mobintl). Everything here runs against a fake /proc directory and a
fake signal sender - no real process is ever touched."""
import os
import shutil
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import rocknix_keyboard  # noqa: E402


def _write_proc(proc_dir, pid, argv):
    d = os.path.join(proc_dir, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\x00".join(a.encode("utf-8") for a in argv) + b"\x00")


class FakeProc(unittest.TestCase):
    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="rocknix-keyboard-proc-")
        self.addCleanup(shutil.rmtree, self.proc, ignore_errors=True)


class TestFindPid(FakeProc):
    def test_empty_proc_finds_nothing(self):
        self.assertIsNone(rocknix_keyboard.find_pid(self.proc))

    def test_finds_the_rocknix_instance(self):
        _write_proc(self.proc, 123, ["/usr/bin/wvkbd-mobintl", "-l", "simple", "--hidden"])
        self.assertEqual(rocknix_keyboard.find_pid(self.proc), 123)

    def test_ignores_rp5decks_own_osk_instance(self):
        # osk.py's own wvkbd-mobintl for Firefox: "-L <height> --output <name>",
        # no --hidden, no "-l simple" - must never be matched.
        _write_proc(self.proc, 55, ["/usr/bin/wvkbd-mobintl", "-L", "360", "--output", "DSI-1"])
        self.assertIsNone(rocknix_keyboard.find_pid(self.proc))

    def test_both_present_only_the_rocknix_one_matches(self):
        _write_proc(self.proc, 55, ["/usr/bin/wvkbd-mobintl", "-L", "360", "--output", "DSI-1"])
        _write_proc(self.proc, 123, ["/usr/bin/wvkbd-mobintl", "-l", "simple", "--hidden"])
        self.assertEqual(rocknix_keyboard.find_pid(self.proc), 123)

    def test_requires_hidden_and_simple_together(self):
        _write_proc(self.proc, 1, ["/usr/bin/wvkbd-mobintl", "--hidden"])
        _write_proc(self.proc, 2, ["/usr/bin/wvkbd-mobintl", "-l", "simple"])
        self.assertIsNone(rocknix_keyboard.find_pid(self.proc))

    def test_requires_l_and_simple_adjacent(self):
        # "-l" and "simple" both present but not as one flag/value pair.
        _write_proc(self.proc, 1, ["/usr/bin/wvkbd-mobintl", "--hidden", "simple", "-l", "other"])
        self.assertIsNone(rocknix_keyboard.find_pid(self.proc))

    def test_ignores_unrelated_processes(self):
        _write_proc(self.proc, 1, ["/usr/bin/sway"])
        _write_proc(self.proc, 2, ["/usr/bin/emulationstation"])
        self.assertIsNone(rocknix_keyboard.find_pid(self.proc))

    def test_ignores_non_numeric_proc_entries(self):
        os.makedirs(os.path.join(self.proc, "self"), exist_ok=True)
        os.makedirs(os.path.join(self.proc, "net"), exist_ok=True)
        self.assertIsNone(rocknix_keyboard.find_pid(self.proc))

    def test_missing_proc_dir_returns_none_not_raise(self):
        self.assertIsNone(rocknix_keyboard.find_pid(os.path.join(self.proc, "nope")))

    def test_a_pid_that_vanishes_mid_scan_is_skipped(self):
        # A directory listed by listdir() but whose cmdline is gone by the
        # time it is read (the process just exited) - must not raise.
        os.makedirs(os.path.join(self.proc, "999"), exist_ok=True)  # no cmdline file
        _write_proc(self.proc, 123, ["/usr/bin/wvkbd-mobintl", "-l", "simple", "--hidden"])
        self.assertEqual(rocknix_keyboard.find_pid(self.proc), 123)

    def test_matches_basename_not_full_path(self):
        _write_proc(self.proc, 7, ["wvkbd-mobintl", "-l", "simple", "--hidden"])
        self.assertEqual(rocknix_keyboard.find_pid(self.proc), 7)


class TestToggle(FakeProc):
    def setUp(self):
        FakeProc.setUp(self)
        self.sent = []

    def _kill(self, pid, sig):
        self.sent.append((pid, sig))

    def test_no_process_returns_false_and_never_signals(self):
        ok = rocknix_keyboard.toggle(self.proc, kill_fn=self._kill)
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])

    def test_signals_exactly_the_found_pid_with_toggle_signal(self):
        _write_proc(self.proc, 321, ["/usr/bin/wvkbd-mobintl", "-l", "simple", "--hidden"])
        ok = rocknix_keyboard.toggle(self.proc, kill_fn=self._kill)
        self.assertTrue(ok)
        self.assertEqual(self.sent, [(321, rocknix_keyboard.TOGGLE_SIGNAL)])

    def test_toggle_signal_is_34(self):
        # input_sense's own `kill -34 $(pidof wvkbd-mobintl)`.
        self.assertEqual(rocknix_keyboard.TOGGLE_SIGNAL, 34)

    def test_never_signals_a_decoy_process(self):
        _write_proc(self.proc, 55, ["/usr/bin/wvkbd-mobintl", "-L", "360", "--output", "DSI-1"])
        _write_proc(self.proc, 123, ["/usr/bin/wvkbd-mobintl", "-l", "simple", "--hidden"])
        rocknix_keyboard.toggle(self.proc, kill_fn=self._kill)
        self.assertEqual(self.sent, [(123, rocknix_keyboard.TOGGLE_SIGNAL)])

    def test_a_stale_pid_that_disappears_fails_soft(self):
        _write_proc(self.proc, 321, ["/usr/bin/wvkbd-mobintl", "-l", "simple", "--hidden"])

        def kill_raises(pid, sig):
            raise OSError("no such process")
        ok = rocknix_keyboard.toggle(self.proc, kill_fn=kill_raises)
        self.assertFalse(ok)

    def test_default_kill_fn_is_os_kill(self):
        import inspect
        sig = inspect.signature(rocknix_keyboard.toggle)
        self.assertIsNone(sig.parameters["kill_fn"].default)


if __name__ == "__main__":
    unittest.main()
