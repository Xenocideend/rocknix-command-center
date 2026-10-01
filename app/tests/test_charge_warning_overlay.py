#!/usr/bin/env python3
"""Regression: the charge warning must not crash the game-screen overlay (cc_overlay).

The overlay is a separate process (cc_overlay.OverlayApp). Its build_ui() does not build
self.cc5 (the hidden_overlay.OverlayController the main panel uses to open the Command
Center over an emulator's second screen), and it never runs the main panel's build_ui()
line that adds the charge_warning sheet. When the charger is stuck, main.App's
_charge_stuck_tick() calls set_charge_warning(True) every poll; in HIDDEN mode that path
reached self.cc5.open(...) and raised AttributeError: 'NoneType' object has no attribute
'open', crash-looping the whole overlay (seen 2026-09-27 under RP5DECK_DEMO_NOTICE=charge).

The fix: set_charge_warning() falls back to self.pull.open() when self.cc5 is None (the
overlay's own pull-down is how it opens the Command Center), and the overlay adds the
charge_warning sheet so the warning is actually visible on the game screen. Both are
covered here, on the real OverlayApp (headless harness, like the rest of sw1).
"""
import argparse
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cc_overlay  # noqa: E402
import summon  # noqa: E402
from screen_swap import Placement  # noqa: E402
from sway_ipc import HIDDEN  # noqa: E402
from sw1_patched_cases import OverlayCase  # noqa: E402

PULL = summon.PullDownStateMachine
NORMAL = Placement(True, "DP-1", "DSI-1", "observed-es")
UNDOCKED = Placement(False, "DP-1", "DSI-1", "undocked")


class TestChargeWarningOverlay(OverlayCase):
    """The overlay's own open/closed state is self.pull, not self.mode (the overlay uses
    TAB/FULL/HIDDEN for its surface geometry, the pull-down for the Command Center)."""

    def assert_warning_up(self, app):
        self.assertTrue(app.charge_warning_on)
        self.assertEqual(app.ui.sheet, "charge_warning")
        self.assertTrue(app.pull.is_open())

    def test_undocked_charger_stuck_does_not_crash(self):
        app = self.make_overlay(placement=UNDOCKED)
        self.assertEqual(app.mode, HIDDEN)
        self.assertFalse(app.pull.is_open())
        self.assertIsNone(app.cc5)
        # before the fix this raised AttributeError: 'NoneType' ... 'open'
        app.set_charge_warning(True)
        app.post.drain()
        self.assert_warning_up(app)

    def test_tab_closed_charger_stuck_does_not_crash(self):
        app = self.make_overlay(placement=NORMAL)
        self.assertEqual(app.mode, cc_overlay.TAB)
        self.assertFalse(app.pull.is_open())
        self.assertIsNone(app.cc5)
        app.set_charge_warning(True)
        app.post.drain()
        self.assert_warning_up(app)

    def test_closed_by_hand_it_comes_back_on_the_next_poll(self):
        app = self.make_overlay(placement=UNDOCKED)
        app.set_charge_warning(True)
        app.post.drain()
        self.assert_warning_up(app)
        # the user closes the Command Center (swipe up / back)
        app.pull.close("back")
        app.ui.close()
        app.post.drain()
        # the next poll re-raises it
        app.set_charge_warning(True)
        app.post.drain()
        self.assert_warning_up(app)

    def test_unplug_takes_it_down(self):
        app = self.make_overlay(placement=UNDOCKED)
        app.set_charge_warning(True)
        app.post.drain()
        app.set_charge_warning(False)
        app.post.drain()
        self.assertFalse(app.charge_warning_on)
        self.assertEqual(app.ui.sheet, None)
        self.assertFalse(app.pull.is_open())


if __name__ == "__main__":
    unittest.main()
