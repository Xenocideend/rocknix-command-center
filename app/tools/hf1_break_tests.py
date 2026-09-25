"""HF1 break proofs: each mutation must turn its tests RED, and the restored
file must turn them GREEN again. Sources are hashed before and after.
Run from rp5deck/:  python -B <this> """
import hashlib
import subprocess
import sys

BREAKS = [
    ("web_tiles.py", "session ends on FULL even before the window reached DSI-1",
     "if new == sway_ipc.FULL and self.app is not None and self.seen_bar:",
     "if new == sway_ipc.FULL and self.app is not None:",
     ["tests.test_web_tiles.TestBrowserTile.test_full_while_still_starting_does_not_end_it"]),
    ("osk.py", "keyboard wanted without Firefox focus",
     "        if not focused or self.mode == \"off\":\n            return False\n        if self.mode == \"button\":",
     "        if self.mode == \"off\":\n            return False\n        if self.mode == \"button\":",
     ["tests.test_osk.TestAutoPolicy.test_owner_requested_keyboard_still_needs_focus"]),
    ("web_tiles.py", "poll probes a text field without asking sway who has focus",
     "                    focused = bool(f[\"focused\"])",
     "                    focused = True",
     ["tests.test_web_tiles.TestKeyboardForFirefox.test_never_shows_while_firefox_is_not_focused"]),
    ("youtube.py", "mpv fullscreen again (covers the strip)",
     "            \"--force-window=immediate\",",
     "            \"--fullscreen\",\n            \"--force-window=immediate\",",
     ["tests.test_youtube.TestPlayerCommandLine.test_command_has_required_flags"]),
    ("youtube.py", "mpv default input bindings back (double tap = fullscreen)",
     "            \"--input-default-bindings=no\",\n",
     "",
     ["tests.test_youtube.TestPlayerCommandLine.test_command_has_required_flags"]),
    ("web_tiles.py", "keyboard left up when the mode leaves BAR",
     "        if self.osk_on:\n            self._set_osk(False)            # never over",
     "        if False:\n            self._set_osk(False)            # never over",
     ["tests.test_web_tiles.TestKeyboardForFirefox.test_leaving_bar_hides_it"]),
    ("web_tiles.py", "worker exception not caught (done never runs)",
     "            try:\n                return fn(*a)\n            except Exception:       # noqa: BLE001\n                log.exception(\"web worker call %s failed\", name)\n                return fail",
     "            return fn(*a)",
     ["tests.test_web_tiles.TestBrowserTile.test_worker_exception_still_resolves_the_poll"]),
    ("web_tiles.py", "end_session forgets to stop the process",
     "            self._call(self._w_stop, app)",
     "            pass",
     ["tests.test_web_tiles.TestBrowserTile.test_cancel_during_start_stops_what_the_start_launched",
      "tests.test_web_tiles.TestBrowserTile.test_back_reload_home_close"]),
    ("web_tiles.py", "Close waits behind a slow page load (no immediate SIGTERM)",
     "                target.terminate_now()",
     "                pass",
     ["tests.test_web_tiles.TestBrowserTile.test_close_during_a_slow_page_load_signals_firefox_at_once"]),
    ("web_tiles.py", "registry signals a pid without checking its cmdline",
     "            if cmd and marker in cmd and self.kill is not None:",
     "            if self.kill is not None:",
     ["tests.test_web_tiles.TestRegistry.test_a_reused_pid_is_never_signalled"]),
    ("ui.py", "keyboard row width maths (the old per-row gap count)",
     "            unit = (rw - self.GAP * 9) / 10.0     # ten 1-unit cells and nine gaps",
     "            unit = (rw - self.GAP * (len(row) - 1)) / 10.0",
     ["tests.test_ui.TestKeyboard.test_every_row_fills_the_width_without_overlap_and_keys_are_big"]),
    ("ui.py", "shift sticks after one letter",
     "                ch = ch.upper()\n                self.shift = False",
     "                ch = ch.upper()",
     ["tests.test_ui.TestKeyboard.test_shift_is_one_shot"]),
    ("main.py", "Command Center auto-closes under the BAR strip",
     "        if self.mode == BAR:\n            # HF1:",
     "        if False:\n            # HF1:",
     ["tests.test_web_tiles.TestBrowserTile.test_command_center_is_not_auto_closed_under_the_strip"]),
    ("youtube.py", "a failed search reads as 'no results'",
     "    if rc is None or rc != 0 or not out.strip():\n        return None",
     "    if rc is None or rc != 0 or not out.strip():\n        return []",
     ["tests.test_youtube.TestSearchParsing.test_detailed_failure_is_none_and_empty_result_is_empty_list"]),
    ("screens.py", "app controls shown in FULL mode too",
     "    def shown_app(self):\n        return self.app if self.compact else None",
     "    def shown_app(self):\n        return self.app",
     ["tests.test_ui.TestAppStrip.test_full_mode_never_shows_app_controls"]),
    ("browser.py", "probe failure reported as 'not editable'",
     "        if not ok or not isinstance(value, dict):\n            return None",
     "        if not ok or not isinstance(value, dict):\n            return {\"editable\": False, \"focus\": False}",
     ["tests.test_browser.TestPageScripts.test_text_input_state_unknown_is_none_never_false"]),
]

FILES = sorted({b[0] for b in BREAKS})


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(tests):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest"] + tests,
                       capture_output=True, text=True)
    last = [l for l in r.stderr.splitlines() if l.startswith(("OK", "FAILED", "Ran"))]
    return r.returncode, " ".join(last)


def main():
    before = {p: sha(p) for p in FILES}
    bad = 0
    for i, (path, what, old, new, tests) in enumerate(BREAKS, 1):
        with open(path, "rb") as f:
            orig = f.read()
        text = orig.decode("utf-8")
        if text.count(old) != 1:
            print("%2d SETUP-FAIL %s: anchor found %d times" % (i, what, text.count(old)))
            bad += 1
            continue
        try:
            with open(path, "wb") as f:
                f.write(text.replace(old, new).encode("utf-8"))
            rc_b, out_b = run(tests)
        finally:
            with open(path, "wb") as f:
                f.write(orig)
        rc_g, out_g = run(tests)
        ok = rc_b != 0 and rc_g == 0
        bad += not ok
        print("%2d %s  %-58s broken: %-22s restored: %s" % (
            i, "PROVEN" if ok else "NOT-PROVEN", what[:58], out_b[-22:], out_g))
    after = {p: sha(p) for p in FILES}
    same = before == after
    print("sources byte-identical after: %s (%s)" % (same, ", ".join(FILES)))
    print("%d/%d proven" % (len(BREAKS) - bad, len(BREAKS)))
    return 0 if (bad == 0 and same) else 1


if __name__ == "__main__":
    sys.exit(main())
