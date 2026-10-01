"""I2 break proofs: the checks I2 added or moved while merging the patches
(24 Sep) must go RED when the thing they watch is broken, and GREEN again
once it is restored.

Every break runs in a temp COPY of this tree (never this tree), with -B.

  1 home grid stuck at 4 columns     -> the 9th tile has no hit target
  2 CC7 tile renamed                 -> test_cc7_merged (tile -> sheet)
  3 CC4 fill back to BLACK           -> test_cc4_merged (source + behaviour)
  4 CC4 system colour never applied  -> test_cc4_merged (theme behaviour)
  5 CC23 key dropped from WIRED      -> test_cc23_merged
  6 CC5 deferral removed (cc_overlay)-> test_cc5_merged: two CCs on one press
  7 SW1 Back taken from an open overlay -> test_sw1_merged
  8 094 socket search global again   -> test_supervisor "sway gone" cases,
    with a stray sway-ipc socket planted in /tmp (Linux only: the flaky
    case I2 fixed by scoping the search with RP5DECK_SWAY_SOCK_DIRS)

Run from rp5deck/:  python -B tools/i2_break_tests.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
LINUX = sys.platform.startswith("linux")

SUP = ("tests.test_supervisor.TestSupervisorIntegration."
       "test_sway_gone_pauses_respawn_then_resumes_with_the_new_socket_no_budget_spent")

BREAKS = [
    ("screens.py", "home grid stuck at 4 x 2 (9 tiles)",
     "        return max(HOME_MIN_COLS, -(-len(self.visible_tiles()) // self.grid_rows()))",
     "        return HOME_MIN_COLS",
     ["tests.test_cc1_merged"], False),
    ("screens.py", "CC7 Hotkeys tile renamed",
     'name="home.hotkeys", icon=icon_keyboard',
     'name="home.hotkeyz", icon=icon_keyboard',
     ["tests.test_cc7_merged"], False),
    ("companion.py", "CC4 background fill back to hard-coded BLACK",
     "        g.fill_rect(self.rect, self.bg_color)",
     "        g.fill_rect(self.rect, BLACK)",
     ["tests.test_cc4_merged"], False),
    ("companion.py", "CC4 system colour never reaches the view",
     '        self.set_bg_color(bg if self.mode == "system" else None)',
     "        self.set_bg_color(None)",
     ["tests.test_cc4_merged.TestControllerBehaviour"], False),
    ("config.py", "CC23 in_game_display dropped from WIRED",
     '    ("companion", "in_game_display"),  # companion _in_game_mode()',
     '    # ("companion", "in_game_display"),  # companion _in_game_mode()',
     ["tests.test_cc23_merged"], False),
    ("cc_overlay.py", "CC5: the overlay no longer defers to the panel's own CC",
     "        if panel_takes_summon(panel):",
     "        if False:",
     ["tests.test_cc5_merged"], False),
    ("main.py", "SW1: the panel opens its CC although the overlay's is open",
     "        if screen_swap.peer_open(overlay):\n            return",
     "        if False:\n            return",
     ["tests.test_sw1_merged"], False),
    ("command-center-app", "094 sway socket search global again (stray socket planted)",
     "    sock=$(find $SWAY_SOCK_DIRS -name",
     "    sock=$(find /run /tmp -name",
     [SUP], True),
]


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(tests, cwd):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest"] + tests,
                       capture_output=True, text=True, cwd=cwd)
    last = [ln for ln in r.stderr.splitlines() if ln.startswith(("OK", "FAILED", "Ran"))]
    return r.returncode, " ".join(last)


def prove(i, root, path, what, old, new, tests, stray):
    full = os.path.join(root, path)
    with open(full, "rb") as f:
        orig = f.read()
    text = orig.decode("utf-8")
    if text.count(old) != 1:
        print("%d SETUP-FAIL %s: anchor found %d times" % (i, what, text.count(old)))
        return False
    stray_dir = None
    if stray:
        stray_dir = tempfile.mkdtemp(prefix="i2-stray-", dir="/tmp")
        open(os.path.join(stray_dir, "sway-ipc.1.sock"), "w").close()
    try:
        try:
            with open(full, "wb") as f:
                f.write(text.replace(old, new).encode("utf-8"))
            rc_b, out_b = run(tests, root)
        finally:
            with open(full, "wb") as f:
                f.write(orig)
        rc_g, out_g = run(tests, root)      # restored, the stray socket still there
    finally:
        if stray_dir:
            shutil.rmtree(stray_dir, True)
    ok = rc_b != 0 and rc_g == 0
    print("%d %s  %-60s broken: %-26s restored: %s" % (
        i, "PROVEN" if ok else "NOT-PROVEN", what[:60], out_b[-26:], out_g))
    return ok


def main():
    before = {b[0]: sha(os.path.join(APP, b[0])) for b in BREAKS}
    tmp = tempfile.mkdtemp(prefix="i2-break-")
    good = skipped = 0
    try:
        root = os.path.join(tmp, "rp5deck")
        shutil.copytree(APP, root, ignore=shutil.ignore_patterns(
            "__pycache__", "screenshots", "proto", "es-upstream", "*.pyc"))
        for i, b in enumerate(BREAKS, 1):
            if b[5] and not LINUX:
                print("%d SKIPPED (Linux only)  %s" % (i, b[1]))
                skipped += 1
                continue
            good += prove(i, root, *b)
    finally:
        shutil.rmtree(tmp, True)
    after = {p: sha(os.path.join(APP, p)) for p in before}
    same = before == after
    print("this tree byte-identical after: %s" % same)
    print("%d/%d proven, %d skipped" % (good, len(BREAKS) - skipped, skipped))
    return 0 if (good == len(BREAKS) - skipped and same) else 1


if __name__ == "__main__":
    sys.exit(main())
