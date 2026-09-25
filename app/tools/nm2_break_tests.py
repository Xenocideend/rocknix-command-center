"""Break proofs for the NM2 fixes (24 Sep 2026 test-day, name_guard scoping):
each fix is undone in place, its test must go red, then the file is restored
byte-identical. Follows the pattern of tools/td_fix_break_tests.py.

  1. name_guard's per-system extension filter removed (every file counts,
     regardless of the system's declared <extension> list)
  2. name_guard's es_systems.cfg scoping removed (falls back to walking
     whole ROM roots again, unscoped - the original Steam/theme bug)
  3. name_guard's (st_dev, st_ino) dedupe removed from _dedupe_systems (only
     realpath-string dedupe survives - a bind-mounted pair under two
     different <system> paths would be scanned, and so counted, twice)
  4. fix-bad-names' defence-in-depth refusal removed (a rename candidate
     outside every declared ES system path would be planned/applied again
     instead of being refused)

Run: python3 -B tools/nm2_break_tests.py (Linux/WSL for full coverage - the
symlink-only test cases skip on a Windows account without symlink rights,
same as tools/td_fix_break_tests.py).
"""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("name_guard.py",
     "if ext not in extensions:",
     "if False:",
     "tests.test_name_guard"),
    ("name_guard.py",
     "        if deduped:",
     "        if False:",
     "tests.test_name_guard"),
    ("name_guard.py",
     "        if rp in seen_rp or (key != (0, 0) and key in seen_key):\n"
     "            continue\n"
     "        seen_rp.add(rp)\n"
     "        seen_key.add(key)\n"
     "        out.append({\"name\": sysinfo.get(\"name\") or \"\", \"path\": rp,\n"
     "                    \"extensions\": sysinfo.get(\"extensions\") or frozenset()})",
     "        if rp in seen_rp:\n"
     "            continue\n"
     "        seen_rp.add(rp)\n"
     "        seen_key.add(key)\n"
     "        out.append({\"name\": sysinfo.get(\"name\") or \"\", \"path\": rp,\n"
     "                    \"extensions\": sysinfo.get(\"extensions\") or frozenset()})",
     "tests.test_name_guard"),
    ("tools/fix-bad-names.py",
     "if not path_under_a_known_system(canon, system_index):",
     "if False:",
     "tests.test_fix_bad_names"),
]


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
    if text.count(good) != 1:
        print("%d ANCHOR MISSING in %s (count=%d)" % (i, fn, text.count(good)))
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
    print("%d %s  %-20s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
