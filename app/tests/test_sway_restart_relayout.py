"""26 Sep: after an ES restart the bottom screen ignored every touch - sway's restart re-ran
111-sway-init (built-in touch `events disabled`) but dual-screen-layout-and-power's state
never changed, so it never re-applied the layout. The steady-state loop now notices a
new sway PID and re-applies it. The device pass restarts essway and checks touch."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "..", "..", "scripts", "dual-screen-layout-and-power")


def steady_state_block():
    with open(DAEMON, encoding="utf-8") as f:
        s = f.read()
    start = s.index('    if [ "$state" = "$last" ]; then')
    return s[start:s.index("    else", start)]


class SwayRestart(unittest.TestCase):
    def test_steady_state_reapplies_the_layout_on_a_new_sway(self):
        blk = steady_state_block()
        self.assertIn("pidof sway", blk)
        m = re.search(r'if \[ -n "\$\{SWAY_SEEN:-\}" \]; then(.*?)\n            fi', blk, re.S)
        self.assertIsNotNone(m, "no re-apply branch")
        self.assertIn('apply_layout yes', m.group(1))
        self.assertIn('apply_layout no', m.group(1))
        self.assertIn("SWAY_SEEN=$sway_now", blk)

    def test_every_poll_turns_a_disabled_touchscreen_back_on(self):
        blk = steady_state_block()
        m = re.search(r'if \[ "\$\(touch_int_state\)" = "disabled" \]; then(.*?)\n        fi', blk, re.S)
        self.assertIsNotNone(m, "no touchscreen check in the steady state")
        self.assertIn('swaymsg input "$TOUCH_INT" events enabled', m.group(1))

    def test_apply_layout_turns_the_built_in_touch_back_on(self):
        with open(DAEMON, encoding="utf-8") as f:
            s = f.read()
        body = s[s.index("apply_layout() {"):s.index("\n}\n", s.index("apply_layout() {"))]
        self.assertIn('swaymsg input "$TOUCH_INT" events enabled', body)


if __name__ == "__main__":
    unittest.main()
