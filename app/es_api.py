#!/usr/bin/env python3
"""es_api - client for EmulationStation's local HTTP API (B10a).

Talks to the ROCKNIX build of ROCKNIX/emulationstation-next's built-in
HttpServerThread, bound to 127.0.0.1:1234 on the device (see
HttpServerThread.cpp `mHttpServer->listen(ip.c_str(), 1234)`; it only binds
0.0.0.0 if the "PublicWebAccess" setting is on, which this module never
touches). The companion view (DESIGN.md "Navigation v2") uses this module to
show the art/video of the running (or, once B9 lands, selected) game, plus a
Manual button when a manual exists.

Design rules (non-negotiable, see TASKS.md B10a):
  - Only GET requests. This module contains no code path that can reach a
    POST/mutating endpoint - see ES_HTTP_ENDPOINTS below for the full list
    and which ones are off-limits.
  - Short timeouts (<=1s) and NEVER raise into the caller. "ES is down",
    "timed out" and "returned garbage" are all just empty results (None or
    []), exactly like hud.py's "never 0, never a fake value" rule but
    inverted: here missing data is *empty*, not a sentinel, because the
    companion view's easiest correct behaviour on any failure is "show
    nothing" (fall through to a placeholder), not "crash the app".
  - Plain dataclasses, stdlib only (json, urllib, os, time). No pip.
  - A tiny TTL cache on systems()/games() only - NOT on running_game(),
    which the companion view polls to notice game start/stop and must
    always be fresh.

----------------------------------------------------------------------------
EVERY endpoint HttpServerThread.cpp registers (SOURCED: ROCKNIX/
emulationstation-next, es-app/src/services/HttpServerThread.cpp, read via
`gh api repos/ROCKNIX/emulationstation-next/contents/...` on 2026-09-23 for
this task; also see the file's own top-of-file comment block, which lists
the same routes in prose). This is the complete route table - nothing here
is inferred from behaviour, it is read directly from the registration code:

  GET  /                                              redirect -> /index.html
  GET  /favicon.png                                   window icon PNG
  GET  /index.html                                    the API's own web UI
  GET  /quit                     [?confirm=menu|switchscreen]  MUTATING (quits ES / opens quit menu)
  GET  /shutdown                                      MUTATING (shuts down the device)
  GET  /restart                                       MUTATING (reboots)
  GET  /emukill                                       MUTATING (kills the running emulator)
  GET  /caps                                          {"Version":..., "SortName": bool}
  GET  /systems                                       array of every system (see System)
  GET  /runningGame                                    the running game, or {"msg":"NO GAME RUNNING"}
                                                        (ignores ?localpaths - see below)
  GET  /isIdle                                        [true]/[false] - no scrape/hash/update running
  GET  /systems/{system}/logo                         the system's theme logo image bytes
  GET  /systems/{system}/games                        array of every game in a system
                                                        (media fields are always relative API URLs -
                                                        this route's handler calls getFileDataJson()
                                                        with the localpaths argument OMITTED, so it
                                                        is hardcoded false regardless of query string)
  GET  /systems/{system}/games/{id}/media/{type}      the raw media file bytes
  GET  /systems/{system}/games/{id}  [?localpaths=true]  one game; WITH localpaths=true, every
                                                        MD_PATH field (image/video/manual/...) is
                                                        the real on-device filesystem path instead
                                                        of a URL. This is the only place local paths
                                                        come from - there is no separate "resolve a
                                                        URL to a path" endpoint.
  GET  /systems/{system}  [?localpaths=true]           one system (localpaths affects its "logo")
  GET  /reloadgames                                   MUTATING (reloads all gamelists)
  GET  /resources/{path}                              arbitrary theme/resource file
  GET  /{path}                                        catch-all -> resources/services/{path}

  POST /systems/{system}/games/{id}/media/{type}      MUTATING (uploads/replaces a media file)
  POST /systems/{system}/games/{id}                   MUTATING (overwrites game metadata)
  POST /messagebox                                    MUTATING (pops a message box in the ES UI)
  POST /notify                                        MUTATING (shows a notification)
  POST /storage/event                                 MUTATING (feeds an event into ES's own handler)
  POST /launch                                        MUTATING (launches a ROM by path)
  POST /addgames/{system}                             MUTATING (adds/updates games from gamelist XML)
  POST /removegames/{system}                          MUTATING (deletes games + their ROM files)

Everything this module calls is a GET from the top half of that table
(/caps, /systems, /runningGame, /systems/{s}/games, /systems/{s}/games/{id}).
It never calls anything below the "MUTATING" line, and never issues a POST.

----------------------------------------------------------------------------
THE SELECTED-GAME QUESTION (owner's top feature: art while scrolling the
library, DESIGN.md "Navigation v2", TASKS.md B9/B10):

No endpoint above returns the game currently highlighted-but-not-launched in
the library. /runningGame only reports a game that has actually been
launched (FileData::GetRunningGame() - populated at launch, not at cursor
move). /isIdle is a plain boolean. There is no `/selectedGame`, no
`/cursor`, no long-poll/SSE/websocket route, nothing keyed by "selected" or
"highlight" anywhere in HttpServerThread.cpp's ~30 registered routes. This
confirms research/E9a-es-integration.md's finding (which tested this by
probing candidate URLs and getting 404s) from the other direction, by
reading the source that defines the whole route table rather than guessing
at undocumented paths: the HTTP API is structurally incapable of exposing
selection, not just failing to document it. So selected_game() below always
returns None - see its docstring. The real signal, if there is one, has to
come from ES's Scripting.cpp `game-selected` event hook (owned by TASKS.md
B9/E9, not this module) or a UI-side heuristic; this module does not
attempt either.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Optional

_logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------

BASE_URL = "http://127.0.0.1:1234"

# "Short timeouts (<=1s)" per the task brief - a wedged/absent ES must never
# stall the UI thread that (indirectly) waits on these calls.
DEFAULT_TIMEOUT = 0.8

# "a tiny TTL cache for systems/games lists" - deliberately short: long
# enough to stop a companion view redrawing at 30 Hz from hammering ES with
# one HTTP round trip per frame, short enough that a real change (new ROM
# scraped, system added) shows up within a couple of seconds, not "restart
# the app". running_game() is NEVER cached - see module docstring.
CACHE_TTL = 2.0

# MD_PATH metadata kinds, i.e. the ones that can appear as a media field.
# SOURCED: the 10 image/video/manual kinds are OBSERVED directly in real
# device JSON (tests/fixtures/es-games-*-real-capture-2026-09-23.json).
# "magazine" and "map" are NOT observed in this library (no game happened to
# have them scraped) but are SOURCED from HttpApi.cpp's ImportMedia(), which
# special-cases MetaDataId::Manual, Magazine and Map alongside Video - so
# they are real MD_PATH kinds this same code path can emit, just unseen here.
MEDIA_KINDS = (
    "image", "thumbnail", "marquee", "fanart", "titleshot", "mix",
    "video", "manual", "cartridge", "boxback",
    "magazine", "map",
)

# Fields on a game JSON object that are never media, so anything else is
# swept into Game.extra rather than silently dropped (ES adds metadata
# fields over time; a hardcoded allowlist of "the media fields" plus an
# extra bucket for the rest survives that better than modelling every field).
_GAME_CORE_KEYS = frozenset({"id", "path", "name", "systemName", "desc"})

_MISSING = object()


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

@dataclass
class MediaRef:
    """One media slot (image/video/manual/...) for one game.

    `url` is always set (it is either the literal URL ES gave us, or one
    this module can synthesize from the fixed /systems/{s}/games/{id}/media/
    {kind} pattern - see HttpServerThread.cpp's registration of that route).
    `local_path` is set only when ES told us the real on-device path (via
    ?localpaths=true) AND that path exists on disk right now - a stale
    gamelist entry pointing at a deleted file must not look "local"."""

    kind: str
    url: str
    local_path: Optional[str] = None

    @property
    def best(self) -> str:
        """local_path if it resolved to a real file, else the URL fallback."""
        return self.local_path if self.local_path else self.url

    @property
    def is_local(self) -> bool:
        return self.local_path is not None


@dataclass
class Game:
    id: str
    system: str
    name: str
    rom_path: str = ""
    desc: str = ""
    media: dict = field(default_factory=dict)   # kind -> MediaRef
    extra: dict = field(default_factory=dict)   # rating, developer, publisher, genre,
                                                 # players, favorite, hidden, kidgame,
                                                 # playcount, lastplayed, gametime,
                                                 # releasedate, scraperId, ... (whatever
                                                 # ES sends that isn't id/path/name/
                                                 # systemName/desc/a media kind)

    def manual(self) -> Optional[MediaRef]:
        return self.media.get("manual")

    def has_manual(self) -> bool:
        m = self.media.get("manual")
        return m is not None and bool(m.best)


@dataclass
class System:
    name: str
    fullname: str = ""
    hardware_type: str = ""
    manufacturer: str = ""
    theme: str = ""
    extensions: list = field(default_factory=list)
    visible: Optional[bool] = None
    collection: Optional[bool] = None
    gamesystem: Optional[bool] = None
    groupsystem: Optional[bool] = None
    total_games: int = 0
    visible_games: int = 0
    favorite_games: int = 0
    played_games: int = 0
    hidden_games: int = 0
    most_played_game: str = ""
    logo: Optional[str] = None
    extra: dict = field(default_factory=dict)


# Full route table - see the module docstring for how each was verified.
# Kept as data (not just prose) so a test can assert this module never
# builds a request to anything marked mutating=True.
ES_HTTP_ENDPOINTS = (
    {"method": "GET", "path": "/", "mutating": False, "note": "redirect -> /index.html"},
    {"method": "GET", "path": "/favicon.png", "mutating": False, "note": "window icon PNG"},
    {"method": "GET", "path": "/index.html", "mutating": False, "note": "API's own web UI"},
    {"method": "GET", "path": "/quit", "mutating": True, "note": "quits ES / opens quit menu"},
    {"method": "GET", "path": "/shutdown", "mutating": True, "note": "shuts down the device"},
    {"method": "GET", "path": "/restart", "mutating": True, "note": "reboots"},
    {"method": "GET", "path": "/emukill", "mutating": True, "note": "kills the running emulator"},
    {"method": "GET", "path": "/caps", "mutating": False, "note": "version + capability flags"},
    {"method": "GET", "path": "/systems", "mutating": False, "note": "array of every system"},
    {"method": "GET", "path": "/runningGame", "mutating": False, "note": "the running game or NO GAME RUNNING; ignores ?localpaths"},
    {"method": "GET", "path": "/isIdle", "mutating": False, "note": "[true]/[false]"},
    {"method": "GET", "path": "/systems/{system}/logo", "mutating": False, "note": "theme logo image bytes"},
    {"method": "GET", "path": "/systems/{system}/games", "mutating": False, "note": "array of every game; media is always URLs, ?localpaths has no effect here"},
    {"method": "GET", "path": "/systems/{system}/games/{id}/media/{type}", "mutating": False, "note": "raw media file bytes"},
    {"method": "GET", "path": "/systems/{system}/games/{id}", "mutating": False, "note": "one game; ?localpaths=true returns real filesystem paths"},
    {"method": "GET", "path": "/systems/{system}", "mutating": False, "note": "one system; ?localpaths=true affects logo"},
    {"method": "GET", "path": "/reloadgames", "mutating": True, "note": "reloads all gamelists"},
    {"method": "GET", "path": "/resources/{path}", "mutating": False, "note": "arbitrary theme/resource file"},
    {"method": "GET", "path": "/{path}", "mutating": False, "note": "catch-all -> resources/services/{path}"},
    {"method": "POST", "path": "/systems/{system}/games/{id}/media/{type}", "mutating": True, "note": "uploads/replaces a media file"},
    {"method": "POST", "path": "/systems/{system}/games/{id}", "mutating": True, "note": "overwrites game metadata"},
    {"method": "POST", "path": "/messagebox", "mutating": True, "note": "pops a message box in the ES UI"},
    {"method": "POST", "path": "/notify", "mutating": True, "note": "shows a notification"},
    {"method": "POST", "path": "/storage/event", "mutating": True, "note": "feeds an event into ES's handler"},
    {"method": "POST", "path": "/launch", "mutating": True, "note": "launches a ROM by path"},
    {"method": "POST", "path": "/addgames/{system}", "mutating": True, "note": "adds/updates games from gamelist XML"},
    {"method": "POST", "path": "/removegames/{system}", "mutating": True, "note": "deletes games + their ROM files"},
)

# Endpoints this module's client is allowed to call, as (method, path)
# pairs - several paths above are registered for BOTH a safe GET and a
# mutating POST (e.g. /systems/{system}/games/{id}), so the method must be
# part of the key or a lookup collapses the two onto one entry. Used only
# by the test suite as a guard - see tests/test_es_api.py TestNeverMutates.
_USED_ENDPOINT_PATHS = frozenset({
    ("GET", "/caps"), ("GET", "/systems"), ("GET", "/runningGame"),
    ("GET", "/systems/{system}/games"), ("GET", "/systems/{system}/games/{id}"),
    ("GET", "/systems/{system}"),
})


# ----------------------------------------------------------------------------
# RUNTIME allowlist guard - the last line of defence before a request ever
# leaves this process.
#
# _USED_ENDPOINT_PATHS above documents intent and is checked by tests, but
# it never actually stops a request - a future edit to this file (a typo, a
# careless refactor of a path string, a new caller) could build a path
# that reaches /quit, /shutdown, /restart, /emukill, /reloadgames or any
# POST-only route, and _get() would have happily requested it. ES treats
# ALL of those as GET-able (see ES_HTTP_ENDPOINTS: /quit etc. are
# registered with mHttpServer->Get(...), not Post), so "this module only
# does GET" is not by itself protection against them.
#
# So _get() below re-derives, from the path string ALONE, whether it is
# even shaped like one of the six routes this client is allowed to use -
# independent of and in addition to what the calling code intended. A path
# that doesn't match is refused before urllib is ever touched: no DNS, no
# socket, nothing hits the network.
# ----------------------------------------------------------------------------

def _is_safe_segment(seg: str) -> bool:
    """True if `seg` is fit to be the ENTIRE {system} or {id} part of an
    allowed path - i.e. it cannot smuggle in extra path segments or escape
    into a different route once concatenated into the URL.

    Rejects (case-insensitively where it matters):
      - empty
      - a literal "/" anywhere (would introduce a new path segment)
      - ".." anywhere (path traversal, however it's later interpreted)
      - "?" or "#" anywhere (would start a query/fragment mid-path)
      - "%2f" anywhere (a URL-ENCODED slash - the whole point of a
        traversal attempt like id="1/../../shutdown" is that
        urllib.parse.quote(..., safe="") turns the raw "/" into literal
        "%2F" text, which then sails through a naive `"/" not in seg`
        check while still meaning "slash" to anything that later decodes
        it - so it must be checked as a bare substring, not decoded first)
    """
    if not seg:
        return False
    lowered = seg.lower()
    for bad in ("/", "..", "?", "#", "%2f"):
        if bad in lowered:
            return False
    return True


# Exact paths that take no parameters at all.
_ALLOWED_EXACT_PATHS = frozenset({"/caps", "/systems", "/runningGame"})

# Parameterised paths, most specific first (structurally mutually exclusive
# by segment count, so order does not actually matter for correctness -
# kept specific-first for readability).
_SYSTEM_GAME_DETAIL_RE = re.compile(r"^/systems/([^/]+)/games/([^/]+)$")
_SYSTEM_GAMES_RE = re.compile(r"^/systems/([^/]+)/games$")
_SYSTEM_ONLY_RE = re.compile(r"^/systems/([^/]+)$")


def _path_is_allowed(path_only: str) -> bool:
    """path_only is the request path with any query string already
    stripped (see _get). Returns True only for the 6 shapes in
    _USED_ENDPOINT_PATHS, with their {system}/{id} segments individually
    validated by _is_safe_segment - never on string prefix/substring
    matching against the raw path, which traversal defeats trivially."""
    if path_only in _ALLOWED_EXACT_PATHS:
        return True

    m = _SYSTEM_GAME_DETAIL_RE.match(path_only)
    if m:
        return _is_safe_segment(m.group(1)) and _is_safe_segment(m.group(2))

    m = _SYSTEM_GAMES_RE.match(path_only)
    if m:
        return _is_safe_segment(m.group(1))

    m = _SYSTEM_ONLY_RE.match(path_only)
    if m:
        return _is_safe_segment(m.group(1))

    return False


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def _bool_str(v) -> Optional[bool]:
    """ES encodes most booleans as the literal strings "true"/"false"
    (writer.String, not writer.Bool - see HttpApi.cpp getSystemDataJson /
    getFileDataJson). Anything else (missing, already a JSON bool, garbage)
    degrades to None rather than a guessed True/False."""
    if isinstance(v, bool):
        return v
    if v == "true":
        return True
    if v == "false":
        return False
    return None


def _int_or(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class _TTLCache:
    """Tiny per-key TTL cache. Not thread-safe - callers use one ESClient
    per thread, matching how audio.py/hud.py are used from the app."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._store: dict = {}

    def get(self, key):
        hit = self._store.get(key, _MISSING)
        if hit is _MISSING:
            return _MISSING
        expiry, value = hit
        if time.monotonic() >= expiry:
            self._store.pop(key, None)
            return _MISSING
        return value

    def set(self, key, value):
        self._store[key] = (time.monotonic() + self.ttl, value)

    def clear(self):
        self._store.clear()


def _media_ref(base_url: str, system: str, game_id: str, kind: str, value) -> Optional[MediaRef]:
    """Build a MediaRef from one media field's raw JSON value, in either
    form ES can send it in:

      - "/systems/{system}/games/{id}/media/{kind}" - the URL form used by
        /systems/{s}/games and /runningGame (getFileDataJson with
        localpaths=false, which is *hardcoded* for the bulk games route -
        see the module docstring). local_path is unknown here.
      - anything else - the LOCAL FILESYSTEM PATH form, only ever seen from
        /systems/{s}/games/{id}?localpaths=true. local_path is that path if
        it still exists on disk, else None (deleted/moved file); the URL
        fallback is synthesized from the fixed route pattern either way, so
        the caller always has *something* to fall back to.
    """
    if not value or not isinstance(value, str):
        return None
    if value.startswith("/systems/"):
        return MediaRef(kind=kind, url=base_url + value, local_path=None)
    local_path = value if os.path.exists(value) else None
    url = "%s/systems/%s/games/%s/media/%s" % (
        base_url,
        urllib.parse.quote(system, safe=""),
        urllib.parse.quote(game_id, safe=""),
        urllib.parse.quote(kind, safe=""),
    )
    return MediaRef(kind=kind, url=url, local_path=local_path)


def _parse_game(d: dict, base_url: str, system_hint: str = "") -> Game:
    gid = str(d.get("id", ""))
    system = str(d.get("systemName") or system_hint or "")

    media = {}
    for kind in MEDIA_KINDS:
        ref = _media_ref(base_url, system, gid, kind, d.get(kind))
        if ref is not None:
            media[kind] = ref

    extra = {k: v for k, v in d.items() if k not in _GAME_CORE_KEYS and k not in MEDIA_KINDS}

    return Game(
        id=gid,
        system=system,
        name=str(d.get("name", "")),
        rom_path=str(d.get("path", "")),
        desc=str(d.get("desc", "")),
        media=media,
        extra=extra,
    )


def _parse_system(d: dict) -> System:
    extra = {k: v for k, v in d.items() if k not in {
        "name", "fullname", "hardwareType", "manufacturer", "theme", "extensions",
        "visible", "collection", "gamesystem", "groupsystem",
        "totalGames", "visibleGames", "favoriteGames", "playedGames", "hiddenGames",
        "mostPlayedGame", "logo",
    }}
    return System(
        name=str(d.get("name", "")),
        fullname=str(d.get("fullname", "")),
        hardware_type=str(d.get("hardwareType", "")),
        manufacturer=str(d.get("manufacturer", "")),
        theme=str(d.get("theme", "")),
        extensions=list(d.get("extensions") or []),
        visible=_bool_str(d.get("visible")),
        collection=_bool_str(d.get("collection")),
        gamesystem=_bool_str(d.get("gamesystem")),
        groupsystem=_bool_str(d.get("groupsystem")),
        total_games=_int_or(d.get("totalGames")),
        visible_games=_int_or(d.get("visibleGames")),
        favorite_games=_int_or(d.get("favoriteGames")),
        played_games=_int_or(d.get("playedGames")),
        hidden_games=_int_or(d.get("hiddenGames")),
        most_played_game=str(d.get("mostPlayedGame", "")),
        logo=d.get("logo"),
        extra=extra,
    )


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------

class ESClient:
    """A short-timeout, never-raising, GET-only client for ES's HTTP API.

    One instance per app is normal (its cache is meant to be shared across
    however many places want to know "what's on screen"); tests construct
    fresh ones freely since it holds no OS resources of its own."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = DEFAULT_TIMEOUT,
                 cache_ttl: float = CACHE_TTL, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # `opener` lets tests inject a fake transport; production code never
        # sets it and gets urllib.request.urlopen.
        self._urlopen = opener or urllib.request.urlopen
        self._systems_cache = _TTLCache(cache_ttl)
        self._games_cache = _TTLCache(cache_ttl)

    # -- transport -----------------------------------------------------

    def _get(self, path: str):
        """GET base_url+path, parsed as JSON. None on ANY failure - ES not
        running (connection refused), not answering (timeout), a 4xx/5xx
        (HTTPError, a subclass of OSError), a non-UTF8 or non-JSON body, or
        anything else. Never raises.

        RUNTIME allowlist guard: `path` is checked against
        _path_is_allowed() BEFORE anything else - if it doesn't structurally
        match one of the 6 routes this client is meant to use, this refuses
        the request and returns None without ever constructing a socket.
        This is deliberately independent of what the caller intended: it
        protects against a future bug in THIS file routing a request at
        /quit, /shutdown, /restart, /emukill, /reloadgames, or any POST-only
        route - all of which ES accepts as plain GETs."""
        path_only = urllib.parse.urlsplit(path).path
        if not _path_is_allowed(path_only):
            _logger.warning("es_api: refused disallowed request path: %r", path)
            return None
        url = self.base_url + path
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with self._urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except Exception:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    # -- public API ------------------------------------------------------

    def caps(self) -> Optional[dict]:
        data = self._get("/caps")
        return data if isinstance(data, dict) else None

    def systems(self) -> list:
        cached = self._systems_cache.get("systems")
        if cached is not _MISSING:
            return cached
        data = self._get("/systems")
        result = [_parse_system(s) for s in data] if isinstance(data, list) else []
        self._systems_cache.set("systems", result)
        return result

    def games(self, system: str) -> list:
        cached = self._games_cache.get(system)
        if cached is not _MISSING:
            return cached
        path = "/systems/%s/games" % urllib.parse.quote(system, safe="")
        data = self._get(path)
        result = [_parse_game(g, self.base_url, system_hint=system) for g in data] if isinstance(data, list) else []
        self._games_cache.set(system, result)
        return result

    def game_detail(self, system: str, game_id: str) -> Optional[Game]:
        """One game, with local filesystem paths resolved wherever ES has
        them (?localpaths=true). This is THE media-path lookup the
        companion view needs - see media_for(), which is this plus "just
        give me the media dict"."""
        path = "/systems/%s/games/%s?localpaths=true" % (
            urllib.parse.quote(system, safe=""), urllib.parse.quote(game_id, safe=""),
        )
        data = self._get(path)
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return _parse_game(data, self.base_url, system_hint=system)

    def media_for(self, system: str, game_id: str) -> dict:
        """kind -> MediaRef for one game, local paths resolved where
        possible, HTTP URL as the fallback (MediaRef.best). Empty dict if
        the game can't be found or ES is unreachable - never raises."""
        detail = self.game_detail(system, game_id)
        return dict(detail.media) if detail is not None else {}

    def running_game(self) -> Optional[Game]:
        """The game ES currently has running, or None (nothing running, or
        ES unreachable/broken - these are indistinguishable to a caller by
        design, per the task's "ES down = empty result" rule).

        /runningGame's handler always calls ToJson(file) with the default
        localpaths=false (HttpServerThread.cpp's /runningGame route takes
        no query-string branch at all), so its media fields are URLs, not
        paths, however the request was made. To get real files for the
        companion view, this does one extra lookup via game_detail() (which
        DOES support ?localpaths=true) and merges those paths in. If that
        second call fails for any reason, the URL-only Game from
        /runningGame is still returned rather than turning a partial success
        into a total failure."""
        data = self._get("/runningGame")
        if not isinstance(data, dict) or "msg" in data:
            return None
        game = _parse_game(data, self.base_url)
        if game.system and game.id:
            detail = self.game_detail(game.system, game.id)
            if detail is not None:
                game.media.update(detail.media)
        return game

    def selected_game(self) -> Optional[Game]:
        """Always returns None - see the module docstring's "THE
        SELECTED-GAME QUESTION" section. Read from HttpServerThread.cpp's
        full route table (reproduced above as ES_HTTP_ENDPOINTS): there is
        no endpoint anywhere in this ES fork's HTTP API that reports the
        game currently highlighted (but not launched) in the library. This
        method exists so callers can write `es_api.selected_game()` once
        and get a real signal later without an interface change, IF a
        future ES build or a companion patch adds one - not because this
        build has one now. Verified 2026-09-23 by reading the source, not
        by probing (research/E9a-es-integration.md already probed and got
        404s on every guess; this reads the code that makes those guesses
        unnecessary)."""
        return None

    def clear_cache(self):
        self._systems_cache.clear()
        self._games_cache.clear()


# ----------------------------------------------------------------------------
# Module-level convenience: one default client, like hud.py's module
# functions backed by a singleton sampler.
# ----------------------------------------------------------------------------

_default_client = ESClient()


def systems() -> list:
    return _default_client.systems()


def games(system: str) -> list:
    return _default_client.games(system)


def running_game() -> Optional[Game]:
    return _default_client.running_game()


def selected_game() -> Optional[Game]:
    return _default_client.selected_game()


def media_for(system: str, game_id: str) -> dict:
    return _default_client.media_for(system, game_id)


def game_detail(system: str, game_id: str) -> Optional[Game]:
    return _default_client.game_detail(system, game_id)


def caps() -> Optional[dict]:
    return _default_client.caps()


def clear_cache():
    _default_client.clear_cache()


# ----------------------------------------------------------------------------
# CLI - for on-device verification ("run es_api.py against live ES").
# ----------------------------------------------------------------------------

def _jsonable(obj):
    """dataclasses.asdict, but tolerant of a bare None at the top."""
    return asdict(obj) if obj is not None else None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" not in argv:
        print("usage: python3 es_api.py --json [--system NAME] [--media SYSTEM GAME_ID]")
        return 2

    client = ESClient()
    out = {
        "base_url": client.base_url,
        "caps": client.caps(),
    }

    systems_list = client.systems()
    out["systems_count"] = len(systems_list)
    out["systems_sample"] = [_jsonable(s) for s in systems_list[:3]]

    out["running_game"] = _jsonable(client.running_game())
    out["selected_game"] = _jsonable(client.selected_game())

    if "--system" in argv:
        name = argv[argv.index("--system") + 1]
        gs = client.games(name)
        out["games_%s_count" % name] = len(gs)
        out["games_%s_sample" % name] = [_jsonable(g) for g in gs[:3]]

    if "--media" in argv:
        i = argv.index("--media")
        sysname, gid = argv[i + 1], argv[i + 2]
        media = client.media_for(sysname, gid)
        out["media_%s_%s" % (sysname, gid)] = {k: _jsonable(v) for k, v in media.items()}

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
