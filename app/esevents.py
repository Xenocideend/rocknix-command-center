#!/usr/bin/env python3
"""esevents: EmulationStation's selection, game and screensaver events, delivered to rp5deck.

ES's HTTP API cant report the highlighted game, only its event scripts can. So rp5deck
installs tiny hooks (es-hooks/, via tools/install-es-hooks.sh) for game-selected,
system-selected, game-start, game-end, screensaver-start, screensaver-stop, sleep and wake.

ES runs them async through `sh -c` and never waits (Scripting.cpp executeScript and
Platform.cpp). A hook can never stall ES, but hooks for back to back events run at the same
time and can finish out of order. ES also shell-parses the arguments before the hook runs
(see name_guard.py).

The spool, one file per event, never overwritten
-------------------------------------------------
Each hook writes its event as its own file in /var/run/rp5deck/es-events/ (tmpfs),
atomically: a hidden `.tmp.<pid>` in the same folder, then mv onto

    <start>-<anchor>-<pid>.ev        like 000000008453-0000000408-0000000415.ev

anchor is the `sh -c` process ES forked for that event and start is its start time (clock
ticks since boot). ES makes those on its own thread one event after another (only the hooks
run detached), so (start, anchor) is the order ES fired them no matter how late the hook ran.
pid (the hook's own) keeps names unique. The fields are fixed width so the names also sort
right as text.

SpoolReader reads the files in key order and deletes each one once handled:

A selection event (game/system-selected) or a screensaver/sleep/wake event older than one
already delivered in its class is stale (a hook that ran late) and gets dropped, so the
companion never shows the previous game because of reordering.

Within one batch only the newest selection and the newest idle event get delivered, the
rest would only flicker. Every game-start/game-end in the batch is delivered in order unless
it's older than one already delivered.

The folder is bounded. Past `max_spool` files the oldest get dropped and logged (the hooks
also prune past 512 when nothing reads them).

Record format (version 1): NUL-terminated key=value fields in this order

    rp5deck-es-event=1 \\0 event=<event> \\0 argc=<N> \\0 arg1=<...> \\0 ... argN=<...> \\0

NUL is the one byte that cant be in a command line argument, so any game name or path works
(apostrophes, '&', spaces, newlines, non-ASCII, bad UTF-8). Values split on the first '='
only. Bytes decode as UTF-8 with surrogateescape so a non-UTF-8 file name comes back to the
same bytes through os.fsencode(). `tr '\\0' '\\n' < FILE` shows one on the device.

Argument layouts (from emulationstation-next's Scripting/FileData):
    game-selected    system, rompath, name
    system-selected  system            (on the device: "system [rompath name]")
    game-start       rompath, rom basename, name   (paths come shell escaped, see below)
    game-end         same as game-start
    screensaver-*, sleep, wake: no game arguments (whatever ES passes is kept)
ES drops every argument after the first empty one, so a missing trailing argument is
normal.

The watcher is one daemon thread with inotify (ctypes) on the spool folder for
IN_MOVED_TO|IN_CLOSE_WRITE of `*.ev` names, plus a slow safety rescan. Without inotify (the
Windows PC, tests) it rescans every `stat_poll` seconds.

A second daemon thread polls ES's /runningGame every `poll_interval` seconds as a fallback
(hooks not installed, or a missed game-start). A failed or timed out request is UNKNOWN, never
"not running". Only ES's own {"msg": "NO GAME RUNNING"} counts, and a poll-sourced game-end
needs `end_after` of those in a row. Changes come out as made-up "game-start"/"game-end"
events with source="poll". Both threads call on_event(EsEvent) from their own thread, so the
caller has to hand it to its own loop (main.py posts it to the UI thread).

    python3 esevents.py --watch [--seconds N]     print events as they come in
    python3 esevents.py --parse FILE              decode one record file
"""
import collections
import ctypes
import errno
import logging
import os
import re
import select
import socket
import struct
import sys
import threading
import time

log = logging.getLogger("rp5deck.esevents")

DEFAULT_SPOOL_DIR = "/var/run/rp5deck/es-events"
ENV_SPOOL_DIR = "RP5DECK_ES_SPOOL"  # the same override the hook scripts use
ENV_EVENT_FILE = "RP5DECK_ES_EVENT_FILE"  # older setting: its folder + /es-events
MAGIC_KEY = "rp5deck-es-event"
RECORD_VERSION = "1"

GAME_SELECTED = "game-selected"
SYSTEM_SELECTED = "system-selected"
GAME_START = "game-start"
GAME_END = "game-end"
SCREENSAVER_START = "screensaver-start"
SCREENSAVER_STOP = "screensaver-stop"
SLEEP = "sleep"
WAKE = "wake"
GAME_EVENTS = (GAME_SELECTED, SYSTEM_SELECTED, GAME_START, GAME_END)
IDLE_EVENTS = (SCREENSAVER_START, SCREENSAVER_STOP, SLEEP, WAKE)
EVENTS = GAME_EVENTS + IDLE_EVENTS            # every event rp5deck installs a hook for
IDLE_ON = (SCREENSAVER_START, SLEEP)          # stop companion video
IDLE_OFF = (SCREENSAVER_STOP, WAKE)           # allow it again

# ordering classes, staleness is judged within a class
EVENT_CLASS = {GAME_SELECTED: "select", SYSTEM_SELECTED: "select",
               GAME_START: "game", GAME_END: "game",
               SCREENSAVER_START: "idle", SCREENSAVER_STOP: "idle", SLEEP: "idle",
               WAKE: "idle"}
COALESCED = ("select", "idle")                # only the newest per batch matters

DEFAULT_MAX_SPOOL = 256
SPOOL_NAME_RE = re.compile(r"^([0-9]+)-([0-9]+)-([0-9]+)\.ev$")
TMP_PREFIX = ".tmp."
TMP_MAX_AGE = 300.0

IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_Q_OVERFLOW = 0x00004000
_WATCH_MASK = IN_CLOSE_WRITE | IN_MOVED_TO
_INOTIFY_HEADER = struct.Struct("iIII")          # wd, mask, cookie, name length

_ROMS_SYSTEM_RE = re.compile(r"/roms/([^/]+)/")

EsEvent = collections.namedtuple(
    "EsEvent", "kind system rom_path name args source t game seq")
EsEvent.__new__.__defaults__ = ("hook", 0.0, None, None)
EsEvent.__doc__ = """One ES event. kind is one of EVENTS; system/rom_path/name
are "" when unknown; args are the raw hook arguments; source is "hook" or
"poll"; t is time.monotonic() at receipt; game is the es_api.Game for
poll-sourced events (None for hook events); seq is the spool key (a tuple of
ints in ES's fire order) for hook events read from the spool."""


def spool_dir_path(env=None):
    """RP5DECK_ES_SPOOL, else <dir of RP5DECK_ES_EVENT_FILE>/es-events, else
    /var/run/rp5deck/es-events, the same rule the hook scripts use.
    """
    env = os.environ if env is None else env
    d = env.get(ENV_SPOOL_DIR)
    if d:
        return d
    f = env.get(ENV_EVENT_FILE)
    if f and "/" in f.replace(os.sep, "/"):
        return os.path.join(os.path.dirname(f), "es-events")
    return DEFAULT_SPOOL_DIR


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------
def parse_fields(data):
    """bytes -> dict of key -> value, or None if it isnt a v1 record. A cut off record (no final NUL)
    gets refused instead of half read.
    """
    if not data or not data.endswith(b"\0"):
        return None
    fields = {}
    order = []
    for chunk in data[:-1].split(b"\0"):
        key, sep, value = chunk.partition(b"=")
        if not sep:
            return None
        k = key.decode("ascii", "replace")
        fields[k] = value.decode("utf-8", "surrogateescape")
        order.append(k)
    if not order or order[0] != MAGIC_KEY or fields.get(MAGIC_KEY) != RECORD_VERSION:
        return None
    return fields


def system_from_path(rom_path):
    """'/storage/roms/snes/x.zip' -> 'snes' (also /storage/games-internal/roms/..)."""
    m = _ROMS_SYSTEM_RE.search(rom_path or "")
    return m.group(1) if m else ""


def _looks_like_path(s):
    return isinstance(s, str) and s.startswith("/")


_BACKSLASH_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def unescape_shell_backslashes(s):
    """Undoes shell backslash escaping: 'Foo\\ \\(Bar\\).zip' -> 'Foo (Bar).zip'.

    ES's game-start/game-end hooks get their paths pre-escaped for a shell, a backslash before
    every character a shell treats specially (space, '(', ')', ','), like

        rom='/storage/roms/gb/Adventures\\ of\\ Rocky\\ and\\ Bullwinkle\\
              and\\ Friends,\\ The\\ \\(USA\\).zip'

    while the /runningGame poll gives the same file's clean path. Without this the two never
    match even though they're the same file. Dropping one backslash per escaped character is the
    exact inverse. A real backslash cant be in a ROM path here (Linux paths), so doing this to
    every game-start/game-end argument is safe.
    """
    if not isinstance(s, str) or "\\" not in s:
        return s
    return _BACKSLASH_ESCAPE_RE.sub(r"\1", s)


def interpret(kind, args, source="hook", t=0.0, seq=None):
    """Turns a hook's raw arguments into an EsEvent (pure, see the module doc)."""
    args = list(args)
    if kind in (GAME_START, GAME_END):
        # ES gives these two events' paths shell escaped (see unescape_shell_backslashes). Every field
        # below (system, rom, name) comes from `args`, so fixing them here is enough for the hook path
        # to match the poll's clean path.
        args = [unescape_shell_backslashes(a) for a in args]
    system = rom = name = ""
    if kind == SYSTEM_SELECTED:
        system = args[0] if args else ""
        if len(args) >= 2 and _looks_like_path(args[1]):
            rom = args[1]
        if len(args) >= 3:
            name = args[2]
    elif kind == GAME_SELECTED:
        if len(args) >= 2 and _looks_like_path(args[1]):
            system, rom = args[0], args[1]
            name = args[2] if len(args) >= 3 else ""
        elif args and _looks_like_path(args[0]):  # also take (rompath, name)
            rom = args[0]
            name = args[1] if len(args) >= 2 else ""
    elif kind in (GAME_START, GAME_END):
        idx = next((i for i, a in enumerate(args) if _looks_like_path(a)), None)
        if idx is not None:
            rom = args[idx]
            if idx >= 1:
                system = args[0]
            rest = args[idx + 1:]
            # (rompath, basename, name), the last one is the display name
            name = rest[-1] if rest else ""
    if rom and not system:
        system = system_from_path(rom)
    if rom and not name:
        name = os.path.splitext(os.path.basename(rom))[0]
    return EsEvent(kind, system, rom, name, tuple(args), source, t, None, seq)


def parse_record(data, t=0.0, seq=None):
    """bytes of one record file -> EsEvent, or None if broken or unknown."""
    fields = parse_fields(data)
    if fields is None:
        return None
    kind = fields.get("event", "")
    if kind not in EVENTS:
        return None
    try:
        argc = int(fields.get("argc", "0"))
    except ValueError:
        return None
    args = []
    for i in range(1, argc + 1):
        k = "arg%d" % i
        if k not in fields:
            return None                     # a record missing a promised arg is broken
        args.append(fields[k])
    return interpret(kind, args, "hook", t, seq)


def read_record(path, t=None, seq=None):
    try:
        with open(path, "rb") as f:
            data = f.read(65536)
    except OSError:
        return None
    return parse_record(data, time.monotonic() if t is None else t, seq)


def build_record(event, args):
    """What the hook scripts write, built in Python (tests, and install-es-hooks.sh's self-test
    compares against it).
    """
    out = [b"%s=%s\0" % (MAGIC_KEY.encode(), RECORD_VERSION.encode()),
           b"event=%s\0" % event.encode(), b"argc=%d\0" % len(args)]
    for i, a in enumerate(args, 1):
        v = a if isinstance(a, bytes) else a.encode("utf-8", "surrogateescape")
        out.append(b"arg%d=" % i + v + b"\0")
    return b"".join(out)


# ---------------------------------------------------------------------------
# The spool (plain file operations, no threads)
# ---------------------------------------------------------------------------
def spool_key(name):
    """'000000008453-0000000408-0000000415.ev' -> (8453, 408, 415), else None."""
    m = SPOOL_NAME_RE.match(name)
    return tuple(int(g) for g in m.groups()) if m else None


def spool_name(key):
    return "%012d-%010d-%010d.ev" % tuple(key)


def list_spool(directory):
    """[(key, name)] of the event files in fire order ([] if unreadable)."""
    out = []
    try:
        with os.scandir(directory) as it:
            for e in it:
                k = spool_key(e.name)
                if k is not None:
                    out.append((k, e.name))
    except OSError:
        return []
    out.sort()
    return out


def write_spool_event(directory, key, event, args):
    """What a hook does, in Python for tests and tools: temp file + rename."""
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, "%s%d.%d" % (TMP_PREFIX, os.getpid(), threading.get_ident()))
    with open(tmp, "wb") as f:
        f.write(build_record(event, args))
    final = os.path.join(directory, spool_name(key))
    os.replace(tmp, final)
    return final


class SpoolReader:
    """Reads the spool folder: drain() takes every event file in key order, drops stale, replaced
    or excess ones, deletes every file it handled, and returns the EsEvents to deliver in
    order. Not thread safe, one reader (the Watcher thread) owns it.
    """

    def __init__(self, directory, max_spool=DEFAULT_MAX_SPOOL, log_fn=None):
        self.dir = directory
        self.max_spool = max(1, int(max_spool))
        self._log = log_fn or log.info
        self.last = {}                     # class -> newest key delivered
        self.delivered = 0
        self.stale = 0
        self.superseded = 0
        self.overflow = 0
        self.bad = 0

    def _remove(self, name):
        try:
            os.remove(os.path.join(self.dir, name))
        except FileNotFoundError:
            pass
        except OSError as e:
            log.debug("esevents: cannot remove %s: %s", name, e)

    def _clean_temps(self):
        now = time.time()
        try:
            with os.scandir(self.dir) as it:
                for e in it:
                    if e.name.startswith(TMP_PREFIX):
                        try:
                            if now - e.stat().st_mtime > TMP_MAX_AGE:
                                os.remove(e.path)   # a hook killed mid-write
                        except OSError:
                            pass
        except OSError:
            pass

    def drain(self, t=None):
        t = time.monotonic() if t is None else t
        files = list_spool(self.dir)
        if not files:
            return []
        if len(files) > self.max_spool:
            extra = files[:len(files) - self.max_spool]
            files = files[len(files) - self.max_spool:]
            for _, name in extra:
                self._remove(name)
            self.overflow += len(extra)
            self._log("esevents: spool over %d files: dropped the %d oldest"
                      % (self.max_spool, len(extra)))
        batch = []
        for key, name in files:
            ev = read_record(os.path.join(self.dir, name), t, key)
            self._remove(name)
            if ev is None:
                self.bad += 1
                log.debug("esevents: unparsable spool file %s dropped", name)
                continue
            batch.append(ev)
        newest = {}
        for i, ev in enumerate(batch):
            c = EVENT_CLASS.get(ev.kind)
            if c in COALESCED:
                newest[c] = i
        out = []
        for i, ev in enumerate(batch):
            c = EVENT_CLASS.get(ev.kind)
            last = self.last.get(c)
            if last is not None and ev.seq <= last:
                self.stale += 1
                log.debug("esevents: stale %s %s (older than %s) dropped", ev.kind, ev.seq, last)
                continue
            if c in COALESCED and newest.get(c) != i:
                self.superseded += 1
                continue
            self.last[c] = ev.seq
            out.append(ev)
        self.delivered += len(out)
        if len(batch) > 8:
            self._clean_temps()
        return out


# ---------------------------------------------------------------------------
# inotify (ctypes)
# ---------------------------------------------------------------------------
def parse_inotify_events(buf):
    """Raw inotify read -> [(wd, mask, cookie, name)], a cut off tail gets dropped."""
    out = []
    i, n, hs = 0, len(buf), _INOTIFY_HEADER.size
    while i + hs <= n:
        wd, mask, cookie, length = _INOTIFY_HEADER.unpack_from(buf, i)
        i += hs
        if i + length > n:
            break
        name = buf[i:i + length].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        i += length
        out.append((wd, mask, cookie, name))
    return out


def is_spool_event(mask, name):
    """An event file showing up by rename (or a direct close after write), never a hook's
    `.tmp.<pid>` file. A queue overflow means rescan.
    """
    if mask & IN_Q_OVERFLOW:
        return True
    return bool(mask & _WATCH_MASK) and spool_key(name) is not None


class _Inotify:
    def __init__(self, fd):
        self.fd = fd

    @classmethod
    def create(cls, directory):
        """None where there's no inotify (not Linux), never raises."""
        if not sys.platform.startswith("linux"):
            return None
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.inotify_init1.argtypes = [ctypes.c_int]
            libc.inotify_init1.restype = ctypes.c_int
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            libc.inotify_add_watch.restype = ctypes.c_int
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return None
            wd = libc.inotify_add_watch(fd, os.fsencode(directory), _WATCH_MASK)
            if wd < 0:
                os.close(fd)
                return None
            return cls(fd)
        except (OSError, AttributeError, ValueError):
            return None

    def read_events(self):
        try:
            buf = os.read(self.fd, 16384)
        except OSError:
            return []
        return parse_inotify_events(buf) if buf else []

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Running-game poll (pure decision logic + a probe + a thread)
# ---------------------------------------------------------------------------
class _Sentinel:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name

    def __bool__(self):
        return False


UNKNOWN = _Sentinel("UNKNOWN")  # ES didnt answer (timeout, refused, bad JSON)
NOT_RUNNING = _Sentinel("NOT_RUNNING")  # ES answered {"msg": "NO GAME RUNNING"}
DEFAULT_END_AFTER = 2


def probe_running_game(client=None):
    """/runningGame, telling a failure apart from nothing running: an es_api.Game, NOT_RUNNING (ES
    said {"msg": "NO GAME RUNNING"}), or UNKNOWN (no answer in time, refused, a non-JSON or odd
    body). es_api.running_game() returns None for both of the last two on purpose, which is why
    the poll cant use it. Never raises.
    """
    try:
        import es_api
        c = client if client is not None else es_api._default_client
        data = c._get("/runningGame")
        if not isinstance(data, dict):
            return UNKNOWN
        if "msg" in data:
            return NOT_RUNNING if "NO GAME" in str(data.get("msg", "")).upper() else UNKNOWN
        game = es_api._parse_game(data, c.base_url)
        if game.system and game.id:
            detail = c.game_detail(game.system, game.id)
            if detail is not None:
                game.media.update(detail.media)
        return game
    except Exception as e:                  # noqa: BLE001 - a probe must never raise
        log.debug("esevents: probe failed: %s", e)
        return UNKNOWN


def game_key(game):
    """identity of an es_api.Game for spotting changes"""
    if game is None or isinstance(game, _Sentinel):
        return None
    return (getattr(game, "system", ""), getattr(game, "rom_path", "") or getattr(game, "id", ""))


class RunningPoll:
    """Turns /runningGame results one after another into start/end events. Pure, feed(result, t)
    returns the EsEvents to emit.

    result is an es_api.Game (running), NOT_RUNNING (ES said nothing runs), or UNKNOWN / None (no
    usable answer). UNKNOWN never ends a game and breaks a run of NOT_RUNNING answers, and a
    poll-sourced game-end needs `end_after` NOT_RUNNING answers in a row. None counts as UNKNOWN
    since es_api.running_game() returns None for failures too.
    """

    def __init__(self, end_after=DEFAULT_END_AFTER):
        self.key = None
        self.end_after = max(1, int(end_after))
        self.clean = 0                      # consecutive NOT_RUNNING answers
        self.unknown = 0

    def feed(self, game, t=0.0):
        if game is None or game is UNKNOWN:
            self.unknown += 1
            self.clean = 0
            return []
        if game is NOT_RUNNING:
            self.clean += 1
            if self.key is None or self.clean < self.end_after:
                return []
            out = [EsEvent(GAME_END, self.key[0], self.key[1], "", (), "poll", t, None)]
            self.key = None
            return out
        self.clean = 0
        k = game_key(game)
        if k == self.key:
            return []
        out = []
        if self.key is not None:
            out.append(EsEvent(GAME_END, self.key[0], self.key[1], "", (), "poll", t, None))
        rom = getattr(game, "rom_path", "") or ""
        out.append(EsEvent(GAME_START, getattr(game, "system", "") or system_from_path(rom), rom,
                           getattr(game, "name", "") or "", (), "poll", t, game))
        self.key = k
        return out


def _is_es_api_running_game(fn):
    return (getattr(fn, "__module__", None) == "es_api"
            and getattr(fn, "__name__", None) == "running_game")


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------
class Watcher:
    """Watches the hook spool (and can poll the running game) and calls on_event(EsEvent) from its
    own threads. start()/stop(), and stop() returns within about 1 s with no thread left behind.

    running_probe() -> Game | NOT_RUNNING | UNKNOWN is what the poll uses. running_game() is the
    older interface (Game or None). es_api's own running_game gets swapped for
    probe_running_game (same request, but it can tell a timeout from nothing running), and any
    other callable has its None treated as UNKNOWN, so it can start a game but never end one.
    """

    def __init__(self, on_event, spool_dir=None, running_game=None, running_probe=None,
                 poll_interval=3.0, stat_poll=1.0, read_initial=True, log_fn=None,
                 max_spool=DEFAULT_MAX_SPOOL, end_after=DEFAULT_END_AFTER, rescan=5.0):
        self.on_event = on_event
        self.dir = spool_dir or spool_dir_path()
        self._log = log_fn or log.info
        if running_probe is None and running_game is not None:
            if _is_es_api_running_game(running_game):
                running_probe = probe_running_game
            else:
                running_probe = running_game          # None -> UNKNOWN in RunningPoll
        self.running_probe = running_probe
        self.poll_interval = poll_interval
        self.stat_poll = stat_poll
        self.rescan = rescan
        self.read_initial = read_initial
        self._stop = threading.Event()
        self._wake_r, self._wake_w = socket.socketpair()
        self._threads = []
        self.inotify = None
        self.mode = None            # "inotify" | "stat" once running
        self.events_seen = 0
        self.last_error = None
        self.reader = SpoolReader(self.dir, max_spool, self._log)
        self._poll = RunningPoll(end_after)

    @property
    def path(self):
        """the watched folder (older callers called it the record path)"""
        return self.dir

    def start(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as e:
            self.last_error = "mkdir %s: %s" % (self.dir, e)
            self._log("esevents: " + self.last_error)
        self.inotify = _Inotify.create(self.dir)
        self.mode = "inotify" if self.inotify else "stat"
        self._log("esevents: watching spool %s (%s)" % (self.dir, self.mode))
        t = threading.Thread(target=self._run_spool, name="rp5deck-esevents", daemon=True)
        self._threads.append(t)
        t.start()
        if self.running_probe is not None and self.poll_interval:
            p = threading.Thread(target=self._run_poll, name="rp5deck-es-poll", daemon=True)
            self._threads.append(p)
            p.start()

    def stop(self, timeout=1.5):
        self._stop.set()
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass
        for t in self._threads:
            t.join(timeout)
        for s in (self._wake_r, self._wake_w):
            try:
                s.close()
            except OSError:
                pass
        if self.inotify is not None:
            self.inotify.close()
            self.inotify = None

    # -- delivery ---------------------------------------------------------
    def _emit(self, ev):
        self.events_seen += 1
        try:
            self.on_event(ev)
        except Exception:           # noqa: BLE001 - one bad consumer call must not kill the thread
            log.exception("esevents: on_event failed")

    def _drain(self):
        try:
            evs = self.reader.drain()
        except Exception as e:      # noqa: BLE001 - never let the spool kill the thread
            self.last_error = "drain: %s" % e
            log.exception("esevents: drain failed")
            return
        for ev in evs:
            self._emit(ev)

    # -- threads ----------------------------------------------------------
    def _run_spool(self):
        if self.read_initial:
            self._drain()
        else:
            for _, name in list_spool(self.dir):  # start clean, forget the backlog
                self.reader._remove(name)
        while not self._stop.is_set():
            rlist = [self._wake_r]
            timeout = self.stat_poll
            if self.inotify is not None:
                rlist.append(self.inotify.fd)
                timeout = self.rescan
            try:
                ready, _, _ = select.select(rlist, [], [], timeout)
            except (OSError, ValueError) as e:
                if getattr(e, "errno", None) == errno.EINTR:
                    continue
                self.last_error = "select: %s" % e
                self._stop.wait(0.2)
                continue
            if self._wake_r in ready:
                return
            if self.inotify is not None and self.inotify.fd in ready:
                evs = self.inotify.read_events()
                if not any(is_spool_event(m, n) for _, m, _, n in evs):
                    continue
            self._drain()           # inotify hit, safety rescan, or stat-mode tick

    def _run_poll(self):
        while not self._stop.is_set():
            try:
                g = self.running_probe()
            except Exception as e:  # noqa: BLE001 - a probe should never raise, but be sure
                self.last_error = "running_game: %s" % e
                g = UNKNOWN
            for ev in self._poll.feed(g, time.monotonic()):
                self._emit(ev)
            if self._stop.wait(self.poll_interval):
                return


# ---------------------------------------------------------------------------
# CLI (device-side checks)
# ---------------------------------------------------------------------------
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--parse", metavar="FILE")
    ap.add_argument("--no-poll", action="store_true", help="do not poll /runningGame")
    ap.add_argument("--spool", metavar="DIR", help="spool directory (default %s)"
                    % DEFAULT_SPOOL_DIR)
    a = ap.parse_args(argv)
    if a.parse:
        with open(a.parse, "rb") as f:
            data = f.read()
        print("fields:", parse_fields(data))
        print("event: ", parse_record(data))
        return 0 if parse_record(data) else 1
    if a.watch:
        def show(ev):
            print("%.3f %-17s src=%-4s seq=%s system=%r rom=%r name=%r args=%r"
                  % (time.monotonic(), ev.kind, ev.source, ev.seq, ev.system, ev.rom_path,
                     ev.name, ev.args), flush=True)
        w = Watcher(show, spool_dir=a.spool, running_probe=None if a.no_poll else
                    probe_running_game, log_fn=lambda m: print(m, flush=True))
        w.start()
        try:
            time.sleep(a.seconds)
        except KeyboardInterrupt:
            pass
        w.stop()
        r = w.reader
        print("events seen: %d (mode %s); spool: delivered %d, stale %d, superseded %d, "
              "overflow %d, bad %d" % (w.events_seen, w.mode, r.delivered, r.stale,
                                       r.superseded, r.overflow, r.bad))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(_main(sys.argv[1:]))
