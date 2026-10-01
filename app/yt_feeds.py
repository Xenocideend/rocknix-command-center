#!/usr/bin/env python3
"""yt_feeds: the YouTube feeds that need your sign-in cookies (subscriptions, history) and the
resume position store.

Everything cookie related lives here so youtube.py stays the plain mpv/Player/search module.
browser.py and config.py are only read here (PROFILE_DIR, get_value()/config_path()), never
written.

rp5deck never sees, stores or types your YouTube or Google password. This reads Firefox's
own cookie database through yt-dlp's `--cookies-from-browser firefox:<profile>`, which copies
cookies.sqlite into a temp folder before opening it (so it works while Firefox runs) and never
writes into the profile. Signing in is something you do yourself in the Browser tile, there's
no sign-in UI here, only a check of whether it happened.

yt-dlp's stderr could echo a raw `Cookie:` header, but only with --verbose/-v, and nothing
here ever passes those (tests/test_yt_feeds.py checks, and tools/yt2_break_tests.py proves
the check works). On top of that _redact() replaces any stderr line with "cookie:" or
"set-cookie:" in it before returning it to anything that might log it. Cookie values never
leave Firefox's profile and yt-dlp's short lived temp copy.

The resume store sits next to config.json (/storage/rp5deck/ by default, RP5DECK_CONFIG
relative otherwise), not in RP5DECK_RUN_DIR (/run/rp5deck), since a thumbnail cache is fine
to lose on reboot but where you left off shouldnt vanish every restart. If config cant be
imported it falls back to /storage/rp5deck, the same folder config.py uses.

What's checked and what isnt:

Checked in yt-dlp's source (yt_dlp/cookies.py, _extract_firefox_cookies): a wrong or missing
profile gives "could not find firefox cookies database in {search_root}". looks_like_auth_error()
matches it and groups it with auth errors, since the fix is the same message either way (sign
in again in the Browser tile, which also means check that profile still exists).

From yt-dlp's docs, not its own strings: "Sign in to confirm you're not a bot" and "Sign in to
confirm your age" are YouTube messages yt-dlp passes through
(https://yt-dlp.net/errors/sign-in-to-confirm-not-a-bot).

Not checked yet: what :ytsubs / :ythistory / :ytwatchlater print when the cookies are missing
or expired (not just a wrong path). A logged-out capture on the device would confirm or fix
_AUTH_ERROR_MARKERS. Until then looks_like_auth_error() is a loose substring match, and
classify_failure() doesnt rely on it alone. Its main signal is comparing: a cookie feed
failing while a plain search works in the same session points at auth, not the network.

patches/YT2-NOTES.md has the capture commands to run on the device before trusting this.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Optional

import browser  # read only: browser.PROFILE_DIR
import signin_vault  # the cookies are sealed, a RAM copy per call
import config  # read only: config.get_value()/config_path()
import youtube  # read only: youtube.YTDLP_PATH / youtube.DENO_PATH

# ---------------------------------------------------------------------------
# Kill switch (config key "youtube.sign_in_enabled")
# ---------------------------------------------------------------------------

SIGN_IN_ENABLED_KEY = ("youtube", "sign_in_enabled")

# what the UI says for not signed in or cookies expired, you're one tab away
SIGN_IN_MESSAGE = "Sign in to YouTube in the Browser tile"


def sign_in_enabled(cfg: Optional[dict] = None) -> bool:
    """True unless the kill switch is set to False.

    True even if config.py's SCHEMA doesnt have the key yet, since config.get_value() gives None
    for an unknown key and that counts as not configured, not off. cfg=None is also True, a
    missing config never turns the feature off.
    """
    if cfg is None:
        return True
    try:
        val = config.get_value(cfg, SIGN_IN_ENABLED_KEY)
    except Exception:
        return True
    return True if val is None else bool(val)


def _cookies_args(cfg: Optional[dict] = None, profile_dir: Optional[str] = None) -> list[str]:
    """The `--cookies-from-browser firefox:<profile>` argv part, or [] with the kill switch off.
    profile_dir defaults to browser.PROFILE_DIR so the path only lives in browser.py.
    """
    if not sign_in_enabled(cfg):
        return []
    profile_dir = profile_dir or browser.PROFILE_DIR
    return ["--cookies-from-browser", "firefox:%s" % profile_dir]


# ---------------------------------------------------------------------------
# Running yt-dlp, and redaction
# ---------------------------------------------------------------------------

FEED_TIMEOUT = 25.0  # feeds can be heavier than search

FEED_EXTRACTORS = {
    "subscriptions": ":ytsubs",
    "history": ":ythistory",
    "watch_later": ":ytwatchlater",
}

_COOKIE_LOG_MARKERS = ("cookie:", "set-cookie:")


def _redact(text: str) -> str:
    """Strips any line that looks like it carries a raw cookie header. yt-dlp only prints one in
    --verbose mode, which nothing here passes, so this is a backup, not the main guard.
    """
    if not text:
        return text
    out = []
    for ln in text.splitlines():
        low = ln.lower()
        if any(m in low for m in _COOKIE_LOG_MARKERS):
            out.append("[redacted: cookie-related log line]")
        else:
            out.append(ln)
    return "\n".join(out)


def _run_yt_dlp(args: list[str], timeout: float):
    """Same shape as youtube._run_yt_dlp(): returns (returncode, stdout, stderr), returncode None
    means it never gave a result (missing interpreter or zipapp, timeout, any OS failure). Never
    raises, never hangs, never passes --verbose/-v (tests/test_yt_feeds.py checks), and always
    redacts stderr before returning it.
    """
    assert "--verbose" not in args and "-v" not in args, \
        "yt_feeds must never pass --verbose/-v to yt-dlp (YT1 redaction rule)"
    cmd = ["python3", youtube.YTDLP_PATH] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None, "", _redact("exec failed or timed out: %s" % " ".join(cmd))
    return proc.returncode, proc.stdout, _redact(proc.stderr)


def _parse_entries(data) -> Optional[list[dict]]:
    """Same field extraction as youtube.search_detailed(), copied on purpose so youtube.py doesnt
    need a shared helper. A change here should go there too and the other way round.
    tests/test_yt_feeds.py's fixture shape test guards this side.
    """
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


def feed_detailed_ex(kind: str, n: int = 20, cfg: Optional[dict] = None,
                      profile_dir: Optional[str] = None, timeout: float = FEED_TIMEOUT):
    """Like feed_detailed(), but also returns the redacted stderr so a caller can run
    classify_failure() for the sign in again check. Returns (results_or_None, stderr_text).
    """
    if kind not in FEED_EXTRACTORS:
        raise ValueError("unknown feed kind %r" % (kind,))
    if n <= 0:
        return [], ""

    extractor = FEED_EXTRACTORS[kind]
    tail = ["--js-runtimes", "deno:%s" % youtube.DENO_PATH,
            extractor, "--flat-playlist", "--playlist-items", "1-%d" % n, "-J"]
    vault = signin_vault.default()
    if vault is not None and _cookies_args(cfg, profile_dir):
        # the cookies are stored encrypted. yt-dlp reads a RAM copy for this one call (removed after),
        # or Firefox's own RAM profile while it runs
        with vault.cookie_profile(profile_dir or browser.PROFILE_DIR) as ram:
            rc, out, err = _run_yt_dlp(_cookies_args(cfg, ram) + tail, timeout=timeout)
    else:
        rc, out, err = _run_yt_dlp(_cookies_args(cfg, profile_dir) + tail, timeout=timeout)
    if rc is None or rc != 0 or not out.strip():
        return None, err

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, err

    return _parse_entries(data), err


def feed_detailed(kind: str, n: int = 20, cfg: Optional[dict] = None,
                   profile_dir: Optional[str] = None, timeout: float = FEED_TIMEOUT) -> Optional[list[dict]]:
    """Works like youtube.search_detailed() for a cookie feed: None = failed, [] = ran but empty, a
    list = the same {id, title, channel, duration_s, thumbnail_url} shape, so YouTubeSheet's card
    grid shows it with no changes.
    """
    results, _err = feed_detailed_ex(kind, n, cfg, profile_dir, timeout)
    return results


def subscriptions(n: int = 20, cfg: Optional[dict] = None, profile_dir: Optional[str] = None):
    return feed_detailed("subscriptions", n, cfg, profile_dir)


def history(n: int = 20, cfg: Optional[dict] = None, profile_dir: Optional[str] = None):
    return feed_detailed("history", n, cfg, profile_dir)


def watch_later(n: int = 20, cfg: Optional[dict] = None, profile_dir: Optional[str] = None):
    return feed_detailed("watch_later", n, cfg, profile_dir)


# ---------------------------------------------------------------------------
# Signed-in / cookies-expired detection
# ---------------------------------------------------------------------------

# Loose matching (see "not checked yet" in the module docstring), gathered from public sources,
# not a real capture of an auth failure on the device. "cookies database" is the one exact piece
# (from yt_dlp/cookies.py: "could not find firefox cookies database in {search_root}").
_AUTH_ERROR_MARKERS = (
    "sign in to confirm",      # "...you're not a bot" / "...your age" (yt-dlp.net/errors)
    "please sign in",
    "you must be signed in",
    "this video is private",
    "login required",
    "cookies database",  # yt_dlp/cookies.py: profile path wrong or missing
)


def looks_like_auth_error(text: Optional[str]) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _AUTH_ERROR_MARKERS)


def classify_failure(feed_result: Optional[list], plain_search_result: Optional[list],
                      feed_stderr: str = "") -> str:
    """The comparing check as a small pure function so it can be tested directly: "ok" (the feed
    worked, maybe empty), "cookies_expired" (show SIGN_IN_MESSAGE), or "network_error" (the
    normal "Search failed" text, a real outage shouldnt get called an auth problem).
    """
    if feed_result is not None:
        return "ok"
    if looks_like_auth_error(feed_stderr):
        return "cookies_expired"
    if plain_search_result is not None:
        return "cookies_expired"
    return "network_error"


# ---------------------------------------------------------------------------
# Local resume-position store
# ---------------------------------------------------------------------------

RESUME_FILENAME = "yt_resume.json"
RESUME_MAX_ENTRIES = 300  # size cap, cheap and survives corruption
RESUME_MIN_SECONDS = 5.0  # dont bother resuming a video that barely started
RESUME_NEAR_END_MARGIN_S = 15.0  # finished means no resume, not restart at the end


def _state_dir(state_dir: Optional[str] = None) -> str:
    if state_dir:
        return state_dir
    try:
        d = os.path.dirname(config.config_path())
        return d or "/storage/rp5deck"
    except Exception:
        return "/storage/rp5deck"


def _resume_path(state_dir: Optional[str] = None) -> str:
    return os.path.join(_state_dir(state_dir), RESUME_FILENAME)


def _atomic_write(path: str, text: str) -> None:
    """Same pattern as config.py's _atomic_write(): temp file in the same folder, flushed and
    fsynced, then renamed over the target, so a crash mid-write leaves the old file (or none),
    never half of one. Copied instead of imported since config's is private.
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".yt_resume.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_store(path: str) -> dict:
    """Never raises. A missing or unreadable file, bad JSON or the wrong top level type drops the
    whole file, and a broken single entry drops just that video id. Resume is a nice extra and
    one bad entry shouldnt take out the rest.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}

    out = {}
    for vid, entry in data.items():
        if not isinstance(vid, str) or not isinstance(entry, dict):
            continue
        pos = entry.get("pos")
        at = entry.get("at")
        if isinstance(pos, bool) or isinstance(at, bool):
            continue  # bool counts as int, leave it out on purpose
        if not isinstance(pos, (int, float)) or not isinstance(at, (int, float)):
            continue
        dur = entry.get("duration")
        dur = float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) else None
        out[vid] = {"pos": float(pos), "duration": dur, "at": float(at)}
    return out


def _save_store(path: str, data: dict) -> None:
    try:
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        _atomic_write(path, text)
    except OSError:
        pass  # a resume write failure must never break playback


def resume_for(video_id: str, state_dir: Optional[str] = None) -> Optional[float]:
    """The last saved position for `video_id` in seconds, or None if there isnt one, it's under
    RESUME_MIN_SECONDS, the video looks finished (within RESUME_NEAR_END_MARGIN_S of its
    duration), or the store cant be read. Never raises.
    """
    if not video_id:
        return None
    store = _load_store(_resume_path(state_dir))
    entry = store.get(video_id)
    if not entry:
        return None
    pos = entry["pos"]
    dur = entry.get("duration")
    if pos < RESUME_MIN_SECONDS:
        return None
    if dur and pos >= dur - RESUME_NEAR_END_MARGIN_S:
        return None
    return pos


def record(video_id: str, pos, duration=None, state_dir: Optional[str] = None) -> None:
    """Saves (pos, duration, at=now) for video_id right away. Caps the store at RESUME_MAX_ENTRIES
    by dropping the oldest "at" entries once it's over. That's a size cap, not a real LRU (a read
    doesnt bump "at"), which is enough to keep it from growing forever. Never raises, a disk error
    here must never break playback.
    """
    if not video_id or pos is None:
        return
    try:
        pos = float(pos)
    except (TypeError, ValueError):
        return

    path = _resume_path(state_dir)
    store = _load_store(path)
    store[video_id] = {
        "pos": pos,
        "duration": float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        "at": time.time(),
    }
    if len(store) > RESUME_MAX_ENTRIES:
        ordered = sorted(store.items(), key=lambda kv: kv[1]["at"])
        store = dict(ordered[-RESUME_MAX_ENTRIES:])
    _save_store(path, store)
