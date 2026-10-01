"""rocknix-config/deploy_rp5deck.py: after a successful install the older staged builds (a full copy of the app each,
about 5 MB, 72 of them had piled up) are removed, keeping the newest two. stale_builds() picks them from the device's
listing, newest first, and never returns a folder that is not a staged build."""
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
PATH = os.path.join(os.path.dirname(APP), "scripts", "deploy_rp5deck.py")


def load():
    had = "rk" in sys.modules
    sys.modules.setdefault("rk", types.ModuleType("rk"))     # the real rk.py exits when there are no device credentials
    spec = importlib.util.spec_from_file_location("deploy_rp5deck_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        if not had:
            sys.modules.pop("rk", None)
    return mod


def listing(*names):
    return ["/storage/rp5deck-%s/rp5deck/MANIFEST.md5" % n for n in names]


@unittest.skipUnless(os.path.exists(PATH), "deploy_rp5deck.py is not part of this tree")
class TestStaleBuilds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import paramiko  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("paramiko is needed to import the deploy script")
        cls.m = load()

    def test_keeps_the_newest_two_with_the_current_one_counting(self):
        out = self.m.stale_builds(listing("c", "b", "a", "z"), "/storage/rp5deck-c")
        self.assertEqual(out, ["/storage/rp5deck-a", "/storage/rp5deck-z"])

    def test_the_current_build_is_kept_even_when_it_is_not_the_newest(self):
        out = self.m.stale_builds(listing("new1", "new2", "new3", "cur"), "/storage/rp5deck-cur")
        self.assertIn("/storage/rp5deck-new3", out)
        self.assertNotIn("/storage/rp5deck-cur", out)
        self.assertEqual(len(out), 2)

    def test_fewer_builds_than_the_limit_removes_none(self):
        self.assertEqual(self.m.stale_builds(listing("a", "b"), "/storage/rp5deck-a"), [])

    def test_the_keep_count_is_respected(self):
        out = self.m.stale_builds(listing("e", "d", "c", "b", "a"), "/storage/rp5deck-e", keep=4)
        self.assertEqual(out, ["/storage/rp5deck-a"])

    def test_only_staged_build_folders_can_be_returned(self):
        odd = ["/storage/rp5deck-backups/rp5deck/MANIFEST.md5", "/storage/rp5deck/rp5deck/MANIFEST.md5",
               "/storage/rp5deck-x y/rp5deck/MANIFEST.md5", "/storage/other-thing/rp5deck/MANIFEST.md5",
               "/storage/rp5deck-ok/rp5deck/MANIFEST.md5", "/storage/rp5deck-ok2/notes/MANIFEST.md5"]
        out = self.m.stale_builds(odd + listing("a", "b", "c"), "/storage/rp5deck-a", keep=1)
        for bad in ("/storage/rp5deck", "/storage/other-thing", "/storage/rp5deck-x y", "/storage/rp5deck-ok2"):
            self.assertNotIn(bad, out)
        self.assertTrue(all(self.m.STAGED.match(b) for b in out), out)

    def test_a_listing_with_repeats_does_not_count_a_build_twice(self):
        out = self.m.stale_builds(listing("c", "c", "b", "a"), "/storage/rp5deck-c")
        self.assertEqual(out, ["/storage/rp5deck-a"])
        self.assertEqual(self.m.stale_builds(listing("c", "a", "a"), "/storage/rp5deck-c", keep=1),
                         ["/storage/rp5deck-a"])                       # removed once, not twice

    def test_the_device_is_asked_newest_first_and_only_the_stale_ones_are_removed(self):
        sent = []

        def fake_sh(c, cmd, timeout=600):
            sent.append(cmd)
            if cmd.startswith("ls "):
                return 0, "\n".join(listing("cur", "mid", "old1", "old2")) + "\n"
            return 0, ""
        real = self.m.sh
        self.m.sh = fake_sh
        self.addCleanup(setattr, self.m, "sh", real)
        self.m.prune_old_builds(object(), "/storage/rp5deck-cur")
        self.assertTrue(sent[0].startswith("ls -dt "), sent[0])        # -t: newest first, never reversed
        self.assertNotIn(" -dtr", sent[0])
        self.assertEqual(sorted(c for c in sent[1:]),
                         ["rm -rf /storage/rp5deck-old1", "rm -rf /storage/rp5deck-old2"])

    def test_the_deploy_prunes_only_after_the_install_succeeded(self):
        with open(PATH, encoding="utf-8") as f:
            src = f.read()
        i = src.index("prune_old_builds(c, base)")
        self.assertGreater(i, src.index("guard-live"))
        self.assertIn("if rc == 0:", src[i - 60:i])
        self.assertEqual(src.count("prune_old_builds(c, base)"), 1)


if __name__ == "__main__":
    unittest.main()
