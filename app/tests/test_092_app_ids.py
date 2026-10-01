"""Every app_id rp5deck owns must be placed on the Command Center's screen by
rocknix-config/dual-screen-layout-and-power.

Device, 24 Sep 20:02: the YouTube App's Firefox (rp5deck-ytapp, added to
sway_ipc.OWN_APP_IDS) had no 092 rule, so it mapped on the add-on behind the
fullscreen EmulationStation and "tapping the tile did nothing". This test
reads sway_ipc.OWN_APP_IDS and checks each id against all three places 092
names rp5deck's windows:
  1. ensure_rp5deck_rules: a `for_window [app_id=...] move container to
     output $INTERNAL` rule (the placement at map time);
  2. place_cc_windows: the one-shot move used by the swap;
  3. ensure_swap_rules: the `--place-cc-windows` exec rule.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import sway_ipc  # noqa: E402

REPO = os.path.dirname(os.path.dirname(APP))
PATH_092 = os.environ.get("SW1_092_PATH") or os.path.join(
    os.path.dirname(APP), "scripts", "dual-screen-layout-and-power")

# app_id=\"PATTERN\" inside a double-quoted swaymsg argument
APP_ID_RE = re.compile(r'app_id=\\"(.+?)\\"\]')


def _function_body(text, name):
    start = text.index("\n%s() {" % name)
    end = text.index("\n}\n", start)
    return text[start:end]


def _patterns(lines):
    out = []
    for line in lines:
        for raw in APP_ID_RE.findall(line):
            out.append(raw.replace("\\$", "$"))
    return out


def _matches(patterns, app_id):
    return any(re.search(p, app_id) for p in patterns)


class TestEveryOwnAppIdIsPlaced(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PATH_092, encoding="utf-8") as f:
            cls.text = f.read()

    def test_there_are_ids_to_check(self):
        self.assertIn("rp5deck-ytapp", sway_ipc.OWN_APP_IDS)
        self.assertGreaterEqual(len(sway_ipc.OWN_APP_IDS), 3)

    def test_map_time_rule_moves_each_id_to_the_internal_panel(self):
        body = _function_body(self.text, "ensure_rp5deck_rules")
        lines = [l for l in body.splitlines()
                 if "for_window" in l and "move container to output $INTERNAL" in l]
        pats = _patterns(lines)
        for aid in sorted(sway_ipc.OWN_APP_IDS):
            self.assertTrue(_matches(pats, aid), "%s has no map-time rule in 092: %s" % (aid, pats))

    def test_place_cc_windows_moves_each_id(self):
        pats = _patterns(_function_body(self.text, "place_cc_windows").splitlines())
        for aid in sorted(sway_ipc.OWN_APP_IDS):
            self.assertTrue(_matches(pats, aid), "%s not in place_cc_windows: %s" % (aid, pats))

    def test_swap_exec_rule_covers_each_id(self):
        body = _function_body(self.text, "ensure_swap_rules")
        pats = _patterns(l for l in body.splitlines() if "--place-cc-windows" in l)
        for aid in sorted(sway_ipc.OWN_APP_IDS):
            self.assertTrue(_matches(pats, aid), "%s not in the swap exec rule: %s" % (aid, pats))

    def test_patterns_do_not_leak_to_foreign_ids(self):
        body = _function_body(self.text, "ensure_rp5deck_rules")
        pats = _patterns(body.splitlines())
        for foreign in ("rp5deck-test-other", "emulationstation", "firefox", "rp5deck-ytapp2"):
            self.assertFalse(_matches(pats, foreign), foreign)


if __name__ == "__main__":
    unittest.main()
