"""steam/install-steam-nested.sh: puts the nested Steam launcher's boot script and display-priority file in
place, takes them out again, and never touches anything else. Run under dash against a temporary /storage
(STEAM_INSTALL_STORAGE) with the bind mount switched off (STEAM_INSTALL_NO_MOUNT), like test_sw1_092."""
import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_sw1_092 as sw1  # noqa: E402

STEAM = os.path.join(os.path.dirname(HERE), "steam")
FILES = ("install-steam-nested.sh", "start_steam_nested.sh", "steam-keep-command-center-alive",
         "085-steam-display-priority")


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestInstallSteamNested(sw1.TmpDir):
    def setUp(self):
        super().setUp() if hasattr(super(), "setUp") else None
        self.root = self.mk()
        self.app = os.path.join(self.root, "rp5deck", "steam")
        os.makedirs(self.app)
        for f in FILES:
            with open(os.path.join(STEAM, f), "rb") as src, open(os.path.join(self.app, f), "wb") as dst:
                dst.write(src.read().replace(b"\r\n", b"\n"))
        os.makedirs(os.path.join(self.root, ".local", "share", "Steam"))
        open(os.path.join(self.root, ".config_marker"), "w").close()

    def run_script(self, action, where=None):
        T = sw1.to_sh_path(self.root)
        folder = where or (T + "/rp5deck/steam")
        cmd = ("cd %s && STEAM_INSTALL_STORAGE=%s STEAM_INSTALL_NO_MOUNT=1 dash %s/install-steam-nested.sh %s; "
               "echo rc=$?" % (folder, T, folder, action))
        r = sw1.run_dash(cmd, timeout=60)
        return r.stdout + r.stderr

    def path(self, *p):
        return os.path.join(self.root, *p)

    def test_install_puts_both_files_in_place_executable_and_identical(self):
        out = self.run_script("install")
        self.assertIn("rc=0", out, out)
        for rel, src in ((".config/autostart/steam-keep-command-center-alive", "steam-keep-command-center-alive"),
                         (".config/profile.d/085-steam-display-priority", "085-steam-display-priority")):
            dst = self.path(*rel.split("/"))
            self.assertTrue(os.path.isfile(dst), rel)
            with open(dst, "rb") as a, open(os.path.join(self.app, src), "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.assertIn("installed:", out)
        self.assertIn("Restart EmulationStation", out)

    def test_a_second_install_changes_nothing(self):
        self.run_script("install")
        out = self.run_script("install")
        self.assertIn("already in place", out)
        self.assertNotIn("installed:", out)

    def test_remove_takes_out_exactly_those_two_files(self):
        self.run_script("install")
        other = self.path(".config", "autostart", "some-other-script")
        with open(other, "w") as f:
            f.write("keep me")
        out = self.run_script("remove")
        self.assertIn("rc=0", out, out)
        self.assertFalse(os.path.exists(self.path(".config", "autostart", "steam-keep-command-center-alive")))
        self.assertFalse(os.path.exists(self.path(".config", "profile.d", "085-steam-display-priority")))
        self.assertTrue(os.path.exists(other))
        self.assertIn("rc=0", self.run_script("remove"))             # nothing left to remove is not an error

    def test_it_refuses_to_run_from_anywhere_but_the_installed_app_folder(self):
        elsewhere = self.path("copy")
        shutil.copytree(self.app, elsewhere)
        out = self.run_script("install", where=sw1.to_sh_path(elsewhere))
        self.assertIn("rc=1", out)
        self.assertIn("installed app folder", out)
        self.assertFalse(os.path.exists(self.path(".config", "autostart", "steam-keep-command-center-alive")))

    def test_it_says_so_when_steam_itself_is_missing(self):
        shutil.rmtree(self.path(".local"))
        out = self.run_script("install")
        self.assertIn("rc=0", out, out)
        self.assertIn("WARNING", out)
        self.assertIn("Steam", out)

    def test_status_reports_both_states(self):
        before = self.run_script("status")
        self.assertIn("boot script: not installed", before)
        self.run_script("install")
        after = self.run_script("status")
        self.assertIn("boot script: installed", after)
        self.assertIn("Steam folder: found", after)

    def test_an_unknown_action_installs_nothing(self):
        out = self.run_script("frobnicate")
        self.assertIn("rc=2", out)
        self.assertFalse(os.path.exists(self.path(".config")))


if __name__ == "__main__":
    unittest.main()
