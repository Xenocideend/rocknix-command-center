"""The add-on replug notice in dual-screen-layout-and-power: addon_replug_wanted, verbatim, under dash."""
import os
import unittest

from tests.test_sw1_092 import DASH, NEW_092, TmpDir, lib_for, read, run_dash, sh_quote, to_sh_path


@unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
class TestAddonReplugWanted(TmpDir):
    def ask(self, state="single", seen="yes", wake=4, resets=0, partner=True, top_off=False):
        d = self.mk()
        os.makedirs(os.path.join(d, "port0"))
        if partner:
            os.makedirs(os.path.join(d, "port0-partner"))
        if top_off:
            open(os.path.join(d, "top-screen-off"), "w").close()
        lib = lib_for(read(NEW_092), ["addon_replug_wanted", "top_off"],
                      extra="PORT=%s; TOPOFF=%s; WAKE_MAX=4; DP_RACE_MAX=2"
                      % (sh_quote(to_sh_path(os.path.join(d, "port0"))),
                         sh_quote(to_sh_path(os.path.join(d, "top-screen-off")))))
        body = ('state=%s; addon_seen=%s; wake_tries=%d; dp_race_resets=%d\n'
                'addon_replug_wanted && echo yes || echo no' % (state, seen, wake, resets))
        r = run_dash(lib + body)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_the_add_on_with_every_wake_spent(self):
        self.assertEqual(self.ask(), "yes")

    def test_every_dp_reset_spent(self):
        self.assertEqual(self.ask(wake=0, resets=2), "yes")

    def test_nudges_left(self):
        self.assertEqual(self.ask(wake=3, resets=1), "no")

    def test_never_for_something_that_was_never_the_add_on(self):
        self.assertEqual(self.ask(seen="no"), "no")

    def test_not_while_the_screen_is_up(self):
        self.assertEqual(self.ask(state="dual"), "no")

    def test_not_once_unplugged(self):
        self.assertEqual(self.ask(partner=False), "no")

    def test_not_with_the_top_screen_off_on_purpose(self):
        self.assertEqual(self.ask(top_off=True), "no")


if __name__ == "__main__":
    unittest.main()
