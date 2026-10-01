"""sync_dual_flag restarts ROCKNIX's touchscreen keyboard when the dual-screen flag changes (the
service reads the flag only at its start). The real function under dash, systemctl stood in."""
import os
import unittest

from tests.test_sw1_092 import DASH, NEW_092, TmpDir, lib_for, read, run_dash, sh_quote, to_sh_path


@unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
class TestKeyboardServiceRestart(TmpDir):
    def run_sync(self, states, start=None):
        d = self.mk()
        flag = os.path.join(d, "080-dual_screen_mode")
        if start is not None:
            with open(flag, "w", newline="\n") as f:
                f.write("DEVICE_HAS_DUAL_SCREEN=%s\n" % start)
        extra = ("DUAL_FLAG=%s; KB_SERVICE=touchkeyboard.service\n"
                 'systemctl() { echo "SYSTEMCTL $*"; }' % sh_quote(to_sh_path(flag)))
        lib = lib_for(read(NEW_092), ["sync_dual_flag"], extra=extra)
        r = run_dash(lib + "\n".join("sync_dual_flag %s" % s for s in states))
        self.assertEqual(r.returncode, 0, r.stderr)
        return [l for l in r.stdout.splitlines() if l.startswith("SYSTEMCTL")]

    def test_a_dock_restarts_the_keyboard(self):
        self.assertEqual(self.run_sync(["dual"], start="false"),
                         ["SYSTEMCTL try-restart touchkeyboard.service"])

    def test_an_undock_restarts_it_too(self):
        self.assertEqual(self.run_sync(["single"], start="true"),
                         ["SYSTEMCTL try-restart touchkeyboard.service"])

    def test_no_change_no_restart(self):
        self.assertEqual(self.run_sync(["dual", "dual", "dual"], start="true"), [])

    def test_once_per_change(self):
        self.assertEqual(len(self.run_sync(["dual", "dual", "single", "single"], start="false")), 2)


if __name__ == "__main__":
    unittest.main()
