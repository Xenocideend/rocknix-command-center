"""CC6 break proofs: each mutation of the safety logic must turn its tests
RED, and the restored file must turn them GREEN again.

  * IN PLACE: window_switcher.py / app_tabs.py (CC6's own files) are
    mutated in this tree; their sha256 is checked before and after.
  * MERGED (was PATCHED): CC6's code in files other tasks own (web_tiles'
    `parked`, focus_guard's parked skip, main's after_mode hook). I2 merged
    patches/CC6-*.patch into the real files on 24 Sep, so these breaks now
    run in a temp COPY of this merged tree (no patching), never touching it.

Run from rp5deck/:  python -B tools/cc6_break_tests.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "tests"))

WS = "tests.test_window_switcher"
TABS = "tests.test_app_tabs"
CASES = "tests.cc6_patched_cases"

IN_PLACE = [
    ("window_switcher.py", "allow-list lets any part through",
     '        raise CommandRefused("part %r is not on the allow-list" % part)',
     "        continue",
     [WS + ".TestAllowList.test_arbitrary_commands_are_refused",
      WS + ".TestRealWire.test_run_command_refuses_before_a_byte_is_sent"]),
    ("window_switcher.py", "EmulationStation becomes movable",
     "    if aid == ES_APP_ID:\n        return None\n    if aid == WEB_APP_ID:",
     "    if aid == WEB_APP_ID:",
     [WS + ".TestRecognise.test_never_es_the_game_or_anything_unknown",
      WS + ".TestAllowList.test_never_moves_es_the_game_or_an_unknown_window"]),
    ("window_switcher.py", "any title counts as an emulator second window",
     "    if _SECOND_RE.search(_second_title(node.get(\"name\"))):",
     "    if True:",
     [WS + ".TestRecognise.test_never_es_the_game_or_anything_unknown",
      WS + ".TestAllowList.test_never_moves_es_the_game_or_an_unknown_window"]),
    ("window_switcher.py", "show may target the game's screen",
     "            if out != cc_output or out == es_output:",
     "            if False:",
     [WS + ".TestAllowList.test_show_only_from_the_parking_workspace_to_the_cc_screen"]),
    ("window_switcher.py", "park a window that is not on the CC screen",
     "            if w is None or w.where != SHOWN:",
     "            if w is None:",
     [WS + ".TestAllowList.test_park_only_what_is_shown_on_the_cc_screen"]),
    ("window_switcher.py", "focus may be sent to mpv",
     "            if str(target[0].get(\"app_id\") or \"\") == YT_APP_ID:",
     "            if False:",
     [WS + ".TestAllowList.test_focus_rules"]),
    ("window_switcher.py", "mode: showing Firefox leaves the emulator window up",
     "        p.park = [s for s in shown if s.kind != kind]",
     "        p.park = []",
     [WS + ".TestSwitchInTheSim.test_browser_over_a_ds_game"]),
    ("window_switcher.py", "no focus hand-back after a park",
     "    else:\n        p.focus = snap.home  # it was parked",
     "    else:\n        p.focus = None  # it was parked",
     [WS + ".TestSwitchInTheSim.test_touched_second_window_had_focus_then_focus_goes_to_the_game",
      WS + ".TestSwitchInTheSim.test_firefox_had_focus_parking_gives_it_back_to_es"]),
    ("window_switcher.py", "an unknown window on the screen is ignored, not refused",
     "    if snap.foreign_shown:\n        p.refused",
     "    if False:\n        p.refused",
     [WS + ".TestSwitchInTheSim.test_unknown_window_on_the_cc_screen_refuses_everything_and_sends_nothing"]),
    ("window_switcher.py", "undocked layouts are not refused",
     "        self.problem = \"undocked: %s absent\" % es_output",
     "        pass",
     [WS + ".TestSwitchInTheSim.test_undocked_or_bad_layout_refuses"]),
    ("window_switcher.py", "sway's answer is not verified",
     "    wrong = []\n    for w in p.park:",
     "    return []\n    wrong = []\n    for w in p.park:",
     [WS + ".TestSwitchInTheSim.test_sway_saying_no_or_lying_is_an_error_not_ok"]),
    ("app_tabs.py", "active tab stored instead of derived (always Companion)",
     '    """What the bottom screen shows right now, as a tab id (or None)."""',
     '    """What the bottom screen shows right now, as a tab id (or None)."""\n'
     '    return COMPANION',
     [TABS + ".TestActiveTab.test_follows_what_is_on_screen",
      TABS + ".TestWhichTabsExist.test_emulator_tab_only_while_its_second_window_exists"]),
    ("app_tabs.py", "tabs for apps that can neither run nor start",
     '    if web_running or avail.get("web"):',
     "    if True:",
     [TABS + ".TestWhichTabsExist.test_nothing_running_nothing_installed"]),
]

PATCHED = [
    ("web_tiles.py", "a parked Firefox is treated as closed (session ends)",
     "        if self.parked:\n            return  # parked by the app tabs",
     "        if False:\n            return  # parked by the app tabs",
     [CASES + ".TestFirefoxAndMpv.test_bar_tabs_parks_firefox_it_keeps_running_and_comes_back",
      CASES + ".TestFirefoxAndMpv.test_parked_flag_is_set_before_sway_reports_full"]),
    ("focus_guard.py", "focus_guard may focus a parked window",
     "        if not _is_view(n) or n.get(\"id\") in parked:",
     "        if not _is_view(n):",
     [CASES + ".TestEmulatorSecondWindow.test_focus_guard_never_picks_a_parked_window"]),
    ("app_tabs.py", "follow-up runs before the promised mode (CC5 undoes it)",
     "        if expect is None or app.mode == expect or (expect == HIDDEN and app.mode == OVERLAY):",
     "        if True:",
     [CASES + ".TestEmulatorSecondWindow.test_ds_screen_to_hud_and_back_keeps_mode_and_focus_consistent"]),
    ("app_tabs.py", "web_tiles not told before the park",
     "        if target != web.app:\n            web.parked = True",
     "        if False:\n            web.parked = True",
     [CASES + ".TestFirefoxAndMpv.test_parked_flag_is_set_before_sway_reports_full"]),
    ("main.py", "main.on_mode does not call tabs.after_mode",
     "            self.tabs.after_mode(old, mode, reason)  # a tab's follow-up",
     "            pass  # a tab's follow-up",
     [CASES + ".TestEmulatorSecondWindow.test_ds_screen_to_hud_and_back_keeps_mode_and_focus_consistent"]),
    ("app_tabs.py", "a window 092 moved back is not parked again",
     "        if back:\n            it[\"budget\"] -= 1",
     "        if False:\n            it[\"budget\"] -= 1",
     [CASES + ".TestSafety.test_a_window_092_moves_back_is_parked_again_once"]),
]


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(tests, cwd):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest"] + tests,
                       capture_output=True, text=True, cwd=cwd)
    last = [ln for ln in r.stderr.splitlines() if ln.startswith(("OK", "FAILED", "Ran"))]
    return r.returncode, " ".join(last)


def prove(i, root, path, what, old, new, tests):
    full = os.path.join(root, path)
    with open(full, "rb") as f:
        orig = f.read()
    text = orig.decode("utf-8")
    if text.count(old) != 1:
        print("%2d SETUP-FAIL %s: anchor found %d times" % (i, what, text.count(old)))
        return False
    try:
        with open(full, "wb") as f:
            f.write(text.replace(old, new).encode("utf-8"))
        rc_b, out_b = run(tests, root)
    finally:
        with open(full, "wb") as f:
            f.write(orig)
    rc_g, out_g = run(tests, root)
    ok = rc_b != 0 and rc_g == 0
    print("%2d %s  %-56s broken: %-24s restored: %s" % (
        i, "PROVEN" if ok else "NOT-PROVEN", what[:56], out_b[-24:], out_g))
    return ok


def main():
    files = sorted({b[0] for b in IN_PLACE})
    before = {p: sha(os.path.join(APP, p)) for p in files}
    good = 0
    for i, b in enumerate(IN_PLACE, 1):
        good += prove(i, APP, *b)
    after = {p: sha(os.path.join(APP, p)) for p in files}
    same = before == after
    tmp = tempfile.mkdtemp(prefix="cc6-break-")
    try:
        root = os.path.join(tmp, "rp5deck")
        shutil.copytree(APP, root, ignore=shutil.ignore_patterns(
            "__pycache__", "screenshots", "proto", "es-upstream", "*.pyc"))
        print("-- copy of the merged tree (I2)")
        for i, b in enumerate(PATCHED, len(IN_PLACE) + 1):
            good += prove(i, root, *b)
    finally:
        shutil.rmtree(tmp, True)
    total = len(IN_PLACE) + len(PATCHED)
    print("own sources byte-identical after: %s (%s)" % (same, ", ".join(files)))
    print("%d/%d proven" % (good, total))
    return 0 if (good == total and same) else 1


if __name__ == "__main__":
    sys.exit(main())
