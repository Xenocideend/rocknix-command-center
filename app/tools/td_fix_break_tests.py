"""Break proofs for the test-day fixes (24 Sep 2026): each fix is undone in
place, its test must go red, then the file is restored byte-identical.

  1. es_health.es_pids back to comm == "emulationstation" (16 bytes; the
     kernel's comm holds 15, so ES is never found on the device)
  2. cleanstate's gates back to the untruncated name sets
  3. name_guard follows symlinks at any depth again (Steam's z: -> /)
  4. companion keeps a hook game-start fired by an ES that has exited

Run: python3 -B tools/td_fix_break_tests.py (Linux, for the symlink case).
"""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("es_health.py",
     "        return self.pids_named(ES_COMM)\n",
     "        return [p for p in self.pids() if self.comm(p) == ES_COMM]\n",
     "tests.test_es_health.TestProcProbeComm"),
    ("cleanstate.py",
     "if comm in _SHELLS or comm[:15] in _PROTECTED_COMM:",
     "if comm in _SHELLS or comm in PROTECTED_NAMES:",
     "tests.test_cleanstate"),
    ("cleanstate.py",
     'or (comm or "")[:15] in _PROTECTED_COMM or comm in _SHELLS:',
     "or comm in PROTECTED_NAMES or comm in _SHELLS:",
     "tests.test_cleanstate"),
    ("companion.py",
     "return es is not None and fired > 0 and fired < es",
     "return False",
     "tests.test_companion.TestStaleGameStart"),
    ("browser.py",
     "            if sock is not None:\n                try:\n                    sock.close()",
     "            if True:\n                try:\n                    sock.close()",
     "tests.test_browser"),
    ("window_switcher.py",
     "if _SECOND_RE.search(_second_title(node.get(\"name\"))):",
     "if _SECOND_RE.fullmatch(str(node.get(\"name\") or \"\")):",
     "tests.test_window_switcher"),
    ("focus_guard.py",
     "        if find_game_window(tree) is None:",
     "        if False:",
     "tests.test_focus_guard"),
    ("hotkeys.py",
     'hk = RA_INDEX_LABEL.get(live("input_enable_hotkey_btn"))',
     "hk = None",
     "tests.test_hotkeys"),
    ("name_guard.py",
     "if not is_dir and e.is_symlink() and depth == 0:",
     "if not is_dir and e.is_symlink():",
     "tests.test_name_guard"),
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
        print("%d ANCHOR MISSING in %s" % (i, fn))
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
    print("%d %s  %-14s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
