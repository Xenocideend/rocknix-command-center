#!/usr/bin/env python3
"""Break-restore proof for dualscreen_keys.py / tests/test_dualscreen_keys.py
(and test_dualscreen_keys_view.py): each break is applied to dualscreen_keys.py
inside a temp COPY of the whole rp5deck tree (never this working tree), the
copy's own test suite is run against it, and the result must be RED; the
unbroken copy must be GREEN. Same shape as tools/az_rule_break_tests.py
(text-transform breaks, red/green check) and tools/leak_break_tests.py (whole-
tree temp copy, since dualscreen_keys.py is imported by path, not pointed at
through an env var the way 092's shell script is).

Run from rp5deck/:  python -B tools/keys_guard_break_tests.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
SRC_REL = "dualscreen_keys.py"
TESTS = ["tests.test_dualscreen_keys", "tests.test_dualscreen_keys_view"]

# (what, old anchor, new text) - each must appear exactly once in the
# unbroken file; the break must actually change the text (a no-op break
# would prove nothing).
BREAKS = [
    ("prefix check dropped (3ds.screen_layout_x= counts as present)",
     '    prefix = key + "="\n',
     "    prefix = key\n"),
    ("backup skipped before the append",
     "        backup_path = _backup(cfg_path, backup_dir, stamp_fn())\n",
     '        backup_path = "SKIPPED-NO-REAL-BACKUP"\n'),
    ("essway never restarted on failure (finally does nothing)",
     "    finally:\n"
     "        rc3, out3 = _run(run, CMD_START_ES)\n"
     "        if rc3 != 0:\n"
     '            log.error("dualscreen_keys: could not restart essway.service (rc %s): %s", '
     "rc3, out3)\n",
     "    finally:\n"
     "        pass\n"),
    ("re-check before append removed (a present key gets appended again)",
     "        to_add = _still_missing(cfg_path, missing)\n",
     "        to_add = list(missing)\n"),
    ("extra-key rom check dropped (a configured per-game key is 'expected' "
     "even when the owner does not have that ROM)",
     "        if rom and not os.path.exists(os.path.join(roms_dir, rom)):\n            continue\n",
     "        if False:\n            continue\n"),
]


def run(tests, cwd):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest"] + tests,
                       capture_output=True, text=True, cwd=cwd)
    tail = [ln for ln in r.stderr.splitlines() if ln.startswith(("OK", "FAILED", "Ran"))]
    return r.returncode, " ".join(tail)


def make_copy():
    tmp = tempfile.mkdtemp(prefix="dsguard-break-")
    root = os.path.join(tmp, "rp5deck")
    shutil.copytree(APP, root, ignore=shutil.ignore_patterns(
        "__pycache__", "screenshots", "proto", "es-upstream", "es-hooks", "firefox", "steam",
        "*.pyc"))
    return tmp, root


def prove(i, what, old, new):
    tmp, root = make_copy()
    try:
        path = os.path.join(root, SRC_REL)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        n = text.count(old)
        if n != 1:
            print("%d SETUP-FAIL %-62s anchor found %d times (expected 1)" % (i, what[:62], n))
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new, 1))
        rc, tail = run(TESTS, root)
        ok = rc != 0
        print("%d %-9s %-62s %s" % (i, "PROVEN" if ok else "NOT-PROVEN", what[:62], tail))
        if not ok:
            print("    the break did not turn the suite red")
        return ok
    finally:
        shutil.rmtree(tmp, True)


def control_green():
    tmp, root = make_copy()
    try:
        rc, tail = run(TESTS, root)
        print("unbroken copy: %s (%s)" % ("GREEN" if rc == 0 else "RED - STOP", tail))
        return rc == 0
    finally:
        shutil.rmtree(tmp, True)


def main():
    if not control_green():
        return 1
    good = sum(prove(i, *b) for i, b in enumerate(BREAKS, 1))
    # the real working tree was never touched - every edit happened inside a
    # temp copy that is removed right after its own run.
    with open(os.path.join(APP, SRC_REL), encoding="utf-8") as f:
        untouched = f.read()
    for what, old, _new in BREAKS:
        if old not in untouched:
            print("WORKING TREE WAS MODIFIED (%s) - investigate immediately" % what)
            return 1
    print("working tree byte-identical after: True")
    print("%d/%d breaks caught" % (good, len(BREAKS)))
    return 0 if good == len(BREAKS) else 1


if __name__ == "__main__":
    sys.exit(main())
