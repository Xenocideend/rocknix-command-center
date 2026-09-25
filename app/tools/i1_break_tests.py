"""I1 break/restore proofs. Each break edits the REAL file (one exact,
unique replacement), runs the named tests under WSL with -B, requires them
to FAIL, restores the original bytes, checks sha256 identity, and requires
the same tests to PASS again. Exit 1 if any break stayed green or any
restore is not identical/green."""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)          # this repo's app/ directory, wherever it is checked out


def _to_wsl_path(p):
    """'D:\\Tools\\x' -> '/mnt/d/Tools/x' (see tests/test_supervisor.py's
    identical helper - this tool shells out to wsl.exe the same way)."""
    p = p.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = "/mnt/%s%s" % (p[0].lower(), p[2:])
    return p


WSL_APP = _to_wsl_path(APP)

BREAKS = [
    ("video stops when a game starts: a hook game-start is ignored",
     "companion.py",
     '''            elif not same or self.running is None:
                self.running = Target("game", ev.system, ev.rom_path, ev.name, None, True)
                self.running_confirmed = False''',
     '''            elif False:
                self.running = Target("game", ev.system, ev.rom_path, ev.name, None, True)
                self.running_confirmed = False''',
     ["tests.test_companion.TestControllerVideoRules.test_video_stops_the_moment_a_game_starts_and_never_restarts_while_it_runs",
      "tests.test_companion.TestAppNavigation.test_video_stops_when_a_game_starts_through_the_app"]),
    ("video never restarts while a game runs: the running check is dropped",
     "companion.py",
     '''        return (self.active and self.running is None and self.manual_state is None''',
     '''        return (self.active and self.manual_state is None''',
     ["tests.test_companion.TestControllerVideoRules.test_video_stops_the_moment_a_game_starts_and_never_restarts_while_it_runs"]),
    ("video stops when the companion is left: set_active(False) no longer stops it",
     "companion.py",
     '''        if not active:
            self._stop_video("inactive")''',
     '''        if not active:
            pass''',
     ["tests.test_companion.TestControllerVideoRules.test_inactive_stops_and_active_resumes",
      "tests.test_companion.TestAppNavigation.test_opening_the_command_center_stops_video"]),
    ("the CC opens on a swipe: the gesture no longer reaches the state machine",
     "main.py",
     '''        self.pull.handle_gesture(name)
''',
     '''        pass
''',
     ["tests.test_companion.TestAppNavigation.test_swipe_down_from_the_top_opens_the_command_center_volume_first"]),
    ("the CC closes on the hardware button: the press no longer reaches the state machine",
     "main.py",
     '''        self.pull.summon_button()
''',
     '''        pass
''',
     ["tests.test_companion.TestAppNavigation.test_the_hardware_button_closes_it_and_opens_it_again"]),
    # (dropped: removing ONLY the pull-state check stays green - on_pull switches
    #  the view in the same call, so ui.showing enforces the same rule; the
    #  BOTH-checks break below is the proof for this behaviour.)
    ("the hook writes atomically: it writes under a spool event name instead of a hidden temp (FX-B spool)",
     "es-hooks/rp5deck-game-selected.sh",
     '''    t="$sp/.tmp.$$"''',
     '''    t="$sp/000000000000-0000000000-$$.ev"''',
     ["tests.test_esevents.TestHookScripts.test_written_atomically_each_file_only_ever_appears_by_rename"]),
    ("the hook handles an apostrophe (and space) name: the argument is left unquoted",
     "es-hooks/rp5deck-game-selected.sh",
     '''            printf 'arg%s=%s\\000' "$i" "$a"''',
     '''            printf 'arg%s=%s\\000' "$i" $a''',
     ["tests.test_esevents.TestHookScripts.test_every_shell_writes_every_nasty_name_byte_exact"]),
    ("the settings sheet lays out a newly selected tab (settings_view bug found by I1)",
     "settings_view.py",
     '''        if self.rect[2] > 0 and self.rect[3] > 0:
            self.layout(self.rect)''',
     '''        pass''',
     ["tests.test_companion.TestAppSettings.test_a_control_on_another_tab_is_tappable_after_switching"]),
    ("video frames are double-buffered on the worker thread: the swap is removed",
     "companion.py",
     '''                    self._front, self._back = self._back, self._front
''',
     '''                    self._front = self._back
''',
     ["tests.test_companion.TestVideoWorker.test_renders_on_its_own_thread_into_alternating_buffers"]),
    ("video never plays while a game runs: BOTH gates removed (choice + video_allowed)",
     "companion.py",
     [("""        return (self.active and self.running is None and self.manual_state is None""",
       """        return (self.active and self.manual_state is None"""),
      ("""        allow_video = bool(self._c("play_video")) and not t.running""",
       """        allow_video = bool(self._c("play_video"))""")],
     ["tests.test_companion.TestControllerVideoRules.test_video_stops_the_moment_a_game_starts_and_never_restarts_while_it_runs"]),
    ("the OSD only appears while the CC is closed: BOTH the pull-down and view checks removed",
     "main.py",
     [("        if self.pull.state != PULL.COMPANION or self.mode != FULL or " + chr(92) + "\n"
       "                self.ui.showing != \"companion\":",
       "        if self.mode != FULL:")],
     ["tests.test_companion.TestAppVolumeOverlay.test_overlay_only_while_the_command_center_is_closed"]),
]



def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(tests):
    cmd = ["wsl.exe", "-e", "sh", "-c", "cd %s && python3 -B -m unittest %s 2>&1 | tail -4"
           % (WSL_APP, " ".join(tests))]
    r = subprocess.run(cmd, capture_output=True, text=True, env=dict(__import__("os").environ,
                                                                      MSYS_NO_PATHCONV="1"))
    out = (r.stdout or "") + (r.stderr or "")
    lines = [l for l in out.splitlines() if l.strip()]
    ok = any(l.startswith("OK") for l in lines)
    summary = next((l for l in reversed(lines) if l.startswith(("OK", "FAILED"))), lines[-1] if lines else "?")
    return ok, summary


def main():
    bad = 0
    for entry in BREAKS:
        if len(entry) == 5:
            title, rel, old, new, tests = entry
            edits = [(old, new)]
        else:
            title, rel, edits, tests = entry
        path = APP + "\\" + rel.replace("/", "\\")
        with open(path, "rb") as f:
            orig = f.read()
        h0 = sha(path)
        text = orig.decode("utf-8")
        counts = [text.count(o) for o, _ in edits]
        if counts != [1] * len(edits):
            print("SETUP ERROR %s: anchors found %r times" % (title, counts))
            bad += 1
            continue
        for o, nw in edits:
            text = text.replace(o, nw)
        try:
            with open(path, "wb") as f:
                f.write(text.encode("utf-8"))
            ok_broken, s_broken = run(tests)
        finally:
            with open(path, "wb") as f:
                f.write(orig)
        same = sha(path) == h0
        ok_fixed, s_fixed = run(tests)
        verdict = "PROVEN" if (not ok_broken and same and ok_fixed) else "NOT PROVEN"
        if verdict != "PROVEN":
            bad += 1
        print("%-10s %s\n           broken: %s | restored identical: %s | restored: %s"
              % (verdict, title, s_broken, same, s_fixed))
    print("\n%d of %d breaks proven" % (len(BREAKS) - bad, len(BREAKS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
