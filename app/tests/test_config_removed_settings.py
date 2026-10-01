"""A config.json on a device can still hold a setting a later version removed (youtube.sign_in_enabled went with the
unused YouTube yt-dlp code). The loader has to start cleanly, keep the settings that still exist and not offer the
removed one on the Settings screen."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import config  # noqa: E402


class TestRemovedSettings(unittest.TestCase):
    def load(self, data):
        d = tempfile.mkdtemp(prefix="cfg-")
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and os.rmdir(d))
        p = os.path.join(d, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
        res = config.load(p)
        return res[0] if isinstance(res, tuple) else res

    def test_an_old_file_with_the_removed_setting_still_loads_and_keeps_the_rest(self):
        cfg = self.load({"youtube": {"sign_in_enabled": True, "tv_bar_hide_s": 7}, "steam": {"in_game_display": "art"}})
        self.assertEqual(config.get_value(cfg, ("youtube", "tv_bar_hide_s")), 7)
        self.assertEqual(config.get_value(cfg, ("steam", "in_game_display")), "art")

    def test_the_removed_setting_is_no_longer_a_field(self):
        self.assertIsNone(config.field_for(("youtube", "sign_in_enabled")))
        self.assertNotIn(("youtube", "sign_in_enabled"), config.WIRED)

    def test_the_youtube_settings_that_remain_are_the_tv_ones(self):
        keys = {f["key_path"][1] for f in config.fields_in_group("youtube")}
        self.assertEqual(keys, {"tv_bar_hide_s", "tv_swipe_natural"})


if __name__ == "__main__":
    unittest.main()
