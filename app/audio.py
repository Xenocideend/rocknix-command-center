#!/usr/bin/env python3
"""Audio for rp5deck (ROCKNIX on the RP5: PipeWire, WirePlumber and pipewire-pulse).

Volume matters most, so this is narrow and careful: every external call has a timeout, and a
failure comes back as state "error" (or None), never as a value that looks like a real quiet
volume. AUDIO-NOTES.md has how each of these was checked on the device.

What this module relies on:

- The volume keys run /usr/bin/input_sense -> /usr/bin/volume, which sets the sink with
  `pactl -- set-sink-volume @DEFAULT_SINK@ N%` and saves N to system.cfg as audio.volume.
  Change it through wpctl only and the next key press starts from the stale saved value and
  jumps. So a live drag can use wpctl, but the value you land on is committed through
  /usr/bin/volume.
- /usr/bin/volume only treats "+"/"up", "-"/"down" and "restore" as special, anything else is
  the new absolute percent (a bad value falls back to 50 inside the script). This always
  passes a plain integer 0-100.
- /usr/bin/volume knows nothing about mute. Mute is wpctl only, never saved, and a volume key
  doesnt touch it.
- The card's HiFi profile gives the Speaker and Headphones sinks. The dual-screen daemon or the
  HDMI switch can remove the sink for a moment (switching to an HDMI profile) and put HiFi
  back. When @DEFAULT_AUDIO_SINK@ resolves to nothing this reports "restoring", not zero.
- `pactl list cards` times out against this pipewire-pulse while wpctl works right away, so
  card and profile state are never read with pactl here.
- `pactl subscribe` and `pactl info` arent used: subscribe delivered no events at all and info
  hung for the full timeout. The shim answers writes but not reads or subscriptions on this
  build. Subscriber watches inotify on system.cfg (the volume keys) and pw-mon (wpctl and
  anything else) instead.
- `wpctl get-volume <id>` can exit 0 with nothing on stdout and an error on stderr, so the
  exit code is never trusted, every parse needs the expected pattern in stdout.
- Playback stream ids from pw-dump come and go (ES's own UI sound stream vanishes within
  seconds), so a vanished id is a normal result, not an error.
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

# timeout for one external call, so a stuck pactl/wpctl/pw-dump can never hang the UI thread
DEFAULT_TIMEOUT = 3.0
PW_DUMP_TIMEOUT = 5.0

# Subscriber settings. /usr/bin/volume writes system.cfg through a temp file (system.cfgXXXXXX)
# and renames it into place.
CONFIG_DIR = "/storage/.config/system/configs"
CONFIG_FILE_NAME = "system.cfg"
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
_INOTIFY_WATCH_MASK = IN_CLOSE_WRITE | IN_MOVED_TO
_INOTIFY_HEADER = struct.Struct("iIII")  # wd, mask, cookie, name-length

PW_MON_CMD = ["pw-mon", "--no-colors"]
# at most one callback per 50-100 ms with a trailing one: fire as soon as a marker line shows
# up, and the pending set plus a one-shot timer fold a burst into one flush
PW_MON_COALESCE = 0.075
PW_MON_RESTART_BACKOFF = 1.0

VOLUME_SCRIPT = "/usr/bin/volume"
DEFAULT_SINK_ALIAS = "@DEFAULT_AUDIO_SINK@"

# wpctl takes values above 1.0 (boost past 100%). A live drag gets a bit of headroom,
# commit_master() clamps harder since /usr/bin/volume only knows 0-100.
MAX_LIVE_VOLUME = 1.5
MAX_COMMIT_VOLUME = 1.0

_VOLUME_LINE_RE = re.compile(r"Volume:\s*([0-9]*\.?[0-9]+)(\s*\[MUTED\])?", re.IGNORECASE)
_INSPECT_HEADER_RE = re.compile(r"^id\s+(\d+)\s*,")
_PROP_LINE_RE = re.compile(r"^\*?\s*([A-Za-z0-9_.]+)\s*=\s*\"?([^\"\n]*)\"?\s*$")


log = logging.getLogger("rp5deck.audio")

# how long pw-mon gets to exit after SIGTERM before SIGKILL
PW_MON_TERM_TIMEOUT = 1.0


class _LogLimiter:
    """Logs a failure (with its traceback inside an except block) the first time a key fails, then
    at most once per `interval` with a count, so a failure on every event cant flood the 512 KB
    log.
    """

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
# Process helpers, the only place that touches subprocess.
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = DEFAULT_TIMEOUT):
    """Runs a command and captures output. Never raises, never hangs.

    Returns (returncode, stdout, stderr). returncode is None if the process never gave a result
    (missing binary, timeout, any OS failure), and callers must treat that as a failure whatever
    stdout says.
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
    """Parses a `wpctl get-volume` line. (volume, muted), or (None, None) without the expected
    pattern.
    """
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
    """Parses `wpctl inspect`'s property dump into a str->str dict. Lines look like
    `* node.name = "foo"` or `  node.pause-on-idle = "false"`.
    """
    props = {}
    for line in out.splitlines():
        line = line.strip()
        m = _PROP_LINE_RE.match(line)
        if m:
            props[m.group(1)] = m.group(2)
    return props


def _sink_label(props: dict) -> Optional[str]:
    """A short sink label from a `wpctl inspect` dump.

    Uses node.description (like "Built-in Audio Speaker Playback", minus "Built-in Audio "). That
    key went missing in at least one capture, so it falls back to matching node.name against the
    two sinks this device has:
      alsa_output ..._HiFi__Speaker__sink     -> "Speaker Playback"
      alsa_output ..._HiFi__Headphones__sink  -> "Headphones Playback"
    Otherwise the raw node name, no guessing.
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
    """The master (default sink) volume, mute and label, as a dict:
        volume:           float, 1.0 == 100%, can go past 1.0 (boost), None if unknown
        muted:            bool or None
        sink_description: short label or None
        state:            "ok"        volume/muted are real current values
                          "restoring" no default sink right now (a profile switch, like to or
                                      from HDMI, which the dual-screen daemon fixes). volume and
                                      muted are None, never show a stale or fake value.
                          "error"     something failed (wpctl missing, PipeWire down).
                                      volume and muted are None.
    """
    result = {"volume": None, "muted": None, "sink_description": None, "state": "error"}

    rc, out, _err = _run(["wpctl", "inspect", DEFAULT_SINK_ALIAS])
    if rc is None:
        return result  # wpctl missing or hung

    header = out.strip().splitlines()[0] if out.strip() else ""
    if rc != 0 or not _INSPECT_HEADER_RE.match(header):
        # the alias resolved to nothing, most likely the speaker sink is gone mid profile switch
        result["state"] = "restoring"
        return result

    props = _inspect_props(out)
    result["sink_description"] = _sink_label(props)

    rc2, out2, _err2 = _run(["wpctl", "get-volume", DEFAULT_SINK_ALIAS])
    if rc2 is None:
        return result  # still "error", but keep the label
    volume, muted = _wpctl_volume_line(out2)
    if volume is None:
        # inspect found a node but get-volume gave nothing parseable, dont invent a number
        return result

    result["volume"] = volume
    result["muted"] = muted
    result["state"] = "ok"
    return result


def set_master(v: float) -> bool:
    """Live volume change while dragging, not saved (see commit_master).

    Clamped to [0, MAX_LIVE_VOLUME]. True if wpctl took it. A False for one dragged frame isnt
    fatal, but dont assume the change happened.
    """
    v = _clamp(float(v), 0.0, MAX_LIVE_VOLUME)
    rc, out, _err = _run(["wpctl", "set-volume", DEFAULT_SINK_ALIAS, "%.4f" % v])
    if rc is None or rc != 0:
        return False
    return True


def commit_master(v: float) -> bool:
    """Saves a volume the way the hardware keys do, through /usr/bin/volume (sets the sink with pactl
    and saves audio.volume), so the next key press doesnt jump off a stale value.

    v uses the same units as the rest of this module (1.0 == 100%), clamped to
    [0, MAX_COMMIT_VOLUME] since the script only takes an integer 0-100. True on apparent success.
    """
    v = _clamp(float(v), 0.0, MAX_COMMIT_VOLUME)
    percent = int(round(v * 100))
    rc, _out, _err = _run([VOLUME_SCRIPT, str(percent)])
    if rc is None or rc != 0:
        return False
    return True


def toggle_master_mute() -> bool:
    """Toggles mute on the default sink. ROCKNIX doesnt save it (it only lives in PipeWire), and a
    volume key doesnt clear it, so the UI should show it clearly.
    """
    rc, _out, _err = _run(["wpctl", "set-mute", DEFAULT_SINK_ALIAS, "toggle"])
    return rc == 0


# --------------------------------------------------------------------------
# Per-app streams
# --------------------------------------------------------------------------

def list_streams() -> Optional[list]:
    """The current playback streams (pw-dump nodes with media.class "Stream/Output/Audio"), each as
    {id, app_name, media_name, node_name, volume, muted, display_name}.

    volume is the cube root of the loudest raw channelVolumes value, which matches wpctl's scale
    for both values seen here (0.000125 <-> 0.05 and 1.0 <-> 1.00). A stream without readable
    Props gets None, not 0/False.

    None (not []) if pw-dump couldnt be read at all. [] means nothing is playing, which is the
    common case, so the UI has to tell the two apart.
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
    """Sets one stream's volume, clamped to [0, MAX_LIVE_VOLUME]. False (not an exception) if the id
    is already gone.
    """
    v = _clamp(float(v), 0.0, MAX_LIVE_VOLUME)
    rc, out, _err = _run(["wpctl", "set-volume", str(stream_id), "%.4f" % v])
    if rc is None or rc != 0:
        return False
    return True


def toggle_stream_mute(stream_id: int) -> bool:
    """Toggles mute on one stream. False if the id is gone or wpctl failed."""
    rc, _out, _err = _run(["wpctl", "set-mute", str(stream_id), "toggle"])
    return rc == 0


# --------------------------------------------------------------------------
# Event subscriber
#
# Two sources feed the same callback (pactl subscribe doesnt work here, see the module
# docstring):
#
# 1. inotify on CONFIG_DIR for CONFIG_FILE_NAME. /usr/bin/volume (the hardware keys) writes a
#    temp system.cfgXXXXXX and renames it onto system.cfg, so the event we want is
#    IN_MOVED_TO/IN_CLOSE_WRITE named exactly system.cfg, the temp name must never fire.
# 2. `pw-mon --no-colors` for changes that skip /usr/bin/volume (our own live wpctl drag, or
#    another app). The sink's volume here is a hardware ALSA mixer route
#    (route.hw-volume = "true"), so pw-mon only reports the live volume and mute on the ALSA
#    Device's Route param, the sink Node only ever reports PropInfo. Both ids come from
#    `wpctl inspect` and both are watched, either one firing is fine since the callback only
#    means "read get_master() again".
#
# Real captures: AUDIO-NOTES.md and tests/fixtures/pw-mon-*.
# --------------------------------------------------------------------------


def _parse_inotify_events(buf: bytes) -> list:
    """Parses a raw read() of an inotify fd into (wd, mask, cookie, name) tuples (name "" when there
    isnt one). A truncated trailing event is dropped, the next read() has it whole.
    """
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
    """True for the rename onto system.cfg itself, never one of /usr/bin/volume's system.cfgXXXXXX
    temp files (the name has to be exactly CONFIG_FILE_NAME).
    """
    return name == CONFIG_FILE_NAME and bool(mask & _INOTIFY_WATCH_MASK)


_LIBC = None


def _inotify_libc():
    """libc with the two inotify calls' signatures pinned, so ctypes doesnt have to guess from the
    Python values.
    """
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
    """ctypes wrapper around inotify_init1/inotify_add_watch. create() never raises, it returns None
    if inotify isnt there (not Linux, or the calls fail) so Subscriber can run on pw-mon alone.
    The fd is O_CLOEXEC so no child inherits it.
    """

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
        """non-blocking, only call once select() says the fd is readable"""
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
    """The default sink's own node id and its ALSA Device id (device.id from the same
    `wpctl inspect` dump). Returns (node_id, device_id), either can be None (mid HDMI switch, or no
    wpctl).
    """
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
# Props:volume / Props:mute / Props:channelVolumes but not volumeBase / volumeStep (the \b stops
# at a word boundary and those two keep going with a word character)
_VALUE_MARKER_RE = re.compile(r"Props:(?:volume|mute|channelVolumes)\b")


class _PwMonClassifier:
    """Line-based classifier for `pw-mon --no-colors` output. feed() takes raw text as it comes
    (partial lines are fine) and returns the "master" kinds to fire for that chunk.

    Only a "changed:" block for the watched node or device id that has a real Props value line
    fires. That skips pw-mon's startup dump for free, since the dump is all "added:" blocks and
    only a live change is ever "changed:".
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
    """Background thread: inotify on system.cfg (the hardware keys through /usr/bin/volume) plus
    pw-mon (a live wpctl set-volume, like our own slider, or another app). Calls
    callback("master") at most once per ~75 ms burst without missing the last value. The callback
    only means "read get_master() again", neither source's parsed content is passed on.

    One select() based daemon thread, it never blocks the caller. stop() wakes it with a
    self-pipe, joins within about 1 s and kills pw-mon (no zombies). A missing or dead pw-mon is
    restarted with backoff, inotify keeps working either way since its a plain fd this thread
    owns.
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

    # -- tests can swap these --
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
                    # it ignored SIGTERM, dont leave it running
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
        # A thread that dies has to say so in rp5deck.log (not only stderr) and must not leave pw-mon
        # running until stop().
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
                    # a restart can follow a sink swap (HDMI <-> HiFi), the ids are cheap to look up again
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

            # wake up for a pending pw-mon retry instead of sleeping a full second past it, otherwise
            # restart_backoff would only be honoured to the 1 s safety net below
            select_timeout = 1.0
            if self._pwmon_proc is None:
                select_timeout = max(0.0, min(select_timeout, self._pwmon_next_attempt - now))
            try:
                ready, _, _ = select.select(rlist, [], [], select_timeout)
            except (OSError, ValueError):
                # a bad fd shouldnt spin this thread at 100% CPU forever, back off briefly so the stop check at
                # the top of the loop gets a chance to end it
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
                    # pw-mon exited by itself, reap it, back off and retry
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
                # rate limited, so a handler that fails on every change logs once, not every event
                self._faillog.exception("callback", "audio change callback failed for %r",
                                        kind)
