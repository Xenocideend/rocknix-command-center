#!/usr/bin/env python3
"""charge_limit.py - Safe Charge's device I/O, mirroring rocknix-config/
095-charge-limit's own file/sysfs contract (read only, never edited here -
see patches/YT4-NOTES.md). Every path is a tempfile, never real sysfs."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import charge_limit  # noqa: E402


class TempPaths(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-charge-")
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.conf = os.path.join(self.d, "charge-limit")
        self.end = os.path.join(self.d, "end_threshold")
        self.start = os.path.join(self.d, "start_threshold")

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()


class TestIsPct(unittest.TestCase):
    def test_valid(self):
        for s in ("0", "50", "85", "100"):
            self.assertTrue(charge_limit.is_pct(s), s)

    def test_invalid(self):
        for s in ("", "-5", "101", "1.5", "abc", "85 ", None, 85):
            self.assertFalse(charge_limit.is_pct(s), s)


class TestReadSaved(TempPaths):
    def test_missing_file_is_the_default(self):
        self.assertEqual(charge_limit.read_saved(os.path.join(self.d, "nope")),
                         (charge_limit.DEFAULT_END, None))

    def test_bare_end_only(self):
        self.write(self.conf, "70\n")
        self.assertEqual(charge_limit.read_saved(self.conf), (70, None))

    def test_end_and_start(self):
        self.write(self.conf, "85 80\n")
        self.assertEqual(charge_limit.read_saved(self.conf), (85, 80))

    def test_off_is_bare_100(self):
        self.write(self.conf, "100")
        self.assertEqual(charge_limit.read_saved(self.conf), (100, None))

    def test_garbage_first_field_falls_back_to_default(self):
        # each field is validated independently (095's own is_pct() per
        # token) - a garbage END does not also discard a valid START
        self.write(self.conf, "not-a-number 80\n")
        self.assertEqual(charge_limit.read_saved(self.conf), (charge_limit.DEFAULT_END, 80))

    def test_garbage_second_field_is_just_dropped(self):
        self.write(self.conf, "85 not-a-number\n")
        self.assertEqual(charge_limit.read_saved(self.conf), (85, None))

    def test_out_of_range_field_is_refused(self):
        self.write(self.conf, "150\n")
        self.assertEqual(charge_limit.read_saved(self.conf), (charge_limit.DEFAULT_END, None))

    def test_empty_file(self):
        self.write(self.conf, "")
        self.assertEqual(charge_limit.read_saved(self.conf), (charge_limit.DEFAULT_END, None))


class TestReadLive(TempPaths):
    def test_available_when_both_readable(self):
        self.write(self.end, "85")
        self.write(self.start, "80")
        self.assertEqual(charge_limit.read_live(self.end, self.start),
                         {"available": True, "end": 85, "start": 80})

    def test_unavailable_when_either_file_is_missing(self):
        self.write(self.end, "85")
        live = charge_limit.read_live(self.end, os.path.join(self.d, "nope"))
        self.assertFalse(live["available"])
        self.assertIsNone(live["start"])

    def test_unavailable_when_unparseable(self):
        self.write(self.end, "not-a-number")
        self.write(self.start, "80")
        self.assertFalse(charge_limit.read_live(self.end, self.start)["available"])

    def test_reads_the_real_current_value_not_a_cache(self):
        self.write(self.end, "50")
        self.write(self.start, "45")
        self.assertEqual(charge_limit.read_live(self.end, self.start)["end"], 50)
        self.write(self.end, "95")     # 095 (or the owner) ran behind rp5deck's back
        self.assertEqual(charge_limit.read_live(self.end, self.start)["end"], 95)


class TestApply(TempPaths):
    def test_writes_end_then_start_then_the_saved_file(self):
        res = charge_limit.apply(85, conf_path=self.conf, end_path=self.end,
                                 start_path=self.start)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["end"], 85)
        self.assertEqual(res["start"], 80)
        self.assertEqual(self.read(self.end), "85")
        self.assertEqual(self.read(self.start), "80")
        self.assertEqual(self.read(self.conf), "85 80")

    def test_off_writes_bare_100_to_the_saved_file(self):
        res = charge_limit.apply(100, conf_path=self.conf, end_path=self.end,
                                 start_path=self.start)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.read(self.end), "100")
        self.assertEqual(self.read(self.conf), "100")     # bare, not "100 95"

    def test_clamps_into_the_reviewed_range(self):
        res = charge_limit.apply(30, conf_path=self.conf, end_path=self.end,
                                 start_path=self.start)
        self.assertEqual(res["end"], charge_limit.MIN_END)
        res = charge_limit.apply(99, conf_path=self.conf, end_path=self.end,
                                 start_path=self.start)
        self.assertEqual(res["end"], charge_limit.MAX_END)

    def test_end_write_order_before_start(self):
        order = []
        real_open = open

        def tracing_open(path, mode="r", **kw):
            if "w" in mode:
                order.append(path)
            return real_open(path, mode, **kw)
        import builtins
        orig = builtins.open
        builtins.open = tracing_open
        try:
            charge_limit.apply(85, conf_path=self.conf, end_path=self.end,
                               start_path=self.start)
        finally:
            builtins.open = orig
        self.assertEqual(order, [self.end, self.start, self.conf])

    def test_rejected_end_falls_back_to_100_and_reports_failure(self):
        # end_path's directory does not exist -> every write to it fails
        bad_end = os.path.join(self.d, "no-such-dir", "end_threshold")
        res = charge_limit.apply(85, conf_path=self.conf, end_path=bad_end,
                                 start_path=self.start)
        self.assertFalse(res["ok"])
        self.assertIn("rejected", res["detail"])

    def test_readback_mismatch_is_reported(self):
        """Simulates a kernel that silently clamps END lower than asked -
        095's own documented failure mode ("kept 45" after asking for 50)."""
        def clamping_write(_path, value):
            with open(self.end, "w", encoding="utf-8") as f:
                f.write(str(min(int(value), 60)))    # the "kernel" refuses anything > 60
            return True
        orig = charge_limit._write_int
        charge_limit._write_int = lambda path, value: (
            clamping_write(path, value) if path == self.end else orig(path, value))
        try:
            res = charge_limit.apply(85, conf_path=self.conf, end_path=self.end,
                                     start_path=self.start)
        finally:
            charge_limit._write_int = orig
        self.assertFalse(res["ok"])
        self.assertIn("60", res["detail"])

    def test_start_write_failure_is_a_mismatch_even_if_end_succeeded(self):
        bad_start = os.path.join(self.d, "no-such-dir", "start_threshold")
        res = charge_limit.apply(85, conf_path=self.conf, end_path=self.end,
                                 start_path=bad_start)
        self.assertFalse(res["ok"])
        self.assertEqual(self.read(self.end), "85")   # end itself did succeed


if __name__ == "__main__":
    unittest.main()
