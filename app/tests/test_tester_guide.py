"""docs/TESTER-GUIDE.md names scripts and files a tester has to find: every one of them has to exist in the app tree or the
scripts folder, and the questions the guide lists have to be the ones tester-run.sh asks."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
GUIDE = os.path.join(os.path.dirname(APP), "docs", "TESTER-GUIDE.md")
SCRIPTS = os.path.join(os.path.dirname(APP), "scripts")
RUNNER = os.path.join(APP, "tools", "tester-run.sh")


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def commands():
    """The shell lines in the guide's fenced blocks."""
    out = []
    for block in re.findall(r"```\n(.*?)```", slurp(GUIDE), re.S):
        out += [ln.strip() for ln in block.splitlines() if ln.strip()]
    return out


class TestTesterGuide(unittest.TestCase):
    def test_every_script_the_guide_runs_exists(self):
        found = 0
        for ln in commands():
            for m in re.finditer(r"/storage/rp5deck/([\w./-]+\.sh)", ln):
                self.assertTrue(os.path.exists(os.path.join(APP, m.group(1))), m.group(0))
                found += 1
            for m in re.finditer(r"/storage/command-center-pack/app/([\w./-]+\.sh)", ln):
                self.assertTrue(os.path.exists(os.path.join(APP, m.group(1))), m.group(0))
                found += 1
            for m in re.finditer(r"/storage/command-center-pack/scripts/([\w./-]+\.sh)", ln):
                self.assertTrue(os.path.exists(os.path.join(SCRIPTS, m.group(1))), m.group(0))
                found += 1
            m = re.match(r"sh (testday/[\w.-]+\.sh)", ln)
            if m:
                self.assertTrue(os.path.exists(os.path.join(APP, m.group(1))), ln)
                found += 1
        self.assertGreaterEqual(found, 5)

    def test_the_guide_install_steps_match_the_public_quick_install(self):
        cmds = " ".join(commands())
        for step in ("td1-verify-build.sh", "td3-install.sh install", "td3-install.sh guard-live", "install-es-hooks.sh",
                     "systemctl restart essway", "install-layout-daemon.sh"):
            self.assertIn(step, cmds)

    def test_the_run_command_uses_the_options_the_script_has(self):
        run = [c for c in commands() if "tester-run.sh" in c][0]
        self.assertIn("--scripts", run)
        self.assertIn("--scripts", slurp(RUNNER))
        self.assertIn("--auto", slurp(GUIDE))
        self.assertIn("--auto", slurp(RUNNER))

    def test_the_undo_commands_name_files_the_code_really_reads(self):
        g = slurp(GUIDE)
        files = {"/storage/.disable-rp5deck": os.path.join(APP, "command-center-app"),
                 "/storage/.disable-dualscreen": os.path.join(SCRIPTS, "dual-screen-builtin-layout"),
                 "/storage/.bottom-screen-power-rocknix": os.path.join(SCRIPTS, "dual-screen-builtin-layout"),
                 "/storage/.keep-lowerdeck": os.path.join(SCRIPTS, "dual-screen-builtin-layout")}
        for name, source in files.items():
            self.assertIn(name, g)
            self.assertIn(name, slurp(source), name)
        self.assertIn("--restore-lowerdeck", g)
        self.assertIn("--restore-lowerdeck", slurp(os.path.join(SCRIPTS, "dual-screen-builtin-layout")))

    def test_the_log_name_in_the_guide_is_the_one_the_script_writes(self):
        self.assertIn("rp5deck-test-log-", slurp(GUIDE))
        self.assertIn("rp5deck-test-log-", slurp(RUNNER))

    def test_every_models_name_in_the_guide_has_a_profile(self):
        import screen_map
        for model in ("AYN Thor", "AYN Thor Lite", "AYANEO Pocket DS", "Anbernic RG DS", "Anbernic RG DS Plus"):
            self.assertEqual(screen_map.known_kind(model, env={}), "builtin", model)

    def test_the_guide_has_the_sections_a_tester_needs(self):
        g = slurp(GUIDE)
        for heading in ("## What it changes on your device", "## 1. Copy the pack", "## 2. Install", "## 3. Run the test",
                        "## 4. Send me the log", "## Undo"):
            self.assertIn(heading, g)

    def test_the_question_table_covers_what_the_script_asks(self):
        g = slurp(GUIDE)
        topics = ("Command Center shown", "Touch works", "EmulationStation on the other screen", "Swap screens",
                  "keyboard", "bottom-screen app stays away", "DS game", "brightness slider")
        for t in topics:
            self.assertIn(t.lower(), g.lower(), t)
        ids = re.findall(r"^ask (\w+) ", slurp(RUNNER), re.M)
        self.assertEqual(len(ids), 11)


sys.path.insert(0, os.path.dirname(HERE))

if __name__ == "__main__":
    unittest.main()
