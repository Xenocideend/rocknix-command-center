"""rocknix-config/install-layout-daemon.sh, run verbatim under dash: it installs the layout daemon the device's profile calls
for (the add-on one on a Retroid Pocket 5, the built-in-panels one on the others), never leaves both installed, backs up what
it replaces and writes the profile file when it is missing."""
import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_sw1_092 as sw1  # noqa: E402

APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
SCRIPT = os.path.join(os.path.dirname(APP), "scripts", "install-layout-daemon.sh")
ADDON, BUILTIN = "dual-screen-layout-and-power", "dual-screen-builtin-layout"


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestInstall(sw1.TmpDir):
    def setUp(self):
        t = self.mk()
        self.src = os.path.join(t, "scripts")
        self.auto = os.path.join(t, "autostart")
        self.bk = os.path.join(t, "backups")
        self.app = os.path.join(t, "rp5deck")
        for d in (self.src, self.auto, self.bk, self.app):
            os.makedirs(d)
        shutil.copy(SCRIPT, os.path.join(self.src, "install-layout-daemon.sh"))
        for name in (ADDON, BUILTIN):
            self.put(os.path.join(self.src, name), "#!/bin/sh\n# the %s daemon\necho ran >> \"$STUB_MARK\"\n" % name)
        self.mark = os.path.join(t, "ran")
        for f in ("screen_map.py", "device.py"):
            shutil.copy(os.path.join(APP, f), self.app)

    def put(self, path, text):
        with open(path, "w", newline="\n") as f:
            f.write(text)

    def run_install(self, device, *args):
        env = ("RP5DECK_DEVICE=%s INSTALL_AUTOSTART=%s INSTALL_BACKUP_DIR=%s RP5DECK_HOME=%s STUB_MARK=%s RP5DECK_SCREENS= "
               % tuple(sw1.sh_quote(sw1.to_sh_path(x) if os.path.isabs(x) else x)
                       for x in (device, self.auto, self.bk, self.app, self.mark)))
        cmd = "export %s; sh %s %s" % (env, sw1.sh_quote(sw1.to_sh_path(os.path.join(self.src, "install-layout-daemon.sh"))),
                                       " ".join(args))
        return sw1.run_dash(cmd)

    def installed(self):
        return sorted(os.listdir(self.auto))

    def test_a_retroid_pocket_5_gets_the_add_on_daemon_only(self):
        r = self.run_install("Retroid Pocket 5", "--no-start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.installed(), [ADDON])
        self.assertIn("the addon daemon", r.stdout)

    def test_a_built_in_dual_screen_handheld_gets_the_built_in_daemon_only(self):
        r = self.run_install("AYN Thor", "--no-start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.installed(), [BUILTIN])
        self.assertIn("give lowerdeck back with", r.stdout)

    def test_no_start_does_not_run_the_daemon(self):
        self.run_install("AYN Thor", "--no-start")
        self.assertFalse(os.path.exists(self.mark))

    def test_the_installed_file_is_the_real_one_and_executable(self):
        self.run_install("AYN Thor", "--no-start")
        self.assertEqual(slurp(os.path.join(self.auto, BUILTIN)), slurp(os.path.join(self.src, BUILTIN)))
        if not sys.platform.startswith("win"):
            self.assertTrue(os.access(os.path.join(self.auto, BUILTIN), os.X_OK))

    def test_the_other_kind_is_moved_aside_not_left_installed(self):
        self.put(os.path.join(self.auto, ADDON), "old add-on daemon\n")
        r = self.run_install("AYN Thor", "--no-start")
        self.assertEqual(self.installed(), [BUILTIN])
        moved = [n for n in os.listdir(self.bk) if n.startswith(ADDON + ".disabled-")]
        self.assertEqual(len(moved), 1)
        self.assertEqual(slurp(os.path.join(self.bk, moved[0])), "old add-on daemon\n")
        self.assertIn("moved the %s daemon aside" % ADDON, r.stdout)

    def test_the_other_kind_is_moved_aside_the_other_way_round_too(self):
        self.put(os.path.join(self.auto, BUILTIN), "old builtin daemon\n")
        self.run_install("Retroid Pocket 5", "--no-start")
        self.assertEqual(self.installed(), [ADDON])
        self.assertEqual(len([n for n in os.listdir(self.bk) if n.startswith(BUILTIN + ".disabled-")]), 1)

    def test_an_existing_copy_is_backed_up_before_it_is_replaced(self):
        self.put(os.path.join(self.auto, ADDON), "previous version\n")
        self.run_install("Retroid Pocket 5", "--no-start")
        baks = [n for n in os.listdir(self.bk) if n.startswith(ADDON + ".bak-")]
        self.assertEqual(len(baks), 1)
        self.assertEqual(slurp(os.path.join(self.bk, baks[0])), "previous version\n")
        self.assertEqual(slurp(os.path.join(self.auto, ADDON)), slurp(os.path.join(self.src, ADDON)))

    def test_the_profile_file_is_written_when_it_is_missing(self):
        self.run_install("AYN Thor", "--no-start")
        text = slurp(os.path.join(self.app, "device-profile.env"))
        self.assertIn("BOTTOM_OUTPUT='DSI-1'", text)
        self.assertIn("TOUCH_BOTTOM='0:0:bottom_touchscreen'", text)

    def test_an_existing_profile_file_is_kept(self):
        self.put(os.path.join(self.app, "device-profile.env"), "BOTTOM_OUTPUT='DSI-7'\n")
        self.run_install("AYN Thor", "--no-start")
        self.assertEqual(slurp(os.path.join(self.app, "device-profile.env")), "BOTTOM_OUTPUT='DSI-7'\n")

    def test_without_the_app_nothing_is_installed(self):
        os.remove(os.path.join(self.app, "screen_map.py"))
        r = self.run_install("AYN Thor", "--no-start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("install the Command Center first", r.stdout)
        self.assertEqual(self.installed(), [])

    def test_a_missing_daemon_file_installs_nothing_and_says_so(self):
        os.remove(os.path.join(self.src, BUILTIN))
        r = self.run_install("AYN Thor", "--no-start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is missing", r.stdout)
        self.assertEqual(self.installed(), [])

    def test_kind_prints_the_kind_and_installs_nothing(self):
        self.assertEqual(self.run_install("AYN Thor", "--kind").stdout.strip(), "builtin")
        self.assertEqual(self.run_install("Retroid Pocket 5", "--kind").stdout.strip(), "addon")
        self.assertEqual(self.installed(), [])

    def test_a_wrong_argument_prints_the_usage(self):
        r = self.run_install("AYN Thor", "--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stdout)

    def test_the_script_parses(self):
        r = sw1.run_dash("dash -n %s" % sw1.sh_quote(sw1.to_sh_path(SCRIPT)))
        self.assertEqual((r.returncode, r.stderr), (0, ""))


class TestStatic(unittest.TestCase):
    def test_a_daemon_is_only_stopped_by_its_own_pid_file_and_while_it_is_still_a_layout_daemon(self):
        s = slurp(SCRIPT)
        self.assertIn("*dual-screen-*) kill", s)
        self.assertEqual(s.count("kill "), 1)
        self.assertNotIn("pkill", s)
        self.assertNotIn("killall", s)

    def test_the_other_daemon_is_stopped_before_it_is_moved_and_the_new_one_before_it_starts(self):
        s = slurp(SCRIPT)
        self.assertLess(s.index('stop_daemon "$otherlock"'), s.index('mv "$AUTOSTART/$other"'))
        self.assertLess(s.index('stop_daemon "$lock"'), s.index('    "$AUTOSTART/$want"\n    sleep 6'))


if __name__ == "__main__":
    unittest.main()
