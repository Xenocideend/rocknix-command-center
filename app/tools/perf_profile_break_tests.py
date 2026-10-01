#!/usr/bin/env python3
"""Mutation check for tests/test_perf_profile.py: every mutation must turn the suite red.
Runs on a temp copy of rp5deck (the real files are never edited), with -B so no stale
bytecode can mask a mutation."""
import os
import shutil
import subprocess
import sys
import tempfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(APP))

MUTATIONS = [
    ("perf_profile.py", "GPU_LIGHT_DEFAULT_HZ = 587000000", "GPU_LIGHT_DEFAULT_HZ = 925000000"),
    ("perf_profile.py", '    "cemu",                                             # Wii U\n', ""),
    ("perf_profile.py", "        if cores >= self.gate_cores:", "        if cores >= 0:"),
    ("perf_profile.py", "            if pid in old:", "            if True:"),
    ("perf_profile.py", "now - self.last_trigger < self.hold_s", "now - self.last_trigger < 0"),
    ("perf_profile.py", 'if self._r(gp) == "performance" and gov != "performance":',
     'if gov != "performance":'),
    ("perf_profile.py", '        if mode == "saver":\n            self.reason = "perf-mode saver"\n            return False\n', ""),
    ("perf_profile.py", 'return GPU_OC_VALUES.get(v, GPU_LIGHT_DEFAULT_HZ)', 'return GPU_HEAVY_HZ'),
    ("perf_profile.py", 'keys.add("wine")', 'pass'),
    ("perf_profile.py", 'l, r = st.find("("), st.rfind(")")', 'l, r = st.find("("), st.find(")")'),
    ("perf_profile.py", "    def shutdown(self):\n        \"\"\"On exit leaves the device light (capped GPU, governors put back).\"\"\"\n        try:",
     "    def shutdown(self):\n        \"\"\"On exit leaves the device light (capped GPU, governors put back).\"\"\"\n        return\n        try:"),
    ("perf_profile.py", '("cpu", self.hw.cpu_heavy if heavy else self.hw.cpu_restore)', '("cpu", lambda: None)'),
]


def run_suite(app):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_perf_profile"],
                       cwd=app, env=env, capture_output=True, text=True)
    return r.returncode


def main():
    tmp = tempfile.mkdtemp()
    try:
        # the launcher test reads ../../rocknix-config/gpu-cap-heavy-game-boost from APP
        app = os.path.join(tmp, "bottom-screen-app", "rp5deck")
        shutil.copytree(APP, app, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "firefox",
                                                                "screenshots", "steam"))
        os.makedirs(os.path.join(tmp, "rocknix-config"))
        shutil.copy(os.path.join(REPO, "rocknix-config", "gpu-cap-heavy-game-boost"),
                    os.path.join(tmp, "rocknix-config"))
        if run_suite(app) != 0:
            print("BASELINE RED - fix the suite first")
            return 1
        print("baseline green")
        caught = 0
        for i, (fn, old, new) in enumerate(MUTATIONS, 1):
            p = os.path.join(app, fn)
            orig = open(p, newline="").read()
            if orig.count(old) != 1:
                print("  M%-2d ANCHOR NOT UNIQUE/FOUND (%d): %r" % (i, orig.count(old), old[:50]))
                return 1
            with open(p, "w", newline="\n") as f:
                f.write(orig.replace(old, new))
            rc = run_suite(app)
            with open(p, "w", newline="\n") as f:
                f.write(orig)
            ok = rc != 0
            caught += ok
            print("  M%-2d %s  %s" % (i, "caught" if ok else "MISSED", old.strip()[:60]))
        # the launcher CRLF check: mutate the launcher itself
        lp = os.path.join(tmp, "rocknix-config", "gpu-cap-heavy-game-boost")
        orig = open(lp, newline="").read()
        with open(lp, "w", newline="") as f:
            f.write(orig.replace("\n", "\r\n"))
        ok = run_suite(app) != 0
        caught += ok
        print("  M%-2d %s  launcher with CRLF" % (len(MUTATIONS) + 1, "caught" if ok else "MISSED"))
        total = len(MUTATIONS) + 1
        print("%d/%d mutations caught" % (caught, total))
        return 0 if caught == total else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
