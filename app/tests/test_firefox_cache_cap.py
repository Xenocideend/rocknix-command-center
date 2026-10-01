"""Both Firefox profiles (Browser and Discord, the YouTube App's own) cap their disk cache. Left alone Firefox sizes it
from the free space and each profile on the device grew to about 150 MB of flash writes."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIREFOX = os.path.join(os.path.dirname(HERE), "firefox")
FILES = ("user.js", "tvprofile-user.js")
MAX_KIB = 128 * 1024


def prefs(name):
    with open(os.path.join(FIREFOX, name), encoding="utf-8") as f:
        return dict(re.findall(r'^user_pref\("([^"]+)",\s*(.+?)\);', f.read(), re.M))


class TestDiskCacheCap(unittest.TestCase):
    def test_smart_sizing_is_off_so_the_capacity_counts(self):
        for name in FILES:
            self.assertEqual(prefs(name).get("browser.cache.disk.smart_size.enabled"), "false", name)

    def test_the_capacity_is_set_and_modest(self):
        for name in FILES:
            v = prefs(name).get("browser.cache.disk.capacity")
            self.assertIsNotNone(v, name)
            self.assertTrue(0 < int(v) <= MAX_KIB, (name, v))

    def test_each_pref_is_set_once(self):
        for name in FILES:
            with open(os.path.join(FIREFOX, name), encoding="utf-8") as f:
                text = f.read()
            self.assertEqual(len(re.findall(r'^user_pref\("browser\.cache\.disk\.capacity"', text, re.M)), 1, name)


if __name__ == "__main__":
    unittest.main()
