"""The layout daemon's device-profile file, verbatim under dash: /storage/rp5deck/device-profile.env may replace the daemon's
screen, touch and audio names, and nothing else. With no file, or the RP5's own values, nothing changes."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import screen_map  # noqa: E402
from tests.test_sw1_092 import (DASH, NEW_092, LoopSim, TmpDir, lib_for, read, run_dash, sh_quote,  # noqa: E402
                                to_sh_path, world)

DEFAULTS = ("EXTERNAL='DP-1'\nINTERNAL='DSI-1'\nTOUCH_EXT='8746:1:RetroidPocket_RDS_Touchscreen'\n"
            "TOUCH_INT='0:0:generic_ft5x06_(a0)'\nAUDIO_CARD_NAME='Built-in Audio'\nPOLL=5\n")
NAMES = ("INTERNAL", "EXTERNAL", "TOUCH_EXT", "TOUCH_INT", "AUDIO_CARD_NAME", "POLL")


def daemon_defaults():
    """The constants as the daemon text declares them (the lines before the loader)."""
    text = read(NEW_092)
    out = {}
    for name in ("EXTERNAL", "INTERNAL", "TOUCH_EXT", "TOUCH_INT", "AUDIO_CARD_NAME"):
        m = re.search(r"^%s=(['\"])(.*?)\1" % name, text, re.M)
        out[name] = m.group(2)
    return out


@unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
class TestLoader(TmpDir):
    def load(self, content=None, binary=None):
        d = self.mk()
        path = os.path.join(d, "device-profile.env")
        if content is not None:
            with open(path, "wb") as f:
                f.write(binary if binary is not None else content.encode("utf-8"))
        lib = lib_for(read(NEW_092), ["load_device_profile"], extra=DEFAULTS + "PROFILE_FILE=%s\n" % sh_quote(to_sh_path(path)))
        script = lib + "load_device_profile\n" + "".join('echo "%s=[$%s]"\n' % (n, n) for n in NAMES)
        r = run_dash(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        return dict(line.split("=", 1) for line in r.stdout.splitlines() if line.split("=", 1)[0] in NAMES)

    def values(self, **kw):
        base = {"INTERNAL": "DSI-1", "EXTERNAL": "DP-1", "TOUCH_EXT": "8746:1:RetroidPocket_RDS_Touchscreen",
                "TOUCH_INT": "0:0:generic_ft5x06_(a0)", "AUDIO_CARD_NAME": "Built-in Audio", "POLL": "5"}
        base.update(kw)
        return {k: "[%s]" % v for k, v in base.items()}

    def test_no_file_changes_nothing(self):
        self.assertEqual(self.load(None), self.values())

    def test_the_rp5s_own_profile_changes_nothing(self):
        self.assertEqual(self.load(screen_map.env_lines(screen_map.RP5)), self.values())

    def test_other_values_replace_the_defaults(self):
        got = self.load("INTERNAL='DSI-2'\nEXTERNAL='DSI-1'\nTOUCH_INT='0:0:bottom_touchscreen'\nAUDIO_CARD_NAME='Sound Card 1'\n")
        self.assertEqual(got, self.values(INTERNAL="DSI-2", EXTERNAL="DSI-1", TOUCH_INT="0:0:bottom_touchscreen",
                                          AUDIO_CARD_NAME="Sound Card 1"))

    def test_only_the_five_names_are_read(self):
        got = self.load("POLL='1'\nLOG='/x'\nPATH='/bin'\nINTERNAL='DSI-2'\n")
        self.assertEqual(got, self.values(INTERNAL="DSI-2"))

    def test_a_value_that_could_run_something_is_ignored(self):
        for bad in ("DSI-2;reboot", "$(reboot)", "`reboot`", "a b'c", "DSI-2|x", "a&b", "x>y", "a\\b", "a$b"):
            got = self.load("INTERNAL='%s'\n" % bad)
            self.assertEqual(got["INTERNAL"], "[DSI-1]", bad)

    def test_an_empty_or_unquoted_value_is_ignored(self):
        self.assertEqual(self.load("INTERNAL=''\nEXTERNAL=DSI-2\n"), self.values())

    def test_a_last_line_without_a_newline_is_read(self):
        self.assertEqual(self.load("INTERNAL='DSI-2'"), self.values(INTERNAL="DSI-2"))

    def test_a_bad_line_does_not_stop_the_good_ones(self):
        got = self.load("junk\nINTERNAL='$x'\nEXTERNAL='DSI-1'\n")
        self.assertEqual(got, self.values(EXTERNAL="DSI-1"))

    def test_the_last_line_for_a_name_wins(self):
        self.assertEqual(self.load("INTERNAL='DSI-2'\nINTERNAL='DSI-3'\n"), self.values(INTERNAL="DSI-3"))

    def test_an_unreadable_file_changes_nothing(self):
        d = self.mk()                       # a folder where the file should be
        os.makedirs(os.path.join(d, "device-profile.env"))
        lib = lib_for(read(NEW_092), ["load_device_profile"],
                      extra=DEFAULTS + "PROFILE_FILE=%s\n" % sh_quote(to_sh_path(os.path.join(d, "device-profile.env"))))
        r = run_dash(lib + 'load_device_profile\necho "[$INTERNAL]"')
        self.assertEqual(r.stdout.strip(), "[DSI-1]")


@unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
class TestWholeLoop(LoopSim):
    """The whole daemon against the sway stand-in: the RP5's own profile file changes no command at all."""

    def scenario(self):
        st = world(docked=True)
        st["timeline"] = {"4": [["game_start", None]], "6": [["game_exit", None]]}
        st["stop_at"] = 9
        return st

    def test_the_rp5_profile_file_sends_the_same_commands_as_no_file(self):
        without = self.simulate(self.scenario(), read(NEW_092), new=True)
        with_file = self.simulate(self.scenario(), read(NEW_092), new=True, profile=screen_map.env_lines(screen_map.RP5))
        self.assertTrue(without[1].strip(), "the simulation sent no commands at all")
        tmp = re.compile(r"sw1-092-\w+")        # each run's own temp folder shows in the commands
        self.assertEqual(tmp.sub("T", with_file[1]), tmp.sub("T", without[1]))
        self.assertEqual(with_file[0]["history"], without[0]["history"])

    def test_another_output_name_reaches_the_commands(self):
        got = self.simulate(self.scenario(), read(NEW_092), new=True, profile="INTERNAL='DSI-9'\n")
        self.assertIn("DSI-9", got[1])

    def test_without_the_file_the_default_name_is_used(self):
        got = self.simulate(self.scenario(), read(NEW_092), new=True)
        self.assertNotIn("DSI-9", got[1])
        self.assertIn("DSI-1", got[1])


class TestTheDefaultsMatchTheProfile(unittest.TestCase):
    """The daemon's own constants and screen_map's RP5 profile say the same, so an RP5 needs no file."""

    def test_the_rp5_profile_equals_the_daemon_constants(self):
        d = daemon_defaults()
        s = screen_map.RP5
        self.assertEqual(d, {"INTERNAL": s.bottom, "EXTERNAL": s.top, "TOUCH_EXT": s.touch_top,
                             "TOUCH_INT": s.touch_bottom, "AUDIO_CARD_NAME": s.audio_card})

    def test_the_rp5_env_text_lists_all_five_names_with_the_daemons_values(self):
        got = {}
        for line in screen_map.env_lines(screen_map.RP5).splitlines():
            name, _, val = line.partition("=")
            got[name] = val.strip("'")
        self.assertEqual(got, daemon_defaults())

    def test_the_env_text_round_trips_through_the_daemons_accepted_characters(self):
        for line in screen_map.env_lines(screen_map.RP5).splitlines():
            name, _, val = line.partition("=")
            self.assertRegex(val.strip("'"), r"^[A-Za-z0-9:_.,() -]+$", name)

    def test_the_stray_output_name_in_the_python_snippet_is_gone(self):
        text = read(NEW_092)
        self.assertNotIn('o.get("name")=="DSI-1"', text)
        self.assertIn('o.get("name")==sys.argv[1]', text)
        self.assertIn('print("absent")\n\' "$INTERNAL" 2>/dev/null)', text)      # and the name is handed to it

    def test_a_built_in_panels_profile_writes_the_names_its_own_daemon_reads(self):
        self.assertEqual(screen_map.env_lines(screen_map.BUILTIN_DUAL), "BOTTOM_OUTPUT='DSI-2'\nTOP_OUTPUT='DSI-1'\n")
        self.assertEqual(screen_map.env_lines(screen_map.THOR),
                         "BOTTOM_OUTPUT='DSI-1'\nTOP_OUTPUT='DSI-2'\nTOUCH_BOTTOM='0:0:bottom_touchscreen'\n"
                         "TOUCH_TOP='0:0:top_touchscreen'\n")
        self.assertNotIn("INTERNAL", screen_map.env_lines(screen_map.THOR))


if __name__ == "__main__":
    unittest.main()
