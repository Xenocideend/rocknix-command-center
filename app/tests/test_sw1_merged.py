#!/usr/bin/env python3
"""SW1 (screen swap: main.py, screens.py, 094-rp5deck), against the REAL merged files.

I2 (24 Sep) applied patches/SW1-*.patch to the shipped files. The behaviour
cases in tests/sw1_patched_cases.py used to run only inside a patched temp copy
(tests/test_sw1_patches.py, retired); this module runs every TestCase
DEFINED in that file (not the ones it imports from test_companion etc.,
which run in their own modules) against the working tree."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import sw1_patched_cases as cases  # noqa: E402


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in sorted(dir(cases)):
        obj = getattr(cases, name)
        if (isinstance(obj, type) and issubclass(obj, unittest.TestCase)
                and obj.__module__ == cases.__name__):
            suite.addTests(loader.loadTestsFromTestCase(obj))
    return suite


if __name__ == "__main__":
    unittest.main()
