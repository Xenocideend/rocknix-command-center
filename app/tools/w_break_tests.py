"""Break proofs for W (app tabs BAR picker + YouTube tap-to-pause, 24 Sep
2026): each fix/feature is undone IN PLACE in one of W's own files, its test
must go red, then the file is restored byte-identical. Pattern:
tools/td_fix_break_tests.py. Only W's own files are ever touched here - never
another agent's break-test script, never another agent's module (screens.py/
main.py/config.py/settings_view.py are G's and are never written by this
script, even transiently).

  1. app_tabs.py: TabsController.select() no longer closes the BAR strip's
     inline picker after a tap (screens.CommandCenter.close_bar_tabs()).
  2. app_tabs.py: TabsController.bar_tabs() drops the Home pill - the picker
     would lose its only escape hatch to Settings / Mixer.
  3. web_tiles.py: _set_osk() no longer re-shows the keyboard when osk_on
     says "on" but ROCKNIX's killall already took the real process (only a
     fresh focus transition would notice).
  4. youtube.py: build_command() drops --input-conf, so a tap on the video
     would no longer reach mpv's "cycle pause" binding at all.
  5. youtube-input.conf: the MBTN_LEFT -> cycle pause binding itself is
     dropped from the shipped file (a mistake in the asset, not the code
     that points at it).
  6. browser.py: close() goes back to WebDriver:DeleteSession instead of
     Marionette:Quit - Firefox stays running, no sessionstore flush, tabs
     do not survive Close.
  7. browser.py: send_key() drops the keyUp half of the action pair, so
     leanback would see a key stuck down.
  8. app_tabs.py: route() forgets the YouTube TV tab, so tapping it would
     raise ValueError instead of switching.
  9. web_tiles.py: _reason_mentions() goes back to a naive substring test -
     "rp5deck-yt" is a PREFIX of "rp5deck-ytapp", so WebApps.on_mode() would
     wrongly think a BAR transition caused by the YouTube TV tile's window
     was its own, and reset a correctly-parked Browser/Discord session's
     `parked` flag (test day's real device bug class).
 10. firefox/user.js: browser.startup.page goes back to 1 - "the browser
     needs to save tabs when closed" stops working again.
 11. window_switcher.py: recognise() forgets the YouTube TV tile's app_id,
     so its window would never get a tab, never be parkable, and would
     read as a "foreign window" instead.

Run: python -B tools/w_break_tests.py   (Windows or WSL/python3 - none of
these eleven need a real mpv, wvkbd, Firefox, sway or main.App)."""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("app_tabs.py",
     '        self._note("tab", tab_id)\n        self._close_bar_tabs()\n',
     '        self._note("tab", tab_id)\n',
     "tests.test_app_tabs.TestBarTabsInlinePickerHook.test_a_tap_always_closes_the_picker"),
    ("app_tabs.py",
     'return self.tabs() + [{"id": CC, "label": "Home", "running": False, "active": False}]',
     "return self.tabs()",
     "tests.test_app_tabs.TestBarTabsInlinePickerHook."
     "test_bar_tabs_is_the_normal_list_plus_a_home_pill"),
    ("web_tiles.py",
     "if on == self.osk_on and (not on or self.keyboard.visible()):",
     "if on == self.osk_on:",
     "tests.test_web_tiles.TestKeyboardForFirefox."
     "test_killed_externally_by_rocknix_reshows_on_the_next_poll"),
    ("youtube.py",
     '            "--input-conf=%s" % INPUT_CONF_PATH,\n',
     "",
     "tests.test_youtube.TestPlayerCommandLine.test_command_has_required_flags"),
    ("youtube-input.conf",
     "MBTN_LEFT cycle pause\n",
     "",
     "tests.test_youtube.TestInputConfFile.test_binds_a_tap_to_cycle_pause"),
    ("browser.py",
     '                self._send("Marionette:Quit", {"flags": []}, timeout=QUIT_WAIT_TIMEOUT)\n',
     '                self._send("WebDriver:DeleteSession", {}, timeout=QUIT_WAIT_TIMEOUT)\n',
     "tests.test_browser.TestGracefulQuit."
     "test_navigates_home_then_sends_marionette_quit_not_delete_session"),
    ("browser.py",
     '                              {"type": "keyUp", "value": code}]}]',
     '                              ]}]',
     "tests.test_browser.TestBrowserControls.test_send_key_performs_a_key_down_and_up"),
    ("app_tabs.py",
     "    if tab_id == YOUTUBE_TV:\n        return wsw.YTAPP, tab_id\n",
     "",
     "tests.test_app_tabs.TestRoute.test_routes"),
    ("web_tiles.py",
     'return app_id in reason.rsplit(": ", 1)[-1].split(", ")',
     "return app_id in reason  # W2b break: naive substring, prefix collision",
     "tests.test_web_tiles.TestReasonMentions."
     "test_yt_app_id_is_a_prefix_of_tv_app_id_but_must_not_match_it"),
    ("firefox/user.js",
     'user_pref("browser.startup.page", 3);',
     'user_pref("browser.startup.page", 1);',
     "tests.test_browser.TestUserJsSessionRestorePrefs."
     "test_startup_page_resumes_the_previous_session"),
    ("window_switcher.py",
     "    if aid == YTAPP_APP_ID:\n        return YTAPP\n",
     "",
     "tests.test_app_tabs.TestWhichTabsExist."
     "test_youtube_tv_is_its_own_window_kind_not_a_web_label"),
]
# W2b: sway_ipc.py's own OWN_APP_IDS fix (patches/W2b-sway_ipc.patch) is
# deliberately NOT broken here, even transiently - sway_ipc.py is treated
# the same as G's files (never written directly, patch only), and this
# script's own rule is to never write ANY file outside W's ownership, break
# proof or not.


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
    print("%d %s  %-20s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
