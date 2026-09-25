"""Break proofs for AH (YouTube TV app auto-hide, 24 Sep 2026): each behaviour
is undone IN PLACE in bar_autohide.py, its test must go red, then the file is
restored byte-identical. Pattern: tools/w_break_tests.py. Only bar_autohide.py
is ever touched here - never screens.py/main.py/config.py/sway_ipc.py/
wl_layer.py (patch-only, delivered as patches/AH-*.patch instead - see
patches/AH-NOTES.md for why this script cannot exercise those the same way).

  1. geometry() stops forcing zone 0 for the tv app while HIDDEN - the
     leanback window would get reflowed back down to 1920x940 every time the
     strip auto-hides, exactly the black-bars regression this feature fixes.
  2. geometry() stops forcing zone 0 for the tv app while SHOWN - same
     regression, the other state.
  3. on_mode() stops arming the timer on activation - the strip would never
     auto-hide at all.
  4. _on_timeout() stops flipping state to HIDDEN - the timer fires forever
     but the strip never actually goes away.
  5. touch() stops returning to SHOWN from HIDDEN - a tap on the handle (or
     a swipe up) would do nothing.
  6. touch() stops rearming the timeout - a tap on the SHOWN strip would not
     keep it open; it would hide on the ORIGINAL schedule regardless.
  7. wants() stops gating by app kind (always True) - Browser/Discord/mpv
     would lose their fixed 140 px reserved strip to this feature too.
  8. on_mode() stops deactivating when the bar app is no longer "tv" -
     leaving the YouTube app would leave the surface pinned at handle_h/0.

Run: python -B tools/ah_break_tests.py"""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("bar_autohide.py",
     "        size_h = self.handle_h if self.state == HIDDEN else default_h\n"
     "        return size_h, 0\n",
     "        size_h = self.handle_h if self.state == HIDDEN else default_h\n"
     "        return size_h, default_zone\n",
     "tests.test_bar_autohide.TestGeometryPerState.test_tv_hidden_is_handle_only_zone_zero"),
    ("bar_autohide.py",
     "        size_h = self.handle_h if self.state == HIDDEN else default_h\n"
     "        return size_h, 0\n",
     "        size_h = default_h\n"
     "        return size_h, 0\n",
     "tests.test_bar_autohide.TestGeometryPerState.test_tv_hidden_is_handle_only_zone_zero"),
    ("bar_autohide.py",
     "        if self.active:\n            if not was_active:\n                self.state = SHOWN\n"
     "            self._arm()\n",
     "        if self.active:\n            if not was_active:\n                self.state = SHOWN\n",
     "tests.test_bar_autohide.TestTimerHidesTheStrip.test_the_timer_hides_the_strip_after_timeout"),
    ("bar_autohide.py",
     "        self.state = HIDDEN\n        self._apply()\n\n    def _apply(self):",
     "        self._apply()\n\n    def _apply(self):",
     "tests.test_bar_autohide.TestTimerHidesTheStrip.test_the_timer_hides_the_strip_after_timeout"),
    ("bar_autohide.py",
     "        was_hidden = self.state == HIDDEN\n        self.state = SHOWN\n        self._arm()\n",
     "        was_hidden = self.state == HIDDEN\n        self._arm()\n",
     "tests.test_bar_autohide.TestTouch.test_a_tap_on_the_handle_shows_the_strip"),
    ("bar_autohide.py",
     "        was_hidden = self.state == HIDDEN\n        self.state = SHOWN\n        self._arm()\n"
     "        if was_hidden:\n            self._apply()",
     "        was_hidden = self.state == HIDDEN\n        self.state = SHOWN\n"
     "        if was_hidden:\n            self._apply()",
     "tests.test_bar_autohide.TestTouch."
     "test_a_tap_on_the_shown_strip_resets_the_timeout_without_reapplying_geometry"),
    ("bar_autohide.py",
     "        return app == TV_APP\n",
     "        return True  # AH break: no longer gated by app kind\n",
     "tests.test_bar_autohide.TestOtherAppsUnaffected."
     "test_entering_bar_with_web_never_activates_or_arms_a_timer"),
    ("bar_autohide.py",
     "        self.active = (new == sway_ipc.BAR) and self.wants(app)\n",
     "        self.active = (new == sway_ipc.BAR) or self.active\n",
     "tests.test_bar_autohide.TestLeavingRestoresNormalBehaviour."
     "test_mode_leaving_bar_deactivates_and_cancels_the_timer"),
]


def clear_caches():
    for root, dirs, _ in os.walk(APP):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


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
        print("%d ANCHOR MISSING/AMBIGUOUS in %s (count=%d)" % (i, fn, text.count(good)))
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
    print("%d %s  %-12s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
