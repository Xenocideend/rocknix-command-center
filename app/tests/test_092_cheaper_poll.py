"""The layout daemon's poll steps that were made cheaper (fewer processes per poll), verbatim under dash:
stop_output_monitor (a /proc scan instead of ps|grep|awk), touch_int_state (awk instead of python3),
set_audio_internal (speaker check first) and the sway pid read."""
import os
import subprocess
import sys
import time
import unittest

from tests.test_sw1_092 import DASH, NEW_092, TmpDir, lib_for, read, run_dash, sh_quote, to_sh_path

# a background `sh -c ...` shows its parent's command line until it has exec'd, so wait for it to start with "sh"
WAIT_EXEC = """
wait_exec() {
    local n=0 c
    while [ "$n" -lt 100 ]; do
        c=""
        read -r c 2>/dev/null < /proc/$1/cmdline
        case "$c" in sh*) return 0 ;; esac
        sleep 0.05
        n=$((n + 1))
    done
}
"""

INPUTS = """[
  {
    "identifier": "8746:1:RetroidPocket_RDS_Touchscreen",
    "name": "RetroidPocket RDS Touchscreen",
    "type": "touch",
    "libinput": {
      "send_events": "enabled",
      "calibration_matrix": [
        1.0,
        0.0
      ]
    }
  },
  {
    "identifier": "0:0:wlr_virtual_keyboard_v1",
    "name": "wlr_virtual_keyboard_v1",
    "type": "keyboard"
  },
  {
    "identifier": "0:0:generic_ft5x06_(a0)",
    "name": "generic_ft5x06 (a0)",
    "type": "touch",
    "libinput": {
      "send_events": "%s"
    }
  }
]
"""


@unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
class TestTouchIntState(TmpDir):
    def ask(self, state, ident="0:0:generic_ft5x06_(a0)", text=None):
        d = self.mk()
        with open(os.path.join(d, "inputs.json"), "w", newline="\n") as f:
            f.write(text if text is not None else INPUTS % state)
        stub = os.path.join(d, "bin")
        os.makedirs(stub)
        with open(os.path.join(stub, "swaymsg"), "w", newline="\n") as f:
            f.write("#!/bin/sh\ncat %s\n" % sh_quote(to_sh_path(os.path.join(d, "inputs.json"))))
        lib = lib_for(read(NEW_092), ["touch_int_state"], extra="TOUCH_INT=%s" % sh_quote(ident))
        r = run_dash("chmod +x %s/swaymsg; PATH=%s:$PATH\n" % (sh_quote(to_sh_path(stub)), sh_quote(to_sh_path(stub)))
                     + lib + 'echo "[$(touch_int_state)]"')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_disabled(self):
        self.assertEqual(self.ask("disabled"), "[disabled]")

    def test_enabled(self):
        self.assertEqual(self.ask("enabled"), "[enabled]")

    def test_the_other_touchscreens_state_is_not_taken(self):
        self.assertEqual(self.ask("disabled", ident="8746:1:RetroidPocket_RDS_Touchscreen"), "[enabled]")

    def test_a_device_with_no_libinput_block_gives_nothing(self):
        self.assertEqual(self.ask("x", ident="0:0:wlr_virtual_keyboard_v1"), "[]")

    def test_a_device_that_is_not_listed_gives_nothing(self):
        self.assertEqual(self.ask("disabled", ident="0:0:nothing"), "[]")

    def test_an_empty_answer_gives_nothing(self):
        self.assertEqual(self.ask("", text=""), "[]")

    def test_the_match_is_the_whole_identifier(self):
        self.assertEqual(self.ask("disabled", ident="0:0:generic_ft5x06"), "[]")


@unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
class TestStopOutputMonitor(TmpDir):
    def run_scan(self, victims):
        """Start the given sleepers (each as `sh -c 'sleep 60; :' ARG`), run stop_output_monitor, say which are alive."""
        lib = lib_for(read(NEW_092), ["stop_output_monitor"]) + WAIT_EXEC
        body = ""
        for i, arg in enumerate(victims):
            body += "sh -c 'sleep 60; :' %s &\np%d=$!\nwait_exec $p%d\n" % (sh_quote(arg), i, i)
        body += "stop_output_monitor\nsleep 0.3\n"
        for i in range(len(victims)):
            body += 'if kill -0 "$p%d" 2>/dev/null; then echo alive%d; else echo dead%d; fi\n' % (i, i, i)
        body += "kill $(jobs -p) 2>/dev/null\ntrue\n"
        r = run_dash(lib + body)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [ln for ln in r.stdout.split() if ln.startswith(("alive", "dead"))]

    def test_the_output_monitor_script_is_stopped(self):
        self.assertEqual(self.run_scan(["/usr/bin/output_monitor"]), ["dead0"])

    def test_other_processes_are_left_alone(self):
        self.assertEqual(self.run_scan(["/usr/bin/something_else"]), ["alive0"])

    def test_only_the_matching_one_dies(self):
        self.assertEqual(self.run_scan(["/usr/bin/something_else", "/usr/bin/output_monitor"]), ["alive0", "dead1"])

    def test_a_grep_for_it_is_not_killed(self):
        self.assertEqual(self.run_scan(["grep /usr/bin/output_monitor"]), ["alive0"])

    def test_the_log_line_names_the_pid(self):
        lib = lib_for(read(NEW_092), ["stop_output_monitor"]) + WAIT_EXEC
        r = run_dash(lib + "sh -c 'sleep 60; :' /usr/bin/output_monitor &\np=$!\nwait_exec $p\nstop_output_monitor\necho \"pid=$p\"\n")
        out = r.stdout.split()
        pid = [w for w in out if w.startswith("pid=")][0][4:]
        self.assertIn("LOG stopped output_monitor pid %s" % pid, r.stdout)

    def test_it_starts_no_other_process(self):
        """The old scan started ps, grep, grep and awk on every poll."""
        text = read(NEW_092)
        start = text.index("\nstop_output_monitor() {\n")
        body = text[start:text.index("\n}\n", start)]
        for tool in ("ps ", "grep ", "awk ", "sed ", "tr "):
            self.assertNotIn(tool, body.replace("*grep*", ""))


@unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
class TestSetAudioInternal(TmpDir):
    def run_audio(self, speakers):
        """wpctl/pw-cli stubs log every call; `speakers` decides whether the Sinks block lists a speaker."""
        d = self.mk()
        stub = os.path.join(d, "bin")
        os.makedirs(stub)
        calls = os.path.join(d, "calls.log")
        status = ("Audio\n |- Sinks:\n |      47. Built-in Audio Speaker Playback  [vol: 0.40]\n |- Sources:\n"
                  if speakers else "Audio\n |- Sinks:\n |      52. HDMI Playback\n |- Sources:\n")
        status += " Settings\n 40. Built-in Audio [alsa]\n"
        with open(os.path.join(d, "status.txt"), "w", newline="\n") as f:
            f.write(status)
        with open(os.path.join(stub, "wpctl"), "w", newline="\n") as f:
            f.write('#!/bin/sh\necho "wpctl $*" >> %s\n'
                    '[ "$1" = status ] && cat %s\nexit 0\n'
                    % (sh_quote(to_sh_path(calls)), sh_quote(to_sh_path(os.path.join(d, "status.txt")))))
        with open(os.path.join(stub, "pw-cli"), "w", newline="\n") as f:
            f.write('#!/bin/sh\necho "pw-cli $*" >> %s\n'
                    'printf "Param:Profile:index\\n Int 3\\nParam:Profile:name\\n String \\"HiFi\\"\\n"\n'
                    % sh_quote(to_sh_path(calls)))
        lib = lib_for(read(NEW_092), ["set_audio_internal"], extra='AUDIO_CARD_NAME="Built-in Audio"')
        r = run_dash("chmod +x %s/*; PATH=%s:$PATH\n" % (sh_quote(to_sh_path(stub)), sh_quote(to_sh_path(stub)))
                     + lib + "set_audio_internal\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = read(calls) if os.path.exists(calls) else ""
        return got, r.stdout

    def test_with_the_speakers_present_nothing_is_looked_up_or_changed(self):
        calls, out = self.run_audio(True)
        self.assertNotIn("pw-cli", calls)
        self.assertNotIn("set-profile", calls)
        self.assertEqual(calls.count("wpctl status"), 1)
        self.assertEqual(out.strip(), "")

    def test_with_the_speakers_gone_the_hifi_profile_is_set(self):
        calls, out = self.run_audio(False)
        self.assertIn("wpctl set-profile 40 3", calls)
        self.assertIn("LOG audio: restored HiFi profile (card 40, profile 3)", out)


class TestSwayPidRead(unittest.TestCase):
    def test_no_awk_for_the_first_pid(self):
        text = read(NEW_092)
        self.assertNotIn("pidof sway 2>/dev/null | awk", text)
        self.assertGreaterEqual(text.count("pid=${pid%% *}"), 3)

    @unittest.skipUnless(DASH, "needs dash (Linux or WSL)")
    def test_the_first_of_several_pids_is_taken(self):
        r = run_dash('pid="4242 77 9"; pid=${pid%% *}; echo "[$pid]"; pid=""; pid=${pid%% *}; echo "[$pid]"')
        self.assertEqual(r.stdout.split(), ["[4242]", "[]"])


if __name__ == "__main__":
    unittest.main()
