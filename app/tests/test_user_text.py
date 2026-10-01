"""Owner, 26 Sep: everything the Command Center shows must read plainly - no task codes,
raw setting keys, program names in brackets or "85/80" pairs. tools/user_text_scan.py
walks the real UI; this keeps it at zero."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import user_text_scan as scan  # noqa: E402


class UserText(unittest.TestCase):
    def test_nothing_on_screen_reads_like_developer_notes(self):
        rows = scan.collect()
        self.assertGreater(len(rows), 300, "the scan saw too little of the UI to mean anything")
        bad = sorted(t for _k, _n, t in rows if scan.DEV.search(t) and not scan.OK_PAIRS.match(t))
        self.assertEqual(bad, [])

    def test_the_scan_catches_what_it_is_for(self):
        for text in ("85/80", "OFF (key.dpad.events=0)", "CC5 overlay", "input_sense execute_kill",
                     "restarted by 092"):
            self.assertTrue(scan.DEV.search(text), text)
        for text in ("85%", "Page 1 of 2", "Back button", "Quit game"):
            self.assertFalse(scan.DEV.search(text) and not scan.OK_PAIRS.match(text), text)


if __name__ == "__main__":
    unittest.main()
