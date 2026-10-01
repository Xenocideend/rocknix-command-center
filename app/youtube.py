#!/usr/bin/env python3
"""YouTube backend for rp5deck (mpv + yt-dlp + Deno), checked on the RP5, see YOUTUBE-NOTES.md.

yt-dlp needs an outside JavaScript runtime for full YouTube support. This points it at Deno
with `--js-runtimes deno:<path>` for search calls and `ytdl_hook-ytdl_raw_options=js-runtimes=
deno:<path>` when mpv's ytdl hook does the resolving.

Playback always goes through mpv's ytdl hook, never a googlevideo URL resolved in Python. The
hook keeps mpv, yt-dlp and the CDN URL's request context together in one process. Resolving
a raw URL by hand hit an HTTP 403 once, and the hook path was clean 3 of 3 times.

Every outside call has a timeout, so search() and fetch_thumbnail() never block forever if
yt-dlp, Deno or the network hang.

A dead mpv gets reported, not left as a zombie. Player.is_alive() checks the real process,
get_status() gives an explicit "mpv not running" error instead of stale numbers, and stop()
goes from SIGTERM to SIGKILL with a wait() so no child gets abandoned.

RP5DECK_YT_HEADLESS=1 adds --vo=null --ao=null so tests never open a window or make sound.

Tapping the video pauses it. mpv binds the tap itself (--input-conf=youtube-input.conf,
MBTN_LEFT -> cycle pause, with --native-touch=no so a touch arrives as MBTN_LEFT) instead of
rp5deck reading it and calling toggle_pause() over IPC, which is simpler and still works if
rp5deck's loop is behind. There's no MBTN_LEFT_DBL binding, so a double tap never hits mpv's
"cycle fullscreen" (a fullscreen mpv would cover the strip). Only tried on the PC so far,
see W-NOTES.md.
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

# Everything that needs cookies (subscription and history feeds, the resume position store)
# lives in yt_feeds.py, this stays the plain mpv/Player/search module. The import is guarded
# so youtube.py still works on its own if yt_feeds.py is missing, and every caller below
# treats _yt_feeds being None as the feature not being there, not an error.
try:
    import yt_feeds as _yt_feeds
except ImportError:
    _yt_feeds = None

# --------------------------------------------------------------------------
# Settings and device paths
# --------------------------------------------------------------------------

# where the binaries were downloaded and checked on the device
YTDLP_PATH = "/storage/rp5deck-bin/yt-dlp"
DENO_PATH = "/storage/rp5deck-bin/deno"
MPV_BIN = "mpv"

# Tapping the video pauses it. This ships with youtube.py (not in your own mpv config, see
# build_command) so it deploys with the rest of rp5deck.
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_CONF_PATH = os.path.join(HERE, "youtube-input.conf")

SEARCH_TIMEOUT = 20.0  # yt-dlp ytsearch + deno JS eval took about 3.5 s, plenty of margin here
THUMB_TIMEOUT = 10.0
IPC_TIMEOUT = 3.0
SOCKET_WAIT_TIMEOUT = 10.0
STOP_WAIT_TIMEOUT = 3.0
RESUME_SEEK_WAIT_TIMEOUT = 5.0  # how long to wait for mpv to report seekable

DEFAULT_IPC_SOCKET = "/run/rp5deck/mpv-yt.sock"

# Prefer avc1 (H.264). mpv --hwdec=help here lists h264_v4l2m2m and h264-vulkan, the two hwdec
# paths that exist (av01/vp9 only have vulkan, and that vulkan hwaccel wouldnt even set up
# under --vo=null, see YOUTUBE-NOTES.md). Falls back to any codec at 1080p or less, then a
# single progressive stream, instead of failing.
DEFAULT_FORMAT = (
    "bv*[vcodec^=avc1][height<=1080]+ba/"
    "bv*[height<=1080]+ba/"
    "b[height<=1080]"
)


# --------------------------------------------------------------------------
# search()
# --------------------------------------------------------------------------

def _run_yt_dlp(args: list[str], timeout: float):
    """Runs yt-dlp as `python3 <zipapp> ...`. Never raises, never hangs.

    Returns (returncode, stdout, stderr). returncode is None if the process never gave a result
    (missing interpreter or zipapp, timeout, any OS failure), and callers must treat that as a
    failure.
    """
    cmd = ["python3", YTDLP_PATH] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None, "", "exec failed or timed out: %s" % " ".join(cmd)
    return proc.returncode, proc.stdout, proc.stderr


def search(query: str, n: int = 20) -> list[dict]:
    """Searches YouTube with `yt-dlp ytsearchN:<query> --flat-playlist -J`.

    Returns a list of {id, title, channel, duration_s, thumbnail_url} in result order, or [] if
    yt-dlp is missing, times out or gives anything that doesnt parse as the expected JSON.
    Never raises or hangs.
    """
    return search_detailed(query, n) or []


def search_detailed(query: str, n: int = 20) -> Optional[list[dict]]:
    """Like search(), but a failure (yt-dlp missing, timed out, bad exit, unparseable output) is
    None and only a search that ran and found nothing is []. The UI says "Search failed" for one
    and "No results" for the other.
    """
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
            # yt-dlp lists thumbnails smallest first, so the last one is the biggest
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
# Cookie feeds (subscriptions, history) for the YouTube view's buttons. Same contract as
# search_detailed() (None = failed, [] = empty, a list = ok). The real work is in yt_feeds.py,
# these just pass through so callers keep importing from here. patches/YT2-NOTES.md has
# where the buttons would go in screens.py.
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
    """Downloads `url` into `cache_dir`, named by a hash of the URL so repeat calls are cache hits.
    Returns the local path, or None on any failure (never raises).
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
    """Drives one mpv over its JSON IPC socket.

    mpv runs with `--idle=yes` so one instance lasts across play() calls. The first play()
    launches mpv with the video as its first file, later ones reuse it with `loadfile ...
    replace`. If the running mpv or its socket is unhealthy, play() restarts it fresh instead of
    failing.

    Nothing touches a real window or speaker when RP5DECK_YT_HEADLESS=1, that adds --vo=null
    --ao=null so tests are silent.
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
        self._current_video_id: Optional[str] = None  # for resume on play and record on stop

    # -- command line -------------------------------------------------

    def _headless(self) -> bool:
        return os.environ.get("RP5DECK_YT_HEADLESS") == "1"

    def build_command(self, url: Optional[str] = None) -> list[str]:
        """Builds the mpv argv. Public so tests can check the exact flags without starting mpv."""
        raw_opts = "js-runtimes=deno:%s,format=%s" % (self.deno_path, self.format_str)
        script_opts = "ytdl_hook-ytdl_path=%s,ytdl_hook-ytdl_raw_options=%s" % (
            self.yt_dlp_path, raw_opts,
        )
        # The window has to leave rp5deck's BAR strip visible and usable:
        # not --fullscreen, sway draws fullscreen windows above the layer shell's TOP layer (where
        # rp5deck is) and ignores exclusive zones for them, so a fullscreen mpv hides the strip. A
        # plain tiled window fills DSI-1 above the strip.
        # --force-window=immediate maps the window right away (rp5deck goes BAR while yt-dlp
        # resolves) and keeps it across `loadfile replace`.
        # --keep-open=yes pauses on the last frame at the end instead of going idle and dropping the
        # window.
        # --input-default-bindings=no and --no-config: no bindings but our own --input-conf, not mpv's
        # stock keys and not your ~/.config/mpv/input.conf.
        # --input-conf=INPUT_CONF_PATH has one binding, MBTN_LEFT -> cycle pause. No MBTN_LEFT_DBL, so
        # a double tap never reaches "cycle fullscreen". Seek and the strip's own Pause button go
        # through the JSON IPC.
        # --native-touch=no makes a tap arrive as a plain MBTN_LEFT click, which the binding sees,
        # instead of a native touch mpv handles itself (pinch, pan).
        # --input-vo-keyboard=no, no keyboard shortcuts either.
        cmd = [
            self.mpv_bin,
            "--idle=yes",
            "--no-config",
            "--border=no",
            "--osc=no",  # no on-screen controller inviting touches
            "--force-window=immediate",
            "--keep-open=yes",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--native-touch=no",
            "--input-conf=%s" % INPUT_CONF_PATH,
            "--wayland-app-id=rp5deck-yt",
            "--audio-client-name=YouTube",  # the per-app mixer shows "YouTube"
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
        """mpv's pid while it runs, else None."""
        return self._proc.pid if self.is_alive() else None

    def terminate_now(self) -> None:
        """SIGTERMs mpv right away from any thread (one kill(2), no socket), so a Close doesnt wait
        behind play()'s socket wait of up to 10 s. stop() on the control thread cleans up after.
        """
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
        """Plays a YouTube video id. Reuses a healthy running mpv with IPC `loadfile`, otherwise starts
        mpv fresh with the video as its first file. True if playback (re)started, False if mpv
        couldnt start or never opened an IPC socket.
        """
        url = "https://www.youtube.com/watch?v=%s" % video_id
        self._record_current_position()  # save whatever was playing before this call

        if self.is_alive() and self._sock is not None:
            resp = self._send_ipc(["loadfile", url, "replace"], timeout=5.0)
            if resp is not None and resp.get("error") == "success":
                self._current_video_id = video_id
                self._maybe_resume(video_id)
                return True
            # the running instance or its socket is unhealthy, fall through to a full restart instead of
            # leaving the caller stuck

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
            self._maybe_resume(video_id)
        return ok

    def stop(self) -> None:
        """Stops mpv if running and never leaves a process behind: SIGTERM then SIGKILL with a bounded
        wait, so a hung mpv never becomes a zombie.
        """
        self._record_current_position()  # last chance before mpv goes away
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
        self._current_video_id = None  # nothing to resume into until the next play()

    # -- resume on play / record on stop -----------------------------------

    def _record_current_position(self) -> None:
        """Saves the outgoing video's last position through yt_feeds.record() if it can. Never raises
        and never blocks longer than one get_status() round trip, a failure here must not break
        play()/stop().
        """
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
        """After play() works, seeks to the last saved position for this video if there's one worth
        resuming (yt_feeds.resume_for() already skips too close to the start or basically done).
        Waits a moment for mpv to say the file is seekable, since a seek sent before that just gets
        lost, capped by RESUME_SEEK_WAIT_TIMEOUT so a slow stream cant hang play(). play() already
        returned True by now, so a failure here never undoes that.
        """
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
        """Sends one mpv IPC command and returns the matching reply dict, or None on any failure (no
        socket, send error, timeout, no reply). Never raises.
        """
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
        """mpv's own volume (0-150, softvol style), separate from the per-app mixer audio.py drives."""
        pct = max(0.0, min(150.0, float(pct)))
        resp = self._send_ipc(["set_property", "volume", pct])
        return bool(resp and resp.get("error") == "success")

    def get_status(self) -> dict:
        """Returns {title, time_pos, duration, paused}. If mpv isnt running all four are None and an
        "error" key says why, never a stale or fake value.
        """
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
