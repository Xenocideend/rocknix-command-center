#!/usr/bin/env python3
"""system_sleep.py - the Sleep tile's own `systemctl suspend` call. Every
test injects a fake `run` (subprocess.run-like); nothing here ever touches
a real systemctl or actually suspends anything."""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import system_sleep  # noqa: E402


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestSuspend(unittest.TestCase):
    def test_success(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return FakeCompleted(0)
        ok, detail = system_sleep.suspend(run=run)
        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertEqual(calls, [system_sleep.SUSPEND_CMD])

    def test_nonzero_exit_is_reported(self):
        ok, detail = system_sleep.suspend(run=lambda cmd, **kw: FakeCompleted(1, stderr="nope"))
        self.assertFalse(ok)
        self.assertIn("nope", detail)

    def test_missing_systemctl_never_raises(self):
        def run(cmd, **kw):
            raise FileNotFoundError("no such file")
        ok, detail = system_sleep.suspend(run=run)
        self.assertFalse(ok)
        self.assertIn("not found", detail)

    def test_timeout_never_raises(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        ok, detail = system_sleep.suspend(run=run)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_other_oserror_never_raises(self):
        def run(cmd, **kw):
            raise OSError("permission denied")
        ok, detail = system_sleep.suspend(run=run)
        self.assertFalse(ok)
        self.assertIn("permission denied", detail)

    def test_long_stderr_is_truncated(self):
        ok, detail = system_sleep.suspend(
            run=lambda cmd, **kw: FakeCompleted(1, stderr="x" * 500))
        self.assertFalse(ok)
        self.assertLessEqual(len(detail), 160)

    def test_default_run_parameter_is_subprocess_run(self):
        # Never actually calls it (that would risk a REAL systemctl suspend
        # on whatever machine runs this suite, exactly what the brief says
        # never to do) - just proves the wiring, not the behaviour.
        self.assertIs(system_sleep.suspend.__defaults__[0], None)
        import inspect
        src = inspect.getsource(system_sleep.suspend)
        self.assertIn("run = run or subprocess.run", src)


if __name__ == "__main__":
    unittest.main()
