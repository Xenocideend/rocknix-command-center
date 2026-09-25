"""The YouTube App's Firefox profile files (browser.TV_PROFILE_FILES).

Device, 24 Sep: Firefox's tab strip and address bar took ~170 px of the
YouTube App's 940 px window, so YouTube's 16:9 TV interface shrank into a
pillarboxed column. tvprofile-userChrome.css hides them. The profile files
used to need a manual copy on every deploy; browser.Browser now installs them
before each launch, for the TV profile only.
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import browser  # noqa: E402
import web_tiles  # noqa: E402


def _prefs(path):
    with open(path, encoding="utf-8") as f:
        return dict(re.findall(r'user_pref\("([^"]+)",\s*(.+?)\);', f.read()))


class TestShippedFiles(unittest.TestCase):
    def test_sources_exist(self):
        for rel, src in browser.TV_PROFILE_FILES.items():
            self.assertTrue(os.path.isfile(src), (rel, src))

    def test_user_js_and_userchrome_are_the_installed_pair(self):
        self.assertEqual(set(browser.TV_PROFILE_FILES), {"user.js", "chrome/userChrome.css"})

    def test_userchrome_hides_the_toolbars(self):
        with open(browser.TV_PROFILE_FILES["chrome/userChrome.css"], encoding="utf-8") as f:
            css = f.read()
        self.assertRegex(css, r"#navigator-toolbox\s*\{[^}]*visibility:\s*collapse\s*!important")

    def test_user_js_enables_userchrome(self):
        prefs = _prefs(browser.TV_PROFILE_FILES["user.js"])
        self.assertEqual(prefs.get("toolkit.legacyUserProfileCustomizations.stylesheets"), "true")

    def test_no_kiosk_flag(self):
        """--kiosk makes the window fullscreen, which can cover rp5deck's strip."""
        session = web_tiles.YtAppSession(mock.Mock(), submit=lambda *a, **k: None)
        self.assertNotIn("--kiosk", session.browser.build_command())


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src_dir = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src_dir)
        self.profile = os.path.join(self.tmp.name, "profile")
        self.files = {}
        for rel, body in (("user.js", b'user_pref("a", 1);\n'), ("chrome/userChrome.css", b"#x{}\n")):
            src = os.path.join(self.src_dir, rel.replace("/", "_"))
            with open(src, "wb") as f:
                f.write(body)
            self.files[rel] = src

    def _browser(self):
        return browser.Browser(profile_dir=self.profile, profile_files=self.files)

    def test_installs_missing_files_and_creates_chrome_dir(self):
        written = self._browser().install_profile_files()
        self.assertEqual(len(written), 2)
        with open(os.path.join(self.profile, "chrome", "userChrome.css"), "rb") as f:
            self.assertEqual(f.read(), b"#x{}\n")

    def test_identical_files_are_not_rewritten(self):
        b = self._browser()
        b.install_profile_files()
        self.assertEqual(b.install_profile_files(), [])

    def test_a_changed_profile_file_is_replaced(self):
        b = self._browser()
        b.install_profile_files()
        with open(os.path.join(self.profile, "user.js"), "wb") as f:
            f.write(b"stale")
        self.assertEqual(b.install_profile_files(), [os.path.join(self.profile, "user.js")])

    def test_a_missing_source_is_skipped_not_fatal(self):
        self.files["user.js"] = os.path.join(self.src_dir, "nope")
        written = self._browser().install_profile_files()
        self.assertEqual([os.path.basename(w) for w in written], ["userChrome.css"])

    def test_open_installs_before_launching_firefox(self):
        b = self._browser()
        seen = {}

        def fake_popen(*a, **k):
            seen["installed"] = os.path.isfile(os.path.join(self.profile, "chrome", "userChrome.css"))
            raise OSError("no firefox in tests")

        with mock.patch.object(browser.subprocess, "Popen", side_effect=fake_popen):
            self.assertFalse(b.open())
        self.assertEqual(seen, {"installed": True})


class TestOnlyTheTvProfile(unittest.TestCase):
    def test_yt_app_session_uses_the_tv_files(self):
        session = web_tiles.YtAppSession(mock.Mock(), submit=lambda *a, **k: None)
        self.assertEqual(session.browser.profile_files, browser.TV_PROFILE_FILES)
        self.assertEqual(session.browser.profile_dir, browser.TV_PROFILE_DIR)

    def test_main_browser_installs_nothing(self):
        self.assertEqual(browser.Browser().profile_files, {})


if __name__ == "__main__":
    unittest.main()
