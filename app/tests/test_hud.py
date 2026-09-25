#!/usr/bin/env python3
"""hud.py follow-ups: fdinfo de-duplication by drm-client-id and the
stateful GPU-load delta. Offline; the fdinfo texts are copied from a real
device read (sway had three fds on client 6, each reporting the same total)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hud  # noqa: E402

# Real /proc/3281/fdinfo/12 (sway) from the device, 2026-09-23.
SWAY_FD = """pos:\t0
flags:\t02100002
mnt_id:\t26
ino:\t1234
drm-driver:\tmsm
drm-client-id:\t6
drm-engine-gpu:\t26006942606 ns
drm-cycles-gpu:\t7947908204
drm-maxfreq-gpu:\t925000000 Hz
drm-total-memory:\t116376 KiB
"""
SEATD_FD = """drm-driver:\tmsm
drm-client-id:\t5
drm-engine-gpu:\t0 ns
"""


class TestFdinfo(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(hud.parse_drm_fdinfo(SWAY_FD), ("msm", "6", 26006942606))
        self.assertEqual(hud.parse_drm_fdinfo("pos:\t0\n"), (None, None, None))

    def test_dedupe_by_client_id(self):
        recs = [hud.parse_drm_fdinfo(t) for t in (SWAY_FD, SWAY_FD, SWAY_FD, SEATD_FD, SEATD_FD)]
        self.assertEqual(hud.dedupe_gpu_totals(recs), {"6": 26006942606, "5": 0})

    def test_other_drivers_ignored_and_nothing_is_none(self):
        self.assertIsNone(hud.dedupe_gpu_totals([("i915", "1", 5)]))
        self.assertIsNone(hud.dedupe_gpu_totals([]))


class TestGpuLoad(unittest.TestCase):
    def test_first_call_has_no_baseline(self):
        g = hud.GpuLoadSampler(reader=lambda: {"6": 0}, clock=lambda: 0.0)
        self.assertIsNone(g.sample())

    def test_delta_over_interval(self):
        g = hud.GpuLoadSampler()
        self.assertIsNone(g.update(10.0, {"6": 1_000_000_000, "5": 0}))
        # 0.25 s busy on client 6 over 1.0 s -> 25 %
        self.assertEqual(g.update(11.0, {"6": 1_250_000_000, "5": 0}), 25.0)
        # two clients, 0.1 s + 0.2 s over 0.5 s -> 60 %
        self.assertEqual(g.update(11.5, {"6": 1_350_000_000, "5": 200_000_000}), 60.0)

    def test_duplicate_fds_do_not_inflate_load(self):
        # The same 0.25 s delta, seen through three fds of one client, must
        # still read 25 %, not 75 %.
        g = hud.GpuLoadSampler()
        before = hud.dedupe_gpu_totals([("msm", "6", 1_000_000_000)] * 3)
        after = hud.dedupe_gpu_totals([("msm", "6", 1_250_000_000)] * 3)
        g.update(0.0, before)
        self.assertEqual(g.update(1.0, after), 25.0)

    def test_unreadable_is_none_not_zero_and_resets(self):
        g = hud.GpuLoadSampler()
        g.update(0.0, {"6": 0})
        self.assertIsNone(g.update(1.0, None))
        self.assertIsNone(g.update(2.0, {"6": 5}))     # baseline was reset

    def test_new_client_skipped_and_clamped(self):
        g = hud.GpuLoadSampler()
        g.update(0.0, {"6": 0})
        self.assertEqual(g.update(1.0, {"6": 0, "9": 50_000_000_000}), 0.0)
        self.assertEqual(g.update(2.0, {"6": 3_000_000_000, "9": 50_000_000_000}), 100.0)


class TestStorageUnits(unittest.TestCase):
    """read_storage_stats() must report binary GiB, matching `df -h`.

    Real `df -k /storage` from the device, 2026-09-23 (df -h: 947.8G / 393.6G).
    The old divisor 1_024_000 gave 970.5 / 403.0 - neither GB nor GiB.
    """
    DF = ("Filesystem           1K-blocks      Used Available Use% Mounted on\n"
          "/dev/mmcblk0p2       993789028 581054416 412718228  58% /storage\n")

    def test_read_storage_stats_reports_gib(self):
        from unittest import mock
        with mock.patch.object(hud.subprocess, "check_output", return_value=self.DF):
            r = hud.read_storage_stats()
        self.assertAlmostEqual(r["storage_total_gb"], 947.75, places=1)
        self.assertAlmostEqual(r["storage_free_gb"], 393.60, places=1)

    def test_df_failure_gives_none_not_zero(self):
        from unittest import mock
        with mock.patch.object(hud.subprocess, "check_output", side_effect=OSError("no df")):
            r = hud.read_storage_stats()
        self.assertIsNone(r["storage_total_gb"])
        self.assertIsNone(r["storage_free_gb"])


if __name__ == "__main__":
    unittest.main()
