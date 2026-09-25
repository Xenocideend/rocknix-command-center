#!/usr/bin/env python3
"""Mode computation (FULL / BAR / HIDDEN) from sway get_tree fixtures, and
the read-only guard on the IPC client. Offline, stdlib only.

Fixtures (tests/fixtures/):
  sway-tree-FULL-real-capture-2026-09-23.json   REAL get_tree from the device:
      ES fullscreen on DP-1, DSI-1 workspace 2 empty.
  sway-tree-*-real-capture-*.json               REAL captures taken during the
      B1 device mode tests (foot --app-id rp5deck-test-bar / -other on DSI-1),
      if present.
  sway-tree-*-SYNTHETIC.json                    derived from the real FULL
      capture by adding/removing nodes; every one carries "_synthetic": true
      and a note saying exactly what was changed.
"""
import glob
import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sway_ipc  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN, compute_mode  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
LINUX = sys.platform.startswith("linux")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


class TestModeFromFixtures(unittest.TestCase):
    def check(self, name, want, reason_part=None):
        mode, reason = compute_mode(load(name))
        self.assertEqual(mode, want, "%s -> %s (%s)" % (name, mode, reason))
        if reason_part:
            self.assertIn(reason_part, reason)

    # test_full_real_capture removed: depended on
    # tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json, dropped
    # from the public release (see DROPPED-FIXTURES-list.txt).

    def test_bar_rp5deck_window(self):
        self.check("sway-tree-BAR-SYNTHETIC.json", BAR, "rp5deck-yt")

    def test_hidden_foreign_window(self):
        self.check("sway-tree-HIDDEN-foreign-SYNTHETIC.json", HIDDEN, "melonDS")

    def test_hidden_xwayland_window_without_app_id(self):
        self.check("sway-tree-HIDDEN-xwayland-SYNTHETIC.json", HIDDEN, "drastic")

    def test_hidden_when_any_window_is_foreign(self):
        self.check("sway-tree-HIDDEN-mixed-SYNTHETIC.json", HIDDEN, "Azahar")

    def test_hidden_floating_foreign_window(self):
        self.check("sway-tree-HIDDEN-floating-SYNTHETIC.json", HIDDEN, "Azahar")

    def test_window_on_top_screen_does_not_count(self):
        self.check("sway-tree-FULL-window-on-DP1-SYNTHETIC.json", FULL)

    def test_window_on_invisible_workspace_does_not_count(self):
        self.check("sway-tree-FULL-foreign-on-hidden-workspace-SYNTHETIC.json", FULL)

    def test_undocked_is_hidden(self):
        self.check("sway-tree-UNDOCKED-SYNTHETIC.json", HIDDEN, "undocked")

    # test_real_device_captures removed: it globbed
    # tests/fixtures/sway-tree-*-real-capture-*.json (the B1 device-mode-test
    # captures) and asserted at least one was found. All of those files
    # (sway-tree-BAR-real-capture-2026-09-23.json,
    # sway-tree-FULL-real-capture-after-other-2026-09-23.json,
    # sway-tree-HIDDEN-real-capture-2026-09-23.json,
    # sway-tree-FULL-real-capture-2026-09-23.json) are dropped from the
    # public release (see DROPPED-FIXTURES-list.txt), so the glob would now
    # always return zero and the test would always fail.

    def test_synthetic_fixtures_are_marked(self):
        for path in glob.glob(os.path.join(FIX, "sway-tree-*SYNTHETIC*.json")):
            with self.subTest(os.path.basename(path)):
                self.assertTrue(load(os.path.basename(path)).get("_synthetic"))


# TestModeEdgeCases removed entirely (test_rp5deck_test_other_hides,
# test_rp5deck_test_bar_hides, test_allow_list_rp5deck_web,
# test_allow_list_rp5deck_yt, test_prefix_not_enough,
# test_no_internal_output, test_split_container_is_not_a_window): every
# method used _with_view(), or loaded the fixture directly, both of which
# read tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json, dropped
# from the public release (see DROPPED-FIXTURES-list.txt). The available
# *-SYNTHETIC.json fixtures are shaped for their own specific FULL/HIDDEN
# cases (extra windows already added for a different purpose), not a
# trivial same-shape swap for this class's plain baseline tree, so nothing
# here could be rewired without inventing new fixture content.


class TestIpcIsReadOnly(unittest.TestCase):
    def test_run_command_refused(self):
        ipc = sway_ipc.Ipc.__new__(sway_ipc.Ipc)   # no socket needed: refused before sending

        class NoSock:
            def sendall(self, data):
                raise AssertionError("a command reached the socket")
        ipc.sock = NoSock()
        with self.assertRaises(sway_ipc.CommandRefused):
            ipc.send(sway_ipc.RUN_COMMAND, b"output DSI-1 disable")

    def test_read_only_set(self):
        self.assertNotIn(sway_ipc.RUN_COMMAND, sway_ipc.READ_ONLY_TYPES)
        self.assertIn(sway_ipc.GET_TREE, sway_ipc.READ_ONLY_TYPES)

    def test_every_non_read_only_message_type_is_refused(self):
        """FX-D item 4: not just RUN_COMMAND - EVERY i3/sway IPC message type
        outside READ_ONLY_TYPES must be refused before it ever reaches the
        socket. Covers the rest of the real i3-ipc protocol's command-ish
        types (GET_MARKS=5, GET_BAR_CONFIG=6, GET_BINDING_MODES=8,
        GET_CONFIG=9, SEND_TICK=10, SYNC=11, GET_BINDING_STATE=12) plus an
        arbitrary unknown type, so a future protocol addition or typo is
        refused by default rather than silently allowed through."""
        ipc = sway_ipc.Ipc.__new__(sway_ipc.Ipc)

        class NoSock:
            def sendall(self, data):
                raise AssertionError("a non-read-only message reached the socket")
        ipc.sock = NoSock()
        non_read_only = (sway_ipc.RUN_COMMAND, 5, 6, 8, 9, 10, 11, 12, 999)
        for mtype in non_read_only:
            self.assertNotIn(mtype, sway_ipc.READ_ONLY_TYPES)
            with self.subTest(mtype=mtype):
                with self.assertRaises(sway_ipc.CommandRefused):
                    ipc.send(mtype, b"")

    def test_read_only_types_are_still_accepted(self):
        # The flip side of the refusal test: every type rp5deck actually
        # needs must NOT raise (this would have caught an over-broad guard).
        class RecordingSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)
        ipc = sway_ipc.Ipc.__new__(sway_ipc.Ipc)
        ipc.sock = RecordingSock()
        for mtype in sway_ipc.READ_ONLY_TYPES:
            ipc.send(mtype, b"")
        self.assertEqual(len(ipc.sock.sent), len(sway_ipc.READ_ONLY_TYPES))


# _FakeModeWatcherIpc and TestModeWatcherBurst removed entirely
# (test_forced_recheck_during_a_sustained_event_flood,
# test_max_delay_zero_gap_between_events_still_bounds_the_wait): both tests
# seeded trees["now"] from
# tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json, dropped from
# the public release (see DROPPED-FIXTURES-list.txt), before switching to
# the still-present sway-tree-HIDDEN-foreign-SYNTHETIC.json mid-test. The
# available *-SYNTHETIC.json fixtures are shaped for other specific cases,
# not a trivial same-shape swap for the plain FULL baseline this class
# needs, so nothing here could be rewired without inventing new fixture
# content. _make_watcher() had no remaining caller once both tests were gone.


if __name__ == "__main__":
    unittest.main()
