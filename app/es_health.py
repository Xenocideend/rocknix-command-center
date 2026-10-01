#!/usr/bin/env python3
"""es_health: is EmulationStation running, and is it frozen?

Read only. Nothing here signals a process, runs a command that changes anything or sends ES
a mutating request. It looks, and judge() turns what it saw into a verdict the Command
Center can show. The one thing that acts on it (`systemctl restart essway.service`, behind a
confirm) is in cleanstate.py.

Running means the ES process exists and its window is in the sway tree (when no game runs,
since ES closes its window during a game and maps a new one after). Frozen needs several
signals together, because ES's HTTP API runs on its own thread and can answer while the UI
is stuck.

The evidence (from emulationstation-next at the commit ROCKNIX pins):

Main thread progress. ES's UI runs on the main thread (tid == pid, the HTTP server is another
thread). es-app/src/main.cpp's loop is
    ps_standby ? SDL_WaitEventTimeout(&ev, PowerSaver::getTimeout())
               : SDL_PollEvent(&ev)  ...  window.render(); swapBuffers();
and PowerSaver getTimeout() is 40 ms in the "default" PowerSaverMode, so a live ES blocks and
wakes at least ~25 times a second, and each one is a voluntary context switch of the main
thread (/proc/<pid>/task/<pid>/status). A deadlocked main thread makes none and neither does
one spinning in a loop, so 0 voluntary switches in the whole window is the stall signal. It
doesnt count with PowerSaverMode "instant"/"enhanced" (the timeout is then the screensaver's,
minutes of normal blocking), when ES's screen is off (the swap can block as long as the
output is off), or while a game runs (ES sits in waitpid).

The HTTP API: GET /caps within 2 s, read only, two tries at the start and end of the window,
and both have to fail. On its own it says nothing about the UI.

Process state from /proc/<pid>/stat: Z is dead, and D (stuck in the kernel) or T (stopped by a
signal) in every sample of the window. ROCKNIX's rocknix-fake-suspend SIGSTOPs the kill file's
target, which is "emulationstation" when no game runs, but it also blanks the screens, so the
screen on precondition rules it out.

The window: app_id "emulationstation" in GET_TREE on the ES output.

Vetoes: an ES event hook firing during the window (ES fires them from the UI thread, so the
UI was alive), or any main thread progress.

Looked at and not usable: sway's GET_TREE has no per-surface commit or frame counter,
`inhibit_idle` is SDL's idle inhibitor (true whether ES is live or frozen), and the title
never changes. ES has no heartbeat event either, Scripting::fireEvent only fires on state
changes.

Verdicts (judge()):
  OK           everything answered.
  GAME         a game is running so health isnt judged (ES is meant to be windowless and
               blocked), kill-emulator is the tool for that.
  STARTING     the ES process is younger than STARTUP_GRACE_S.
  NOT_RUNNING  no ES process for the whole (extended) window, or a zombie.
  NO_WINDOW    process alive, no game, but no ES window for NO_WINDOW_S or more.
  FROZEN       2 or more of: HTTP down, main thread stalled, stuck or stopped state, window
               missing, with the preconditions met (process alive, past the grace period,
               no game, ES's screen on, sway readable).
  SUSPECT      exactly one of those, shown with its evidence, and restart is only offered as
               "Restart anyway" (you can see the top screen, so you're the second signal).
  UNKNOWN      preconditions not met (screen off, sway unreadable, ...).
Restart is offered for NOT_RUNNING, NO_WINDOW and FROZEN.
"""
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import screen_map

log = logging.getLogger("rp5deck.es_health")

ES_COMM = "emulationstation"
ES_APP_ID = "emulationstation"
OWN_APP_PREFIX = "rp5deck-"
HTTP_BASE = "http://127.0.0.1:1234"
HTTP_TIMEOUT = 2.0
# The only paths this ever requests, both read-only GETs (es_api.ES_HTTP_ENDPOINTS has the full
# route table, mutating ones included).
ALLOWED_HTTP_PATHS = frozenset({"/caps", "/runningGame"})

WINDOW_S = 5.0              # observation window for one check
SAMPLES = 6                 # samples across it (first at t=0, last at WINDOW_S)
STARTUP_GRACE_S = 60.0  # ES loads gamelists and themes after start, never judged frozen during it
NO_WINDOW_S = 30.0  # window missing for 30 s
ABSENT_S = 10.0  # essway has Restart=always RestartSec=2, wait out that gap

KILL_DATA = "/tmp/.process-kill-data"
ES_SETTINGS = "/storage/.config/emulationstation/es_settings.cfg"

OK, GAME, STARTING, NOT_RUNNING, NO_WINDOW, FROZEN, SUSPECT, UNKNOWN = (
    "ok", "game", "starting", "not_running", "no_window", "frozen", "suspect", "unknown")
RESTART_VERDICTS = frozenset({NOT_RUNNING, NO_WINDOW, FROZEN})

# signal names (Health.signals)
SIG_HTTP, SIG_STALL, SIG_STATE, SIG_WINDOW = "http", "stall", "state", "window"


class HttpRefused(Exception):
    """A path outside ALLOWED_HTTP_PATHS was requested."""


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------
def parse_stat(text):
    """/proc/<pid>/stat -> {state, utime, stime, starttime} (clock ticks). comm can have spaces and
    ')' in it, so split at the last ')'.
    """
    try:
        rest = text[text.rindex(")") + 2:].split()
        return {"state": rest[0], "utime": int(rest[11]), "stime": int(rest[12]),
                "starttime": int(rest[19])}
    except (ValueError, IndexError):
        return None


def parse_ctxt(text):
    """/proc/<pid>/task/<tid>/status -> (voluntary, nonvoluntary) or None."""
    vol = invol = None
    for line in text.splitlines():
        if line.startswith("voluntary_ctxt_switches:"):
            vol = int(line.split(":", 1)[1])
        elif line.startswith("nonvoluntary_ctxt_switches:"):
            invol = int(line.split(":", 1)[1])
    if vol is None or invol is None:
        return None
    return vol, invol


_PS_RE = re.compile(r'<string\s+name="PowerSaverMode"\s+value="([^"]*)"')


def parse_powersaver(text):
    """PowerSaverMode from es_settings.cfg, or None when the file doesnt set it (ES's default is
    "default" then).
    """
    m = _PS_RE.search(text or "")
    return m.group(1) if m else None


def parse_kill_data(text):
    """ROCKNIX set_kill file -> (signal token or None, [process names]). input_sense runs
    `killall ${TO_KILL}` word split, so the file has an optional killall signal flag and one or
    more names: "retroarch retroarch32", "-9 melonDS", "-HUP gmu.bin", "emulationstation".
    """
    toks = (text or "").split()
    sig = None
    if toks and toks[0].startswith("-"):
        sig, toks = toks[0], toks[1:]
    return sig, [t for t in toks if not t.startswith("-")]


def _walk(node, output=None):
    """(node, output name) for every node."""
    if node.get("type") == "output":
        output = node.get("name")
    yield node, output
    for key in ("nodes", "floating_nodes"):
        for c in node.get(key) or []:
            yield from _walk(c, output)


def _is_view(n):
    if n.get("type") not in ("con", "floating_con"):
        return False
    if n.get("nodes") or n.get("floating_nodes"):
        return False
    return (n.get("pid") is not None or n.get("app_id") is not None
            or n.get("window") is not None or n.get("shell") is not None)


def _label(v):
    props = v.get("window_properties") or {}
    return v.get("app_id") or props.get("class") or v.get("name") or "con#%s" % v.get("id")


def tree_facts(tree, es_output=screen_map.CURRENT.top):
    """What a check needs from one GET_TREE reply: es_window (bool), es_visible, es_focused, es_pid,
    es_output (where ES is, else the configured one), output_on (that output active, powered,
    DPMS on, None if missing), foreign (labels of other real windows on it).
    """
    f = {"es_window": False, "es_visible": None, "es_focused": False, "es_pid": None,
         "es_output": es_output, "output_on": None, "foreign": []}
    outputs = {}
    views = []
    for n, out in _walk(tree):
        if n.get("type") == "output":
            outputs[n.get("name")] = n
        elif _is_view(n):
            views.append((n, out))
    for v, out in views:
        if str(v.get("app_id") or "") == ES_APP_ID:
            f.update(es_window=True, es_visible=v.get("visible"), es_focused=bool(v.get("focused")),
                     es_pid=v.get("pid"), es_output=out)
            break
    o = outputs.get(f["es_output"])
    if o is not None:
        f["output_on"] = bool(o.get("active", True)) and o.get("power", True) is not False \
            and o.get("dpms", True) is not False
    for v, out in views:
        aid = str(v.get("app_id") or "")
        if out == f["es_output"] and aid != ES_APP_ID and not aid.startswith(OWN_APP_PREFIX):
            f["foreign"].append(_label(v))
    return f


# ---------------------------------------------------------------------------
# The judgement (pure)
# ---------------------------------------------------------------------------
class Health:
    def __init__(self, verdict, summary, signals=(), notes=(), evidence=None):
        self.verdict = verdict
        self.summary = summary              # one line for the dialog
        self.signals = list(signals)        # SIG_* that fired
        self.notes = list(notes)            # the evidence, one line each
        self.evidence = evidence or {}

    @property
    def offer_restart(self):
        return self.verdict in RESTART_VERDICTS

    @property
    def allow_force(self):
        """SUSPECT: restart only as an explicit "Restart anyway"."""
        return self.verdict == SUSPECT

    def to_dict(self):
        return {"verdict": self.verdict, "summary": self.summary, "signals": self.signals,
                "notes": self.notes, "offer_restart": self.offer_restart,
                "allow_force": self.allow_force, "evidence": self.evidence}

    def __repr__(self):
        return "Health(%s, %r, %s)" % (self.verdict, self.summary, self.signals)


def _fmt_s(v):
    return "%d s" % round(v) if v >= 1.5 else "%.1f s" % v


def judge(ev):
    """Turns a check's evidence into a Health. `ev` keys are all optional, and a missing or None
    value means unknown, never fine or broken: pid, zombie, age_s, states [per sample],
    vol_delta, invol_delta, cpu_delta_s, window_s, http_fail (both tries failed), http_note,
    windows [per sample bool], window_missing_s, visible, output_on, sway_ok, game (bool),
    game_why, powersaver, events_during (int), absent_s, essway.
    """
    pid = ev.get("pid")
    notes = []
    if pid is None:
        absent = ev.get("absent_s") or 0.0
        st = ev.get("essway")
        return Health(NOT_RUNNING,
                      "ES isn't running (no process for %s%s)" % (
                          _fmt_s(absent), ", essway.service is %s" % st if st else ""),
                      notes=["no emulationstation process in any sample over %s" % _fmt_s(absent)]
                      + (["systemctl is-active essway.service: %s" % st] if st else []),
                      evidence=ev)
    if ev.get("zombie"):
        return Health(NOT_RUNNING, "ES has exited (zombie process %s)" % pid,
                      notes=["/proc/%s/stat state Z" % pid], evidence=ev)
    if ev.get("game"):
        return Health(GAME, "A game is running (%s): ES is not checked while it waits"
                      % (ev.get("game_why") or "detected"), evidence=ev)
    age = ev.get("age_s")
    if age is not None and age < STARTUP_GRACE_S:
        return Health(STARTING, "ES started %s ago; give it a moment" % _fmt_s(age),
                      evidence=ev)
    if not ev.get("sway_ok"):
        return Health(UNKNOWN, "Can't tell: sway's window tree could not be read", evidence=ev)
    if ev.get("output_on") is False:
        return Health(UNKNOWN, "Can't tell: ES's screen is off", evidence=ev)
    if ev.get("game") is None:
        return Health(UNKNOWN, "Can't tell whether a game is running", evidence=ev)

    window_s = ev.get("window_s") or 0.0
    signals = []
    # 1. the HTTP API
    if ev.get("http_fail"):
        signals.append(SIG_HTTP)
        notes.append("HTTP API: %s" % (ev.get("http_note") or "no answer"))
    else:
        notes.append("HTTP API: ok%s" % (" (%s)" % ev["http_note"] if ev.get("http_note") else ""))
    # 2. main-thread progress
    ps = (ev.get("powersaver") or "default").lower()
    vol = ev.get("vol_delta")
    if vol is None:
        notes.append("main thread: not measured")
    elif vol > 0:
        notes.append("main thread: %d wake-ups in %s (alive)" % (vol, _fmt_s(window_s)))
    elif ps in ("instant", "enhanced"):
        notes.append("main thread: no wake-ups in %s, but PowerSaverMode=%s allows that"
                     % (_fmt_s(window_s), ps))
    elif ev.get("visible") is False:
        notes.append("main thread: no wake-ups, but ES's window is not visible (no frames)")
    else:
        signals.append(SIG_STALL)
        cpu = ev.get("cpu_delta_s")
        how = ""
        if cpu is not None:
            how = " (spinning: %.1f s CPU)" % cpu if cpu > 0.5 * window_s else " (blocked)"
        notes.append("main thread: no progress for %s%s" % (_fmt_s(window_s), how))
    # 3. process state
    states = [s for s in (ev.get("states") or []) if s]
    if states and all(s in ("D", "T", "t") for s in states):
        signals.append(SIG_STATE)
        what = {"D": "stuck in the kernel (D)", "T": "stopped by a signal (T)",
                "t": "stopped by a tracer (t)"}.get(states[-1], states[-1])
        notes.append("process %s: %s in all %d samples" % (pid, what, len(states)))
    elif states:
        notes.append("process %s: state %s" % (pid, "/".join(sorted(set(states)))))
    # 4. the window
    wins = ev.get("windows") or []
    missing_s = ev.get("window_missing_s") or 0.0
    if wins and not any(wins):
        signals.append(SIG_WINDOW)
        notes.append("window: missing for %s" % _fmt_s(missing_s))
    elif wins:
        notes.append("window: present")
    # vetoes: evidence the UI thread was alive during the window
    if ev.get("events_during"):
        notes.append("ES fired %d event hook(s) during the check: its UI was alive"
                     % ev["events_during"])
        signals = [s for s in signals if s == SIG_WINDOW]
    if vol:     # any progress at all clears a stuck/stopped reading too
        signals = [s for s in signals if s not in (SIG_STATE, SIG_STALL)]

    if len(signals) >= 2:
        return Health(FROZEN, "ES isn't responding (%s)" % _summary(signals, ev, window_s),
                      signals, notes, ev)
    if signals == [SIG_WINDOW] and missing_s >= NO_WINDOW_S:
        return Health(NO_WINDOW, "ES is running but has no window (HTTP %s, window missing "
                      "for %s)" % ("down" if ev.get("http_fail") else "ok", _fmt_s(missing_s)),
                      signals, notes, ev)
    if len(signals) == 1:
        return Health(SUSPECT, "ES might be stuck (%s)" % _summary(signals, ev, window_s),
                      signals, notes, ev)
    return Health(OK, "ES is running and responding", signals, notes, ev)


def _summary(signals, ev, window_s):
    parts = []
    if SIG_HTTP in signals:
        parts.append("HTTP %s" % (ev.get("http_note") or "no answer"))
    else:
        parts.append("HTTP ok")
    if SIG_STALL in signals:
        parts.append("UI thread idle for %s" % _fmt_s(window_s))
    if SIG_STATE in signals:
        st = (ev.get("states") or ["?"])[-1]
        parts.append({"T": "process stopped", "D": "process stuck in the kernel"}.get(
            st, "process state %s" % st))
    if SIG_WINDOW in signals:
        parts.append("window missing for %s" % _fmt_s(ev.get("window_missing_s") or window_s))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# The probe (all I/O, injectable)
# ---------------------------------------------------------------------------
def _read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


class Probe:
    """Everything a check reads. Tests replace methods or pass a fake."""

    def __init__(self, proc="/proc", http_base=HTTP_BASE, http_timeout=HTTP_TIMEOUT,
                 kill_data=KILL_DATA, es_settings=ES_SETTINGS, opener=None):
        self.proc = proc
        self.http_base = http_base
        self.http_timeout = http_timeout
        self.kill_data_path = kill_data
        self.es_settings = es_settings
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            self.clk_tck = os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError, OSError):
            self.clk_tck = 100

    # -- processes ---------------------------------------------------------
    def pids(self):
        try:
            return [int(e) for e in os.listdir(self.proc) if e.isdigit()]
        except OSError:
            return []

    def comm(self, pid):
        t = _read("%s/%d/comm" % (self.proc, pid))
        return t.strip() if t is not None else None

    def cmdline(self, pid):
        t = _read("%s/%d/cmdline" % (self.proc, pid), "rb")
        if t is None:
            return None
        return [a.decode("utf-8", "replace") for a in t.split(b"\0") if a]

    def ppid(self, pid):
        t = _read("%s/%d/stat" % (self.proc, pid))
        try:
            return int(t[t.rindex(")") + 2:].split()[1])
        except (TypeError, ValueError, IndexError):
            return None

    def es_pids(self):
        # /proc/<pid>/comm holds at most 15 bytes, so ES reads "emulationstatio" on the device. Match
        # the way killall does.
        return self.pids_named(ES_COMM)

    def stat(self, pid):
        t = _read("%s/%d/stat" % (self.proc, pid))
        return parse_stat(t) if t else None

    def ctxt(self, pid):
        t = _read("%s/%d/task/%d/status" % (self.proc, pid, pid))
        return parse_ctxt(t) if t else None

    def uptime(self):
        t = _read("%s/uptime" % self.proc)
        try:
            return float(t.split()[0])
        except (AttributeError, ValueError, IndexError):
            return None

    def runemu_pids(self):
        """ROCKNIX's game launcher, `bash /usr/bin/runemu.sh ...` (ES runs it through sh -c for every
        game, ports too, and waits for it).
        """
        out = []
        for p in self.pids():
            cl = self.cmdline(p) or []
            if any(os.path.basename(a) == "runemu.sh" for a in cl[:2]):
                out.append(p)
        return out

    def pids_named(self, name):
        """killall's match: comm is the first 15 bytes of the name, and a longer name also has to be
        argv[0]'s basename. Zombies dont count (a SIGTERMed child stays in /proc as Z until its
        parent reaps it).
        """
        out = []
        for p in self.pids():
            if self.comm(p) != name[:15]:
                continue
            st = self.stat(p)
            if st and st["state"] in ("Z", "X"):
                continue  # exited, not reaped yet, already gone
            if len(name) > 15:
                cl = self.cmdline(p) or []
                if not cl or os.path.basename(cl[0]) != name:
                    continue
            out.append(p)
        return out

    def kill_data(self):
        return _read(self.kill_data_path)

    def powersaver(self):
        return parse_powersaver(_read(self.es_settings))

    def essway_state(self):
        try:
            r = subprocess.run(["systemctl", "is-active", "essway.service"],
                               capture_output=True, text=True, timeout=3)
            return r.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    # -- sway (read-only client) ---------------------------------------------
    def sway_tree(self):
        try:
            import sway_ipc
            path = sway_ipc.find_socket()
            if not path:
                return None
            c = sway_ipc.Ipc(path, 2.0)
            try:
                return c.request(sway_ipc.GET_TREE)
            finally:
                c.close()
        except Exception:           # noqa: BLE001 - "unknown"
            return None

    # -- the HTTP API ---------------------------------------------------------
    def http(self, path):
        """GETs one allowed path. Returns (status, seconds, body), status is "ok", "timeout", "refused",
        "http <code>" or "error <text>".
        """
        if path not in ALLOWED_HTTP_PATHS:
            raise HttpRefused("es_health only requests %s, not %r"
                              % (sorted(ALLOWED_HTTP_PATHS), path))
        t0 = time.monotonic()
        try:
            with self._opener.open(urllib.request.Request(self.http_base + path),
                                   timeout=self.http_timeout) as r:
                body = r.read(65536)
                code = getattr(r, "status", 200)
            dt = time.monotonic() - t0
            if code != 200:
                return "http %d" % code, dt, None
            try:
                return "ok", dt, json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return "error bad JSON", dt, None
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            dt = time.monotonic() - t0
            reason = getattr(e, "reason", e)
            text = str(reason).lower()
            if isinstance(reason, TimeoutError) or "timed out" in text:
                return "timeout", dt, None
            if isinstance(reason, ConnectionRefusedError) or "refused" in text:
                return "refused", dt, None
            return "error %s" % reason, dt, None


# ---------------------------------------------------------------------------
# One check
# ---------------------------------------------------------------------------
def game_running(probe, es_game=None, tree_f=None):
    """(running: True/False, why). ES's own answer when it has one, plus ROCKNIX's state: runemu.sh
    alive, a live kill file target other than ES, or another window on ES's screen.
    """
    why = []
    if es_game:
        why.append("ES reports %s" % es_game)
    if probe.runemu_pids():
        why.append("runemu.sh is running")
    sig, names = parse_kill_data(probe.kill_data() or "")
    for n in names:
        if n != ES_COMM and probe.pids_named(n):
            why.append("%s is running" % n)
    if tree_f and tree_f.get("foreign"):
        why.append("%s is on ES's screen" % ", ".join(tree_f["foreign"]))
    return bool(why), "; ".join(why)


def check(probe=None, es_output=screen_map.CURRENT.top, window_s=WINDOW_S, samples=SAMPLES,
          no_window_s=NO_WINDOW_S, absent_s=ABSENT_S, events_seen=None,
          progress=None, clock=time.monotonic, sleep=time.sleep):
    """Watches ES for window_s (longer when its process or window is missing, up to absent_s /
    no_window_s, stopping early when it comes back) and judges. events_seen is a callable -> int
    (esevents' counter) or None, and progress(text) updates the sheet while it waits.
    """
    probe = probe or Probe()
    ev = {"window_s": window_s}
    ev0 = events_seen() if events_seen else None
    t0 = clock()
    step = window_s / max(1, samples - 1)
    states, windows, visible, first, last = [], [], None, None, None
    pid = None
    tree_ok = True
    tf = None
    missing_since = None
    http1 = probe.http("/caps")
    es_game = None
    if http1[0] == "ok":
        rg = probe.http("/runningGame")
        if rg[0] == "ok" and isinstance(rg[2], dict) and rg[2].get("name"):
            es_game = "%s (%s)" % (rg[2].get("name"), rg[2].get("systemName") or "?")
    i = 0
    while True:
        now = clock() - t0
        pids = probe.es_pids()
        if pids:
            if pid not in pids:
                pid = pids[0]
                first = None  # a new ES process, start measuring again
            st = probe.stat(pid)
            cx = probe.ctxt(pid)
            if st:
                states.append(st["state"])
                cur = (cx, st["utime"] + st["stime"])
                if first is None:
                    first = cur
                last = cur
        tree = probe.sway_tree()
        if tree is None:
            tree_ok = False
        else:
            tf = tree_facts(tree, es_output)
            windows.append(tf["es_window"])
            if tf["es_window"]:
                visible = tf["es_visible"]
                missing_since = None
            elif missing_since is None:
                missing_since = now
        i += 1
        done_base = i >= samples
        if done_base:
            extend = None
            if not pids and now < absent_s:
                extend = "ES's process is missing; waiting up to %d s" % absent_s
            elif pids and windows and not windows[-1] and missing_since is not None \
                    and now - missing_since < no_window_s and not probe.runemu_pids():
                extend = "ES's window is missing; watching up to %d s" % no_window_s
            if extend is None:
                break
            if progress:
                progress(extend)
        sleep(step)
    total = clock() - t0
    ev["window_s"] = round(total, 2)
    http2 = probe.http("/caps")
    ev["http_fail"] = http1[0] != "ok" and http2[0] != "ok"
    ev["http_note"] = http1[0] if http1[0] != "ok" else "%.2f s" % http1[1]
    if http1[0] != "ok" and http2[0] != "ok":
        ev["http_note"] = "%s twice (%s limit)" % (http2[0], _fmt_s(probe.http_timeout))
    ev["sway_ok"] = tree_ok and tf is not None
    if tf:
        ev["output_on"] = tf["output_on"]
        ev["es_output"] = tf["es_output"]
        ev["focused"] = tf["es_focused"]
    ev["visible"] = visible
    ev["windows"] = windows
    ev["window_missing_s"] = round(total - missing_since, 1) if missing_since is not None else 0.0
    alive_pids = probe.es_pids()
    if pid is None or not alive_pids:
        ev["pid"] = None
        ev["absent_s"] = round(total, 1)
        ev["essway"] = probe.essway_state()
        return judge(ev)
    ev["pid"] = pid
    ev["states"] = states
    ev["zombie"] = bool(states) and states[-1] == "Z"
    st = probe.stat(pid)
    up = probe.uptime()
    if st and up is not None:
        ev["age_s"] = round(up - st["starttime"] / float(probe.clk_tck), 1)
    if first and last and first[0] and last[0]:
        ev["vol_delta"] = last[0][0] - first[0][0]
        ev["invol_delta"] = last[0][1] - first[0][1]
        ev["cpu_delta_s"] = round((last[1] - first[1]) / float(probe.clk_tck), 2)
    running, why = game_running(probe, es_game, tf)
    ev["game"] = running
    ev["game_why"] = why
    ev["powersaver"] = probe.powersaver()
    if ev0 is not None:
        ev1 = events_seen()
        ev["events_during"] = max(0, (ev1 or 0) - ev0)
    h = judge(ev)
    log.info("ES health: %s - %s | %s", h.verdict, h.summary, "; ".join(h.notes))
    return h


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Is EmulationStation running / frozen? (read-only)")
    ap.add_argument("--window", type=float, default=WINDOW_S)
    ap.add_argument("--es-output", default=screen_map.CURRENT.top)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    h = check(es_output=a.es_output, window_s=a.window,
              progress=lambda t: print("...", t, file=sys.stderr))
    if a.json:
        print(json.dumps(h.to_dict(), indent=1, default=str))
    else:
        print("%s: %s" % (h.verdict.upper(), h.summary))
        for n in h.notes:
            print("  -", n)
        print("restart offered:", h.offer_restart, "| restart-anyway:", h.allow_force)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
