"""Leak-guard break proofs (25 Sep 2026): the harness teardown added to
tests/test_supervisor.py after 89 supervisors and 160 `sleep 3600` were found
orphaned in WSL must go RED when the thing it watches is broken, and GREEN
again once it is restored.

  1 group kill removed from _kill_group      -> the per-harness leak check
                                                (addCleanup) and tearDownModule
  2 group kill removed AND the per-harness   -> tearDownModule alone
    check disabled
  3 stub sleep fix reverted (plain           -> TestHarnessTeardown: one
    `trap 'exit 0'`)                            orphaned `sleep 3600`

Every break runs in a temp COPY of this tree (never this tree), with -B. A
broken run leaks real processes on purpose; they are swept (by process group,
found by cwd under the copy) after every run, and the sweep is checked before
the copy is deleted - an unswept process would hold the copy open.

Run from rp5deck/:  python -B tools/leak_break_tests.py   (Windows or WSL)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
LINUX = sys.platform.startswith("linux")

GUARD = "tests.test_supervisor.TestHarnessTeardown"
ONE = "tests.test_supervisor.TestSupervisorIntegration.test_both_children_start"

KILL_OLD = "    proc.wait()\n    if LINUX:\n        pgid = proc.pid"
KILL_NEW = "    proc.wait()\n    return\n    if LINUX:\n        pgid = proc.pid"
CHECK_OLD = "    left = [row for row in _survivors(tag) if row[1] == tag or tag in row[2]]\n    if left:"
CHECK_NEW = "    left = [row for row in _survivors(tag) if row[1] == tag or tag in row[2]]\n    if False:"
STUB_OLD = "trap '[ -n \"$sp\" ] && kill \"$sp\" 2>/dev/null; exit 0' TERM INT"
STUB_NEW = "trap 'exit 0' TERM INT"

SUP = "tests/test_supervisor.py"
CHILD = "tests/fixtures/supervisor-stub-child-SYNTHETIC.sh"

# (what, [(file, old, new)], tests, text the broken run must print)
BREAKS = [
    ("group kill removed", [(SUP, KILL_OLD, KILL_NEW)],
     [GUARD, ONE], "leaked"),
    ("group kill removed, per-harness check off (tearDownModule only)",
     [(SUP, KILL_OLD, KILL_NEW), (SUP, CHECK_OLD, CHECK_NEW)],
     [ONE], "leftover harness process"),
    ("stub trap back to plain exit (orphans its sleep)", [(CHILD, STUB_OLD, STUB_NEW)],
     [GUARD], "3 != 2"),
]

# Kills the process group of every process whose cwd is under $ROOT (the
# copy; unittest runs there, so every harness descendant inherits it), then
# prints how many such processes are left. dash: no `--` in kill.
SWEEP = r"""
me=$(ps -o pgid= -p $$ | tr -d ' ')
scan() {
    for d in /proc/[0-9]*; do
        p=${d#/proc/}; [ "$p" = "$$" ] && continue
        c=$(readlink "$d/cwd" 2>/dev/null) || continue
        case "$c" in "$ROOT"*) ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ';; esac
    done | sort -u | grep -vx "$me"
}
for sig in TERM KILL; do
    for g in $(scan); do kill -$sig "-$g" 2>/dev/null; done
    sleep 1
done
rm -rf /tmp/rp5deck-suptest-*
scan | wc -l
"""


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def wsl_path(p):
    p = p.replace("\\", "/")
    return "/mnt/%s%s" % (p[0].lower(), p[2:]) if p[1:2] == ":" else p


def sweep(root):
    """Kill everything a run left under `root`; returns the survivors."""
    if LINUX:
        r = subprocess.run(["sh", "-c", SWEEP], capture_output=True, text=True,
                           env=dict(os.environ, ROOT=root))
    else:
        wsl = shutil.which("wsl.exe") or shutil.which("wsl")
        r = subprocess.run([wsl, "-e", "env", "ROOT=" + wsl_path(root), "sh", "-c", SWEEP],
                           capture_output=True, text=True)
    return int(r.stdout.strip().splitlines()[-1])


def run(tests, cwd):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest"] + tests,
                       capture_output=True, text=True, cwd=cwd)
    last = [ln for ln in r.stderr.splitlines() if ln.startswith(("OK", "FAILED", "Ran"))]
    return r.returncode, " ".join(last), r.stdout + r.stderr


def prove(i, root, what, edits, tests, marker):
    origs = {}
    for path, old, new in edits:
        full = os.path.join(root, path)
        if path not in origs:
            with open(full, "rb") as f:
                origs[path] = f.read()
        with open(full, "rb") as f:
            text = f.read().decode("utf-8")
        if text.count(old) != 1:
            print("%d SETUP-FAIL %s: anchor found %d times in %s" % (i, what, text.count(old), path))
            for p, b in origs.items():
                with open(os.path.join(root, p), "wb") as f:
                    f.write(b)
            return False
        with open(full, "wb") as f:
            f.write(text.replace(old, new).encode("utf-8"))
    try:
        rc_b, out_b, full_b = run(tests, root)
    finally:
        for p, b in origs.items():
            with open(os.path.join(root, p), "wb") as f:
                f.write(b)
    left_b = sweep(root)
    rc_g, out_g, full_g = run(tests, root)
    left_g = sweep(root)
    ok = rc_b != 0 and marker in full_b and rc_g == 0 and left_b == 0 and left_g == 0
    print("%d %s  %-62s broken: %-22s restored: %s  (swept after: %d/%d)" % (
        i, "PROVEN" if ok else "NOT-PROVEN", what[:62], out_b[-22:], out_g, left_b, left_g))
    if not ok and marker not in full_b:
        print("   broken run did not print %r:\n%s" % (marker, full_b[-3000:]))
    return ok


def main():
    files = sorted({e[0] for b in BREAKS for e in b[1]})
    before = {p: sha(os.path.join(APP, p)) for p in files}
    tmp = tempfile.mkdtemp(prefix="leak-break-")
    good = 0
    root = os.path.join(tmp, "rp5deck")
    try:
        shutil.copytree(APP, root, ignore=shutil.ignore_patterns(
            "__pycache__", "screenshots", "proto", "es-upstream", "*.pyc"))
        for i, b in enumerate(BREAKS, 1):
            good += prove(i, root, *b)
    finally:
        left = sweep(root)
        shutil.rmtree(tmp, True)
    after = {p: sha(os.path.join(APP, p)) for p in files}
    same = before == after
    print("this tree byte-identical after: %s" % same)
    print("processes left under the copy: %d; copy removed: %s" % (left, not os.path.exists(tmp)))
    print("%d/%d proven" % (good, len(BREAKS)))
    return 0 if (good == len(BREAKS) and same and left == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
