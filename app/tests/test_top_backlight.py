"""The top screen's brightness on a device whose top panel has a real backlight: TopBacklight writes it like the bottom one
(with the top's own minimum), make_top picks it only when a backlight can be named, and the add-on keeps its software dimming."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import brightness  # noqa: E402
import screen_map  # noqa: E402


def slurp(path):
    with open(path) as f:
        return f.read().strip()


class Backlight(unittest.TestCase):
    def make(self, max_value=255, current=100):
        d = tempfile.mkdtemp(prefix="bl-")
        self.addCleanup(shutil.rmtree, d, True)
        for name, v in (("max_brightness", max_value), ("brightness", current)):
            with open(os.path.join(d, name), "w") as f:
                f.write(str(v))
        return d


class TestTopBacklight(Backlight):
    def test_it_writes_a_percentage_of_the_maximum(self):
        d = self.make()
        self.assertTrue(brightness.TopBacklight(d).set_pct(50))
        self.assertEqual(slurp(os.path.join(d, "brightness")), "128")

    def test_it_never_goes_below_the_tops_own_minimum(self):
        d = self.make()
        brightness.TopBacklight(d).set_pct(0)
        self.assertEqual(slurp(os.path.join(d, "brightness")), str(round(255 * brightness.TOP_MIN_PCT / 100.0)))

    def test_the_bottom_keeps_its_lower_minimum(self):
        d = self.make()
        brightness.BottomBacklight(d).set_pct(0)
        self.assertEqual(slurp(os.path.join(d, "brightness")), str(round(255 * brightness.BOTTOM_MIN_PCT / 100.0)))
        self.assertLess(brightness.BOTTOM_MIN_PCT, brightness.TOP_MIN_PCT)

    def test_full_brightness_writes_the_maximum(self):
        d = self.make(max_value=1000)
        brightness.TopBacklight(d).set_pct(100)
        self.assertEqual(slurp(os.path.join(d, "brightness")), "1000")
        brightness.TopBacklight(d).set_pct(500)
        self.assertEqual(slurp(os.path.join(d, "brightness")), "1000")

    def test_it_reads_the_live_value_back(self):
        d = self.make(max_value=200, current=50)
        self.assertEqual(brightness.TopBacklight(d).get_pct(), 25)

    def test_an_unreadable_backlight_says_so_and_returns_false(self):
        t = brightness.TopBacklight(os.path.join(tempfile.gettempdir(), "no-such-backlight-folder"))
        self.assertFalse(t.set_pct(50))
        self.assertIsNone(t.get_pct())

    def test_it_has_the_calls_the_controller_uses_on_a_top_screen_control(self):
        t = brightness.TopBacklight(self.make())
        self.assertFalse(t.running())
        self.assertIsNone(t.stop())                       # nothing to hand back, and no error

    def test_the_failure_message_names_the_top(self):
        with self.assertLogs("rp5deck.brightness", level="WARNING") as cm:
            brightness.TopBacklight(os.path.join(tempfile.gettempdir(), "no-such-backlight-folder")).set_pct(50)
        self.assertIn("top brightness", cm.output[0])


class TestMakeTop(unittest.TestCase):
    def names(self, *n):
        return lambda p: list(n)

    def test_two_built_in_panels_with_two_backlights_get_a_real_top_backlight(self):
        t = brightness.make_top(screen_map.THOR, listdir=self.names("a", "b"), env={})
        self.assertIsInstance(t, brightness.TopBacklight)
        self.assertEqual(t.path, "/sys/class/backlight/b")

    def test_the_rp5_keeps_software_dimming(self):
        t = brightness.make_top(screen_map.RP5, listdir=self.names("a", "b"), env={})
        self.assertIsInstance(t, brightness.TopDim)
        self.assertEqual(t.output, "DP-1")

    def test_a_built_in_device_with_no_nameable_top_backlight_falls_back_to_software_dimming(self):
        for names in ((), ("only",), ("a", "b", "c")):
            t = brightness.make_top(screen_map.THOR, listdir=self.names(*names), env={})
            self.assertIsInstance(t, brightness.TopDim, names)
            self.assertEqual(t.output, "DSI-2")

    def test_the_environment_override_makes_a_real_backlight_even_on_the_rp5(self):
        t = brightness.make_top(screen_map.RP5, listdir=self.names(), env={"RP5DECK_BACKLIGHTS": "/p/1,/p/2"})
        self.assertIsInstance(t, brightness.TopBacklight)
        self.assertEqual(t.path, "/p/2")

    def test_the_choice_is_logged_with_both_paths(self):
        with self.assertLogs("rp5deck.brightness", level="INFO") as cm:
            brightness.make_top(screen_map.THOR, listdir=self.names("a", "b"), env={})
        self.assertIn("/sys/class/backlight/b", cm.output[0])
        self.assertIn("/sys/class/backlight/a", cm.output[0])


class TestTheAppUsesIt(unittest.TestCase):
    def test_main_builds_the_top_control_with_make_top(self):
        with open(os.path.join(os.path.dirname(HERE), "main.py"), encoding="utf-8") as f:
            s = f.read()
        self.assertIn("self.top_dim = brightness.make_top()", s)
        self.assertNotIn("brightness.TopDim(", s)

    def test_both_kinds_of_top_control_answer_to_what_main_calls(self):
        for cls in (brightness.TopDim, brightness.TopBacklight):
            for name in ("set_pct", "stop"):
                self.assertTrue(callable(getattr(cls, name)), (cls, name))


if __name__ == "__main__":
    unittest.main()
