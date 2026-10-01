"""14c: screen_idle - the Command Center's panel follows ES's screensaver. Offline:
a temp backlight dir, a temp state file, a temp es_settings.cfg."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import esevents  # noqa: E402
import screen_idle as si  # noqa: E402


class Rig(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.bl = os.path.join(self.d, "bl")
        os.makedirs(self.bl)
        self.set_bl(127)
        self.state = os.path.join(self.d, "state.json")
        self.docked = True
        self.style = "dim"

    def set_bl(self, v):
        with open(os.path.join(self.bl, "brightness"), "w") as f:
            f.write("%d\n" % v)

    def bl_now(self):
        with open(os.path.join(self.bl, "brightness")) as f:
            return int(f.read())

    def make(self):
        return si.ScreenIdle(is_docked=lambda: self.docked, behavior=lambda: self.style,
                             backlight=self.bl, state_path=self.state)


class Styles(Rig):
    def test_dim_then_wake(self):
        s = self.make()
        self.assertTrue(s.on_es_event(esevents.SCREENSAVER_START))
        self.assertEqual(self.bl_now(), int(127 * si.DIM_FRACTION))
        with open(self.state) as f:
            self.assertEqual(json.load(f), {"kind": "dim", "brightness": 127})
        self.assertTrue(s.on_es_event(esevents.SCREENSAVER_STOP))
        self.assertEqual(self.bl_now(), 127)
        self.assertFalse(os.path.exists(self.state))

    def test_dim_never_reaches_zero(self):
        self.set_bl(20)
        s = self.make()
        s.idle()
        self.assertEqual(self.bl_now(), si.DIM_MIN)

    def test_black_styles_go_dark(self):
        for style in ("black", "random video", "slideshow", "Black"):
            self.set_bl(127)
            self.style = style
            s = self.make()
            self.assertTrue(s.idle(), style)
            self.assertEqual(self.bl_now(), 0, style)
            s.wake()
            self.assertEqual(self.bl_now(), 127, style)

    def test_suspend_and_unknown_do_nothing(self):
        for style in ("suspend", "", "something new"):
            self.style = style
            self.assertFalse(self.make().idle(), style)
            self.assertEqual(self.bl_now(), 127)

    def test_wake_event_also_restores(self):
        s = self.make()
        s.idle()
        self.assertTrue(s.on_es_event(esevents.WAKE))
        self.assertEqual(self.bl_now(), 127)

    def test_other_events_ignored(self):
        s = self.make()
        self.assertFalse(s.on_es_event(esevents.SLEEP))
        self.assertEqual(self.bl_now(), 127)


class Guards(Rig):
    def test_undocked_does_nothing(self):
        self.docked = False
        self.assertFalse(self.make().idle())
        self.assertEqual(self.bl_now(), 127)

    def test_second_start_keeps_the_first_saved_value(self):
        s = self.make()
        s.idle()
        self.assertFalse(s.idle())
        s.wake()
        self.assertEqual(self.bl_now(), 127)

    def test_already_dark_is_left_alone(self):
        self.set_bl(0)
        self.assertFalse(self.make().idle())
        self.assertFalse(os.path.exists(self.state))

    def test_wake_without_idle(self):
        self.assertFalse(self.make().wake())

    def test_touch_wakes_once_and_is_swallowed_once(self):
        s = self.make()
        s.idle()
        self.assertTrue(s.on_touch())
        self.assertFalse(s.on_touch())
        self.assertEqual(self.bl_now(), 127)

    def test_unreadable_backlight(self):
        shutil.rmtree(self.bl)
        self.assertFalse(self.make().idle())
        self.assertFalse(os.path.exists(self.state))

    def test_shutdown_restores(self):
        s = self.make()
        s.idle()
        s.shutdown()
        self.assertEqual(self.bl_now(), 127)


class CrashSafety(Rig):
    def test_leftover_record_is_undone_at_start(self):
        s = self.make()
        s.idle()                       # "crash": no wake
        self.assertEqual(self.bl_now(), int(127 * si.DIM_FRACTION))
        fresh = self.make()
        self.assertTrue(fresh.restore_leftovers())
        self.assertEqual(self.bl_now(), 127)
        self.assertFalse(os.path.exists(self.state))

    def test_no_leftover(self):
        self.assertFalse(self.make().restore_leftovers())

    def test_record_is_written_before_the_backlight(self):
        seen = []
        s = self.make()
        orig = s._write_brightness

        def spy(v):
            seen.append(os.path.exists(self.state))
            orig(v)
        s._write_brightness = spy
        s.idle()
        self.assertEqual(seen, [True])


class Behavior(unittest.TestCase):
    def cfg(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "es_settings.cfg")
        with open(p, "w") as f:
            f.write(text)
        return si.es_screensaver_behavior(p)

    def test_reads_value(self):
        self.assertEqual(self.cfg('<config>\n\t<string name="ScreenSaverBehavior" value="black" />\n</config>'),
                         "black")

    def test_default_is_dim(self):
        self.assertEqual(self.cfg("<config>\n</config>"), "dim")
        self.assertEqual(si.es_screensaver_behavior("/nonexistent/es_settings.cfg"), "dim")


if __name__ == "__main__":
    unittest.main()


class Wiring(unittest.TestCase):
    """main.App routes ES events and touches through screen_idle (the real App, no SDL)."""

    def setUp(self):
        import argparse
        import types
        import main
        self.types = types
        self.app = main.App(argparse.Namespace(seconds=0, output=None))
        self.calls = []
        test = self

        class FakeIdle:
            woke = True

            def on_es_event(self, kind):
                test.calls.append(("es", kind))

            def on_touch(self):
                test.calls.append(("touch",))
                return self.woke

            def shutdown(self):
                test.calls.append(("shutdown",))

        class FakeRouter:
            def down(self, pid, x, y):
                test.calls.append(("router.down",))
                return None

        class FakeCompanion:
            def on_es_event(self, ev):
                test.calls.append(("companion", ev.kind))

        self.idle = FakeIdle()
        self.app.screen_idle = self.idle
        self.app.router = FakeRouter()
        self.app.companion = FakeCompanion()
        self.app.rgb = None

    def ev(self, kind):
        return self.types.SimpleNamespace(kind=kind, source="hook", system="", rom_path="", name="")

    def test_es_events_reach_screen_idle(self):
        self.app.on_es_event(self.ev(esevents.SCREENSAVER_START))
        self.assertIn(("es", esevents.SCREENSAVER_START), self.calls)
        self.assertIn(("companion", esevents.SCREENSAVER_START), self.calls)

    def test_waking_touch_is_swallowed(self):
        self.idle.woke = True
        self.app._down(1, 10, 10, "touch")
        self.assertIn(("touch",), self.calls)
        self.assertNotIn(("router.down",), self.calls)

    def test_normal_touch_goes_to_the_ui(self):
        self.idle.woke = False
        self.app._down(1, 10, 10, "touch")
        self.assertIn(("router.down",), self.calls)

    def fake_sway(self, outputs):
        """Replace sway_ipc's socket with a fake answering GET_OUTPUTS."""
        import main
        orig = (main.sway_ipc.find_socket, main.sway_ipc.Ipc)

        class FakeIpc:
            def __init__(self, path, timeout):
                pass

            def request(self, mtype):
                assert mtype == main.sway_ipc.GET_OUTPUTS
                return outputs

            def close(self):
                pass
        main.sway_ipc.find_socket = lambda: "/fake/sway.sock"
        main.sway_ipc.Ipc = FakeIpc
        self.addCleanup(lambda: (setattr(main.sway_ipc, "find_socket", orig[0]),
                                 setattr(main.sway_ipc, "Ipc", orig[1])))

    def test_acts_when_on_dsi1_and_addon_active(self):
        self.fake_sway([{"name": "DSI-1", "active": True}, {"name": "DP-1", "active": True}])
        self.app.output, self.app.es_output = "DSI-1", "DP-1"
        self.assertTrue(self.app._panel_is_ours_and_docked())

    def test_only_acts_on_dsi1(self):
        self.fake_sway([{"name": "DSI-1", "active": True}, {"name": "DP-1", "active": True}])
        self.app.output, self.app.es_output = "DP-1", "DSI-1"    # screens swapped
        self.assertFalse(self.app._panel_is_ours_and_docked())

    def test_not_when_addon_inactive(self):
        self.fake_sway([{"name": "DSI-1", "active": True}, {"name": "DP-1", "active": False}])
        self.app.output, self.app.es_output = "DSI-1", "DP-1"
        self.assertFalse(self.app._panel_is_ours_and_docked())
