"""Break proofs for YT4 (24 Sep 2026): swipeable YouTube App (A), the Sleep
tile (B), and Safe Charge settings (C). Each fix is undone IN PLACE in one
of this wave's own files, its test must go red, then the file is restored
byte-identical. Pattern: tools/w_break_tests.py / tools/yt3_break_tests.py.

  A - swipe
   1. web_tiles.py: YtAppSession.on_mode() no longer arms the swipe poll
      when this tile's own window is what BAR shows - swiping would do
      nothing at all.
   2. web_tiles.py: on_mode() no longer cancels the swipe poll when a
      DIFFERENT app is shown (parked in the background) - it would keep
      polling (and sending key presses into) a hidden tab.
   3. web_tiles.py: close() no longer cancels the swipe poll immediately -
      a stray poll could still be in flight the instant Firefox quits.
   4. web_tiles.py: _direction_to_key() no longer applies the natural-swipe
      inversion - "natural" mode would send the raw compass direction
      instead, backwards from the phone-scroll convention it promises.
   5. browser.py: SWIPE_POLL_SCRIPT's own re-install marker check is
      dropped - a page that already has the listener would get a SECOND
      one on every poll (duplicate swipes, doubled step counts).

  B - Sleep tile
   6. main.py: open_sleep() drops its double-tap guard - a second tap
      during the delay/suspend call would queue a second suspend.
   7. main.py: _slept() no longer shows an error toast on failure - a
      failed suspend would look identical to a successful one.

  C - Safe charge
   8. charge_limit.py: apply() no longer reads the sysfs files back after
      writing - a kernel that silently clamped/rejected a value would be
      reported as a success.
   9. charge_limit.py: apply() writes START before END instead of after -
      095's own documented reason this matters (lowering END below the
      CURRENT start pulls start down as a kernel side effect) is defeated.
  10. main.py: _apply_charge_limit() no longer forces end=100 when the
      toggle is off - turning Safe charge off would silently keep
      whatever percentage the slider last showed.

Run: python -B tools/yt4_break_tests.py"""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("web_tiles.py",
     'mine = new == sway_ipc.BAR and self.browser.is_running() and \\\n'
     '            _reason_mentions(YTAPP_APP_ID, reason)\n        if mine:\n'
     '            self.ui.cc.set_bar_app("tv", keys_offered=False)\n'
     '            self._arm_swipe_poll()\n            return',
     'mine = new == sway_ipc.BAR and self.browser.is_running() and \\\n'
     '            _reason_mentions(YTAPP_APP_ID, reason)\n        if mine:\n'
     '            self.ui.cc.set_bar_app("tv", keys_offered=False)\n'
     '            return',
     "tests.test_web_tiles.TestYouTubeTvTile.test_mine_bar_entry_arms_the_swipe_poll"),
    ("web_tiles.py",
     "        self._cancel_swipe_poll()\n        # leaving BAR because of this tile closes nothing by itself",
     "        # leaving BAR because of this tile closes nothing by itself",
     "tests.test_web_tiles.TestYouTubeTvTile."
     "test_another_app_shown_in_bar_cancels_the_swipe_poll"),
    ("web_tiles.py",
     "        self.ui.cc.set_bar_app(None)\n        self._cancel_swipe_poll()\n"
     "        self.submit(self.browser.close, done=lambda _r=None: self._closed())",
     "        self.ui.cc.set_bar_app(None)\n"
     "        self.submit(self.browser.close, done=lambda _r=None: self._closed())",
     "tests.test_web_tiles.TestYouTubeTvTile.test_close_cancels_the_swipe_poll_immediately"),
    ("web_tiles.py",
     "        if self.swipe_natural:\n            direction = self._NATURAL_INVERT.get(direction, direction)",
     "        if False:\n            direction = self._NATURAL_INVERT.get(direction, direction)",
     "tests.test_web_tiles.TestYouTubeTvTile.test_natural_direction_inverts_the_axis"),
    ("browser.py",
     "if (!window.__rp5swipeInstalled) {",
     "if (true) {",
     "tests.test_browser.TestPageScripts.test_swipe_listener_only_installs_once"),
    ("main.py",
     "        if self.sleep_pending:\n            return\n        self.sleep_pending = True",
     "        self.sleep_pending = True",
     "tests.test_web_tiles.TestSleepTile.test_double_tap_is_a_no_op_while_pending"),
    ("main.py",
     '        if not ok:\n            log.warning("sleep: %s", detail)\n'
     '            if self.ui.bar.show_hint(("Could not sleep: %s" % detail)[:60]):\n'
     '                self.call_later(2.5, self.ui.bar.clear_hint)',
     '        if not ok:\n            log.warning("sleep: %s", detail)',
     "tests.test_web_tiles.TestSleepTile.test_failure_shows_an_error_toast_and_clears_pending"),
    ("charge_limit.py",
     '    live = read_live(end_path, start_path)\n'
     '    mismatch = (not start_ok) or live["end"] != end or (not off and live["start"] != start)\n'
     '    if mismatch:\n'
     '        return {"ok": False, "end": end, "start": start,\n'
     '               "detail": "kernel holds end=%s start=%s" % (live["end"], live["start"])}\n'
     '    return {"ok": True, "end": end, "start": start, "detail": ""}',
     '    return {"ok": True, "end": end, "start": start, "detail": ""}',
     "tests.test_charge_limit.TestApply.test_readback_mismatch_is_reported"),
    ("charge_limit.py",
     "    if not _write_int(end_path, end):\n"
     "        # the script's own fallback on a refused END: back to no limit instead of leaving the kernel on\n"
     "        # a stale unknown value\n"
     "        _write_int(end_path, 100)\n"
     '        return {"ok": False, "end": end, "start": start,\n'
     '               "detail": "kernel rejected end=%d" % end}\n'
     "    start_ok = _write_int(start_path, start)",
     "    start_ok = _write_int(start_path, start)\n"
     "    if not _write_int(end_path, end):\n"
     '        return {"ok": False, "end": end, "start": start,\n'
     '               "detail": "kernel rejected end=%d" % end}',
     "tests.test_charge_limit.TestApply.test_end_write_order_before_start"),
    ("main.py",
     '        enabled = config.get_value(self.cfg, ("battery", "safe_charge_enabled"))\n'
     '        pct = config.get_value(self.cfg, ("battery", "safe_charge_end_pct"))\n'
     '        end = pct if enabled else 100',
     '        enabled = config.get_value(self.cfg, ("battery", "safe_charge_enabled"))\n'
     '        pct = config.get_value(self.cfg, ("battery", "safe_charge_end_pct"))\n'
     '        end = pct',
     "tests.test_web_tiles.TestSafeChargeSettings.test_disabled_toggle_never_applies"),
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
    print("%d %s  %-16s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
