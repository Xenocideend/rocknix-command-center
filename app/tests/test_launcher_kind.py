"""command-center-app (the launcher) on the two kinds of handheld, its real functions cut out and run under bash: on the RP5 it
still pins ROCKNIX's dual-screen flag off and applies the add-on's touch settings, on a handheld with two built-in panels it
touches neither (ROCKNIX's second-screen handling needs the flag, and the touch names are the RP5's)."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_builtin_layout as tb  # noqa: E402
import test_sw1_092 as sw1  # noqa: E402

APP = os.path.dirname(HERE)
LAUNCHER = os.path.join(APP, "command-center-app")


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def pieces():
    s = slurp(LAUNCHER)
    detect = re.search(r"(KIND=addon\ngrep -q [^\n]*KIND=builtin\n)", s).group(1)
    pin = re.search(r"(pin_dual_flag\(\) \{\n.*?\n\}\n)", s, re.S).group(1)
    touch = re.search(r"(apply_touch_config\(\) \{\n.*?\n\}\n)", s, re.S).group(1)
    return detect, pin, touch


@unittest.skipUnless(tb.BASH, "needs bash (Linux or WSL)")
class Run(sw1.TmpDir):
    def setUp(self):
        self.t = self.mk()
        self.flag = os.path.join(self.t, "flag")
        self.profile = os.path.join(self.t, "device-profile.env")
        self.calls = os.path.join(self.t, "calls")

    def write(self, path, text):
        with open(path, "w", newline="\n") as f:
            f.write(text)

    def run_bash(self, body, profile=None, flag=None):
        if profile is not None:
            self.write(self.profile, profile)
        if flag is not None:
            self.write(self.flag, flag)
        detect, pin, touch = pieces()
        q = lambda p: sw1.sh_quote(sw1.to_sh_path(p))                   # noqa: E731
        script = ("RP5DECK_HOME=%s\nRP5DECK_PROFILE_FILE=%s\nFLAG_FILE=%s\nCALLS=%s\nEXTERNAL=DP-1\n"
                  'TOUCH_ADDON="a"\nTOUCH_BUILTIN="b"\nTOUCH_ADDON_MATRIX="0 1 0 -1 0 1"\n'
                  'log() { echo "LOG $*"; }\nswaymsg() { echo "SWAYMSG $*" >> "$CALLS"; return 0; }\n'
                  "%s\n%s\n%s\n%s\n" % (q(self.t), q(self.profile), q(self.flag), q(self.calls), detect, pin, touch, body))
        path = os.path.join(self.t, "run.sh")
        self.write(path, script)
        return tb_run(path)

    def flag_text(self):
        return slurp(self.flag)

    def calls_text(self):
        return slurp(self.calls) if os.path.exists(self.calls) else ""

    # ---- which kind -----------------------------------------------------------------------------------------------
    def kind(self, profile):
        r = self.run_bash('echo "KIND=$KIND"', profile=profile, flag="")
        return re.search(r"KIND=(\w+)", r.stdout).group(1)

    def test_a_built_in_panels_profile_file_means_builtin(self):
        self.assertEqual(self.kind("BOTTOM_OUTPUT='DSI-1'\nTOP_OUTPUT='DSI-2'\n"), "builtin")

    def test_the_add_on_profile_file_means_addon(self):
        self.assertEqual(self.kind("INTERNAL='DSI-1'\nEXTERNAL='DP-1'\n"), "addon")

    def test_no_profile_file_means_addon_the_rp5s_behaviour(self):
        r = self.run_bash('echo "KIND=$KIND"', flag="")
        self.assertIn("KIND=addon", r.stdout)

    def test_an_empty_or_odd_profile_file_means_addon(self):
        for text in ("", "junk\n", "BOTTOM_OUTPUT=DSI-1\n", "# BOTTOM_OUTPUT='DSI-1'\n"):
            self.assertEqual(self.kind(text), "addon", repr(text))

    # ---- the flag pin ---------------------------------------------------------------------------------------------
    def test_on_the_rp5_the_flag_is_pinned_off_as_before(self):
        r = self.run_bash("pin_dual_flag", profile="INTERNAL='DSI-1'\n", flag="DEVICE_HAS_DUAL_SCREEN=true\n")
        self.assertEqual(self.flag_text(), "DEVICE_HAS_DUAL_SCREEN=false\n")
        self.assertIn("pinned DEVICE_HAS_DUAL_SCREEN=false", r.stdout)

    def test_on_a_built_in_panels_handheld_the_flag_is_never_touched(self):
        r = self.run_bash("pin_dual_flag", profile="BOTTOM_OUTPUT='DSI-1'\n", flag="DEVICE_HAS_DUAL_SCREEN=true\n")
        self.assertEqual(self.flag_text(), "DEVICE_HAS_DUAL_SCREEN=true\n")
        self.assertIn("left to ROCKNIX", r.stdout)
        self.assertNotIn("pinned", r.stdout)

    def test_an_rp5_flag_that_is_already_false_is_left_alone(self):
        self.run_bash("pin_dual_flag", profile="INTERNAL='DSI-1'\n", flag="DEVICE_HAS_DUAL_SCREEN=false\n")
        self.assertEqual(self.flag_text(), "DEVICE_HAS_DUAL_SCREEN=false\n")

    # ---- the touch settings -------------------------------------------------------------------------------------
    def test_on_the_rp5_the_touch_settings_are_applied_as_before(self):
        self.run_bash("apply_touch_config", profile="INTERNAL='DSI-1'\n", flag="")
        c = self.calls_text()
        self.assertIn('input "b" map_to_output DSI-1', c)
        self.assertIn('input "a" calibration_matrix 0 1 0 -1 0 1', c)
        self.assertIn('input "a" map_to_output DP-1', c)

    def test_on_a_built_in_panels_handheld_sway_is_not_sent_the_rp5s_touch_settings(self):
        self.run_bash("apply_touch_config", profile="BOTTOM_OUTPUT='DSI-1'\n", flag="")
        self.assertEqual(self.calls_text(), "")


def tb_run(path):
    import subprocess
    p = sw1.to_sh_path(path)
    if sw1.LINUX:
        return subprocess.run(["bash", p], capture_output=True, text=True, timeout=60)
    return subprocess.run([sw1.wsl(), "-e", "bash", p], capture_output=True, text=True, timeout=60)


class TestStatic(unittest.TestCase):
    def test_the_pin_and_the_touch_settings_are_guarded_by_the_kind(self):
        detect, pin, touch = pieces()
        self.assertIn('[ "$KIND" = addon ] ||', pin)
        self.assertIn('[ "$KIND" = addon ] || return 0', touch)

    def test_the_kind_is_known_before_the_pin_runs(self):
        s = slurp(LAUNCHER)
        self.assertLess(s.index("KIND=addon\n"), s.index("\npin_dual_flag\n"))

    def test_the_kind_is_logged_at_start(self):
        self.assertIn("device kind: $KIND", slurp(LAUNCHER))


if __name__ == "__main__":
    unittest.main()
