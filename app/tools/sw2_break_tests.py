"""Break proofs for SW2 (24 Sep 2026): each fix is undone in place, its test
must go red, then the file is restored byte-identical and __pycache__ is
cleared before every run (a same-size same-second swap can otherwise keep a
stale cached .pyc - see memory/mutation-test-stale-pyc.md).

  1. main.py's _on_watched_mode: the stale-event guard that drops a mode
     computed for an output pairing the surface has since moved away from
     (the real bug: ES's post-game blip on DSI-1, main.on_placement's own
     fresh recompute silently undone by a queued, stale ModeWatcher event).
  2. screen_swap.py's ScreenWatcher._apply_hold: the HOLD_S hold on an
     observed ES output that disagrees with the configured screen (stops
     the surface from ever chasing a transient blip in the first place).
  3. main.py's _reconcile: the in-app mode watchdog (a backstop for
     "stuck" - Main's follow-up request, 24 Sep).
  4. 094-rp5deck's heartbeat_monitor: the hang detector (a pid alive with a
     stale heartbeat gets SIGTERM'd/SIGKILL'd and restarted).

Run: python -B tools/sw2_break_tests.py (also under WSL: python3 -B
tools/sw2_break_tests.py - break 4 needs wsl.exe/Linux either way, since its
test shells out to run 094-rp5deck as a real process; break 1-3 are pure
Python and run identically on both platforms).
"""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("main.py",
     '''        if (internal, external) != (self.output, self.es_output):
            log.info("mode event dropped (stale): computed for %s/%s, now on %s/%s (%s)",
                     internal, external, self.output, self.es_output, reason)
            return
        self.on_mode(mode, reason)''',
     "        self.on_mode(mode, reason)",
     "tests.sw1_patched_cases.TestModeRaceFix"),
    ("screen_swap.py",
     "        if now - self._disagree_since >= self.hold_s:\n            return raw\n"
     "        return self.current",
     "        if now - self._disagree_since >= self.hold_s:\n            return raw\n"
     "        return raw",
     "tests.test_screen_swap.TestHold"),
    ("main.py",
     '''        log.warning("watchdog: mode %s should be %s (%s) - corrected", self.mode, mode, reason)
        self.reconcile_last = now
        self.reconcile_corrections += 1
        self.state_dirty = True
        self.on_mode(mode, "watchdog: %s" % reason)''',
     '''        log.warning("watchdog: mode %s should be %s (%s) - corrected", self.mode, mode, reason)
        self.reconcile_last = now
        self.reconcile_corrections += 1
        self.state_dirty = True''',
     "tests.sw1_patched_cases.TestReconcileWatchdog.test_the_stuck_bug_self_heals_via_the_watchdog_alone"),
    ("094-rp5deck",
     '        if [ "$age" -gt "$HB_STALE_SECS" ]; then',
     '        if false; then',
     "tests.test_supervisor.TestHeartbeatHangDetector.test_a_hung_but_alive_child_is_restarted"),
]


def _match_eol(anchor, text):
    """main.py is CRLF; screen_swap.py and 094-rp5deck are LF (mixed line
    endings across this repo - memory: CRLF breaks a literal-\\n anchor).
    Anchors above are written with plain \\n; adapt them to whichever the
    target file actually uses before searching/replacing."""
    if "\r\n" in text and "\r\n" not in anchor:
        return anchor.replace("\n", "\r\n")
    return anchor


def clear_caches():
    for root, dirs, _ in os.walk(APP):
        for d in dirs:
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
    good_e, bad_e = _match_eol(good, text), _match_eol(bad, text)
    if text.count(good_e) != 1:
        print("%d ANCHOR MISSING in %s (found %d times, need 1)" % (i, fn, text.count(good_e)))
        ok_all = False
        continue
    try:
        open(path, "wb").write(text.replace(good_e, bad_e).encode("utf-8"))
        passed_broken, last_b = run(mod)
    finally:
        open(path, "wb").write(orig)
    passed_restored, last_r = run(mod)
    same = open(path, "rb").read() == orig
    proven = (not passed_broken) and passed_restored and same
    ok_all &= proven
    print("%d %s  %-14s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
