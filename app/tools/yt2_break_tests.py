"""Break proofs for yt_feeds.py (YT2, 24 Sep 2026): each guard is undone in
place, its test must go red, then the file is restored byte-identical.
Pattern: tools/td_fix_break_tests.py. Scoped to ONLY yt_feeds.py - the one
file this agent is allowed to edit among the files these proofs touch (per
the brief: never write/restore youtube.py, web_tiles.py, app_tabs.py,
browser.py, osk.py, screens.py, config.py, main.py, settings_view.py).

  1. Cookie-line redaction removed (_redact() becomes a no-op) - a secret
     could then reach a caller that logs stderr.
  2. The --verbose/-v guard assertion removed from _run_yt_dlp() - a future
     debug flag could silently defeat the redaction rule (see #1: redaction
     only matters because -v is never passed).
  3. Signed-out detection's comparative heuristic disabled (classify_failure
     always returns "network_error") - "sign in again" would never show,
     a real auth problem always mislabeled as a generic outage.
  4. Resume store's size cap removed - the file would grow without bound.
  5. Resume store's corrupt-file recovery removed (a JSONDecodeError is no
     longer caught) - one bad write would crash every future resume lookup.
  6. RESUME_MIN_SECONDS filter removed from resume_for() - a video barely
     started would falsely "resume" at ~0s forever.

Run: python3 -B tools/yt2_break_tests.py (Windows or WSL - yt_feeds.py has
no platform-specific code).
"""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("yt_feeds.py",
     '        if any(m in low for m in _COOKIE_LOG_MARKERS):\n'
     '            out.append("[redacted: cookie-related log line]")\n'
     '        else:\n'
     '            out.append(ln)',
     '        out.append(ln)',
     "tests.test_yt_feeds.TestRedaction"),
    ("yt_feeds.py",
     '    assert "--verbose" not in args and "-v" not in args, \\\n'
     '        "yt_feeds must never pass --verbose/-v to yt-dlp (YT1 redaction rule)"\n',
     "",
     "tests.test_yt_feeds.TestRedaction.test_run_yt_dlp_never_passes_verbose_or_v"),
    ("yt_feeds.py",
     "    if feed_result is not None:\n"
     "        return \"ok\"\n"
     "    if looks_like_auth_error(feed_stderr):\n"
     "        return \"cookies_expired\"\n"
     "    if plain_search_result is not None:\n"
     "        return \"cookies_expired\"\n"
     "    return \"network_error\"",
     "    return \"network_error\"",
     "tests.test_yt_feeds.TestAuthHeuristic"),
    ("yt_feeds.py",
     "    if len(store) > RESUME_MAX_ENTRIES:\n"
     "        ordered = sorted(store.items(), key=lambda kv: kv[1][\"at\"])\n"
     "        store = dict(ordered[-RESUME_MAX_ENTRIES:])\n",
     "",
     "tests.test_yt_feeds.TestResumeStore.test_size_cap_evicts_oldest"),
    ("yt_feeds.py",
     "    try:\n"
     "        data = json.loads(raw)\n"
     "    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):\n"
     "        return {}",
     "    data = json.loads(raw)",
     "tests.test_yt_feeds.TestResumeStore.test_corrupt_json_recovers_as_empty_store"),
    ("yt_feeds.py",
     "    if pos < RESUME_MIN_SECONDS:\n        return None\n",
     "",
     "tests.test_yt_feeds.TestResumeStore.test_below_min_seconds_is_none"),
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
