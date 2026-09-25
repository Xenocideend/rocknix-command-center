"""Azahar's game window must be placed on the game screen by
rocknix-config/092-dual-screen-persist.

Device, 25 Sep: Azahar restored a Qt geometry saved on the built-in panel, so
its game window mapped on DSI-1 on top of the Secondary Window; with no frame
callbacks for the covered window the renderer blocked and the game froze.
Clearing the geometry fixed that boot only (Azahar re-saves it). 092 now:
  1. ensure_rp5deck_rules: `for_window [<game window>] move container to
     output $EXTERNAL, focus` (the add-on; the pre-SW1 layout);
  2. ensure_swap_rules: `for_window [<game window>] exec $SELF
     --place-cc-windows game`;
  3. the --place-cc-windows handler: with "game", and only then, moves the
     game window to $INTERNAL (swapped, the games live there with ES).
The title pattern is read from the script, bash-unescaped, and checked against
real Azahar titles seen on the device and the Secondary Window it must skip.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
PATH_092 = os.environ.get("SW1_092_PATH") or os.path.join(os.path.dirname(APP), "scripts", "092-dual-screen-persist")

# [title=\"...\" app_id=\"...\"] inside a double-quoted swaymsg argument
CRIT_RE = re.compile(r'\[title=\\"(\^Azahar .+?)\\" app_id=\\"(.+?)\\"\]')

GAME_TITLES = [
    "Azahar fbd3fb0 | Ocarina of Time 3D",           # device, 25 Sep
    "Azahar fbd3fb0 | Majora's Mask 3D",
    "Azahar 2123.1 | Test Game: Robo Edition",        # a release build's version
]
NOT_GAME_TITLES = [
    "Azahar fbd3fb0 | Ocarina of Time 3D | Secondary Window",   # device, 25 Sep
    "Azahar fbd3fb0",                                 # before a game is loaded
    "Secondary Window",
    "EmulationStation",
]


def bash_unescape(s):
    """What bash makes of a double-quoted string: \\\\ \\" \\$ \\` lose the backslash."""
    return re.sub(r'\\([\\"$`])', r"\1", s)


def function_body(text, name):
    start = text.index("\n%s() {" % name)
    end = text.index("\n}\n", start)
    return text[start:end]


def handler_block(text):
    start = text.index('if [ "${1:-}" = "--place-cc-windows" ]; then')
    end = text.index("\nfi\n", start)
    return text[start:end]


def rules(block):
    """[(title_regex, app_id_regex, the rest of the line)] for each Azahar rule."""
    out = []
    for line in block.split("\n"):
        m = CRIT_RE.search(line)
        if m:
            out.append((bash_unescape(m.group(1)), bash_unescape(m.group(2)),
                        line[m.end():]))
    return out


class TestAzaharGameWindowRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PATH_092, encoding="utf-8") as f:
            cls.text = f.read()

    def test_map_time_rule_sends_it_to_the_addon_with_focus(self):
        r = rules(function_body(self.text, "ensure_rp5deck_rules"))
        self.assertEqual(len(r), 1, r)
        self.assertIn("move container to output $EXTERNAL, focus", r[0][2])
        line = [l for l in function_body(self.text, "ensure_rp5deck_rules").split("\n")
                if CRIT_RE.search(l)][0]
        self.assertIn('swaymsg "for_window [title=', line)

    def test_swap_rule_execs_the_handler_with_game(self):
        r = rules(function_body(self.text, "ensure_swap_rules"))
        self.assertEqual(len(r), 1, r)
        self.assertIn("exec $SELF --place-cc-windows game", r[0][2])

    def test_handler_moves_it_to_the_builtin_panel_only_for_game(self):
        block = handler_block(self.text)
        r = rules(block)
        self.assertEqual(len(r), 1, r)
        self.assertIn("move container to output $INTERNAL, focus", r[0][2])
        # the move sits inside the "game" branch, and the Command Center move
        # is in the other branch, so a CC window opening never steals focus
        game_at = block.index('if [ "${2:-}" = "game" ]; then')
        else_at = block.index("else", game_at)
        self.assertLess(game_at, block.index(CRIT_RE.search(block).group(0)), "move before the game test")
        self.assertLess(block.index(CRIT_RE.search(block).group(0)), else_at)
        self.assertGreater(block.index('place_cc_windows "$EXTERNAL"'), else_at)

    def test_all_three_use_the_same_pattern(self):
        pats = {(t, a) for t, a, _ in rules(self.text)}
        self.assertEqual(len(pats), 1, pats)

    def test_pattern_matches_game_windows_and_skips_the_secondary(self):
        title_re, app_re, _ = rules(function_body(self.text, "ensure_rp5deck_rules"))[0]
        for t in GAME_TITLES:
            with self.subTest(t):
                self.assertTrue(re.search(title_re, t), t)
        for t in NOT_GAME_TITLES:
            with self.subTest(t):
                self.assertFalse(re.search(title_re, t), t)
        self.assertTrue(re.search(app_re, "org.azahar_emu.Azahar"))
        self.assertFalse(re.search(app_re, "org.azahar_emu.AzaharX"))
        self.assertFalse(re.search(app_re, "rp5deck-web"))


if __name__ == "__main__":
    unittest.main()
