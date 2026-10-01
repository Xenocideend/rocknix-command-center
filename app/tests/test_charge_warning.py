"""The big "charger connected but not charging" warning in the real app: it comes up in every
mode, the auto-close timers never take it down, and it closes on unplug."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import hidden_overlay  # noqa: E402
import summon  # noqa: E402
from hidden_overlay import HIDDEN, OVERLAY  # noqa: E402
from sway_ipc import FULL  # noqa: E402
from test_cc5_overlay import Harness  # noqa: E402

PULL = summon.PullDownStateMachine
UNDOCKED = "undocked: DP-1 absent"


class Warning(Harness):
    def assert_warning_showing(self, app, mode):
        self.assertEqual(app.mode, mode)
        self.assertEqual(app.ui.sheet, "charge_warning")
        self.assertFalse(app.ui.cc.bar.visible)          # full screen, over the volume strip too

    def test_full_mode_opens_the_command_center_to_the_warning(self):
        app = self.make_cc5_app(mode=FULL)
        app.set_charge_warning(True)
        self.assert_warning_showing(app, FULL)
        self.assertNotEqual(app.pull.state, PULL.COMPANION)

    def test_over_a_ds_game_it_opens_the_overlay(self):
        app = self.make_cc5_app()                         # HIDDEN by an emulator's window
        app.set_charge_warning(True)
        app.post.drain()
        self.assert_warning_showing(app, OVERLAY)

    def test_undocked_it_opens_the_overlay(self):
        app = self.make_cc5_app(reason=UNDOCKED)
        self.query_result = (HIDDEN, UNDOCKED)
        app.set_charge_warning(True)
        app.post.drain()
        self.assert_warning_showing(app, OVERLAY)

    def test_the_overlay_timeout_never_takes_it_down(self):
        app = self.make_cc5_app()
        app.set_charge_warning(True)
        app.post.drain()
        self.run_timers(app, hidden_overlay.OVERLAY_TIMEOUT_S * 3)
        self.assert_warning_showing(app, OVERLAY)

    def test_the_command_center_auto_close_never_takes_it_down(self):
        app = self.make_cc5_app(mode=FULL)
        app.pull.auto_close_timeout_s = 5
        app.set_charge_warning(True)
        app.pull._deadline = app.pull._clock() - 1       # the auto-close is overdue
        app._cc_tick()
        self.assertNotEqual(app.pull.state, PULL.COMPANION)
        self.assertEqual(app.ui.sheet, "charge_warning")

    def test_closed_by_hand_it_comes_back_on_the_next_poll(self):
        app = self.make_cc5_app(mode=FULL)
        app.set_charge_warning(True)
        app.pull.close("back")
        app.ui.close()
        app.set_charge_warning(True)                      # what the next poll does
        self.assert_warning_showing(app, FULL)

    def test_unplug_closes_it_and_what_it_opened(self):
        app = self.make_cc5_app()
        app.set_charge_warning(True)
        app.post.drain()
        app.set_charge_warning(False)
        app.post.drain()
        self.assertEqual(app.mode, HIDDEN)                # the game gets its screen back
        self.assertNotEqual(app.ui.sheet, "charge_warning")
        self.assertFalse(app.charge_warning_on)


if __name__ == "__main__":
    unittest.main()
