"""Break proofs for the CM fixes (test day, 24 Sep 2026): each fix is undone
in place, its test must go red, then the file is restored byte-identical.

  Bug 1 (in-game "hud"/"slideshow" do nothing):
  1. set_active(True) no longer resumes the hud/slideshow timer loop after
     the Command Center/Settings sheet covers and uncovers the companion.
  2. "slideshow" mode goes back to arming a timer with no immediate art -
     the screen keeps showing whatever was there before "slideshow" was
     picked until the first tick resolves (and if that resolves to
     nothing - e.g. a system with no scraped slides - forever).
  3. the "manual falls back to art" hint text is skipped.

  Bug 2 (Settings -> Companion: "cartridge/boxback buttons are not there"):
  4. MediaPriorityPage's header goes blank - no sub-page says what it is
     or which ranks it covers.

Only touches companion.py / settings_view.py (this agent's own files) and
their tests, never SW2's or HK's modules.

Run: python -B tools/cm_break_tests.py   (Windows)
     python3 -B tools/cm_break_tests.py  (WSL/Linux)
"""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("companion.py",
     '            # the timer.\n'
     '            self._resume_ingame_mode()\n',
     '            # the timer.\n'
     '            pass  # (cm_break_tests: resume disabled)\n',
     "tests.test_companion.TestInGameDisplay.test_hud_loop_resumes_once_the_settings_sheet_closes"),
    ("companion.py",
     '            self.view.show_info(info, image, logo, lines, dim)\n'
     '            self._arm_ingame_timer(0.0, self._ingame_slide_tick)\n'
     '            return\n'
     '        if mode == "off":',
     '            self._arm_ingame_timer(0.0, self._ingame_slide_tick)\n'
     '            return\n'
     '        if mode == "off":',
     "tests.test_companion.TestInGameDisplay.test_slideshow_falls_back_to_art_when_the_system_has_no_slides"),
    ("companion.py",
     '        if raw == "manual":\n'
     '            # "manual" with no manual for this game',
     '        if False:  # (cm_break_tests: hint disabled)\n'
     '            # "manual" with no manual for this game',
     "tests.test_companion.TestInGameDisplay.test_manual_fallback_to_art_shows_a_hint_why"),
    ("settings_view.py",
     '        self.header.set_text("%s  (%d-%d of %d)" % (self.field["label"], lo, hi, n))\n',
     '        self.header.set_text("")\n',
     "tests.test_settings_view.TestMediaPriorityReorder"
     ".test_every_subpage_has_a_labelled_header_with_its_range"),
]


def clear_caches():
    for root, dirs, _ in os.walk(APP):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)


def run(mod):
    clear_caches()
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", mod], cwd=APP,
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr.strip().splitlines() or ["?"])[-1]


ok_all = True
for i, (fn, good, bad, mod) in enumerate(BREAKS, 1):
    path = os.path.join(APP, fn)
    orig = open(path, "rb").read()
    text = orig.decode("utf-8")
    if text.count(good) != 1:
        print("%d ANCHOR MISSING (count=%d) in %s" % (i, text.count(good), fn))
        ok_all = False
        continue
    try:
        open(path, "wb").write(text.replace(good, bad).encode("utf-8"))
        passed_broken, last_b = run(mod)
    finally:
        open(path, "wb").write(orig)
    passed_restored, last_r = run(mod)
    same = open(path, "rb").read() == orig
    proven = (not passed_broken) and passed_restored and same
    ok_all &= proven
    print("%d %s  %-16s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
