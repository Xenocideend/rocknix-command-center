#!/usr/bin/env python3
"""Break-restore proof for tests/test_092_winfix.py (W1, 25 Sep).

Each mutation is applied to a SCRATCH copy of 092 (the working file is never
touched) and the W1 tests are run against it via SW1_092_PATH. Every
mutation must make at least one test fail; the unmutated copy must pass.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
SRC = os.path.join(REPO, "rocknix-config", "dual-screen-layout-and-power")
BS = chr(92)

PY_PAT = "|GamePad View)( - FPS: [0-9.]+)?$|"
MUTATIONS = [
    ("no fullscreen disable", '"[con_id=%d] fullscreen disable"', '"[con_id=%d] fullscreen enable"', 1),
    ("python pattern end-anchored again", PY_PAT, "|GamePad View)$|", 1),
    ("one shell pattern end-anchored again",
     "|GamePad View)( - FPS: [0-9.]+)?" + BS + "$|" + BS*2 + "[w2" + BS*2 + "]" + BS + "\"] move container to output $INTERNAL",
     "|GamePad View)" + BS + "$|" + BS*2 + "[w2" + BS*2 + "]" + BS + "\"] move container to output $INTERNAL", 1),
    ("loop guard never trips", "        if n >= WINFIX_MAX:", "        if n >= 10 ** 9:", 1),
    ("no fallback when cc_output_for is missing", ' || cc="$INTERNAL"', "", 2),
    ("steady-state call removed",
     "\n        fix_game_windows \"$state\"\n",
     "\n", 1),
    ("dual never moves the game", "            if game and game[1] != game_out:", "            if False:", 1),
    ("single never joins the workspace", "                if w != gw:", "                if False:", 1),
]


def run(path):
    env = dict(os.environ, SW1_092_PATH=path)
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_092_winfix"],
                       cwd=APP, env=env, capture_output=True, text=True, timeout=600)
    tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
    return r.returncode, tail


def main():
    src = open(SRC, encoding="utf-8", newline="").read()
    bad = 0
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "092")
        shutil.copyfile(SRC, p)
        rc, tail = run(p)
        print("control (unmutated): %s  %s" % ("PASS" if rc == 0 else "FAIL", tail))
        bad += rc != 0
        for name, old, new, want in MUTATIONS:
            n = src.count(old)
            if n != want:
                print("SKIP-BROKEN %-40s anchor found %d times (want %d)" % (name, n, want))
                bad += 1
                continue
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(src.replace(old, new))
            rc, tail = run(p)
            ok = rc != 0
            print("%s %-40s %s" % ("CAUGHT  " if ok else "MISSED  ", name, tail))
            bad += not ok
    print("RESULT: %s" % ("all mutations caught" if not bad else "%d problem(s)" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
