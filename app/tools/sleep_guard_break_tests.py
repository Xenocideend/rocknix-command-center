#!/usr/bin/env python3
"""Break-restore proof for tests/test_sleep_guard.py (B3, 25 Sep).

Copies rp5deck/ and rocknix-config/ into a scratch tree with the same layout,
applies ONE mutation at a time there, and runs the tests in the copy (the
tests put their own APP first on sys.path, so a copy is the only honest way
to test a mutated module). The working files are never touched. Every
mutation must turn at least one test red; the unmutated copy must pass.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))

MUTATIONS = [   # (name, file relative to the repo, old, new)
    ("veto always allows", "rocknix-config/charge-sleep-veto.sh",
     "    exit 1\nfi\nexit 0", "    exit 0\nfi\nexit 0"),
    ("veto ignores whether the pid is alive", "rocknix-config/charge-sleep-veto.sh",
     'if [ -d "/proc/$p" ]; then', "if true; then"),
    ("no flag file written", "bottom-screen-app/rp5deck/sleep_guard.py",
     "        os.replace(tmp, self.flag_path)\n", "        os.unlink(tmp)\n"),
    ("flag never removed", "bottom-screen-app/rp5deck/sleep_guard.py",
     "            os.unlink(self.flag_path)\n", "            pass\n"),
    ("inhibitor spawned every poll", "bottom-screen-app/rp5deck/sleep_guard.py",
     "        if stuck and not self.locks.held:", "        if stuck:"),
    ("dead inhibitor not retaken", "bottom-screen-app/rp5deck/sleep_guard.py",
     "        elif stuck and self.locks.proc is not None and self.locks.proc.poll() is not None:",
     "        elif False:"),
    ("inhibitor only delays", "bottom-screen-app/rp5deck/sleep_guard.py",
     '"--mode=block"', '"--mode=delay"'),
    ("unit not ordered before sleep.target", "rocknix-config/charge-sleep-veto.service",
     "Before=sleep.target\n", "\n"),
]


def run(tree):
    app = os.path.join(tree, "bottom-screen-app", "rp5deck")
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_sleep_guard"],
                       cwd=app, capture_output=True, text=True, timeout=300)
    tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
    return r.returncode, tail


def copy_tree(dst):
    shutil.copytree(APP, os.path.join(dst, "bottom-screen-app", "rp5deck"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(os.path.join(REPO, "rocknix-config"), os.path.join(dst, "rocknix-config"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "rk_local.json"))


def main():
    bad = 0
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, "base")
        copy_tree(base)
        rc, tail = run(base)
        print("control (unmutated): %s  %s" % ("PASS" if rc == 0 else "FAIL", tail))
        bad += rc != 0
        for i, (name, rel, old, new) in enumerate(MUTATIONS):
            tree = os.path.join(d, "m%d" % i)
            copy_tree(tree)
            p = os.path.join(tree, rel)
            text = open(p, encoding="utf-8", newline="").read()
            if text.count(old) != 1:
                print("SKIP-BROKEN %-40s anchor found %d times" % (name, text.count(old)))
                bad += 1
                continue
            open(p, "w", encoding="utf-8", newline="").write(text.replace(old, new))
            rc, tail = run(tree)
            print("%s %-40s %s" % ("CAUGHT  " if rc != 0 else "MISSED  ", name, tail))
            bad += rc == 0
    print("RESULT: %s" % ("all mutations caught" if not bad else "%d problem(s)" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
