"""rocknix-config/dual-screen-layout-and-power --show-undocked-window: what sway's for_window rule runs the
moment a Browser/Discord or YouTube App window maps. Undocked it moves that window to a workspace of its
own and shows it, docked it does nothing. Run under dash with a stub swaymsg, like test_sw1_092."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_sw1_092 as sw1  # noqa: E402
import window_switcher as wsw  # noqa: E402

OUTPUTS_DOCKED = '[\n  {\n    "name": "DSI-1"\n  },\n  {\n    "name": "DP-1"\n  }\n]\n'
OUTPUTS_UNDOCKED = '[\n  {\n    "name": "DSI-1"\n  }\n]\n'


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestShowUndockedWindow(sw1.TmpDir):
    def run_handler(self, outputs, *args, disabled=False):
        t = self.mk()
        b = os.path.join(t, "bin")
        os.makedirs(b)
        with open(os.path.join(b, "swaymsg"), "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\n'
                    'case "$1" in -t|-r) cat "$OUTPUTS_FILE"; exit 0;; esac\n'
                    'echo "$1" >> "$SENT_FILE"\n')
        os.chmod(os.path.join(b, "swaymsg"), 0o755)
        with open(os.path.join(t, "outputs.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(outputs)
        if disabled:
            open(os.path.join(t, "disable"), "w").close()
        script = os.path.join(t, "092")
        text = sw1.read(sw1.NEW_092)
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(sw1.prepare_script(text, t, new=True))
        os.chmod(script, 0o755)
        T = sw1.to_sh_path(t)
        cmd = ("export PATH=%s/bin:$PATH OUTPUTS_FILE=%s/outputs.json SENT_FILE=%s/sent; "
               "chmod +x %s/bin/* %s/092; dash %s/092 %s; echo rc=$?"
               % (T, T, T, T, T, T, " ".join(sw1.sh_quote(a) for a in args)))
        r = sw1.run_dash(cmd, timeout=60)
        self.assertIn("rc=0", r.stdout, r.stdout + r.stderr)
        sent = os.path.join(t, "sent")
        if not os.path.exists(sent):
            return []
        with open(sent, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_undocked_moves_the_window_and_shows_its_workspace(self):
        for kind in ("web", "ytapp"):
            sent = self.run_handler(OUTPUTS_UNDOCKED, "--show-undocked-window", kind)
            self.assertEqual(sent, [
                '[app_id="^rp5deck-%s$"] move container to workspace rp5deck-undocked-%s' % (kind, kind),
                "workspace rp5deck-undocked-%s" % kind], kind)

    def test_the_names_are_the_ones_the_switcher_uses(self):
        sent = self.run_handler(OUTPUTS_UNDOCKED, "--show-undocked-window", "web")
        self.assertTrue(sent[0].endswith(wsw.UNDOCKED_WS[wsw.WEB]), sent)
        sent = self.run_handler(OUTPUTS_UNDOCKED, "--show-undocked-window", "ytapp")
        self.assertTrue(sent[1].endswith(wsw.UNDOCKED_WS[wsw.YTAPP]), sent)

    def test_docked_does_nothing(self):
        self.assertEqual(self.run_handler(OUTPUTS_DOCKED, "--show-undocked-window", "web"), [])

    def test_unknown_kind_or_the_kill_switch_does_nothing(self):
        self.assertEqual(self.run_handler(OUTPUTS_UNDOCKED, "--show-undocked-window", "yt"), [])
        self.assertEqual(self.run_handler(OUTPUTS_UNDOCKED, "--show-undocked-window", "web; exit"), [])
        self.assertEqual(self.run_handler(OUTPUTS_UNDOCKED, "--show-undocked-window"), [])
        self.assertEqual(self.run_handler(OUTPUTS_UNDOCKED, "--show-undocked-window", "web",
                                          disabled=True), [])

    def test_es_going_back_to_workspace_one_when_undocked(self):
        sent = self.run_handler(OUTPUTS_UNDOCKED, "--home-es-window")
        self.assertEqual(sent, ['[app_id="^emulationstation$"] move container to workspace number 1',
                                "workspace number 1"])

    def test_es_is_left_alone_docked_or_when_the_daemon_is_off(self):
        self.assertEqual(self.run_handler(OUTPUTS_DOCKED, "--home-es-window"), [])
        self.assertEqual(self.run_handler(OUTPUTS_UNDOCKED, "--home-es-window", disabled=True), [])


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestParkEsLeavesTheWebWorkspaceAlone(unittest.TestCase):
    """park_es runs every poll while undocked and used to pull the panel back to workspace 1."""

    def poll(self, shown):
        extra = ("es_swapped_seen=no\n"
                 "es_output() { echo DSI-1; }\n"
                 "swaymsg() { if [ \"$1\" = -t ]; then printf '%%s' '[{\"name\":\"DSI-1\",\"current_workspace\":\"%s\"}]'; "
                 "else echo \"SWAY $*\"; fi; }\n" % shown)
        lib = sw1.lib_for(sw1.read(sw1.NEW_092), ["park_es"], extra=extra)
        r = sw1.run_dash(lib + 'park_es DSI-1; echo "rc=$?"')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_a_web_workspace_stays_up(self):
        for ws_name in (wsw.UNDOCKED_WS[wsw.WEB], wsw.UNDOCKED_WS[wsw.YTAPP]):
            out = self.poll(ws_name)
            self.assertNotIn("switched", out, ws_name)
            self.assertIn("rc=1", out)

    def test_any_other_workspace_still_goes_back_to_es(self):
        out = self.poll("2")
        self.assertIn("switched DSI-1 from workspace 2 to 1", out)
        self.assertIn("rc=0", out)


class TestTheRulesThatRunIt(unittest.TestCase):
    def test_both_kinds_have_a_map_time_rule_in_a_function_of_their_own(self):
        text = sw1.read(sw1.NEW_092)
        start = text.index("\nensure_undocked_web_rules() {")
        body = text[start:text.index("\n}\n", start)]
        for kind in ("web", "ytapp"):
            self.assertIn('app_id=\\"^rp5deck-%s\\$\\"] exec $SELF --show-undocked-window %s' % (kind, kind),
                          body)

    def test_es_has_a_map_time_rule_too(self):
        text = sw1.read(sw1.NEW_092)
        start = text.index("\nensure_undocked_web_rules() {")
        body = text[start:text.index("\n}\n", start)]
        self.assertIn('for_window [app_id=\\"^emulationstation\\$\\"] exec $SELF --home-es-window', body)

    def test_the_rules_are_applied_from_the_main_loop(self):
        text = sw1.read(sw1.NEW_092)
        self.assertEqual(text.count("\n    ensure_undocked_web_rules\n"), 1)


if __name__ == "__main__":
    unittest.main()
