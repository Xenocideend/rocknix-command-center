"""Batch 1: brightness.py (bottom backlight, top gamma dimming, Match) and its
main.App wiring. Offline: a temp sysfs dir and a fake gamma helper process."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import brightness as b  # noqa: E402
import config  # noqa: E402


class Ramps(unittest.TestCase):
    def test_shape_and_ends(self):
        r = b.ramps(256, 1.0)
        self.assertEqual(len(r), 3 * 256 * 2)
        self.assertEqual(int.from_bytes(r[0:2], "little"), 0)
        self.assertEqual(int.from_bytes(r[510:512], "little"), 65535)
        self.assertEqual(r[:512], r[512:1024])          # R == G == B

    def test_level_scales_the_top(self):
        r = b.ramps(256, 0.5)
        self.assertEqual(int.from_bytes(r[510:512], "little"), 32768)

    def test_level_is_clamped(self):
        self.assertEqual(b.ramps(4, 0.0), b.ramps(4, 0.05))
        self.assertEqual(b.ramps(4, 3.0), b.ramps(4, 1.0))


class Match(unittest.TestCase):
    def test_matched_top(self):
        self.assertEqual(b.matched_top(50, 1.2), 60)
        self.assertEqual(b.matched_top(90, 1.5), 100)            # capped
        self.assertEqual(b.matched_top(5, 1.0), b.TOP_MIN_PCT)   # floored

    def test_ratio(self):
        self.assertAlmostEqual(b.match_ratio(60, 50), 1.2)
        self.assertAlmostEqual(b.match_ratio(60, 0), 60.0)       # no division by zero


class Backlight(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        for n, v in (("max_brightness", 255), ("brightness", 127)):
            with open(os.path.join(self.d, n), "w") as f:
                f.write("%d\n" % v)
        self.bl = b.BottomBacklight(self.d)

    def raw(self):
        with open(os.path.join(self.d, "brightness")) as f:
            return int(f.read())

    def test_get_and_set(self):
        self.assertEqual(self.bl.get_pct(), 50)
        self.assertTrue(self.bl.set_pct(100))
        self.assertEqual(self.raw(), 255)

    def test_never_zero(self):
        self.bl.set_pct(0)
        self.assertEqual(self.raw(), round(255 * b.BOTTOM_MIN_PCT / 100.0))

    def test_unreadable(self):
        shutil.rmtree(self.d)
        self.assertIsNone(self.bl.get_pct())
        self.assertFalse(self.bl.set_pct(50))


class FakeProc:
    def __init__(self):
        self.lines = []
        self.alive = True
        self.stdin = self

    def write(self, s):
        if not self.alive:
            raise BrokenPipeError()
        self.lines.append(s)

    def flush(self):
        pass

    def close(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None if self.alive else 0

    def kill(self):
        self.alive = False


class Top(unittest.TestCase):
    def setUp(self):
        self.spawned = []

        def spawn():
            p = FakeProc()
            self.spawned.append(p)
            return p
        self.t = b.TopDim("DP-1", spawn=spawn)

    def test_100_needs_no_helper(self):
        self.t.set_pct(100)
        self.assertEqual(self.spawned, [])

    def test_dim_starts_once_and_feeds_levels(self):
        self.t.set_pct(60)
        self.t.set_pct(40)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0].lines, ["0.600\n", "0.400\n"])

    def test_back_to_100_stops_the_helper(self):
        self.t.set_pct(60)
        self.t.set_pct(100)
        self.assertFalse(self.spawned[0].alive)
        self.assertFalse(self.t.running())

    def test_floor(self):
        self.t.set_pct(1)
        self.assertEqual(self.spawned[0].lines, ["%.3f\n" % (b.TOP_MIN_PCT / 100.0)])

    def test_a_dead_helper_is_restarted(self):
        self.t.set_pct(60)
        self.spawned[0].alive = False
        self.t.set_pct(50)
        self.assertEqual(len(self.spawned), 2)


class Wiring(unittest.TestCase):
    """main.App.on_setting drives the two screens and keeps Match in step."""

    def setUp(self):
        import argparse
        import main
        self.app = main.App(argparse.Namespace(seconds=0, output=None))
        self.app.cfg = config.defaults()
        self.bottom = []
        self.top = []
        test = self

        class FakeBottom:
            live = 50

            def set_pct(self, p):
                test.bottom.append(p)

            def get_pct(self):
                return self.live

        class FakeTop:
            def set_pct(self, p):
                test.top.append(p)

            def stop(self):
                test.top.append("stop")
        self.app.bottom_bl = FakeBottom()
        self.app.top_dim = FakeTop()
        self.app._schedule_save = lambda: None
        self.app._save_now = lambda: None

    def set(self, key, value):
        config.set_value(self.app.cfg, ("screens", key), value)
        self.app.on_setting(("screens", key), value)

    def get(self, key):
        return config.get_value(self.app.cfg, ("screens", key))

    def test_sliders_drive_each_screen(self):
        self.set("bottom_brightness", 70)
        self.set("top_brightness", 40)
        self.assertEqual(self.bottom, [70])
        self.assertEqual(self.top, [40])

    def test_match_keeps_the_ratio(self):
        self.set("top_brightness", 60)                  # bottom live 50 -> ratio 1.2
        self.set("match_brightness", True)
        self.assertAlmostEqual(self.get("match_ratio"), 1.2)
        self.set("bottom_brightness", 70)
        self.assertEqual(self.get("top_brightness"), 84)
        self.assertEqual(self.top[-1], 84)

    def test_without_match_top_does_not_follow(self):
        self.set("bottom_brightness", 70)
        self.assertEqual(self.top, [])

    def test_moving_top_while_matched_resets_the_ratio(self):
        self.set("match_brightness", True)
        config.set_value(self.app.cfg, ("screens", "bottom_brightness"), 50)
        self.set("top_brightness", 25)
        self.assertAlmostEqual(self.get("match_ratio"), 0.5)

    def test_top_applied_at_start_only_when_dimmed(self):
        self.app._apply_top_brightness()
        self.assertEqual(self.top, [])
        config.set_value(self.app.cfg, ("screens", "top_brightness"), 55)
        self.app._apply_top_brightness()
        self.assertEqual(self.top, [55])


if __name__ == "__main__":
    unittest.main()
