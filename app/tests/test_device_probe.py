"""tools/device-probe.sh is a read-only report: it only asks sway questions, writes one file (its own output) and blanks serial
numbers and MAC addresses. Checked on the real script text."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_sw1_092 as sw1  # noqa: E402

PROBE = os.path.join(os.path.dirname(HERE), "tools", "device-probe.sh")


def text():
    with open(PROBE, encoding="utf-8") as f:
        return f.read()


def code_lines():
    return [ln for ln in text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


class TestStatic(unittest.TestCase):
    def test_every_sway_call_is_a_query(self):
        calls = [ln for ln in code_lines() if "swaymsg" in ln]
        self.assertGreaterEqual(len(calls), 4)
        for ln in calls:
            self.assertRegex(ln, r"swaymsg( -r)? -t get_", ln)

    def test_it_changes_nothing_on_the_device(self):
        forbidden = re.compile(r"\b(rm|mv|cp|kill|pkill|reboot|poweroff|systemctl|chmod|chown|echo\s+\S+\s*>>?\s*/(sys|proc|storage/\.config)|"
                               r"set_setting|swaymsg\s+(output|input|workspace|exec|seat|for_window|move|focus))\b")
        for ln in code_lines():
            self.assertIsNone(forbidden.search(ln), ln)

    def test_the_only_file_it_writes_is_its_own_report(self):
        writes = [ln for ln in code_lines() if re.search(r"(?<![0-9&])>\s*[^&\s]|\btee\b", ln) and "/dev/null" not in ln]
        for ln in writes:
            self.assertIn('tee "$OUT"', ln)
        self.assertIn("OUT=${PROBE_OUT:-/storage/rp5deck-probe.txt}", text())

    def test_it_reports_what_a_profile_needs(self):
        s = text()
        for need in ("get_outputs", "get_inputs", "get_seats", "get_tree", "/sys/class/drm", "/sys/class/backlight",
                     "device-tree/model", "screen_map.py --env", "lowerdeck", "map_to_output"):
            self.assertIn(need, s)

    def test_the_redaction_covers_mac_addresses_and_serials(self):
        m = re.search(r"sed -E '([^']+)'", text())
        self.assertIsNotNone(m)
        self.assertIn("xx:xx:xx:xx:xx:xx", m.group(1))
        self.assertIn("serial removed", m.group(1))


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestUnderDash(sw1.TmpDir):
    def test_the_script_parses(self):
        r = sw1.run_dash("dash -n %s" % sw1.sh_quote(sw1.to_sh_path(PROBE)))
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_the_redaction_works_on_a_sample(self):
        m = re.search(r"sed -E '([^']+)'", text())
        sample = "ps-controller-battery-" + ":".join(["aa", "bb", "cc", "dd", "ee", "ff"]) + " and Serial Number: ABC123|next"
        r = sw1.run_dash("echo %s | sed -E '%s'" % (sw1.sh_quote(sample), m.group(1)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("aa:bb:cc", r.stdout)
        self.assertNotIn("ABC123", r.stdout)
        self.assertIn("xx:xx:xx:xx:xx:xx", r.stdout)
        self.assertIn("|next", r.stdout)

    def test_with_no_sway_it_still_reports_and_says_so(self):
        d = self.mk()
        out = os.path.join(d, "out.txt")
        src = text().replace("OUT=${PROBE_OUT:-/storage/rp5deck-probe.txt}", "OUT=%s" % sw1.to_sh_path(out))
        script = os.path.join(d, "probe.sh")
        with open(script, "w", newline="\n") as f:
            f.write(src)
        stub = os.path.join(d, "bin")
        os.makedirs(stub)
        with open(os.path.join(stub, "swaymsg"), "w", newline="\n") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(stub, "swaymsg"), 0o755)
        T = sw1.to_sh_path(d)
        r = sw1.run_dash("chmod +x %s/bin/swaymsg; PATH=%s/bin:$PATH sh %s/probe.sh" % (sw1.sh_quote(T), sw1.sh_quote(T),
                                                                                         sw1.sh_quote(T)), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("== Command Center device probe ==", r.stdout)
        self.assertEqual(r.stdout.count("sway did not answer"), 4)      # outputs, inputs, seats and windows
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
