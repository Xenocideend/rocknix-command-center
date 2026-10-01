"""rocknix-config/dual-screen-builtin-layout, the layout daemon for handhelds with two built-in panels, run verbatim under bash
against a sway stand-in (tests/builtin_sway_sim.py): it powers the bottom panel on, enables and maps its touch screen,
switches lowerdeck off through ROCKNIX's setting, and otherwise changes nothing."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import test_sw1_092 as sw1  # noqa: E402

APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
DAEMON = os.path.join(os.path.dirname(APP), "scripts", "dual-screen-builtin-layout")
SIM = os.path.join(HERE, "builtin_sway_sim.py")


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def have_bash():
    if sw1.LINUX:
        return shutil.which("bash") is not None
    w = sw1.wsl()
    if not w:
        return False
    try:
        r = subprocess.run([w, "-e", "sh", "-c", "command -v bash && command -v python3"], capture_output=True, text=True,
                           timeout=30)
        return r.returncode == 0 and "bash" in r.stdout
    except Exception:
        return False


BASH = have_bash()

PROFILE = ("BOTTOM_OUTPUT='DSI-1'\nTOP_OUTPUT='DSI-2'\nTOUCH_BOTTOM='0:0:bottom_touchscreen'\n"
           "TOUCH_TOP='0:0:top_touchscreen'\n")
PROFILE_SH = ('get_setting() { grep "^$1=" "$SETTINGS_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }\n'
              'set_setting() { grep -v "^$1=" "$SETTINGS_FILE" > "$SETTINGS_FILE.t" 2>/dev/null; '
              'echo "$1=$2" >> "$SETTINGS_FILE.t"; mv "$SETTINGS_FILE.t" "$SETTINGS_FILE"; }\n')


def state(bottom_power=False, touch="disabled", extra_outputs=(), extra_inputs=()):
    return {"outputs": [{"name": "DSI-2", "power": True}, {"name": "DSI-1", "power": bottom_power}] + list(extra_outputs),
            "inputs": [{"identifier": "0:0:top_touchscreen", "type": "touch", "send_events": "enabled"},
                       {"identifier": "0:0:bottom_touchscreen", "type": "touch", "send_events": touch}] + list(extra_inputs),
            "mapped": {}, "silent": False}


@unittest.skipUnless(BASH, "needs bash and python3 (Linux or WSL)")
class Run(sw1.TmpDir):
    def setUp(self):
        self.t = self.mk()
        self.files = {k: os.path.join(self.t, k) for k in ("state.json", "cmds.log", "profile.env", "settings", "prev",
                                                           "profile.sh", "daemon.log", "disable", "power-flag",
                                                           "keep-lowerdeck", "pid-sway", "sleep-hook.sh")}
        os.makedirs(os.path.join(self.t, "bin"))
        for name, body in {
            "swaymsg": '#!/bin/sh\nexec python3 "$SIM_PY" "$@"\n',
            "pidof": '#!/bin/sh\n[ "$1" = sway ] && { cat "$SWAY_PID_FILE" 2>/dev/null || echo 4242; exit 0; }\nexit 1\n',
            "sleep": '#!/bin/sh\n[ -n "${SLEEP_HOOK:-}" ] && sh "$SLEEP_HOOK"\nexit 0\n',
        }.items():
            p = os.path.join(self.t, "bin", name)
            with open(p, "w", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.write("profile.env", PROFILE)
        self.write("profile.sh", PROFILE_SH)
        self.write("settings", "")
        self.set_state(state())

    def write(self, key, text):
        with open(self.files[key], "w", newline="\n", encoding="utf-8") as f:
            f.write(text)

    def set_state(self, st):
        with open(self.files["state.json"], "w", encoding="utf-8") as f:
            json.dump(st, f)

    def get_state(self):
        with open(self.files["state.json"], encoding="utf-8") as f:
            return json.load(f)

    def cmds(self):
        p = self.files["cmds.log"]
        return slurp(p).splitlines() if os.path.exists(p) else []

    def changes(self):
        return [c for c in self.cmds() if not c.startswith(("-r -t get_", "-t get_"))]

    def settings(self):
        return slurp(self.files["settings"])

    def run_daemon(self, *args, env_extra=None, sleep_hook=None):
        T = sw1.to_sh_path(self.t)
        f = {k: sw1.to_sh_path(v) for k, v in self.files.items()}
        env = {
            "SIM_PY": sw1.to_sh_path(SIM), "SIM_STATE": f["state.json"], "SIM_LOG": f["cmds.log"],
            "RP5DECK_BUILTIN_LOG": f["daemon.log"], "RP5DECK_BUILTIN_LOCK": T + "/lock", "RP5DECK_DISABLE_FILE": f["disable"],
            "RP5DECK_PROFILE_FILE": f["profile.env"], "RP5DECK_POWER_FLAG": f["power-flag"],
            "RP5DECK_KEEP_LOWERDECK": f["keep-lowerdeck"], "RP5DECK_LOWERDECK_PREV": f["prev"],
            "RP5DECK_PROFILE_SH": f["profile.sh"], "SETTINGS_FILE": f["settings"], "SWAY_PID_FILE": f["pid-sway"],
            "RP5DECK_BUILTIN_POLL": "1",
        }
        if sleep_hook:
            self.write("sleep-hook.sh", sleep_hook)
            env["SLEEP_HOOK"] = T + "/sleep-hook.sh"
        env.update(env_extra or {})
        exports = " ".join("%s=%s" % (k, sw1.sh_quote(v)) for k, v in env.items())
        cmd = "export %s PATH=%s/bin:$PATH; chmod +x %s/bin/*; bash %s %s" % (
            exports, sw1.sh_quote(T), sw1.sh_quote(T), sw1.sh_quote(sw1.to_sh_path(DAEMON)), " ".join(args))
        if sw1.LINUX:
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=120)
        else:
            r = subprocess.run([sw1.wsl(), "-e", "bash", "-c", cmd], capture_output=True, text=True, timeout=120)
        return r

    # ---- the bottom panel's power -------------------------------------------------------------------------------
    def test_a_bottom_panel_that_is_off_is_powered_on(self):
        r = self.run_daemon("--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("output DSI-1 power on", self.cmds())
        self.assertTrue(next(o for o in self.get_state()["outputs"] if o["name"] == "DSI-1")["power"])
        self.assertIn("powered on", r.stdout)

    def test_the_other_panel_is_never_touched(self):
        self.set_state(state(bottom_power=False))
        self.run_daemon("--once")
        self.assertFalse([c for c in self.cmds() if c.startswith("output DSI-2")])

    def test_a_decoy_output_with_power_true_does_not_hide_the_bottom_panel(self):
        self.set_state(state(bottom_power=False, extra_outputs=[{"name": "HDMI-A-1", "power": True}]))
        self.run_daemon("--once")
        self.assertIn("output DSI-1 power on", self.cmds())

    def test_a_panel_that_is_already_on_gets_no_power_command(self):
        self.set_state(state(bottom_power=True, touch="enabled"))
        self.run_daemon("--polls", "3")
        self.assertFalse([c for c in self.cmds() if c.startswith("output ")])

    def test_the_rocknix_power_flag_leaves_the_power_alone(self):
        self.write("power-flag", "")
        self.run_daemon("--once")
        self.assertFalse([c for c in self.cmds() if c.startswith("output ")])

    def test_a_panel_sway_does_not_list_is_reported_once_and_left_alone(self):
        self.write("profile.env", PROFILE.replace("DSI-1", "DSI-9"))
        r = self.run_daemon("--polls", "3")
        self.assertFalse([c for c in self.cmds() if c.startswith("output ")])
        self.assertEqual(r.stdout.count("does not list DSI-9"), 1)

    # ---- the touch screen -----------------------------------------------------------------------------------------
    def test_a_disabled_bottom_touch_screen_is_enabled_and_mapped(self):
        self.run_daemon("--once")
        c = self.cmds()
        self.assertIn("input 0:0:bottom_touchscreen events enabled", c)
        self.assertIn("input 0:0:bottom_touchscreen map_to_output DSI-1", c)
        st = self.get_state()
        self.assertEqual(next(i for i in st["inputs"] if i["identifier"].endswith("bottom_touchscreen"))["send_events"],
                         "enabled")

    def test_the_top_touch_screen_is_never_changed(self):
        self.set_state(state(touch="disabled"))
        for i in self.get_state()["inputs"]:
            pass
        st = self.get_state()
        st["inputs"][0]["send_events"] = "disabled"          # even a disabled top one is ROCKNIX's business
        self.set_state(st)
        self.run_daemon("--polls", "2")
        self.assertFalse([c for c in self.cmds() if "top_touchscreen" in c and not c.startswith("-")])

    def test_an_enabled_touch_screen_is_left_alone_but_mapped_once_at_the_start(self):
        self.set_state(state(bottom_power=True, touch="enabled"))
        self.run_daemon("--polls", "4")
        maps = [c for c in self.cmds() if "map_to_output" in c]
        self.assertEqual(maps, ["input 0:0:bottom_touchscreen map_to_output DSI-1"])
        self.assertFalse([c for c in self.cmds() if "events" in c and not c.startswith("-")])

    def test_a_new_sway_gets_the_mapping_again(self):
        self.set_state(state(bottom_power=True, touch="enabled"))
        self.write("pid-sway", "4242\n")
        hook = 'n=$(cat "$SIM_LOG.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$SIM_LOG.n"; [ "$n" -ge 2 ] && echo 5151 > "$SWAY_PID_FILE"\n'
        r = self.run_daemon("--polls", "4", sleep_hook=hook)
        maps = [c for c in self.cmds() if "map_to_output" in c]
        self.assertEqual(len(maps), 2, self.cmds())
        self.assertIn("sway restarted (pid 4242 -> 5151)", r.stdout)

    def test_a_device_with_no_touch_name_gets_no_touch_commands(self):
        self.write("profile.env", "BOTTOM_OUTPUT='DSI-1'\nTOP_OUTPUT='DSI-2'\n")
        self.run_daemon("--once")
        self.assertFalse([c for c in self.cmds() if c.startswith("input ")])
        self.assertIn("output DSI-1 power on", self.cmds())

    def test_a_touch_screen_sway_does_not_list_is_left_alone(self):
        self.write("profile.env", PROFILE.replace("bottom_touchscreen", "no_such_screen"))
        self.run_daemon("--polls", "2")
        self.assertFalse([c for c in self.cmds() if "events" in c and not c.startswith("-")])

    # ---- lowerdeck ---------------------------------------------------------------------------------------------
    def test_lowerdeck_is_switched_off_and_the_old_value_kept(self):
        self.write("settings", "rocknix.bottomscreen.type=vc\n")
        r = self.run_daemon("--once")
        self.assertIn("rocknix.bottomscreen.type=none", self.settings())
        self.assertEqual(slurp(self.files["prev"]).strip(), "vc")
        self.assertIn("lowerdeck switched off", r.stdout)

    def test_an_empty_setting_counts_as_the_default_vc(self):
        self.run_daemon("--once")
        self.assertIn("rocknix.bottomscreen.type=none", self.settings())
        self.assertEqual(slurp(self.files["prev"]).strip(), "vc")

    def test_a_setting_the_user_chose_is_left_alone(self):
        self.write("settings", "rocknix.bottomscreen.type=something\n")
        self.run_daemon("--once")
        self.assertEqual(self.settings(), "rocknix.bottomscreen.type=something\n")
        self.assertFalse(os.path.exists(self.files["prev"]))

    def test_lowerdeck_already_off_is_left_alone(self):
        self.write("settings", "rocknix.bottomscreen.type=none\n")
        self.run_daemon("--once")
        self.assertFalse(os.path.exists(self.files["prev"]))

    def test_the_keep_file_leaves_lowerdeck_alone(self):
        self.write("keep-lowerdeck", "")
        self.write("settings", "rocknix.bottomscreen.type=vc\n")
        self.run_daemon("--once")
        self.assertEqual(self.settings(), "rocknix.bottomscreen.type=vc\n")

    def test_the_first_value_is_not_overwritten_by_a_later_run(self):
        self.write("settings", "rocknix.bottomscreen.type=vc\n")
        self.run_daemon("--once")
        self.write("settings", "rocknix.bottomscreen.type=\n")       # something put it back to empty
        self.run_daemon("--once")
        self.assertEqual(slurp(self.files["prev"]).strip(), "vc")

    def test_restore_gives_the_old_value_back_once(self):
        self.write("settings", "rocknix.bottomscreen.type=vc\n")
        self.run_daemon("--once")
        r = self.run_daemon("--restore-lowerdeck")
        self.assertIn("rocknix.bottomscreen.type=vc", self.settings())
        self.assertFalse(os.path.exists(self.files["prev"]))
        self.assertIn("set back to 'vc'", r.stdout)
        r = self.run_daemon("--restore-lowerdeck")
        self.assertIn("nothing to restore", r.stdout)

    def test_restore_leaves_a_value_the_user_changed_since(self):
        self.write("settings", "rocknix.bottomscreen.type=vc\n")
        self.run_daemon("--once")
        self.write("settings", "rocknix.bottomscreen.type=other\n")
        r = self.run_daemon("--restore-lowerdeck")
        self.assertIn("rocknix.bottomscreen.type=other", self.settings())
        self.assertIn("left alone", r.stdout)

    # ---- doing nothing, and doing no harm ---------------------------------------------------------------------
    def test_with_no_profile_it_sends_sway_nothing(self):
        os.remove(self.files["profile.env"])
        r = self.run_daemon("--once")
        self.assertEqual(self.cmds(), [])
        self.assertEqual(self.settings(), "")
        self.assertIn("nothing to do", r.stdout)

    def test_a_profile_with_an_unsafe_value_counts_as_no_profile(self):
        for bad in ("DSI-1;reboot", "$(reboot)", "`x`", "a|b", "a b'c"):
            self.write("profile.env", "BOTTOM_OUTPUT='%s'\n" % bad)
            self.run_daemon("--once")
            self.assertEqual(self.cmds(), [], bad)

    def test_only_the_four_names_are_read(self):
        # a line that tried to change the daemon's own settings must not reach them
        self.write("profile.env", "LOWERDECK_KEY='some.other.key'\nINTERNAL='DSI-1'\nBOTTOM_OUTPUT='DSI-1'\n")
        self.run_daemon("--once")
        self.assertIn("output DSI-1 power on", self.cmds())
        self.assertIn("rocknix.bottomscreen.type=none", self.settings())
        self.assertNotIn("some.other.key", self.settings())

    def test_a_touch_screen_disabled_while_running_is_enabled_and_mapped_again(self):
        self.set_state(state(bottom_power=True, touch="enabled"))
        # after the first poll something (a sway reload) disables the touch screen again, once
        hook = ('[ -e "$SIM_LOG.done" ] && exit 0\n'
                'touch "$SIM_LOG.done"\n'
                'python3 -c "import json,os;p=os.environ[\'SIM_STATE\'];st=json.load(open(p));'
                '[i.update(send_events=\'disabled\') for i in st[\'inputs\'] if i[\'identifier\'].endswith(\'bottom_touchscreen\')];'
                'json.dump(st,open(p,\'w\'))"\n')
        self.run_daemon("--polls", "3", sleep_hook=hook)
        c = self.cmds()
        self.assertEqual(c.count("input 0:0:bottom_touchscreen events enabled"), 1, c)
        self.assertEqual(c.count("input 0:0:bottom_touchscreen map_to_output DSI-1"), 2, c)

    def test_it_never_disables_moves_focuses_or_kills(self):
        for st in (state(), state(bottom_power=True, touch="enabled"), state(touch="disabled")):
            self.set_state(st)
            self.run_daemon("--polls", "3")
        for c in self.cmds():
            self.assertNotRegex(c, r"\b(disable|move|focus|kill|exec|reload|workspace|for_window)\b", c)

    def test_a_silent_sway_gets_no_commands_and_no_crash(self):
        st = state()
        st["silent"] = True
        self.set_state(st)
        r = self.run_daemon("--polls", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in self.cmds() if not c.startswith(("-r -t get_", "-t get_"))
                          and not c.startswith("input ")])
        self.assertFalse([c for c in self.cmds() if c.startswith("output ")])

    def test_no_sway_process_means_no_commands(self):
        self.write("pid-sway", "")
        os.remove(self.files["pid-sway"])
        T = sw1.to_sh_path(self.t)
        r = self.run_daemon("--once", env_extra={"SWAY_PID_FILE": T + "/none"})
        self.assertEqual(r.returncode, 0)
        # pidof stub answers 4242 when the file is missing, so make it answer nothing
        with open(os.path.join(self.t, "bin", "pidof"), "w", newline="\n") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.remove(self.files["cmds.log"])
        self.run_daemon("--once")
        self.assertEqual(self.cmds(), [])

    def test_the_kill_switch_stops_it_before_it_asks_sway_anything(self):
        self.write("disable", "")
        r = self.run_daemon("--once")
        self.assertEqual((r.returncode, self.cmds()), (0, []))

    def test_status_reports_and_changes_nothing(self):
        self.write("settings", "rocknix.bottomscreen.type=vc\n")
        r = self.run_daemon("--status")
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.settings(), "rocknix.bottomscreen.type=vc\n")
        self.assertIn("bottom output: DSI-1", r.stdout)
        self.assertIn("bottom power in sway: false", r.stdout)
        self.assertIn("bottom touch send_events: disabled", r.stdout)

    def test_a_wrong_argument_prints_the_usage(self):
        r = self.run_daemon("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stdout)

    def test_the_script_parses(self):
        T = sw1.to_sh_path(DAEMON)
        if sw1.LINUX:
            r = subprocess.run(["bash", "-n", T], capture_output=True, text=True)
        else:
            r = subprocess.run([sw1.wsl(), "-e", "bash", "-n", T], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_a_steady_poll_asks_sway_for_two_things_only(self):
        self.set_state(state(bottom_power=True, touch="enabled"))
        self.run_daemon("--polls", "3")
        queries = [c for c in self.cmds() if c.startswith("-r -t get_")]
        self.assertEqual(sorted(set(queries)), ["-r -t get_inputs", "-r -t get_outputs"])
        self.assertEqual(len(queries), 6)


class TestStatic(unittest.TestCase):
    def text(self):
        with open(DAEMON, encoding="utf-8") as f:
            return f.read()

    def test_it_has_no_command_that_removes_or_moves_anything(self):
        code = [ln for ln in self.text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        for ln in code:
            self.assertNotRegex(ln, r"\b(pkill|killall|reboot|poweroff|systemctl)\b|\bkill\b(?! switch)|"
                                r"swaymsg\s+(?:\[|move|focus|kill|reload|exec)", ln)
            self.assertNotRegex(ln, r"output\s+\S+\s+(disable|enable)\b", ln)

    def test_every_sway_change_is_one_of_the_three_it_documents(self):
        calls = re.findall(r"swaymsg (output|input) [^\n]*", self.text())
        self.assertGreaterEqual(len(calls), 3)
        for line in re.findall(r"swaymsg (?:output|input)[^\n>]*", self.text()):
            self.assertRegex(line, r"power on|events enabled|map_to_output", line)

    def test_the_header_names_the_switches_the_code_reads(self):
        t = self.text()
        for name in ("/storage/.disable-dualscreen", "/storage/.bottom-screen-power-rocknix", "/storage/.keep-lowerdeck"):
            self.assertIn(name, t)


if __name__ == "__main__":
    unittest.main()
