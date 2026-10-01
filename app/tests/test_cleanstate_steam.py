#!/usr/bin/env python3
"""cleanstate: the Steam stop items. They exist only while Steam is open (found from the process table),
stop the running game or the whole of Steam with SIGTERM only, and look at a process again right before
signalling it. Same fake process table and fake clock as test_cleanstate.py."""
import os
import signal
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import cleanstate  # noqa: E402
from cleanstate import EXIT_STEAM, KILL_EMULATOR, STOP_STEAM_GAME, Refused  # noqa: E402
from test_cleanstate import ES, RA, RP5DECK, RUNEMU, SWAY, Case  # noqa: E402

STEAM = {"comm": "steam", "ppid": 1, "cmdline": [
    "/storage/.local/share/Steam/steamrtarm64/steam", "-deckard", "-steamos3", "-gamepadui", "-noshaders"]}
REAPER = {"comm": "reaper", "ppid": 1, "cmdline": [
    "/storage/roms/steam/steamrtarm64/reaper", "SteamLaunch", "AppId=377160", "--", "/game/run"]}
GAMESCOPE = {"comm": "gamescope-wl", "ppid": 1, "cmdline": [
    "gamescope", "-W", "1920", "-H", "1080", "--backend", "wayland", "-e", "--", "/bin/bash"]}
BASE = {10: ES, 11: RP5DECK, 12: SWAY}


def T(extra=None):
    t = dict(BASE)
    t.update(extra or {})
    return t


class TestWhichProcessIsSteam(unittest.TestCase):
    def test_the_three_programs(self):
        self.assertEqual(cleanstate.steam_kind(STEAM["comm"], STEAM["cmdline"]), (cleanstate.STEAM_CLIENT, None))
        self.assertEqual(cleanstate.steam_kind(REAPER["comm"], REAPER["cmdline"]), (cleanstate.STEAM_REAPER, 377160))
        self.assertEqual(cleanstate.steam_kind(GAMESCOPE["comm"], GAMESCOPE["cmdline"]),
                         (cleanstate.STEAM_GAMESCOPE, None))

    def test_lookalikes_are_not(self):
        k = cleanstate.steam_kind
        self.assertIsNone(k("steam", ["/usr/bin/steam", "-gamepadui"]))                   # not our client path
        self.assertIsNone(k("steam", ["/storage/x/steamrtarm64/steam", "-silent"]))       # not Big Picture
        self.assertIsNone(k("bash", STEAM["cmdline"]))                                    # name must agree
        self.assertIsNone(k("reaper", ["/usr/bin/reaper", "SteamLaunch", "AppId=1", "--"]))
        self.assertIsNone(k("reaper", ["/storage/x/steamrtarm64/reaper", "SteamLaunch", "AppId=x"]))
        self.assertIsNone(k("reaper", ["/storage/x/steamrtarm64/reaper", "SteamLaunch"]))
        self.assertIsNone(k("gamescope-wl", ["gamescope", "-W", "1"]))                     # no --backend
        self.assertIsNone(k("gamescopereaper", ["gamescopereaper", "--", "x", "--backend"]))
        self.assertIsNone(k("python3", ["python3", "/storage/rp5deck/main.py"]))
        self.assertIsNone(k("", []))
        self.assertIsNone(k(None, None))

    def test_the_generic_kill_path_still_refuses_steam_and_gamescope(self):
        for n in ("steam", "gamescope", "FEX"):
            with self.assertRaises(Refused):
                cleanstate.enforce_kill_name(n)

    def test_the_new_actions_are_allowed(self):
        for a in (STOP_STEAM_GAME, EXIT_STEAM):
            cleanstate.enforce_action(a)
            self.assertIn(a, cleanstate.CLEAN_ACTIONS)


class TestDiscover(Case):
    def discover(self, t, **kw):
        h = self.make(t, **kw)
        h._steam_name = lambda appid: "Fallout 4" if appid == 377160 else None
        return h, h.discover(check_health=False)

    def test_nothing_steam_when_steam_is_not_open(self):
        h, plan = self.discover(T())
        self.assertEqual(plan.steam, {"client": [], "reapers": [], "gamescope": []})
        self.assertEqual([i.action for i in plan.items], [])

    def test_steam_open_offers_closing_it(self):
        h, plan = self.discover(T({20: STEAM, 21: GAMESCOPE}))
        self.assertEqual([i.action for i in plan.items], [EXIT_STEAM])
        self.assertEqual(plan.items[0].label, "Steam")

    def test_a_running_game_offers_stopping_just_the_game_with_its_name(self):
        h, plan = self.discover(T({20: STEAM, 21: GAMESCOPE, 22: REAPER}))
        self.assertEqual([i.action for i in plan.items], [STOP_STEAM_GAME])
        self.assertIn("Fallout 4", plan.items[0].line())
        self.assertIn("Steam stays open", plan.items[0].line())

    def test_an_unknown_game_is_named_by_its_appid(self):
        reaper = dict(REAPER, cmdline=["/storage/roms/steam/steamrtarm64/reaper", "SteamLaunch", "AppId=5", "--"])
        h, plan = self.discover(T({20: STEAM, 22: reaper}))
        self.assertIn("appid 5", plan.items[0].line())

    def test_es_saying_steam_runs_does_not_add_a_game_item_that_could_only_fail(self):
        h, plan = self.discover(T({20: STEAM, 21: GAMESCOPE}), running_game=("Steam", "steam"))
        self.assertNotIn(KILL_EMULATOR, [i.action for i in plan.items])
        self.assertEqual([i.action for i in plan.items], [EXIT_STEAM])

    def test_other_games_keep_their_item(self):
        h, plan = self.discover(T({30: RUNEMU, 31: RA}), kill_data="retroarch\n",
                                running_game=("Sonic", "megadrive"))
        self.assertEqual([i.action for i in plan.items], [KILL_EMULATOR])

    def test_the_plan_lists_the_actions_in_clean_up_order(self):
        h, plan = self.discover(T({20: STEAM, 22: REAPER}))
        self.assertEqual(plan.actions(), [STOP_STEAM_GAME])


class TestStop(Case):
    def run_steam(self, actions, extra, **kw):
        h = self.make(T(extra), **kw)
        plan = h.discover(check_health=False)
        return h, h.execute(plan, actions, confirmed=True)

    def test_stopping_the_game_leaves_steam_alone(self):
        h, steps = self.run_steam([STOP_STEAM_GAME], {20: STEAM, 21: GAMESCOPE, 22: REAPER})
        self.assertTrue(steps[0].ok, steps)
        self.assertEqual(self.procs.sent, [(22, signal.SIGTERM)])
        self.assertIn(20, self.procs.table)
        self.assertIn(21, self.procs.table)

    def test_a_game_that_ignores_the_signal_is_reported_not_claimed(self):
        h, steps = self.run_steam([STOP_STEAM_GAME], {20: STEAM, 22: REAPER}, stubborn=("reaper",))
        self.assertFalse(steps[0].ok)
        self.assertIn("still running", steps[0].text)

    def test_closing_steam_signals_the_client_and_waits_for_gamescope(self):
        # the fake client dies, gamescope does not follow, so it gets its own signal after the wait
        h, steps = self.run_steam([EXIT_STEAM], {20: STEAM, 21: GAMESCOPE})
        self.assertTrue(steps[0].ok, steps)
        self.assertEqual([p for p, _ in self.procs.sent], [20, 21])
        self.assertNotIn(21, self.procs.table)
        self.assertIn("second signal", steps[0].text)

    def test_closing_steam_when_gamescope_follows_the_client_is_one_signal(self):
        h = self.make(T({20: STEAM, 21: GAMESCOPE}))
        plan = h.discover(check_health=False)
        kill = self.procs.kill

        def kill_with_gamescope(pid, sig):
            kill(pid, sig)
            if pid == 20:
                self.procs.table.pop(21, None)
        h._kill = kill_with_gamescope
        steps = h.execute(plan, [EXIT_STEAM], confirmed=True)
        self.assertTrue(steps[0].ok)
        self.assertEqual(steps[0].text, "Steam closed")
        self.assertEqual(self.procs.sent, [(20, signal.SIGTERM)])

    def test_steam_that_will_not_close_is_a_failed_step(self):
        h, steps = self.run_steam([EXIT_STEAM], {20: STEAM, 21: GAMESCOPE}, stubborn=("steam", "gamescope-wl"))
        self.assertFalse(steps[0].ok)
        self.assertIn("still running", steps[0].text)

    def test_only_sigterm_is_ever_sent(self):
        self.run_steam([STOP_STEAM_GAME, EXIT_STEAM], {20: STEAM, 21: GAMESCOPE, 22: REAPER},
                       stubborn=("steam", "gamescope-wl", "reaper"))
        self.assertTrue(self.procs.sent)
        self.assertEqual({s for _, s in self.procs.sent}, {signal.SIGTERM})

    def test_nothing_else_is_signalled(self):
        self.run_steam([STOP_STEAM_GAME, EXIT_STEAM], {20: STEAM, 21: GAMESCOPE, 22: REAPER})
        for pid in (10, 11, 12):                      # ES, rp5deck, sway
            self.assertIn(pid, self.procs.table)
        self.assertEqual({p for p, _ in self.procs.sent} - {20, 21, 22}, set())

    def test_nothing_running_is_an_ok_step_and_signals_nothing(self):
        h, steps = self.run_steam([STOP_STEAM_GAME, EXIT_STEAM], {})
        self.assertEqual([s.ok for s in steps], [True, True])
        self.assertEqual(self.procs.sent, [])

    def test_a_dry_run_signals_nothing_but_logs_what_it_would_do(self):
        h, steps = self.run_steam([EXIT_STEAM], {20: STEAM, 21: GAMESCOPE}, dry_run=True)
        self.assertEqual(self.procs.sent, [])
        self.assertTrue(steps[0].ok)
        self.assertIn("would signal", [r["what"] for r in self.audit_lines()])

    def test_it_needs_the_owners_confirmation(self):
        h = self.make(T({20: STEAM}))
        plan = h.discover(check_health=False)
        with self.assertRaises(Refused):
            h.execute(plan, [EXIT_STEAM], confirmed=False)
        self.assertEqual(self.procs.sent, [])

    def test_a_pid_reused_since_the_scan_is_not_signalled(self):
        h = self.make(T({20: STEAM}))
        self.procs.table[20] = {"comm": "bash", "cmdline": ["/bin/bash"], "ppid": 1}   # same pid, new process
        self.assertFalse(h._send_steam_signal(20, cleanstate.STEAM_CLIENT, EXIT_STEAM))
        self.assertEqual(self.procs.sent, [])
        self.assertIn("skipped: pid changed", [r["what"] for r in self.audit_lines()])

    def test_the_wrong_kind_is_not_signalled(self):
        h = self.make(T({20: STEAM}))
        self.assertFalse(h._send_steam_signal(20, cleanstate.STEAM_REAPER, STOP_STEAM_GAME))
        self.assertEqual(self.procs.sent, [])


if __name__ == "__main__":
    unittest.main()
