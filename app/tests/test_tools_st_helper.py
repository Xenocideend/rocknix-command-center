"""tools/st.py, the device test helper that reads state.json and taps widgets: dotted paths that mix flat and
nested keys, and the tap origin on the real surface (not the layout canvas, which is 9874 px wide in BAR)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import st  # noqa: E402

STATE = {
    "targets": {"home.mixer": [30, 420, 348, 300], "bar.slider.track": [228, 1142, 70]},
    "volume": {"master": {"volume": 0.85}},
    "surface": {"size": [1920, 1080], "surface_size": [1920, 1080]},
    "mode": "FULL",
    "tabs": {"busy": False},
}


class Resolve(unittest.TestCase):
    def test_a_flat_key_with_dots_inside_a_dict(self):
        self.assertEqual(st.resolve(STATE, "targets.home.mixer"), [30, 420, 348, 300])
        self.assertEqual(st.resolve(STATE, "targets.bar.slider.track"), [228, 1142, 70])

    def test_nested_keys(self):
        self.assertEqual(st.resolve(STATE, "volume.master.volume"), 0.85)
        self.assertIs(st.resolve(STATE, "tabs.busy"), False)
        self.assertEqual(st.resolve(STATE, "surface.size"), [1920, 1080])

    def test_a_single_key_and_a_missing_one(self):
        self.assertEqual(st.resolve(STATE, "mode"), "FULL")
        self.assertIsNone(st.resolve(STATE, "nonexistent.thing"))
        self.assertEqual(st.resolve(STATE, "targets"), STATE["targets"])


class Origin(unittest.TestCase):
    def setUp(self):
        real = st.get_dsi_y
        st.get_dsi_y = lambda: 0
        self.addCleanup(setattr, st, "get_dsi_y", real)

    def test_full_surface(self):
        self.assertEqual(st.origin(STATE), 0)

    def test_the_bar_uses_the_real_surface_not_the_layout_canvas(self):
        bar = {"surface": {"size": [9874, 720], "surface_size": [1920, 140]}}
        self.assertEqual(st.origin(bar), 940)

    def test_an_older_state_without_surface_size_falls_back(self):
        self.assertEqual(st.origin({"surface": {"size": [1920, 1080]}}), 0)


if __name__ == "__main__":
    unittest.main()
