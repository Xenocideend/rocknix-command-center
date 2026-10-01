#!/usr/bin/env python3
"""cleanstate: the helper behind the Clean up button. It stops what's running and puts the
device back how it is after boot, from an explicit allow list, logging every action, with a
dry run mode, and nothing else.

rp5deck runs as root with no privilege separation, so this module is the separation.
main.py (through cleanstate_view.py) only calls Helper.discover() and Helper.execute(), and
everything that signals a process or runs a command lives here behind the guards below.
tests/test_cleanstate.py breaks each guard to prove the suite notices.

The actions (ACTIONS, nothing else is accepted):

  kill-emulator          closes the running game the way ROCKNIX does:
                         1. ES's own GET /emukill through emukill(), a separate named
                            function so es_api.py's allow list stays closed. In ES's
                            source ApiSystem::emuKill() runs `batocera-es-swissknife
                            --emukill`, a script that isnt anywhere in ROCKNIX's tree, so
                            on this build /emukill probably does nothing. It's still tried
                            first, checked by watching, and not waited on long.
                         2. ROCKNIX's own kill target, the name(s) in
                            /tmp/.process-kill-data that set_kill writes and input_sense's
                            L1+SELECT+START runs as `killall ${TO_KILL}`. Only names get
                            read from the file, never pids, and each has to be on
                            KILLABLE_NAMES and not PROTECTED_NAMES (the file says
                            "emulationstation" when no game runs and "python3" while
                            GPcal runs, so both get refused by name). The signal follows
                            the file's own flag (-9 -> SIGKILL, none -> SIGTERM) like
                            killall would.
                         3. a PortMaster port (ports dont set a kill target, runemu.sh
                            calls `set_kill stop`): SIGTERM to the processes in
                            runemu.sh's own subtree that arent shells, interpreters or
                            protected names (the port binary, gptokeyb), and the port
                            script runs its own cleanup.
  stop-rp5deck-children  Firefox, mpv and wvkbd through web_tiles.ChildRegistry.reap_stale(),
                         the existing orphan stop (a pid only gets signalled while its
                         cmdline still has the marker saved with it). cleanstate_view ends
                         the live session through WebApps.end_session() on the UI thread
                         first.
  enable-touch           `swaymsg input 0:0:generic_ft5x06_(a0) events enabled`, the same
                         command the dual-screen daemon runs in apply_layout(), and only when
                         `swaymsg -t get_inputs` says the device is off. runemu.sh turns
                         this touchscreen off after every game when
                         DEVICE_HAS_DUAL_SCREEN=true. The Command Center daemon pins that
                         flag off, but the pin can be skipped or land a boot late, and after
                         boot touch is on. It never changes the mapping, the daemon owns
                         map_to_output.
  restart-essway         `systemctl restart essway.service`, ES only, never sway
                         (restarting sway costs the internal panel, and essway.service
                         Requires=sway.service). Only when es_health offers it
                         (NOT_RUNNING / NO_WINDOW / FROZEN) or you picked "Restart anyway"
                         on a SUSPECT verdict. Refused if this process is in essway's
                         cgroup, since the restart would kill it halfway. A game ES started
                         is in that cgroup too, so the restart ends it as well.

The only commands this can run are the exact argv tuples in MUTATING_COMMANDS and
READ_COMMANDS (enforce_command()).

Never touched: system services, sway, the rp5deck daemons, pipewire/wireplumber,
inputplumber, input_sense, NetworkManager, the charger scripts (limit-watch,
dp-sleep-guard), ES itself (except the explicit restart), anything not named above.

CLI (on the device, plan only unless told otherwise):
    python3 cleanstate.py                      # show the plan (stop list + ES health)
    python3 cleanstate.py run --dry-run ACTION...
    python3 cleanstate.py run --i-confirm ACTION...  [--force-restart]
"""
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import es_health
import screen_map

log = logging.getLogger("rp5deck.cleanstate")

KILL_EMULATOR = "kill-emulator"
STOP_STEAM_GAME = "stop-steam-game"
EXIT_STEAM = "exit-steam"
STOP_CHILDREN = "stop-rp5deck-children"
ENABLE_TOUCH = "enable-touch"
RESTART_ES = "restart-essway"
ACTIONS = (KILL_EMULATOR, STOP_STEAM_GAME, EXIT_STEAM, STOP_CHILDREN, ENABLE_TOUCH, RESTART_ES)
CLEAN_ACTIONS = (KILL_EMULATOR, STOP_STEAM_GAME, EXIT_STEAM, STOP_CHILDREN, ENABLE_TOUCH)  # "Clean up"

# Steam, only while it is open (nested in sway, so this process is running): the three programs of a
# session, found by their own command line and never by a bare name, signalled with SIGTERM only.
STEAM_CLIENT, STEAM_REAPER, STEAM_GAMESCOPE = "client", "reaper", "gamescope"
_STEAM_APPID = re.compile(r"^AppId=(\d+)$")

# ES's /emukill, compared exactly in emukill(), never built from input
EMUKILL_URL = "http://127.0.0.1:1234/emukill"
SWISSKNIFE = "batocera-es-swissknife"

TOUCH_INT = "0:0:generic_ft5x06_(a0)"      # dual-screen-layout-and-power TOUCH_INT
CMD_ENABLE_TOUCH = ("swaymsg", "input", TOUCH_INT, "events", "enabled")
CMD_RESTART_ES = ("systemctl", "restart", "essway.service")
CMD_GET_INPUTS = ("swaymsg", "-t", "get_inputs", "-r")
MUTATING_COMMANDS = frozenset({CMD_ENABLE_TOUCH, CMD_RESTART_ES})
READ_COMMANDS = frozenset({CMD_GET_INPUTS})

# every set_kill target in ROCKNIX that's a game, emulator, player or PortMaster (73 files)
KILLABLE_NAMES = frozenset({
    "retroarch", "retroarch32", "mednafen", "melonDS", "azahar", "SkyEmu", "vita3k-sa",
    "rpcs3", "rpcs3-sa", "dolphin-emu", "dolphin-emu-nogui", "xemu", "touchHLE", "scummvm",
    "ppsspp", "mpv", "moonlight", "hatarisa", "flycast", "daedalus", "cemu", "bigpemu",
    "armsx2-qt", "aethersx2", "aethersx2-sa", "PortMaster", "gmu.bin", "yabasanshiro",
    "supermodel", "mupen64plus", "m8c", "gzdoom", "gopher64", "drastic", "ares", "amiberry",
    "NanoBoyAdvance", "heroic", "Heroic", "AppRun.wrapped",
})
# Refused by name even if a kill file, a subtree or anything else names them. Includes the
# set_kill targets that arent games: ES itself (the idle value), python3 (GPcal, and also
# rp5deck and focus_guard), the installer, the terminals, and the Steam/gamescope stack (sway
# is stopped then so rp5deck isnt running).
PROTECTED_NAMES = frozenset({
    "emulationstation", "start_es.sh", "python", "python3", "installer", "foot", "qterminal",
    "commander", "gamepad-tester", "sdltouchtest", "FEXConfig", "FEX", "steam", "gamescope",
    "sway", "swaybg", "swaymsg", "Xwayland", "systemd", "systemd-logind", "systemd-udevd",
    "udevd", "dbus-daemon", "bash", "sh", "dash", "busybox", "pipewire", "pipewire-pulse",
    "wireplumber", "inputplumber", "input_sense", "evtest", "NetworkManager", "wpa_supplicant",
    "iwd", "connmand", "sshd", "dropbear", "limit-watch", "dp-sleep-guard",
    "rocknix-fake-suspend", "runemu.sh", "gptokeyb-helper",
})
# the same sets the way /proc/<pid>/comm shows them, the kernel keeps 15 bytes so
# "emulationstation" is "emulationstatio" there
_KILLABLE_COMM = frozenset(n[:15] for n in KILLABLE_NAMES)
_PROTECTED_COMM = frozenset(n[:15] for n in PROTECTED_NAMES)
_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_SHELLS = frozenset({"bash", "sh", "dash", "busybox", "timeout", "env", "nohup", "setsid"})

_SIG = {"-9": "SIGKILL", "-KILL": "SIGKILL", "-15": "SIGTERM", "-TERM": "SIGTERM",
        "-1": "SIGHUP", "-HUP": "SIGHUP", "-2": "SIGINT", "-INT": "SIGINT", None: "SIGTERM"}
_SIGNUM = {"SIGKILL": 9, "SIGTERM": 15, "SIGHUP": 1, "SIGINT": 2}

NEVER_TOUCHED = ("Never touched: system services, sway, the dual-screen scripts (092-095), "
                 "audio, controllers, network, charging.")


class Refused(Exception):
    """An action, command or signal target outside the allow lists."""


# ---------------------------------------------------------------------------
# The guards (pure)
# ---------------------------------------------------------------------------
def enforce_action(name):
    if name not in ACTIONS:
        raise Refused("cleanstate refuses action %r; allowed: %s" % (name, ", ".join(ACTIONS)))


def enforce_command(argv):
    """The whole command allow list. It also refuses anything naming sway as a unit to stop or
    restart, a second check on top of sway not being in the sets.
    """
    t = tuple(argv)
    lowered = [a.lower() for a in t]
    if t and t[0] == "systemctl" and any(a in ("sway", "sway.service") for a in lowered):
        raise Refused("cleanstate never touches sway: %r" % (t,))
    if t not in MUTATING_COMMANDS and t not in READ_COMMANDS:
        raise Refused("cleanstate refuses to run %r" % (t,))
    return t


def enforce_kill_name(name):
    """A process name from /tmp/.process-kill-data (or a port subtree) can only be signalled if
    it's a known game or emulator and not protected.
    """
    if not _NAME_RE.match(name or ""):
        raise Refused("kill target %r is not a plain process name" % (name,))
    if name in PROTECTED_NAMES:
        raise Refused("kill target %r is protected" % name)
    if name not in KILLABLE_NAMES:
        raise Refused("kill target %r is not on the allow-list (use L1+SELECT+START)" % name)
    return name


def steam_kind(comm, cmd):
    """Which Steam program a process is (STEAM_CLIENT / STEAM_REAPER / STEAM_GAMESCOPE), or None. Both the
    process name and the command line have to say so: a game's own `steam` helper or an unrelated
    gamescope is not one of ours. For a reaper, also the appid it launched."""
    cmd = list(cmd or [])
    if not cmd or not _NAME_RE.match(comm or ""):
        return None
    exe = cmd[0]
    if comm == "steam" and exe.endswith("/steamrtarm64/steam") and "-gamepadui" in cmd:
        return STEAM_CLIENT, None
    if comm == "reaper" and exe.endswith("/steamrtarm64/reaper") and "SteamLaunch" in cmd:
        i = cmd.index("SteamLaunch")
        m = _STEAM_APPID.match(cmd[i + 1]) if i + 1 < len(cmd) else None
        return (STEAM_REAPER, int(m.group(1))) if m else None
    if comm.startswith("gamescope") and exe.rsplit("/", 1)[-1] == "gamescope" and "--backend" in cmd:
        return STEAM_GAMESCOPE, None
    return None


def signal_for(flag):
    """killall's flag from the kill file -> signal name, unknown flags get refused."""
    if flag not in _SIG:
        raise Refused("kill signal %r is not one ROCKNIX uses" % (flag,))
    return _SIG[flag]


def _signum(name):
    return getattr(signal, name, _SIGNUM[name])


# ---------------------------------------------------------------------------
# The plan: what's running and what would be stopped (the confirm's stop list)
# ---------------------------------------------------------------------------
class Item:
    def __init__(self, action, label, detail=""):
        self.action, self.label, self.detail = action, label, detail

    def line(self):
        return "%s - %s" % (self.label, self.detail) if self.detail else self.label

    def to_dict(self):
        return {"action": self.action, "label": self.label, "detail": self.detail}


class Plan:
    def __init__(self):
        self.items = []  # the stop list, in order
        self.health = None          # es_health.Health, or None if not checked
        self.game = {}              # what kill-emulator saw
        self.children = []          # [(kind, pid)] with a live marker
        self.touch = None  # "enabled" / "disabled" / None (unknown or missing)
        self.steam = {}             # {"client": [pid], "reapers": [(pid, appid)], "gamescope": [pid]}
        self.notes = []

    def actions(self):
        return [a for a in CLEAN_ACTIONS if any(i.action == a for i in self.items)]

    def stop_list(self):
        return [i.line() for i in self.items]

    @property
    def restart_offered(self):
        return bool(self.health and self.health.offer_restart)

    @property
    def restart_forceable(self):
        return bool(self.health and self.health.allow_force)

    def to_dict(self):
        return {"items": [i.to_dict() for i in self.items], "actions": self.actions(),
                "health": self.health.to_dict() if self.health else None,
                "game": self.game, "children": self.children, "touch": self.touch,
                "steam": self.steam, "notes": self.notes, "never": NEVER_TOUCHED}


class Step:
    def __init__(self, action, ok, text, detail=None):
        self.action, self.ok, self.text, self.detail = action, ok, text, detail or {}

    def to_dict(self):
        return {"action": self.action, "ok": self.ok, "text": self.text, "detail": self.detail}

    def __repr__(self):
        return "Step(%s, %s, %r)" % (self.action, self.ok, self.text)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------
def _default_log_dir():
    return os.environ.get("RP5DECK_LOG_DIR", "/storage/rp5deck/log")


def _default_registry():
    return os.path.join(os.environ.get("RP5DECK_RUN_DIR", "/run/rp5deck"), "children.json")


class Helper:
    """discover() -> Plan, execute(plan, actions, confirmed) -> [Step]. Every I/O seam can be
    swapped for tests: probe (es_health.Probe), run (like subprocess.run), kill (like os.kill),
    opener (urllib), registry (like web_tiles.ChildRegistry), which (like shutil.which), cgroup
    text, clock/sleep, health_fn (like es_health.check).
    """

    def __init__(self, probe=None, dry_run=False, run=None, kill=None, opener=None,
                 registry=None, which=shutil.which, cgroup=None, audit_path=None,
                 clock=time.monotonic, sleep=time.sleep, health_fn=None, es_output=screen_map.CURRENT.top,
                 verify_s=5.0, emukill_wait_s=4.0, steam_wait_s=15.0, steam_name=None):
        self.steam_wait_s = steam_wait_s
        self._steam_name = steam_name       # appid -> name, for the stop list (steam_library by default)
        self.probe = probe or es_health.Probe()
        self.dry_run = bool(dry_run)
        self._run = run or subprocess.run
        self._kill = kill if kill is not None else getattr(os, "kill", None)
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._registry = registry
        self._which = which
        self._cgroup = cgroup
        self.audit_path = audit_path if audit_path is not None else os.path.join(
            _default_log_dir(), "cleanstate.log")
        self.clock, self.sleep = clock, sleep
        self._health_fn = health_fn or es_health.check
        self.es_output = es_output
        self.verify_s = verify_s
        self.emukill_wait_s = emukill_wait_s
        self.signalled = []  # [(pid, name, signal)], also in the audit log
        self.commands = []  # every argv run, or that a dry run would have run

    # -- logging ------------------------------------------------------------
    def audit(self, action, what, **detail):
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, "what": what,
               "dry_run": self.dry_run}
        rec.update(detail)
        log.info("cleanstate %s%s: %s %s", "[dry-run] " if self.dry_run else "", action, what,
                 json.dumps(detail, default=str) if detail else "")
        if not self.audit_path:
            return
        try:
            os.makedirs(os.path.dirname(self.audit_path) or ".", exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            log.warning("could not append to %s", self.audit_path)

    # -- the two gated primitives -------------------------------------------
    def run_command(self, argv, timeout=10.0):
        """Runs one allow-listed command. Mutating ones are only logged in a dry run, read-only ones
        run either way.
        """
        t = enforce_command(argv)
        self.commands.append(t)
        mutating = t in MUTATING_COMMANDS
        if mutating and self.dry_run:
            self.audit("command", "would run", argv=list(t))
            return 0, ""
        env = dict(os.environ)
        if t[0] == "swaymsg" and not env.get("SWAYSOCK"):
            try:
                import sway_ipc
                sock = sway_ipc.find_socket()
                if sock:
                    env["SWAYSOCK"] = sock
            except Exception:       # noqa: BLE001
                pass
        try:
            r = self._run(list(t), capture_output=True, text=True, timeout=timeout, env=env)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError) as e:
            rc, out = -1, str(e)
        if mutating:
            self.audit("command", "ran", argv=list(t), rc=rc, output=out.strip()[-300:])
        return rc, out

    def _send_signal(self, pid, name, signame, action):
        """Signals one pid the caller found by name (or subtree) in this process's own /proc scan.
        Checks the name again right before sending so a reused pid never gets hit.
        """
        enforce_kill_name(name)
        if self.probe.comm(pid) != name[:15]:
            self.audit(action, "skipped: pid changed", pid=pid, name=name)
            return False
        if self.dry_run:
            self.audit(action, "would signal", pid=pid, name=name, signal=signame)
            return True
        if self._kill is None:
            raise Refused("no kill() on this platform")
        try:
            self._kill(pid, _signum(signame))
        except OSError as e:
            self.audit(action, "signal failed", pid=pid, name=name, signal=signame, error=str(e))
            return False
        self.signalled.append((pid, name, signame))
        self.audit(action, "signalled", pid=pid, name=name, signal=signame)
        return True

    # -- ES's own /emukill ---------------------------------------------------
    def swissknife_present(self):
        return bool(self._which(SWISSKNIFE)) or os.path.exists("/usr/bin/" + SWISSKNIFE)

    def emukill(self, confirmed=False, url=EMUKILL_URL):
        """GETs ES's /emukill, the one mutating ES route cleanstate may call, and only from here: `url`
        has to be EMUKILL_URL exactly, you have to have confirmed, and a dry run only logs. Returns
        (sent, note).
        """
        if url != EMUKILL_URL:
            raise Refused("emukill() only calls %s, not %r" % (EMUKILL_URL, url))
        if not confirmed:
            raise Refused("emukill() needs the owner's confirmation")
        if self.dry_run:
            self.audit(KILL_EMULATOR, "would GET", url=url)
            return True, "dry run"
        try:
            with self._opener.open(urllib.request.Request(url), timeout=2.0) as r:
                code = getattr(r, "status", 200)
            self.audit(KILL_EMULATOR, "GET /emukill", status=code)
            return code == 200, "HTTP %s" % code
        except (OSError, urllib.error.URLError) as e:
            self.audit(KILL_EMULATOR, "GET /emukill failed", error=str(e))
            return False, str(getattr(e, "reason", e))

    # -- discovery -----------------------------------------------------------
    def _registry_obj(self):
        if self._registry is None:
            import web_tiles
            self._registry = web_tiles.ChildRegistry(_default_registry())
        return self._registry

    def live_children(self):
        """[(kind, pid)] saved by web_tiles whose cmdline still has the marker (the same test
        reap_stale() uses before signalling).
        """
        reg = self._registry_obj()
        out = []
        for kind, e in sorted(reg.entries().items()):
            try:
                pid, marker = int(e["pid"]), str(e["marker"])
            except (KeyError, TypeError, ValueError):
                continue
            cmd = reg.read_cmdline(pid)
            if cmd and marker in cmd:
                out.append((kind, pid))
        return out

    def touch_state(self):
        rc, out = self.run_command(CMD_GET_INPUTS)
        if rc != 0:
            return None
        try:
            for dev in json.loads(out):
                if dev.get("identifier") == TOUCH_INT:
                    return (dev.get("libinput") or {}).get("send_events")
        except (ValueError, AttributeError, TypeError):
            return None
        return None

    def runemu_platform(self, pid):
        for a in self.probe.cmdline(pid) or []:
            if a.startswith("-P"):
                return a[2:]
        return None

    def game_facts(self):
        """What's running, from ES (read-only /runningGame), runemu.sh and the kill file. Never
        signals anything.
        """
        g = {"es_game": None, "system": None, "runemu": self.probe.runemu_pids(),
             "platform": None, "kill_data": None, "signal": None, "names": [],
             "alive": {}, "refused": {}}
        try:
            st, _, body = self.probe.http("/runningGame")
            if st == "ok" and isinstance(body, dict) and body.get("name"):
                g["es_game"], g["system"] = body.get("name"), body.get("systemName")
        except es_health.HttpRefused:
            pass
        for p in g["runemu"]:
            g["platform"] = g["platform"] or self.runemu_platform(p)
        raw = self.probe.kill_data()
        g["kill_data"] = raw.strip() if raw else None
        flag, names = es_health.parse_kill_data(raw or "")
        g["signal"], g["names"] = flag, names
        for n in names:
            try:
                enforce_kill_name(n)
                signal_for(flag)
            except Refused as e:
                g["refused"][n] = str(e)
                continue
            pids = self.probe.pids_named(n)
            if pids:
                g["alive"][n] = pids
        return g

    def steam_processes(self):
        """What of a Steam session is running: {"client": [pid], "reapers": [(pid, appid)], "gamescope":
        [pid]}. Empty lists when Steam is not open, which is also when the stop items are not offered."""
        out = {"client": [], "reapers": [], "gamescope": []}
        for pid in self.probe.pids():
            try:
                k = steam_kind(self.probe.comm(pid), self.probe.cmdline(pid))
            except Exception:       # noqa: BLE001 - a process that vanished mid-scan
                continue
            if k is None:
                continue
            kind, appid = k
            if kind == STEAM_CLIENT:
                out["client"].append(pid)
            elif kind == STEAM_REAPER:
                out["reapers"].append((pid, appid))
            else:
                out["gamescope"].append(pid)
        return out

    def steam_game_name(self, appid):
        try:
            if self._steam_name is not None:
                return self._steam_name(appid)
            import steam_library
            return steam_library.info(steam_library.default_root(), appid).get("name")
        except Exception:           # noqa: BLE001 - a name is only for the stop list
            return None

    def _send_steam_signal(self, pid, kind, action):
        """SIGTERM to one Steam program found by steam_processes(). Looks at the process again right
        before sending, so a pid that has been reused for something else is never signalled."""
        try:
            now = steam_kind(self.probe.comm(pid), self.probe.cmdline(pid))
        except Exception:           # noqa: BLE001
            now = None
        if now is None or now[0] != kind:
            self.audit(action, "skipped: pid changed", pid=pid, kind=kind)
            return False
        if self.dry_run:
            self.audit(action, "would signal", pid=pid, kind=kind, signal="SIGTERM")
            return True
        if self._kill is None:
            raise Refused("no kill() on this platform")
        try:
            self._kill(pid, _signum("SIGTERM"))
        except OSError as e:
            self.audit(action, "signal failed", pid=pid, kind=kind, error=str(e))
            return False
        self.signalled.append((pid, kind, "SIGTERM"))
        self.audit(action, "signalled", pid=pid, kind=kind, signal="SIGTERM")
        return True

    def port_processes(self, runemu_pids):
        """[(pid, comm)] in runemu.sh's subtree that are a port's own programs, not shells and not
        protected. runemu.sh's own background child (lowerdeck's `python3 ra_proxy.py`) is python3 so
        it's protected, and so is anything of rp5deck's. _send_port_signal() checks again.
        """
        children = {}
        for p in self.probe.pids():
            pp = self.probe.ppid(p)
            if pp is not None:
                children.setdefault(pp, []).append(p)
        out, todo, seen = [], list(runemu_pids), set()
        while todo:
            p = todo.pop()
            for c in children.get(p, []):
                if c in seen:
                    continue
                seen.add(c)
                todo.append(c)
                comm = self.probe.comm(c) or ""
                if comm in _SHELLS or comm[:15] in _PROTECTED_COMM:
                    continue
                out.append((c, comm))
        return sorted(out)

    def discover(self, check_health=True, events_seen=None, progress=None):
        plan = Plan()
        g = self.game_facts()
        plan.game = g
        sp = self.steam_processes()
        plan.steam = sp
        steam_open = bool(sp["client"] or sp["reapers"] or sp["gamescope"])
        # With Steam open its own items below stop it. ES's /emukill and ROCKNIX's kill target can not
        # (the targets are on the protected list), so a "Game: Steam" item would only fail.
        running = bool(g["runemu"] or g["alive"] or g["es_game"]) and not (steam_open and g["system"] == "steam")
        if sp["reapers"]:
            pid, appid = sp["reapers"][0]
            name = self.steam_game_name(appid) or "appid %d" % appid
            plan.items.append(Item(STOP_STEAM_GAME, "Steam game: %s" % name,
                                   "stop the game, Steam stays open"))
        elif steam_open:
            plan.items.append(Item(EXIT_STEAM, "Steam", "close Steam and go back to ES"))
        if running:
            what = g["es_game"] and "%s (%s)" % (g["es_game"], g["system"] or "?") \
                or (", ".join(sorted(g["alive"])) or "a game")
            how = ["ES's /emukill first"]
            if g["alive"]:
                how.append("then ROCKNIX's kill target %s (%s)" % (
                    " ".join(sorted(g["alive"])), signal_for(g["signal"])))
            if g["platform"] == "ports" or (g["runemu"] and not g["names"]):
                ports = self.port_processes(g["runemu"])
                g["port_processes"] = ports
                if ports:
                    how.append("then the port's own programs: %s" % ", ".join(
                        "%s (%d)" % (c, p) for p, c in ports))
            for n, why in sorted(g["refused"].items()):
                if n != "emulationstation":
                    plan.notes.append("not stopped: %s" % why)
            plan.items.append(Item(KILL_EMULATOR, "Game: %s" % what, ", ".join(how)))
        labels = {"web": "Browser / Discord", "yt": "YouTube player", "osk": "On-screen keyboard"}
        try:
            plan.children = self.live_children()
        except Exception:           # noqa: BLE001 - reported, not fatal
            log.exception("reading the child registry")
            plan.children = []
        for kind, pid in plan.children:
            plan.items.append(Item(STOP_CHILDREN, labels.get(kind, kind), "running"))
        plan.touch = self.touch_state()
        if plan.touch == "disabled":
            plan.items.append(Item(ENABLE_TOUCH, "Built-in touch screen",
                                   "turn it back on (it is off)"))
        if check_health:
            plan.health = self._health_fn(probe=self.probe, es_output=self.es_output,
                                          events_seen=events_seen, progress=progress)
        self.audit("plan", "discovered", items=plan.stop_list(), notes=plan.notes,
                   health=plan.health.verdict if plan.health else None)
        return plan

    # -- execution -----------------------------------------------------------
    def execute(self, plan, actions, confirmed=False, force_restart=False):
        """Runs the chosen actions in ACTIONS order. Needs confirmed=True unless it's a dry run. Returns
        [Step].
        """
        for a in actions:
            enforce_action(a)
        if not confirmed and not self.dry_run:
            raise Refused("cleanstate.execute() needs the owner's confirmation")
        steps = []
        for a in ACTIONS:
            if a not in actions:
                continue
            try:
                if a == KILL_EMULATOR:
                    steps.append(self._kill_emulator(plan, confirmed))
                elif a == STOP_STEAM_GAME:
                    steps.append(self._stop_steam_game())
                elif a == EXIT_STEAM:
                    steps.append(self._exit_steam())
                elif a == STOP_CHILDREN:
                    steps.append(self._stop_children())
                elif a == ENABLE_TOUCH:
                    steps.append(self._enable_touch())
                elif a == RESTART_ES:
                    steps.append(self._restart_es(plan, force_restart))
            except Refused as e:
                self.audit(a, "refused", reason=str(e))
                steps.append(Step(a, False, "Refused: %s" % e))
            except Exception as e:      # noqa: BLE001 - one action failing never stops the rest
                log.exception("cleanstate %s", a)
                self.audit(a, "failed", error=repr(e))
                steps.append(Step(a, False, "Failed: %s" % e))
        self.audit("execute", "done", steps=[s.to_dict() for s in steps])
        return steps

    def _game_gone(self):
        if self.probe.runemu_pids():
            return False
        flag, names = es_health.parse_kill_data(self.probe.kill_data() or "")
        for n in names:
            if n in KILLABLE_NAMES and self.probe.pids_named(n):
                return False
        return True

    def _wait_gone(self, seconds):
        end = self.clock() + seconds
        while True:
            if self._game_gone():
                return True
            if self.clock() >= end:
                return False
            self.sleep(0.25)

    def _kill_emulator(self, plan, confirmed):
        a = KILL_EMULATOR
        if self._game_gone() and not (plan.game or {}).get("es_game"):
            self.audit(a, "nothing running")
            return Step(a, True, "No game was running")
        how = []
        sk = self.swissknife_present()
        sent, note = self.emukill(confirmed=confirmed or self.dry_run)
        wait = self.emukill_wait_s if sk else min(1.0, self.emukill_wait_s)
        if not sk:
            how.append("ES's /emukill (%s; %s is not installed, so ES has nothing to run)"
                       % (note, SWISSKNIFE))
        else:
            how.append("ES's /emukill (%s)" % note)
        if not self.dry_run and self._wait_gone(wait):
            return Step(a, True, "Game closed by ES's /emukill", {"how": how})
        # ROCKNIX's kill target, read again now since runemu may have changed it
        flag, names = es_health.parse_kill_data(self.probe.kill_data() or "")
        try:
            signame = signal_for(flag)
        except Refused as e:
            signame = None
            how.append(str(e))
        hit = []
        for n in names:
            try:
                enforce_kill_name(n)
            except Refused as e:
                if n != "emulationstation":
                    how.append(str(e))
                continue
            if signame is None:
                continue
            for pid in self.probe.pids_named(n):
                if self._send_signal(pid, n, signame, a):
                    hit.append("%s %d" % (n, pid))
        if hit:
            how.append("ROCKNIX's kill target: %s (%s)" % (", ".join(hit), signame))
            if self.dry_run or self._wait_gone(self.verify_s):
                return Step(a, True, "Game closed (%s)" % signame, {"how": how})
        # a port, its own programs in runemu.sh's subtree
        runemu = self.probe.runemu_pids()
        platform = next((self.runemu_platform(p) for p in runemu if self.runemu_platform(p)),
                        None)
        if runemu and (platform == "ports" or not names):
            port_hit = []
            for pid, comm in self.port_processes(runemu):
                if comm[:15] in _KILLABLE_COMM or platform == "ports":
                    try:
                        if self._send_port_signal(pid, comm):
                            port_hit.append("%s %d" % (comm, pid))
                    except Refused as e:
                        how.append(str(e))
            if port_hit:
                how.append("the port's programs: %s (SIGTERM)" % ", ".join(port_hit))
                if self.dry_run or self._wait_gone(self.verify_s):
                    return Step(a, True, "Port closed", {"how": how})
        if self.dry_run:
            return Step(a, True, "Dry run: %s" % "; ".join(how), {"how": how})
        return Step(a, False, "The game is still running (try L1+SELECT+START)", {"how": how})

    def _steam_wait(self, done):
        """Polls done() until it is true or steam_wait_s has passed."""
        end = self.clock() + self.steam_wait_s
        while True:
            if done():
                return True
            if self.clock() >= end:
                return False
            self.sleep(0.25)

    def _stop_steam_game(self):
        """SIGTERM to the running game's reaper(s), Steam itself stays open."""
        a = STOP_STEAM_GAME
        reapers = self.steam_processes()["reapers"]
        if not reapers:
            self.audit(a, "nothing running")
            return Step(a, True, "No Steam game was running")
        for pid, appid in reapers:
            self._send_steam_signal(pid, STEAM_REAPER, a)
        if self.dry_run:
            return Step(a, True, "Dry run: would stop %s" % ", ".join("appid %d" % ap for _, ap in reapers))
        if self._steam_wait(lambda: not self.steam_processes()["reapers"]):
            return Step(a, True, "Steam game closed", {"appids": [ap for _, ap in reapers]})
        return Step(a, False, "The Steam game is still running")

    def _exit_steam(self):
        """SIGTERM to the Steam client, which closes its games and its gamescope; gamescope gets the same
        signal if it outlives the client."""
        a = EXIT_STEAM
        sp = self.steam_processes()
        if not (sp["client"] or sp["gamescope"] or sp["reapers"]):
            self.audit(a, "nothing running")
            return Step(a, True, "Steam was not running")
        for pid in sp["client"]:
            self._send_steam_signal(pid, STEAM_CLIENT, a)
        if self.dry_run:
            for pid in sp["gamescope"]:
                self._send_steam_signal(pid, STEAM_GAMESCOPE, a)
            return Step(a, True, "Dry run: would close Steam")

        def gone():
            s = self.steam_processes()
            return not (s["client"] or s["gamescope"])
        if self._steam_wait(gone):
            return Step(a, True, "Steam closed")
        for pid in self.steam_processes()["gamescope"]:
            self._send_steam_signal(pid, STEAM_GAMESCOPE, a)
        if self._steam_wait(gone):
            return Step(a, True, "Steam closed (gamescope needed a second signal)")
        return Step(a, False, "Steam is still running")

    def _send_port_signal(self, pid, comm):
        """A port's program isnt on KILLABLE_NAMES (every port ships its own binary), so it gets its
        own narrower gate: found in runemu.sh's subtree by port_processes(), not protected, not a
        shell, SIGTERM only.
        """
        if not _NAME_RE.match(comm or "") or (comm or "")[:15] in _PROTECTED_COMM or comm in _SHELLS:
            raise Refused("port process %r is protected" % (comm,))
        if self.probe.comm(pid) != comm:
            self.audit(KILL_EMULATOR, "skipped: pid changed", pid=pid, name=comm)
            return False
        if self.dry_run:
            self.audit(KILL_EMULATOR, "would signal port process", pid=pid, name=comm,
                       signal="SIGTERM")
            return True
        try:
            self._kill(pid, _signum("SIGTERM"))
        except OSError as e:
            self.audit(KILL_EMULATOR, "signal failed", pid=pid, name=comm, error=str(e))
            return False
        self.signalled.append((pid, comm, "SIGTERM"))
        self.audit(KILL_EMULATOR, "signalled port process", pid=pid, name=comm,
                   signal="SIGTERM")
        return True

    def _stop_children(self):
        a = STOP_CHILDREN
        live = self.live_children()
        if not live:
            self.audit(a, "nothing running")
            return Step(a, True, "No Browser / YouTube / keyboard was running")
        if self.dry_run:
            self.audit(a, "would stop", children=live)
            return Step(a, True, "Dry run: would stop %s" % ", ".join(
                "%s %d" % kp for kp in live))
        done = self._registry_obj().reap_stale()
        self.audit(a, "stopped", children=done)
        end = self.clock() + self.verify_s
        while self._still_alive(live) and self.clock() < end:
            self.sleep(0.25)
        left = self._still_alive(live)
        if left:
            return Step(a, False, "Still running: %s" % ", ".join("%s %d" % kp for kp in left))
        return Step(a, True, "Stopped %s" % ", ".join(k for k, _ in done or live))

    def _still_alive(self, live):
        reg = self._registry_obj()
        markers = getattr(reg, "MARKERS", {})
        out = []
        for kind, pid in live:
            cmd = reg.read_cmdline(pid)
            if cmd and markers.get(kind, kind) in cmd:
                out.append((kind, pid))
        return out

    def _enable_touch(self):
        a = ENABLE_TOUCH
        st = self.touch_state()
        if st != "disabled":
            self.audit(a, "no change needed", state=st)
            return Step(a, True, "Touch was already on" if st == "enabled"
                        else "Touch state unknown (%s): left alone" % st)
        rc, out = self.run_command(CMD_ENABLE_TOUCH)
        if self.dry_run:
            return Step(a, True, "Dry run: would turn touch back on")
        after = self.touch_state()
        ok = rc == 0 and after == "enabled"
        return Step(a, ok, "Touch turned back on" if ok else
                    "Touch still %s (swaymsg rc %s)" % (after, rc))

    def own_cgroup(self):
        if self._cgroup is not None:
            return self._cgroup
        try:
            with open("/proc/self/cgroup") as f:
                return f.read()
        except OSError:
            return ""

    def _restart_es(self, plan, force):
        a = RESTART_ES
        h = plan.health
        if h is None:
            raise Refused("no ES health check was made")
        if not (h.offer_restart or (force and h.allow_force)):
            raise Refused("ES is %s (%s): restart not offered" % (h.verdict, h.summary))
        if "essway" in self.own_cgroup():
            raise Refused("this process runs inside essway.service: the restart would kill it")
        old = set(self.probe.es_pids())
        self.audit(a, "restarting", verdict=h.verdict, summary=h.summary, old_pids=sorted(old),
                   forced=bool(force and not h.offer_restart))
        rc, out = self.run_command(CMD_RESTART_ES, timeout=60.0)
        if self.dry_run:
            return Step(a, True, "Dry run: would run %s" % " ".join(CMD_RESTART_ES))
        if rc != 0:
            return Step(a, False, "systemctl failed (rc %s): %s" % (rc, out.strip()[-120:]))
        end = self.clock() + 30.0
        new = []
        while self.clock() < end:
            new = [p for p in self.probe.es_pids() if p not in old]
            if new:
                break
            self.sleep(0.5)
        if not new:
            return Step(a, False, "essway restarted, but no new ES process within 30 s")
        return Step(a, True, "EmulationStation restarted")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_plan(plan):
    print("Would stop:" if plan.items else "Nothing to stop.")
    for line in plan.stop_list():
        print("  -", line)
    for n in plan.notes:
        print("  !", n)
    print(NEVER_TOUCHED)
    if plan.health:
        h = plan.health
        print("ES: %s - %s" % (h.verdict.upper(), h.summary))
        for n in h.notes:
            print("  -", n)
        print("  restart offered: %s, restart-anyway: %s" % (h.offer_restart, h.allow_force))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="rp5deck clean state helper (CC1)")
    ap.add_argument("cmd", nargs="?", choices=("plan", "run"), default="plan")
    ap.add_argument("actions", nargs="*", help="with run: %s" % ", ".join(ACTIONS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--i-confirm", action="store_true",
                    help="required to act for real (the CLI has no confirm dialog)")
    ap.add_argument("--force-restart", action="store_true",
                    help="restart ES on a SUSPECT verdict (the dialog's 'Restart anyway')")
    ap.add_argument("--no-health", action="store_true")
    ap.add_argument("--es-output", default=screen_map.CURRENT.top)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    bad = [x for x in a.actions if x not in ACTIONS]
    if bad or (a.cmd == "run" and not a.actions) or (a.cmd == "plan" and a.actions):
        print("usage: cleanstate.py [plan] | run ACTION... (%s)" % ", ".join(ACTIONS),
              file=sys.stderr)
        return 2
    dry = a.cmd != "run" or a.dry_run
    if a.cmd == "run" and not a.dry_run and not a.i_confirm:
        print("refusing: add --dry-run, or --i-confirm to act", file=sys.stderr)
        return 2
    h = Helper(dry_run=dry, es_output=a.es_output)
    plan = h.discover(check_health=not a.no_health,
                      progress=lambda t: print("...", t, file=sys.stderr))
    if a.cmd != "run":
        print(json.dumps(plan.to_dict(), indent=1, default=str)) if a.json else _print_plan(plan)
        return 0
    steps = h.execute(plan, a.actions, confirmed=a.i_confirm, force_restart=a.force_restart)
    if a.json:
        print(json.dumps({"plan": plan.to_dict(), "steps": [s.to_dict() for s in steps]},
                         indent=1, default=str))
    else:
        _print_plan(plan)
        for s in steps:
            print("%s %s: %s" % ("OK  " if s.ok else "FAIL", s.action, s.text))
    return 0 if all(s.ok for s in steps) else 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
