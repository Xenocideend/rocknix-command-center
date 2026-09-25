#!/usr/bin/env python3
"""
Audio backend module for rp5deck (Retroid Pocket 5 / ROCKNIX / PipeWire +
WirePlumber + pipewire-pulse).

Volume is the top-priority feature, so this module is deliberately narrow and
paranoid about failure: every external call has a timeout, and a failure is
reported as state "error" (or None fields) - never as a value that looks like
a real, quiet volume. See AUDIO-NOTES.md for how each fact below was verified
on the device.

Key facts this module encodes (verified on a real RP5, ROCKNIX 20260923):

- Hardware volume keys run /usr/bin/input_sense -> /usr/bin/volume, which sets
  the sink volume with `pactl -- set-sink-volume @DEFAULT_SINK@ N%` and saves N
  to system.cfg as `audio.volume` (0-100 integer). If the app changes volume
  through wpctl only, the *next* hardware key press recomputes from the stale
  saved value and jumps. So live dragging may use wpctl, but any value the user
  "lands on" must be committed through /usr/bin/volume.
- /usr/bin/volume's argument is NOT a delta except for the literal strings
  "+"/"up", "-"/"down" and "restore" (a no-op resync). Any other argument is
  used as-is as the new absolute percentage (0-100, clamped by the script; a
  non-numeric value or empty string falls back to 50 inside the script). This
  module always calls it with a plain integer string in [0, 100].
- /usr/bin/volume has no concept of mute. Mute is wpctl-only
  (`wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle`) and is not persisted anywhere;
  a volume-key press does not touch mute.
- The card's HiFi profile provides the Speaker and Headphones sinks. Something
  else (ROCKNIX's 092 dual-screen daemon, or the HDMI switch path) can remove
  the sink entirely, e.g. while switching to an HDMI audio profile; 092 then
  restores HiFi. When @DEFAULT_AUDIO_SINK@ does not resolve to anything, this
  module reports state "restoring", not a fake/zero volume.
- `pactl list cards` (and `pactl list short cards`) reliably timed out
  ("Connection failure: Timeout") against this device's pipewire-pulse, twice
  in a row, while `wpctl status`, `wpctl get-volume` and `wpctl inspect` all
  worked immediately. So profile/card state is deliberately never queried
  with `pactl list cards` here - see AUDIO-NOTES.md.
- `pactl subscribe` and `pactl info` are not used anywhere in this module.
  Measured on the device (23 Sep): `pactl subscribe` delivered zero events
  across a hardware key press, two `/usr/bin/volume` calls and idle time,
  through both a pipe and a pty, and `pactl info` hung for the full 5 s
  timeout. The PulseAudio compatibility shim answers *writes*
  (`set-sink-volume`) but not *reads or subscriptions* on this build.
  `Subscriber` instead watches inotify on `system.cfg` (catches
  `/usr/bin/volume`, i.e. the hardware keys) and `pw-mon` (catches
  `wpctl set-volume` and anything else that bypasses `/usr/bin/volume`).
  See AUDIO-NOTES.md for the device evidence and the pw-mon block format.
- `wpctl get-volume <id>` can return exit code 0 with NOTHING on stdout and an
  error on stderr (e.g. "Node '999999' not found"). Exit code is therefore not
  trusted anywhere in this module; every parse requires the expected pattern
  to actually be present in stdout.
- Playback stream node ids from `pw-dump` (media.class Stream/Output/Audio)
  are ephemeral: a node seen in one pw-dump can be gone by the next call, a
  handful of seconds later (observed with EmulationStation's own UI-sound
  stream). list_streams() and the per-stream setters treat a vanished id as a
  normal, non-fatal outcome, not an error.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

# Timeout for a single external call. Chosen so a wedged pactl/wpctl/pw-dump
# can never hang the UI thread that (indirectly) waits on these functions.
DEFAULT_TIMEOUT = 3.0
PW_DUMP_TIMEOUT = 5.0

# Subscriber tunables. /usr/bin/volume rewrites system.cfg via a temp file
# (system.cfgXXXXXX) then an atomic rename onto system.cfg - see AUDIO-NOTES.md.
CONFIG_DIR = "/storage/.config/system/configs"
CONFIG_FILE_NAME = "system.cfg"
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
_INOTIFY_WATCH_MASK = IN_CLOSE_WRITE | IN_MOVED_TO
_INOTIFY_HEADER = struct.Struct("iIII")  # wd, mask, cookie, name-length

PW_MON_CMD = ["pw-mon", "--no-colors"]
# "At most one callback per 50-100ms, with a trailing callback" (task spec):
# fire as soon as a marker line is seen (bounded latency), and let the
# pending-set + single-shot timer below fold a burst into one flush.
PW_MON_COALESCE = 0.075
PW_MON_RESTART_BACKOFF = 1.0

VOLUME_SCRIPT = "/usr/bin/volume"
DEFAULT_SINK_ALIAS = "@DEFAULT_AUDIO_SINK@"

# wpctl accepts values above 1.0 (a "boost" beyond 100%). Dragging live is
# allowed a bit of headroom; commit_master() clamps harder because
# /usr/bin/volume only understands 0-100 (i.e. 0.0-1.0).
MAX_LIVE_VOLUME = 1.5
MAX_COMMIT_VOLUME = 1.0

_VOLUME_LINE_RE = re.compile(r"Volume:\s*([0-9]*\.?[0-9]+)(\s*\[MUTED\])?", re.IGNORECASE)
_INSPECT_HEADER_RE = re.compile(r"^id\s+(\d+)\s*,")
_PROP_LINE_RE = re.compile(r"^\*?\s*([A-Za-z0-9_.]+)\s*=\s*\"?([^\"\n]*)\"?\s*$")


log = logging.getLogger("rp5deck.audio")

# How long pw-mon gets to exit after SIGTERM before it is SIGKILLed.
PW_MON_TERM_TIMEOUT = 1.0


class _LogLimiter:
    """Log a failure (with its traceback when called inside an except block)
    the first time a key fails, then at most once per `interval` seconds
    with a count of the repeats in between: the Subscriber thread loops, and
    a failure that recurs per event must not flood the app's 512 KB log."""

    def __init__(self, interval: float = 60.0, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._state: dict = {}

    def _due(self, key):
        now = self.clock()
        with self._lock:
            st = self._state.get(key)
            if st is not None and now - st[0] < self.interval:
                st[1] += 1
                return None
            repeats = st[1] if st is not None else 0
            self._state[key] = [now, 0]
            return repeats

    @staticmethod
    def _suffix(repeats):
        return " (%d more like this since the last report)" % repeats if repeats else ""

    def exception(self, key, msg, *args) -> bool:
        repeats = self._due(key)
        if repeats is None:
            return False
        log.exception(msg + self._suffix(repeats), *args)
        return True

    def warning(self, key, msg, *args) -> bool:
        repeats = self._due(key)
        if repeats is None:
            return False
        log.warning(msg + self._suffix(repeats), *args)
        return True


# --------------------------------------------------------------------------
# Low-level process helpers - the only place that touches subprocess.
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = DEFAULT_TIMEOUT):
    """Run a command and capture output. Never raises, never hangs.

    Returns (returncode, stdout, stderr). returncode is None if the process
    never produced a result at all (binary missing, timed out, or any other
    OS-level failure) - callers must treat that as a hard failure regardless
    of what is in stdout/stderr.
    """
    if shutil.which(cmd[0]) is None:
        return None, "", "binary not found: %s" % cmd[0]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, "", "exec failed or timed out: %s" % " ".join(cmd)
    return proc.returncode, proc.stdout, proc.stderr


def _wpctl_volume_line(out: str):
    """Parse a `wpctl get-volume` stdout line. Returns (volume, muted) or
    (None, None) if the expected pattern is not present."""
    m = _VOLUME_LINE_RE.search(out)
    if not m:
        return None, None
    try:
        volume = float(m.group(1))
    except ValueError:
        return None, None
    muted = m.group(2) is not None
    return volume, muted


def _inspect_props(out: str) -> dict:
    """Parse `wpctl inspect` plain-text property dump into a dict of str->str.
    Lines look like `* node.name = "foo"` or `  node.pause-on-idle = "false"`.
    """
    props = {}
    for line in out.splitlines():
        line = line.strip()
        m = _PROP_LINE_RE.match(line)
        if m:
            props[m.group(1)] = m.group(2)
    return props


def _sink_label(props: dict) -> Optional[str]:
    """Derive a short, human sink label from a `wpctl inspect` property dump.

    Prefers `node.description` (e.g. "Built-in Audio Speaker Playback"),
    stripping the common "Built-in Audio " prefix. That key was present in
    some captures on this device but appeared to be omitted in at least one
    earlier capture (field set/order was not perfectly stable), so this
    falls back to a substring match on `node.name` - verified against the
    two sinks actually seen on this device:
      alsa_output ..._HiFi__Speaker__sink     -> "Speaker Playback"
      alsa_output ..._HiFi__Headphones__sink  -> "Headphones Playback"
    If neither is usable, returns the raw node name rather than guessing.
    """
    description = props.get("node.description")
    if description:
        prefix = "Built-in Audio "
        if description.startswith(prefix):
            return description[len(prefix):]
        return description

    node_name = props.get("node.name")
    if not node_name:
        return None
    lname = node_name.lower()
    if "speaker" in lname:
        return "Speaker Playback"
    if "headphone" in lname:
        return "Headphones Playback"
    return node_name


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------
# Master volume
# --------------------------------------------------------------------------

def get_master() -> dict:
    """Read the master (default sink) volume/mute/description.

    Returns a dict:
        volume:           float, 1.0 == 100%, may exceed 1.0 (boost); None if
                           unavailable.
        muted:             bool, or None if unavailable.
        sink_description: short label ("Speaker Playback" / "Headphones
                           Playback" / raw node name), or None.
        state:            "ok"        - volume/muted are real, current values.
                           "restoring" - no default sink resolves right now
                                         (profile switch in flight, e.g. to/
                                         from HDMI); 092 is expected to fix
                                         this without our help. volume/muted
                                         are None; do not show a stale or
                                         fake value.
                           "error"     - something unexpected failed (wpctl
                                         missing, pw internals down, etc).
                                         volume/muted are None.
    """
    result = {"volume": None, "muted": None, "sink_description": None, "state": "error"}

    rc, out, _err = _run(["wpctl", "inspect", DEFAULT_SINK_ALIAS])
    if rc is None:
        return result  # wpctl missing or hung: "error"

    header = out.strip().splitlines()[0] if out.strip() else ""
    if rc != 0 or not _INSPECT_HEADER_RE.match(header):
        # The alias did not resolve to any node - most likely the speaker
        # sink is gone mid-profile-switch (see module docstring).
        result["state"] = "restoring"
        return result

    props = _inspect_props(out)
    result["sink_description"] = _sink_label(props)

    rc2, out2, _err2 = _run(["wpctl", "get-volume", DEFAULT_SINK_ALIAS])
    if rc2 is None:
        return result  # still "error", but we keep sink_description
    volume, muted = _wpctl_volume_line(out2)
    if volume is None:
        # Inconsistent: inspect resolved a node but get-volume produced
        # nothing parseable. Do not invent a number.
        return result

    result["volume"] = volume
    result["muted"] = muted
    result["state"] = "ok"
    return result


def set_master(v: float) -> bool:
    """Live volume change while dragging. No persistence (see commit_master).

    Clamped to [0, MAX_LIVE_VOLUME]. Returns True if wpctl accepted the call
    (i.e. produced no error), False otherwise. The UI should not treat a
    False return as fatal for a single dragged frame, but should not assume
    the change happened either.
    """
    v = _clamp(float(v), 0.0, MAX_LIVE_VOLUME)
    rc, out, _err = _run(["wpctl", "set-volume", DEFAULT_SINK_ALIAS, "%.4f" % v])
    if rc is None or rc != 0:
        return False
    return True


def commit_master(v: float) -> bool:
    """Persist a volume level the way the hardware keys would: through
    /usr/bin/volume, which sets the sink volume with pactl AND saves
    audio.volume to system.cfg. This is what keeps the next hardware key
    press from jumping off a stale saved value.

    v is in the same units as everywhere else in this module (1.0 == 100%),
    clamped to [0, MAX_COMMIT_VOLUME] because the script only understands an
    integer 0-100 percentage. Returns True on apparent success.
    """
    v = _clamp(float(v), 0.0, MAX_COMMIT_VOLUME)
    percent = int(round(v * 100))
    rc, _out, _err = _run([VOLUME_SCRIPT, str(percent)])
    if rc is None or rc != 0:
        return False
    return True


def toggle_master_mute() -> bool:
    """Toggle mute on the default sink. Not persisted by ROCKNIX (mute state
    lives only in PipeWire); the UI should show mute clearly since a volume
    key press does not clear it."""
    rc, _out, _err = _run(["wpctl", "set-mute", DEFAULT_SINK_ALIAS, "toggle"])
    return rc == 0


# --------------------------------------------------------------------------
# Per-app streams
# --------------------------------------------------------------------------

def list_streams() -> Optional[list]:
    """List current playback streams (pw-dump nodes with media.class
    "Stream/Output/Audio"), each as:
        {id, app_name, media_name, node_name, volume, muted, display_name}

    volume is derived from the node's raw `channelVolumes` (cube root of the
    loudest channel), which matches wpctl's displayed/accepted scale for the
    two reference values seen on this device (0.000125 <-> wpctl 0.05, and
    1.0 <-> wpctl 1.00 - see AUDIO-NOTES.md). If a stream has no readable
    Props, volume/muted are None for that entry rather than 0/False.

    Returns None (not []) if pw-dump itself could not be read at all - the
    UI must tell those two cases apart, since [] legitimately means "no app
    is playing anything right now", which is the common case on this device.
    """
    rc, out, _err = _run(["pw-dump"], timeout=PW_DUMP_TIMEOUT)
    if rc is None or rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    streams = []
    for obj in data:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue

        node_id = obj.get("id")
        app_name = props.get("application.name")
        media_name = props.get("media.name")
        node_name = props.get("node.name")
        proc_binary = props.get("application.process.binary")
        display_name = app_name or proc_binary or media_name or node_name or (
            "stream-%s" % node_id
        )

        volume = None
        muted = None
        params = info.get("params") or {}
        prop_entries = params.get("Props") or []
        if prop_entries:
            p0 = prop_entries[0] or {}
            chan = p0.get("channelVolumes")
            if isinstance(chan, list) and chan:
                try:
                    volume = max(chan) ** (1.0 / 3.0)
                except (TypeError, ValueError):
                    volume = None
            if "mute" in p0:
                muted = bool(p0.get("mute"))

        streams.append({
            "id": node_id,
            "app_name": app_name,
            "media_name": media_name,
            "node_name": node_name,
            "volume": volume,
            "muted": muted,
            "display_name": display_name,
        })
    return streams


def set_stream_volume(stream_id: int, v: float) -> bool:
    """Set a single stream's volume. Clamped to [0, MAX_LIVE_VOLUME]. Returns
    False (not a raised exception) if the stream id has already vanished -
    these ids are ephemeral, see module docstring."""
    v = _clamp(float(v), 0.0, MAX_LIVE_VOLUME)
    rc, out, _err = _run(["wpctl", "set-volume", str(stream_id), "%.4f" % v])
    if rc is None or rc != 0:
        return False
    return True


def toggle_stream_mute(stream_id: int) -> bool:
    """Toggle mute on a single stream. False if the id is gone or wpctl
    failed for any other reason."""
    rc, _out, _err = _run(["wpctl", "set-mute", str(stream_id), "toggle"])
    return rc == 0


# --------------------------------------------------------------------------
# Event subscriber
#
# `pactl subscribe` is gone (see the module docstring for the measured
# reason). Two independent sources feed the same callback instead:
#
#  1. inotify on CONFIG_DIR, filtered to CONFIG_FILE_NAME. /usr/bin/volume
#     (which the hardware volume keys call through input_sense) writes a
#     temp file `system.cfgXXXXXX` and renames it onto `system.cfg`, so the
#     event we want is IN_MOVED_TO/IN_CLOSE_WRITE whose *name* is exactly
#     `system.cfg` - the temp name must never fire.
#  2. `pw-mon --no-colors`, for changes that bypass /usr/bin/volume (our own
#     live `wpctl set-volume` drag, or another app). On this device the
#     sink's volume is a *hardware* ALSA mixer route (`route.hw-volume =
#     "true"` in `wpctl inspect`'s dump), so the live channelVolumes/mute
#     values are only ever emitted by pw-mon on the owning ALSA **Device**
#     object's Route param - the sink **Node** itself only ever reports
#     PropInfo (parameter metadata, not values) in its own "changed:"
#     blocks. Both ids are resolved once (`wpctl inspect`, which already
#     exposes the node's own id and its `device.id` property) and watched;
#     either firing is safe because the callback only means "go re-read
#     get_master()", never a value pw-mon parsed itself.
#
# Real captures backing this: AUDIO-NOTES.md "pw-mon / inotify: real device
# capture" section, and tests/fixtures/pw-mon-*.
# --------------------------------------------------------------------------


def _parse_inotify_events(buf: bytes) -> list:
    """Parse a raw `read()` of an inotify fd into a list of (wd, mask,
    cookie, name) tuples. name is "" for events with no name (len 0).
    Never raises on a truncated trailing event - it is simply dropped,
    since the next read() will include it in full."""
    events = []
    header_size = _INOTIFY_HEADER.size
    i = 0
    n = len(buf)
    while i + header_size <= n:
        wd, mask, cookie, length = _INOTIFY_HEADER.unpack_from(buf, i)
        i += header_size
        if i + length > n:
            break
        name = buf[i:i + length].split(b"\x00", 1)[0].decode("utf-8", "replace")
        i += length
        events.append((wd, mask, cookie, name))
    return events


def _config_rewrite_event(mask: int, name: str) -> bool:
    """True if this is the atomic rewrite of system.cfg itself (the rename
    target), not one of /usr/bin/volume's `system.cfgXXXXXX` temp files -
    those never match this because the name must be exactly CONFIG_FILE_NAME."""
    return name == CONFIG_FILE_NAME and bool(mask & _INOTIFY_WATCH_MASK)


_LIBC = None


def _inotify_libc():
    """libc with the two inotify calls' signatures pinned (RV3): without
    argtypes/restype ctypes guesses from the Python values, which happens to
    work for these int/char* calls but is not guaranteed."""
    global _LIBC
    if _LIBC is None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        _LIBC = libc
    return _LIBC


class _InotifyWatch:
    """ctypes wrapper around inotify_init1/inotify_add_watch. `create()`
    never raises: it returns None if inotify is unavailable (not Linux, or
    the syscalls themselves fail), so Subscriber can fall back to
    pw-mon-only rather than crash the background thread. The fd is
    O_CLOEXEC (RV3), like esevents.py's, so no child ever inherits it."""

    _faillog = _LogLimiter()

    def __init__(self, fd: int):
        self.fd = fd

    @classmethod
    def create(cls, path: str = CONFIG_DIR) -> Optional["_InotifyWatch"]:
        linux = sys.platform.startswith("linux")
        try:
            libc = _inotify_libc()
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                err = ctypes.get_errno()
                log.warning("inotify_init1 failed: %s; hardware-key volume changes are "
                            "seen only through pw-mon", os.strerror(err))
                return None
            wd = libc.inotify_add_watch(fd, os.fsencode(path), _INOTIFY_WATCH_MASK)
            if wd < 0:
                err = ctypes.get_errno()
                os.close(fd)
                log.warning("inotify watch on %s failed: %s; hardware-key volume changes "
                            "are seen only through pw-mon", path, os.strerror(err))
                return None
            return cls(fd)
        except (OSError, AttributeError, ValueError) as e:
            (log.warning if linux else log.debug)(
                "inotify unavailable (%s); hardware-key volume changes are seen only "
                "through pw-mon", e)
            return None

    def read_events(self) -> list:
        """Non-blocking: call only once select() reports fd readable."""
        try:
            buf = os.read(self.fd, 4096)
        except BlockingIOError:
            return []
        except OSError:
            self._faillog.exception("read", "inotify read failed")
            return []
        if not buf:
            return []
        return _parse_inotify_events(buf)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def _resolve_master_ids(timeout: float = DEFAULT_TIMEOUT):
    """Resolve the default sink's own node id and its owning ALSA Device id
    (`device.id` in the same `wpctl inspect` property dump `get_master()`
    already parses). Returns (node_id, device_id); either is None if
    unresolved (e.g. mid HDMI-profile switch, or wpctl missing)."""
    rc, out, _err = _run(["wpctl", "inspect", DEFAULT_SINK_ALIAS], timeout=timeout)
    if rc is None or rc != 0:
        return None, None
    header = out.strip().splitlines()[0] if out.strip() else ""
    m = _INSPECT_HEADER_RE.match(header)
    if not m:
        return None, None
    node_id = int(m.group(1))
    device_id = None
    raw = _inspect_props(out).get("device.id")
    if raw is not None:
        try:
            device_id = int(raw)
        except ValueError:
            device_id = None
    return node_id, device_id


_ID_LINE_RE = re.compile(r"^id:\s*(\d+)\s*$")
# Props:volume / Props:mute / Props:channelVolumes, but NOT volumeBase /
# volumeStep (the \b after the alternation stops at a word boundary, and
# both of those continue with a word character).
_VALUE_MARKER_RE = re.compile(r"Props:(?:volume|mute|channelVolumes)\b")


class _PwMonClassifier:
    """Incremental, line-based classifier for `pw-mon --no-colors` output.
    Feed it raw text as it arrives (feed() handles partial lines); it
    returns the list of "master" kinds to fire for that chunk, in order.

    Only a "changed:" block whose object id is the watched node/device id
    AND that contains a real Props value marker line fires. This also
    "skips the initial dump" for free, with no timer or byte-count
    heuristic: pw-mon's startup dump of every existing object is entirely
    "added:" blocks (confirmed against a real capture - AUDIO-NOTES.md);
    only a genuine live change is ever reported as "changed:".
    """

    def __init__(self, node_id: Optional[int], device_id: Optional[int]):
        self.node_id = node_id
        self.device_id = device_id
        self._buf = ""
        self._block_kind: Optional[str] = None
        self._block_id: Optional[int] = None
        self._fired = False

    def feed(self, chunk: str) -> list:
        self._buf += chunk
        fired = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            kind = self._feed_line(line)
            if kind:
                fired.append(kind)
        return fired

    def _feed_line(self, line: str) -> Optional[str]:
        stripped = line.strip()
        if stripped in ("added:", "changed:", "removed:"):
            self._block_kind = stripped[:-1]
            self._block_id = None
            self._fired = False
            return None
        if self._block_kind is None:
            return None
        if self._block_id is None:
            m = _ID_LINE_RE.match(stripped)
            if m:
                self._block_id = int(m.group(1))
            return None
        if self._fired or self._block_kind != "changed":
            return None
        if self._block_id not in (self.node_id, self.device_id):
            return None
        if _VALUE_MARKER_RE.search(line):
            self._fired = True
            return "master"
        return None


class Subscriber:
    """Background thread: inotify on system.cfg (primary - catches the
    hardware volume keys via /usr/bin/volume) plus `pw-mon` (secondary -
    catches a live `wpctl set-volume`, e.g. our own slider drag, or any
    other app). Calls `callback("master")` at most once per ~75ms burst,
    with the final value never missed - the callback only means "go
    re-read get_master()"; neither source's own parsed content is ever
    handed to the caller.

    One daemon thread, select()-based: never blocks the caller. stop()
    wakes the select loop via a self-pipe, joins within ~1s, and kills any
    running pw-mon (no zombies). If pw-mon is absent or dies, it is
    restarted with backoff; inotify keeps working regardless, since it is
    a plain fd this thread owns directly, not tied to pw-mon's lifecycle.
    """

    def __init__(
        self,
        callback: Callable[[str], None],
        coalesce: float = PW_MON_COALESCE,
        restart_backoff: float = PW_MON_RESTART_BACKOFF,
        config_dir: str = CONFIG_DIR,
    ):
        self._callback = callback
        self._coalesce = coalesce
        self._restart_backoff = restart_backoff
        self._config_dir = config_dir

        self._stop_event = threading.Event()
        self._stop_r, self._stop_w = os.pipe()
        self._lock = threading.Lock()
        self._pending: set = set()
        self._timer: Optional[threading.Timer] = None

        self._inotify: Optional[_InotifyWatch] = None
        self._pwmon_proc = None
        self._pwmon_next_attempt = 0.0
        self._classifier: Optional[_PwMonClassifier] = None
        self._faillog = _LogLimiter()

        self._thread = threading.Thread(
            target=self._run, name="rp5deck-audio-subscriber", daemon=True
        )

    # -- overridable in tests --
    def _spawn_pwmon(self):
        return subprocess.Popen(
            PW_MON_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )

    def _make_inotify(self):
        return _InotifyWatch.create(self._config_dir)

    def _resolve_ids(self):
        return _resolve_master_ids()

    def start(self) -> None:
        self._thread.start()

    def stop(self, join_timeout: float = 1.0) -> None:
        self._stop_event.set()
        try:
            os.write(self._stop_w, b"x")
        except OSError:
            pass
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._thread.join(timeout=join_timeout)
        self._kill_pwmon()
        if self._inotify is not None:
            self._inotify.close()
            self._inotify = None
        for fd in (self._stop_r, self._stop_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def _kill_pwmon(self) -> None:
        proc, self._pwmon_proc = self._pwmon_proc, None
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=PW_MON_TERM_TIMEOUT)
                except subprocess.TimeoutExpired:
                    # it ignored SIGTERM: do not leave it running (RV1 minor)
                    log.warning("pw-mon ignored SIGTERM for %.1fs; sending SIGKILL",
                                PW_MON_TERM_TIMEOUT)
                    proc.kill()
                    proc.wait(timeout=PW_MON_TERM_TIMEOUT)
            except ProcessLookupError:
                pass                    # already gone
            except Exception:
                self._faillog.exception("kill", "stopping pw-mon failed")
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass                    # closing our end of a dead pipe

    # -- the one background thread --
    def _run(self) -> None:
        # A thread that dies must say so in rp5deck.log (not only on stderr)
        # and must not leave pw-mon running until stop() (RV2-m5).
        try:
            self._loop()
        except Exception:
            log.exception("audio Subscriber thread died: volume changes made outside "
                          "the app are no longer tracked")
        finally:
            self._kill_pwmon()

    def _loop(self) -> None:
        self._inotify = self._make_inotify()
        node_id, device_id = self._resolve_ids()
        self._classifier = _PwMonClassifier(node_id, device_id)

        while not self._stop_event.is_set():
            now = time.monotonic()
            if self._pwmon_proc is None and now >= self._pwmon_next_attempt:
                try:
                    self._pwmon_proc = self._spawn_pwmon()
                    # A restart may follow a sink swap (HDMI <-> HiFi); the
                    # ids are cheap to re-resolve and rarely change.
                    node_id, device_id = self._resolve_ids()
                    self._classifier.node_id = node_id
                    self._classifier.device_id = device_id
                except (FileNotFoundError, OSError) as e:
                    self._pwmon_proc = None
                    self._pwmon_next_attempt = now + self._restart_backoff
                    self._faillog.warning(
                        "spawn", "pw-mon could not start (%s); retrying every %.1fs - "
                        "live volume changes by other apps are not seen meanwhile",
                        e, self._restart_backoff)

            rlist = [self._stop_r]
            if self._inotify is not None:
                rlist.append(self._inotify.fd)
            pwmon_fd = None
            if self._pwmon_proc is not None and self._pwmon_proc.stdout is not None:
                pwmon_fd = self._pwmon_proc.stdout.fileno()
                rlist.append(pwmon_fd)

            # Wake promptly for a pending pw-mon retry rather than sleeping
            # a full second past it - otherwise restart_backoff would only
            # be honoured up to the 1.0s safety-net granularity below.
            select_timeout = 1.0
            if self._pwmon_proc is None:
                select_timeout = max(0.0, min(select_timeout, self._pwmon_next_attempt - now))
            try:
                ready, _, _ = select.select(rlist, [], [], select_timeout)
            except (OSError, ValueError):
                # A genuinely bad fd should not spin this thread at 100%
                # CPU forever - back off briefly and let the top-of-loop
                # stop_event check (not just the stop_r-in-ready branch
                # below) get a chance to end the thread.
                self._faillog.exception("select", "select() failed in the audio Subscriber")
                self._stop_event.wait(0.05)
                ready = []

            if self._stop_r in ready:
                break

            if self._inotify is not None and self._inotify.fd in ready:
                events = self._inotify.read_events()
                if any(_config_rewrite_event(mask, name) for _, mask, _, name in events):
                    self._mark_pending("master")

            if pwmon_fd is not None and pwmon_fd in ready:
                try:
                    chunk = os.read(pwmon_fd, 65536)
                except OSError:
                    self._faillog.exception("pwmon-read", "reading pw-mon failed")
                    chunk = b""
                if not chunk:
                    # pw-mon exited on its own - reap it, back off, retry.
                    self._faillog.warning("pwmon-exit", "pw-mon exited; restarting in %.1fs",
                                          self._restart_backoff)
                    self._kill_pwmon()
                    self._pwmon_next_attempt = time.monotonic() + self._restart_backoff
                else:
                    for kind in self._classifier.feed(chunk.decode("utf-8", "replace")):
                        self._mark_pending(kind)

        self._kill_pwmon()

    def _mark_pending(self, kind: str) -> None:
        with self._lock:
            self._pending.add(kind)
            if self._timer is None:
                self._timer = threading.Timer(self._coalesce, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = set()
            self._timer = None
        for kind in pending:
            try:
                self._callback(kind)
            except Exception:
                # RV5b: this used to vanish without a trace. Rate-limited, so a
                # handler that fails on every change logs once, not per event.
                self._faillog.exception("callback", "audio change callback failed for %r",
                                        kind)
