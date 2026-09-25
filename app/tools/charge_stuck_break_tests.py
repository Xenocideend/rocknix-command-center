#!/usr/bin/env python3
"""Break-restore proof for charge_stuck.py / charge_stuck_view.py and their
tests (test_charge_stuck.py, test_charge_stuck_view.py, test_charge_stuck_app.py):
each break is applied inside a temp COPY of the whole rp5deck tree (never
this working tree), the copy's own test suite is run against it, and the
result must be RED; the unbroken copy must be GREEN. Same shape as
tools/keys_guard_break_tests.py.

Run from rp5deck/:  python -B tools/charge_stuck_break_tests.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
TESTS = ["tests.test_charge_stuck", "tests.test_charge_stuck_view", "tests.test_charge_stuck_app"]

# (what, file, old anchor, new text) - each anchor must appear exactly once
# in the unbroken file.
BREAKS = [
    ("sign of current flipped (discharging looks like charging)", "charge_stuck.py",
     "    if mean_current <= DISCHARGE_UA:\n",
     "    if -mean_current <= DISCHARGE_UA:\n"),
    ("ICL_STATUS majority check dropped (any ICL reading counts as stuck)", "charge_stuck.py",
     "    if zero_count * 2 < len(icls):\n",
     "    if False:\n"),
    ("read_register reads the whole file instead of seeking", "charge_stuck.py",
     "            f.seek(reg * REG_LINE_LEN)\n            line = f.read(REG_LINE_LEN)\n",
     "            data = f.read()\n"
     "            line = data[reg * REG_LINE_LEN:reg * REG_LINE_LEN + REG_LINE_LEN]\n"),
    ("dismiss never re-arms on unplug", "charge_stuck_view.py",
     '        if not sample["plugged"]:\n'
     "            self.dismissed = False       # re-arm: the owner's own \"next plug-in\" rule\n",
     '        if not sample["plugged"]:\n'
     "            pass       # BROKEN: never re-arms\n"),
]


def run(tests, cwd):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest"] + tests,
                       capture_output=True, text=True, cwd=cwd)
    tail = [ln for ln in r.stderr.splitlines() if ln.startswith(("OK", "FAILED", "Ran"))]
    return r.returncode, " ".join(tail)


def make_copy():
    tmp = tempfile.mkdtemp(prefix="chgbreak-")
    root = os.path.join(tmp, "rp5deck")
    shutil.copytree(APP, root, ignore=shutil.ignore_patterns(
        "__pycache__", "screenshots", "proto", "es-upstream", "es-hooks", "firefox", "steam",
        "*.pyc"))
    return tmp, root


def prove(i, what, rel, old, new):
    tmp, root = make_copy()
    try:
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        n = text.count(old)
        if n != 1:
            print("%d SETUP-FAIL %-62s anchor found %d times in %s" % (i, what[:62], n, rel))
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
    files = sorted({b[1] for b in BREAKS})
    same = True
    for rel in files:
        with open(os.path.join(APP, rel), encoding="utf-8") as f:
            untouched = f.read()
        for what, brel, old, _new in BREAKS:
            if brel == rel and old not in untouched:
                print("WORKING TREE WAS MODIFIED (%s, %s) - investigate immediately" % (what, rel))
                same = False
    print("working tree byte-identical after: %s" % same)
    print("%d/%d breaks caught" % (good, len(BREAKS)))
    return 0 if (good == len(BREAKS) and same) else 1


if __name__ == "__main__":
    sys.exit(main())
