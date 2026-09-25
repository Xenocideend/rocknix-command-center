#!/usr/bin/env python3
"""focus_guard.py: the FOC1 fix (TASKS.md) - after a game exits, ES's new
window must get keyboard focus back unless a real window (an emulator,
rp5deck-web, ...) already holds it.

Fixtures (tests/fixtures/): every sway-tree-*-SYNTHETIC-from-real-capture*
file here starts from the real sway-tree-FULL-real-capture-2026-09-23.json
capture (see test_modes.py) with a small, documented, field-level edit
(never hand-typed from scratch); each carries "_synthetic": true and a
"_synthetic_note" saying exactly what changed. Offline, stdlib only - no
sway socket is touched.
"""
import glob
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import focus_guard  # noqa: E402
from focus_guard import (ALLOWED_COMMAND, CommandRefused, FocusGuard,  # noqa: E402
                         build_focus_con_command, decide, enforce_allowed, find_es,
                         find_game_window, focused_node, is_empty_workspace)

FIX = os.path.join(HERE, "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def wait_for(pred, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ---------------------------------------------------------------------------
# decide(): the pure rule, against real-capture-derived fixtures
# ---------------------------------------------------------------------------
class TestDecideFromFixtures(unittest.TestCase):
    def test_empty_dsi1_workspace_focused_acts(self):
        should, reason = decide(load("sway-tree-empty-workspace-focused-SYNTHETIC-"
                                     "from-real-capture.json"))
        self.assertTrue(should, reason)
        self.assertIn("empty workspace", reason)

    def test_es_just_mapped_while_nothing_focused_acts(self):
        tree = load("sway-tree-nothing-focused-SYNTHETIC-from-real-capture.json")
        should, reason = decide(tree, just_mapped_es=True)
        self.assertTrue(should, reason)
        self.assertIn("just mapped", reason)

    def test_nothing_focused_no_game_acts_without_just_mapped(self):
        # Device, test day 16:00:45: ES re-mapped unfocused after a game
        # exit; the one-shot just_mapped_es flag was lost in the event burst
        # and nothing had focus for 15 s. Nothing focused + no game window:
        # focusing ES steals from nobody. (This test replaced the old
        # "never guess" one, which encoded exactly that dead-pad state.)
        tree = load("sway-tree-nothing-focused-SYNTHETIC-from-real-capture.json")
        self.assertIsNone(focus_guard.find_game_window(tree))
        should, reason = decide(tree, just_mapped_es=False)
        self.assertTrue(should, reason)
        self.assertIn("nothing is focused and no game is running", reason)

    def test_nonempty_workspace_itself_focused_acts(self):
        # Device 16:05:52: focused node = workspace 1 (DP-1) holding ES,
        # no window focused, no game: the pad was dead until a tap.
        tree = load("sway-tree-nothing-focused-SYNTHETIC-from-real-capture.json")
        es = focus_guard.find_es(tree)
        ws = [n for n in focus_guard._walk(tree) if n.get("type") == "workspace"
              and es in list(focus_guard._walk(n))][0]
        ws["focused"] = True
        self.assertFalse(focus_guard.is_empty_workspace(ws))
        should, reason = decide(tree, just_mapped_es=False)
        self.assertTrue(should, reason)
        self.assertIn("itself is focused", reason)

    def test_nothing_focused_with_a_game_window_does_not_act(self):
        # A game/emulator window exists (e.g. still mapping, or ES gone):
        # this rule must not pull focus to ES over it.
        tree = load("sway-tree-nothing-focused-SYNTHETIC-from-real-capture.json")
        game = {"type": "con", "id": 99999, "pid": 4242, "app_id": "com.libretro.RetroArch",
                "name": "RetroArch", "fullscreen_mode": 1, "focused": False,
                "nodes": [], "floating_nodes": []}
        ws = [n for n in focus_guard._walk(tree) if n.get("type") == "workspace"][0]
        ws.setdefault("nodes", []).append(game)
        self.assertIsNotNone(focus_guard.find_game_window(tree))
        should, reason = decide(tree, just_mapped_es=False)
        self.assertFalse(should, reason)

    def test_retroarch_focused_never_steals(self):
        should, reason = decide(load("sway-tree-retroarch-focused-SYNTHETIC-"
                                     "from-real-capture.json"))
        self.assertFalse(should, reason)
        self.assertIn("RetroArch", reason)

    def test_rp5deck_web_focused_never_steals(self):
        # The owner must still be able to tap Firefox to type - the guard
        # must not fight the browser for focus.
        should, reason = decide(load("sway-tree-rp5deck-web-focused-SYNTHETIC-"
                                     "from-real-capture.json"))
        self.assertFalse(should, reason)
        self.assertIn("rp5deck-web", reason)

    # test_es_already_focused_does_nothing removed: depended on
    # tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json, dropped
    # from the public release (see DROPPED-FIXTURES-list.txt).

    def test_no_es_in_tree_does_nothing(self):
        should, reason = decide(load("sway-tree-no-es-SYNTHETIC-from-real-capture.json"))
        self.assertFalse(should, reason)
        self.assertIn("no EmulationStation", reason)

    def test_just_mapped_is_ignored_when_a_real_window_has_focus(self):
        # Even if the caller (mis-)reports just_mapped_es=True, a real
        # window in focus must still win - the "never steal" rule is
        # checked before the OR of the two trigger conditions.
        tree = load("sway-tree-retroarch-focused-SYNTHETIC-from-real-capture.json")
        should, reason = decide(tree, just_mapped_es=True)
        self.assertFalse(should, reason)

    def test_synthetic_fixtures_are_marked(self):
        for path in glob.glob(os.path.join(FIX, "sway-tree-*SYNTHETIC-from-real-capture*.json")):
            with self.subTest(os.path.basename(path)):
                data = load(os.path.basename(path))
                self.assertTrue(data.get("_synthetic"))
                self.assertTrue(data.get("_synthetic_note"))


class TestDecideHelpers(unittest.TestCase):
    def test_find_es_returns_none_when_absent(self):
        self.assertIsNone(find_es({"type": "root", "nodes": []}))

    def test_focused_node_none_when_nothing_focused(self):
        t = {"type": "root", "focused": False, "nodes": [
            {"type": "workspace", "focused": False, "nodes": []}]}
        self.assertIsNone(focused_node(t))

    def test_is_empty_workspace(self):
        self.assertTrue(is_empty_workspace({"type": "workspace", "nodes": [],
                                            "floating_nodes": []}))
        self.assertFalse(is_empty_workspace({"type": "workspace", "nodes": [{"id": 1}],
                                             "floating_nodes": []}))
        self.assertFalse(is_empty_workspace({"type": "con", "nodes": [], "floating_nodes": []}))

    def test_find_game_window_skips_es_and_own_windows(self):
        tree = {"type": "root", "nodes": [
            {"type": "con", "id": 1, "app_id": "emulationstation", "pid": 1,
             "fullscreen_mode": 1, "nodes": [], "floating_nodes": []},
            {"type": "con", "id": 2, "app_id": "rp5deck-web", "pid": 2,
             "fullscreen_mode": 1, "nodes": [], "floating_nodes": []},
            {"type": "con", "id": 3, "app_id": "rp5deck-yt", "pid": 3,
             "fullscreen_mode": 1, "nodes": [], "floating_nodes": []},
            {"type": "con", "id": 4, "app_id": "com.libretro.RetroArch", "pid": 4,
             "fullscreen_mode": 1, "nodes": [], "floating_nodes": []},
        ]}
        game = find_game_window(tree)
        self.assertIsNotNone(game)
        self.assertEqual(game.get("id"), 4)

    def test_find_game_window_requires_fullscreen(self):
        tree = {"type": "root", "nodes": [
            {"type": "con", "id": 9, "app_id": "com.libretro.RetroArch", "pid": 9,
             "fullscreen_mode": 0, "nodes": [], "floating_nodes": []},
        ]}
        self.assertIsNone(find_game_window(tree))

    def test_find_game_window_none_when_absent(self):
        self.assertIsNone(find_game_window({"type": "root", "nodes": []}))


# ---------------------------------------------------------------------------
# FOC2 gap 1: ES has no window at all (it closes its own window for a
# running game) - decide() must find the game/emulator window directly and
# focus it BY CON_ID, since there is no app_id for ALLOWED_COMMAND to match.
# ---------------------------------------------------------------------------
class TestDecideFOC2Gap1NoEsWindow(unittest.TestCase):
    def test_empty_workspace_focused_refocuses_the_game_by_con_id(self):
        tree = load("sway-tree-no-es-game-empty-workspace-focused-SYNTHETIC-from-real-capture.json")
        command, reason = decide(tree)
        self.assertEqual(command, build_focus_con_command(501))
        self.assertIn("RetroArch", reason)
        self.assertIn("refocusing the game window", reason)

    def test_nothing_focused_refocuses_the_game_by_con_id(self):
        # Same as above, but nothing at all is focused (the gap right after
        # Firefox closes, before sway assigns focus to anything) - must
        # still act; this does NOT depend on just_mapped_es (that flag only
        # means anything when ES itself just mapped).
        tree = load("sway-tree-no-es-game-nothing-focused-SYNTHETIC-from-real-capture.json")
        command, reason = decide(tree, just_mapped_es=False)
        self.assertEqual(command, build_focus_con_command(501))
        command2, _ = decide(tree, just_mapped_es=True)
        self.assertEqual(command2, build_focus_con_command(501))

    def test_game_already_focused_does_nothing(self):
        tree = load("sway-tree-no-es-game-already-focused-SYNTHETIC-from-real-capture.json")
        command, reason = decide(tree)
        self.assertIsNone(command, reason)

    def test_no_es_and_no_game_does_nothing(self):
        # The existing no-es fixture: no ES, no game either - nothing to do.
        tree = load("sway-tree-no-es-SYNTHETIC-from-real-capture.json")
        command, reason = decide(tree)
        self.assertIsNone(command, reason)
        self.assertIn("no EmulationStation", reason)

    def test_break_restore_find_game_window_is_load_bearing(self):
        # If find_game_window() were broken to always return None, gap 1
        # would silently do nothing (the owner stuck on an empty workspace
        # with no controller input) - prove the fixture actually exercises
        # this path, then restore.
        tree = load("sway-tree-no-es-game-empty-workspace-focused-SYNTHETIC-from-real-capture.json")
        orig = focus_guard.find_game_window
        try:
            focus_guard.find_game_window = lambda t: None
            command, reason = focus_guard.decide(tree)
            self.assertIsNone(command, "broken find_game_window unexpectedly still acted")
        finally:
            focus_guard.find_game_window = orig
        command2, _ = focus_guard.decide(tree)
        self.assertEqual(command2, build_focus_con_command(501))


# ---------------------------------------------------------------------------
# FOC2 gap 2: mpv (rp5deck-yt) took focus on a tap (no_focus only applies at
# map time). If a game is running, return focus to it; with no game, leave
# mpv alone - this is the one deliberate exception to "never steal".
# ---------------------------------------------------------------------------
class TestDecideFOC2Gap2Mpv(unittest.TestCase):
    def test_mpv_focused_with_a_game_running_returns_focus_to_the_game(self):
        tree = load("sway-tree-mpv-focused-game-running-SYNTHETIC-from-real-capture.json")
        command, reason = decide(tree)
        self.assertEqual(command, build_focus_con_command(501))
        self.assertIn("mpv", reason)
        self.assertIn("rp5deck-yt", reason)
        self.assertIn("RetroArch", reason)

    def test_mpv_focused_with_no_game_is_left_alone(self):
        tree = load("sway-tree-mpv-focused-no-game-SYNTHETIC-from-real-capture.json")
        command, reason = decide(tree)
        self.assertIsNone(command, reason)
        self.assertIn("rp5deck-yt", reason)

    def test_break_restore_mpv_rule_is_load_bearing(self):
        # If the mpv carve-out (_mpv_has_focus) were disabled, a focused mpv
        # with a game running would fall through to the generic "never
        # steal" rule instead (mpv IS a real window) and the game would
        # never get focus back - monkeypatch the real function decide()
        # calls (the same technique TestBreakRestoreNeverSteal uses for
        # _is_view / is_empty_workspace) and prove the fixture goes wrong,
        # then restore.
        tree = load("sway-tree-mpv-focused-game-running-SYNTHETIC-from-real-capture.json")
        orig = focus_guard._mpv_has_focus
        try:
            focus_guard._mpv_has_focus = lambda node: False
            command, reason = focus_guard.decide(tree)
            self.assertIsNone(command, "fixture does not exercise the mpv-vs-never-steal bug")
            self.assertIn("never steal", reason)
        finally:
            focus_guard._mpv_has_focus = orig

        command2, reason2 = focus_guard.decide(tree)
        self.assertEqual(command2, build_focus_con_command(501), reason2)


# ---------------------------------------------------------------------------
# Break/restore proof: the "never steal from a real window" rule
# ---------------------------------------------------------------------------
class TestBreakRestoreNeverSteal(unittest.TestCase):
    """decide() actually has TWO independent guards against stealing from a
    real window: the explicit `_is_view(node)` early-return, AND
    is_empty_workspace()'s `type == "workspace"` check (a real window's own
    con is always a leaf - empty `nodes`/`floating_nodes` - so without that
    type check it would be misread as an "empty workspace" too). Breaking
    only one of the two does not reproduce a live bug against these
    fixtures (the other independently catches it) - so this monkeypatches
    BOTH real module functions at once (decide() looks them up by name at
    call time, so this genuinely changes its behaviour), proves the fully
    broken pair wrongly steals focus, then restores is_empty_workspace ALONE
    and proves that is sufficient by itself to refuse again - i.e. the
    fixtures do exercise a real bug, and the suite would notice if EITHER
    guard were removed, not just when both are gone. Restores both
    functions in `finally` no matter what."""

    def _assert_defense_in_depth(self, fixture, label):
        tree = load(fixture)
        orig_is_view, orig_is_empty = focus_guard._is_view, focus_guard.is_empty_workspace

        def broken_is_view(n):
            return False                              # BUG: never a "real window"

        def broken_is_empty(n):
            # BUG: drops the type check - any leaf node (empty nodes AND
            # floating_nodes, true of every real window's own con) counts.
            return not n.get("nodes") and not n.get("floating_nodes")

        try:
            focus_guard._is_view = broken_is_view
            focus_guard.is_empty_workspace = broken_is_empty
            should, reason = focus_guard.decide(tree)
            self.assertTrue(should, "fixture does not exercise the combined bug: %s" % (reason,))

            # Restore ONLY the type check (leave _is_view broken): still
            # refused, proving is_empty_workspace's own type check is by
            # itself enough to stop the steal.
            focus_guard.is_empty_workspace = orig_is_empty
            should2, reason2 = focus_guard.decide(tree)
            self.assertFalse(should2, "is_empty_workspace alone should have stopped it: %s"
                             % (reason2,))
        finally:
            focus_guard._is_view = orig_is_view
            focus_guard.is_empty_workspace = orig_is_empty

        # GREEN: both guards restored.
        should3, reason3 = focus_guard.decide(tree)
        self.assertFalse(should3, reason3)
        self.assertIn(label, reason3)

    def test_defense_in_depth_against_retroarch(self):
        self._assert_defense_in_depth(
            "sway-tree-retroarch-focused-SYNTHETIC-from-real-capture.json", "RetroArch")

    def test_defense_in_depth_against_rp5deck_web(self):
        self._assert_defense_in_depth(
            "sway-tree-rp5deck-web-focused-SYNTHETIC-from-real-capture.json", "rp5deck-web")


# ---------------------------------------------------------------------------
# The command allow-list
# ---------------------------------------------------------------------------
class TestCommandAllowList(unittest.TestCase):
    def test_allowed_command_passes(self):
        enforce_allowed(ALLOWED_COMMAND)         # must not raise

    def test_con_id_command_passes(self):
        enforce_allowed('[con_id=501] focus')    # must not raise
        enforce_allowed(build_focus_con_command(501))
        enforce_allowed(build_focus_con_command("501"))

    def test_anything_else_is_refused(self):
        for bad in ('output DSI-1 disable', 'exit', '[app_id="emulationstation"] kill',
                    ALLOWED_COMMAND + "; exit", ALLOWED_COMMAND.upper(), "", None,
                    '[app_id="emulationstation"] focus ', 'focus',
                    # FOC2: everything that is NOT a strict fullmatch of the
                    # con_id form must be refused too.
                    '[con_id=501]focus', '[con_id=501] focus ', ' [con_id=501] focus',
                    '[con_id=] focus', '[con_id=abc] focus', '[con_id=-1] focus',
                    '[con_id=501] focus; exit', '[con_id=501] kill',
                    '[con_id=501] focus\n[con_id=502] focus'):
            with self.subTest(bad):
                with self.assertRaises(CommandRefused):
                    enforce_allowed(bad)

    def test_run_command_refuses_before_touching_the_socket(self):
        ipc = focus_guard.Ipc.__new__(focus_guard.Ipc)

        class NoSock:
            def sendall(self, data):
                raise AssertionError("a refused command reached the socket")
        ipc.sock = NoSock()
        with self.assertRaises(CommandRefused):
            ipc.run_command("output DSI-1 disable")
        with self.assertRaises(CommandRefused):
            ipc.run_command('[app_id="emulationstation"] move to workspace 1')

    def test_break_restore_the_allow_list(self):
        # Monkeypatch the REAL module function (Ipc.run_command calls
        # enforce_allowed by name, so this genuinely changes its behaviour),
        # then drive it through Ipc.run_command with a socket stand-in that
        # records what it was sent, instead of a hand-rolled stand-in.
        ipc = focus_guard.Ipc.__new__(focus_guard.Ipc)

        class RecordingSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)
        ipc.sock = RecordingSock()
        original = focus_guard.enforce_allowed

        def broken_enforce(command):
            return None                           # BUG: refuses nothing

        try:
            focus_guard.enforce_allowed = broken_enforce
            with self.assertRaises((OSError, AttributeError, ConnectionError, TypeError)):
                # RED: the allow-list no longer stops it - it gets as far as
                # trying to read a reply from a socket that never answers
                # (RecordingSock has no recv()) - but only AFTER sending.
                ipc.run_command("output DSI-1 disable")
            self.assertEqual(len(ipc.sock.sent), 1, "the dangerous command reached the socket")
            self.assertIn(b"output DSI-1 disable", ipc.sock.sent[0])
        finally:
            focus_guard.enforce_allowed = original

        # GREEN: with the real enforce_allowed back, the socket is never
        # touched at all.
        ipc.sock.sent = []
        with self.assertRaises(CommandRefused):
            ipc.run_command("output DSI-1 disable")
        self.assertEqual(ipc.sock.sent, [])


# ---------------------------------------------------------------------------
# The daemon: debounce, rate-limit, event handling, dry-run, reconnection
# ---------------------------------------------------------------------------
class FakeIpc:
    """A stand-in sway connection for _maybe_act(): run_command()/focus_es()
    record calls instead of touching a socket. enforce_allowed() still runs
    for real, so a test that (mis-)builds a bad command still gets refused."""

    def __init__(self):
        self.commands = []

    def run_command(self, command):
        enforce_allowed(command)
        self.commands.append(command)
        return [{"success": True}]

    def focus_es(self):
        return self.run_command(ALLOWED_COMMAND)


class TestFocusGuardDaemon(unittest.TestCase):
    def test_window_new_es_arms_just_mapped_flag(self):
        g = FocusGuard()
        g._note_event(0, json.dumps({"change": "new",
                                    "container": {"app_id": "emulationstation"}}).encode())
        self.assertTrue(g._just_mapped_es)

    def test_window_new_other_app_does_not_arm_the_flag(self):
        g = FocusGuard()
        g._note_event(0, json.dumps({"change": "new",
                                    "container": {"app_id": "com.libretro.RetroArch"}}).encode())
        self.assertFalse(g._just_mapped_es)

    def test_window_close_does_not_arm_the_flag(self):
        g = FocusGuard()
        g._note_event(0, json.dumps({"change": "close",
                                    "container": {"app_id": "emulationstation"}}).encode())
        self.assertFalse(g._just_mapped_es)

    def test_garbage_event_payload_does_not_raise(self):
        g = FocusGuard()
        g._note_event(0, b"not json")
        self.assertFalse(g._just_mapped_es)

    def test_end_to_end_acts_and_logs_with_reason(self):
        tree = load("sway-tree-empty-workspace-focused-SYNTHETIC-from-real-capture.json")
        logged = []
        g = FocusGuard(log_fn=logged.append)
        fake = FakeIpc()
        command, reason = decide(tree)
        self.assertTrue(command)
        acted = g._maybe_act(fake, command, reason)
        self.assertTrue(acted)
        self.assertEqual(fake.commands, [ALLOWED_COMMAND])
        self.assertEqual(g.actions_taken, 1)
        self.assertTrue(any("focused EmulationStation" in m and "empty workspace" in m
                           for m in logged), logged)

    def test_end_to_end_focuses_a_game_by_con_id(self):
        # FOC2 gap 1: no ES window at all - the command must be the con_id
        # form, and the log must name it (not "EmulationStation").
        tree = load("sway-tree-no-es-game-empty-workspace-focused-SYNTHETIC-from-real-capture.json")
        logged = []
        g = FocusGuard(log_fn=logged.append)
        fake = FakeIpc()
        command, reason = decide(tree)
        self.assertEqual(command, build_focus_con_command(501))
        acted = g._maybe_act(fake, command, reason)
        self.assertTrue(acted)
        self.assertEqual(fake.commands, [build_focus_con_command(501)])
        self.assertTrue(any("focused con_id=501" in m and "RetroArch" in m for m in logged), logged)

    def test_dry_run_never_calls_focus_es(self):
        tree = load("sway-tree-empty-workspace-focused-SYNTHETIC-from-real-capture.json")
        logged = []
        g = FocusGuard(dry_run=True, log_fn=logged.append)
        fake = FakeIpc()
        command, reason = decide(tree)
        acted = g._maybe_act(fake, command, reason)
        self.assertTrue(acted)
        self.assertEqual(fake.commands, [])          # never sent
        self.assertTrue(any("[dry-run]" in m and "would focus" in m for m in logged), logged)

    def test_rate_limit_skips_a_second_action_too_soon(self):
        tree = load("sway-tree-empty-workspace-focused-SYNTHETIC-from-real-capture.json")
        times = iter([0.0, 0.4, 1.4])                # 0.4s and 1.4s after the first action
        g = FocusGuard(rate_limit=1.0, now=lambda: next(times))
        fake = FakeIpc()
        command, reason = decide(tree)
        self.assertTrue(g._maybe_act(fake, command, reason))          # t=0.0: acts
        self.assertFalse(g._maybe_act(fake, command, reason))         # t=0.4: rate-limited
        self.assertTrue(g._maybe_act(fake, command, reason))          # t=1.4: allowed again
        self.assertEqual(fake.commands, [ALLOWED_COMMAND, ALLOWED_COMMAND])
        self.assertEqual(g.actions_taken, 2)
        self.assertEqual(g.actions_skipped_rate_limited, 1)

    def test_break_restore_rate_limit(self):
        tree = load("sway-tree-empty-workspace-focused-SYNTHETIC-from-real-capture.json")
        command, reason = decide(tree)

        # Break: a rate limit that always allows (a common off-by-logic bug:
        # comparing against the wrong sign or unit).
        def broken_maybe_act(g, ipc, command, reason, now_fn):
            g._last_action = now_fn()
            ipc.run_command(command)
            return True
        g = FocusGuard(rate_limit=1.0, now=lambda: 0.0)
        fake = FakeIpc()
        broken_maybe_act(g, fake, command, reason, lambda: 0.0)
        broken_maybe_act(g, fake, command, reason, lambda: 0.01)    # RED: fires again 10ms later
        self.assertEqual(len(fake.commands), 2)

        g2 = FocusGuard(rate_limit=1.0, now=iter([0.0, 0.01]).__next__)
        fake2 = FakeIpc()
        g2._maybe_act(fake2, command, reason)
        g2._maybe_act(fake2, command, reason)                        # GREEN: rate-limited
        self.assertEqual(len(fake2.commands), 1)

    def test_start_stop_is_prompt_with_no_sway_socket(self):
        # No SWAYSOCK on this PC: the daemon's reconnect loop must still
        # start and stop cleanly (find_socket() returns None -> logged and
        # retried with backoff, never a crash, never blocks stop()).
        env = dict(os.environ)
        os.environ.pop("SWAYSOCK", None)
        try:
            logged = []
            g = FocusGuard(log_fn=logged.append)
            g.start()
            self.assertTrue(wait_for(lambda: any("no sway IPC socket" in m for m in logged),
                                    timeout=3.0), logged)
            t0 = time.monotonic()
            g.stop(timeout=2.0)
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertFalse(g._thread.is_alive())
        finally:
            os.environ.clear()
            os.environ.update(env)

    def test_reconnect_backoff_after_socket_error(self):
        # A raising _session (as if the socket died) must be retried, not
        # crash the thread. Small backoff_start keeps this test fast.
        g = FocusGuard(log_fn=lambda m: None, backoff_start=0.01, backoff_cap=0.05)
        calls = []

        def flaky_session():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("socket closed")
            g._stop.set()
        g._session = flaky_session
        g.start()
        self.assertTrue(wait_for(lambda: len(calls) >= 3, timeout=5.0), calls)
        g.stop()

    def test_idle_cpu_thread_blocks_in_select(self):
        # A regression here (busy-looping instead of blocking in select())
        # would burn a CPU core on a battery device; a thread that is still
        # alive after a short sleep with near-zero wall time spent inside
        # the test itself is the practical proxy available on the PC (a
        # true CPU% measurement is part of the device test plan).
        env = dict(os.environ)
        os.environ.pop("SWAYSOCK", None)
        try:
            g = FocusGuard(log_fn=lambda m: None)
            g.start()
            time.sleep(0.3)
            self.assertTrue(g._thread.is_alive())
            g.stop()
        finally:
            os.environ.clear()
            os.environ.update(env)


# ---------------------------------------------------------------------------
# SW1: with the screens swapped, ES lives on DSI-1 and the Command Center on
# DP-1. The guard must focus ES on whichever output it is on, and still
# never steal from a real window on either output. Trees are derived from
# the real capture by swapping the two outputs' workspaces (ES + ws 1 onto
# DSI-1, the empty ws 2 onto DP-1) - what 092 produces when swapped.
#
# REAL_CAPTURE, _outputs(), _all(), swapped(), mirrored(), and every
# TestSwappedScreensSW1 method except test_allow_list_names_no_output
# removed: swapped() (and, via it, most of this class) read
# tests/fixtures/sway-tree-FULL-real-capture-2026-09-23.json directly
# through REAL_CAPTURE, dropped from the public release (see
# DROPPED-FIXTURES-list.txt). No synthetic equivalent exists for the
# swapped-output shape this section builds. Removed methods:
# test_fixture_really_has_es_on_the_builtin_panel,
# test_empty_addon_workspace_focused_acts,
# test_es_just_mapped_on_the_builtin_panel_acts,
# test_es_already_focused_on_the_builtin_panel,
# test_never_steals_from_the_game_on_the_es_panel,
# test_never_steals_from_command_center_windows_on_the_addon,
# test_decision_does_not_depend_on_output_names,
# test_break_restore_an_output_bound_guard_is_caught (and its only helper,
# es_output()).
# ---------------------------------------------------------------------------
class TestSwappedScreensSW1(unittest.TestCase):
    def test_allow_list_names_no_output(self):
        # one command, focusing ES wherever it is; no second command needed
        self.assertEqual(ALLOWED_COMMAND, '[app_id="emulationstation"] focus')
        for out in ("DP-1", "DSI-1", "output"):
            self.assertNotIn(out, ALLOWED_COMMAND)


if __name__ == "__main__":
    unittest.main()
