#!/usr/bin/env python3
"""Break-restore proof for tests/test_092_azahar_rule.py: each break is applied
to a scratch copy of 092 (SW1_092_PATH), never the working file, and the test
must go red; the unbroken copy must be green."""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
SRC = os.path.join(os.path.dirname(os.path.dirname(APP)), "rocknix-config", "dual-screen-layout-and-power")

BREAKS = {
    "static rule removed": lambda t: "\n".join(
        l for l in t.split("\n")
        if not ("Azahar [^|]+" in l and "output $EXTERNAL, focus" in l)),
    "static rule points at the built-in panel": lambda t: t.replace(
        "move container to output $EXTERNAL, focus", "move container to output $INTERNAL, focus", 1),
    "swap rule loses the game argument": lambda t: t.replace(
        "exec $SELF --place-cc-windows game", "exec $SELF --place-cc-windows", 1),
    "handler moves it for every window (focus steal)": lambda t: t.replace(
        'if [ "${2:-}" = "game" ]; then', 'if true; then', 1),
    "pattern stops excluding the Secondary Window": lambda t: t.replace(
        "(?!.*Secondary Window\\$)", "", 1),
    "one of the three patterns drifts": lambda t: t.replace(
        "^Azahar [^|]+", "^Azahar [0-9a-f]+", 1),
}


def run(text):
    d = tempfile.mkdtemp(prefix="azbreak-")
    p = os.path.join(d, "092")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    env = dict(os.environ, SW1_092_PATH=p)
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_092_azahar_rule"],
                       cwd=APP, env=env, capture_output=True, text=True)
    os.remove(p)
    os.rmdir(d)
    return r.returncode == 0


def main():
    with open(SRC, encoding="utf-8", newline="") as f:
        orig = f.read()
    ok = run(orig)
    print("unbroken copy:", "GREEN" if ok else "RED (control failed - stop)")
    if not ok:
        return 1
    bad = 0
    for name, brk in BREAKS.items():
        t = brk(orig)
        if t == orig:
            print("  NO-OP break (fix the break): %s" % name)
            bad += 1
            continue
        green = run(t)
        print("  %-48s %s" % (name, "GREEN - NOT CAUGHT" if green else "red (caught)"))
        bad += green
    print("restored copy:", "GREEN" if run(orig) else "RED")
    print("%d/%d breaks caught" % (len(BREAKS) - bad, len(BREAKS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
