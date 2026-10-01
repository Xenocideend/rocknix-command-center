"""Owner, 26 Sep: no secret stored in plain text, sign-ins hidden. Firefox never saves a
password, and the profile that holds the sign-ins (session cookies) is owner-only."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import browser  # noqa: E402


class NoSavedPasswords(unittest.TestCase):
    def test_both_profiles_turn_password_saving_off(self):
        for fn in ("user.js", "tvprofile-user.js"):
            with open(os.path.join(APP, "firefox", fn), encoding="utf-8") as f:
                s = f.read()
            for pref in ('"signon.rememberSignons", false', '"signon.autofillForms", false',
                         '"browser.formfill.enable", false'):
                self.assertIn(pref, s, fn)

    def test_policy_turns_the_password_manager_off(self):
        with open(os.path.join(APP, "firefox", "policies.json"), encoding="utf-8") as f:
            pol = json.load(f)["policies"]
        self.assertIs(pol["PasswordManagerEnabled"], False)
        self.assertIs(pol["OfferToSaveLogins"], False)


class PolicyInstalled(unittest.TestCase):
    def test_policies_are_copied_next_to_firefox(self):
        d = tempfile.mkdtemp(prefix="ffbin-")
        self.addCleanup(shutil.rmtree, d, True)
        b = browser.Browser(firefox_bin=os.path.join(d, "firefox"))
        self.assertTrue(b.install_policies())
        with open(os.path.join(d, "distribution", "policies.json"), encoding="utf-8") as f:
            self.assertIs(json.load(f)["policies"]["PasswordManagerEnabled"], False)
        self.assertFalse(b.install_policies())          # unchanged: not rewritten


class OwnerOnlyProfile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ffprof-")
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(os.path.join(self.d, "storage"))
        for rel in ("cookies.sqlite", "storage/x.sqlite"):
            open(os.path.join(self.d, rel), "w").close()

    def test_lock_profile_makes_dirs_0700_and_files_0600(self):
        b = browser.Browser(vault=False)
        b.run_profile = self.d
        calls = {}
        with mock.patch.object(browser.os, "chmod", side_effect=lambda p, m: calls.__setitem__(p, m)):
            b.lock_profile()
        self.assertEqual(calls[os.path.join(self.d, "cookies.sqlite")], 0o600)
        self.assertEqual(calls[os.path.join(self.d, "storage", "x.sqlite")], 0o600)
        self.assertEqual(calls[self.d], 0o700)
        self.assertEqual(calls[os.path.join(self.d, "storage")], 0o700)

    def test_open_locks_the_profile_and_launches_owner_only(self):
        b = browser.Browser()
        b.profile_dir = self.d
        order = []
        with mock.patch.object(b, "lock_profile", side_effect=lambda: order.append("lock")), \
             mock.patch.object(browser.subprocess, "Popen",
                               side_effect=lambda *a, **kw: order.append(("popen", kw.get("umask")))
                               or (_ for _ in ()).throw(OSError("stop here"))):
            b.open()
        self.assertEqual(order, ["lock", ("popen", 0o077)])


if __name__ == "__main__":
    unittest.main()
