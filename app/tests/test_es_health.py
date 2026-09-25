#!/usr/bin/env python3
"""es_health.py (CC1): is ES running, is it frozen?

The frozen rule must need SEVERAL signals (the owner: the HTTP API thread
can answer while the UI is stuck, and a false "frozen" restarts ES mid-use).
TestJudge pins the rule on hand-made evidence; TestCheck drives check()
with a fake probe built from the REAL sway capture; TestRealProcess (Linux:
WSL / the device) runs a real stand-in "ES" - a process with a live main
loop and an HTTP server on its own thread, like ES - and freezes it with
SIGSTOP, the device test plan's simulation, then SIGCONT.
"""
import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import es_health  # noqa: E402
from es_health import (FROZEN, GAME, NO_WINDOW, NOT_RUNNING, OK, STARTING, SUSPECT,  # noqa: E402
                       UNKNOWN, judge)

FIX = os.path.join(HERE, "fixtures")
LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc/self/task")


# real_tree() and its derivatives tree_without_es()/tree_with_game()/
# tree_output_off() removed: all read tests/fixtures/sway-tree-FULL-real-
# capture-2026-09-23.json, dropped from the public release (see
# DROPPED-FIXTURES-list.txt). Removed along with every test that used them
# (directly or via FakeProbe's default tree) - see TestCheck and
# TestRealProcess below.


def base_ev(**kw):
    """Evidence of a healthy, idle ES (every field a real check fills)."""
    ev = {"pid": 3389, "zombie": False, "age_s": 3600.0, "states": ["S"] * 6,
          "vol_delta": 150, "invol_delta": 3, "cpu_delta_s": 0.4, "window_s": 5.0,
          "http_fail": False, "http_note": "0.01 s", "windows": [True] * 6,
          "window_missing_s": 0.0, "visible": True, "output_on": True, "sway_ok": True,
          "game": False, "game_why": "", "powersaver": None, "events_during": 0}
    ev.update(kw)
    return ev


FROZEN_SIGSTOP = dict(http_fail=True, http_note="timeout twice (2.0 s limit)", vol_delta=0,
                      invol_delta=0, cpu_delta_s=0.0, states=["T"] * 6)


class TestJudge(unittest.TestCase):
    def test_healthy_is_ok(self):
        h = judge(base_ev())
        self.assertEqual(h.verdict, OK)
        self.assertFalse(h.offer_restart)
        self.assertEqual(h.signals, [])

    def test_sigstop_shape_is_frozen_with_three_signals(self):
        h = judge(base_ev(**FROZEN_SIGSTOP))
        self.assertEqual(h.verdict, FROZEN)
        self.assertEqual(sorted(h.signals), ["http", "stall", "state"])
        self.assertTrue(h.offer_restart)
        self.assertIn("ES isn't responding", h.summary)
        self.assertIn("HTTP timeout", h.summary)
        self.assertIn("process stopped", h.summary)

    def test_http_down_alone_is_never_frozen(self):
        """The owner's point: HTTP is one signal, not a verdict."""
        h = judge(base_ev(http_fail=True, http_note="refused twice"))
        self.assertEqual(h.verdict, SUSPECT)
        self.assertFalse(h.offer_restart)
        self.assertTrue(h.allow_force)

    def test_stall_alone_is_suspect_not_frozen(self):
        """HTTP answers while the UI thread is stuck: one signal -> SUSPECT
        (restart only as "Restart anyway")."""
        h = judge(base_ev(vol_delta=0, cpu_delta_s=0.0))
        self.assertEqual(h.verdict, SUSPECT)
        self.assertEqual(h.signals, ["stall"])
        self.assertIn("UI thread idle", h.summary)

    def test_stall_plus_http_is_frozen(self):
        h = judge(base_ev(vol_delta=0, http_fail=True, http_note="timeout twice"))
        self.assertEqual(h.verdict, FROZEN)

    def test_spinning_is_described(self):
        h = judge(base_ev(vol_delta=0, cpu_delta_s=4.9, http_fail=True, http_note="timeout"))
        self.assertEqual(h.verdict, FROZEN)
        self.assertTrue(any("spinning" in n for n in h.notes), h.notes)

    def test_any_main_thread_progress_clears_stall_and_state(self):
        h = judge(base_ev(vol_delta=4, states=["T"] * 6, http_fail=True, http_note="timeout"))
        self.assertEqual(h.verdict, SUSPECT)            # only HTTP left
        self.assertEqual(h.signals, ["http"])

    def test_an_es_event_during_the_check_vetoes(self):
        h = judge(base_ev(events_during=2, **FROZEN_SIGSTOP))
        self.assertNotEqual(h.verdict, FROZEN)
        self.assertEqual(h.verdict, OK)

    def test_powersaver_enhanced_makes_idle_legitimate(self):
        h = judge(base_ev(vol_delta=0, powersaver="enhanced", http_fail=True, http_note="x"))
        self.assertEqual(h.verdict, SUSPECT)            # HTTP only; the stall is excused
        self.assertNotIn("stall", h.signals)

    def test_invisible_window_excuses_the_stall(self):
        h = judge(base_ev(vol_delta=0, visible=False, http_fail=True, http_note="x"))
        self.assertNotIn("stall", h.signals)

    def test_game_running_is_never_judged(self):
        h = judge(base_ev(game=True, game_why="retroarch is running", windows=[False] * 6,
                          **FROZEN_SIGSTOP))
        self.assertEqual(h.verdict, GAME)
        self.assertFalse(h.offer_restart)
        self.assertFalse(h.allow_force)

    def test_screen_off_is_unknown(self):
        h = judge(base_ev(output_on=False, **FROZEN_SIGSTOP))
        self.assertEqual(h.verdict, UNKNOWN)
        self.assertFalse(h.offer_restart)

    def test_sway_unreadable_is_unknown(self):
        self.assertEqual(judge(base_ev(sway_ok=False, **FROZEN_SIGSTOP)).verdict, UNKNOWN)

    def test_young_process_is_starting(self):
        h = judge(base_ev(age_s=12.0, **FROZEN_SIGSTOP))
        self.assertEqual(h.verdict, STARTING)
        self.assertFalse(h.offer_restart)

    def test_no_process_is_not_running(self):
        h = judge({"pid": None, "absent_s": 10.2, "essway": "failed"})
        self.assertEqual(h.verdict, NOT_RUNNING)
        self.assertTrue(h.offer_restart)
        self.assertIn("essway.service is failed", h.summary)

    def test_zombie_is_not_running(self):
        self.assertEqual(judge(base_ev(zombie=True)).verdict, NOT_RUNNING)

    def test_window_missing_30s_is_no_window_with_the_owners_wording(self):
        h = judge(base_ev(windows=[False] * 30, window_missing_s=30.4))
        self.assertEqual(h.verdict, NO_WINDOW)
        self.assertTrue(h.offer_restart)
        self.assertEqual(h.summary, "ES is running but has no window (HTTP ok, window missing "
                                    "for 30 s)")

    def test_window_missing_briefly_is_only_suspect(self):
        h = judge(base_ev(windows=[False] * 6, window_missing_s=5.0))
        self.assertEqual(h.verdict, SUSPECT)
        self.assertFalse(h.offer_restart)

    def test_missing_evidence_is_never_a_signal(self):
        """Unknown (None) values must not count as broken."""
        ev = base_ev(vol_delta=None, states=[], windows=[True])
        self.assertEqual(judge(ev).verdict, OK)


class TestParsers(unittest.TestCase):
    def test_parse_stat_with_spaces_and_parens_in_comm(self):
        text = "3389 (emulation (x) st) S 3380 3389 3389 0 -1 4194560 1 0 0 0 " \
               "1234 567 0 0 20 0 12 0 9876 1 2 3"
        st = es_health.parse_stat(text)
        self.assertEqual(st, {"state": "S", "utime": 1234, "stime": 567, "starttime": 9876})
        self.assertIsNone(es_health.parse_stat("garbage"))

    def test_parse_ctxt(self):
        self.assertEqual(es_health.parse_ctxt("Name:\tx\nvoluntary_ctxt_switches:\t42\n"
                                              "nonvoluntary_ctxt_switches:\t7\n"), (42, 7))
        self.assertIsNone(es_health.parse_ctxt("Name:\tx\n"))

    def test_parse_kill_data_rocknix_values(self):
        """Values from ROCKNIX dc5f51a's set_kill calls."""
        self.assertEqual(es_health.parse_kill_data("retroarch retroarch32\n"),
                         (None, ["retroarch", "retroarch32"]))
        self.assertEqual(es_health.parse_kill_data("-9 melonDS\n"), ("-9", ["melonDS"]))
        self.assertEqual(es_health.parse_kill_data("-HUP gmu.bin"), ("-HUP", ["gmu.bin"]))
        self.assertEqual(es_health.parse_kill_data("emulationstation\n"),
                         (None, ["emulationstation"]))
        self.assertEqual(es_health.parse_kill_data(""), (None, []))

    def test_parse_powersaver(self):
        self.assertEqual(es_health.parse_powersaver(
            '<config>\n\t<string name="PowerSaverMode" value="enhanced" />\n</config>'),
            "enhanced")
        self.assertIsNone(es_health.parse_powersaver("<config/>"))

    # test_tree_facts_on_the_real_capture and test_tree_facts_game_and_off
    # removed: both depended (directly, or via tree_with_game()/
    # tree_output_off()) on tests/fixtures/sway-tree-FULL-real-capture-
    # 2026-09-23.json, dropped from the public release (see
    # DROPPED-FIXTURES-list.txt).


class TestHttpAllowList(unittest.TestCase):
    def test_only_caps_and_running_game(self):
        calls = []

        class Opener:
            def open(self, req, timeout=None):
                calls.append(req.full_url)
                raise OSError("no network in tests")

        p = es_health.Probe(opener=Opener())
        for path in ("/emukill", "/quit", "/restart", "/shutdown", "/reloadgames",
                     "/caps/../emukill", "/systems"):
            with self.assertRaises(es_health.HttpRefused, msg=path):
                p.http(path)
        self.assertEqual(calls, [])
        p.http("/caps")
        self.assertEqual(calls, ["http://127.0.0.1:1234/caps"])


# ---------------------------------------------------------------------------
# check() with a fake probe
#
# FakeProbe, Clock, run_check(), and the entire TestCheck class (13 tests)
# removed entirely: FakeProbe.__init__ defaults `tree` to real_tree(), and
# every TestCheck test either relied on that default or explicitly passed
# tree_with_game()/tree_without_es()/tree_output_off() - all derived from
# tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json, dropped from
# the public release (see DROPPED-FIXTURES-list.txt). Every test in the
# class depended on it either way, so nothing here could be salvaged without
# a new fixture, which is out of scope for this cleanup.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# A real process: live main loop + HTTP thread, then SIGSTOP / SIGCONT
#
# STAND_IN, TestRealProcess.start(), and TestRealProcess.probe() removed:
# probe() set pr.sway_tree = real_tree, which reads
# tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json, dropped from
# the public release (see DROPPED-FIXTURES-list.txt). start()/STAND_IN were
# only used to build the process that probe() then pointed at, so they
# became dead once the two tests that used them (below) were removed.
# ---------------------------------------------------------------------------


class TestProcProbeComm(unittest.TestCase):
    """The real Probe against a /proc laid out as the device has it."""

    def test_es_found_by_its_15_byte_comm(self):
        # device, 24 Sep: /proc/178254/comm = "emulationstatio" (15 bytes)
        procs = ((178254, "emulationstatio", [b"emulationstation", b"--log-path", b"/var/log"]),
                 (500, "emulationstatio", [b"/usr/bin/emulationstation-helper"]),
                 (600, "python3", [b"python3", b"main.py"]))
        with tempfile.TemporaryDirectory() as d:
            for pid, comm, argv in procs:
                os.makedirs(os.path.join(d, str(pid)))
                with open(os.path.join(d, str(pid), "comm"), "w") as f:
                    f.write(comm + "\n")
                with open(os.path.join(d, str(pid), "cmdline"), "wb") as f:
                    f.write(bytes(1).join(argv) + bytes(1))
                with open(os.path.join(d, str(pid), "stat"), "w") as f:
                    f.write("%d (%s) S 1 1 1 0 -1 0 0 0 0 0 10 5 0 0 20 0 1 0 100 0 0\n" % (pid, comm))
            p = es_health.Probe(proc=d)
            self.assertEqual(p.es_pids(), [178254])


@unittest.skipUnless(LINUX, "needs Linux /proc (WSL or the device)")
class TestRealProcess(unittest.TestCase):
    # test_live_then_sigstop_then_sigcont and
    # test_deadlocked_main_thread_with_live_http_is_suspect removed: both
    # depended on probe(), which read the missing real sway capture (see
    # comment above STAND_IN). test_real_proc_readers is unaffected - it
    # never touches the sway tree.

    def test_real_proc_readers(self):
        pr = es_health.Probe()
        st = pr.stat(os.getpid())
        self.assertIn(st["state"], ("R", "S"))
        vol, invol = pr.ctxt(os.getpid())
        self.assertGreaterEqual(vol, 0)
        self.assertIsNotNone(pr.uptime())


if __name__ == "__main__":
    unittest.main()
