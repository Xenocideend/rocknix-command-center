#!/usr/bin/env python3
"""yt_feeds - cookie-gated YouTube feeds for rp5deck (YT2, Phase 1 of
research/YT1-youtube-full-design.md).

Ownership / constraints (see the brief this was built under): PC-only, no
device access, no git. This module owns everything cookie-gated so
`youtube.py` (W's file, actively edited this week) stays the plain
mpv/Player/search module it already is - `youtube.py` only gets a minimal
patch (see patches/YT2-youtube.patch) adding two thin entry points plus
resume-on-play. `browser.py` and `config.py` are read here (PROFILE_DIR,
get_value()/config_path()) but never written to or edited - both are other
agents' files.

Credentials rule (owner, restated in YT1): rp5deck must never see, store, or
type the owner's YouTube/Google password. This module never does - it reads
Firefox's own already-existing cookie database (via yt-dlp's
`--cookies-from-browser firefox:<profile>`, verified against yt-dlp's own
`cookies.py` source: it copies `cookies.sqlite` into a `TemporaryDirectory`
before opening it, so it works even while Firefox is running, and it is
read-only - nothing here ever writes into the Firefox profile). "Signing in"
is entirely the owner's own action in the Browser tile; there is no
sign-in UI here, only detection of whether it already happened.

Redaction rule: yt-dlp's stderr can in principle echo a raw `Cookie:` header
value, but only in `--verbose`/`-v` mode - this module never passes either
flag anywhere (see `_run_yt_dlp()`; `tests/test_yt_feeds.py` asserts this
directly, and `tools/yt2_break_tests.py` proves the assertion is real by
removing it). `_redact()` is defence in depth on top of that: any stderr
line containing "cookie:" or "set-cookie:" (case-insensitive) is replaced
with a placeholder before this module ever returns it to a caller that
might log it. Cookie *values* are never logged, copied, or held anywhere
outside Firefox's own profile and yt-dlp's own short-lived temp copy of it.

State dir: the resume-position store lives next to config.json
(`os.path.dirname(config.config_path())`, i.e. `/storage/rp5deck/` by
default, `RP5DECK_CONFIG`-relative otherwise) rather than
`RP5DECK_RUN_DIR` (default `/run/rp5deck`, a tmpfs-style runtime dir
main.py already uses for the thumbnail cache) - a thumbnail cache is fine
to lose on reboot, a "pick up where I left off" position is not the kind of
thing that should evaporate every time rp5deck restarts. If `config` cannot
be imported for any reason, falls back to the same literal `/storage/rp5deck`
path config.py itself hard-codes as `DEFAULT_PATH`'s directory.

Verified vs unverified (kept separate on purpose, per YT1 and the project's
own verification standard):
- **Verified from yt-dlp's own source** (`yt_dlp/cookies.py`,
  `_extract_firefox_cookies`, master branch as fetched 24 Sep 2026): the
  literal error text is `'could not find firefox cookies database in
  {search_root}'` when the profile path is wrong/missing - this is one of
  the markers `looks_like_auth_error()` matches (grouped with "auth" rather
  than a separate "misconfigured" bucket for Phase 1 simplicity: either way
  the fix is the same on-screen message, "sign in again in the Browser
  tile", which doubles as "check the Browser tile's Firefox profile still
  exists").
- **Verified from public yt-dlp documentation, not yt-dlp's own error
  strings** (cited in YT1): "Sign in to confirm you're not a bot" /
  "Sign in to confirm your age" are YouTube-side messages yt-dlp surfaces,
  described at https://yt-dlp.net/errors/sign-in-to-confirm-not-a-bot - the
  exact source line was not locatable in the extractor package via a plain
  source fetch (YouTube's playability-status reason strings are not
  necessarily hard-coded as one literal in yt-dlp; they may pass the
  server's own text through).
- **Explicitly UNVERIFIED**: the exact stdout/stderr shape of `:ytsubs` /
  `:ythistory` / `:ytwatchlater` when the profile's cookies are absent or
  expired (as opposed to just wrong-path) has never been captured - YT1's
  own capture list (#6, deliberately-logged-out runs) is exactly what would
  confirm or correct `_AUTH_ERROR_MARKERS` below. Until then,
  `looks_like_auth_error()` is a **tolerant, best-effort substring match**
  against markers gathered from public sources, not a verified-exact match -
  `classify_failure()` does not depend on it alone: its primary signal is
  the comparative heuristic YT1 §"Error states" describes (a cookie-gated
  feed failing while a plain unauthenticated search succeeds in the same
  session is strong evidence of an auth problem, not a network one).

See patches/YT2-NOTES.md for the exact real-capture commands Main should
run on-device before this is fully trusted, and for the youtube.py/config.py
patch details (anchors, md5s).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Optional

import browser          # read-only: browser.PROFILE_DIR (W's module)
import config           # read-only: config.get_value()/config_path() (G's module)
import youtube           # read-only: youtube.YTDLP_PATH / youtube.DENO_PATH (W's module)

# ---------------------------------------------------------------------------
# Kill switch (config key "youtube.sign_in_enabled" - patches/YT2-config.patch)
# ---------------------------------------------------------------------------

SIGN_IN_ENABLED_KEY = ("youtube", "sign_in_enabled")

# The exact, actionable UI copy for "not signed in / cookies expired" -
# the owner is one tab away (YT1 §"Error states").
SIGN_IN_MESSAGE = "Sign in to YouTube in the Browser tile"


def sign_in_enabled(cfg: Optional[dict] = None) -> bool:
    """True unless the owner's kill switch is explicitly set to False.

    Defaults to True whether or not config.py's SCHEMA has actually been
    patched with this key yet - config.get_value() returns None for a
    key_path with no matching schema field (or missing from `cfg`), and
    that is treated the same as "not configured", not "off". `cfg=None`
    (no config loaded at all) is also True - never crash or silently
    disable a feature because the caller didn't have a config dict handy.
    """
    if cfg is None:
        return True
    try:
        val = config.get_value(cfg, SIGN_IN_ENABLED_KEY)
    except Exception:
        return True
    return True if val is None else bool(val)


def _cookies_args(cfg: Optional[dict] = None, profile_dir: Optional[str] = None) -> list[str]:
    """The `--cookies-from-browser firefox:<profile>` argv fragment, or []
    if the kill switch is off. `profile_dir` defaults to
    `browser.PROFILE_DIR` (read, never hard-coded a second time - YT1
    §3 Architecture) so a future profile-path change only needs updating
    in one file (browser.py)."""
    if not sign_in_enabled(cfg):
        return []
    profile_dir = profile_dir or browser.PROFILE_DIR
    return ["--cookies-from-browser", "firefox:%s" % profile_dir]


# ---------------------------------------------------------------------------
# yt-dlp invocation + redaction
# ---------------------------------------------------------------------------

FEED_TIMEOUT = 25.0     # feeds can be heavier than search (YT1's latency note)

FEED_EXTRACTORS = {
    "subscriptions": ":ytsubs",
    "history": ":ythistory",
    "watch_later": ":ytwatchlater",
}

_COOKIE_LOG_MARKERS = ("cookie:", "set-cookie:")


def _redact(text: str) -> str:
    """Strip any line that looks like it carries a raw cookie header value.
    yt-dlp only ever echoes such a line in --verbose mode (never passed by
    this module - see `_run_yt_dlp()`), so this is defence in depth, not
    the primary control."""
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
    """Same shape as youtube._run_yt_dlp(): returns (returncode, stdout,
    stderr), returncode None means "never produced a result" (missing
    interpreter/zipapp, timeout, any other OS-level failure). Never raises,
    never hangs, never passes --verbose/-v (asserted by
    tests/test_yt_feeds.py so a future edit can't reintroduce it), and
    always redacts stderr before returning it."""
    assert "--verbose" not in args and "-v" not in args, \
        "yt_feeds must never pass --verbose/-v to yt-dlp (YT1 redaction rule)"
    cmd = ["python3", youtube.YTDLP_PATH] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None, "", _redact("exec failed or timed out: %s" % " ".join(cmd))
    return proc.returncode, proc.stdout, _redact(proc.stderr)


def _parse_entries(data) -> Optional[list[dict]]:
    """Same field extraction as youtube.search_detailed() (duplicated
    intentionally, not imported: that logic is inlined in a function W is
    actively editing, and factoring it out mid-week would be a second,
    avoidable edit surface on youtube.py - see the module docstring's
    "youtube.py stays the plain module it is today" rationale, straight
    from YT1 §3). Any change here should be mirrored there and vice versa;
    tests/test_yt_feeds.py's fixture-shape test guards this module's half."""
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
    """Like feed_detailed(), but also returns the redacted stderr text so a
    caller can run classify_failure() for the "sign in again" heuristic.
    Returns (results_or_None, stderr_text)."""
    if kind not in FEED_EXTRACTORS:
        raise ValueError("unknown feed kind %r" % (kind,))
    if n <= 0:
        return [], ""

    extractor = FEED_EXTRACTORS[kind]
    args = _cookies_args(cfg, profile_dir)
    args += ["--js-runtimes", "deno:%s" % youtube.DENO_PATH,
              extractor, "--flat-playlist", "--playlist-items", "1-%d" % n, "-J"]
    rc, out, err = _run_yt_dlp(args, timeout=timeout)
    if rc is None or rc != 0 or not out.strip():
        return None, err

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, err

    return _parse_entries(data), err


def feed_detailed(kind: str, n: int = 20, cfg: Optional[dict] = None,
                   profile_dir: Optional[str] = None, timeout: float = FEED_TIMEOUT) -> Optional[list[dict]]:
    """Drop-in equivalent of youtube.search_detailed() for a cookie-gated
    feed: None = failed, [] = ran but empty, a list = the same
    {id, title, channel, duration_s, thumbnail_url} shape search_detailed()
    already produces - YouTubeSheet's card grid needs zero changes to
    render it (YT1 §3)."""
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

# TOLERANT matching (see module docstring's "explicitly UNVERIFIED" note):
# gathered from public sources, not a confirmed real capture of an
# on-device auth failure. "cookies database" is the one verified-exact
# fragment (yt_dlp/cookies.py's `_extract_firefox_cookies`, master, fetched
# 24 Sep 2026: "could not find firefox cookies database in {search_root}").
_AUTH_ERROR_MARKERS = (
    "sign in to confirm",      # "...you're not a bot" / "...your age" (yt-dlp.net/errors)
    "please sign in",
    "you must be signed in",
    "this video is private",
    "login required",
    "cookies database",        # yt_dlp/cookies.py: profile path wrong/missing
)


def looks_like_auth_error(text: Optional[str]) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _AUTH_ERROR_MARKERS)


def classify_failure(feed_result: Optional[list], plain_search_result: Optional[list],
                      feed_stderr: str = "") -> str:
    """YT1 §"Error states"'s heuristic, as a small pure function so it can
    be unit-tested directly: "ok" (the feed call actually worked - possibly
    empty), "cookies_expired" (a distinct, actionable state - show
    SIGN_IN_MESSAGE), or "network_error" (the existing generic "Search
    failed" copy - a real outage must not get mislabeled as an auth
    problem, per YT1)."""
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
RESUME_MAX_ENTRIES = 300     # size cap (YT1: "cheap and robust to corruption")
RESUME_MIN_SECONDS = 5.0     # don't bother resuming a video barely started
RESUME_NEAR_END_MARGIN_S = 15.0   # treat "finished" as "no resume", not "restart at the end"


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
    """Same pattern as config.py's own _atomic_write(): temp file in the
    same directory, flushed + fsync'd, then renamed over the target - a
    crash mid-write leaves the old (or no) file, never a half-written one.
    Duplicated rather than imported: config._atomic_write is a private
    (leading-underscore) helper in a file this module never edits."""
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
    """Never raises. A missing file, unreadable file, corrupt JSON, wrong
    top-level type, or a malformed individual entry is dropped (the whole
    file for the first three, just that one video id for the last) rather
    than failing the caller - resume is a nice-to-have, never a crash
    surface, and a single bad entry must not take down every other one."""
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
            continue                      # bool is technically int - exclude explicitly
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
        pass          # best-effort: a resume write failure must never break playback


def resume_for(video_id: str, state_dir: Optional[str] = None) -> Optional[float]:
    """The last recorded playback position for `video_id`, in seconds, or
    None if there is none, it is below RESUME_MIN_SECONDS, the video looks
    finished (within RESUME_NEAR_END_MARGIN_S of its recorded duration), or
    the store cannot be read (never raises)."""
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
    """Write-through: persist (pos, duration, at=now) for video_id. Caps
    the store at RESUME_MAX_ENTRIES, evicting the oldest ("at") entries
    first once it grows past the cap - a size cap, not a true LRU (a read
    never bumps "at"), which is enough to satisfy "robust to corruption /
    never grows forever" without the extra bookkeeping a real LRU needs.
    Never raises - a disk error here must never break playback."""
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
