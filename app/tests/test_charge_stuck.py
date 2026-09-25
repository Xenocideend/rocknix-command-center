#!/usr/bin/env python3
"""charge_stuck.py - the "charger connected but not charging" guard:
evaluate() (pure, synthetic sample sequences), read_register() (seek-based,
against a real and a fake regmap file - never a full read), Sampler.sample()
(every path a tempfile or a poison stand-in - never real sysfs/debugfs)."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import charge_stuck as cs  # noqa: E402


def s(t, plugged, current_ua=None, icl=None):
    return {"t": t, "plugged": plugged, "current_ua": current_ua, "icl": icl}


# ---------------------------------------------------------------------------
# read_register()
# ---------------------------------------------------------------------------
class SeekTrackingFile:
    """A fake file object: records every seek()/read() call so a test can
    prove read_register() seeks straight to the register's own line and
    reads exactly REG_LINE_LEN bytes - never walks the file from the start
    the way a naive full read would."""

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def seek(self, pos):
        self.calls.append(("seek", pos))
        self.pos = pos

    def read(self, n=-1):
        self.calls.append(("read", n))
        chunk = self.data[self.pos:] if n < 0 else self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


def regmap_bytes(overrides, max_reg=0x1110):
    """A synthetic regmap file: one "rrrr: vv\\n" line per register up to
    max_reg, every value 0xff except `overrides` (reg -> value)."""
    lines = []
    for r in range(max_reg + 1):
        v = overrides.get(r, 0xFF)
        lines.append(b"%04x: %02x\n" % (r, v))
    return b"".join(lines)


class TestReadRegisterRealFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-cs-regmap-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, "registers")

    def write(self, data):
        with open(self.path, "wb") as f:
            f.write(data)

    def test_reads_the_right_register(self):
        self.write(regmap_bytes({cs.REG_ICL_STATUS: 0x00, cs.REG_POWER_PATH: 0x13}))
        self.assertEqual(cs.read_register(cs.REG_ICL_STATUS, self.path), 0x00)
        self.assertEqual(cs.read_register(cs.REG_POWER_PATH, self.path), 0x13)

    def test_good_values(self):
        self.write(regmap_bytes({cs.REG_ICL_STATUS: 0x0C, cs.REG_POWER_PATH: 0x95}))
        self.assertEqual(cs.read_register(cs.REG_ICL_STATUS, self.path), 0x0C)

    def test_missing_file(self):
        self.assertIsNone(cs.read_register(cs.REG_ICL_STATUS, os.path.join(self.d, "nope")))

    def test_short_file_past_the_register(self):
        self.write(regmap_bytes({}, max_reg=4))          # far short of REG_ICL_STATUS
        self.assertIsNone(cs.read_register(cs.REG_ICL_STATUS, self.path))

    def test_truncated_line(self):
        full = regmap_bytes({cs.REG_ICL_STATUS: 0x00})
        self.write(full[:cs.REG_ICL_STATUS * cs.REG_LINE_LEN + 5])   # cut mid-line
        self.assertIsNone(cs.read_register(cs.REG_ICL_STATUS, self.path))

    def test_malformed_line_wrong_register_number(self):
        # simulate the offset math landing on the wrong line (a defence-in-
        # depth check, not something a real regmap file should ever do).
        data = bytearray(regmap_bytes({cs.REG_ICL_STATUS: 0x00}))
        off = cs.REG_ICL_STATUS * cs.REG_LINE_LEN
        data[off:off + 4] = b"dead"
        self.write(bytes(data))
        self.assertIsNone(cs.read_register(cs.REG_ICL_STATUS, self.path))

    def test_directory_instead_of_file(self):
        self.assertIsNone(cs.read_register(cs.REG_ICL_STATUS, self.d))


class TestReadRegisterSeeksOnly(unittest.TestCase):
    def test_never_reads_more_than_one_line(self):
        data = regmap_bytes({cs.REG_ICL_STATUS: 0x00})
        f = SeekTrackingFile(data)
        v = cs.read_register(cs.REG_ICL_STATUS, "unused", opener=lambda *a, **k: f)
        self.assertEqual(v, 0x00)
        self.assertEqual(f.calls, [("seek", cs.REG_ICL_STATUS * cs.REG_LINE_LEN),
                                   ("read", cs.REG_LINE_LEN)])


# ---------------------------------------------------------------------------
# evaluate() - pure
# ---------------------------------------------------------------------------
class TestEvaluate(unittest.TestCase):
    def test_no_samples(self):
        self.assertEqual(cs.evaluate([])[0], False)

    def test_stuck_steady(self):
        samples = [s(i * 10.0, True, 700_000, 0x00) for i in range(6)]
        stuck, reason = cs.evaluate(samples)
        self.assertTrue(stuck, reason)
        self.assertIn("uA", reason)

    def test_stuck_flipping_icl_half_zero(self):
        # ICL flips 0x00 <-> nonzero roughly every sample - exactly half
        # zero must still count as stuck (the owner's own ">= half" rule).
        vals = [0x00, 0x0A, 0x00, 0x0A, 0x00, 0x0A]
        samples = [s(i * 10.0, True, 600_000, v) for i, v in enumerate(vals)]
        stuck, reason = cs.evaluate(samples)
        self.assertTrue(stuck, reason)

    def test_not_stuck_when_fewer_than_half_zero(self):
        vals = [0x00, 0x0A, 0x0A, 0x0A, 0x00, 0x0A]     # 2/6 zero
        samples = [s(i * 10.0, True, 600_000, v) for i, v in enumerate(vals)]
        stuck, reason = cs.evaluate(samples)
        self.assertFalse(stuck, reason)

    def test_good_charging(self):
        # negative current_now: actually charging.
        samples = [s(i * 10.0, True, -300_000, 0x0C) for i in range(6)]
        self.assertFalse(cs.evaluate(samples)[0])

    def test_good_but_paused_at_the_safe_charge_limit(self):
        samples = [s(i * 10.0, True, 0, None) for i in range(6)]   # too low to trigger ICL reads
        self.assertFalse(cs.evaluate(samples)[0])

    def test_heavy_load_on_a_good_charger_never_warns(self):
        # discharging under load, but ICL_STATUS reads nonzero throughout.
        samples = [s(i * 10.0, True, 300_000, 0x0C) for i in range(6)]
        stuck, reason = cs.evaluate(samples)
        self.assertFalse(stuck, reason)

    def test_unplugged_and_sourcing_the_addon_never_warns(self):
        samples = [s(i * 10.0, False, 860_000, None) for i in range(6)]
        stuck, reason = cs.evaluate(samples)
        self.assertFalse(stuck)
        self.assertEqual(reason, "unplugged")

    def test_debugfs_unreadable_never_warns(self):
        # plugged, discharging, but every ICL read failed (icl always None).
        samples = [s(i * 10.0, True, 700_000, None) for i in range(6)]
        stuck, reason = cs.evaluate(samples)
        self.assertFalse(stuck, reason)
        self.assertIn("ICL_STATUS", reason)

    def test_replug_clears_instantly(self):
        stuck_samples = [s(i * 10.0, True, 700_000, 0x00) for i in range(6)]
        self.assertTrue(cs.evaluate(stuck_samples)[0])
        unplugged = stuck_samples + [s(60.0, False, None, None)]
        stuck, reason = cs.evaluate(unplugged)
        self.assertFalse(stuck)
        self.assertEqual(reason, "unplugged")

    def test_two_good_samples_in_a_row_clear_before_the_window_rolls_off(self):
        bad = [s(i * 10.0, True, 700_000, 0x00) for i in range(6)]
        # replugged: still plugged, but current now negative (charging) for
        # the last two samples - the window itself (mostly bad) would still
        # say "stuck" on its own; the fast-clear rule must win anyway.
        recovered = bad + [s(60.0, True, -50_000, 0x0C), s(70.0, True, -50_000, 0x0C)]
        stuck, reason = cs.evaluate(recovered)
        self.assertFalse(stuck, reason)
        self.assertIn("recovered", reason)

    def test_one_good_sample_alone_does_not_clear(self):
        bad = [s(i * 10.0, True, 700_000, 0x00) for i in range(6)]
        one_good = bad + [s(60.0, True, -50_000, 0x0C)]
        stuck, _ = cs.evaluate(one_good)
        self.assertTrue(stuck)

    def test_not_enough_samples_yet(self):
        samples = [s(0.0, True, 700_000, 0x00), s(10.0, True, 700_000, 0x00)]
        stuck, reason = cs.evaluate(samples, min_samples=3)
        self.assertFalse(stuck)
        self.assertIn("not enough", reason)

    def test_a_gap_in_being_plugged_within_the_window_refuses(self):
        samples = [s(0.0, True, 700_000, 0x00), s(10.0, False, None, None),
                  s(20.0, True, 700_000, 0x00), s(30.0, True, 700_000, 0x00)]
        stuck, reason = cs.evaluate(samples)
        self.assertFalse(stuck)
        self.assertIn("whole window", reason)

    def test_current_exactly_at_the_discharge_threshold_is_not_discharging(self):
        samples = [s(i * 10.0, True, cs.DISCHARGE_UA, 0x00) for i in range(6)]
        self.assertFalse(cs.evaluate(samples)[0])

    def test_current_one_over_the_threshold_is_discharging(self):
        samples = [s(i * 10.0, True, cs.DISCHARGE_UA + 1, 0x00) for i in range(6)]
        self.assertTrue(cs.evaluate(samples)[0])

    def test_fast_clear_current_exactly_zero_counts_as_recovered(self):
        bad = [s(i * 10.0, True, 700_000, 0x00) for i in range(4)]
        recovered = bad + [s(40.0, True, 0, None), s(50.0, True, 0, None)]
        self.assertFalse(cs.evaluate(recovered)[0])

    def test_fast_clear_current_one_above_zero_does_not_count_alone(self):
        bad = [s(i * 10.0, True, 700_000, 0x00) for i in range(4)]
        still_bad = bad + [s(40.0, True, 1, 0x00), s(50.0, True, 1, 0x00)]
        self.assertTrue(cs.evaluate(still_bad)[0])

    def test_window_only_considers_samples_within_window_s(self):
        old_good = [s(0.0, True, -50_000, 0x0C)]
        # a big gap, then a fresh run of bad samples starting well past
        # window_s later - the old good sample must not dilute the mean.
        bad = [s(1000.0 + i * 10.0, True, 700_000, 0x00) for i in range(6)]
        stuck, reason = cs.evaluate(old_good + bad, window_s=60.0)
        self.assertTrue(stuck, reason)


# ---------------------------------------------------------------------------
# Sampler - every seam faked
# ---------------------------------------------------------------------------
class TempFiles(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-cs-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.role = os.path.join(self.d, "power_role")
        self.voltage = os.path.join(self.d, "in_voltage_usb_in_v_div_16_input")
        self.current = os.path.join(self.d, "current_now")
        self.regmap = os.path.join(self.d, "registers")

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def write_regmap(self, overrides):
        with open(self.regmap, "wb") as f:
            f.write(regmap_bytes(overrides))

    def make(self, **kw):
        kw.setdefault("power_role_path", self.role)
        kw.setdefault("voltage_glob", self.voltage)     # a literal path also matches glob()
        kw.setdefault("current_path", self.current)
        kw.setdefault("regmap_path", self.regmap)
        kw.setdefault("clock", lambda: 123.0)
        return cs.Sampler(**kw)


class TestSampler(TempFiles):
    def test_full_stuck_reading(self):
        self.write(self.role, "source [sink]\n")
        self.write(self.voltage, "8992720\n")
        self.write(self.current, "700000\n")
        self.write_regmap({cs.REG_ICL_STATUS: 0x00, cs.REG_POWER_PATH: 0x13})
        r = self.make().sample()
        self.assertEqual(r, {"t": 123.0, "plugged": True, "current_ua": 700000, "icl": 0x00})

    def test_not_sink_role_never_reads_anything_else(self):
        self.write(self.role, "[source] sink\n")

        def opener(path, *a, **kw):
            if path != self.role:
                raise AssertionError("must not open %r once the role is not [sink]" % path)
            return open(path, *a, **kw)
        sampler = self.make(opener=opener)
        r = sampler.sample()
        self.assertEqual(r, {"t": 123.0, "plugged": False, "current_ua": None, "icl": None})

    def test_sink_but_low_voltage_is_not_plugged(self):
        self.write(self.role, "source [sink]\n")
        self.write(self.voltage, "4000000\n")            # <= PLUGGED_VOLTAGE_UV
        r = self.make().sample()
        self.assertFalse(r["plugged"])
        self.assertIsNone(r["current_ua"])

    def test_voltage_exactly_at_threshold_is_not_plugged(self):
        self.write(self.role, "source [sink]\n")
        self.write(self.voltage, str(cs.PLUGGED_VOLTAGE_UV))
        r = self.make().sample()
        self.assertFalse(r["plugged"])

    def test_missing_voltage_glob_match_is_not_plugged(self):
        self.write(self.role, "source [sink]\n")
        sampler = self.make(voltage_glob=os.path.join(self.d, "no-such-*-file"))
        r = sampler.sample()
        self.assertFalse(r["plugged"])

    def test_low_current_never_reads_the_regmap(self):
        self.write(self.role, "source [sink]\n")
        self.write(self.voltage, "8992720\n")
        self.write(self.current, "0\n")
        sampler = self.make(regmap_path=os.path.join(self.d, "poison"))

        def poison_open(path, *a, **kw):
            if path == sampler.regmap_path:
                raise AssertionError("must not read the regmap under the discharge threshold")
            return open(path, *a, **kw)
        sampler.opener = poison_open
        r = sampler.sample()
        self.assertTrue(r["plugged"])
        self.assertIsNone(r["icl"])

    def test_glob_only_runs_once_then_is_cached(self):
        self.write(self.role, "source [sink]\n")
        self.write(self.voltage, "8992720\n")
        self.write(self.current, "700000\n")
        self.write_regmap({cs.REG_ICL_STATUS: 0x00})
        calls = []

        def glob_fn(pattern):
            calls.append(pattern)
            return [self.voltage]
        sampler = self.make(voltage_glob="pattern", glob_fn=glob_fn)
        sampler.sample()
        sampler.sample()
        self.assertEqual(calls, ["pattern"])

    def test_debugfs_unreadable_logs_once(self):
        self.write(self.role, "source [sink]\n")
        self.write(self.voltage, "8992720\n")
        self.write(self.current, "700000\n")
        sampler = self.make(regmap_path=os.path.join(self.d, "no-such-regmap"))
        r1 = sampler.sample()
        r2 = sampler.sample()
        self.assertIsNone(r1["icl"])
        self.assertIsNone(r2["icl"])
        self.assertTrue(sampler._regmap_warned)


if __name__ == "__main__":
    unittest.main()
