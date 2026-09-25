"""tests for rgb_leds.py (RG: thumb-stick RGB).

Everything here runs with a fake ledcontrol invoker, a fake sysfs
directory and a fake clock - no device, no subprocess (except one
deliberate call to a binary that cannot exist, to prove _run_tool fails
soft). The config round-trip test runs against the real, already-wired
config.py (the "lights" schema group main.py's build_ui() and
tools/g_break_tests.py both depend on) with RP5DECK_CONFIG pointed at a
temp file - no `patches/RG-config.patch` application needed any more; G
merged that patch's content into config.py directly (see config.py's
"lights" SCHEMA entries and the note in WIRED explaining why none of the
four are in that set)."""
import os
import shutil
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import rgb_leds  # noqa: E402


# ---------------------------------------------------------------------------
# Pure colour helpers
# ---------------------------------------------------------------------------
class TestHueRgb(unittest.TestCase):
    def test_primary_hues(self):
        self.assertEqual(rgb_leds.hue_to_rgb(0), (255, 0, 0))
        self.assertEqual(rgb_leds.hue_to_rgb(120), (0, 255, 0))
        self.assertEqual(rgb_leds.hue_to_rgb(240), (0, 0, 255))

    def test_wraps(self):
        self.assertEqual(rgb_leds.hue_to_rgb(360), rgb_leds.hue_to_rgb(0))
        self.assertEqual(rgb_leds.hue_to_rgb(-120), rgb_leds.hue_to_rgb(240))

    def test_zero_saturation_is_white(self):
        self.assertEqual(rgb_leds.hue_to_rgb(180, sat=0.0), (255, 255, 255))

    def test_rgb_to_hue_round_trip(self):
        for hue in (0, 45, 90, 135, 200, 300, 359):
            rgb = rgb_leds.hue_to_rgb(hue)
            got, sat = rgb_leds.rgb_to_hue(rgb)
            self.assertAlmostEqual(got, hue, delta=1.0, msg=(hue, rgb, got))
            self.assertAlmostEqual(sat, 1.0, delta=0.02)

    def test_rgb_to_hue_grey_has_zero_saturation(self):
        _hue, sat = rgb_leds.rgb_to_hue((128, 128, 128))
        self.assertEqual(sat, 0.0)


# ---------------------------------------------------------------------------
# The allow-list: build_argv()
# ---------------------------------------------------------------------------
class TestBuildArgv(unittest.TestCase):
    def test_order_is_right_then_left(self):
        argv = rgb_leds.build_argv(200, (1, 2, 3), (4, 5, 6))
        self.assertEqual(argv, [rgb_leds.TOOL, "200", "1", "2", "3", "4", "5", "6"])

    def test_refuses_out_of_range(self):
        with self.assertRaises(ValueError):
            rgb_leds.build_argv(200, (1, 2, 300), (4, 5, 6))
        with self.assertRaises(ValueError):
            rgb_leds.build_argv(-1, (1, 2, 3), (4, 5, 6))

    def test_refuses_bool(self):
        with self.assertRaises(ValueError):
            rgb_leds.build_argv(True, (0, 0, 0), (0, 0, 0))

    def test_refuses_non_int(self):
        with self.assertRaises(ValueError):
            rgb_leds.build_argv("200", (0, 0, 0), (0, 0, 0))
        with self.assertRaises(ValueError):
            rgb_leds.build_argv(1.5, (0, 0, 0), (0, 0, 0))

    def test_refuses_wrong_length(self):
        with self.assertRaises(ValueError):
            rgb_leds.build_argv(1, (1, 2), (1, 2, 3))


class TestRunTool(unittest.TestCase):
    def test_refuses_wrong_tool_or_length(self):
        with self.assertRaises(ValueError):
            rgb_leds._run_tool(["/bin/echo"] + ["0"] * 7)
        with self.assertRaises(ValueError):
            rgb_leds._run_tool([rgb_leds.TOOL] + ["0"] * 6)

    def test_missing_binary_fails_soft(self):
        orig = rgb_leds.TOOL
        rgb_leds.TOOL = os.path.join(tempfile.gettempdir(), "rg-no-such-ledcontrol-binary")
        try:
            argv = [rgb_leds.TOOL] + ["0"] * 7
            self.assertFalse(rgb_leds._run_tool(argv))
        finally:
            rgb_leds.TOOL = orig


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------
class TestApply(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake_run(self, argv):
        self.calls.append(argv)
        return True

    def test_rocknix_mode_runs_nothing(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_ROCKNIX)
        self.assertIsNone(rgb_leds.apply(st, run=self.fake_run))
        self.assertEqual(self.calls, [])

    def test_off_mode_zeroes_everything(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_OFF, left=(9, 9, 9), right=(8, 8, 8),
                                 brightness=50)
        rgb_leds.apply(st, run=self.fake_run)
        self.assertEqual(self.calls, [[rgb_leds.TOOL, "0", "0", "0", "0", "0", "0", "0"]])

    def test_colour_mode_right_first(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_COLOUR, left=(1, 2, 3), right=(4, 5, 6),
                                 brightness=100, linked=False)
        rgb_leds.apply(st, run=self.fake_run)
        self.assertEqual(self.calls, [[rgb_leds.TOOL, "100", "4", "5", "6", "1", "2", "3"]])


# ---------------------------------------------------------------------------
# LightState
# ---------------------------------------------------------------------------
class TestLightState(unittest.TestCase):
    def test_linked_forces_right_to_match_left(self):
        st = rgb_leds.LightState(linked=True, left=(1, 2, 3), right=(9, 9, 9))
        self.assertEqual(st.right, (1, 2, 3))

    def test_with_colour_linked_changes_both(self):
        st = rgb_leds.LightState(linked=True, left=(1, 1, 1))
        st2 = st.with_colour("right", (5, 6, 7))
        self.assertEqual((st2.left, st2.right), ((5, 6, 7), (5, 6, 7)))

    def test_with_colour_unlinked_changes_one_stick(self):
        st = rgb_leds.LightState(linked=False, left=(1, 1, 1), right=(2, 2, 2))
        st2 = st.with_colour("right", (9, 8, 7))
        self.assertEqual((st2.left, st2.right), ((1, 1, 1), (9, 8, 7)))

    def test_clamps_out_of_range_channels(self):
        st = rgb_leds.LightState(left=(-5, 300, 10), brightness=999)
        self.assertEqual(st.left, (0, 255, 10))
        self.assertEqual(st.brightness, 255)

    def test_equality_and_replace(self):
        a = rgb_leds.LightState(mode="colour", brightness=10)
        b = a.replace(brightness=10)
        self.assertEqual(a, b)
        c = a.replace(brightness=11)
        self.assertNotEqual(a, c)


# ---------------------------------------------------------------------------
# read_current(): a fake sysfs tree
# ---------------------------------------------------------------------------
def _write_led(base, name, brightness, index_order, intensities):
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "brightness"), "w") as f:
        f.write(str(brightness) + "\n")
    with open(os.path.join(d, "multi_index"), "w") as f:
        f.write(" ".join(index_order) + "\n")
    with open(os.path.join(d, "multi_intensity"), "w") as f:
        f.write(" ".join(str(v) for v in intensities) + "\n")


@unittest.skipIf(os.name == "nt", "rgb:l1/rgb:r1 have a ':' in the name - not a legal NTFS "
                                 "directory name, only exercised on WSL/the device")
class TestReadCurrent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rg-sysfs-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_parses_device_b_g_r_order(self):
        # multi_index = "blue green red", multi_intensity = "0 20 255"
        # (blue=0 green=20 red=255) - the real device's own order.
        _write_led(self.tmp, "rgb:l1", 200, ["blue", "green", "red"], [0, 20, 255])
        _write_led(self.tmp, "rgb:r1", 200, ["blue", "green", "red"], [7, 8, 9])
        got = rgb_leds.read_current(sysfs_dir=self.tmp)
        self.assertEqual(got, {"left": (255, 20, 0), "right": (9, 8, 7), "brightness": 200})

    def test_tolerates_a_different_order(self):
        _write_led(self.tmp, "rgb:l1", 50, ["red", "green", "blue"], [1, 2, 3])
        _write_led(self.tmp, "rgb:r1", 50, ["red", "green", "blue"], [4, 5, 6])
        got = rgb_leds.read_current(sysfs_dir=self.tmp)
        self.assertEqual(got["left"], (1, 2, 3))
        self.assertEqual(got["right"], (4, 5, 6))

    def test_missing_ring_is_unreadable(self):
        _write_led(self.tmp, "rgb:l1", 50, ["blue", "green", "red"], [1, 2, 3])
        # rgb:r1 deliberately absent
        self.assertIsNone(rgb_leds.read_current(sysfs_dir=self.tmp))

    def test_malformed_intensity_is_unreadable(self):
        _write_led(self.tmp, "rgb:l1", 50, ["blue", "green", "red"], [1, 2, 3])
        _write_led(self.tmp, "rgb:r1", 50, ["blue", "green"], [1, 2])   # short
        self.assertIsNone(rgb_leds.read_current(sysfs_dir=self.tmp))


# ---------------------------------------------------------------------------
# Controller: rate limiting + keeper
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestControllerDrag(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.calls = []
        self.state = rgb_leds.LightState(mode=rgb_leds.MODE_COLOUR, left=(1, 1, 1),
                                         right=(1, 1, 1), brightness=10, linked=True)
        self.c = rgb_leds.Controller(state=self.state, run=self._run, now=self.clock.now,
                                     rate_limit=0.08)

    def _run(self, argv):
        self.calls.append(argv)
        return True

    def test_set_state_always_applies(self):
        self.c.set_state(self.state.replace(brightness=20))
        self.assertEqual(len(self.calls), 1)

    def test_drag_is_rate_limited_and_flush_applies_final_value(self):
        self.c.set_state(self.state)                 # t=0, applies (call 1)
        self.assertEqual(len(self.calls), 1)

        self.clock.advance(0.05)                      # t=0.05, < 0.08 since last apply
        self.c.drag(self.state.replace(brightness=30))
        self.assertEqual(len(self.calls), 1, "a quick drag must not apply yet")

        self.clock.advance(0.05)                      # t=0.10, >= 0.08 since last apply (t=0)
        self.c.drag(self.state.replace(brightness=40))
        self.assertEqual(len(self.calls), 2, "the limiter must open and apply eventually")
        self.assertIn("40", self.calls[-1])

        self.c.drag(self.state.replace(brightness=50))   # same instant: rate-limited again
        self.assertEqual(len(self.calls), 2)

        self.c.drag_flush()
        self.assertEqual(len(self.calls), 3, "the drag's LAST value must always land")
        self.assertIn("50", self.calls[-1])

    def test_drag_flush_with_nothing_pending_is_a_no_op(self):
        self.c.set_state(self.state)
        n = len(self.calls)
        self.c.drag_flush()
        self.assertEqual(len(self.calls), n)


class TestKeeper(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.calls = []
        self.logs = []
        self.current = None            # what fake read_current() answers

    def _run(self, argv):
        self.calls.append(argv)
        return True

    def _read(self):
        return self.current

    def _log(self, msg):
        self.logs.append(msg)

    def _controller(self, state, **kw):
        return rgb_leds.Controller(state=state, run=self._run, read=self._read,
                                   now=self.clock.now, keeper_period=5.0, max_tries=3,
                                   log_fn=self._log, **kw)

    def test_never_acts_in_rocknix_mode(self):
        c = self._controller(rgb_leds.LightState(mode=rgb_leds.MODE_ROCKNIX))
        self.current = None                # unreadable, would look like a mismatch otherwise
        self.assertEqual(c.keeper_tick(force=True), "rocknix")
        self.assertEqual(self.calls, [])

    def test_too_soon_without_force(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_COLOUR, left=(1, 1, 1), right=(1, 1, 1),
                                 brightness=10)
        c = self._controller(st)
        self.current = {"left": st.left, "right": st.right, "brightness": st.brightness}
        self.assertEqual(c.keeper_tick(), "ok")
        self.assertEqual(c.keeper_tick(), "too-soon")

    def test_restores_on_mismatch_and_logs(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_COLOUR, left=(10, 20, 30), right=(1, 2, 3),
                                 brightness=100)
        c = self._controller(st)
        self.current = {"left": (0, 0, 0), "right": (0, 0, 0), "brightness": 0}   # ROCKNIX stomped it
        self.assertEqual(c.keeper_tick(force=True), "restored")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any("restored stick lights" in m for m in self.logs))

    def test_gives_up_after_max_tries_and_logs_once(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_COLOUR, left=(10, 20, 30), right=(1, 2, 3),
                                 brightness=100)
        c = self._controller(st)
        self.current = {"left": (0, 0, 0), "right": (0, 0, 0), "brightness": 0}
        results = [c.keeper_tick(force=True) for _ in range(6)]
        self.assertEqual(results, ["restored", "restored", "restored", "gave-up", "gave-up",
                                   "gave-up"])
        self.assertEqual(len(self.calls), 3, "must stop calling ledcontrol once given up")
        give_up_logs = [m for m in self.logs if "giving up" in m]
        self.assertEqual(len(give_up_logs), 1, "the give-up message must be logged exactly once")

    def test_set_state_resets_backoff(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_COLOUR, left=(10, 20, 30), right=(1, 2, 3),
                                 brightness=100)
        c = self._controller(st)
        self.current = {"left": (0, 0, 0), "right": (0, 0, 0), "brightness": 0}
        for _ in range(4):
            c.keeper_tick(force=True)
        self.assertEqual(c.keeper_tick(force=True), "gave-up")
        c.set_state(st.replace(brightness=101))
        self.assertEqual(c.keeper_tick(force=True), "restored", "a fresh change must retry")

    def test_on_es_wake_forces_a_check(self):
        st = rgb_leds.LightState(mode=rgb_leds.MODE_COLOUR, left=(1, 1, 1), right=(1, 1, 1),
                                 brightness=10)
        c = self._controller(st)
        self.current = {"left": (9, 9, 9), "right": (9, 9, 9), "brightness": 9}
        self.assertEqual(c.on_es_wake(), "restored")


# ---------------------------------------------------------------------------
# config.py bridge - state_from_config()/config_changes_for()
#
# G merged RG-config.patch's content into the real config.py directly (it
# is no longer a dangling patch file applied only to temp copies) - see
# config.py's "lights" SCHEMA entries and its WIRED comment. These tests
# run against the real `config` module, and the round-trip test below
# points RP5DECK_CONFIG at a temp file rather than re-applying a patch
# that is already part of the file on disk.
# ---------------------------------------------------------------------------
class TestConfigBridgeAgainstRealConfig(unittest.TestCase):
    """config.py's "lights" schema defaults match LightState()'s own
    defaults exactly (rocknix / linked / #ff0000 / #ff0000 / 200), so
    state_from_config() on a freshly-defaulted config must still equal
    LightState() - this also covers state_from_config()'s fallback path
    (get_value() answering None) for any key a future schema change drops."""

    def test_defaults_match_light_state_defaults(self):
        import config
        cfg = config.defaults()
        st = rgb_leds.state_from_config(cfg)
        self.assertEqual(st, rgb_leds.LightState())

    def test_config_changes_for_shape(self):
        st = rgb_leds.LightState(mode="colour", linked=False, left=(1, 2, 3), right=(4, 5, 6),
                                 brightness=42)
        changes = rgb_leds.config_changes_for(st)
        self.assertEqual(changes, {
            ("lights", "mode"): "colour",
            ("lights", "linked"): False,
            ("lights", "left"): "#010203",
            ("lights", "right"): "#040506",
            ("lights", "brightness"): 42,
        })


class TestConfigRoundTripThroughTheRealSchema(unittest.TestCase):
    """Round-trips a LightState through the real config.save_changes()/
    config.load() API (a temp file, never the device's actual
    config.json), proving rgb_leds talks to config.py exactly the way
    config.py itself expects."""

    def setUp(self):
        import config
        self.config = config
        self.tmp = tempfile.mkdtemp(prefix="rg-config-roundtrip-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_lights_schema_present_but_not_generically_wired(self):
        # In SCHEMA/GROUPS (get_value/set_value/save_changes all work), but
        # deliberately NOT in WIRED: their one real control is the
        # dedicated rgb_view.RGBSheet, not a generic settings_view.py row -
        # see config.py's WIRED comment for why (settings_view.py has no
        # "hex_color" renderer, and none is needed).
        self.assertIn("lights", self.config.GROUPS)
        self.assertIsNotNone(self.config.field_for(("lights", "mode")))
        self.assertIsNotNone(self.config.field_for(("lights", "brightness")))
        self.assertNotIn(("lights", "mode"), self.config.WIRED)
        self.assertNotIn(("lights", "left"), self.config.WIRED)
        self.assertNotIn(("lights", "brightness"), self.config.WIRED)

    def test_round_trip_through_save_changes_and_load(self):
        cfg = self.config.defaults()
        default_state = rgb_leds.state_from_config(cfg)
        self.assertEqual(default_state.mode, rgb_leds.MODE_ROCKNIX)

        state = rgb_leds.LightState(mode="colour", linked=False, left=(10, 20, 30),
                                    right=(40, 50, 60), brightness=77)
        changes = rgb_leds.config_changes_for(state)
        json_path = os.path.join(self.tmp, "config.json")
        self.config.save_changes(changes, path=json_path)

        cfg2, _note = self.config.load(path=json_path)
        state2 = rgb_leds.state_from_config(cfg2)
        self.assertEqual(state2, state)

    def test_invalid_hex_value_is_refused_not_written(self):
        cfg = self.config.defaults()
        ok = self.config.set_value(cfg, ("lights", "left"), "not-a-colour")
        self.assertFalse(ok)
        self.assertEqual(self.config.get_value(cfg, ("lights", "left")), "#ff0000")


if __name__ == "__main__":
    unittest.main()
