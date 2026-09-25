#!/usr/bin/env python3
"""device_name(): device-tree model, then DMI product_name, then "Handheld"."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import device  # noqa: E402


class TestDeviceName(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.model = os.path.join(self.d, "model")
        self.dmi = os.path.join(self.d, "product_name")

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, path, data):
        with open(path, "wb") as f:
            f.write(data)

    def name(self):
        return device.device_name(self.model, self.dmi)

    def test_model_with_trailing_nul(self):
        # exactly what the RP5 exposes: the string plus a terminating NUL
        self.write(self.model, b"Retroid Pocket 5\x00")
        self.write(self.dmi, b"Should Not Win\n")
        self.assertEqual(self.name(), "Retroid Pocket 5")

    def test_model_whitespace_is_trimmed(self):
        self.write(self.model, b"  Retroid   Pocket 5 \n\x00")
        self.assertEqual(self.name(), "Retroid Pocket 5")

    def test_model_missing_falls_back_to_dmi(self):
        self.write(self.dmi, b"ROG Ally RC71L\n")
        self.assertEqual(self.name(), "ROG Ally RC71L")

    def test_empty_model_falls_back_to_dmi(self):
        self.write(self.model, b"\x00")
        self.write(self.dmi, b"Legion Go\n")
        self.assertEqual(self.name(), "Legion Go")

    def test_dmi_placeholder_is_not_a_name(self):
        self.write(self.dmi, b"To be filled by O.E.M.\n")
        self.assertEqual(self.name(), "Handheld")

    def test_both_missing(self):
        self.assertEqual(self.name(), "Handheld")

    def test_title(self):
        self.assertEqual(device.app_title("Retroid Pocket 5"), "Retroid Pocket 5 Command Center")


if __name__ == "__main__":
    unittest.main()
