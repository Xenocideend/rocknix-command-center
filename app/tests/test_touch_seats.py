"""touch_seats and main.App's use of it: with sway's two seats every finger arrived twice (once rotated, once not, same
finger id) and the second replaced the first in the router, so taps landed in random places. The app now listens to the
default seat's touch device only."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import touch_seats  # noqa: E402

TWO = {11: "Virtual core touch (seat0)", 12: "Virtual core touch (seat1)"}


class TestWantedIds(unittest.TestCase):
    def test_two_seats_keep_the_default_one(self):
        self.assertEqual(touch_seats.wanted_ids(TWO), {11})

    def test_the_order_of_the_ids_does_not_matter(self):
        self.assertEqual(touch_seats.wanted_ids({5: "Virtual core touch (seat1)", 9: "Virtual core touch (seat0)"}), {9})

    def test_one_seat_keeps_it(self):
        self.assertEqual(touch_seats.wanted_ids({11: "Virtual core touch (seat0)"}), {11})

    def test_a_lone_second_seat_is_kept_rather_than_losing_all_touch(self):
        self.assertEqual(touch_seats.wanted_ids({12: "Virtual core touch (seat1)"}), {12})

    def test_three_seats_keep_only_the_default(self):
        d = dict(TWO)
        d[13] = "Virtual core touch (seat2)"
        self.assertEqual(touch_seats.wanted_ids(d), {11})

    def test_other_touch_devices_are_never_dropped(self):
        d = dict(TWO)
        d[20] = "RetroidPocket RDS Touchscreen"
        self.assertEqual(touch_seats.wanted_ids(d), {11, 20})

    def test_names_that_say_nothing_about_seats_keep_everything(self):
        d = {1: "generic ft5x06 (a0)", 2: "RetroidPocket RDS Touchscreen"}
        self.assertEqual(touch_seats.wanted_ids(d), {1, 2})

    def test_no_devices_is_fine(self):
        self.assertEqual(touch_seats.wanted_ids({}), set())

    def test_seat_of_reads_only_a_trailing_seat_number(self):
        self.assertEqual(touch_seats.seat_of("Virtual core touch (seat1)"), 1)
        self.assertEqual(touch_seats.seat_of("seat1 pad"), None)
        self.assertIsNone(touch_seats.seat_of(None))
        self.assertIsNone(touch_seats.seat_of(""))


class TestTheAppUsesIt(unittest.TestCase):
    """The app's check on each finger event, with the device list faked."""

    def setUp(self):
        import main
        self.main = main
        from test_companion import AppCase

        class Case(AppCase):
            def runTest(self):
                pass
        self.case = Case()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.app = self.case.make_app()
        self.devices = dict(TWO)
        self.reads = []

        def read():
            self.reads.append(1)
            return dict(self.devices)
        self.app._read_touch_devices = read

    def test_the_second_seats_finger_is_dropped_and_the_first_kept(self):
        self.assertTrue(self.app._touch_wanted(11))
        self.assertFalse(self.app._touch_wanted(12))

    def test_the_decision_is_remembered_not_asked_again_for_each_event(self):
        for _ in range(20):
            self.app._touch_wanted(11)
            self.app._touch_wanted(12)
        self.assertEqual(len(self.reads), 1)

    def test_a_seat_that_appears_later_is_dropped_when_its_first_finger_arrives(self):
        self.devices = {11: "Virtual core touch (seat0)"}
        self.assertTrue(self.app._touch_wanted(11))
        self.devices = dict(TWO)                       # ES started and sway made seat1
        self.assertFalse(self.app._touch_wanted(12))
        self.assertTrue(self.app._touch_wanted(11))

    def test_an_id_that_is_not_in_the_device_list_is_kept(self):
        self.devices = {}
        self.assertTrue(self.app._touch_wanted(99))

    def test_the_real_device_reader_works_through_the_apps_own_sdl(self):
        """_read_touch_devices on the app's self.sdl (sdl3 is imported inside setup(), not at module level) against a
        fake that fills the count and hands back an id array like SDL_GetTouchDevices does."""
        import ctypes
        names = {11: b"Virtual core touch (seat0)", 12: b"Virtual core touch (seat1)"}
        freed = []

        class FakeSdl:
            @staticmethod
            def GetTouchDevices(count_ptr):
                count_ptr.contents.value = 2
                return (ctypes.c_uint64 * 2)(11, 12)

            @staticmethod
            def GetTouchDeviceName(i):
                return names[int(i)]

            @staticmethod
            def free(p):
                freed.append(1)
        app = self.case.make_app()
        app.sdl = FakeSdl
        self.assertEqual(type(app)._read_touch_devices(app), {11: "Virtual core touch (seat0)", 12: "Virtual core touch (seat1)"})
        self.assertEqual(freed, [1])

    def test_no_touch_devices_gives_an_empty_list(self):
        class FakeSdl:
            @staticmethod
            def GetTouchDevices(count_ptr):
                return None

            @staticmethod
            def free(p):
                raise AssertionError("nothing to free")
        app = self.case.make_app()
        app.sdl = FakeSdl
        self.assertEqual(type(app)._read_touch_devices(app), {})

    def test_the_finger_handler_asks_before_it_does_anything_else(self):
        import inspect
        src = inspect.getsource(self.main.App._handle)
        i = src.index("f = sdl.TouchFinger.from_buffer(ev)")
        self.assertLess(i, src.index("_touch_wanted(f.touchID)"))
        self.assertLess(src.index("_touch_wanted(f.touchID)"), src.index("self.cc5.handle_event("))


if __name__ == "__main__":
    unittest.main()
