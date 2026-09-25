"""Break proofs for RG (thumb-stick RGB): each safety property is undone in
rgb_leds.py IN PLACE, its test must go red, then the file is restored
byte-identical. Pattern: tools/td_fix_break_tests.py. Only rgb_leds.py is
ever touched here - never another agent's break-test script, never another
agent's module.

  1. apply() sends (left, right) instead of ledcontrol's own (right, left)
  2. apply() no longer hands off in "rocknix" mode
  3. build_argv() drops its allow-list validation (bool/out-of-range/
     non-int arguments would reach subprocess)
  4. Controller.keeper_tick() no longer skips "rocknix" mode
  5. Controller.keeper_tick() never gives up (would spam ledcontrol forever
     against a ROCKNIX that keeps winning the tug-of-war)

Run: python -B tools/rg_break_tests.py   (Windows or WSL/python3 - no
device, no cairo needed for any of these five)."""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("rgb_leds.py",
     "        argv = build_argv(state.brightness, state.right, state.left)\n",
     "        argv = build_argv(state.brightness, state.left, state.right)\n",
     "tests.test_rgb_leds.TestApply.test_colour_mode_right_first"),
    ("rgb_leds.py",
     "    if state.mode == MODE_ROCKNIX:\n        return None\n",
     "    if False:\n        return None\n",
     "tests.test_rgb_leds.TestApply.test_rocknix_mode_runs_nothing"),
    ("rgb_leds.py",
     "    vals = [_validate_byte_arg(v) for v in vals]\n",
     "    vals = list(vals)\n",
     "tests.test_rgb_leds.TestBuildArgv"),
    ("rgb_leds.py",
     "        if self.state.mode == MODE_ROCKNIX:\n            return \"rocknix\"\n",
     "        if False:\n            return \"rocknix\"\n",
     "tests.test_rgb_leds.TestKeeper.test_never_acts_in_rocknix_mode"),
    ("rgb_leds.py",
     "        if self._fail_streak > self.max_tries:\n",
     "        if False:\n",
     "tests.test_rgb_leds.TestKeeper.test_gives_up_after_max_tries_and_logs_once"),
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
    print("%d %s  %-8s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
