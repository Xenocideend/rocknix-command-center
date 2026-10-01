#!/usr/bin/env python3
"""Client for EmulationStation's local HTTP API.

ROCKNIX's emulationstation-next runs an HttpServerThread on 127.0.0.1:1234 (it only binds
0.0.0.0 with PublicWebAccess on, which this never touches). The companion view uses it for
the running game's art and video, and a Manual button when theres a manual.

Rules:
  - GET only, and no code path here can reach a mutating route (see ES_HTTP_ENDPOINTS)
  - short timeouts (1 s or less) and never raise: ES down, a timeout or garbage all come
    back empty (None or []), since "show nothing" is always a safe answer for the companion
  - stdlib only
  - a small TTL cache on systems()/games() only, never on running_game(), which has to be
    fresh to notice a game start or stop

Every route HttpServerThread.cpp registers (read from ROCKNIX/emulationstation-next's
es-app/src/services/HttpServerThread.cpp):

  GET  /                                              redirect -> /index.html
  GET  /favicon.png                                   window icon PNG
  GET  /index.html                                    the API's own web UI
  GET  /quit                     [?confirm=menu|switchscreen]  MUTATING (quits ES / opens quit menu)
  GET  /shutdown                                      MUTATING (shuts down the device)
  GET  /restart                                       MUTATING (reboots)
  GET  /emukill                                       MUTATING (kills the running emulator)
  GET  /caps                                          {"Version":..., "SortName": bool}
  GET  /systems                                       array of every system
  GET  /runningGame                                   the running game, or {"msg":"NO GAME RUNNING"}
                                                      (ignores ?localpaths)
  GET  /isIdle                                        [true]/[false] - no scrape/hash/update running
  GET  /systems/{system}/logo                         the system's theme logo image bytes
  GET  /systems/{system}/games                        every game in a system (media fields are
                                                      always API URLs, localpaths is hardcoded off)
  GET  /systems/{system}/games/{id}/media/{type}      the raw media file bytes
  GET  /systems/{system}/games/{id}  [?localpaths=true]  one game; with localpaths=true every media
                                                      field is the real on-device path. The only
                                                      place local paths come from.
  GET  /systems/{system}  [?localpaths=true]          one system (localpaths affects its "logo")
  GET  /reloadgames                                   MUTATING (reloads all gamelists)
  GET  /resources/{path}                              arbitrary theme/resource file
  GET  /{path}                                        catch-all -> resources/services/{path}

  POST /systems/{system}/games/{id}/media/{type}      MUTATING (uploads/replaces a media file)
  POST /systems/{system}/games/{id}                   MUTATING (overwrites game metadata)
  POST /messagebox                                    MUTATING (pops a message box in ES)
  POST /notify                                        MUTATING (shows a notification)
  POST /storage/event                                 MUTATING (feeds an event into ES)
  POST /launch                                        MUTATING (launches a ROM by path)
  POST /addgames/{system}                             MUTATING (adds/updates games from gamelist XML)
  POST /removegames/{system}                          MUTATING (deletes games and their ROM files)

This module only calls /caps, /systems, /runningGame, /systems/{s}/games and
/systems/{s}/games/{id}, all GET.

No route returns the game highlighted in the library but not launched. /runningGame only
knows a launched game, and theres no /selectedGame, cursor or event stream in the route
table. So selected_game() always returns None, and the real signal comes from ES's
game-selected script hook (esevents.py), not this module.
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

# a stuck or missing ES must never stall the UI thread waiting on these
DEFAULT_TIMEOUT = 0.8

# short on purpose: long enough that a companion redraw doesnt hit ES every frame, short
# enough that a new ROM or system shows up in a couple of seconds. running_game() is never
# cached.
CACHE_TTL = 2.0

# The media kinds (MD_PATH metadata). The 10 image/video/manual kinds are in real device JSON.
# "magazine" and "map" were never scraped here but ES's ImportMedia() handles them too, so
# theyre real kinds, just unseen.
MEDIA_KINDS = (
    "image", "thumbnail", "marquee", "fanart", "titleshot", "mix",
    "video", "manual", "cartridge", "boxback",
    "magazine", "map",
)

# Game fields that are never media. Anything else goes into Game.extra instead of being
# dropped, since ES adds fields over time.
_GAME_CORE_KEYS = frozenset({"id", "path", "name", "systemName", "desc"})

_MISSING = object()


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

@dataclass
class MediaRef:
    """One media slot (image/video/manual/...) for one game.

    `url` is always set (what ES gave us, or built from the fixed
    /systems/{s}/games/{id}/media/{kind} route). `local_path` is only set when ES gave the real
    path (?localpaths=true) and that file exists right now, so a stale gamelist entry for a
    deleted file doesnt look local.
    """

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
    extra: dict = field(default_factory=dict)  # rating, developer, publisher, genre,
                                                 # players, favorite, hidden, kidgame, playcount, lastplayed, gametime, releasedate,
                                                 # scraperId... whatever ES sends that isnt id/path/name/systemName/desc or a media kind

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


# The full route table as data, so a test can check this module never builds a request to
# anything marked mutating=True.
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

# The (method, path) pairs this client may call. Several paths have both a safe GET and a
# mutating POST, so the method is part of the key. Used by tests as a guard.
_USED_ENDPOINT_PATHS = frozenset({
    ("GET", "/caps"), ("GET", "/systems"), ("GET", "/runningGame"),
    ("GET", "/systems/{system}/games"), ("GET", "/systems/{system}/games/{id}"),
    ("GET", "/systems/{system}"),
})


# ----------------------------------------------------------------------------
# The runtime allowlist, the last check before a request leaves this process.
#
# ES accepts /quit, /shutdown, /restart, /emukill and /reloadgames as plain GETs, so "GET only"
# doesnt protect against them. _get() works out from the path string alone whether its one of
# the six routes this client uses, and refuses anything else before urllib is touched (no
# DNS, no socket).
# ----------------------------------------------------------------------------

def _is_safe_segment(seg: str) -> bool:
    """True if `seg` is safe as the whole {system} or {id} part of an allowed path, meaning it
    cant add path segments or escape into another route.

    Rejects: empty, any "/", any "..", any "?" or "#", and any "%2f" (an encoded slash, checked
    as plain text since quote(..., safe="") turns a "/" into "%2F" that would slip past a plain
    "/" check).
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

# parameterised paths, most specific first (they differ by segment count so order doesnt
# matter, its just easier to read)
_SYSTEM_GAME_DETAIL_RE = re.compile(r"^/systems/([^/]+)/games/([^/]+)$")
_SYSTEM_GAMES_RE = re.compile(r"^/systems/([^/]+)/games$")
_SYSTEM_ONLY_RE = re.compile(r"^/systems/([^/]+)$")


def _path_is_allowed(path_only: str) -> bool:
    """path_only is the path with any query string already stripped. True only for the 6 shapes in
    _USED_ENDPOINT_PATHS, with each {system}/{id} segment checked by _is_safe_segment, never by
    prefix or substring matching on the raw path.
    """
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
    """ES sends most booleans as the strings "true"/"false". Anything else (missing, a real bool,
    garbage) becomes None instead of a guess.
    """
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
    """Tiny per-key TTL cache. Not thread-safe, callers use one ESClient per thread."""

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
    """Builds a MediaRef from one media field's raw value, in either form ES sends:

      - "/systems/{system}/games/{id}/media/{kind}": the URL form from /systems/{s}/games and
        /runningGame. local_path is unknown.
      - anything else: a local path, only from /systems/{s}/games/{id}?localpaths=true.
        local_path is that path if it still exists, else None. The URL fallback is built from
        the fixed route either way so theres always something to fall back to.
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
    """A short-timeout, never-raising, GET-only client for ES's HTTP API. One per app is normal
    (its cache is shared), tests make fresh ones freely.
    """

    def __init__(self, base_url: str = BASE_URL, timeout: float = DEFAULT_TIMEOUT,
                 cache_ttl: float = CACHE_TTL, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # `opener` lets tests swap the transport, production uses urllib.request.urlopen.
        self._urlopen = opener or urllib.request.urlopen
        self._systems_cache = _TTLCache(cache_ttl)
        self._games_cache = _TTLCache(cache_ttl)

    # -- transport -----------------------------------------------------

    def _get(self, path: str):
        """GET base_url+path as JSON. None on any failure (ES not running, a timeout, an HTTP error, a
        non-UTF8 or non-JSON body). Never raises.

        The path is checked against _path_is_allowed() first, and anything that isnt one of the 6
        allowed routes is refused without making a socket. Thats on purpose independent of what the
        caller meant, so a future bug here cant hit /quit, /shutdown, /restart, /emukill,
        /reloadgames or a POST route.
        """
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
        """One game, with local paths wherever ES has them (?localpaths=true). This is the media path
        lookup the companion needs (media_for() is this plus just the media dict).
        """
        path = "/systems/%s/games/%s?localpaths=true" % (
            urllib.parse.quote(system, safe=""), urllib.parse.quote(game_id, safe=""),
        )
        data = self._get(path)
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return _parse_game(data, self.base_url, system_hint=system)

    def media_for(self, system: str, game_id: str) -> dict:
        """kind -> MediaRef for one game, local paths where possible, the URL as a fallback. Empty if
        the game isnt found or ES is unreachable, never raises.
        """
        detail = self.game_detail(system, game_id)
        return dict(detail.media) if detail is not None else {}

    def running_game(self) -> Optional[Game]:
        """The game ES has running, or None (nothing running and ES unreachable look the same to the
        caller on purpose).

        /runningGame always gives URLs, not paths, so this does one more lookup through
        game_detail() (which supports ?localpaths=true) and merges the paths in. If that fails the
        URL-only Game is still returned instead of turning a partial answer into nothing.
        """
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
        """Always None. No route in this ES's HTTP API reports the highlighted but not launched game
        (see the module docstring). Its here so callers can use selected_game() now and get a real
        signal later without changing, if a future ES adds one.
        """
        return None

    def clear_cache(self):
        self._systems_cache.clear()
        self._games_cache.clear()


# ----------------------------------------------------------------------------
# One default client at module level, like hud.py's module functions.
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
# CLI, for checking against the live ES on the device.
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
