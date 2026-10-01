"""W1: rocknix-config/dual-screen-layout-and-power keeps every emulator's second
window visible and every game window on the game screen, on every poll.

Device, 25 Sep:
  16:07 add-on unplugged: Cemu's Wind Waker HD froze on "Loading..." - its
        "GamePad View" sat behind the fullscreen game on DSI-1 (no frame
        callbacks for a covered Vulkan window; Cemu draws both in one loop).
        Un-fullscreening the game resumed it at once.
  16:22 add-on replugged mid-game: 092 logged single -> dual but the game
        stayed on DSI-1 and the pad window, renamed "GamePad View - FPS: 2.69",
        no longer matched the end-anchored pattern - frozen and "flipped".
The Python in the WINFIX_PY heredoc is read from the script (never a copy),
run as-is, and checked against sway trees shaped like the device's.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import window_switcher  # noqa: E402

REPO = os.path.dirname(os.path.dirname(APP))
PATH_092 = os.environ.get("SW1_092_PATH") or os.path.join(
    os.path.dirname(APP), "scripts", "dual-screen-layout-and-power")
PY_START = "WINFIX_PY=$(cat <<'PYEOF'\n"
PY_END = "\nPYEOF\n)"
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


BASH = _which_bash()


def read_092():
    with open(PATH_092, encoding="utf-8", newline="") as f:
        return f.read()


def winfix_source(text):
    a = text.index(PY_START) + len(PY_START)
    return text[a:text.index(PY_END, a)]


def load_winfix():
    ns = {"__name__": "winfix"}
    exec(compile(winfix_source(read_092()), "WINFIX_PY", "exec"), ns)
    return ns


def bash_unescape(s):
    return re.sub(r'\\([\\"$`])', r"\1", s)


# --- sway tree builder ------------------------------------------------------
_ids = [100]


def view(title, pid, app_id="info.cemu.Cemu", fullscreen=0, floating=False):
    _ids[0] += 1
    return {"id": _ids[0], "type": "floating_con" if floating else "con", "name": title,
            "pid": pid, "app_id": app_id, "fullscreen_mode": fullscreen, "nodes": [],
            "floating_nodes": []}


def ws(name, *views):
    tiled = [v for v in views if v["type"] == "con"]
    fl = [v for v in views if v["type"] == "floating_con"]
    return {"id": 0, "type": "workspace", "name": name, "nodes": tiled, "floating_nodes": fl}


def output(name, *workspaces):
    return {"id": 0, "type": "output", "name": name, "nodes": list(workspaces), "floating_nodes": []}


def tree(*outputs):
    scratch = output("__i3", ws("__i3_scratch"))
    return {"id": 1, "type": "root", "name": "root", "nodes": [scratch] + list(outputs),
            "floating_nodes": []}


GAME_T = "Cemu 6f6c129 - FPS: 30.09 [Vulkan] [Generic] [TitleId: 00050000-10143500] The Wind Waker HD [US v0]"
PAD_T = "GamePad View - FPS: 2.69"
ES = lambda: view("EmulationStation", 900, app_id="emulationstation")


class TestSecondWindowPattern(unittest.TestCase):
    """One pattern, four copies: the WINFIX Python, 092's three swaymsg title
    criteria, and the Command Center's window switcher."""

    @classmethod
    def setUpClass(cls):
        cls.text = read_092()
        cls.rx = load_winfix()["SECOND"]

    def test_all_copies_are_the_same_pattern(self):
        crit = re.findall(r'\[title=\\"(\(\^\|.+?)\\"\]', self.text)
        shell = sorted({bash_unescape(c) for c in crit if "GamePad" in c})
        self.assertEqual(len([c for c in crit if "GamePad" in c]), 3, crit)
        self.assertEqual(shell, [self.rx.pattern])
        self.assertEqual(window_switcher.SECOND_WINDOW_TITLE, self.rx.pattern)

    def test_titles(self):
        yes = ["GamePad View", "GamePad View - FPS: 2.69", "GamePad View - FPS: 30.03",
               "Azahar fbd3fb0 | Ocarina of Time 3D | Secondary Window", "Secondary Window",
               "DSperate (Bottom)", "[w2] melonDS 1.1"]
        no = [GAME_T, "Cemu 6f6c129 - Loading...", "Cemu 2.0 - GamePad View",
              "Azahar fbd3fb0 | Ocarina of Time 3D", "DSperate (Top)", "[w1] melonDS 1.1",
              "EmulationStation", "Secondary Window 2", "GamePad View - FPS: fast"]
        for t in yes:
            self.assertTrue(self.rx.search(t), t)
        for t in no:
            self.assertFalse(self.rx.search(t), t)


class TestPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = staticmethod(load_winfix()["plan"])

    def cmds(self, t, mode, game="DP-1", cc="DSI-1"):
        return [c for _id, c, _m in self.plan(t, mode, game, cc)]

    def test_single_fullscreen_game_hides_the_pad(self):
        # 16:07: both on DSI-1, the game fullscreen
        g, p = view(GAME_T, 42, fullscreen=1), view(PAD_T, 42)
        c = self.cmds(tree(output("DSI-1", ws("1", g, p))), "single", "DSI-1", "DSI-1")
        self.assertEqual(c, ["[con_id=%d] fullscreen disable" % g["id"],
                             "[con_id=%d] resize set width 30 ppt" % p["id"],
                             "[con_id=%d] focus" % g["id"]])

    def test_single_pad_on_another_workspace_joins_the_game(self):
        g, p = view(GAME_T, 42), view("GamePad View", 42)
        c = self.cmds(tree(output("DSI-1", ws("2", g), ws("1", p))), "single", "DSI-1", "DSI-1")
        self.assertIn("[con_id=%d] move container to workspace 2" % p["id"], c)
        self.assertEqual(c[-1], "[con_id=%d] focus" % g["id"])

    def test_single_already_visible_does_nothing(self):
        # after the fix: tiled side by side, not fullscreen - must not loop
        g, p = view(GAME_T, 42), view(PAD_T, 42)
        self.assertEqual(self.cmds(tree(output("DSI-1", ws("1", g, p))), "single",
                                   "DSI-1", "DSI-1"), [])

    def test_single_floating_pad_needs_only_the_fullscreen_off(self):
        g, p = view(GAME_T, 42, fullscreen=1), view(PAD_T, 42, floating=True)
        c = self.cmds(tree(output("DSI-1", ws("1", g, p))), "single", "DSI-1", "DSI-1")
        self.assertEqual(c, ["[con_id=%d] fullscreen disable" % g["id"],
                             "[con_id=%d] focus" % g["id"]])

    def test_dual_flipped_after_replug_is_put_back(self):
        # 16:22-16:27: game on DSI-1, pad (renamed) moved to DP-1; ES on DP-1
        g, p = view(GAME_T, 42, fullscreen=1), view(PAD_T, 42)
        t = tree(output("DSI-1", ws("2", g)), output("DP-1", ws("1", ES(), p)))
        c = self.cmds(t, "dual", "DP-1", "DSI-1")
        self.assertEqual(c, ["[con_id=%d] move container to output DSI-1" % p["id"],
                             "[con_id=%d] move container to output DP-1" % g["id"],
                             "[con_id=%d] focus" % g["id"]])

    def test_dual_swapped_targets_are_honoured(self):
        # SW1 swapped: games on the built-in panel, second windows on the add-on
        g, p = view(GAME_T, 42), view(PAD_T, 42)
        t = tree(output("DSI-1", ws("1", g, p)), output("DP-1", ws("2")))
        self.assertEqual(self.cmds(t, "dual", "DSI-1", "DP-1"),
                         ["[con_id=%d] move container to output DP-1" % p["id"]])

    def test_dual_correct_layout_does_nothing(self):
        g, p = view(GAME_T, 42, fullscreen=1), view(PAD_T, 42)
        t = tree(output("DSI-1", ws("2", p)), output("DP-1", ws("1", g)))
        self.assertEqual(self.cmds(t, "dual"), [])

    def test_single_window_game_untouched_in_single_mode(self):
        ra = view("RetroArch", 7, app_id="com.libretro.RetroArch", fullscreen=1)
        web = view("Discord", 8, app_id="rp5deck-web")
        t = tree(output("DSI-1", ws("1", ra, web)))
        self.assertEqual(self.cmds(t, "single", "DSI-1", "DSI-1"), [])

    def test_dual_single_window_game_on_the_cc_panel_goes_to_the_game_screen(self):
        """26 Sep: NeoCD/Saturn/32X games mapped fullscreen on DSI-1 (ES had closed its
        window, focus fell to workspace 2) and the top screen showed nothing."""
        ra = view("RetroArch NeoCD 2022", 7, app_id="com.libretro.RetroArch", fullscreen=1)
        web = view("Discord", 8, app_id="rp5deck-web", fullscreen=1)
        t = tree(output("DSI-1", ws("2", ra, web)), output("DP-1", ws("1")))
        self.assertEqual(self.cmds(t, "dual"),
                         ["[con_id=%d] move container to output DP-1" % ra["id"],
                          "[con_id=%d] focus" % ra["id"]])

    def test_dual_single_window_game_left_alone_when_screens_are_swapped(self):
        ra = view("RetroArch", 7, app_id="com.libretro.RetroArch", fullscreen=1)
        t = tree(output("DSI-1", ws("1", ra)), output("DP-1", ws("2")))
        self.assertEqual(self.cmds(t, "dual", "DSI-1", "DP-1"), [])

    def test_dual_non_fullscreen_or_es_windows_are_not_moved(self):
        ra = view("RetroArch", 7, app_id="com.libretro.RetroArch", fullscreen=0)
        es = ES()
        es["fullscreen_mode"] = 1
        t = tree(output("DSI-1", ws("2", ra, es)), output("DP-1", ws("1")))
        self.assertEqual(self.cmds(t, "dual"), [])

    def test_azahar_pair_is_matched_by_pid(self):
        g = view("Azahar fbd3fb0 | Ocarina of Time 3D", 55, app_id="org.azahar_emu.Azahar", fullscreen=1)
        p = view("Azahar fbd3fb0 | Ocarina of Time 3D | Secondary Window", 55,
                 app_id="org.azahar_emu.Azahar")
        other = view(GAME_T, 42, fullscreen=0)     # a different emulator: not its game
        c = self.cmds(tree(output("DSI-1", ws("1", other, g, p))), "single", "DSI-1", "DSI-1")
        self.assertEqual(c[0], "[con_id=%d] fullscreen disable" % g["id"])
        self.assertNotIn("[con_id=%d]" % other["id"], " ".join(c))

    def test_scratchpad_windows_are_ignored(self):
        g = view(GAME_T, 42, fullscreen=1)
        t = tree(output("DSI-1", ws("1", g)))
        t["nodes"][0]["nodes"][0]["floating_nodes"].append(view(PAD_T, 42, floating=True))
        self.assertEqual(self.cmds(t, "single", "DSI-1", "DSI-1"), [])


class TestLoopGuard(unittest.TestCase):
    """The script's main(): commands go out as CMD lines, and a window that
    keeps undoing the layout is given up on after WINFIX_MAX fixes."""

    def run_main(self, t, state):
        return subprocess.run([sys.executable, "-c", winfix_source(read_092()),
                               "single", "DSI-1", "DSI-1", state],
                              input=json.dumps(t), capture_output=True, text=True, timeout=30).stdout

    def test_gives_up_after_the_limit(self):
        g, p = view(GAME_T, 42, fullscreen=1), view(PAD_T, 42)
        t = tree(output("DSI-1", ws("1", g, p)))
        mx = load_winfix()["WINFIX_MAX"]
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "s.json")
            outs = [self.run_main(t, state) for _ in range(mx + 2)]
        for o in outs[:mx]:
            self.assertIn("CMD [con_id=%d] fullscreen disable" % g["id"], o)
        self.assertNotIn("CMD [con_id=%d] fullscreen disable" % g["id"], outs[mx])
        self.assertIn("giving up", outs[mx])
        self.assertEqual(outs[mx + 1].strip(), "", outs[mx + 1])

    def test_bad_json_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, "-c", winfix_source(read_092()),
                                "single", "DSI-1", "DSI-1", os.path.join(d, "s.json")],
                               input="", capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout, "")


class TestLoopCallsIt(unittest.TestCase):
    """map-time rules miss renamed and pre-existing windows, so the fix must
    run on every steady-state poll AND right after a single/dual change."""

    def test_both_call_sites(self):
        loop = read_092()
        loop = loop[loop.index("\nwhile true; do\n"):]
        steady = loop[:loop.index('        if [ "$state" = "$candidate" ]; then')]
        trans = loop[loop.index('        if [ "$confirm" -ge "$CONFIRM_NEEDED" ]; then'):]
        self.assertRegex(steady, r'park_es "\$\(dominant_for "\$state"\)"\n(\s*#.*\n)*\s*fix_game_windows "\$state"\n')
        # 26 Sep: sync_dual_flag (ROCKNIX's live dual-screen flag) may follow it
        self.assertRegex(trans, r'fix_game_windows "\$state"\n(\s*sync_dual_flag "\$state"\n)?\s*last="\$state"')


@unittest.skipUnless(BASH, "bash needed")
class TestShellWiring(unittest.TestCase):
    """The heredoc survives bash's $( ) intact, and fix_game_windows sends
    exactly the CMD lines to swaymsg with the right screens."""

    def shell(self, body):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.sh")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            return subprocess.run([BASH, p.replace("\\", "/")], capture_output=True, text=True,
                                  timeout=60).stdout

    def extract(self, text):
        a = text.index("WINFIX_STATE=")
        b = text.index("\n}\n", text.index("fix_game_windows() {", a)) + 3
        return text[a:b]

    def test_heredoc_value_is_the_python_verbatim(self):
        text = read_092()
        out = self.shell(self.extract(text) + '\nprintf "%s" "$WINFIX_PY"\n')
        self.assertEqual(out, winfix_source(text))

    def test_fix_game_windows_uses_internal_without_cc_output_for(self):
        text = read_092()
        g, p = view(GAME_T, 42), view(PAD_T, 42)
        t = tree(output("DSI-1", ws("2", g)), output("DP-1", ws("1", p)))
        py = sys.executable.replace("\\", "/")
        stub = ('INTERNAL=DSI-1; EXTERNAL=DP-1\n'
                'python3() { "%s" "$@"; }\n'
                'log() { echo "LOGGED $*"; }\n'
                'dominant_for() { [ "$1" = dual ] && echo DP-1 || echo DSI-1; }\n'
                # 092 sends swaymsg's own output to /dev/null: record to a file
                'swaymsg() { if [ "$1" = -t ]; then cat "$TREE"; else echo "SWAY $*" >> "$CMDS"; fi; }\n'
                % py)
        with tempfile.TemporaryDirectory() as d:
            tp = os.path.join(d, "tree.json")
            cmds = os.path.join(d, "cmds.log")
            with open(tp, "w") as f:
                json.dump(t, f)
            body = ("TREE='%s'\nCMDS='%s'\n" % (tp.replace("\\", "/"), cmds.replace("\\", "/"))
                    + stub + self.extract(text)
                    + "\nWINFIX_STATE='%s'\nfix_game_windows dual\n" % os.path.join(d, "s").replace("\\", "/"))
            out = self.shell(body)
            with open(cmds) as f:
                out += f.read()
        self.assertIn("SWAY [con_id=%d] move container to output DSI-1" % p["id"], out)
        self.assertIn("SWAY [con_id=%d] move container to output DP-1" % g["id"], out)
        self.assertIn("LOGGED windows:", out)


if __name__ == "__main__":
    unittest.main()
