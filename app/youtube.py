#!/usr/bin/env python3
"""
YouTube backend for rp5deck (Retroid Pocket 5 / ROCKNIX / mpv + yt-dlp + Deno).

Verified on a real RP5, ROCKNIX 20260923, per YOUTUBE-NOTES.md:

- yt-dlp needs an external JavaScript runtime for full YouTube support; Deno
  is the one this module points it at, via `--js-runtimes deno:<path>` for
  standalone search calls and `ytdl_hook-ytdl_raw_options=js-runtimes=deno:
  <path>` when mpv's built-in ytdl hook does the resolving.
- Playback always goes through mpv's ytdl hook (never a Python-side resolve
  of the googlevideo URL): the hook keeps mpv, yt-dlp and the CDN URL's
  matching request context together in one process. A one-off "HTTP error
  403" was seen resolving a raw URL by hand; the ytdl-hook path was 3/3
  clean in repro runs (see notes).
- Every external call has a timeout: search() and fetch_thumbnail() must
  never block the caller forever if yt-dlp, Deno or the network wedge.
- A dead mpv is reported, not left as a zombie: Player.is_alive() checks the
  actual process, get_status() reflects a dead process as an explicit
  "mpv not running" error rather than stale/fake numbers, and stop() escalates
  from SIGTERM to SIGKILL with a wait() so no child is ever abandoned.
- RP5DECK_YT_HEADLESS=1 adds --vo=null --ao=null so tests (and this module's
  own on-device end-to-end check) never open a window or make sound while
  another agent owns the display and the owner is listening.
- Owner request 2 (test day, 24 Sep): tapping the video pauses it. mpv itself
  binds the tap (--input-conf=youtube-input.conf, MBTN_LEFT -> cycle pause;
  --native-touch=no so a touch arrives as MBTN_LEFT) rather than rp5deck
  reading the tap and calling toggle_pause() over IPC - simpler, and it keeps
  working even if rp5deck's own poll loop is behind. No MBTN_LEFT_DBL binding
  exists, so a double tap never reaches mpv's normal "cycle fullscreen" (a
  fullscreen mpv would cover rp5deck's strip). UNVERIFIED on the device (PC/
  WSL only, per the PC-only constraint) - see W-NOTES.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Optional

# YT2 (Phase 1, research/YT1-youtube-full-design.md): everything
# cookie-gated (subscriptions/history feeds, resume-position store)
# lives in yt_feeds.py, never here - this stays the plain
# mpv/Player/search module. Import guarded so youtube.py keeps
# working standalone (own tests, a partial deployment) even if
# yt_feeds.py is ever missing - every call site below treats
# _yt_feeds is None as "feature not available", not an error.
try:
    import yt_feeds as _yt_feeds
except ImportError:
    _yt_feeds = None

# --------------------------------------------------------------------------
# Tunables / device paths
# --------------------------------------------------------------------------

# Where STEP 1 downloaded and verified the binaries on the device.
YTDLP_PATH = "/storage/rp5deck-bin/yt-dlp"
DENO_PATH = "/storage/rp5deck-bin/deno"
MPV_BIN = "mpv"

# Owner request 2 (test day, 24 Sep): "tapping the video should pause it."
# Shipped alongside youtube.py (not the owner's own mpv config - see
# build_command) so it deploys with the rest of rp5deck wherever that lands.
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_CONF_PATH = os.path.join(HERE, "youtube-input.conf")

SEARCH_TIMEOUT = 20.0   # yt-dlp ytsearch + deno JS eval measured ~3.5s; generous margin
THUMB_TIMEOUT = 10.0
IPC_TIMEOUT = 3.0
SOCKET_WAIT_TIMEOUT = 10.0
STOP_WAIT_TIMEOUT = 3.0
RESUME_SEEK_WAIT_TIMEOUT = 5.0  # YT2: bound on waiting for mpv to report seekable

DEFAULT_IPC_SOCKET = "/run/rp5deck/mpv-yt.sock"

# Prefer avc1 (H.264): mpv --hwdec=help on this device lists h264_v4l2m2m AND
# h264-vulkan, the two hwdec paths that actually exist here (av01/vp9 only
# have a vulkan path, and that vulkan hwaccel failed to even set up under
# --vo=null in testing - see YOUTUBE-NOTES.md). Fall back to any codec at
# <=1080p, then to a single progressive stream, rather than failing outright.
DEFAULT_FORMAT = (
    "bv*[vcodec^=avc1][height<=1080]+ba/"
    "bv*[height<=1080]+ba/"
    "b[height<=1080]"
)


# --------------------------------------------------------------------------
# search()
# --------------------------------------------------------------------------

def _run_yt_dlp(args: list[str], timeout: float):
    """Run yt-dlp as `python3 <zipapp> ...`. Never raises, never hangs.

    Returns (returncode, stdout, stderr). returncode is None if the process
    never produced a result (missing interpreter/zipapp, timeout, or any
    other OS-level failure) - callers must treat that as a hard failure.
    """
    cmd = ["python3", YTDLP_PATH] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None, "", "exec failed or timed out: %s" % " ".join(cmd)
    return proc.returncode, proc.stdout, proc.stderr


def search(query: str, n: int = 20) -> list[dict]:
    """Search YouTube via `yt-dlp ytsearchN:<query> --flat-playlist -J`.

    Returns a list of {id, title, channel, duration_s, thumbnail_url} in
    result order. Returns [] (never raises, never hangs) if yt-dlp is
    missing, times out, or produces anything that doesn't parse as the
    expected JSON shape.
    """
    return search_detailed(query, n) or []


def search_detailed(query: str, n: int = 20) -> Optional[list[dict]]:
    """Like search(), but a failure (yt-dlp missing, timed out, nonzero
    exit, unparseable output) is None, and only a search that really ran and
    found nothing is []. The UI says "Search failed" for one and "No
    results" for the other (HF1)."""
    if not query or n <= 0:
        return []

    search_expr = "ytsearch%d:%s" % (n, query)
    rc, out, _err = _run_yt_dlp(
        ["--js-runtimes", "deno:%s" % DENO_PATH, search_expr,
         "--flat-playlist", "-J"],
        timeout=SEARCH_TIMEOUT,
    )
    if rc is None or rc != 0 or not out.strip():
        return None

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None

    results = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        thumbnails = e.get("thumbnails")
        thumbnail_url = None
        if isinstance(thumbnails, list) and thumbnails:
            # yt-dlp lists thumbnails smallest-first; the last one is the
            # highest resolution available.
            last = thumbnails[-1]
            if isinstance(last, dict):
                thumbnail_url = last.get("url")
        results.append({
            "id": e.get("id"),
            "title": e.get("title"),
            "channel": e.get("channel"),
            "duration_s": e.get("duration"),
            "thumbnail_url": thumbnail_url,
        })
    return results


# --------------------------------------------------------------------------
# YT2 entry points: cookie-gated feeds (subscriptions/history), for the
# YouTube view's "Subscriptions"/"History" buttons. Same contract as
# search_detailed() (None = failed, [] = empty, a list = ok) - everything
# cookie-gated actually lives in yt_feeds.py; these are thin delegates so
# this module stays the one youtube.py callers already import from. See
# patches/YT2-NOTES.md for where the buttons themselves should be added
# (screens.py's YouTubeSheet - out of this patch's scope).
# --------------------------------------------------------------------------

def subscriptions_detailed(n: int = 20, cfg: Optional[dict] = None) -> Optional[list[dict]]:
    if _yt_feeds is None:
        return None
    return _yt_feeds.subscriptions(n, cfg=cfg)


def history_detailed(n: int = 20, cfg: Optional[dict] = None) -> Optional[list[dict]]:
    if _yt_feeds is None:
        return None
    return _yt_feeds.history(n, cfg=cfg)


# --------------------------------------------------------------------------
# fetch_thumbnail()
# --------------------------------------------------------------------------

def fetch_thumbnail(url: str, cache_dir: str, timeout: float = THUMB_TIMEOUT) -> Optional[str]:
    """Download `url` into `cache_dir`, keyed by a hash of the URL so repeat
    calls are cache hits. Returns the local path, or None on any failure
    (never raises).
    """
    if not url:
        return None

    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return None

    parsed_path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(parsed_path)[1]
    if not ext or len(ext) > 5 or "/" in ext:
        ext = ".jpg"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    local_path = os.path.join(cache_dir, digest + ext)

    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception:
        return None

    if not data:
        return None

    tmp_path = local_path + ".tmp-%d" % os.getpid()
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, local_path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None
    return local_path


# --------------------------------------------------------------------------
# Player
# --------------------------------------------------------------------------

class Player:
    """Drives one mpv instance over its JSON IPC socket.

    mpv is launched with `--idle=yes`, so a single instance survives across
    play() calls: the first play() launches mpv with the video as its
    initial file, later play() calls reuse the same process via a
    `loadfile ... replace` IPC command. If the running mpv or its socket is
    unhealthy, play() falls back to a fresh restart rather than failing.

    Nothing here ever touches a real window/speaker unless
    RP5DECK_YT_HEADLESS is unset: with RP5DECK_YT_HEADLESS=1, --vo=null
    --ao=null are added so tests are silent and headless.
    """

    def __init__(
        self,
        ipc_socket: str = DEFAULT_IPC_SOCKET,
        yt_dlp_path: str = YTDLP_PATH,
        deno_path: str = DENO_PATH,
        mpv_bin: str = MPV_BIN,
        format_str: str = DEFAULT_FORMAT,
        extra_mpv_args: Optional[list[str]] = None,
    ):
        self.ipc_socket = ipc_socket
        self.yt_dlp_path = yt_dlp_path
        self.deno_path = deno_path
        self.mpv_bin = mpv_bin
        self.format_str = format_str
        self.extra_mpv_args = list(extra_mpv_args or [])

        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._req_id = 0
        self._current_video_id: Optional[str] = None   # YT2: for resume-on-play/record-on-stop

    # -- command line -------------------------------------------------

    def _headless(self) -> bool:
        return os.environ.get("RP5DECK_YT_HEADLESS") == "1"

    def build_command(self, url: Optional[str] = None) -> list[str]:
        """Build the mpv argv. Exposed (not just used internally) so tests
        can check the exact flags without launching a real process."""
        raw_opts = "js-runtimes=deno:%s,format=%s" % (self.deno_path, self.format_str)
        script_opts = "ytdl_hook-ytdl_path=%s,ytdl_hook-ytdl_raw_options=%s" % (
            self.yt_dlp_path, raw_opts,
        )
        # HF1 (the window must leave rp5deck's BAR strip visible and usable):
        #  - NOT --fullscreen: sway draws fullscreen windows ABOVE the layer
        #    shell's TOP layer (where rp5deck lives) and ignores exclusive
        #    zones for them, so a fullscreen mpv would hide the transport
        #    strip. A plain tiled window fills DSI-1 above the strip.
        #  - --force-window=immediate: the window maps at once (rp5deck goes
        #    BAR while yt-dlp resolves) and stays across `loadfile replace`.
        #  - --keep-open=yes: at the end of a video mpv pauses on the last
        #    frame instead of going idle and dropping its window.
        #  - --input-default-bindings=no, --no-config: no bindings at all
        #    except our own --input-conf (below) - not mpv's stock keymap,
        #    not the owner's own ~/.config/mpv/input.conf.
        #  - --input-conf=INPUT_CONF_PATH (owner request 2): one binding,
        #    MBTN_LEFT -> cycle pause, so tapping the video pauses it; no
        #    MBTN_LEFT_DBL binding at all, so a double tap never reaches
        #    mpv's normal "cycle fullscreen" - a fullscreen mpv is drawn
        #    ABOVE rp5deck's own layer-shell strip (see the top-layer note
        #    above) and would hide it. Everything else (seek, the strip's own
        #    Pause button) still goes through the JSON IPC.
        #  - --native-touch=no: a tap on the touchscreen arrives as a plain
        #    MBTN_LEFT click (what the input.conf binding above matches)
        #    instead of a native touch event mpv would otherwise handle on
        #    its own (e.g. for pinch/pan), which the binding cannot see.
        #  - --input-vo-keyboard=no: no keyboard shortcuts either.
        cmd = [
            self.mpv_bin,
            "--idle=yes",
            "--no-config",
            "--border=no",
            "--osc=no",                          # no on-screen controller to invite touches
            "--force-window=immediate",
            "--keep-open=yes",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--native-touch=no",
            "--input-conf=%s" % INPUT_CONF_PATH,
            "--wayland-app-id=rp5deck-yt",
            "--audio-client-name=YouTube",        # per-app PipeWire/Pulse mixer shows "YouTube"
            "--input-ipc-server=%s" % self.ipc_socket,
            "--ytdl=yes",
            "--script-opts=%s" % script_opts,
        ]
        if self._headless():
            cmd += ["--vo=null", "--ao=null"]
        cmd += self.extra_mpv_args
        if url:
            cmd.append(url)
        return cmd

    # -- process lifecycle ---------------------------------------------

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def pid(self) -> Optional[int]:
        """mpv's process id while it runs, else None."""
        return self._proc.pid if self.is_alive() else None

    def terminate_now(self) -> None:
        """SIGTERM mpv at once, from any thread (one kill(2), no socket): a
        Close must not wait behind play()'s up-to-10 s socket wait. stop(),
        run afterwards on the control thread, cleans up the rest."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _ensure_socket_dir(self) -> None:
        d = os.path.dirname(self.ipc_socket)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass

    def _connect_socket(self, timeout: float = SOCKET_WAIT_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False  # mpv exited before the socket ever appeared
            if os.path.exists(self.ipc_socket):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(IPC_TIMEOUT)
                    s.connect(self.ipc_socket)
                    self._sock = s
                    return True
                except OSError:
                    pass
            time.sleep(0.1)
        return False

    def play(self, video_id: str) -> bool:
        """Play a YouTube video id. Reuses a live mpv via IPC `loadfile` if
        one is already running and healthy; otherwise (re)launches mpv fresh
        with the video as its initial file. Returns True if playback was
        (re)started, False if mpv could not be launched or never exposed a
        connectable IPC socket."""
        url = "https://www.youtube.com/watch?v=%s" % video_id
        self._record_current_position()          # YT2: persist whatever was playing before this call

        if self.is_alive() and self._sock is not None:
            resp = self._send_ipc(["loadfile", url, "replace"], timeout=5.0)
            if resp is not None and resp.get("error") == "success":
                self._current_video_id = video_id
                self._maybe_resume(video_id)      # YT2
                return True
            # The running instance or its socket is unhealthy - fall through
            # to a full restart rather than leaving the caller stuck.

        self.stop()
        self._ensure_socket_dir()
        try:
            if os.path.exists(self.ipc_socket):
                os.remove(self.ipc_socket)
        except OSError:
            pass

        cmd = self.build_command(url)
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._proc = None
            return False

        self._current_video_id = video_id
        ok = self._connect_socket()
        if ok:
            self._maybe_resume(video_id)          # YT2
        return ok

    def stop(self) -> None:
        """Terminate mpv if running. Always leaves no process behind:
        escalates SIGTERM -> SIGKILL with a bounded wait either way, so a
        wedged mpv is never left as a zombie."""
        self._record_current_position()          # YT2: last chance before mpv goes away
        if self._sock is not None:
            try:
                self._send_ipc(["quit"], timeout=1.0)
            except Exception:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=STOP_WAIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=STOP_WAIT_TIMEOUT)
                except Exception:
                    pass
            except Exception:
                pass

        try:
            if os.path.exists(self.ipc_socket):
                os.remove(self.ipc_socket)
        except OSError:
            pass
        self._current_video_id = None             # YT2: nothing to resume into until the next play()

    # -- YT2: resume-on-play / record-on-stop ------------------------------

    def _record_current_position(self) -> None:
        """Best-effort write-through of the outgoing video's last known
        position, via yt_feeds.record(). Never raises and never blocks
        longer than one get_status() IPC round-trip - a lookup/record
        failure here must never break play()/stop() themselves."""
        if _yt_feeds is None or not self._current_video_id or not self.is_alive():
            return
        try:
            status = self.get_status()
            pos = status.get("time_pos")
            if pos is not None:
                _yt_feeds.record(self._current_video_id, pos, status.get("duration"))
        except Exception:
            pass

    def _maybe_resume(self, video_id: str) -> None:
        """After a successful play(), seek to the last recorded position
        for this video if there is one worth resuming (yt_feeds.resume_for()
        already filters "too close to the start" / "basically finished").
        Waits briefly for mpv to report the file seekable - a seek sent
        before that is silently lost - bounded by
        RESUME_SEEK_WAIT_TIMEOUT so a slow-to-load stream can never hang
        play(). Best-effort only: by the time this runs play() has already
        returned True, so any failure here must never undo that."""
        if _yt_feeds is None:
            return
        try:
            pos = _yt_feeds.resume_for(video_id)
        except Exception:
            return
        if not pos:
            return
        deadline = time.time() + RESUME_SEEK_WAIT_TIMEOUT
        while time.time() < deadline:
            if self._get_property("seekable"):
                self.seek(pos, "absolute")
                return
            time.sleep(0.1)

    # -- IPC --------------------------------------------------------------

    def _send_ipc(self, command: list, timeout: float = IPC_TIMEOUT) -> Optional[dict]:
        """Send one mpv IPC command, return the matching reply dict, or None
        on any failure (no socket, send error, timeout, or a reply that
        never arrives). Never raises."""
        if self._sock is None:
            return None

        self._req_id += 1
        rid = self._req_id
        payload = json.dumps({"command": command, "request_id": rid}) + "\n"
        try:
            self._sock.sendall(payload.encode("utf-8"))
        except OSError:
            self._sock = None
            return None

        self._sock.settimeout(timeout)
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                break
            except OSError:
                self._sock = None
                return None
            if not chunk:
                self._sock = None
                return None
            buf += chunk
            for line in buf.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("request_id") == rid:
                    return obj
        return None

    def _get_property(self, name: str):
        resp = self._send_ipc(["get_property", name])
        if resp is None or resp.get("error") != "success":
            return None
        return resp.get("data")

    # -- controls -----------------------------------------------------

    def toggle_pause(self) -> bool:
        resp = self._send_ipc(["cycle", "pause"])
        return bool(resp and resp.get("error") == "success")

    def seek(self, sec: float, mode: str = "absolute") -> bool:
        resp = self._send_ipc(["seek", sec, mode])
        return bool(resp and resp.get("error") == "success")

    def set_volume(self, pct: float) -> bool:
        """mpv's own internal volume (0-150, softvol-style), independent of
        the system per-app mixer that audio.py drives."""
        pct = max(0.0, min(150.0, float(pct)))
        resp = self._send_ipc(["set_property", "volume", pct])
        return bool(resp and resp.get("error") == "success")

    def get_status(self) -> dict:
        """Returns {title, time_pos, duration, paused}. If mpv is not
        running, all four are None and an "error" key explains why - never a
        stale or fake-looking value."""
        if not self.is_alive():
            return {
                "title": None, "time_pos": None, "duration": None,
                "paused": None, "error": "mpv not running",
            }
        return {
            "title": self._get_property("media-title"),
            "time_pos": self._get_property("time-pos"),
            "duration": self._get_property("duration"),
            "paused": self._get_property("pause"),
        }
