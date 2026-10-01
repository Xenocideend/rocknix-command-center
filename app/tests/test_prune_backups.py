"""testday/td-common.sh td_prune_backups: the installer keeps the newest TD_KEEP_BACKUPS timestamped backups and
removes older ones (each is a whole copy of the app, 8 MB), never touching a folder with another name. The real
function is cut out of td-common.sh and run under dash on a temporary backup folder, like test_install_keeps_user_data."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_sw1_092 as sw1  # noqa: E402

COMMON = os.path.join(HERE, "..", "testday", "td-common.sh")
INSTALL = os.path.join(HERE, "..", "testday", "td3-install.sh")


def function_text():
    with open(COMMON, encoding="utf-8") as f:
        s = f.read()
    m = re.search(r"(td_prune_backups\(\) \{\n.*?\n\}\n)", s, re.S)
    return m.group(1)


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestPruneBackups(sw1.TmpDir):
    NAMES = ["20260920-101010", "20260921-101010", "20260922-101010", "20260923-101010", "20260924-101010",
             "20260925-101010", "20260926-101010", "20260927-101010"]

    def setUp(self):
        self.root = self.mk()
        for n in self.NAMES + ["system.cfg.before-keys-restore-20260101T000000", "notes-from-owner"]:
            os.makedirs(os.path.join(self.root, n))
        with open(os.path.join(self.root, "a-file"), "w") as f:
            f.write("x")

    def run_prune(self, keep=None):
        T = sw1.to_sh_path(self.root)
        env = "TD_KEEP_BACKUPS=%s " % keep if keep is not None else ""
        fn = function_text()
        script = os.path.join(self.mk(), "prune.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("BK_ROOT=%s\n%s\ntd_prune_backups\necho rc=$?\n" % (T, fn))
        r = sw1.run_dash("%sdash %s" % (env, sw1.to_sh_path(script)), timeout=60)
        self.assertIn("rc=0", r.stdout, r.stdout + r.stderr)
        return r.stdout

    def left(self):
        return sorted(os.listdir(self.root))

    def test_keeps_the_newest_five_by_default(self):
        out = self.run_prune()
        stamped = [n for n in self.left() if re.match(r"\d{8}-\d{6}$", n)]
        self.assertEqual(stamped, self.NAMES[-5:])
        self.assertEqual(out.count("removed old backup"), 3)

    def test_the_keep_count_can_be_changed(self):
        self.run_prune(keep=2)
        self.assertEqual([n for n in self.left() if re.match(r"\d{8}-\d{6}$", n)], self.NAMES[-2:])

    def test_other_folders_and_files_are_never_touched(self):
        self.run_prune(keep=1)
        left = self.left()
        for keepme in ("system.cfg.before-keys-restore-20260101T000000", "notes-from-owner", "a-file"):
            self.assertIn(keepme, left)

    def test_fewer_backups_than_the_limit_removes_nothing(self):
        self.run_prune(keep=50)
        self.assertEqual(len([n for n in self.left() if re.match(r"\d{8}-\d{6}$", n)]), len(self.NAMES))

    def test_a_missing_backup_folder_is_not_an_error(self):
        T = sw1.to_sh_path(self.root) + "/nope"
        script = os.path.join(self.mk(), "prune.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("BK_ROOT=%s\n%s\ntd_prune_backups\necho rc=$?\n" % (T, function_text()))
        r = sw1.run_dash("dash %s" % sw1.to_sh_path(script), timeout=60)
        self.assertIn("rc=0", r.stdout, r.stdout + r.stderr)

    def test_the_installer_prunes_after_it_has_made_its_own_backup(self):
        with open(INSTALL, encoding="utf-8") as f:
            s = f.read()
        self.assertLess(s.index('echo "BACKUP=$BK'), s.index("td_prune_backups"))
        self.assertEqual(s.count("td_prune_backups"), 1)


if __name__ == "__main__":
    unittest.main()
