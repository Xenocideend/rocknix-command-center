"""Break proofs for YT3 (YouTube App tv strip Close/Home + swipe, and the
old mpv-based "YouTube" tile's removal, 24 Sep 2026): each fix is undone IN
PLACE in one of this wave's own files, its test must go red, then the file
is restored byte-identical. Pattern: tools/w_break_tests.py.

  1. main.py: on_app_action() no longer routes "tv.close" to on_tv_close() -
     it would fall through to the D-pad path instead (a no-op, guarded by
     YtAppSession.action() itself) and the session would never end.
  2. main.py: on_app_action() no longer routes "tv.home" to on_tv_home() -
     tapping Home would do nothing at all.
  3. main.py: on_tv_close() no longer cancels the auto-hide timer - a strip
     that had already auto-hidden would leave a stale, still-armed timer
     behind after the session ended.
  4. main.py: on_tv_home() no longer cancels the auto-hide timer (same class
     of bug as #3, the other button).
  5. main.py: gestures.wants() no longer offers "swipe_up"/"swipe_up_from_bottom"
     for the tv strip in BAR mode - a swipe up over the strip would do
     nothing (TouchRouter never claims the stroke as a gesture at all).
  6. web_tiles.py: YtAppSession.close() no longer clears the bar app
     immediately - the D-pad/Close/Home row would stay on screen until
     Firefox actually finished quitting, instead of disappearing the moment
     Close is tapped (WebApps.end_session()'s own pattern for Browser/
     Discord).
  7. web_tiles.py: YtAppSession.action() no longer guards "tv.close"/
     "tv.home" against being sent to the page as bogus D-pad key names (a
     defence-in-depth check, since main.App.on_app_action already intercepts
     both before they would ever reach here in the real app).
  8. screens.py: Bar._apply_app_visibility() no longer hides the % volume
     readout for the tv app - the row would lose the width YT3 needed for
     Close/Home alongside the 6-button D-pad (see Bar.layout()'s own note).

Run: python -B tools/yt3_break_tests.py   (Windows or WSL/python3 - none of
these eight need a real mpv, wvkbd, Firefox or sway)."""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("main.py",
     '        if name == "tv.close":\n            self.on_tv_close()\n'
     '        elif name == "tv.home":\n            self.on_tv_home()\n'
     '        elif name.startswith("tv.") and self.ytapp is not None:',
     '        if name.startswith("tv.") and self.ytapp is not None:',
     "tests.test_web_tiles.TestYouTubeAppTvStrip.test_close_ends_the_session_and_returns_to_full"),
    ("main.py",
     '        elif name == "tv.home":\n            self.on_tv_home()\n',
     "",
     "tests.test_web_tiles.TestYouTubeAppTvStrip."
     "test_home_parks_the_session_and_shows_the_command_center"),
    ("main.py",
     '        log.info("tv strip -> Close")\n        if self.ytapp is not None:\n'
     '            self.ytapp.close()\n        if self.ytauto is not None:\n'
     '            self.ytauto.stop()          # cancel the auto-hide timer right away',
     '        log.info("tv strip -> Close")\n        if self.ytapp is not None:\n'
     '            self.ytapp.close()',
     "tests.test_web_tiles.TestYouTubeAppTvStrip.test_close_cancels_the_auto_hide_timer"),
    ("main.py",
     '        log.info("tv strip -> Home")\n        if self.tabs is not None:\n'
     '            self.tabs.select(app_tabs.CC)\n        elif self.pull is not None:\n'
     '            self.pull.open("tv_home")\n        if self.ytauto is not None:\n'
     '            self.ytauto.stop()          # cancel the auto-hide timer right away',
     '        log.info("tv strip -> Home")\n        if self.tabs is not None:\n'
     '            self.tabs.select(app_tabs.CC)\n        elif self.pull is not None:\n'
     '            self.pull.open("tv_home")',
     "tests.test_web_tiles.TestYouTubeAppTvStrip.test_home_cancels_the_auto_hide_timer"),
    ("main.py",
     'or (self.mode == BAR and self.ytauto is not None and self.ytauto.active\n'
     '                and g in ("swipe_up", "swipe_up_from_bottom")),',
     ",",
     "tests.test_web_tiles.TestYouTubeAppTvStrip."
     "test_swipe_up_on_the_tv_strip_opens_the_command_center"),
    ("web_tiles.py",
     # YT4 added self._cancel_swipe_poll() between the two lines this
     # anchor originally spanned directly - re-pointed to include it rather
     # than assume it away, still covering the same behaviour (immediate
     # set_bar_app(None) on Close).
     '        if not self.browser.is_running():\n            return\n'
     '        self.ui.cc.set_bar_app(None)\n'
     '        self._cancel_swipe_poll()\n'
     '        self.submit(self.browser.close, done=lambda _r=None: self._closed())',
     '        if not self.browser.is_running():\n            return\n'
     '        self._cancel_swipe_poll()\n'
     '        self.submit(self.browser.close, done=lambda _r=None: self._closed())',
     "tests.test_web_tiles.TestYouTubeTvTile.test_close_clears_the_bar_app_immediately"),
    ("web_tiles.py",
     'if not name.startswith("tv.") or name in ("tv.close", "tv.home"):',
     'if not name.startswith("tv."):',
     "tests.test_web_tiles.TestYouTubeTvTile."
     "test_dpad_ignores_close_and_home_even_if_called_directly"),
    ("screens.py",
     "self.readout.set_visible(not self.hinting and app != \"tv\")",
     "self.readout.set_visible(not self.hinting)",
     "tests.test_ui.TestAppStrip.test_tv_strip_drops_the_percent_readout"),
]


def clear_caches():
    for root, dirs, _ in os.walk(APP):
        for d in list(dirs):
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
        print("%d ANCHOR MISSING/AMBIGUOUS in %s (count=%d)" % (i, fn, text.count(good)))
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
