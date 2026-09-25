#!/usr/bin/env python3
"""companion_modes.py - pure logic, no fakes needed (CC2 + CC3)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import companion_modes  # noqa: E402


class TestEffectiveInGameMode(unittest.TestCase):
    def test_every_real_mode_passes_through_when_not_manual(self):
        for m in companion_modes.IN_GAME_MODES:
            if m == "manual":
                continue
            self.assertEqual(companion_modes.effective_in_game_mode(m, True), m)
            self.assertEqual(companion_modes.effective_in_game_mode(m, False), m)

    def test_manual_needs_a_manual(self):
        self.assertEqual(companion_modes.effective_in_game_mode("manual", True), "manual")
        self.assertEqual(companion_modes.effective_in_game_mode("manual", False), "art")

    def test_unknown_or_missing_falls_back_to_art(self):
        self.assertEqual(companion_modes.effective_in_game_mode("typo", True), "art")
        self.assertEqual(companion_modes.effective_in_game_mode(None, True), "art")
        self.assertEqual(companion_modes.effective_in_game_mode("", True), "art")


class TestInGameDim(unittest.TestCase):
    def test_off_is_fully_opaque(self):
        self.assertEqual(companion_modes.in_game_dim("off", 0.0), 1.0)
        self.assertEqual(companion_modes.in_game_dim("off", 0.9), 1.0)

    def test_dim_floors_at_point_six(self):
        self.assertEqual(companion_modes.in_game_dim("dim", 0.1), 0.6)
        self.assertEqual(companion_modes.in_game_dim("dim", 0.8), 0.8)   # never LOWERS a heavier setting

    def test_other_modes_pass_the_configured_value_through(self):
        for m in ("art", "hud", "clock", "slideshow", "manual"):
            self.assertEqual(companion_modes.in_game_dim(m, 0.33), 0.33)


class TestHudLines(unittest.TestCase):
    def test_empty_or_none_sample(self):
        self.assertEqual(companion_modes.hud_lines(None), [])
        self.assertEqual(companion_modes.hud_lines({}), [])

    def test_only_present_fields_are_shown_never_none(self):
        lines = companion_modes.hud_lines({"battery_percent": 87, "gpu_load_percent": None,
                                           "ram_available_mb": 2048})
        self.assertEqual(lines, ["Battery 87%", "RAM free 2048 MB"])

    def test_hottest_of_several_temps(self):
        lines = companion_modes.hud_lines({"temps_c": {"battery": 30.1, "gpu-top-thermal": 41.9}})
        self.assertEqual(lines, ["Temp 42°C"])

    def test_fastest_cpu_cluster(self):
        lines = companion_modes.hud_lines({"cpu_clusters": [
            {"cur_mhz": 1804, "max_mhz": 1804}, {"cur_mhz": 2841, "max_mhz": 2841},
            {"cur_mhz": None, "max_mhz": 2841},
        ]})
        self.assertEqual(lines, ["CPU 2841 MHz"])

    def test_a_malformed_sample_does_not_crash(self):
        lines = companion_modes.hud_lines({"cpu_clusters": "not a list", "battery_percent": 10})
        self.assertEqual(lines, ["Battery 10%"])


class TestFmtPlaytime(unittest.TestCase):
    def test_zero_or_missing_is_empty(self):
        for extra in (None, {}, {"playcount": "0"}, {"playcount": "garbage"}, {"playcount": None}):
            self.assertEqual(companion_modes.fmt_playtime(extra), "")

    def test_singular_and_plural(self):
        self.assertEqual(companion_modes.fmt_playtime({"playcount": "1"}), "Played once")
        self.assertEqual(companion_modes.fmt_playtime({"playcount": "5"}), "Played 5 times")

    def test_negative_is_empty(self):
        self.assertEqual(companion_modes.fmt_playtime({"playcount": "-3"}), "")


if __name__ == "__main__":
    unittest.main()
