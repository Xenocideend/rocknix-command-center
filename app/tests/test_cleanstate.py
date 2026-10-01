#!/usr/bin/env python3
"""cleanstate.py (CC1): the clean-state helper's allow-lists and actions.

Every test runs against a FAKE process table and fake command runner - no
real process is signalled and no command runs - except TestRealProcess
(Linux), which starts a real process NAMED like an emulator (a copy of
`sleep` called "retroarch") and lets the helper close it the ROCKNIX way.

The safety claims each have a test that fails if the guard is removed
(tools/cc1_break_tests.py breaks them one by one and requires red):
  * only ACTIONS / the exact command tuples / KILLABLE names pass;
  * sway can never be restarted or stopped;
  * the idle kill target "emulationstation" and "python3" are never hit;
  * nothing happens without confirmed=True; a dry run signals nothing and
    runs no mutating command, but logs what it would do.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import cleanstate  # noqa: E402
import es_health  # noqa: E402
from cleanstate import (CMD_ENABLE_TOUCH, CMD_GET_INPUTS, CMD_RESTART_ES, ENABLE_TOUCH,  # noqa: E402
                        KILL_EMULATOR, RESTART_ES, STOP_CHILDREN, Refused)

LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc/self")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class Procs:
    """A fake process table + kill(). pid -> dict(comm, cmdline, ppid)."""

    def __init__(self, table=None, stubborn=()):
        self.table = dict(table or {})
        self.stubborn = set(stubborn)       # comms that ignore signals
        self.sent = []

    def kill(self, pid, sig):
        self.sent.append((pid, sig))
        p = self.table.get(pid)
        if p is None:
            raise ProcessLookupError(pid)
        if p["comm"] not in self.stubborn:
            del self.table[pid]


class FakeProbe:
    http_timeout = 2.0
    clk_tck = 100

    def __init__(self, procs, kill_data="emulationstation\n", running_game=None):
        self.procs = procs
        self._kill = kill_data
        self.running_game = running_game
        self.http_calls = []

    def pids(self):
        return sorted(self.procs.table)

    def comm(self, pid):
        p = self.procs.table.get(pid)
        return p["comm"] if p else None

    def cmdline(self, pid):
        p = self.procs.table.get(pid)
        return list(p.get("cmdline") or [p["comm"]]) if p else None

    def ppid(self, pid):
        p = self.procs.table.get(pid)
        return p.get("ppid", 1) if p else None

    def stat(self, pid):
        p = self.procs.table.get(pid)
        return {"state": p.get("state", "S"), "utime": 0, "stime": 0, "starttime": 0}             if p else None

    def es_pids(self):
        return es_health.Probe.es_pids(self)      # the real matcher

    def runemu_pids(self):
        return [p for p in self.pids()
                if any(os.path.basename(a) == "runemu.sh" for a in (self.cmdline(p) or [])[:2])]

    def pids_named(self, name):
        return es_health.Probe.pids_named(self, name)

    def kill_data(self):
        return self._kill

    def http(self, path):
        self.http_calls.append(path)
        if path not in es_health.ALLOWED_HTTP_PATHS:
            raise es_health.HttpRefused(path)
        if path == "/runningGame" and self.running_game:
            return "ok", 0.01, {"name": self.running_game[0], "systemName": self.running_game[1]}
        return "ok", 0.01, {"msg": "NO GAME RUNNING"}


class Runner:
    """subprocess.run stand-in: records argv, answers get_inputs."""

    def __init__(self, touch="enabled", rc=0, on_restart=None):
        self.calls = []
        self.touch = touch
        self.rc = rc
        self.on_restart = on_restart

    def __call__(self, argv, **kw):
        self.calls.append(tuple(argv))
        out = ""
        if tuple(argv) == CMD_GET_INPUTS:
            out = json.dumps([{"identifier": "0:0:generic_ft5x06_(8d)",
                               "libinput": {"send_events": "enabled"}},
                              {"identifier": cleanstate.TOUCH_INT,
                               "libinput": {"send_events": self.touch}}])
        elif tuple(argv) == CMD_ENABLE_TOUCH:
            self.touch = "enabled"
        elif tuple(argv) == CMD_RESTART_ES and self.on_restart:
            self.on_restart()
        return subprocess.CompletedProcess(argv, self.rc, out, "")


class Opener:
    def __init__(self, effect=None):
        self.urls = []
        self.effect = effect

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        if self.effect:
            self.effect()

        class R:
            status = 200

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return R()


class Registry:
    MARKERS = {"web": "rp5deck-web", "yt": "rp5deck-yt", "osk": "wvkbd"}

    def __init__(self, procs, entries=None):
        self.procs = procs
        self._entries = dict(entries or {})
        self.reaped = 0

    def read_cmdline(self, pid):
        p = self.procs.table.get(pid)
        return " ".join(p.get("cmdline") or [p["comm"]]) if p else None

    def entries(self):
        return dict(self._entries)

    def reap_stale(self):
        self.reaped += 1
        done = []
        for kind, e in sorted(self._entries.items()):
            cmd = self.read_cmdline(e["pid"])
            if cmd and e["marker"] in cmd:
                self.procs.kill(e["pid"], signal.SIGTERM)
                done.append((kind, e["pid"]))
        self._entries = {}
        return done


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def health(verdict, summary="x"):
    sig = {"frozen": ["http", "stall"], "suspect": ["stall"]}.get(verdict, [])
    return es_health.Health(verdict, summary, sig, ["note"])


# comm as /proc shows it on the device: the kernel keeps 15 bytes
ES = {"comm": "emulationstatio", "cmdline": ["emulationstation", "--log-path", "/var/log"],
      "ppid": 900}
RUNEMU = {"comm": "bash", "cmdline": ["/bin/bash", "/usr/bin/runemu.sh", "/storage/roms/snes/x.sfc",
                                      "-Psnes", "--core=snes9x", "--emulator=retroarch"],
          "ppid": 1000}
RA = {"comm": "retroarch", "cmdline": ["/usr/bin/retroarch", "-L", "x"], "ppid": 2000}
RP5DECK = {"comm": "python3", "cmdline": ["python3", "/storage/rp5deck/main.py"], "ppid": 1}
GUARD = {"comm": "python3", "cmdline": ["python3", "/storage/rp5deck/focus_guard.py"], "ppid": 1}
SWAY = {"comm": "sway", "cmdline": ["sway"], "ppid": 1}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cleanstate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.audit = os.path.join(self.tmp, "log", "cleanstate.log")

    def make(self, table, kill_data="emulationstation\n", running_game=None, dry_run=False,
             touch="enabled", registry=None, stubborn=(), cgroup="0::/system.slice/"
             "rocknix-autostart.service\n", swissknife=False, emukill_effect=None, runner=None):
        self.procs = Procs(table, stubborn)
        self.probe = FakeProbe(self.procs, kill_data, running_game)
        self.runner = runner or Runner(touch)
        self.opener = Opener(emukill_effect)
        self.clock = Clock()
        self.reg = Registry(self.procs, registry)
        h = cleanstate.Helper(probe=self.probe, dry_run=dry_run, run=self.runner,
                              kill=self.procs.kill, opener=self.opener, registry=self.reg,
                              which=lambda n: "/usr/bin/" + n if swissknife else None,
                              cgroup=cgroup, audit_path=self.audit, clock=self.clock,
                              sleep=self.clock.sleep, health_fn=lambda **kw: health("ok"))
        return h

    def audit_lines(self):
        if not os.path.exists(self.audit):
            return []
        with open(self.audit, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------
class TestGuards(unittest.TestCase):
    def test_action_allow_list(self):
        for a in cleanstate.ACTIONS:
            cleanstate.enforce_action(a)
        for a in ("restart-sway", "kill-all", "reboot", "", "kill-emulator "):
            with self.assertRaises(Refused, msg=a):
                cleanstate.enforce_action(a)

    def test_command_allow_list_is_exact(self):
        for t in (CMD_ENABLE_TOUCH, CMD_RESTART_ES, CMD_GET_INPUTS):
            self.assertEqual(cleanstate.enforce_command(list(t)), t)
        for bad in (("systemctl", "restart", "sway.service"), ("systemctl", "restart", "sway"),
                    ("systemctl", "stop", "essway.service"),
                    ("systemctl", "restart", "essway.service", "sway.service"),
                    ("systemctl", "restart", "pipewire"), ("swaymsg", "exit"),
                    ("swaymsg", "input", cleanstate.TOUCH_INT, "events", "disabled"),
                    ("swaymsg", "input", "*", "events", "enabled"),
                    ("sh", "-c", "systemctl restart essway.service"), ("reboot",), ()):
            with self.assertRaises(Refused, msg=bad):
                cleanstate.enforce_command(bad)

    def test_sway_is_refused_even_if_someone_adds_it_to_the_list(self):
        """The belt: a sway tuple slipped into MUTATING_COMMANDS is still refused."""
        orig = cleanstate.MUTATING_COMMANDS
        try:
            cleanstate.MUTATING_COMMANDS = orig | {("systemctl", "restart", "sway.service"),
                                                   ("systemctl", "restart", "sway")}
            for t in (("systemctl", "restart", "sway.service"), ("systemctl", "restart", "sway")):
                with self.assertRaises(Refused):
                    cleanstate.enforce_command(t)
        finally:
            cleanstate.MUTATING_COMMANDS = orig

    def test_kill_names(self):
        for n in ("retroarch", "retroarch32", "melonDS", "azahar", "cemu", "PortMaster",
                  "dolphin-emu-nogui"):
            cleanstate.enforce_kill_name(n)
        for n in ("emulationstation", "python3", "sway", "pipewire", "wireplumber",
                  "inputplumber", "input_sense", "NetworkManager", "limit-watch",
                  "dp-sleep-guard", "installer", "bash", "systemd", "gamescope", "steam",
                  "unknown-thing", "retroarch;reboot", "../x", ""):
            with self.assertRaises(Refused, msg=n):
                cleanstate.enforce_kill_name(n)

    def test_protected_and_killable_do_not_overlap(self):
        self.assertEqual(cleanstate.KILLABLE_NAMES & cleanstate.PROTECTED_NAMES, frozenset())

    def test_signal_flags(self):
        self.assertEqual(cleanstate.signal_for(None), "SIGTERM")
        self.assertEqual(cleanstate.signal_for("-9"), "SIGKILL")
        self.assertEqual(cleanstate.signal_for("-HUP"), "SIGHUP")
        with self.assertRaises(Refused):
            cleanstate.signal_for("-STOP")


# ---------------------------------------------------------------------------
# kill-emulator
# ---------------------------------------------------------------------------
class TestKillEmulator(Case):
    def table(self, **extra):
        t = {900: {"comm": "bash", "cmdline": ["/bin/bash", "/usr/bin/start_es.sh"], "ppid": 1},
             1000: ES, 2000: RUNEMU, 2100: RA, 3000: RP5DECK, 3100: GUARD, 10: SWAY}
        t.update(extra)
        return t

    def test_retroarch_the_rocknix_way(self):
        def game_ends():                    # SIGTERM to retroarch -> runemu.sh returns
            self.procs.table.pop(2000, None)
        h = self.make(self.table(), kill_data="retroarch retroarch32\n",
                      running_game=("Super Metroid", "snes"))
        self.procs.kill_orig = self.procs.kill
        plan = h.discover(check_health=False)
        self.assertEqual(plan.actions(), [KILL_EMULATOR])
        self.assertIn("Super Metroid (snes)", plan.stop_list()[0])
        self.assertIn("retroarch (SIGTERM)", plan.stop_list()[0])
        orig = self.procs.kill

        def kill(pid, sig):
            orig(pid, sig)
            game_ends()
        h._kill = kill
        steps = h.execute(plan, [KILL_EMULATOR], confirmed=True)
        self.assertEqual(self.opener.urls, [cleanstate.EMUKILL_URL])     # ES's own route first
        self.assertEqual(self.procs.sent, [(2100, signal.SIGTERM)])        # then the kill target
        self.assertTrue(steps[0].ok, steps)
        for pid in (1000, 3000, 3100, 10):                                 # ES, rp5deck, guard, sway
            self.assertIn(pid, self.procs.table)
        self.assertTrue(any(r["what"] == "signalled" and r["pid"] == 2100
                            for r in self.audit_lines()))

    def test_emukill_that_works_means_no_signal(self):
        def es_kills():
            self.procs.table.pop(2100, None)
            self.procs.table.pop(2000, None)
        h = self.make(self.table(), kill_data="retroarch retroarch32\n", swissknife=True,
                      emukill_effect=es_kills)
        steps = h.execute(h.discover(check_health=False), [KILL_EMULATOR], confirmed=True)
        self.assertEqual(self.procs.sent, [])
        self.assertEqual(steps[0].text, "Game closed by ES's /emukill")

    def test_minus_9_target_gets_sigkill(self):
        t = self.table()
        t[2100] = {"comm": "melonDS", "cmdline": ["/usr/bin/melonDS"], "ppid": 2000}
        h = self.make(t, kill_data="-9 melonDS\n")
        h.execute(h.discover(check_health=False), [KILL_EMULATOR], confirmed=True)
        self.assertEqual(self.procs.sent, [(2100, getattr(signal, "SIGKILL", 9))])

    def test_idle_kill_file_never_kills_es(self):
        t = self.table()
        del t[2000], t[2100]                    # no game
        h = self.make(t, kill_data="emulationstation\n")
        plan = h.discover(check_health=False)
        self.assertEqual(plan.items, [])
        steps = h.execute(plan, [KILL_EMULATOR], confirmed=True)
        self.assertEqual(self.procs.sent, [])
        self.assertEqual(self.opener.urls, [])
        self.assertEqual(steps[0].text, "No game was running")

    def test_es_named_by_the_kill_file_during_a_game_is_never_hit(self):
        """runemu.sh running, but the kill file still says emulationstation
        (its idle value): ES must not be signalled, whatever else happens."""
        t = self.table()
        del t[2100]
        h = self.make(t, kill_data="emulationstation")
        steps = h.execute(h.discover(check_health=False), [KILL_EMULATOR], confirmed=True)
        self.assertNotIn(1000, [p for p, _ in self.procs.sent])
        self.assertIn(1000, self.procs.table)
        self.assertFalse(steps[0].ok)

    def test_python3_target_is_refused(self):
        """GPcal sets the kill target to python3 - which is also rp5deck and
        focus_guard. Refused by name."""
        t = self.table()
        t[2100] = {"comm": "python3", "cmdline": ["python3", "/usr/bin/gpcal.py"], "ppid": 2000}
        h = self.make(t, kill_data="python3\n")
        plan = h.discover(check_health=False)
        self.assertTrue(any("python3" in n for n in plan.notes), plan.notes)
        steps = h.execute(plan, [KILL_EMULATOR], confirmed=True)
        self.assertEqual(self.procs.sent, [])
        self.assertFalse(steps[0].ok)
        self.assertIn(3000, self.procs.table)

    def test_unknown_target_is_refused(self):
        t = self.table()
        t[2100] = {"comm": "mystery", "ppid": 2000}
        h = self.make(t, kill_data="mystery\n")
        steps = h.execute(h.discover(check_health=False), [KILL_EMULATOR], confirmed=True)
        self.assertNotIn((2100, signal.SIGTERM), self.procs.sent)
        self.assertFalse(steps[0].ok)

    def test_a_reused_pid_is_not_hit(self):
        """The name is re-read right before the signal."""
        h = self.make(self.table(), kill_data="retroarch\n")
        plan = h.discover(check_health=False)
        orig = self.probe.pids_named
        self.probe.pids_named = lambda n: [3000] if n == "retroarch" else orig(n)
        h.execute(plan, [KILL_EMULATOR], confirmed=True)
        self.assertEqual(self.procs.sent, [])       # 3000 is rp5deck's python3, not retroarch
        self.assertIn(3000, self.procs.table)

    def test_port_programs_in_runemus_subtree(self):
        t = self.table()
        del t[2100]
        t[2000] = dict(RUNEMU, cmdline=["/bin/bash", "/usr/bin/runemu.sh",
                                        "/storage/roms/ports/Half-Life.sh", "-Pports",
                                        "--emulator=ports"])
        t[2200] = {"comm": "bash", "cmdline": ["bash", "/storage/roms/ports/Half-Life.sh"],
                   "ppid": 2000}
        t[2300] = {"comm": "xash3d", "cmdline": ["./xash3d"], "ppid": 2200}
        t[2400] = {"comm": "gptokeyb", "cmdline": ["gptokeyb", "-k", "xash3d"], "ppid": 2200}
        t[2500] = {"comm": "python3", "cmdline": ["python3", "/usr/share/lowerdeck/ra_proxy.py"],
                   "ppid": 2000}

        def after_kill(pid, sig):
            self.procs.table.pop(pid, None)
            if 2300 not in self.procs.table:
                for p in (2200, 2000):
                    self.procs.table.pop(p, None)      # the port script finishes
            self.procs.sent.append((pid, sig))
        h = self.make(t, kill_data=None, running_game=("Half-Life", "ports"))
        h._kill = after_kill
        plan = h.discover(check_health=False)
        self.assertIn("xash3d (2300)", plan.stop_list()[0])
        steps = h.execute(plan, [KILL_EMULATOR], confirmed=True)
        self.assertEqual(sorted(p for p, _ in self.procs.sent), [2300, 2400])
        self.assertIn(2500, self.procs.table)       # lowerdeck's proxy is not the port's
        self.assertIn(3000, self.procs.table)
        self.assertTrue(steps[0].ok, steps)

    def port_table(self):
        t = self.table()
        del t[2100]
        t[2000] = dict(RUNEMU, cmdline=["/bin/bash", "/usr/bin/runemu.sh",
                                        "/storage/roms/ports/Half-Life.sh", "-Pports"])
        t[2200] = {"comm": "bash", "cmdline": ["bash", "Half-Life.sh"], "ppid": 2000}
        t[2300] = {"comm": "xash3d", "ppid": 2200}
        t[2400] = {"comm": "gptokeyb", "ppid": 2200}
        t[2500] = {"comm": "python3", "cmdline": ["python3", "/usr/share/lowerdeck/ra_proxy.py"],
                   "ppid": 2000}
        return t

    def test_port_processes_lists_only_the_ports_programs(self):
        """Layer 1: the subtree scan leaves out shells and protected names."""
        h = self.make(self.port_table(), kill_data=None)
        self.assertEqual(h.port_processes([2000]), [(2300, "xash3d"), (2400, "gptokeyb")])

    def test_truncated_comm_of_a_long_protected_name_is_still_protected(self):
        """/proc comm is 15 bytes: ES is "emulationstatio" on the device and
        rocknix-fake-suspend is "rocknix-fake-su". Both must stay protected
        if they ever sit under runemu.sh, in both layers."""
        t = self.port_table()
        t[2600] = {"comm": "emulationstatio", "cmdline": ["emulationstation"], "ppid": 2200}
        t[2700] = {"comm": "rocknix-fake-su", "cmdline": ["rocknix-fake-suspend"], "ppid": 2200}
        h = self.make(t, kill_data=None)
        self.assertEqual(h.port_processes([2000]), [(2300, "xash3d"), (2400, "gptokeyb")])
        for pid, comm in ((2600, "emulationstatio"), (2700, "rocknix-fake-su")):
            with self.assertRaises(Refused, msg=comm):
                h._send_port_signal(pid, comm)
        self.assertEqual(self.procs.sent, [])

    def test_port_signal_gate_refuses_protected_names(self):
        """Layer 2: the port signal gate refuses them again on its own."""
        h = self.make(self.port_table(), kill_data=None)
        for pid, comm in ((2500, "python3"), (2200, "bash"), (1000, "emulationstation")):
            with self.assertRaises(Refused, msg=comm):
                h._send_port_signal(pid, comm)
        self.assertEqual(self.procs.sent, [])

    def test_stubborn_game_is_reported_not_escalated(self):
        h = self.make(self.table(), kill_data="retroarch\n", stubborn={"retroarch"})
        steps = h.execute(h.discover(check_health=False), [KILL_EMULATOR], confirmed=True)
        self.assertEqual(self.procs.sent, [(2100, signal.SIGTERM)])   # once, not SIGKILL
        self.assertFalse(steps[0].ok)
        self.assertIn("L1+SELECT+START", steps[0].text)


class TestEmukillGuard(Case):
    def test_only_the_constant_url_and_only_confirmed(self):
        h = self.make({})
        with self.assertRaises(Refused):
            h.emukill(confirmed=True, url="http://127.0.0.1:1234/quit")
        with self.assertRaises(Refused):
            h.emukill(confirmed=False)
        self.assertEqual(self.opener.urls, [])
        h.emukill(confirmed=True)
        self.assertEqual(self.opener.urls, [cleanstate.EMUKILL_URL])

    def test_dry_run_sends_nothing(self):
        h = self.make({}, dry_run=True)
        self.assertEqual(h.emukill(confirmed=True), (True, "dry run"))
        self.assertEqual(self.opener.urls, [])


# ---------------------------------------------------------------------------
# confirmation, dry run, logging
# ---------------------------------------------------------------------------
class TestConfirmAndDryRun(Case):
    def table(self):
        return {1000: ES, 2000: RUNEMU, 2100: RA, 3000: RP5DECK,
                4000: {"comm": "firefox", "cmdline": ["firefox", "--name", "rp5deck-web"]},
                4100: {"comm": "wvkbd-mobintl", "cmdline": ["wvkbd-mobintl", "-L", "360"]}}

    def regs(self):
        return {"web": {"pid": 4000, "marker": "rp5deck-web"},
                "osk": {"pid": 4100, "marker": "wvkbd"}}

    def test_execute_needs_confirmation(self):
        h = self.make(self.table(), kill_data="retroarch\n", registry=self.regs())
        plan = h.discover(check_health=False)
        with self.assertRaises(Refused):
            h.execute(plan, [KILL_EMULATOR, STOP_CHILDREN])
        self.assertEqual(self.procs.sent, [])
        self.assertEqual(self.reg.reaped, 0)

    def test_unknown_action_refuses_the_whole_call(self):
        h = self.make(self.table(), registry=self.regs())
        with self.assertRaises(Refused):
            h.execute(h.discover(check_health=False), [STOP_CHILDREN, "restart-sway"],
                      confirmed=True)
        self.assertEqual(self.reg.reaped, 0)

    def test_dry_run_touches_nothing_and_logs_everything(self):
        h = self.make(self.table(), kill_data="retroarch\n", registry=self.regs(), dry_run=True,
                      touch="disabled")
        h._health_fn = lambda **kw: health("frozen", "ES isn't responding (x)")
        plan = h.discover()
        self.assertEqual(plan.actions(), [KILL_EMULATOR, STOP_CHILDREN, ENABLE_TOUCH])
        steps = h.execute(plan, cleanstate.ACTIONS)
        self.assertEqual(self.procs.sent, [])
        self.assertEqual(self.reg.reaped, 0)
        self.assertEqual(self.opener.urls, [])
        mutating = [c for c in self.runner.calls if c in cleanstate.MUTATING_COMMANDS]
        self.assertEqual(mutating, [])
        self.assertTrue(all(s.ok for s in steps), steps)
        whats = [(r["action"], r["what"]) for r in self.audit_lines()]
        for want in (("kill-emulator", "would GET"), ("kill-emulator", "would signal"),
                     ("stop-rp5deck-children", "would stop"), ("command", "would run")):
            self.assertIn(want, whats)
        would_run = [r["argv"] for r in self.audit_lines() if r["what"] == "would run"]
        self.assertIn(list(CMD_RESTART_ES), would_run)
        self.assertIn(list(CMD_ENABLE_TOUCH), would_run)
        self.assertTrue(all(r["dry_run"] for r in self.audit_lines()))

    def test_plan_lists_the_exact_stop_list(self):
        h = self.make(self.table(), kill_data="retroarch retroarch32\n", registry=self.regs(),
                      touch="disabled", running_game=("Super Metroid", "snes"))
        plan = h.discover(check_health=False)
        self.assertEqual(plan.stop_list(), [
            "Game: Super Metroid (snes) - ES's /emukill first, then ROCKNIX's kill target "
            "retroarch (SIGTERM)",
            "On-screen keyboard - running",
            "Browser / Discord - running",
            "Built-in touch screen - turn it back on (it is off)"])


# ---------------------------------------------------------------------------
# children, touch, restart
# ---------------------------------------------------------------------------
class TestChildrenAndTouch(Case):
    def test_children_stop_through_the_registry(self):
        t = {4000: {"comm": "firefox", "cmdline": ["firefox", "--name", "rp5deck-web"]},
             4200: {"comm": "firefox", "cmdline": ["firefox"]}}        # the owner's own? not ours
        h = self.make(t, registry={"web": {"pid": 4000, "marker": "rp5deck-web"},
                                   "yt": {"pid": 4200, "marker": "rp5deck-yt"}})
        plan = h.discover(check_health=False)
        self.assertEqual(plan.children, [("web", 4000)])                 # marker check
        steps = h.execute(plan, [STOP_CHILDREN], confirmed=True)
        self.assertTrue(steps[0].ok, steps)
        self.assertNotIn(4000, self.procs.table)
        self.assertIn(4200, self.procs.table)

    def test_touch_only_when_disabled(self):
        h = self.make({}, touch="enabled")
        plan = h.discover(check_health=False)
        self.assertEqual(plan.touch, "enabled")
        steps = h.execute(plan, [ENABLE_TOUCH], confirmed=True)
        self.assertNotIn(CMD_ENABLE_TOUCH, self.runner.calls)
        self.assertEqual(steps[0].text, "Touch was already on")

        h = self.make({}, touch="disabled")
        steps = h.execute(h.discover(check_health=False), [ENABLE_TOUCH], confirmed=True)
        self.assertEqual([c for c in self.runner.calls if c in cleanstate.MUTATING_COMMANDS],
                         [CMD_ENABLE_TOUCH])
        self.assertTrue(steps[0].ok, steps)


class TestRestart(Case):
    def make_restart(self, verdict, cgroup="0::/system.slice/rocknix-autostart.service\n"):
        def restarted():
            self.procs.table.pop(1000, None)
            self.procs.table[1500] = ES
        runner = Runner(on_restart=restarted)
        h = self.make({1000: ES}, runner=runner, cgroup=cgroup)
        h._health_fn = lambda **kw: health(verdict, "ES isn't responding (HTTP timeout)")
        return h, h.discover()

    def test_frozen_restarts_essway_only(self):
        h, plan = self.make_restart("frozen")
        steps = h.execute(plan, [RESTART_ES], confirmed=True)
        self.assertEqual([c for c in self.runner.calls if c in cleanstate.MUTATING_COMMANDS],
                         [CMD_RESTART_ES])
        self.assertTrue(steps[0].ok, steps)
        self.assertEqual(steps[0].text, "EmulationStation restarted")
        self.assertFalse(any(a in ("sway", "sway.service") for c in self.runner.calls for a in c))

    def test_healthy_es_is_not_restarted(self):
        for v in ("ok", "game", "starting", "unknown"):
            h, plan = self.make_restart(v)
            steps = h.execute(plan, [RESTART_ES], confirmed=True)
            self.assertFalse(steps[0].ok, v)
            self.assertNotIn(CMD_RESTART_ES, self.runner.calls, v)

    def test_suspect_needs_force(self):
        h, plan = self.make_restart("suspect")
        steps = h.execute(plan, [RESTART_ES], confirmed=True)
        self.assertNotIn(CMD_RESTART_ES, self.runner.calls)
        self.assertFalse(steps[0].ok)
        steps = h.execute(plan, [RESTART_ES], confirmed=True, force_restart=True)
        self.assertIn(CMD_RESTART_ES, self.runner.calls)
        self.assertTrue(steps[0].ok, steps)

    def test_force_does_not_override_a_healthy_verdict(self):
        h, plan = self.make_restart("ok")
        h.execute(plan, [RESTART_ES], confirmed=True, force_restart=True)
        self.assertNotIn(CMD_RESTART_ES, self.runner.calls)

    def test_refused_from_inside_essway(self):
        h, plan = self.make_restart("frozen", cgroup="0::/system.slice/essway.service\n")
        steps = h.execute(plan, [RESTART_ES], confirmed=True)
        self.assertNotIn(CMD_RESTART_ES, self.runner.calls)
        self.assertIn("inside essway", steps[0].text)


# ---------------------------------------------------------------------------
# A real process named like an emulator (Linux)
# ---------------------------------------------------------------------------
@unittest.skipUnless(LINUX, "needs Linux /proc")
class TestRealProcess(unittest.TestCase):
    def test_closes_a_real_retroarch_by_name_and_nothing_else(self):
        """A symlink named retroarch to this Python: the kernel sets comm from
        the exec'd name, so /proc says "retroarch". (A copy of `sleep` does
        not work on uutils systems: the multicall binary dispatches on
        argv[0] and exits at once.)"""
        d = tempfile.mkdtemp(prefix="cleanstate-real-")
        self.addCleanup(shutil.rmtree, d, True)
        exe = os.path.join(d, "retroarch")
        os.symlink(sys.executable, exe)
        nap = ["-c", "import time; time.sleep(60)"]
        game = subprocess.Popen([exe] + nap)
        bystander = subprocess.Popen([sys.executable] + nap)
        for p in (game, bystander):
            self.addCleanup(lambda p=p: (p.kill(), p.wait(5)))
        kill_file = os.path.join(d, "process-kill-data")
        with open(kill_file, "w") as f:
            f.write("retroarch retroarch32\n")
        probe = es_health.Probe(kill_data=kill_file)
        probe.http = lambda path: ("refused", 0.0, None)
        probe.runemu_pids = lambda: []
        time.sleep(0.2)
        self.assertEqual(probe.pids_named("retroarch"), [game.pid])
        h = cleanstate.Helper(probe=probe, opener=Opener(), registry=Registry(Procs()),
                              run=Runner(), audit_path=os.path.join(d, "audit.log"),
                              which=lambda n: None, verify_s=5.0)
        plan = h.discover(check_health=False)
        self.assertEqual(plan.actions(), [KILL_EMULATOR])
        steps = h.execute(plan, [KILL_EMULATOR], confirmed=True)
        self.assertTrue(steps[0].ok, steps)
        self.assertEqual(game.wait(5), -signal.SIGTERM)
        self.assertIsNone(bystander.poll())
        self.assertEqual(h.signalled, [(game.pid, "retroarch", "SIGTERM")])


if __name__ == "__main__":
    unittest.main()
