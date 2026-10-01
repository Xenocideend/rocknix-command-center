"""tools/tester-run.sh, the guided test a tester runs on another handheld: run verbatim under dash with stand-ins for the probe,
the backlights and the logs. It has to write one log with the sections and the answers, leave out game names, serials and
addresses, change nothing but its own log (the layout helper only after a yes), and never hang on a closed input."""
import os
import re
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_sw1_092 as sw1  # noqa: E402

APP = os.path.dirname(HERE)
SCRIPT = os.path.join(APP, "tools", "tester-run.sh")

# made-up values, joined here so no address-shaped text sits in the source
MAC = ":".join(["aa", "bb", "cc", "dd", "ee", "ff"])
IP = ".".join(["192", "168", "1", "5"])
IP_IN_NOTE = ".".join(["10", "1", "2", "3"])
PROBE_STUB = ("#!/bin/sh\necho '== stub probe =='\necho 'model: Test Handheld'\necho 'battery-xx %s'\n" % MAC
              + "echo 'Serial Number: SN123|rest'\necho 'seen from %s'\necho \"probe-out=$PROBE_OUT\"\n" % IP)
APP_LOG = ("2026-10-01 10:00:00 INFO rp5deck: running\n"
           "2026-10-01 10:00:01 WARNING rp5deck.name_guard: name guard: bad character in Secret Game\n"
           "2026-10-01 10:00:02 INFO rp5deck: game-start Secret Game\n"
           "2026-10-01 10:00:02 INFO rp5deck: ES game exit Secret Game\n"
           "2026-10-01 10:00:02 INFO rp5deck: reading gamelist Hidden Title\n"
           "2026-10-01 10:00:02 INFO rp5deck: system='nes' rom='/storage/roms/nes/Secret Game.zip' name='Secret Game' ok\n"
           "2026-10-01 10:00:03 INFO rp5deck: touch devices {11: 'a (seat0)', 12: 'a (seat1)'}: using [11]\n"
           "2026-10-01 10:00:04 INFO rp5deck.manuals: no manual found for /storage/roms/nes/Secret Game.zip\n"
           "2026-10-01 10:00:05 INFO rp5deck: loading /storage/roms/snes/Another Game.sfc now\n")


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(sw1.DASH, "dash needed")
class Run(sw1.TmpDir):
    def setUp(self):
        t = self.mk()
        self.t = t
        self.app = os.path.join(t, "rp5deck")
        self.logs = os.path.join(t, "logs")
        self.bl = os.path.join(t, "backlight")
        self.auto = os.path.join(t, "autostart")
        for d in (os.path.join(self.app, "tools"), os.path.join(self.app, "log"), self.logs, self.auto,
                  os.path.join(t, "bin"), os.path.join(self.bl, "a"), os.path.join(self.bl, "b")):
            os.makedirs(d)
        shutil.copy(SCRIPT, os.path.join(self.app, "tools", "tester-run.sh"))
        for f in ("screen_map.py", "device.py"):
            shutil.copy(os.path.join(APP, f), self.app)
        self.write(os.path.join(self.app, "version.py"), 'VERSION = "9.9.9"\n')
        self.write(os.path.join(self.app, "tools", "device-probe.sh"), PROBE_STUB)
        self.write(os.path.join(self.app, "log", "rp5deck.log"), APP_LOG)
        for n, v in (("a", 100), ("b", 100)):
            self.write(os.path.join(self.bl, n, "brightness"), "%d\n" % v)
            self.write(os.path.join(self.bl, n, "max_brightness"), "255\n")
        for name, body in {"sleep": "#!/bin/sh\nexit 0\n", "uptime": "#!/bin/sh\necho ' up 1 min'\n",
                           "dmesg": "#!/bin/sh\necho 'panel dsi probe ok'\necho 'unrelated line'\n"}.items():
            p = os.path.join(t, "bin", name)
            self.write(p, body)
            os.chmod(p, 0o755)
        self.hook_count = os.path.join(t, "hook.n")

    def write(self, path, text):
        with open(path, "w", newline="\n", encoding="utf-8") as f:
            f.write(text)

    def run_tester(self, args="", stdin="", hook=None, env_extra=""):
        T = sw1.to_sh_path(self.t)
        env = ("TESTER_LOG_DIR=%s RP5DECK_HOME=%s TESTER_BACKLIGHT_DIR=%s INSTALL_AUTOSTART=%s TMPDIR=%s"
               % tuple(sw1.sh_quote(sw1.to_sh_path(x)) for x in (self.logs, self.app, self.bl, self.auto, self.t))
               + " " + env_extra)
        if hook:
            self.write(os.path.join(self.t, "hook.sh"), hook)
            env += " TESTER_HOOK=%s" % sw1.sh_quote(T + "/hook.sh")
        self.write(os.path.join(self.t, "stdin.txt"), stdin)
        cmd = "chmod +x %s/bin/* %s/rp5deck/tools/*.sh; export %s PATH=%s/bin:$PATH; sh %s/rp5deck/tools/tester-run.sh %s < %s/stdin.txt" % (
            T, T, env, T, T, args, T)
        return sw1.run_dash(cmd, timeout=120)

    def log(self):
        names = [n for n in os.listdir(self.logs) if n.startswith("rp5deck-test-log-")]
        self.assertEqual(len(names), 1, names)
        return slurp(os.path.join(self.logs, names[0]))

    # ---- --auto ------------------------------------------------------------------------------------------------
    def test_an_auto_run_writes_every_section_and_asks_nothing(self):
        r = self.run_tester("--auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.log()
        for i in range(1, 9):
            self.assertRegex(log, r"== %d\. " % i)
        self.assertIn("tester log, auto run", log)
        self.assertEqual(log.count("= skipped (--auto)"), 11)
        self.assertNotIn("Press Enter", r.stdout)

    def test_an_auto_run_installs_nothing_even_with_the_scripts_at_hand(self):
        sd = os.path.join(self.t, "scripts")
        os.makedirs(sd)
        self.write(os.path.join(sd, "install-layout-daemon.sh"), "#!/bin/sh\necho INSTALLED > %s\n" % sw1.to_sh_path(os.path.join(self.t, "installed")))
        self.run_tester("--auto --scripts %s" % sw1.sh_quote(sw1.to_sh_path(sd)))
        self.assertFalse(os.path.exists(os.path.join(self.t, "installed")))
        self.assertIn("not installed in this run", self.log())

    def test_the_probe_is_told_not_to_leave_its_own_file_behind(self):
        self.run_tester("--auto")
        self.assertEqual(self.log().count("probe-out=/dev/null"), 2)        # before and after

    def test_only_display_related_kernel_lines_are_kept(self):
        self.run_tester("--auto")
        log = self.log()
        self.assertIn("panel dsi probe ok", log)
        self.assertNotIn("unrelated line", log)

    def test_the_log_path_and_how_to_send_it_are_printed(self):
        r = self.run_tester("--auto")
        self.assertRegex(r.stdout, r"The log is: \S+rp5deck-test-log-\d{8}-\d{6}\.txt")
        self.assertIn("scp root@", r.stdout)

    # ---- redaction ---------------------------------------------------------------------------------------------
    def test_serials_macs_and_addresses_are_removed(self):
        self.run_tester("--auto")
        log = self.log()
        self.assertNotIn(MAC, log)
        self.assertNotIn("SN123", log)
        self.assertNotIn(IP, log)
        self.assertIn("xx:xx:xx:xx:xx:xx", log)
        self.assertIn("serial removed", log)
        self.assertIn("x.x.x.x", log)

    def test_game_names_and_rom_paths_are_left_out_of_the_app_log(self):
        self.run_tester("--auto")
        log = self.log()
        self.assertNotIn("Secret Game", log)
        self.assertNotIn("Another Game", log)
        self.assertNotIn("Hidden Title", log)
        self.assertNotIn("name guard", log)
        self.assertNotIn("game-start", log)
        self.assertIn("/storage/roms/...", log)
        self.assertIn("touch devices", log)                 # what a tester log is for survives

    def test_a_note_with_an_address_in_it_is_cleaned_too(self):
        stdin = "y\nmy phone is %s\n" % IP_IN_NOTE + "s\n\n" * 10
        self.run_tester("", stdin=stdin)
        self.assertNotIn(IP_IN_NOTE, self.log())

    # ---- the guided run ---------------------------------------------------------------------------------------
    HOOK = ('n=$(cat "$0.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$0.n"\n'
            'case "$n" in\n'
            '  1) echo 20 > "$BLDIR/b/brightness" ;;\n'
            '  2) echo 30 > "$BLDIR/a/brightness" ;;\n'
            'esac\n')

    def guided(self, stdin=None):
        T = sw1.to_sh_path(self.t)
        answers = stdin if stdin is not None else (
            "y\nlooks fine\n" "n\ncorner dead\n" "s\n\n" "y\n\n" "y\n\n" "x\ny\n\n" "n\nlowerdeck came up\n" "s\n\n"
            "y\n\n" "y\n\n" "s\n\n")
        return self.run_tester("", stdin=answers, hook=self.HOOK, env_extra="BLDIR=%s" % sw1.sh_quote(sw1.to_sh_path(self.bl)))

    def test_the_answers_and_notes_are_logged_with_their_ids(self):
        r = self.guided()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.log()
        self.assertIn("ANSWER command_center_visible = yes | note: looks fine", log)
        self.assertIn("ANSWER touch_works = no | note: corner dead", log)
        self.assertIn("ANSWER touch_position = skip | note: ", log)
        self.assertIn("ANSWER keyboard = yes", log)                    # an invalid first answer is asked again
        self.assertIn("ANSWER game_lowerdeck = no | note: lowerdeck came up", log)
        self.assertEqual(len(re.findall(r"^ANSWER ", log, re.M)), 11)

    def test_the_backlight_that_each_slider_moved_is_named(self):
        self.guided()
        log = self.log()
        self.assertIn("BACKLIGHT MOVED BY THE TOP SLIDER: b", log)
        self.assertIn("BACKLIGHT MOVED BY THE BOTTOM SLIDER: a", log)

    def test_a_slider_that_moved_no_backlight_says_so(self):
        r = self.run_tester("", stdin="s\n\n" * 11, hook="true\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("BACKLIGHT MOVED BY THE TOP SLIDER: nothing changed", self.log())

    def test_a_closed_input_does_not_hang_and_counts_as_skipped(self):
        r = self.run_tester("", stdin="", hook="true\n")
        self.assertEqual(r.returncode, 0)
        log = self.log()
        self.assertEqual(len(re.findall(r"^ANSWER \S+ = skip", log, re.M)), 11)

    def test_it_prompts_in_plain_words(self):
        # no hook here: the real Enter prompts, so the stand-in input has an Enter line for each
        stdin = "s\n\n" * 8 + "\n" + "s\n\n" + "\n" + "s\n\n" + "\n" + "s\n\n"
        r = self.run_tester("", stdin=stdin)
        for needle in ("Is the Command Center", "Tap a tile", "Top screen brightness", "Bottom screen brightness",
                       "[y]es / [n]o / [s]kip", "Press Enter when done."):
            self.assertIn(needle, r.stdout)
        self.assertEqual(len(re.findall(r"^ANSWER \S+ = skip", self.log(), re.M)), 11)

    # ---- installing the helper -------------------------------------------------------------------------------
    def with_scripts(self):
        sd = os.path.join(self.t, "scripts")
        os.makedirs(sd)
        self.write(os.path.join(sd, "install-layout-daemon.sh"),
                   "#!/bin/sh\necho stub-installer-ran\necho x > %s\n" % sw1.to_sh_path(os.path.join(self.t, "installed")))
        return sw1.sh_quote(sw1.to_sh_path(sd))

    def test_the_helper_is_installed_only_after_a_yes(self):
        sd = self.with_scripts()
        self.run_tester("--scripts %s" % sd, stdin="y\n" + "s\n\n" * 11, hook="true\n")
        self.assertTrue(os.path.exists(os.path.join(self.t, "installed")))
        self.assertIn("stub-installer-ran", self.log())

    def test_a_no_installs_nothing(self):
        sd = self.with_scripts()
        self.run_tester("--scripts %s" % sd, stdin="n\n" + "s\n\n" * 11, hook="true\n")
        self.assertFalse(os.path.exists(os.path.join(self.t, "installed")))

    def test_without_the_scripts_the_question_is_not_asked(self):
        r = self.run_tester("", stdin="s\n\n" * 11, hook="true\n")
        self.assertNotIn("Install the layout helper", r.stdout)

    # ---- the rest -----------------------------------------------------------------------------------------------
    def test_a_wrong_argument_prints_the_usage(self):
        r = self.run_tester("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stdout)

    def test_the_script_parses(self):
        r = sw1.run_dash("dash -n %s" % sw1.sh_quote(sw1.to_sh_path(SCRIPT)))
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_its_temp_folder_is_removed(self):
        self.run_tester("--auto")
        self.assertEqual([n for n in os.listdir(self.t) if n.startswith("rp5deck-tester.")], [])
        for d in (self.app, self.bl, self.auto, self.logs):          # and nothing else of the device's went with it
            self.assertTrue(os.path.isdir(d), d)


class TestStatic(unittest.TestCase):
    def code(self):
        return [ln for ln in slurp(SCRIPT).splitlines() if ln.strip() and not ln.lstrip().startswith("#")]

    def test_it_changes_nothing_on_the_device_but_its_own_files(self):
        forbidden = re.compile(r"\b(kill|pkill|killall|reboot|poweroff|systemctl|chmod|chown|set_setting|mv|cp)\b|"
                               r"swaymsg|\bsed\s+-i\b|>\s*/(sys|proc|etc|usr)")
        for ln in self.code():
            self.assertIsNone(forbidden.search(ln), ln)

    def test_the_only_removal_is_its_own_temp_folder(self):
        rms = [ln for ln in self.code() if re.search(r"\brm\b", ln)]
        self.assertEqual(len(rms), 1)
        self.assertIn('rm -rf "$TMP"', rms[0])

    def test_the_installer_runs_only_inside_the_confirmed_branch(self):
        s = slurp(SCRIPT)
        runs = [ln for ln in self.code() if "sh \"$sd/install-layout-daemon.sh\"" in ln]
        self.assertEqual(len(runs), 1)
        # the line that runs it comes right after the `if ... confirm "Install ..."; then` that guards it
        i = s.index('capture sh "$sd/install-layout-daemon.sh"')
        guard = s.rindex('confirm "Install the layout helper now?', 0, i)
        self.assertEqual(s[guard:i].count("\nfi"), 0)
        self.assertEqual(s[guard:i].count("else"), 0)

    def test_every_collected_line_goes_through_the_redaction(self):
        for ln in self.code():
            if ">> \"$LOG\"" in ln and "redact" not in ln and not ln.strip().startswith("echo \"ANSWER $id = skipped"):
                self.fail(ln)

    def test_the_questions_the_guide_lists_are_all_asked(self):
        ids = re.findall(r'^ask (\w+) ', slurp(SCRIPT), re.M)
        self.assertEqual(len(ids), 11)
        self.assertEqual(len(set(ids)), 11)


if __name__ == "__main__":
    unittest.main()
