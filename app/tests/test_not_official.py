"""The app and its documents say this is not an official ROCKNIX project and that problems go to the maintainer, not the
ROCKNIX team (owner, 2 Oct)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ROOT = os.path.dirname(APP)                       # bottom-screen-app/ here, the repo's top folder in the public tree
sys.path.insert(0, APP)
import settings_view  # noqa: E402


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestAbout(unittest.TestCase):
    def test_the_about_credits_say_it_is_not_an_official_rocknix_project(self):
        text = " ".join(settings_view.ABOUT_CREDITS)
        self.assertIn("Not an official ROCKNIX project", text)
        self.assertIn("didnt make it, review it or endorse it", text)

    def test_the_about_credits_say_who_to_tell_instead(self):
        self.assertIn("tell Xenocideend and not the ROCKNIX team", " ".join(settings_view.ABOUT_CREDITS))

    def test_each_credit_line_is_short_enough_for_one_row(self):
        for line in settings_view.ABOUT_CREDITS:
            self.assertLessEqual(len(line), 96, line)


class TestDocuments(unittest.TestCase):
    def check(self, path):
        head = slurp(path)[:900]
        self.assertIn("Not an official ROCKNIX project", head, path)
        self.assertIn("didnt make this, review it or endorse it", head, path)
        self.assertIn("and not them", head, path)

    def test_the_guides_start_with_the_notice(self):
        for name in ("COMMAND-CENTER-GUIDE.md", "TESTER-GUIDE.md"):
            self.check(os.path.join(ROOT, "docs", name))

    def test_the_public_readme_and_quick_install_start_with_it_when_they_are_here(self):
        for rel in ("README.md", os.path.join("docs", "QUICK-INSTALL.md")):
            p = os.path.join(ROOT, rel)
            if os.path.exists(p):
                self.check(p)


if __name__ == "__main__":
    unittest.main()
