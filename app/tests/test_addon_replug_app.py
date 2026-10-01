"""The app side of the add-on replug notice: main.App reads dual-screen-layout-and-power's flag file."""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import main  # noqa: E402
from test_charge_stuck_app import Case  # noqa: E402


class ReplugFlag(Case):
    def setUp(self):
        super().setUp()
        d = tempfile.mkdtemp()
        self.flag = os.path.join(d, "rp5deck-addon-replug")
        p = mock.patch.object(main, "ADDON_REPLUG_FLAG", self.flag)
        p.start()
        self.addCleanup(p.stop)
        self.app = self.build()

    def test_the_flag_shows_the_banner_and_its_removal_clears_it(self):
        self.app._replug_tick()
        self.assertFalse(self.app.ui.home.banner_label.visible)
        open(self.flag, "w").write("id_header=0x00000000 wake_tries=4 dp_race_resets=0\n")
        self.app._replug_tick()
        self.assertEqual(self.app.ui.home.banner_label.text, main.ADDON_REPLUG_TEXT)
        os.remove(self.flag)
        self.app._replug_tick()
        self.assertFalse(self.app.ui.home.banner_label.visible)


if __name__ == "__main__":
    unittest.main()
