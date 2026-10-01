"""26 Sep (owner: move ROCKNIX's on-screen keyboard to the bottom screen): the daemon's
keep_keyboard_on_cc_screen closes a wvkbd sitting on the game screen while the game screen
has focus, so ROCKNIX's rocknix-touchscreen-keyboard loop restarts it on the other screen.
The real functions are cut out of dual-screen-layout-and-power and run in a shell with a
fake /proc, a stubbed focused output, and recorded kill/log calls."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "..", "..", "scripts", "dual-screen-layout-and-power")
def _which_bash():
    # Windows: prefer a real Git Bash by known install location and refuse the
    # WSL launcher shim at %LOCALAPPDATA%\Microsoft\WindowsApps\bash.exe, which
    # cannot open a Windows-style path given as a bare argv script name (see the
    # long explanation in test_supervisor._which_bash).
    if sys.platform == "win32":
        candidates = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
                candidates.append(os.path.join(base, "Git", "usr", "bin", "bash.exe"))
        candidates.append(r"C:\Program Files\Git\bin\bash.exe")
        candidates.append(r"C:\Program Files\Git\usr\bin\bash.exe")
        for c in candidates:
            if os.path.isfile(c):
                return c
        found = shutil.which("bash") or shutil.which("bash.exe")
        if found and "windowsapps" in found.lower():
            return None   # the WSL shim - unusable for a Windows-path argv
        return found
    return shutil.which("bash") or shutil.which("bash.exe")


SH = _which_bash()


def functions():
    with open(DAEMON, encoding="utf-8") as f:
        s = f.read()
    start = s.index("keyboard_output_of() {")
    end = s.index("dominant_for() {")
    return s[start:end]


def sh_path(p):
    p = os.path.abspath(p).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    return "/%s/%s" % (m.group(1).lower(), m.group(2)) if m and os.name == "nt" else p


@unittest.skipUnless(SH, "needs a POSIX shell")
class KeyboardScreen(unittest.TestCase):
    def run_case(self, state, game_out, kb_output, focused, pids=("4278",)):
        d = tempfile.mkdtemp(prefix="kb-")
        self.addCleanup(shutil.rmtree, d, True)
        for pid in pids:
            os.makedirs(os.path.join(d, pid))
            argv = ["/usr/bin/wvkbd-mobintl", "-L", "720", "-l", "simple", "--hidden"]
            if kb_output:
                argv += ["--output", kb_output]
            with open(os.path.join(d, pid, "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        script = "\n".join([
            'EXTERNAL=DP-1; INTERNAL=DSI-1; KB_PROC="%s"' % sh_path(d),
            'log() { echo "LOG $*"; }',
            'kill() { echo "KILL $*"; }',
            'pidof() { echo "%s"; }' % " ".join(pids),
            functions(),
            'focused_output() { echo "%s"; }' % focused,      # after the real one: the stub wins
            'keep_keyboard_on_cc_screen "%s" "%s"' % (state, game_out),
        ])
        r = subprocess.run([SH, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_keyboard_on_the_game_screen_is_closed(self):
        out = self.run_case("dual", "DP-1", "DP-1", focused="DP-1")
        self.assertIn("KILL 4278", out)
        self.assertIn("restarts it on DSI-1", out)

    def test_keyboard_already_on_the_command_center_screen_is_left(self):
        self.assertEqual(self.run_case("dual", "DP-1", "DSI-1", focused="DP-1"), "")

    def test_not_while_the_command_center_screen_has_focus(self):
        # a restart now would pick the game screen again
        self.assertEqual(self.run_case("dual", "DP-1", "DP-1", focused="DSI-1"), "")

    def test_nothing_outside_dual_mode(self):
        self.assertEqual(self.run_case("single", "DSI-1", "DP-1", focused="DSI-1"), "")

    def test_swapped_screens_use_the_other_side(self):
        # ES and games on the built-in panel: the keyboard belongs on the add-on
        out = self.run_case("dual", "DSI-1", "DSI-1", focused="DSI-1")
        self.assertIn("KILL 4278", out)
        self.assertIn("restarts it on DP-1", out)
        self.assertEqual(self.run_case("dual", "DSI-1", "DP-1", focused="DSI-1"), "")

    def test_a_keyboard_without_an_output_is_left(self):
        self.assertEqual(self.run_case("dual", "DP-1", None, focused="DP-1"), "")

    def test_the_loop_calls_it_with_the_game_screen(self):
        with open(DAEMON, encoding="utf-8") as f:
            s = f.read()
        self.assertIn('keep_keyboard_on_cc_screen "$state" "$(dominant_for "$state")"', s)


if __name__ == "__main__":
    unittest.main()
