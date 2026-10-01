"""The version number and its patch notes (version.py, CHANGELOG.md)."""
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import version  # noqa: E402


class Version(unittest.TestCase):
    def test_version_is_major_minor_patch(self):
        self.assertRegex(version.VERSION, r"^\d+\.\d+\.\d+$")

    def test_newest_changelog_section_is_this_version(self):
        rel = version.releases()
        self.assertTrue(rel)
        self.assertEqual(rel[0][0], version.VERSION)
        self.assertTrue(version.whats_new(), "the current version has no summary lines")

    def test_label_has_the_date(self):
        self.assertEqual(version.label(), "%s (%s)" % (version.VERSION, version.release_date()))

    def test_a_wrapped_bullet_is_one_line_and_details_are_left_out(self):
        text = ("## 9.9.9 - 1 Jan 2030\n\n- First line that wraps\n  onto a second line.\n"
                "- Second.\n\nDetails:\n\n- **Not in the summary**\n")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(version.whats_new("9.9.9", f.name),
                         ["First line that wraps onto a second line.", "Second."])

    def test_versions_go_down(self):
        nums = [tuple(int(x) for x in v.split(".")) for v, _d, _s in version.releases()]
        self.assertEqual(nums, sorted(nums, reverse=True))


if __name__ == "__main__":
    unittest.main()
