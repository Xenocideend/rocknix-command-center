"""Batch 1 (25 Sep) small items: the gamepad stays a game control in both Firefox
profiles (YouTube's TV app navigated on the Gamepad API while a game ran)."""
import os
import re
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GamepadOff(unittest.TestCase):
    def check(self, name):
        with open(os.path.join(APP, "firefox", name), encoding="utf-8") as f:
            text = f.read()
        prefs = re.findall(r'^\s*user_pref\("dom\.gamepad\.enabled",\s*(\w+)\);', text, re.M)
        self.assertEqual(prefs, ["false"], name)

    def test_youtube_app_profile(self):
        self.check("tvprofile-user.js")

    def test_browser_profile(self):
        self.check("user.js")


if __name__ == "__main__":
    unittest.main()
