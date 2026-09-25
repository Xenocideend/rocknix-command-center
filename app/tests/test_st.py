#!/usr/bin/env python3
"""Tests for tools/st.py swaymsg output parsing.

Verifies that DSI-1's position is read from swaymsg -t get_outputs instead of
using a hardcoded value, handling docked (y=1080), undocked (y=0), and
missing-output cases.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))

# Import before st.py to prepare the path
tools_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')

# We'll test st.py's get_dsi_y function by importing the module
import importlib.util
st_path = os.path.join(tools_path, 'st.py')
spec = importlib.util.spec_from_file_location("st_module", st_path)
st_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st_module)

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    """Load a JSON fixture."""
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


class TestStGetDsiY(unittest.TestCase):
    """Test st.py's get_dsi_y() function reading from swaymsg."""

    def test_docked_layout_reads_dsi1_at_1080(self):
        """Docked layout: get_dsi_y() returns 1080 from swaymsg output."""
        outputs = load("sway-outputs-docked-SYNTHETIC.json")
        with mock.patch('subprocess.check_output') as mock_output:
            mock_output.return_value = json.dumps(outputs)
            result = st_module.get_dsi_y()
            self.assertEqual(result, 1080)

    # test_undocked_layout_reads_dsi1_at_origin removed: depended on
    # tests/fixtures/sway-outputs-undocked-real-capture-2026-09-23.json,
    # dropped from the public release (see DROPPED-FIXTURES-list.txt). No
    # synthetic undocked-layout fixture exists to swap in.

    def test_missing_dsi1_raises_runtimeerror(self):
        """Missing DSI-1: get_dsi_y() raises RuntimeError with clear message."""
        outputs = load("sway-outputs-no-dsi1-SYNTHETIC.json")
        with mock.patch('subprocess.check_output') as mock_output:
            mock_output.return_value = json.dumps(outputs)
            with self.assertRaises(RuntimeError) as cm:
                st_module.get_dsi_y()
            self.assertIn("DSI-1", str(cm.exception))

    def test_swaymsg_failure_raises_runtimeerror(self):
        """swaymsg error: get_dsi_y() raises RuntimeError with clear message."""
        with mock.patch('subprocess.check_output') as mock_output:
            mock_output.side_effect = OSError("swaymsg not found")
            with self.assertRaises(RuntimeError) as cm:
                st_module.get_dsi_y()
            self.assertIn("swaymsg", str(cm.exception).lower())

    def test_get_dsi_y_no_caching(self):
        """get_dsi_y() calls swaymsg every time (no caching)."""
        outputs = load("sway-outputs-docked-SYNTHETIC.json")
        with mock.patch('subprocess.check_output') as mock_output:
            mock_output.return_value = json.dumps(outputs)
            result1 = st_module.get_dsi_y()
            result2 = st_module.get_dsi_y()
            self.assertEqual(result1, 1080)
            self.assertEqual(result2, 1080)
            # swaymsg should be called each time (no caching)
            self.assertEqual(mock_output.call_count, 2)


if __name__ == "__main__":
    unittest.main()
