"""rocknix-config/migrate-old-daemon-names.sh: moves an older install's autostart files (092-dual-screen-persist,
094-rp5deck and the rest) to a backup folder, carries their logs to the new names, stops a running one by its pid
file, and leaves everything else alone. Run under dash against temporary folders (MIGRATE_AUTOSTART, MIGRATE_RUN,
MIGRATE_BACKUP), like test_sw1_092."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_sw1_092 as sw1  # noqa: E402

APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
SCRIPT = os.path.join(os.path.dirname(APP), "scripts", "migrate-old-daemon-names.sh")


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestMigrateOldDaemonNames(sw1.TmpDir):
    def setUp(self):
        self.root = self.mk()
        self.auto = os.path.join(self.root, "autostart")
        self.run_dir = os.path.join(self.root, "run")
        self.backup = os.path.join(self.root, "backup")
        for d in (self.auto, self.run_dir, self.backup):
            os.makedirs(d)

    def put(self, name, text="x"):
        with open(os.path.join(self.auto, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.auto, name), encoding="utf-8") as f:
            return f.read()

    def go(self):
        T = sw1.to_sh_path(self.root)
        cmd = ("MIGRATE_AUTOSTART=%s/autostart MIGRATE_RUN=%s/run MIGRATE_BACKUP=%s/backup dash %s; echo rc=$?"
               % (T, T, T, sw1.to_sh_path(SCRIPT)))
        r = sw1.run_dash(cmd, timeout=60)
        self.assertIn("rc=0", r.stdout, r.stdout + r.stderr)
        return r.stdout

    def backed_up(self):
        out = []
        for d in os.listdir(self.backup):
            out += os.listdir(os.path.join(self.backup, d))
        return sorted(out)

    def test_the_first_release_names_are_moved_aside(self):
        for n in ("092-dual-screen-persist", "094-rp5deck", "093-kernel-dt-guard"):
            self.put(n)
        self.go()
        self.assertEqual(self.backed_up(), ["092-dual-screen-persist", "093-kernel-dt-guard", "094-rp5deck"])
        self.assertEqual(os.listdir(self.auto), [])

    def test_the_numbered_descriptive_names_are_moved_aside_too(self):
        for n in ("092-dual-screen-layout-and-power", "094-command-center-app", "098-hibernate-swap-file"):
            self.put(n)
        self.go()
        self.assertEqual(self.backed_up(), ["092-dual-screen-layout-and-power", "094-command-center-app",
                                            "098-hibernate-swap-file"])

    def test_the_current_names_and_other_scripts_stay_put(self):
        for n in ("dual-screen-layout-and-power", "command-center-app", "010-persistent-journal", "my-own-script",
                  "092-dual-screen-persist"):
            self.put(n)
        self.go()
        self.assertEqual(self.backed_up(), ["092-dual-screen-persist"])
        self.assertEqual(sorted(os.listdir(self.auto)),
                         ["010-persistent-journal", "command-center-app", "dual-screen-layout-and-power",
                          "my-own-script"])

    def test_an_old_log_goes_to_the_new_name_and_the_newer_log_follows_it(self):
        self.put("094-rp5deck.log", "old line\n")
        self.put("command-center-app.log", "new line\n")
        self.go()
        self.assertEqual(self.read("command-center-app.log"), "old line\nnew line\n")
        self.assertFalse(os.path.exists(os.path.join(self.auto, "094-rp5deck.log")))

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.put("092-dual-screen-persist")
        self.go()
        before = self.backed_up()
        self.go()
        self.assertEqual(self.backed_up(), before)

    def test_a_running_one_is_stopped_by_its_pid_file(self):
        T = sw1.to_sh_path(self.root)
        self.put("094-rp5deck")
        cmd = ("(sleep 120 & echo $! > %s/run/094-rp5deck.pid); sleep 1; P=$(cat %s/run/094-rp5deck.pid); "
               "MIGRATE_AUTOSTART=%s/autostart MIGRATE_RUN=%s/run MIGRATE_BACKUP=%s/backup dash %s; sleep 1; "
               "if [ -d /proc/$P ]; then echo STILL-RUNNING; kill $P; else echo STOPPED; fi"
               % (T, T, T, T, T, sw1.to_sh_path(SCRIPT)))
        r = sw1.run_dash(cmd, timeout=90)
        self.assertIn("STOPPED", r.stdout, r.stdout + r.stderr)
        self.assertIn("stopped 094-rp5deck", r.stdout)

    def test_nothing_to_move_leaves_no_empty_backup_folder(self):
        self.put("my-own-script")
        out = self.go()
        self.assertEqual(os.listdir(self.backup), [])
        self.assertIn("nothing to move", out)

    def test_a_missing_autostart_folder_is_not_an_error(self):
        T = sw1.to_sh_path(self.root)
        r = sw1.run_dash("MIGRATE_AUTOSTART=%s/nope MIGRATE_RUN=%s/run MIGRATE_BACKUP=%s/backup dash %s; echo rc=$?"
                         % (T, T, T, sw1.to_sh_path(SCRIPT)), timeout=60)
        self.assertIn("rc=0", r.stdout, r.stdout + r.stderr)
        self.assertIn("nothing to migrate", r.stdout)
        self.assertEqual(os.listdir(self.backup), [])

    def test_a_stale_pid_file_is_removed_without_killing_anything(self):
        with open(os.path.join(self.run_dir, "094-rp5deck.pid"), "w") as f:
            f.write("999999")
        self.put("094-rp5deck")
        self.go()
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "094-rp5deck.pid")))


if __name__ == "__main__":
    unittest.main()
