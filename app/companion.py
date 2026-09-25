"""companion - the default view (Navigation v2): art / video of the game
selected in EmulationStation, or of the running game, ES-DE-companion style.

Parts (pure unless noted):
  choose_media()      which media kind to show, per companion.media_priority
  metadata_lines()    the optional title / facts / description lines
  video_render_size() / video_rects()   video sizing and SDL src/dst rects
  fs_media_paths()    ES's on-disk media naming, a fallback when ES is down
  Resolver            ES lookups (runs on a worker thread; its ESClient and
                      caches are used by that one thread only)
  VideoWorker         owns the libmpv VideoPlayer on its OWN thread: mpv's SW
                      render runs there into a back buffer, swapped under a
                      lock; the UI thread only uploads the front buffer into
                      its own XRGB8888 texture (DESIGN media budget)
  CompanionView, ManualSheet, VolumeOSD   ui widgets (drawing untested)
  CompanionController the logic that ties them together on the UI thread:
                      targets (running > selected game > selected system >
                      idle), media resolution with stale-result dropping,
                      the video start delay, and the video rules:
                      video ONLY while browsing, NEVER while a game runs,
                      stopped whenever the companion is not the active view.
                      CC2: while a game IS running, companion.in_game_display
                      picks what actually shows (art / dim / off / manual /
                      hud / clock / slideshow) - see companion_modes.py for
                      the pure decision logic and its ES-DE Companion mapping
                      (research/CC3-esde-companion.md), and
                      CompanionController._apply_in_game_mode() for the one
                      timer (ingame_timer) that drives the hud/slideshow
                      choices.

Dependencies are injected (worker submit, timers, video, image loader, ES,
manuals), so tests drive it with fakes and no native library.
"""
import collections
import logging
import os
import posixpath
import random
import re
import threading
import time

import companion_modes
import config
import es_api
import manuals
import media
import screens
import theme_colour
import ui
from ui import THEME, Button, Container, Label, Sheet, Widget

log = logging.getLogger("rp5deck.companion")

BLACK = (0.0, 0.0, 0.0, 1.0)
BAND = (0.0, 0.0, 0.0, 0.62)
ES_SETTINGS = "/storage/.config/emulationstation/es_settings.cfg"

MEDIA_FALLBACK = ("thumbnail",)          # last resort when nothing in the priority exists
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
# ES's scraped-media naming on this device (ES-API-NOTES / es fixtures):
# <romdir>/images/<stem>-<suffix>.<ext>, videos/<stem>-video.mp4
# "cartridge" and "boxback" (CC3: ES-DE Companion's "Physical Media" and
# "Box Back Cover" widgets - research/CC3-esde-companion.md) are CONFIRMED
# on this device (ES-API-NOTES.md: real `ls -la` on
# ".../images/<stem>-cartridge.png" and "...-boxback.png"), unlike
# "magazine"/"map" (in es_api.MEDIA_KINDS from ES source alone, never seen in
# a real fixture) - those two stay out of FS_SUFFIX/config.MEDIA_VALUES.
FS_SUFFIX = {"image": "image", "thumbnail": "thumb", "marquee": "marquee",
             "fanart": "fanart", "titleshot": "titleshot", "mix": "mix",
             "cartridge": "cartridge", "boxback": "boxback"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def choose_media(paths, priority, allow_video):
    """paths: kind -> existing local path. priority: companion.media_priority.
    The FIRST available kind in priority order wins. If that is "video" (and
    video is allowed) it plays, with the next available still as its poster;
    if video is not allowed it is skipped as if absent.
    Returns (video_path | None, still_path | None, still_kind | None)."""
    video = None
    still = still_kind = None
    for kind in list(priority) + [k for k in MEDIA_FALLBACK if k not in priority]:
        p = paths.get(kind)
        if not p:
            continue
        if kind == "video":
            if allow_video and video is None and still is None:
                video = p
            continue
        still, still_kind = p, kind
        break
    return video, still, still_kind


def fmt_year(releasedate):
    """ES releasedate '19980515T000000' -> '1998'; anything else -> ''."""
    m = re.match(r"^(\d{4})", releasedate or "")
    return m.group(1) if m and m.group(1) != "0000" else ""


def fmt_players(players):
    p = (players or "").strip()
    if not p:
        return ""
    return "1 player" if p == "1" else "%s players" % p


def fmt_rating(rating):
    """ES rating '0.7' (0..1) -> 'Rating 70%'."""
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return ""
    if r <= 0:
        return ""
    return "Rating %d%%" % int(round(min(1.0, r) * 100))


def metadata_lines(info, show):
    """(title, facts, description) for the companion's text band, honouring
    companion.show_metadata.*; each is "" when hidden or unknown."""
    show = show or {}
    title = info.get("title", "") if show.get("title", True) else ""
    facts = []
    if show.get("year", True):
        facts.append(fmt_year(info.get("releasedate")))
    if show.get("developer", True):
        facts.append(info.get("developer") or "")
    if show.get("players", True):
        facts.append(fmt_players(info.get("players")))
    if show.get("rating", False):
        facts.append(fmt_rating(info.get("rating")))
    if show.get("playtime", False):
        facts.append(companion_modes.fmt_playtime({"playcount": info.get("playcount")}))
    desc = " ".join((info.get("desc") or "").split()) if show.get("description", False) else ""
    return title, "  ·  ".join(f for f in facts if f), desc


def video_render_size(native, box):
    """Render size for mpv: the video's own size, shrunk (never grown) to fit
    the box - the GPU scales it up, which is free; mpv scaling is not."""
    nw, nh = native
    bw, bh = box
    if nw <= 0 or nh <= 0 or bw <= 0 or bh <= 0:
        return max(1, int(bw)), max(1, int(bh))
    s = min(1.0, bw / float(nw), bh / float(nh))
    return max(2, int(round(nw * s))) & ~1, max(2, int(round(nh * s))) & ~1


def video_rects(fw, fh, box, mode):
    """SDL src/dst rects (x, y, w, h) to draw an fw x fh frame into box per
    companion.image_fit: fit = letterboxed, fill = stretched, crop = covered."""
    bx, by, bw, bh = box
    if fw <= 0 or fh <= 0 or bw <= 0 or bh <= 0:
        return (0, 0, max(0, fw), max(0, fh)), tuple(box)
    mode = media.norm_mode(mode)
    if mode == media.FILL:
        return (0, 0, fw, fh), (bx, by, bw, bh)
    if mode == media.FIT:
        s = min(bw / float(fw), bh / float(fh))
        dw, dh = fw * s, fh * s
        return (0, 0, fw, fh), (bx + (bw - dw) / 2.0, by + (bh - dh) / 2.0, dw, dh)
    s = max(bw / float(fw), bh / float(fh))           # crop / cover
    sw, sh = bw / s, bh / s
    return ((fw - sw) / 2.0, (fh - sh) / 2.0, sw, sh), (bx, by, bw, bh)


def fs_media_paths(rom_path, isfile=os.path.isfile):
    """kind -> path for media that exist under ES's naming convention."""
    if not rom_path:
        return {}
    pp = posixpath                          # device paths, whatever the host OS
    d = pp.dirname(rom_path)
    stem = pp.splitext(pp.basename(rom_path.rstrip("/")))[0]
    out = {}
    for kind, suffix in FS_SUFFIX.items():
        for ext in IMAGE_EXTS:
            p = pp.join(d, "images", "%s-%s%s" % (stem, suffix, ext))
            if isfile(p):
                out[kind] = p
                break
    v = pp.join(d, "videos", "%s-video.mp4" % stem)
    if isfile(v):
        out["video"] = v
    return out


ES_VIDEO_AUDIO_DEFAULT = True     # ES Settings.cpp: VideoAudio defaults to true (R6, RV4-M1)


def video_muted(setting, es_settings_path=ES_SETTINGS):
    """companion.video_audio -> mute? follow_es reads ES's VideoAudio from
    es_settings.cfg. ES only writes a setting that differs from its default,
    so an ABSENT key means ES's default (true: audio on, so not muted,
    RV4-M1); only an explicit value="false" mutes. An UNREADABLE file means
    muted (never surprise-loud when we cannot tell)."""
    if setting == "unmuted":
        return False
    if setting == "muted":
        return True
    try:
        with open(es_settings_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return True
    m = re.search(r'<bool\s+name="VideoAudio"\s+value="([^"]*)"', text)
    if m is None:
        return not ES_VIDEO_AUDIO_DEFAULT
    return m.group(1).strip().lower() == "false"


# ---------------------------------------------------------------------------
# Targets and resolved info
# ---------------------------------------------------------------------------
Target = collections.namedtuple("Target", "kind system rom_path name game running")
Target.__new__.__defaults__ = ("", "", "", None, False)
Target.__doc__ = "kind: 'game' | 'system'. game: an es_api.Game when already known."


def target_key(t):
    if t is None:
        return None
    return (t.kind, t.system, t.rom_path if t.kind == "game" else "", t.running)


class Resolver:
    """Target -> info dict (title, metadata, local media paths, manual,
    logo). ES first (the per-game ?localpaths route), the on-disk naming
    convention as a fallback. Used from ONE worker thread."""

    def __init__(self, es=None, find_manual=None, isfile=os.path.isfile, index_ttl=120.0,
                 clock=time.monotonic):
        self.es = es if es is not None else es_api.ESClient()
        self.find_manual = find_manual or manuals.find_manual
        self.isfile = isfile
        self.index_ttl = index_ttl
        self.clock = clock
        self._index = {}                        # system -> (expiry, {path: Game}, {basename: Game})
        self._detail = collections.OrderedDict()   # (system, id) -> Game
        self._manual = collections.OrderedDict()   # rom path -> path | None
        self._logos = {}
        self._bg_theme = {}                      # CC4: system -> (bg_rgba, fg_rgba) | None

    def _rom_index(self, system):
        hit = self._index.get(system)
        now = self.clock()
        if hit and hit[0] > now:
            return hit[1], hit[2]
        games = self.es.games(system) if system else []
        by_path = {g.rom_path: g for g in games if g.rom_path}
        by_base = {}
        for g in games:
            if g.rom_path:
                by_base.setdefault(os.path.basename(g.rom_path), g)
        if games:                                   # never cache "ES was down"
            self._index[system] = (now + self.index_ttl, by_path, by_base)
        return by_path, by_base

    def game_for(self, system, rom_path):
        by_path, by_base = self._rom_index(system)
        return by_path.get(rom_path) or by_base.get(os.path.basename(rom_path or ""))

    def detail(self, game):
        key = (game.system, game.id)
        g = self._detail.get(key)
        if g is not None:
            self._detail.move_to_end(key)
            return g
        g = self.es.game_detail(game.system, game.id) if game.id else None
        if g is not None:
            self._detail[key] = g
            while len(self._detail) > 64:
                self._detail.popitem(last=False)
        return g

    def manual_for(self, rom_path, system):
        if rom_path in self._manual:
            return self._manual[rom_path]
        try:
            p = self.find_manual(rom_path, system)
        except Exception:           # noqa: BLE001 - manuals.find_manual never raises; be sure
            log.exception("find_manual")
            p = None
        p = os.fspath(p) if p else None
        self._manual[rom_path] = p
        while len(self._manual) > 256:
            self._manual.popitem(last=False)
        return p

    def system_logo(self, system):
        """Local path of the theme's logo for a system (ES /systems/{s}
        ?localpaths=true - es_api has no public call for it, so this uses its
        guarded _get, which allows exactly that route)."""
        if system in self._logos:
            return self._logos[system]
        path = None
        try:
            import urllib.parse
            data = self.es._get("/systems/%s?localpaths=true" % urllib.parse.quote(system, safe=""))
            logo = data.get("logo") if isinstance(data, dict) else None
            if isinstance(logo, str) and logo.startswith("/") and self.isfile(logo):
                path = logo
        except Exception:           # noqa: BLE001
            log.exception("system logo")
        if path is not None:
            self._logos[system] = path
        return path

    def system_bg_color_theme(self, system, logo_path):
        """CC4: (bg_rgba, fg_rgba) from the installed ES theme's own XML
        (theme_colour.theme_background_color()), keyed off the SAME logo
        path system_logo() already fetched (its directory tree is walked
        upward to find the theme's theme.xml - theme_colour.find_theme_root)
        - no extra ES call. None (never cached, like system_logo() above -
        ES/the theme file might come back later) if there is no logo path
        yet, no theme.xml reachable from it, or the theme has nothing
        usable."""
        if system in self._bg_theme:
            return self._bg_theme[system]
        result = None
        if logo_path:
            try:
                theme_root = theme_colour.find_theme_root(logo_path, isfile=self.isfile)
                if theme_root:
                    result = theme_colour.theme_background_color(theme_root, system)
            except Exception:           # noqa: BLE001 - show the system, just without a themed bg
                log.exception("theme bg colour %s", system)
        if result is not None:
            self._bg_theme[system] = result
        return result

    def resolve(self, target):
        if target.kind == "system":
            return self._resolve_system(target)
        return self._resolve_game(target)

    def _resolve_system(self, t):
        info = {"kind": "system", "system": t.system, "title": t.system, "media": {},
                "manual": None, "logo": None, "total_games": None}
        for s in self.es.systems():
            if s.name == t.system:
                info["title"] = s.fullname or s.name
                info["total_games"] = s.visible_games or s.total_games
                break
        info["logo"] = self.system_logo(t.system) if t.system else None
        return info

    def _resolve_game(self, t):
        g = t.game
        if g is None and t.system and t.rom_path:
            g = self.game_for(t.system, t.rom_path)
        detail = None
        if g is not None:
            has_local = any(ref.local_path for ref in g.media.values())
            detail = g if has_local else self.detail(g)
        src = detail or g
        paths = {}
        if src is not None:
            paths = {k: ref.local_path for k, ref in src.media.items() if ref.local_path}
        rom = t.rom_path or (src.rom_path if src is not None else "")
        for k, p in fs_media_paths(rom, self.isfile).items():
            paths.setdefault(k, p)
        extra = src.extra if src is not None else {}
        manual = self.manual_for(rom, t.system) if rom else None
        if manual is None and paths.get("manual"):
            manual = paths["manual"]
        return {
            "kind": "game", "system": t.system or (src.system if src else ""),
            "rom_path": rom, "running": t.running,
            "title": (src.name if src is not None and src.name else t.name) or
                     os.path.splitext(os.path.basename(rom))[0],
            "desc": src.desc if src is not None else "",
            "releasedate": extra.get("releasedate", ""),
            "developer": extra.get("developer", ""),
            "players": extra.get("players", ""),
            "rating": extra.get("rating", ""),
            "playcount": extra.get("playcount", ""),
            "media": paths, "manual": manual,
        }

    def random_art(self, rng=random):
        """A random still from the library, for the idle slideshow."""
        systems = [s for s in self.es.systems() if (s.visible_games or s.total_games)
                   and s.gamesystem is not False and not s.collection]
        rng.shuffle(systems)
        for s in systems[:3]:
            games = self.es.games(s.name)
            for g in rng.sample(games, min(6, len(games))):
                paths = fs_media_paths(g.rom_path, self.isfile)
                for k in ("fanart", "image", "mix", "titleshot", "marquee"):
                    if paths.get(k):
                        return paths[k]
        return None

    def system_art(self, system, rng=random):
        """A random still from ONE system's library, for the in-game
        slideshow (companion.in_game_display == "slideshow", CC2). Unlike
        random_art() (the idle slideshow: whole library, a few systems),
        this is scoped to the system of the game that is currently running
        - the owner asked specifically for "a slideshow of the SYSTEM's
        art" while playing, not a whole-library shuffle."""
        if not system:
            return None
        games = self.es.games(system)
        if not games:
            return None
        for g in rng.sample(games, min(8, len(games))):
            paths = fs_media_paths(g.rom_path, self.isfile)
            for k in ("fanart", "image", "mix", "titleshot", "marquee"):
                if paths.get(k):
                    return paths[k]
        return None


# ---------------------------------------------------------------------------
# Video: a dedicated thread owns the player; the UI thread only uploads
# ---------------------------------------------------------------------------
Frame = collections.namedtuple("Frame", "gen seq w h stride address")


class _FrameView:
    """Context manager for VideoWorker.frame(): holds the buffer lock."""

    def __init__(self, worker):
        self.w = worker

    def __enter__(self):
        self.w._lock.acquire()
        self.w._notify_pending = False
        m = self.w._meta
        return m if (m is not None and m.gen == self.w.gen) else None

    def __exit__(self, *exc):
        self.w._lock.release()


class VideoWorker:
    """Plays one companion video. All VideoPlayer calls happen on this
    object's thread (media.py's one-thread rule); mpv's wake callback only
    sets an Event. Frames are rendered into a back buffer and swapped under
    a lock; notify() (called on the worker thread - it must only post to the
    UI loop) says a new frame is ready. play()/stop() bump a generation so a
    frame of the previous video can never be shown after stop().

    stop() CLOSES the player (frees libmpv's threads and decoder); play()
    after that builds a new one. A muted video gets mpv's ao=null, so no
    audio output (PipeWire stream, audio decode) is created at all; since ao
    is fixed at creation, a change of mute rebuilds the player."""

    def __init__(self, notify, factory=None, log_fn=None):
        self._notify = notify
        self._factory = factory or (lambda path, w, h, **kw: media.VideoPlayer(path, w, h, **kw))
        self._log = log_fn or log.info
        self._q = collections.deque()
        self._evt = threading.Event()
        self._lock = threading.Lock()
        self._front = self._back = None
        self._stride = 0
        self._meta = None
        self._seq = 0
        self._notify_pending = False
        self.gen = 0
        self.state = "idle"             # idle | playing | error | closed (worker's view)
        self.error = None
        self.path = None
        self.frames = 0
        self.players_created = 0
        self.players_closed = 0
        self._thread = threading.Thread(target=self._run, name="rp5deck-video", daemon=True)
        self._thread.start()

    # -- UI thread ---------------------------------------------------------
    def play(self, path, box_w, box_h, loop=True, mute=True):
        with self._lock:
            self.gen += 1
            g = self.gen
            self._meta = None
        self.path = path
        self._q.append(("play", g, path, int(box_w), int(box_h), bool(loop), bool(mute)))
        self._evt.set()
        return g

    def stop(self):
        with self._lock:
            self.gen += 1
            self._meta = None
        self.path = None
        self._q.append(("stop",))
        self._evt.set()

    def frame(self):
        """with worker.frame() as f: -> Frame of the CURRENT generation or None."""
        return _FrameView(self)

    def shutdown(self, timeout=3.0):
        self._q.append(("quit",))
        self._evt.set()
        self._thread.join(timeout)

    def alive(self):
        return self._thread.is_alive()

    # -- worker thread -------------------------------------------------------
    def _alloc(self, w, h):
        stride = media.stride_for(w, 4, media.SW_ALIGN)
        with self._lock:
            self._front = media.AlignedBuffer(stride * h, media.SW_ALIGN)
            self._back = media.AlignedBuffer(stride * h, media.SW_ALIGN)
            self._stride = stride
            self._meta = None

    def _close(self, player, why):
        if player is None:
            return None
        try:
            player.close()
        except Exception:           # noqa: BLE001
            log.exception("video close")
        self.players_closed += 1
        self._log("video: player closed (%s)" % why)
        return None

    def _run(self):
        player = None
        player_muted = None
        pgen = 0
        box = (0, 0)
        sized = False
        while True:
            self._evt.wait(0.5 if player is not None else None)
            self._evt.clear()
            while self._q:
                cmd = self._q.popleft()
                if cmd[0] == "quit":
                    self._close(player, "shutdown")
                    self.state = "closed"
                    return
                if cmd[0] == "stop":
                    player = self._close(player, "stop")
                    self.state = "idle"
                    continue
                _, g, path, bw, bh, loop, mute = cmd
                box, pgen, sized = (bw, bh), g, False
                if player is not None and player_muted != mute:
                    player = self._close(player, "audio output changes (mute %s)" % mute)
                try:
                    if player is None:
                        player = self._factory(path, max(2, bw), max(2, bh), loop=loop,
                                               mute=mute, ao="null" if mute else None,
                                               mode="fill", wake=self._evt.set)
                        player_muted = mute
                        self.players_created += 1
                    else:
                        player.set_loop(loop)
                        player.set_mute(mute)
                        player.load(path)
                    self.state, self.error = "playing", None
                    self._log("video: play %s (gen %d)" % (path, g))
                except Exception as e:  # noqa: BLE001 - no libmpv on the PC, bad file, ...
                    self.state, self.error = "error", str(e)
                    self._log("video: cannot play %s: %s" % (path, e))
                    player = self._close(player, "error")
            if player is None:
                continue
            try:
                if getattr(player, "state", None) == "error":
                    self.state, self.error = "error", getattr(player, "error", None)
                    player = self._close(player, "playback error: %s" % self.error)
                    continue
                if not player.poll_frame():
                    continue
                if not sized:
                    ns = player.native_size()
                    if not ns:
                        continue
                    rw, rh = video_render_size(ns, box)
                    if (rw, rh) != (player.w, player.h):
                        player.resize(rw, rh)
                    self._alloc(rw, rh)
                    sized = True
                    continue            # the next frame renders at the new size
                w, h = player.w, player.h
                player.render_into(self._back, self._stride, w, h)
                self.frames += 1
                with self._lock:
                    if pgen != self.gen:
                        continue
                    self._front, self._back = self._back, self._front
                    self._seq += 1
                    self._meta = Frame(pgen, self._seq, w, h, self._stride, self._front.address)
                    fire = not self._notify_pending
                    self._notify_pending = True
                if fire:
                    self._notify()
            except Exception as e:      # noqa: BLE001
                self.state, self.error = "error", str(e)
                log.exception("video worker")
                player = self._close(player, "exception")


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
def icon_book(g, r, color):
    x, y, w, h = r
    t = max(4, w * 0.07)
    g.stroke_round_rect((x + w * 0.08, y + h * 0.15, w * 0.4, h * 0.7), 6, color, t)
    g.stroke_round_rect((x + w * 0.52, y + h * 0.15, w * 0.4, h * 0.7), 6, color, t)


class PullTab(Button):
    """The grab handle at the top edge: a tap opens the Command Center (the
    swipe-down that starts here is claimed by the gesture recogniser first).
    It is also the way in when swipe-down is disabled and no hardware button
    is bound - otherwise Settings would be unreachable."""

    def draw(self, g):
        x, y, w, h = self.rect
        c = THEME["text"] if self.pressed else (1.0, 1.0, 1.0, 0.55)
        g.round_rect((x + w / 2 - 110, y + 22, 220, 14), 7, c)


class VolumeOSD(Widget):
    """Brief volume overlay on the companion view (audio.show_volume_overlay)."""

    def __init__(self, name="companion.osd"):
        Widget.__init__(self, name=name)
        self.visible = False
        self.volume = None
        self.muted = False

    def set_level(self, volume, muted):
        if (volume, muted) != (self.volume, self.muted):
            self.volume, self.muted = volume, muted
            self.invalidate()

    def draw(self, g):
        x, y, w, h = self.rect
        g.round_rect(self.rect, 36, (0.08, 0.09, 0.12, 0.92))
        screens.icon_speaker(g, (x + 40, y + (h - 90) / 2, 90, 90), THEME["text"],
                             bool(self.muted))
        v = 0.0 if self.volume is None else max(0.0, min(1.0, self.volume))
        tx0, tx1, cy = x + 170, x + w - 220, y + h / 2
        g.round_rect((tx0, cy - 14, tx1 - tx0, 28), 14, THEME["track"])
        if not self.muted:
            g.round_rect((tx0, cy - 14, max(28, (tx1 - tx0) * v), 28), 14, THEME["accent"])
        text = "Muted" if self.muted else ("%d%%" % int(round(v * 100))
                                           if self.volume is not None else "…")
        g.text(text, (x + w - 200, y, 170, h), 60, THEME["text"], True, "center")


class ManualSheet(Sheet):
    """Full-panel manual viewer: two-page spreads, Prev / Next, Back."""

    def __init__(self, h, name="manual"):
        Sheet.__init__(self, "Manual", on_close=h.close_manual, name=name)
        self.prev = self.add(Button("Prev", on_click=lambda: h.manual_page(-1),
                                    name=name + ".prev", size=44))
        self.next = self.add(Button("Next", on_click=lambda: h.manual_page(1),
                                    name=name + ".next", size=44))
        self.msg = self.body.add(Label("", size=48, bold=True, align="center",
                                       name=name + ".message"))
        self.pages = []             # [(Image, x, y)]
        self.visible = False

    def page_height(self):
        return max(1, int(self.body.rect[3]) - 16)

    def body_rect(self):
        return self.body.rect

    def layout(self, rect):
        Sheet.layout(self, rect)
        x, y, w, h = rect
        self.title.set_rect((x + 320, y, max(0, w - 980), self.HEADER_H))
        self.prev.set_rect((x + w - 640, y + 10, 300, self.HEADER_H - 20))
        self.next.set_rect((x + w - 320, y + 10, 300, self.HEADER_H - 20))
        bx, by, bw, bh = self.body.rect
        self.msg.set_rect((bx, by + bh * 0.4, bw, 80))

    def set_nav(self, title, can_prev, can_next):
        self.title.set_text(title)
        self.prev.set_enabled(can_prev)
        self.next.set_enabled(can_next)

    def set_message(self, text):
        self.msg.set_text(text)
        if text:
            self.pages = []
        self.invalidate()

    def set_pages(self, pages):
        self.pages = list(pages)
        self.msg.set_text("")
        self.invalidate()

    def draw(self, g):
        Container.draw(self, g)
        x, y, w, h = self.rect
        g.fill_rect((x, y + self.HEADER_H - 2, w, 2), THEME["line"])
        for img, px, py in self.pages:
            g.image(img, px, py)


class CompanionView(Container):
    """Covers the whole panel (the focus rule). Draws art or leaves a
    transparent hole where the video texture (drawn UNDER the UI) shows."""

    def __init__(self, h, name="companion"):
        Container.__init__(self, name=name)
        self.h = h
        self.mode = "idle"              # idle | game | system
        self.idle_mode = "clock"
        self.image = None               # media.Image, already fitted
        self.image_xy = (0, 0)
        self.logo = None
        self.logo_xy = (0, 0)
        self.title = self.facts = self.desc = ""
        self.badge = ""
        self.dim = 0.15
        self.video_on = False
        self.video_box = None
        self.clock_text = self.date_text = ""
        self.warning = ""               # one line, top left (name guard at startup)
        self.manual_btn_wanted = False
        self.bg_color = BLACK           # CC4: system-carousel background (fill behind the logo)
        self.fg_color = THEME["text"]   # CC4: matching readable text colour
        self.tab = self.add(PullTab("", on_click=h.open_command_center, name="companion.tab"))
        self.manual_btn = self.add(Button("Manual", on_click=h.open_manual,
                                          name="companion.manual", size=44, icon=icon_book))
        self.manual_btn.visible = False
        self.osd = self.add(VolumeOSD())
        self.manual = self.add(ManualSheet(h))

    # -- state setters (each invalidates) ----------------------------------
    def art_box(self):
        return tuple(self.rect)

    def show_idle(self, idle_mode, image=None):
        self.mode, self.idle_mode = "idle", idle_mode
        self.image, self.logo = image, None
        if image is not None:
            self.image_xy = self._placed(image)
        self.title = self.facts = self.desc = self.badge = ""
        self.manual_btn_wanted = False
        self.manual_btn.set_visible(False)
        self.set_bg_color(None)         # CC4: idle/clock is never themed - back to BLACK
        self.invalidate()

    def show_info(self, info, image, logo, lines, dim, bg=None):
        self.mode = info.get("kind", "game")
        self.image, self.logo = image, logo
        if image is not None:
            self.image_xy = self._placed(image)
        if logo is not None:
            x, y, w, h = self.rect
            lx, ly = media.place(logo.w, logo.h, w, int(h * 0.6))
            self.logo_xy = (x + lx, y + ly + int(h * 0.05))
        self.title, self.facts, self.desc = lines
        self.badge = "Now playing" if info.get("running") else ""
        self.dim = dim
        self.manual_btn_wanted = bool(info.get("manual"))
        self.manual_btn.set_visible(self.manual_btn_wanted and not self.manual.visible)
        self.set_bg_color(bg if self.mode == "system" else None)   # CC4
        self.invalidate()

    def logo_box(self):
        """CC4: (x, y, w, h) of the currently-placed logo image, or None -
        NOT used to exclude anything from a DP-1 screenshot (this is the
        BOTTOM screen's own copy of the logo, a different screen/theme
        layout than ES's own carousel on DP-1 - see theme_colour.py's
        default_exclude_box for that) - exposed for introspection/tests."""
        if self.logo is None:
            return None
        x, y = self.logo_xy
        return (x, y, self.logo.w, self.logo.h)

    def set_bg_color(self, bg):
        """CC4: bg = (bg_rgba, fg_rgba) or None (-> BLACK/THEME["text"],
        the pre-CC4 look). Called synchronously from show_info() when the
        colour is already known (theme source, or a cached sample), and
        asynchronously later by the controller once a debounced DP-1
        sample comes back (sample source, first visit to a system)."""
        bg_color, fg_color = bg if bg is not None else (BLACK, THEME["text"])
        if (bg_color, fg_color) != (self.bg_color, self.fg_color):
            self.bg_color, self.fg_color = bg_color, fg_color
            self.invalidate()

    def _placed(self, img):
        x, y, w, h = self.rect
        px, py = media.place(img.w, img.h, w, h)
        return (x + px, y + py)

    def set_video(self, on, box=None):
        on = bool(on)
        if on != self.video_on or (on and box != self.video_box):
            self.video_on, self.video_box = on, (box if on else None)
            self.invalidate()

    def set_manual_open(self, is_open):
        """The manual sheet covers the panel; the pull tab and Manual button
        underneath are hidden too, so a tap on the sheet's blank area can
        never fall through to them (Container.hit tries every child)."""
        self.manual.set_visible(is_open)
        self.tab.set_visible(not is_open)
        self.manual_btn.set_visible(not is_open and self.manual_btn_wanted)
        self.invalidate()

    def set_warning(self, text):
        if text != self.warning:
            self.warning = text
            self.invalidate()

    def set_clock(self, clock, date):
        if (clock, date) != (self.clock_text, self.date_text):
            self.clock_text, self.date_text = clock, date
            if self.mode == "idle" and (self.idle_mode == "clock" or self.image is None):
                self.invalidate()

    # -- layout / drawing ------------------------------------------------------
    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        self.tab.set_rect((x + w / 2 - 240, y, 480, 110))
        self.manual_btn.set_rect((x + w - 400, y + h - 170, 360, 140))
        self.osd.set_rect((x + (w - 960) / 2, y + 140, 960, 170))
        self.manual.layout(rect)
        self.invalidate()

    def band_height(self):
        n = (1 if self.title else 0) + (1 if self.facts else 0) + (1 if self.badge else 0)
        hgt = 0
        if self.badge:
            hgt += 50
        if self.title:
            hgt += 80
        if self.facts:
            hgt += 56
        if self.desc:
            hgt += 3 * 46 + 10
        return (hgt + 50) if (n or self.desc) else 0

    def draw(self, g):
        x, y, w, h = self.rect
        g.fill_rect(self.rect, self.bg_color)      # CC4: was hard-coded BLACK
        if self.mode == "idle":
            if self.idle_mode == "slideshow" and self.image is not None:
                g.image(self.image, *self.image_xy)
            elif self.idle_mode in ("clock", "slideshow"):     # slideshow without art: clock
                g.text(self.clock_text, (x, y + h * 0.28, w, 260), 220, THEME["text"], True,
                       "center")
                g.text(self.date_text, (x, y + h * 0.28 + 270, w, 80), 56, THEME["dim"], False,
                       "center")
        else:
            art = False
            if self.video_on and self.video_box:
                g.clear_rect(self.video_box)
                art = True
            elif self.image is not None:
                g.image(self.image, *self.image_xy)
                art = True
            elif self.logo is not None:
                g.image(self.logo, *self.logo_xy)
            if art and self.dim > 0:
                g.fill_rect(self.rect, (0.0, 0.0, 0.0, min(1.0, self.dim)))
            bh = self.band_height()
            if art or self.logo is not None:
                if bh:
                    g.fill_rect((x, y + h - bh, w, bh), BAND)
                    self._draw_text(g, x + 50, y + h - bh + 25, w - 500)
            elif self.title:
                # no art at all: the text IS the view
                g.text(self.title, (x + 60, y + h * 0.3, w - 120, 120), 88, self.fg_color,
                       True, "center")
                if self.facts:
                    g.text(self.facts, (x + 60, y + h * 0.3 + 130, w - 120, 70), 48,
                           self.fg_color, False, "center")
                if self.badge:
                    g.text(self.badge, (x + 60, y + h * 0.3 - 80, w - 120, 60), 44,
                           THEME["accent"], True, "center")
        if self.warning:
            # left of the pull tab (which sits at the top centre)
            g.round_rect((x + 16, y + 16, w / 2 - 280, 56), 12, (0.0, 0.0, 0.0, 0.7))
            g.text(self.warning, (x + 32, y + 20, w / 2 - 312, 48), 30, THEME["warn"], True)
        for c in self.children:
            c.paint(g)

    def _draw_text(self, g, tx, ty, tw):
        if self.badge:
            g.text(self.badge, (tx, ty, tw, 44), 40, THEME["accent"], True)
            ty += 50
        if self.title:
            g.text(self.title, (tx, ty, tw, 76), 64, THEME["text"], True)
            ty += 80
        if self.facts:
            g.text(self.facts, (tx, ty, tw, 50), 40, THEME["dim"])
            ty += 56
        if self.desc:
            g.text_block(self.desc, (tx, ty + 6, tw, 3 * 46), 36, THEME["text"], False, 3)


# ---------------------------------------------------------------------------
# The controller (UI thread)
# ---------------------------------------------------------------------------
def _es_start_ticks():
    """The running ES's start time in clock ticks since boot, or None."""
    import es_health
    probe = es_health.Probe()
    pids = probe.es_pids()
    if len(pids) != 1:
        return None
    st = probe.stat(pids[0])
    return st["starttime"] if st else None


class CompanionController:
    """UI-thread logic behind CompanionView. Inputs: on_es_event(),
    set_active(), config_changed(), tick_clock(), and the manual actions.

    submit(fn, *args, done=cb): runs fn on a worker and cb(result) back on
    the UI thread. timers: call_later(delay, fn) -> handle, cancel(handle).
    video: a VideoWorker (or fake). cfg_fn() -> the live config dict."""

    def __init__(self, view, submit, timers, video, cfg_fn, resolver=None,
                 load_image=None, manuals_mod=None, clock=time.monotonic, wall=time.localtime,
                 es_settings=ES_SETTINGS, hud_fn=None, es_start_ticks=None):
        self.view = view
        self.submit = submit
        self.timers = timers
        self.video = video
        self.cfg_fn = cfg_fn
        self.resolver = resolver if resolver is not None else Resolver()
        self.load_image = load_image or media.load_image
        self.manuals = manuals_mod or manuals
        self.clock = clock
        self.wall = wall
        self.es_settings = es_settings
        self.hud_fn = hud_fn                # injected for tests; lazy-imports hud.sample otherwise
        # ES's process start (clock ticks since boot), or None: see _start_predates_es
        self.es_start_ticks = es_start_ticks if es_start_ticks is not None else _es_start_ticks
        self.stale_starts = 0               # hook game-starts dropped: their ES has exited
        self.hud_last = None                # most recent hud.sample() dict, or None
        self.ingame_timer = None            # CC2: the ONE timer behind in_game_display "hud"/"slideshow"
        self.ingame_mode_applied = None      # introspection: the mode actually shown last _resolved()
        self._last_display = (None, None, ("", "", ""))   # (image, logo, lines) of the last resolve
        self.selected = None            # Target (game or system) from ES events
        self.running = None             # Target of the running game
        self.running_confirmed = False  # the /runningGame poll has seen it
        self.active = False             # companion is the visible view in FULL mode
        self.gen = 0                    # display generation: stale worker results are dropped
        self.info = None                # resolved info of the displayed target
        self.choice = (None, None, None)
        self.video_path = None          # what the VideoWorker is (asked to be) playing
        self.video_timer = None
        self.video_starts = 0
        self.video_stops = collections.Counter()
        self.slide_timer = None
        self.shown_at = 0.0
        self.last_event = None
        self.events = collections.Counter()
        self.manual_state = None        # {"pdf", "pages", "left", "title"} while open
        self.manual_gen = 0
        self.manual_seq = 0             # bumped per page turn: stale renders are skipped
        self.manual_inflight = False    # a spread render job is queued / running
        self.manual_renders = 0         # spreads actually rendered (introspection)
        self._auto_manual = False       # manuals.open_mode == auto_on_game_start, pending
        self.es_idle = None             # ES screensaver-start / sleep in effect
        self.ignored_ends = 0           # game-ends for another ROM (RV4-N1)
        self.warning = ""
        # CC4: system-carousel background colour, "sample" source only -
        # the "theme" source is cheap enough to resolve inline in
        # _resolve_job (see there); "sample" costs a real `grim` screenshot
        # so it gets its own debounce (bg_gate) and generation counter
        # (stale captures from a system the owner already scrolled past
        # must never overwrite what is on screen now).
        self.bg_sample = theme_colour.SampleCache()
        self.bg_gate = theme_colour.SettleGate(settle_s=0.4)
        self.bg_timer = None
        self.bg_gen = 0

    # -- config ------------------------------------------------------------
    def _c(self, *path):
        return config.get_value(self.cfg_fn(), ("companion",) + path)

    # -- targets -----------------------------------------------------------
    def target(self):
        return self.running or self.selected

    def _start_predates_es(self, ev):
        """True if a hook game-start was fired by an ES that has since exited:
        the spool key's first field is the start time (clock ticks since boot)
        of the `sh -c` ES forked for the event (es-hooks/*.sh), the same clock
        as ES's own /proc/<pid>/stat starttime. Unknown either way -> False
        (keep the start: the safe direction, no video while a game may run)."""
        if ev.source != "hook" or not ev.seq:
            return False
        try:
            fired = int(ev.seq[0])
            es = self.es_start_ticks()
        except Exception:
            return False
        return es is not None and fired > 0 and fired < es

    def on_es_event(self, ev):
        import esevents
        self.events[(ev.kind, ev.source)] += 1
        self.last_event = {"kind": ev.kind, "source": ev.source, "system": ev.system,
                           "rom": ev.rom_path, "name": ev.name}
        before = target_key(self.target())
        if ev.kind == esevents.GAME_SELECTED and ev.rom_path:
            self.selected = Target("game", ev.system, ev.rom_path, ev.name)
        elif ev.kind == esevents.SYSTEM_SELECTED and ev.system:
            self.selected = Target("system", ev.system)
            self._schedule_bg_sample(ev.system)     # CC4 (source="sample" only; no-op otherwise)
        elif ev.kind == esevents.GAME_START and self._start_predates_es(ev):
            # Test day, 24 Sep: a Steam launch stops sway and ES is restarted,
            # so game-end never fires; the spool replayed the old start at the
            # next app start and "Now playing" stuck on a game that was gone.
            self.stale_starts += 1
            log.info("ES game-start (hook) for %r dropped: it predates the running ES",
                     ev.rom_path)
        elif ev.kind == esevents.GAME_START:
            # The game needs the CPU: stop video NOW, before anything else.
            self._stop_video("game-start")
            self._cancel_bg_sample()                # CC4: never sample DP-1 while a game runs
            same = self.running is not None and self.running.rom_path == ev.rom_path
            if ev.source == "poll":
                self.running_confirmed = True
                self.running = Target("game", ev.system, ev.rom_path, ev.name, ev.game, True)
            elif not same or self.running is None:
                self.running = Target("game", ev.system, ev.rom_path, ev.name, None, True)
                self.running_confirmed = False
            if self._c_manual_auto():
                self._auto_manual = True
        elif ev.kind == esevents.GAME_END:
            # A poll "end" only counts if the poll had confirmed that game;
            # a hook-reported start the poll never saw stays until game-end
            # (safe direction: no video while a game may be running). The poll
            # itself only reports an end after repeated clean "NO GAME
            # RUNNING" answers - a timeout is never an end (esevents).
            if self._end_is_for_another_game(ev):
                self.ignored_ends += 1
                log.info("ES game-end (%s) for %r ignored: the running game is %r",
                         ev.source, ev.rom_path, self.running.rom_path)
            elif ev.source == "hook" or self.running_confirmed or \
                    (self.running is not None and self.running.game is not None):
                if self.running is not None:
                    self.running = None
                    self.running_confirmed = False
                    if self.manual_state is not None:
                        self.close_manual()
        elif ev.kind in esevents.IDLE_ON:
            # ES's screensaver started or the device is going to sleep: no
            # video decode (and no slideshow) behind a blanked screen (RV1-M5)
            self.es_idle = ev.kind
            self._stop_video(ev.kind)
            self._cancel_slides()
            self._cancel_ingame_timer()
        elif ev.kind in esevents.IDLE_OFF:
            if self.es_idle is not None:
                self.es_idle = None
                self._update_video()
                self._arm_slides()
                self._resume_ingame_mode()
        if target_key(self.target()) != before:
            self.refresh()

    def _end_is_for_another_game(self, ev):
        """RV4-N1: a game-end whose ROM differs from the tracked running
        game (both known) is late or stale - ES runs one game at a time."""
        r = self.running
        if r is None or not ev.rom_path or not r.rom_path:
            return False
        if not (ev.rom_path.startswith("/") and r.rom_path.startswith("/")):
            return False                    # a poll key can be an id, not a path
        return posixpath.normpath(ev.rom_path) != posixpath.normpath(r.rom_path)

    def set_warning(self, text):
        """A one-line warning on the companion view ("" clears it); main.py
        shows name_guard's startup result here."""
        self.warning = text or ""
        self.view.set_warning(self.warning)

    def _c_manual_auto(self):
        return config.get_value(self.cfg_fn(), ("manuals", "open_mode")) == "auto_on_game_start"

    # -- CC4: system-carousel background colour, "sample" source ------------
    # (the "theme" source is resolved inline in _resolve_job - it is just a
    # couple of file reads, no debounce needed. "sample" is a real `grim`
    # screenshot, so it is debounced (bg_gate, ~400ms of no further
    # scrolling) and generation-guarded the same way image resolution
    # already is (self.gen) - a capture that was still running when the
    # owner scrolled on must never overwrite what the screen shows now.)
    def _cancel_bg_sample(self):
        if self.bg_timer is not None:
            self.timers.cancel(self.bg_timer)
            self.bg_timer = None
        self.bg_gate.cancel()
        self.bg_gen += 1                # invalidate any capture already in flight

    def _schedule_bg_sample(self, system):
        self._cancel_bg_sample()
        if (self._c("system_bg_source") or "sample") != "sample":
            return
        cached = self.bg_sample.cached(system)
        if cached is not None:
            self._apply_bg_if_current(system, cached)
            return                      # already know this system's colour - no capture needed
        self.bg_gate.note(system, self.clock())
        gen = self.bg_gen
        self.bg_timer = self.timers.call_later(self.bg_gate.settle_s,
                                                lambda: self._bg_settle_tick(gen, system))

    def _bg_settle_tick(self, gen, system):
        self.bg_timer = None
        if gen != self.bg_gen or self.bg_gate.ready(self.clock()) != system:
            return                       # superseded by a later scroll, or already cancelled
        if not theme_colour.should_sample(self._c("system_bg_source") or "sample",
                                          self.running is not None):
            return
        self.submit(self.bg_sample.capture, system, done=lambda res: self._bg_captured(gen, system, res))

    def _bg_captured(self, gen, system, result):
        if gen != self.bg_gen or result is None:
            return
        self._apply_bg_if_current(system, result)

    def _apply_bg_if_current(self, system, bg):
        if self.info is not None and self.info.get("kind") == "system" \
                and self.info.get("system") == system:
            self.view.set_bg_color(bg)

    # -- activity / video gate ------------------------------------------------
    def set_active(self, active):
        active = bool(active)
        if active == self.active:
            return
        self.active = active
        if not active:
            self._stop_video("inactive")
            self._cancel_slides()
            self._cancel_ingame_timer()
        else:
            self._update_video()
            self._arm_slides()
            # CC2 bug (test day, 24 Sep): the Command Center/Settings sheet
            # covering the companion makes it inactive; _ingame_active()
            # gates every hud/slideshow tick on self.active, so a tick that
            # fires while covered bails out WITHOUT re-arming itself
            # (_cancel_ingame_timer() above does the same going the other
            # way). Nothing used to restart that loop when the sheet closed
            # and the companion became active again - hud/slideshow just
            # stayed dead until some unrelated event (game end/start) forced
            # a full refresh(). _resume_ingame_mode() (already used for the
            # ES-screensaver case below) re-applies the CURRENT setting
            # against the last resolved info, which re-arms the timer.
            self._resume_ingame_mode()

    def video_allowed(self):
        return (self.active and self.running is None and self.manual_state is None
                and self.es_idle is None and bool(self._c("play_video")))

    def _wanted_video(self):
        if not self.video_allowed() or self.info is None or self.info.get("kind") != "game":
            return None
        return self.choice[0]

    def _update_video(self):
        want = self._wanted_video()
        if want is None:
            self._stop_video("not allowed")
            return
        if want == self.video_path or self.video_timer is not None:
            return
        delay = max(0.0, self._c("video_start_delay_ms") / 1000.0 -
                    (self.clock() - self.shown_at))
        self.video_timer = self.timers.call_later(delay, self._start_video)

    def _start_video(self):
        self.video_timer = None
        want = self._wanted_video()
        if want is None or want == self.video_path:
            return
        x, y, w, h = self.view.art_box()
        self.video.play(want, w, h, loop=bool(self._c("video_loop")),
                        mute=video_muted(self._c("video_audio"), self.es_settings))
        self.video_path = want
        self.video_starts += 1

    def _stop_video(self, why):
        if self.video_timer is not None:
            self.timers.cancel(self.video_timer)
            self.video_timer = None
        if self.video_path is not None:
            self.video.stop()
            self.video_path = None
            self.video_stops[why] += 1
        self.view.set_video(False)

    def video_frame_ready(self, fw, fh):
        """main.py: the first frame of the current video was uploaded."""
        if self.video_path is None:
            return None
        box = self.view.art_box()
        src, dst = video_rects(fw, fh, box, self._c("image_fit"))
        self.view.set_video(True, box)
        return src, dst

    # -- resolution ----------------------------------------------------------
    def refresh(self):
        """The displayed target changed (or its config did): resolve again."""
        self.gen += 1
        gen = self.gen
        self._stop_video("target changed")
        self._cancel_ingame_timer()
        t = self.target()
        self.shown_at = self.clock()
        if t is None:
            self.info = None
            self.choice = (None, None, None)
            self._show_idle()
            return
        self._cancel_slides()
        x, y, w, h = self.view.art_box()
        allow_video = bool(self._c("play_video")) and not t.running
        prio = self._c("media_priority")
        fit = self._c("image_fit")
        self.submit(self._resolve_job, gen, t, allow_video, prio, fit, int(w), int(h),
                    done=self._resolved)

    def _resolve_job(self, gen, t, allow_video, prio, fit, w, h):
        """Worker thread. Returns (gen, info, choice, image, logo) or None if stale."""
        if gen != self.gen:
            return None
        try:
            info = self.resolver.resolve(t)
        except Exception:           # noqa: BLE001 - show the RIGHT target, just without art
            log.exception("resolve %r", t)
            info = {"kind": t.kind, "system": t.system, "rom_path": t.rom_path,
                    "running": t.running, "media": {}, "manual": None, "logo": None,
                    "title": t.name or t.system or os.path.basename(t.rom_path or "")}
        if gen != self.gen:
            return None
        # CC4: the "theme" source is cheap (a couple of file reads, already
        # cached per system in the resolver) - resolved inline, no debounce.
        # "sample" is handled entirely by the controller (_schedule_bg_sample
        # et al.) since it needs a real timer, not just this one-shot job.
        if info.get("kind") == "system" and (self._c("system_bg_source") or "sample") == "theme":
            try:
                bg = self.resolver.system_bg_color_theme(info.get("system"), info.get("logo"))
            except Exception:       # noqa: BLE001 - show the system, just without a themed bg
                log.exception("theme bg colour %r", info.get("system"))
                bg = None
            if bg is not None:
                info["bg_color"] = bg
        choice = choose_media(info.get("media", {}), prio, allow_video)
        image = logo = None
        if choice[1]:
            try:
                image = self.load_image(choice[1], w, h, fit)
            except Exception as e:  # noqa: BLE001 - MediaError, or no native libs
                log.info("art %s: %s", choice[1], e)
        if info.get("logo"):
            try:
                logo = self.load_image(info["logo"], int(w * 0.6), int(h * 0.45), "fit")
            except Exception as e:  # noqa: BLE001 - e.g. an SVG theme logo
                log.info("logo %s: %s", info["logo"], e)
        return gen, info, choice, image, logo

    def _resolved(self, res):
        if res is None or res[0] != self.gen:
            return                      # a newer target is already on its way
        _, info, choice, image, logo = res
        self.info, self.choice = info, choice
        lines = metadata_lines(info, self._c("show_metadata") or {})
        if info.get("kind") == "system" and info.get("total_games"):
            lines = (lines[0], "%d games" % info["total_games"], "")
        self._last_display = (image, logo, lines)
        if info.get("running"):
            self._apply_in_game_mode(info, image, logo, lines)
        else:
            self._cancel_ingame_timer()
            self.ingame_mode_applied = None
            bg = None                   # CC4
            if info.get("kind") == "system":
                source = self._c("system_bg_source") or "sample"
                if source == "theme":
                    bg = info.get("bg_color")
                elif source == "sample":
                    bg = self.bg_sample.cached(info.get("system"))
            self.view.show_info(info, image, logo, lines, float(self._c("background_dim")), bg=bg)
        self._update_video()
        if self._auto_manual and info.get("running"):
            self._auto_manual = False
            # in_game_display == "manual" (just above) may already have
            # opened it this same refresh - never open it twice.
            if info.get("manual") and self.manual_state is None:
                self.open_manual()

    # -- CC2: what the bottom screen shows during a single-screen game -----
    def _in_game_mode(self, info):
        raw = self._c("in_game_display") or "art"
        return companion_modes.effective_in_game_mode(raw, bool(info.get("manual")))

    def _cancel_ingame_timer(self):
        if self.ingame_timer is not None:
            self.timers.cancel(self.ingame_timer)
            self.ingame_timer = None

    def _arm_ingame_timer(self, delay, fn):
        self._cancel_ingame_timer()
        self.ingame_timer = self.timers.call_later(delay, fn)

    def _ingame_active(self, mode):
        """True if `mode` is still the right thing to be doing right now -
        checked at the top of every tick, since none of the paths that stop
        a game/leave the companion/change the setting reach into this
        module's timer directly (RV2-style: fewer places that must
        remember to cancel something)."""
        return (self.active and self.es_idle is None and self.info is not None
                and bool(self.info.get("running")) and self._in_game_mode(self.info) == mode)

    def _apply_in_game_mode(self, info, image, logo, lines):
        """Chooses what actually appears on screen for a running
        single-screen game, per companion.in_game_display (CC2). Video is
        never a candidate here - video_allowed() already refuses it
        unconditionally while a game is running (DESIGN.md media budget);
        this only ever picks among still art, the manual, a HUD-lite
        overlay, the clock, or a slideshow of the system's own art."""
        raw = self._c("in_game_display") or "art"
        mode = companion_modes.effective_in_game_mode(raw, bool(info.get("manual")))
        self.ingame_mode_applied = mode
        dim = float(self._c("background_dim"))
        if mode == "manual":
            self._cancel_ingame_timer()
            self.view.show_info(info, image, logo, lines, dim)
            if self.manual_state is None:
                self.open_manual()
            return
        if mode == "hud":
            self.view.show_info(info, None, None,
                                (lines[0], "Reading device stats…", ""), dim)
            self._arm_ingame_timer(0.0, self._hud_tick)
            return
        if mode == "clock":
            self._cancel_ingame_timer()
            self.view.show_idle("clock")
            self.tick_clock()
            return
        if mode == "slideshow":
            # Show this game's own art right away (RP5 test day: a Mega
            # Drive game had no system_art on this device, and the ONLY
            # thing this branch used to do was arm a timer - so with
            # nothing to slide to, the screen just kept showing whatever
            # mode was applied before "slideshow" was picked, e.g. the
            # idle clock from an earlier setting change, forever). This is
            # the same "never leave stale/blank content" rule "manual"
            # already follows by falling back to art.
            self.view.show_info(info, image, logo, lines, dim)
            self._arm_ingame_timer(0.0, self._ingame_slide_tick)
            return
        if mode == "off":
            self._cancel_ingame_timer()
            self.view.show_idle("blank")
            return
        # "art" (default) or "dim"
        self._cancel_ingame_timer()
        if raw == "manual":
            # CC2 polish (test day): "manual" with no manual for this game
            # falls back to "art" by design (effective_in_game_mode) - but
            # silently showing art with no explanation looks identical to
            # having picked "art" on purpose. A one-line hint in the facts
            # row (the same slot "hud" already repurposes for its own
            # placeholder) says why, without adding any new UI.
            lines = (lines[0], "No manual for this game — showing art", "")
        self.view.show_info(info, image, logo, lines, companion_modes.in_game_dim(mode, dim))

    def _resume_ingame_mode(self):
        """After the screensaver/sleep interval ends (IDLE_OFF), replay the
        currently-running game's in-game mode from the LAST resolved
        result rather than paying for a fresh ES round trip - the target
        has not changed, only es_idle has."""
        if self.info is not None and self.info.get("running"):
            image, logo, lines = self._last_display
            self._apply_in_game_mode(self.info, image, logo, lines)

    def _hud_tick(self):
        self.ingame_timer = None
        if not self._ingame_active("hud"):
            return
        fn = self.hud_fn
        if fn is None:
            import hud as _hud
            fn = self.hud_fn = _hud.sample
        gen = self.gen

        def done(sample):
            if gen != self.gen or not self._ingame_active("hud"):
                return
            self.hud_last = sample
            _, _, lines = self._last_display
            hlines = companion_modes.hud_lines(sample)
            facts = "  ·  ".join(hlines) if hlines else "No device stats available"
            self.view.show_info(self.info, None, None, (lines[0], facts, ""),
                                float(self._c("background_dim")))
            self._arm_ingame_timer(companion_modes.HUD_INTERVAL_S, self._hud_tick)
        self.submit(_guarded(fn), done=done)

    def _ingame_slide_tick(self):
        self.ingame_timer = None
        if not self._ingame_active("slideshow"):
            return
        gen = self.gen
        system = self.info.get("system") if self.info else None
        x, y, w, h = self.view.art_box()
        fit = self._c("image_fit")

        def job():
            p = self.resolver.system_art(system)
            return gen, (self.load_image(p, int(w), int(h), fit) if p else None)

        def done(res):
            if res is None or res[0] != self.gen or not self._ingame_active("slideshow"):
                return
            # A system with no slide art (RP5 test day: a Mega Drive game,
            # confirmed on-device) leaves res[1] None: nothing to show, but
            # also nothing to CLEAR - _apply_in_game_mode() already put this
            # game's own art on screen the moment "slideshow" was picked
            # (see there), so there is no stale mode's view left behind to
            # fall back from here. Just keep retrying at the normal
            # interval in case art becomes available (e.g. a rescrape).
            if res[1] is not None:
                self.view.show_idle("slideshow", res[1])
            self._arm_ingame_timer(float(self._c("idle_slideshow_interval_s")),
                                   self._ingame_slide_tick)
        self.submit(_guarded(job), done=done)

    # -- idle ----------------------------------------------------------------
    def _show_idle(self):
        mode = self._c("idle_mode")
        self.view.show_idle(mode)
        self.tick_clock()
        self._arm_slides()

    def _arm_slides(self):
        if self.slide_timer is None and self.active and self.target() is None and \
                self.es_idle is None and self._c("idle_mode") == "slideshow":
            first = self.view.image is None
            self.slide_timer = self.timers.call_later(
                0.0 if first else float(self._c("idle_slideshow_interval_s")), self._slide)

    def _cancel_slides(self):
        if self.slide_timer is not None:
            self.timers.cancel(self.slide_timer)
            self.slide_timer = None

    def _slide(self):
        self.slide_timer = None
        if self.target() is not None or not self.active:
            return
        gen = self.gen
        x, y, w, h = self.view.art_box()
        fit = self._c("image_fit")

        def job():
            p = self.resolver.random_art()
            return gen, (self.load_image(p, int(w), int(h), fit) if p else None)

        def done(res):
            if self.target() is not None or (res is not None and res[0] != self.gen):
                return
            if res is not None and res[1] is not None:
                self.view.show_idle("slideshow", res[1])
            self._arm_slides()          # keep going even after a failed pick
        self.submit(_guarded(job), done=done)

    def tick_clock(self):
        t = self.wall()
        self.view.set_clock(time.strftime("%H:%M", t), time.strftime("%A %d %B", t))

    def config_changed(self):
        """A companion.* / manuals.* setting changed: redraw with it."""
        self._cancel_slides()
        self.refresh()

    # -- manual viewer -----------------------------------------------------------
    def open_manual(self):
        info = self.info or {}
        pdf = info.get("manual")
        if not pdf:
            return
        self._stop_video("manual")
        self.manual_gen += 1
        self.manual_seq += 1
        self.manual_state = {"pdf": pdf, "pages": None, "left": 1,
                             "title": info.get("title") or "Manual"}
        mv = self.view.manual
        mv.set_nav(self.manual_state["title"], False, False)
        mv.set_message("Opening manual…")
        self.view.set_manual_open(True)
        self._render_spread()

    def close_manual(self):
        if self.manual_state is None:
            return
        self.manual_state = None
        self.manual_gen += 1
        self.view.manual.set_pages([])
        self.view.set_manual_open(False)
        self._update_video()

    def manual_page(self, delta):
        st = self.manual_state
        if st is None or not st["pages"]:
            return
        left = st["left"] + 2 * (1 if delta > 0 else -1)
        if left < 1 or left > st["pages"]:
            return
        st["left"] = left
        self.manual_seq += 1
        self._render_spread()

    def _render_spread(self):
        """Render the LATEST requested spread (RV2-M5). At most one render
        job is queued or running: page turns while it is in flight only move
        manual_state["left"], a job that is already stale when it starts
        returns without rendering, and when a job finishes the newest spread
        is rendered next - so a flick of N taps costs at most two renders,
        and only the settled spread asks manuals for a prerender (one bounded
        worker there, not threads per tap)."""
        if self.manual_inflight or self.manual_state is None:
            return
        st = self.manual_state
        mg, seq = self.manual_gen, self.manual_seq
        mv = self.view.manual
        ph = mv.page_height()
        bx, by, bw, bh = mv.body_rect()
        pdf, left, pages = st["pdf"], st["left"], st["pages"]
        mods, load = self.manuals, self.load_image

        def job():
            if mg != self.manual_gen or seq != self.manual_seq:
                return mg, seq, None, None          # superseded before it started
            n = pages if pages else mods.page_count(pdf)
            self.manual_renders += 1
            lp, rp = mods.render_spread(pdf, left, height=ph, total_pages=n)
            if lp is None:
                return mg, seq, n, []
            sizes = [mods.png_size(p) for p in (lp, rp) if p is not None]
            if not sizes or any(s is None for s in sizes):
                return mg, seq, n, []
            lay = mods.spread_layout(sizes[0], sizes[1] if len(sizes) > 1 else None,
                                     screen=(int(bw), int(bh)))
            out = []
            for p, key in ((lp, "left"), (rp, "right")):
                if p is None or lay.get(key) is None:
                    continue
                lx, ly, lw, lh = lay[key]
                img = load(os.fspath(p), int(lw), int(lh), "fit")
                out.append((img, bx + lx, by + ly))
            return mg, seq, n, out

        def done(res):
            self.manual_inflight = False
            st2 = self.manual_state
            if st2 is None:
                return
            if res is None:                         # the job raised (logged by _guarded)
                res = (mg, seq, None, [])
            rmg, rseq, n, out = res
            if rmg != self.manual_gen or rseq != self.manual_seq:
                self._render_spread()               # a newer spread (or manual) is wanted
                return
            if n is not None:
                st2["pages"] = n
            if not out:
                mv.set_message("This manual could not be rendered")
                mv.set_nav(st2["title"], False, False)
                return
            n = st2["pages"]
            last = min(n or st2["left"], st2["left"] + len(out) - 1)
            # the page range first: a long title is what gets ellipsized
            label = "%s of %s   %s" % (("%d-%d" % (st2["left"], last)) if last > st2["left"]
                                       else str(st2["left"]), n if n else "?", st2["title"])
            mv.set_pages(out)
            mv.set_nav(label, st2["left"] > 1, bool(n) and st2["left"] + 2 <= n)
            if n and left + 2 <= n:                 # warm the cache for the next spread
                mods.prerender(pdf, [p for p in (left + 2, left + 3) if p <= n], height=ph)
        self.manual_inflight = True
        self.submit(_guarded(job), done=done)

    # -- introspection (state.json) ----------------------------------------------
    def state(self):
        t = self.target()
        info = self.info or {}
        return {
            "active": self.active,
            "target": None if t is None else {"kind": t.kind, "system": t.system,
                                              "rom": t.rom_path, "name": t.name,
                                              "running": t.running},
            "running_confirmed": self.running_confirmed,
            "shown": {"kind": info.get("kind"), "title": info.get("title"),
                      "still": self.choice[1], "still_kind": self.choice[2],
                      "video": self.choice[0], "manual": info.get("manual"),
                      "logo": info.get("logo")} if self.info else None,
            "view_mode": self.view.mode,
            "video_on": self.view.video_on,
            "video_path": self.video_path,
            "video_starts": self.video_starts,
            "video_stops": dict(self.video_stops),
            "in_game_mode": self.ingame_mode_applied,
            "manual_open": self.manual_state is not None,
            "manual": {k: self.manual_state[k] for k in ("pdf", "left", "pages")}
            if self.manual_state else None,
            "last_event": self.last_event,
            "events": {"%s/%s" % k: v for k, v in self.events.items()},
            "es_idle": self.es_idle,
            "ignored_game_ends": self.ignored_ends,
            "manual_renders": self.manual_renders,
            "warning": self.warning,
        }


def _guarded(fn):
    """Worker jobs must never raise into the worker (it would only log)."""
    def run(*a):
        try:
            return fn(*a)
        except Exception:           # noqa: BLE001
            log.exception("companion job %r", getattr(fn, "__name__", fn))
            return None
    return run
