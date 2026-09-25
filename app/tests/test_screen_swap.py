#!/usr/bin/env python3
"""screen_swap.py (SW1): where ES is, where the Command Center goes, and the
lockout fix - the panel follows ES as sway shows it, never the setting alone.

Trees are DERIVED from the real device capture
tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json (ES on DP-1 ws 1,
DSI-1 showing empty ws 2) by moving its real nodes around - not built from
an assumption of what sway's JSON looks like."""
import copy
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import config  # noqa: E402
import screen_swap as ss  # noqa: E402
import sway_ipc  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
LINUX = sys.platform.startswith("linux")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


# REAL, previously `REAL = load("sway-tree-FULL-real-capture-2026-09-23.json")`
# evaluated eagerly at IMPORT time, is removed: that fixture is dropped from
# the public release (see DROPPED-FIXTURES-list.txt), and importing this
# module would otherwise raise FileNotFoundError immediately, failing every
# test in this file. outputs(), swapped_tree(), game_running(),
# without_es_and_ws1(), and add_window() - all built on REAL, per this
# module's docstring ("Trees are DERIVED from the real device capture...") -
# are removed too, along with every test that used them (directly or via
# each other). No synthetic equivalent exists for this shape (the
# *-SYNTHETIC.json fixtures are shaped for test_modes.py's own specific
# cases, not a trivial swap here), so most of this file's real test value is
# gone - see the final report for exactly what is left.
UNDOCKED = load("sway-tree-UNDOCKED-SYNTHETIC.json")


class TestPlan(unittest.TestCase):
    # test_real_capture_unswapped, test_swapped_tree_puts_the_panel_on_the_
    # addon, test_lockout_old_092_ignores_the_setting,
    # test_game_running_uses_workspace_1,
    # test_nothing_observable_falls_back_to_the_setting, and
    # test_invariant_the_panel_is_never_on_the_es_screen removed: all used
    # REAL and/or its derivatives (see the module-level comment above).
    # test_undocked_is_the_pre_sw1_pair survives unchanged - it only ever
    # used UNDOCKED (SYNTHETIC, still present), {}, and None.

    def test_undocked_is_the_pre_sw1_pair(self):
        for setting in ("builtin_bottom", "addon_top"):
            p = ss.plan(UNDOCKED, setting)
            self.assertEqual(p, ss.Placement(False, "DP-1", "DSI-1", "undocked"))
            mode, why = sway_ipc.compute_mode(UNDOCKED, p.cc_output, p.es_output)
            self.assertEqual(mode, HIDDEN)
            self.assertIn("undocked", why)
        self.assertFalse(ss.plan({}, "addon_top").docked)
        self.assertFalse(ss.plan(None, "addon_top").docked)


# TestModesWhenSwapped removed entirely (test_full_bar_hidden_on_the_addon):
# its only test used swapped_tree()/add_window(), both removed above.


class TestSetting(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = os.path.join(self.d, "config.json")

    def test_read_setting_matches_the_contract(self):
        self.assertEqual(ss.read_setting(self.p), "addon_top")          # no file
        for text, want in (("{bad", "addon_top"),
                           (json.dumps({"screens": {"es_screen": "builtin_bottom"}}),
                            "builtin_bottom"),
                           (json.dumps({"output": "DP-1", "es_output": "DSI-1"}), "addon_top")):
            with open(self.p, "w") as f:
                f.write(text)
            self.assertEqual(ss.read_setting(self.p), want, text)

    def test_swapped_value(self):
        self.assertEqual(ss.swapped_value("addon_top"), "builtin_bottom")
        self.assertEqual(ss.swapped_value("builtin_bottom"), "addon_top")
        self.assertEqual(ss.swapped_value("junk"), "builtin_bottom")

    def test_fallback_placement(self):
        self.assertEqual(ss.fallback_placement("builtin_bottom")[:3], (True, "DSI-1", "DP-1"))
        self.assertEqual(ss.fallback_placement("addon_top")[:3], (True, "DP-1", "DSI-1"))


class TestWatcherLogic(unittest.TestCase):
    # test_emit_only_on_a_screen_change_not_a_source_change and
    # test_a_setting_read_error_is_the_default removed: both used REAL
    # and/or its derivatives (see the module-level comment above
    # TestPlan).

    def test_uses_the_read_only_sway_client(self):
        # the watcher may only ever send read-only message types
        with open(os.path.join(os.path.dirname(HERE), "screen_swap.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("RUN_COMMAND", src)
        self.assertIn("sway_ipc.Ipc(", src)


# _FakeIpc removed: it was only used by TestHoldWatcherThread and
# TestWatcherThread, both removed below (both drove it with REAL /
# swapped_tree(), see the module-level comment above TestPlan).


class TestHold(unittest.TestCase):
    """SW2 (24 Sep): a transient disagreement between the observed ES output
    and the configured es_screen - ES briefly re-mapping its window on the
    OTHER output right after a game exits (a known ES quirk), self-corrected
    by 092's own next poll ~3s later - must not reach the surface at all.
    See the bug report this fixes: main.on_placement rebound the panel
    surface twice in ~3s chasing exactly this blip, and the second rebind
    raced main.py's own mode recompute (its separate fix, _on_watched_mode)
    and left the panel HIDDEN and blank until restart."""

    def w(self, initial, hold_s=0.15, setting="addon_top"):
        return ss.ScreenWatcher(lambda p: None, setting_fn=lambda: setting,
                                initial=initial, hold_s=hold_s)

    # test_agreeing_observation_is_immediate,
    # test_cold_start_with_no_prior_placement_is_immediate,
    # test_disagreement_is_held_then_followed,
    # test_a_revert_before_the_hold_elapses_resets_the_timer, and
    # test_the_real_bug_sequence_never_rebinds_with_the_hold removed: all
    # used REAL and/or swapped_tree() (see the module-level comment above
    # TestPlan). test_undocked_bypasses_the_hold survives unchanged - it
    # only ever used UNDOCKED.

    def test_undocked_bypasses_the_hold(self):
        cur = ss.Placement(True, "DP-1", "DSI-1", "observed-es")
        w = self.w(cur, hold_s=5.0, setting="builtin_bottom")
        got = w.evaluate(UNDOCKED)
        self.assertFalse(got.docked)


# TestHoldWatcherThread and TestWatcherThread removed entirely
# (test_blip_then_revert_over_real_threads_never_emits and
# test_follows_es_across_a_swap_with_debounce): both seeded trees["now"]
# from REAL and switched to swapped_tree() mid-test (see the module-level
# comment above TestPlan). Their shared helper, _FakeIpc, is removed above.


if __name__ == "__main__":
    unittest.main()
