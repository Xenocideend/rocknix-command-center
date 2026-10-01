"""testday/td-common.sh td_write_device_profile: the installer writes the layout daemon's device-profile.env from the
installed app's own profile, and never fails or clobbers the old file when the profile cannot be read. The real function
is cut out of td-common.sh and run under dash, like test_prune_backups."""
import os
import re
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_sw1_092 as sw1  # noqa: E402

APP = os.path.dirname(HERE)
COMMON = os.path.join(APP, "testday", "td-common.sh")
INSTALL = os.path.join(APP, "testday", "td3-install.sh")


def function_text():
    with open(COMMON, encoding="utf-8") as f:
        s = f.read()
    return re.search(r"(td_write_device_profile\(\) \{\n.*?\n\}\n)", s, re.S).group(1)


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestWriteDeviceProfile(sw1.TmpDir):
    def setUp(self):
        self.home = self.mk()
        self.out = os.path.join(self.home, "device-profile.env")

    def put_app(self, screen_map_text=None):
        shutil.copy(os.path.join(APP, "device.py"), self.home)
        if screen_map_text is None:
            shutil.copy(os.path.join(APP, "screen_map.py"), self.home)
        else:
            with open(os.path.join(self.home, "screen_map.py"), "w", newline="\n") as f:
                f.write(screen_map_text)

    def run_fn(self):
        T = sw1.to_sh_path(self.home)
        script = "HOME_DIR=%s\n%s\ntd_write_device_profile\necho rc=$?\n" % (sw1.sh_quote(T), function_text())
        r = sw1.run_dash("RP5DECK_DEVICE='Retroid Pocket 5' RP5DECK_SCREENS= && export RP5DECK_DEVICE RP5DECK_SCREENS; " + script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rc=0", r.stdout)
        return r.stdout

    def read_out(self):
        with open(self.out, encoding="utf-8") as f:
            return f.read()

    def test_it_writes_the_apps_profile_for_this_model(self):
        self.put_app()
        out = self.run_fn()
        self.assertEqual(self.read_out(), "INTERNAL='DSI-1'\nEXTERNAL='DP-1'\nTOUCH_EXT='8746:1:RetroidPocket_RDS_Touchscreen'\n"
                                          "TOUCH_INT='0:0:generic_ft5x06_(a0)'\nAUDIO_CARD_NAME='Built-in Audio'\n")
        self.assertIn("device profile: INTERNAL='DSI-1'", out)
        self.assertFalse(os.path.exists(self.out + ".new"))

    def test_without_the_app_files_nothing_is_written_and_the_install_goes_on(self):
        out = self.run_fn()
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("not written", out)

    def test_a_profile_that_prints_nothing_keeps_the_old_file(self):
        with open(self.out, "w", newline="\n") as f:
            f.write("INTERNAL='DSI-7'\n")
        self.put_app("print('')\n")
        out = self.run_fn()
        self.assertEqual(self.read_out(), "INTERNAL='DSI-7'\n")
        self.assertIn("not written", out)
        self.assertFalse(os.path.exists(self.out + ".new"))

    def test_a_profile_that_crashes_keeps_the_old_file(self):
        with open(self.out, "w", newline="\n") as f:
            f.write("INTERNAL='DSI-7'\n")
        self.put_app("raise SystemExit(3)\n")
        self.run_fn()
        self.assertEqual(self.read_out(), "INTERNAL='DSI-7'\n")

    def test_a_new_profile_replaces_the_old_file(self):
        with open(self.out, "w", newline="\n") as f:
            f.write("INTERNAL='DSI-7'\n")
        self.put_app()
        self.run_fn()
        self.assertTrue(self.read_out().startswith("INTERNAL='DSI-1'"))


class TestTheInstallerCallsIt(unittest.TestCase):
    def test_the_install_step_writes_the_profile_before_it_starts_the_app(self):
        with open(INSTALL, encoding="utf-8") as f:
            s = f.read()
        a = s.index("td_write_device_profile")
        b = s.index('"$AUTOSTART/command-center-app"            # self-backgrounds')
        self.assertLess(a, b)


if __name__ == "__main__":
    unittest.main()
