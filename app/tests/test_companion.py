#!/usr/bin/env python3
"""companion.py and its integration into main.App (Navigation v2), offline:

  - pure helpers: media choice per media_priority, metadata lines, video
    sizing / SDL rects, ES's on-disk media naming, the video mute setting
  - Resolver against the REAL captured ES fixtures (gb games + localpaths)
  - VideoWorker: a fake player AND the real media.VideoPlayer over
    test_media's FakeMpv - frames are rendered on the worker thread into
    alternating buffers, stop() closes the player and hides frames at once
  - CompanionController: targets, stale results, the video rules (only while
    browsing; stopped at game start, when inactive, for the manual), idle,
    the manual viewer
  - main.App built WITHOUT SDL (build_ui): the Command Center opens on a
    swipe from the top edge and closes on the hardware button; the volume
    overlay appears only while the Command Center is closed; settings apply
    live and save; mixer paging; BAR battery tap; HIDDEN closes everything.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import companion  # noqa: E402
import companion_modes  # noqa: E402
import config  # noqa: E402
import es_api  # noqa: E402
import esevents  # noqa: E402
import main  # noqa: E402
import media  # noqa: E402
import screens  # noqa: E402
import summon  # noqa: E402
import ui  # noqa: E402
from companion import Target  # noqa: E402
from sway_ipc import BAR, FULL, HIDDEN  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
W, H = 1920, 1080
PRIO = list(config.MEDIA_VALUES)     # video, titleshot, mix, image, marquee, fanart


def load_fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeImage:
    def __init__(self, path, w, h, mode="fit"):
        self.path, self.w, self.h, self.mode = path, w, h, mode


def fake_load_image(path, w, h, mode="fit", upscale=True):
    return FakeImage(path, w, h, mode)


class SyncSubmit:
    """Runs a job at once, or holds it (deferred=True) until run_all()."""

    def __init__(self, deferred=False):
        self.deferred = deferred
        self.jobs = []

    def __call__(self, fn, *args, done=None):
        if self.deferred:
            self.jobs.append((fn, args, done))
        else:
            res = fn(*args)
            if done:
                done(res)

    def run(self, i):
        fn, args, done = self.jobs.pop(i)
        res = fn(*args)
        if done:
            done(res)

    def run_all(self):
        while self.jobs:
            self.run(0)


class FakeTimers:
    def __init__(self):
        self.now = 0.0
        self.items = []

    def call_later(self, delay, fn):
        h = [self.now + delay, fn, False]
        self.items.append(h)
        return h

    @staticmethod
    def cancel(h):
        if h is not None:
            h[2] = True

    def advance(self, dt):
        self.now += dt
        due = [h for h in self.items if not h[2] and h[0] <= self.now]
        self.items = [h for h in self.items if h not in due]
        for h in due:
            h[1]()


class FakeVideo:
    def __init__(self):
        self.calls = []
        self.path = None
        self.gen = 0

    def play(self, path, w, h, loop=True, mute=True):
        self.gen += 1
        self.path = path
        self.calls.append(("play", path, w, h, loop, mute))
        return self.gen

    def stop(self):
        self.gen += 1
        self.path = None
        self.calls.append(("stop",))

    def frame(self):
        class _N:
            def __enter__(s):
                return None

            def __exit__(s, *e):
                return False
        return _N()

    def shutdown(self, timeout=0):
        self.calls.append(("shutdown",))

    def names(self):
        return [c[0] for c in self.calls]


class Handlers:
    def __getattr__(self, name):
        return lambda *a, **k: None


class FakeResolver:
    def __init__(self, infos=None):
        self.infos = infos or {}
        self.calls = []

    def resolve(self, t):
        self.calls.append(t)
        key = t.rom_path if t.kind == "game" else ("system", t.system)
        info = dict(self.infos.get(key) or {"kind": t.kind, "title": t.name or t.system,
                                             "media": {}, "manual": None})
        info.setdefault("kind", t.kind)
        info.setdefault("system", t.system)
        info["running"] = t.running
        return info

    def random_art(self):
        return "/art/random.png"

    def system_art(self, system):
        self.calls.append(("system_art", system))
        return "/art/%s-slide.png" % system


def game_ev(rom, name="Game", system="gb", kind=esevents.GAME_SELECTED, source="hook", game=None):
    return esevents.EsEvent(kind, system, rom, name, (), source, 0.0, game)


ROM_A = "/storage/roms/gb/Alpha (USA).gb"
ROM_B = "/storage/roms/gb/Beta (USA).gb"
INFO_A = {"kind": "game", "title": "Alpha", "desc": "An alpha game.", "releasedate": "19950101T0",
          "developer": "Dev", "players": "1-2",
          "media": {"video": "/v/alpha.mp4", "titleshot": "/i/alpha-title.png",
                    "image": "/i/alpha.png"},
          "manual": "/m/alpha-manual.pdf"}
INFO_B = {"kind": "game", "title": "Beta", "media": {"image": "/i/beta.png"}, "manual": None}


def make_controller(infos=None, deferred=False, cfg=None, hud_fn=None):
    cfg = cfg or config.defaults()
    view = companion.CompanionView(Handlers())
    root = ui.Root(W, H)
    root.add(view)
    view.layout((0, 0, W, H))
    timers = FakeTimers()
    video = FakeVideo()
    submit = SyncSubmit(deferred)
    c = companion.CompanionController(view, submit, timers, video, lambda: cfg,
                                      resolver=FakeResolver(infos or {ROM_A: INFO_A,
                                                                      ROM_B: INFO_B}),
                                      load_image=fake_load_image, clock=lambda: timers.now,
                                      es_settings=os.path.join(FIX, "does-not-exist.cfg"),
                                      hud_fn=hud_fn)
    return c, view, timers, video, submit, cfg


class TestStaleGameStart(unittest.TestCase):
    """Test day, 24 Sep: a Steam launch stops sway and ES restarts, so no
    game-end ever fires; the spool replayed the old game-start at the next
    app start and "Now playing" stuck. A hook game-start whose spool key
    (first field: fire time in clock ticks since boot) predates the running
    ES process is dropped; anything unknown keeps the start (safe side)."""

    def ev(self, fired_ticks, rom=ROM_A):
        e = game_ev(rom, "Alpha", kind=esevents.GAME_START)
        return e._replace(seq=(fired_ticks, 4000, 4001))

    def make(self, es_ticks):
        c, view, timers, video, _, _ = make_controller()
        c.es_start_ticks = es_ticks if callable(es_ticks) else (lambda: es_ticks)
        return c, view

    def test_start_from_an_exited_es_is_dropped(self):
        # the device: FFX .desktop start fired before the current ES began
        c, view = self.make(es_ticks=900000)
        c.on_es_event(self.ev(123456))
        self.assertIsNone(c.running)
        self.assertEqual(c.stale_starts, 1)
        self.assertNotEqual(view.badge, "Now playing")

    def test_start_from_the_running_es_is_kept(self):
        c, view = self.make(es_ticks=100000)
        c.on_es_event(self.ev(123456))
        self.assertIsNotNone(c.running)
        self.assertEqual(c.stale_starts, 0)

    def test_unknown_es_start_keeps_the_game_start(self):
        for es in (None, lambda: (_ for _ in ()).throw(OSError("no /proc"))):
            c, _ = self.make(es_ticks=es)
            c.on_es_event(self.ev(123456))
            self.assertIsNotNone(c.running, es)

    def test_poll_and_seqless_starts_are_never_judged(self):
        c, _ = self.make(es_ticks=10 ** 9)
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))   # no seq
        self.assertIsNotNone(c.running)
        c2, _ = self.make(es_ticks=10 ** 9)
        c2.on_es_event(self.ev(5)._replace(source="poll"))
        self.assertIsNotNone(c2.running)

    def test_the_real_es_start_reader_reads_proc(self):
        # the default reader; on a machine without ES it must say None
        # (never raise), which keeps every start
        v = companion._es_start_ticks()
        self.assertTrue(v is None or isinstance(v, int))


def media_priority_with_first(*first):
    """A full, valid companion.media_priority permutation (config.py's
    array_enum validation requires every config.MEDIA_VALUES member,
    nothing dropped - see config._validate) with `first` pinned to the
    front, in order - used instead of a hand-written 6-item literal so
    these tests don't silently stop reordering anything the moment
    config.MEDIA_VALUES grows (CC3 added "cartridge"/"boxback" via
    patches/CC23-config.patch)."""
    rest = [v for v in config.MEDIA_VALUES if v not in first]
    return list(first) + rest


def set_in_game_display(cfg, value):
    """config.py's real SCHEMA does not carry companion.in_game_display yet
    (CC2/CC3 ship it as patches/CC23-config.patch, applied by the
    integration agent) - config.get_value()/_get() read straight through a
    dict regardless of whether config.SCHEMA knows the key (see config.py's
    _get(): it walks the dict, the schema only supplies a DEFAULT), so a
    controller under test can be handed the value directly, exactly as it
    will look post-patch."""
    cfg.setdefault("companion", {})["in_game_display"] = value


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestChooseMedia(unittest.TestCase):
    def test_first_available_in_priority_wins_video_with_poster(self):
        paths = {"video": "/v.mp4", "image": "/i.png", "fanart": "/f.jpg"}
        self.assertEqual(companion.choose_media(paths, PRIO, True), ("/v.mp4", "/i.png", "image"))

    def test_video_skipped_when_not_allowed(self):
        paths = {"video": "/v.mp4", "mix": "/m.png"}
        self.assertEqual(companion.choose_media(paths, PRIO, False), (None, "/m.png", "mix"))

    def test_an_image_ranked_above_video_means_no_video(self):
        prio = ["image", "video", "titleshot", "mix", "marquee", "fanart"]
        paths = {"video": "/v.mp4", "image": "/i.png"}
        self.assertEqual(companion.choose_media(paths, prio, True), (None, "/i.png", "image"))

    def test_video_only_and_nothing(self):
        self.assertEqual(companion.choose_media({"video": "/v.mp4"}, PRIO, True),
                         ("/v.mp4", None, None))
        self.assertEqual(companion.choose_media({}, PRIO, True), (None, None, None))

    def test_thumbnail_is_the_last_resort(self):
        self.assertEqual(companion.choose_media({"thumbnail": "/t.png"}, PRIO, True),
                         (None, "/t.png", "thumbnail"))


class TestMetadata(unittest.TestCase):
    INFO = {"title": "Bomberman GB", "releasedate": "19980515T000000", "developer": "Hudson Soft",
            "players": "1", "rating": "0.7", "desc": "  Bomberman  is\nsearching.  "}

    def test_defaults(self):
        show = config.get_value(config.defaults(), ("companion", "show_metadata"))
        self.assertEqual(companion.metadata_lines(self.INFO, show),
                         ("Bomberman GB", "1998  ·  Hudson Soft  ·  1 player", ""))

    def test_everything_on(self):
        show = {k: True for k in ("title", "year", "developer", "players", "rating", "description")}
        t, f, d = companion.metadata_lines(self.INFO, show)
        self.assertIn("Rating 70%", f)
        self.assertEqual(d, "Bomberman is searching.")

    def test_everything_off_and_unknowns(self):
        show = {k: False for k in ("title", "year", "developer", "players", "rating", "description")}
        self.assertEqual(companion.metadata_lines(self.INFO, show), ("", "", ""))
        self.assertEqual(companion.metadata_lines({"title": "X", "releasedate": "0000"}, {}),
                         ("X", "", ""))
        self.assertEqual(companion.fmt_players("2-4"), "2-4 players")
        self.assertEqual(companion.fmt_rating("0"), "")
        self.assertEqual(companion.fmt_rating("garbage"), "")

    def test_playtime_hidden_by_default_shown_when_asked(self):
        info = dict(self.INFO, playcount="3")
        _, facts, _ = companion.metadata_lines(info, {})
        self.assertNotIn("Played", facts)
        _, facts, _ = companion.metadata_lines(info, {"playtime": True})
        self.assertIn("Played 3 times", facts)

    def test_playtime_absent_or_zero_adds_nothing(self):
        for pc in (None, "", "0", "garbage"):
            _, facts, _ = companion.metadata_lines({"playcount": pc}, {"playtime": True})
            self.assertEqual(facts, "")


class TestVideoGeometry(unittest.TestCase):
    def test_render_size_never_upscales_and_is_even(self):
        self.assertEqual(companion.video_render_size((640, 480), (1920, 1080)), (640, 480))
        self.assertEqual(companion.video_render_size((1920, 1080), (960, 540)), (960, 540))
        w, h = companion.video_render_size((1279, 721), (640, 360))
        self.assertTrue(w % 2 == 0 and h % 2 == 0 and w <= 640 and h <= 360)

    def test_fit_letterboxes_and_centres(self):
        src, dst = companion.video_rects(640, 480, (0, 0, 1920, 1080), "fit")
        self.assertEqual(src, (0, 0, 640, 480))
        self.assertEqual(dst, (240.0, 0.0, 1440.0, 1080.0))

    def test_fill_stretches(self):
        self.assertEqual(companion.video_rects(640, 480, (0, 0, 1920, 1080), "fill"),
                         ((0, 0, 640, 480), (0, 0, 1920, 1080)))

    def test_crop_covers_by_cutting_the_source(self):
        src, dst = companion.video_rects(640, 480, (0, 0, 1920, 1080), "crop")
        self.assertEqual(dst, (0, 0, 1920, 1080))
        self.assertEqual(src, (0.0, 60.0, 640.0, 360.0))


class TestFsMedia(unittest.TestCase):
    def test_es_naming_convention(self):
        rom = "/storage/roms/gb/Bomberman GB (USA, Europe) (SGB Enhanced).zip"
        stem = "Bomberman GB (USA, Europe) (SGB Enhanced)"
        present = {"/storage/roms/gb/images/%s-image.png" % stem,
                   "/storage/roms/gb/images/%s-fanart.jpg" % stem,
                   "/storage/roms/gb/images/%s-thumb.png" % stem,
                   "/storage/roms/gb/videos/%s-video.mp4" % stem}
        got = companion.fs_media_paths(rom, present.__contains__)
        self.assertEqual(set(got), {"image", "fanart", "thumbnail", "video"})
        self.assertEqual(companion.fs_media_paths("", present.__contains__), {})

    def test_cartridge_and_boxback_cc3(self):
        """CC3: ES-DE Companion's "Physical Media" / "Box Back Cover"
        widgets - ES-API-NOTES.md confirmed these two exist on-device with
        the same <stem>-<suffix> naming as every other image kind."""
        rom = "/storage/roms/gb/Bomberman GB (USA, Europe) (SGB Enhanced).zip"
        stem = "Bomberman GB (USA, Europe) (SGB Enhanced)"
        present = {"/storage/roms/gb/images/%s-cartridge.png" % stem,
                   "/storage/roms/gb/images/%s-boxback.png" % stem}
        got = companion.fs_media_paths(rom, present.__contains__)
        self.assertEqual(got, {"cartridge": "/storage/roms/gb/images/%s-cartridge.png" % stem,
                               "boxback": "/storage/roms/gb/images/%s-boxback.png" % stem})


class TestVideoMuted(unittest.TestCase):
    def test_settings(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        on, off = os.path.join(d, "on.cfg"), os.path.join(d, "off.cfg")
        with open(on, "w") as f:
            f.write('<config>\n<bool name="VideoAudio" value="true" />\n</config>\n')
        with open(off, "w") as f:
            f.write('<config>\n<bool name="VideoAudio" value="false" />\n</config>\n')
        self.assertTrue(companion.video_muted("muted", on))
        self.assertFalse(companion.video_muted("unmuted", off))
        self.assertFalse(companion.video_muted("follow_es", on))
        self.assertTrue(companion.video_muted("follow_es", off))
        # an unreadable / missing file: muted (never surprise-loud)
        self.assertTrue(companion.video_muted("follow_es", os.path.join(d, "absent.cfg")))

    def test_follow_es_without_the_key_is_es_default_unmuted(self):
        # RV4-M1: ES only writes settings that differ from its default, and
        # VideoAudio defaults to true - a readable file without the key is ON
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        nokey = os.path.join(d, "nokey.cfg")
        with open(nokey, "w") as f:
            f.write('<?xml version="1.0"?>\n<config>\n<bool name="ShowHelpPrompts" '
                    'value="false" />\n<string name="ThemeSet" value="x" />\n</config>\n')
        self.assertFalse(companion.video_muted("follow_es", nokey))
        empty = os.path.join(d, "empty.cfg")
        open(empty, "w").close()
        self.assertFalse(companion.video_muted("follow_es", empty))
        self.assertTrue(companion.video_muted("muted", nokey))


# ---------------------------------------------------------------------------
# Resolver, against the real ES captures
#
# FakeES and TestResolver removed entirely: every method depended (via
# FakeES.__init__ / TestResolver.setUp, both called unconditionally for
# every test in the class) on tests/fixtures/es-games-gb-real-capture-*.json,
# es-games-gb-localpaths-real-capture-*.json, es-systems-real-capture-*.json,
# and es-system-gb-localpaths-real-capture-*.json - real on-device
# EmulationStation API captures dropped from the public release (see
# DROPPED-FIXTURES-list.txt). No synthetic equivalent exists for this shape,
# so there was nothing to rewire onto.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# VideoWorker
# ---------------------------------------------------------------------------
class FakePlayer:
    def __init__(self, path, w, h, wake=None, **kw):
        self.path, self.w, self.h, self.wake, self.kw = path, w, h, wake, kw
        self.renders = []
        self.closed = False
        self.state = "playing"
        self.native = (640, 480)
        self.pending = 0
        self.lock = threading.Lock()

    def push_frames(self, n=1):
        with self.lock:
            self.pending += n
        self.wake()

    def poll_frame(self):
        with self.lock:
            if self.pending:
                self.pending -= 1
                if self.pending:
                    self.wake()
                return True
        return False

    def native_size(self):
        return self.native

    def resize(self, w, h):
        self.w, self.h = w, h

    def render_into(self, buf, stride, w, h):
        self.renders.append((buf.address, stride, w, h, threading.current_thread().name))

    def load(self, path):
        self.path = path

    def set_loop(self, v):
        pass

    def set_mute(self, v):
        pass

    def close(self):
        self.closed = True


class TestVideoWorker(unittest.TestCase):
    def setUp(self):
        self.players = []
        self.notified = []

        def factory(path, w, h, **kw):
            p = FakePlayer(path, w, h, **kw)
            self.players.append(p)
            return p
        self.w = companion.VideoWorker(lambda: self.notified.append(threading.current_thread().name),
                                       factory=factory, log_fn=lambda m: None)
        self.addCleanup(self.w.shutdown)

    def frame(self):
        with self.w.frame() as f:
            return f

    def test_renders_on_its_own_thread_into_alternating_buffers(self):
        self.w.play("/v/a.mp4", 1920, 1080)
        self.assertTrue(wait_for(lambda: self.players))
        p = self.players[0]
        self.assertEqual(p.kw.get("mode"), "fill")
        p.push_frames(4)            # 1st: learns native size, then 3 renders
        self.assertTrue(wait_for(lambda: len(p.renders) >= 3))
        self.assertEqual({r[4] for r in p.renders}, {"rp5deck-video"})
        addrs = [r[0] for r in p.renders]
        self.assertNotEqual(addrs[0], addrs[1])                 # double buffered
        self.assertEqual(addrs[0], addrs[2])
        self.assertEqual((p.w, p.h), (640, 480))                # native, not the box
        f = self.frame()
        self.assertIsNotNone(f)
        self.assertEqual((f.gen, f.w, f.h), (self.w.gen, 640, 480))
        self.assertEqual(f.address, addrs[-1])                  # the front is the last rendered
        self.assertTrue(self.notified)
        self.assertEqual(set(self.notified), {"rp5deck-video"})

    def test_stop_hides_frames_immediately_and_closes_the_player(self):
        self.w.play("/v/a.mp4", 1920, 1080)
        self.assertTrue(wait_for(lambda: self.players))
        p = self.players[0]
        p.push_frames(3)
        self.assertTrue(wait_for(lambda: self.frame() is not None))
        self.w.stop()
        self.assertIsNone(self.frame())                         # same thread, no wait
        self.assertTrue(wait_for(lambda: p.closed))
        self.assertEqual((self.w.players_created, self.w.players_closed), (1, 1))
        self.w.play("/v/b.mp4", 1920, 1080)                     # a fresh player after stop
        self.assertTrue(wait_for(lambda: len(self.players) == 2))

    def test_a_player_that_cannot_start_is_an_error_not_a_crash(self):
        def bad(path, w, h, **kw):
            raise media.MediaError("libmpv not found")
        w = companion.VideoWorker(lambda: None, factory=bad, log_fn=lambda m: None)
        self.addCleanup(w.shutdown)
        w.play("/v/a.mp4", 100, 100)
        self.assertTrue(wait_for(lambda: w.state == "error"))
        self.assertIn("libmpv", w.error)
        self.assertTrue(w.alive())

    def test_switching_videos_reuses_the_player(self):
        self.w.play("/v/a.mp4", 800, 600)
        self.assertTrue(wait_for(lambda: self.players))
        self.w.play("/v/b.mp4", 800, 600)
        self.assertTrue(wait_for(lambda: self.players[0].path == "/v/b.mp4"))
        self.assertEqual(len(self.players), 1)

    def test_real_videoplayer_over_fake_libmpv(self):
        from test_media import FakeMpv
        fake = FakeMpv()
        fake.props.update({"dwidth": "640", "dheight": "480"})
        fake.flags = media.MPV_RENDER_UPDATE_FRAME
        made = []

        def factory(path, w, h, **kw):
            p = media.VideoPlayer(path, w, h, _lib_obj=fake, **kw)
            made.append(p)
            return p
        w = companion.VideoWorker(lambda: None, factory=factory, log_fn=lambda m: None)
        self.addCleanup(w.shutdown)
        w.play("/v/real.mp4", 1920, 1080)
        self.assertTrue(wait_for(lambda: fake.update_cb is not None))
        for _ in range(40):
            fake.update_cb()                # what mpv's render thread does
            time.sleep(0.005)
            if len([c for c in fake.calls if c[0] == "render"]) >= 2:
                break
        renders = [c for c in fake.calls if c[0] == "render"]
        self.assertGreaterEqual(len(renders), 2)
        self.assertEqual(renders[0][1:4], (640, 480, "bgr0"))
        self.assertNotEqual(renders[0][5], renders[1][5])       # two buffers
        w.stop()
        self.assertTrue(wait_for(lambda: "destroy" in fake.names()))
        self.assertEqual(made[0].state, "closed")


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


# ---------------------------------------------------------------------------
# CompanionController
# ---------------------------------------------------------------------------
class TestControllerVideoRules(unittest.TestCase):
    def browse_to_a(self, c, timers):
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        timers.advance(0.6)             # video_start_delay_ms = 500

    def test_selected_game_plays_its_video_after_the_delay(self):
        c, view, timers, video, _, _ = make_controller()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        self.assertEqual(view.image.path, "/i/alpha-title.png")      # poster at once
        self.assertEqual(video.calls, [])
        timers.advance(0.4)
        self.assertEqual(video.calls, [])
        timers.advance(0.2)
        self.assertEqual(video.calls, [("play", "/v/alpha.mp4", W, H, True, True)])
        self.assertTrue(view.manual_btn.visible)

    def test_video_stops_the_moment_a_game_starts_and_never_restarts_while_it_runs(self):
        c, view, timers, video, _, _ = make_controller()
        self.browse_to_a(c, timers)
        view.set_video(True, (0, 0, W, H))
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        self.assertEqual(video.names()[-1], "stop")
        self.assertIsNone(c.video_path)
        self.assertFalse(view.video_on)
        n = len(video.calls)
        timers.advance(10)
        c.set_active(False)
        c.set_active(True)
        timers.advance(10)
        self.assertEqual(video.calls[n:], [])          # nothing started while running
        self.assertEqual(view.badge, "Now playing")
        self.assertEqual(c.choice[0], None)             # gate 1: still art only
        self.assertFalse(c.video_allowed())             # gate 2, checked on its own
        self.assertEqual(c.video_stops["game-start"], 1)

    def test_a_game_start_during_the_delay_cancels_the_pending_video(self):
        c, view, timers, video, _, _ = make_controller()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        timers.advance(0.3)
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        timers.advance(5)
        self.assertEqual(video.calls, [])

    def test_game_end_returns_to_browsing_and_video(self):
        c, view, timers, video, _, _ = make_controller()
        self.browse_to_a(c, timers)
        c.on_es_event(game_ev(ROM_A, kind=esevents.GAME_START))
        c.on_es_event(game_ev(ROM_A, kind=esevents.GAME_END))
        self.assertIsNone(c.running)
        timers.advance(0.6)
        self.assertEqual(video.calls[-1][:2], ("play", "/v/alpha.mp4"))

    def test_poll_end_does_not_clear_an_unconfirmed_hook_start(self):
        c, view, timers, video, _, _ = make_controller()
        c.on_es_event(game_ev(ROM_A, kind=esevents.GAME_START))
        c.on_es_event(game_ev(ROM_A, kind=esevents.GAME_END, source="poll"))
        self.assertIsNotNone(c.running)
        c.on_es_event(game_ev(ROM_A, kind=esevents.GAME_END, source="hook"))
        self.assertIsNone(c.running)

    def test_poll_start_alone_is_enough(self):
        c, view, timers, video, _, _ = make_controller()
        self.browse_to_a(c, timers)
        c.on_es_event(game_ev(ROM_B, "Beta", kind=esevents.GAME_START, source="poll"))
        self.assertEqual(video.names()[-1], "stop")
        self.assertTrue(c.running_confirmed)
        c.on_es_event(game_ev(ROM_B, kind=esevents.GAME_END, source="poll"))
        self.assertIsNone(c.running)

    def test_inactive_stops_and_active_resumes(self):
        c, view, timers, video, _, _ = make_controller()
        self.browse_to_a(c, timers)
        c.set_active(False)                             # Command Center opened / HIDDEN / BAR
        self.assertEqual(video.names()[-1], "stop")
        timers.advance(5)
        self.assertEqual(video.names()[-1], "stop")
        c.set_active(True)
        timers.advance(0.01)
        self.assertEqual(video.calls[-1][:2], ("play", "/v/alpha.mp4"))

    def test_inactive_from_the_start_never_plays(self):
        c, view, timers, video, _, _ = make_controller()
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        timers.advance(5)
        self.assertEqual(video.calls, [])

    def test_play_video_off_and_image_first_priority(self):
        cfg = config.defaults()
        config.set_value(cfg, ("companion", "play_video"), False)
        c, view, timers, video, _, _ = make_controller(cfg=cfg)
        self.browse_to_a(c, timers)
        self.assertEqual(video.calls, [])
        cfg2 = config.defaults()
        config.set_value(cfg2, ("companion", "media_priority"),
                         media_priority_with_first("image", "video"))
        c, view, timers, video, _, _ = make_controller(cfg=cfg2)
        self.browse_to_a(c, timers)
        self.assertEqual(video.calls, [])
        self.assertEqual(view.image.path, "/i/alpha.png")

    def test_selection_change_stops_the_old_video(self):
        c, view, timers, video, _, _ = make_controller()
        self.browse_to_a(c, timers)
        c.on_es_event(game_ev(ROM_B, "Beta"))
        self.assertEqual(video.names()[-1], "stop")
        timers.advance(5)
        self.assertEqual(video.names()[-1], "stop")     # Beta has no video
        self.assertEqual(view.image.path, "/i/beta.png")

    def test_video_frame_ready_punches_the_hole(self):
        c, view, timers, video, _, _ = make_controller()
        self.browse_to_a(c, timers)
        src, dst = c.video_frame_ready(640, 480)
        self.assertTrue(view.video_on)
        self.assertEqual(view.video_box, (0, 0, W, H))
        self.assertEqual(dst, (240.0, 0.0, 1440.0, 1080.0))


class RecordingCanvas:
    def __init__(self):
        self.ops = []
        self.clip_rect = (0, 0, W, H)

    def __getattr__(self, name):
        def op(*a, **k):
            self.ops.append((name,) + a)
        return op


class TestControllerTargets(unittest.TestCase):
    def test_stale_results_are_dropped(self):
        c, view, timers, video, submit, _ = make_controller(deferred=True)
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        c.on_es_event(game_ev(ROM_B, "Beta"))
        submit.run(1)                                   # Beta resolves first
        submit.run(0)                                   # then the stale Alpha
        self.assertEqual(view.title, "Beta")
        self.assertEqual(view.image.path, "/i/beta.png")

    def test_system_selected_shows_the_systems_art(self):
        infos = {("system", "snes"): {"kind": "system", "title": "Super Nintendo",
                                      "total_games": 831, "logo": "/logos/snes.png", "media": {}}}
        c, view, timers, video, _, _ = make_controller(infos=infos)
        c.set_active(True)
        c.on_es_event(esevents.interpret(esevents.SYSTEM_SELECTED, ["snes", "", ""]))
        self.assertEqual(view.mode, "system")
        self.assertEqual(view.logo.path, "/logos/snes.png")
        self.assertEqual((view.title, view.facts), ("Super Nintendo", "831 games"))
        timers.advance(5)
        self.assertEqual(video.calls, [])

    def test_nothing_known_is_the_idle_clock(self):
        c, view, timers, video, _, _ = make_controller()
        c.set_active(True)
        c.refresh()
        self.assertEqual((view.mode, view.idle_mode), ("idle", "clock"))
        self.assertRegex(view.clock_text, r"^\d\d:\d\d$")
        g = RecordingCanvas()
        view.draw(g)
        self.assertIn(view.clock_text, [op[1] for op in g.ops if op[0] == "text"])

    def test_idle_slideshow_and_blank(self):
        cfg = config.defaults()
        config.set_value(cfg, ("companion", "idle_mode"), "slideshow")
        c, view, timers, video, _, _ = make_controller(cfg=cfg)
        c.set_active(True)
        c.refresh()
        timers.advance(0.01)
        self.assertEqual(view.image.path, "/art/random.png")
        config.set_value(cfg, ("companion", "idle_mode"), "blank")
        c.config_changed()
        g = RecordingCanvas()
        view.draw(g)
        self.assertEqual([op[0] for op in g.ops if op[0] in ("text", "image")], [])

    def test_video_on_clears_the_art_area_under_the_ui(self):
        c, view, timers, video, _, _ = make_controller()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        g = RecordingCanvas()
        view.draw(g)
        self.assertIn("image", [op[0] for op in g.ops])
        self.assertNotIn("clear_rect", [op[0] for op in g.ops])
        view.set_video(True, (0, 0, W, H))
        g = RecordingCanvas()
        view.draw(g)
        names = [op[0] for op in g.ops]
        self.assertIn("clear_rect", names)
        self.assertNotIn("image", names)
        self.assertLess(names.index("clear_rect"), names.index("text"))   # text drawn over video

    def test_a_resolver_crash_still_shows_the_right_target(self):
        c, view, timers, video, _, _ = make_controller()

        def boom(t):
            raise RuntimeError("ES parse bug")
        c.resolver.resolve = boom
        c.set_active(True)
        with self.assertLogs("rp5deck.companion", "ERROR"):
            c.on_es_event(game_ev(ROM_B, "Beta"))
        self.assertEqual(view.title, "Beta")


class FakeManuals:
    def __init__(self, pages=6):
        self.pages = pages
        self.spreads = []
        self.prerenders = []

    def page_count(self, pdf):
        return self.pages

    def render_spread(self, pdf, left, height=1080, total_pages=None):
        self.spreads.append((left, height))
        right = left + 1 if (total_pages is None or left + 1 <= total_pages) else None
        return "/c/p%d.png" % left, ("/c/p%d.png" % right if right else None)

    def png_size(self, p):
        return (700, 924)

    def spread_layout(self, a, b=None, screen=(1920, 1080), gap=8):
        import manuals
        return manuals.spread_layout(a, b, screen, gap)

    def prerender_next_async(self, pdf, page, height=1080):
        self.prerenders.append(page)

    def prerender(self, pdf, pages, height=1080):
        self.prerenders.append(list(pages))


class TestManualViewer(unittest.TestCase):
    def make(self):
        c, view, timers, video, submit, cfg = make_controller()
        c.manuals = FakeManuals()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        timers.advance(0.6)
        return c, view, timers, video

    def test_open_renders_a_two_page_spread_and_stops_video(self):
        c, view, timers, video = self.make()
        self.assertEqual(video.names(), ["play"])
        c.open_manual()
        self.assertEqual(video.names(), ["play", "stop"])
        mv = view.manual
        self.assertTrue(mv.visible)
        self.assertFalse(view.tab.visible)              # nothing beneath can take a tap
        self.assertFalse(view.manual_btn.visible)
        self.assertEqual(c.manuals.spreads, [(1, mv.page_height())])
        self.assertEqual(len(mv.pages), 2)
        (l_img, lx, ly), (r_img, rx, ry) = mv.pages
        self.assertEqual(l_img.path, "/c/p1.png")
        self.assertEqual(r_img.path, "/c/p2.png")
        self.assertGreater(rx, lx)
        self.assertGreaterEqual(ly, mv.body.rect[1])     # below the header
        self.assertFalse(mv.prev.enabled)
        self.assertTrue(mv.next.enabled)
        self.assertIn("1-2 of 6", mv.title.text)
        self.assertEqual(c.manuals.prerenders, [[3, 4]])     # the next spread, one request
        timers.advance(10)
        self.assertEqual(video.names(), ["play", "stop"])   # no video under the manual

    def test_next_prev_and_the_ends(self):
        c, view, timers, video = self.make()
        c.open_manual()
        c.manual_page(1)
        c.manual_page(1)
        self.assertEqual(c.manual_state["left"], 5)
        self.assertFalse(view.manual.next.enabled)
        c.manual_page(1)
        self.assertEqual(c.manual_state["left"], 5)
        c.manual_page(-1)
        c.manual_page(-1)
        c.manual_page(-1)
        self.assertEqual(c.manual_state["left"], 1)

    def test_close_restores_the_companion_and_video(self):
        c, view, timers, video = self.make()
        c.open_manual()
        c.close_manual()
        self.assertFalse(view.manual.visible)
        self.assertTrue(view.tab.visible)
        self.assertTrue(view.manual_btn.visible)
        timers.advance(0.01)
        self.assertEqual(video.names()[-1], "play")

    def test_unrenderable_manual_says_so(self):
        c, view, timers, video = self.make()
        c.manuals.render_spread = lambda *a, **k: (None, None)
        c.open_manual()
        self.assertIn("could not be rendered", view.manual.msg.text)

    def test_no_manual_no_button(self):
        c, view, timers, video, _, _ = make_controller()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_B, "Beta"))
        self.assertFalse(view.manual_btn.visible)
        c.open_manual()
        self.assertIsNone(c.manual_state)

    def test_auto_open_on_game_start(self):
        cfg = config.defaults()
        config.set_value(cfg, ("manuals", "open_mode"), "auto_on_game_start")
        c, view, timers, video, _, _ = make_controller(cfg=cfg)
        c.manuals = FakeManuals()
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        self.assertIsNotNone(c.manual_state)
        self.assertTrue(view.manual.visible)


class TestManualFlipCoalescing(unittest.TestCase):
    """RV2-M5 (s2_manual_flip.py): fast Next taps render only the latest
    spread, and only the settled spread asks for ONE prerender request."""

    def make(self):
        c, view, timers, video, submit, cfg = make_controller(deferred=True)
        c.manuals = FakeManuals(pages=12)
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        submit.run_all()
        timers.advance(0.6)
        c.open_manual()
        submit.run_all()
        return c, view, submit

    def test_four_fast_taps_render_two_spreads_not_five(self):
        c, view, submit = self.make()
        h = view.manual.page_height()
        for _ in range(4):
            c.manual_page(1)            # left 3, 5, 7, 9
        self.assertEqual(len(submit.jobs), 1)            # one job in flight, not four
        self.assertEqual(c.manual_state["left"], 9)
        submit.run_all()
        self.assertEqual(c.manuals.spreads, [(1, h), (9, h)])
        self.assertIn("9-10 of 12", view.manual.title.text)
        self.assertEqual([p[0].path for p in view.manual.pages], ["/c/p9.png", "/c/p10.png"])
        self.assertEqual(c.manuals.prerenders, [[3, 4], [11, 12]])
        self.assertFalse(c.manual_inflight)

    def test_a_stale_result_is_never_shown_under_the_new_label(self):
        c, view, submit = self.make()
        c.manual_page(1)                # job for 3 queued
        fn, args, done = submit.jobs.pop(0)
        res = fn(*args)                 # it renders 3 ...
        c.manual_page(1)                # ... while the user taps on to 5
        done(res)                       # 3's result arrives: dropped, 5 is queued
        self.assertNotIn("/c/p3.png", [p[0].path for p in view.manual.pages])
        self.assertEqual(len(submit.jobs), 1)
        submit.run_all()
        self.assertIn("5-6 of 12", view.manual.title.text)
        self.assertEqual([p[0].path for p in view.manual.pages], ["/c/p5.png", "/c/p6.png"])

    def test_reopening_while_a_render_is_in_flight_still_renders(self):
        c, view, submit = self.make()
        c.manual_page(1)
        c.close_manual()
        c.open_manual()                 # the old job is still queued
        submit.run_all()
        self.assertIn("1-2 of 12", view.manual.title.text)
        self.assertTrue(view.manual.pages)

    def test_a_crashing_render_does_not_wedge_the_viewer(self):
        c, view, submit = self.make()

        def boom(*a, **k):
            raise RuntimeError("pdftoppm exploded")
        good = c.manuals.render_spread
        c.manuals.render_spread = boom
        c.manual_page(1)
        submit.run_all()
        self.assertIn("could not be rendered", view.manual.msg.text)
        self.assertFalse(c.manual_inflight)
        c.manuals.render_spread = good
        c.manual_page(-1)
        c.manual_page(1)
        submit.run_all()
        self.assertEqual([p[0].path for p in view.manual.pages], ["/c/p3.png", "/c/p4.png"])


class G:
    """An es_api.Game as /runningGame returns it."""
    system, rom_path, name, id = "gb", ROM_A, "Alpha", "1"
    media = {}


class TestPollFlap(unittest.TestCase):
    """RV1-M2 / RV2-M2 (s10_poll_flap.py): one slow /runningGame answer while
    a game runs must not restart the companion video."""

    def test_timeouts_during_a_game_never_start_video(self):
        poll = esevents.RunningPoll()
        c, view, timers, video, submit, cfg = make_controller()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        timers.advance(1.0)
        seq = [G(), G(), None, esevents.UNKNOWN, None, G()]   # polls 3 s apart
        for i, r in enumerate(seq):
            for ev in poll.feed(r, t=3.0 * i):
                c.on_es_event(ev)
            timers.advance(3.0)
            self.assertIsNotNone(c.running, i)
            self.assertEqual(c.running.rom_path, ROM_A, i)
            self.assertIsNone(video.path, i)
        self.assertEqual(video.names(), ["play", "stop"])
        # the game really ends: two clean answers, then browsing video again
        for r in (esevents.NOT_RUNNING, esevents.NOT_RUNNING):
            for ev in poll.feed(r):
                c.on_es_event(ev)
        self.assertIsNone(c.running)
        timers.advance(1.0)
        self.assertEqual(video.names(), ["play", "stop", "play"])

    def test_one_clean_answer_is_not_enough(self):
        poll = esevents.RunningPoll()
        c, view, timers, video, submit, cfg = make_controller()
        c.set_active(True)
        for ev in poll.feed(G()):
            c.on_es_event(ev)
        for ev in poll.feed(esevents.NOT_RUNNING):
            c.on_es_event(ev)
        self.assertIsNotNone(c.running)


class TestInGameDisplay(unittest.TestCase):
    """CC2: companion.in_game_display - what the bottom screen shows during
    a single-screen game (any game NOT on the DS/3DS/Wii U HIDDEN path -
    those never reach the companion view at all, see DESIGN.md "Three
    display modes"). Default "art" is exactly today's pre-CC2 behaviour and
    is exercised by every other test class in this file; these are the new
    modes only."""

    def start(self, mode, hud_fn=None, infos=None):
        cfg = config.defaults()
        set_in_game_display(cfg, mode)
        c, view, timers, video, submit, cfg = make_controller(cfg=cfg, hud_fn=hud_fn, infos=infos)
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        return c, view, timers, video, submit

    def test_unknown_mode_falls_back_to_art(self):
        c, view, timers, video, submit = self.start("some-typo-value")
        self.assertEqual(view.mode, "game")
        self.assertEqual(c.ingame_mode_applied, "art")

    def test_off_is_a_plain_blank_view_no_art_shown(self):
        c, view, timers, video, submit = self.start("off")
        self.assertEqual(view.mode, "idle")
        self.assertEqual(view.idle_mode, "blank")
        self.assertIsNone(view.image)
        self.assertEqual(c.ingame_mode_applied, "off")

    def test_dim_shows_art_forced_darker_than_configured(self):
        cfg = config.defaults()
        config.set_value(cfg, ("companion", "background_dim"), 0.1)
        set_in_game_display(cfg, "dim")
        c, view, timers, video, submit, cfg = make_controller(cfg=cfg)
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        self.assertEqual(view.mode, "game")
        self.assertGreaterEqual(view.dim, 0.6)
        self.assertEqual(c.ingame_mode_applied, "dim")

    def test_clock_reuses_the_idle_clock_view(self):
        c, view, timers, video, submit = self.start("clock")
        self.assertEqual(view.mode, "idle")
        self.assertEqual(view.idle_mode, "clock")
        self.assertEqual(c.ingame_mode_applied, "clock")
        self.assertIsNotNone(c.running)             # the game is still tracked underneath

    def test_manual_mode_auto_opens_when_a_manual_exists(self):
        c, view, timers, video, submit = self.start("manual")
        self.assertIsNotNone(c.manual_state)
        self.assertTrue(view.manual.visible)
        self.assertEqual(c.ingame_mode_applied, "manual")
        # closing it reveals this game's art underneath, not a stale/blank view
        c.close_manual()
        self.assertEqual(view.mode, "game")
        self.assertEqual(view.title, "Alpha")

    def test_manual_mode_without_a_manual_falls_back_to_art(self):
        c, view, timers, video, submit = self.start("manual", infos={ROM_A: INFO_B})
        self.assertIsNone(c.manual_state)
        self.assertEqual(c.ingame_mode_applied, "art")

    def test_manual_fallback_to_art_shows_a_hint_why(self):
        """CC2 polish (test day): the owner's report "manual... does
        nothing" for a game with no manual is BY DESIGN (falls back to
        art) - but a silent fallback is indistinguishable from having
        picked "art" on purpose. A one-line hint says why."""
        c, view, timers, video, submit = self.start("manual", infos={ROM_A: INFO_B})
        self.assertEqual(c.ingame_mode_applied, "art")
        self.assertIn("No manual for this game", view.facts)

    def test_hud_shows_a_placeholder_then_the_first_sample(self):
        samples = [{"battery_percent": 61, "gpu_load_percent": 12.3}]
        c, view, timers, video, submit = self.start("hud", hud_fn=lambda: samples[0])
        self.assertIn("Reading device stats", view.facts)
        timers.advance(0.0)                          # fires the 0-delay first tick
        self.assertIn("Battery 61%", view.facts)
        self.assertIn("GPU 12%", view.facts)
        self.assertEqual(c.hud_last, samples[0])

    def test_hud_reschedules_itself_at_the_configured_interval(self):
        calls = []

        def fn():
            calls.append(1)
            return {"battery_percent": 50}
        c, view, timers, video, submit = self.start("hud", hud_fn=fn)
        timers.advance(0.0)
        self.assertEqual(len(calls), 1)
        timers.advance(companion_modes.HUD_INTERVAL_S)
        self.assertEqual(len(calls), 2)

    def test_hud_stops_sampling_once_the_game_ends(self):
        calls = []

        def fn():
            calls.append(1)
            return {"battery_percent": 50}
        c, view, timers, video, submit = self.start("hud", hud_fn=fn)
        timers.advance(0.0)
        self.assertEqual(len(calls), 1)
        c.on_es_event(esevents.EsEvent(esevents.GAME_END, "gb", ROM_A, "Alpha", (), "hook", 0.0, None))
        timers.advance(60.0)
        self.assertEqual(len(calls), 1)               # no more ticks after the game ended

    def test_slideshow_pulls_the_running_games_own_system(self):
        c, view, timers, video, submit = self.start("slideshow")
        timers.advance(0.0)
        self.assertEqual(c.resolver.calls[-1], ("system_art", "gb"))
        self.assertEqual(view.mode, "idle")
        self.assertEqual(view.idle_mode, "slideshow")
        self.assertIsNotNone(view.image)

    def test_slideshow_reschedules_at_the_idle_interval(self):
        cfg = config.defaults()
        config.set_value(cfg, ("companion", "idle_slideshow_interval_s"), 7)
        set_in_game_display(cfg, "slideshow")
        c, view, timers, video, submit, _ = make_controller(cfg=cfg)
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        n0 = len([x for x in c.resolver.calls if x[0] == "system_art"])
        timers.advance(0.0)
        timers.advance(6.9)
        self.assertEqual(len([x for x in c.resolver.calls if x[0] == "system_art"]), n0 + 1)
        timers.advance(0.2)
        self.assertEqual(len([x for x in c.resolver.calls if x[0] == "system_art"]), n0 + 2)

    def test_slideshow_falls_back_to_art_when_the_system_has_no_slides(self):
        """Bug 1 (test day, 24 Sep): the real device ran a Mega Drive game
        with no system_art on the panel, and "slideshow" ended up showing
        the idle CLOCK (whatever mode was applied just before it) forever.
        FakeResolver.system_art() always returns a path, so it never
        exercised the "this system has none" case - override it here the
        way a system with no scraped slides would behave."""
        c, view, timers, video, submit = self.start("slideshow")
        c.resolver.system_art = lambda system: None
        # even before the first tick fires, the game's own art is shown -
        # never a carried-over view from whatever mode ran before this one
        self.assertEqual(view.mode, "game")
        self.assertIsNotNone(view.image)
        timers.advance(0.0)                          # the tick that finds no system art
        self.assertEqual(view.mode, "game")           # still art, not stale/blank
        self.assertIsNotNone(view.image)
        self.assertEqual(c.ingame_mode_applied, "slideshow")   # the setting itself is honoured

    def test_slideshow_stuck_on_a_stale_mode_is_the_bug_this_fixes(self):
        """Reproduces the exact device sequence: clock is applied, then the
        owner picks slideshow for a system with no slide art. Without the
        fallback in _apply_in_game_mode/_ingame_slide_tick, the screen was
        left showing the clock (view_mode idle) while in_game_mode said
        "slideshow" - exactly the real state.json capture from test day."""
        c, view, timers, video, submit = self.start("clock")
        self.assertEqual(view.idle_mode, "clock")
        c.resolver.system_art = lambda system: None
        set_in_game_display(c.cfg_fn(), "slideshow")
        c.config_changed()
        timers.advance(0.0)
        # the fix: art appears (never left stuck on the clock, view.mode
        # "idle" with idle_mode "clock" leftover from the earlier mode)
        self.assertEqual(view.mode, "game")
        self.assertIsNotNone(view.image)

    def test_becoming_inactive_stops_the_hud_loop(self):
        calls = []

        def fn():
            calls.append(1)
            return {"battery_percent": 50}
        c, view, timers, video, submit = self.start("hud", hud_fn=fn)
        timers.advance(0.0)
        self.assertEqual(len(calls), 1)
        c.set_active(False)
        timers.advance(60.0)
        self.assertEqual(len(calls), 1)

    def test_hud_loop_resumes_once_the_settings_sheet_closes(self):
        """Bug 1 (test day): the owner changed in_game_display FROM the
        Settings screen, i.e. while the Command Center covered the
        companion (companion.active == False - see
        TestAppNavigation.test_swipe_down_...: "no video under the CC").
        Before the fix, set_active(True) never resumed the hud/slideshow
        timer loop that going inactive had cancelled - hud was left
        stuck on its "Reading device stats..." placeholder forever,
        exactly matching "hud... do[es] nothing"."""
        calls = []

        def fn():
            calls.append(1)
            return {"battery_percent": 50}
        c, view, timers, video, submit = self.start("hud", hud_fn=fn)
        timers.advance(0.0)
        self.assertEqual(len(calls), 1)
        c.set_active(False)             # Command Center / Settings opens over the companion
        c.set_active(True)              # ... and closes again; the game is still running
        timers.advance(companion_modes.HUD_INTERVAL_S * 2)
        self.assertGreater(len(calls), 1)

    def test_slideshow_loop_resumes_once_the_settings_sheet_closes(self):
        c, view, timers, video, submit = self.start("slideshow")
        timers.advance(0.0)
        n0 = len([x for x in c.resolver.calls if x[0] == "system_art"])
        c.set_active(False)
        c.set_active(True)
        interval = float(config.get_value(config.defaults(), ("companion", "idle_slideshow_interval_s")))
        timers.advance(interval + 1.0)
        n1 = len([x for x in c.resolver.calls if x[0] == "system_art"])
        self.assertGreater(n1, n0)

    def test_config_change_mid_game_switches_mode_live(self):
        c, view, timers, video, submit = self.start("art")
        self.assertEqual(view.mode, "game")
        set_in_game_display(c.cfg_fn(), "off")
        c.config_changed()
        self.assertEqual(view.mode, "idle")
        self.assertEqual(view.idle_mode, "blank")

    def test_video_never_plays_regardless_of_mode(self):
        for mode in companion_modes.IN_GAME_MODES:
            # hud_fn stubbed so this doesn't shell out to the real hud.py
            # sampler for every mode - only "hud"'s own tests exercise that.
            c, view, timers, video, submit = self.start(mode, hud_fn=lambda: None)
            timers.advance(3.0)
            self.assertIsNone(video.path, mode)


class TestIdleEvents(unittest.TestCase):
    """RV1-M5: no video behind ES's screensaver or a sleeping device."""

    def browse(self):
        c, view, timers, video, submit, cfg = make_controller()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        timers.advance(0.6)
        self.assertEqual(video.names(), ["play"])
        return c, view, timers, video

    def idle(self, c, kind):
        c.on_es_event(esevents.EsEvent(kind, "", "", "", (), "hook", 0.0, None))

    def test_screensaver_stops_video_and_its_end_allows_it_again(self):
        c, view, timers, video = self.browse()
        self.idle(c, esevents.SCREENSAVER_START)
        self.assertEqual(video.names(), ["play", "stop"])
        self.assertFalse(c.video_allowed())
        timers.advance(10)
        c.set_active(False)
        c.set_active(True)
        timers.advance(10)
        self.assertEqual(video.names(), ["play", "stop"])      # nothing while it runs
        self.assertEqual(c.video_stops["screensaver-start"], 1)
        self.idle(c, esevents.SCREENSAVER_STOP)
        timers.advance(0.6)
        self.assertEqual(video.names(), ["play", "stop", "play"])

    def test_sleep_and_wake(self):
        c, view, timers, video = self.browse()
        self.idle(c, esevents.SLEEP)
        self.assertEqual(video.names()[-1], "stop")
        c.on_es_event(game_ev(ROM_B, "Beta"))                  # a selection while asleep
        timers.advance(5)
        self.assertEqual(video.names()[-1], "stop")
        self.idle(c, esevents.WAKE)
        c.on_es_event(game_ev(ROM_A, "Alpha"))
        timers.advance(0.6)
        self.assertEqual(video.calls[-1][:2], ("play", "/v/alpha.mp4"))

    def test_the_idle_slideshow_pauses_too(self):
        cfg = config.defaults()
        config.set_value(cfg, ("companion", "idle_mode"), "slideshow")
        c, view, timers, video, submit, _ = make_controller(cfg=cfg)
        c.set_active(True)
        self.assertIsNotNone(c.slide_timer)
        self.idle(c, esevents.SCREENSAVER_START)
        self.assertIsNone(c.slide_timer)
        self.idle(c, esevents.SCREENSAVER_STOP)
        self.assertIsNotNone(c.slide_timer)

    def test_a_stop_without_a_start_is_harmless(self):
        c, view, timers, video = self.browse()
        self.idle(c, esevents.WAKE)
        timers.advance(1)
        self.assertEqual(video.names(), ["play"])


class TestGameEndRomCheck(unittest.TestCase):
    """RV4-N1: a game-end for a different ROM than the running game is late
    or stale and must not end the game (ES runs one game at a time)."""

    def test_mismatched_end_is_ignored_and_logged(self):
        c, view, timers, video, _, _ = make_controller()
        c.set_active(True)
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        with self.assertLogs("rp5deck.companion", "INFO") as cm:
            c.on_es_event(game_ev(ROM_B, "Beta", kind=esevents.GAME_END))
        self.assertIsNotNone(c.running)
        self.assertEqual(c.ignored_ends, 1)
        self.assertIn("ignored", "\n".join(cm.output))
        timers.advance(5)
        self.assertEqual(video.names(), [])                     # still no video
        c.on_es_event(game_ev(ROM_A, "Alpha", kind=esevents.GAME_END))
        self.assertIsNone(c.running)

    def test_unknown_rom_or_a_non_path_key_still_ends(self):
        c, view, timers, video, _, _ = make_controller()
        c.on_es_event(game_ev(ROM_A, kind=esevents.GAME_START))
        c.on_es_event(game_ev("", kind=esevents.GAME_END))      # a hook with no args
        self.assertIsNone(c.running)
        c.on_es_event(game_ev(ROM_A, kind=esevents.GAME_START, source="poll", game=G()))
        c.on_es_event(esevents.EsEvent(esevents.GAME_END, "gb", "1", "", (), "poll", 0.0, None))
        self.assertIsNone(c.running)                            # poll key = id, not a path


class TestVideoAudioOutput(unittest.TestCase):
    """RV2 minor / RV1-m10: a muted companion video opens no audio output."""

    def setUp(self):
        self.players = []

        def factory(path, w, h, **kw):
            p = FakePlayer(path, w, h, **kw)
            self.players.append(p)
            return p
        self.w = companion.VideoWorker(lambda: None, factory=factory, log_fn=lambda m: None)
        self.addCleanup(self.w.shutdown)

    def test_muted_uses_ao_null_and_a_mute_change_rebuilds_the_player(self):
        self.w.play("/v/a.mp4", 800, 600, mute=True)
        self.assertTrue(wait_for(lambda: len(self.players) == 1))
        self.assertEqual(self.players[0].kw.get("ao"), "null")
        self.assertTrue(self.players[0].kw.get("mute"))
        self.w.play("/v/b.mp4", 800, 600, mute=False)
        self.assertTrue(wait_for(lambda: len(self.players) == 2))
        self.assertTrue(wait_for(lambda: self.players[0].closed))
        self.assertIsNone(self.players[1].kw.get("ao"))           # mpv's default output
        self.w.play("/v/c.mp4", 800, 600, mute=False)             # same mute: reused
        self.assertTrue(wait_for(lambda: self.players[1].path == "/v/c.mp4"))
        self.assertEqual(len(self.players), 2)


class TestWarning(unittest.TestCase):
    def test_set_warning_shows_and_clears(self):
        c, view, timers, video, _, _ = make_controller()
        c.set_warning("2 game names would be run as shell code by ES's hooks - see the log")
        self.assertIn("2 game names", view.warning)
        self.assertEqual(c.state()["warning"], view.warning)
        c.set_warning("")
        self.assertEqual(view.warning, "")


# ---------------------------------------------------------------------------
# main.App, built without SDL
# ---------------------------------------------------------------------------
class SyncWorker:
    def __init__(self, post):
        self.post = post

    def submit(self, fn, *args, done=None):
        res = fn(*args)
        if done is not None:
            self.post(done, res)

    def stop(self, timeout=0):
        pass


class FakeLayer:
    def __init__(self):
        self.hidden = False
        self.closed = False
        self.can_present = True
        self.configured = True
        self.configure_count = 1
        self.geometry = (15, (0, 0), 0)
        self.calls = []

    def hide(self):
        self.hidden = True
        self.calls.append("hide")

    def show(self, anchor, size, zone):
        self.hidden = False
        self.geometry = (anchor, size, zone)
        self.calls.append(("show", anchor, size, zone))

    def set_geometry(self, anchor, size, zone):
        self.geometry = (anchor, size, zone)
        self.calls.append(("set_geometry", anchor, size, zone))

    def destroy(self):
        self.calls.append("destroy")


class FakeWl:
    ANCHOR_ALL, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT = 15, 2, 4, 8


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rp5deck-app-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: os.environ.get(k) for k in ("RP5DECK_CONFIG", "RP5DECK_AUDIO_DRYRUN",
                                                   "RP5DECK_RUN_DIR", "RP5DECK_ES_EVENT_FILE")}
        os.environ["RP5DECK_CONFIG"] = os.path.join(self.tmp, "config.json")
        os.environ["RP5DECK_AUDIO_DRYRUN"] = "1"
        os.environ["RP5DECK_RUN_DIR"] = os.path.join(self.tmp, "run")
        os.environ["RP5DECK_ES_EVENT_FILE"] = os.path.join(self.tmp, "run", "es-event")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def make_app(self, cfg_patch=None):
        if cfg_patch:
            with open(os.environ["RP5DECK_CONFIG"], "w") as f:
                json.dump(cfg_patch, f)
        app = main.App(argparse.Namespace(seconds=0, output=None))
        app.title = "Test Device Command Center"
        app.layer = FakeLayer()
        app.wl = FakeWl()
        app.mode = FULL
        app.video = FakeVideo()
        app.media_worker = SyncWorker(app.post)
        app.io_worker = SyncWorker(app.post)
        app.audio_worker = SyncWorker(app.post)
        app.build_ui(W, H)
        app.companion.resolver = FakeResolver({ROM_A: INFO_A, ROM_B: INFO_B})
        app.companion.load_image = fake_load_image
        app.post.drain()
        return app

    def es(self, app, ev):
        app.on_es_event(ev)
        app.post.drain()                # the resolver's result comes back via the loop

    def run_timers(self, app, dt):
        app._run_timers(time.monotonic() + dt)
        app.post.drain()

    def swipe(self, app, x0, y0, x1, y1, pid=("f", 7)):
        app.router.down(pid, x0, y0)
        steps = 6
        for i in range(1, steps + 1):
            app.router.move(pid, x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        app.router.up(pid, x1, y1)

    def tap(self, app, name):
        t = app.ui.targets()
        self.assertIn(name, t, sorted(t))
        x, y, w, h = t[name]
        app.router.down(("f", 1), x + w / 2, y + h / 2)
        app.router.up(("f", 1), x + w / 2, y + h / 2)
        app.post.drain()

    def summon(self, app):
        app.on_summon_button(summon.SummonEvent("btn_c_paddle", "DS5", "/dev/input/event9",
                                                summon.BTN_C_PADDLE_CODE, 1, 0.0, 0, 0))


class TestAppNavigation(AppCase):
    def test_starts_on_the_companion_covering_the_panel(self):
        app = self.make_app()
        self.assertEqual(app.ui.showing, "companion")
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.COMPANION)
        self.assertEqual(app.ui.companion.rect, (0, 0, W, H))
        t = app.ui.targets()
        self.assertIn("companion.tab", t)
        self.assertNotIn("bar.slider", t)
        self.assertTrue(app.companion.active)
        json.dumps(app.state(), default=str)            # state.json still serialises

    def test_swipe_down_from_the_top_opens_the_command_center_volume_first(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.COMMAND_CENTER)
        self.assertEqual(app.ui.showing, "cc")
        t = app.ui.targets()
        self.assertIn("bar.slider", t)
        self.assertEqual(t["bar.slider"][1], 10)          # the first row, top edge
        self.assertTrue(all(t["bar.slider"][1] < t[k][1] for k in
                            ("home.mixer", "home.hud", "home.settings", "home.discord")))
        self.assertFalse(app.companion.active)             # no video under the CC

    def test_the_hardware_button_closes_it_and_opens_it_again(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.summon(app)
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.COMPANION)
        self.assertEqual(app.ui.showing, "companion")
        self.assertTrue(app.companion.active)
        self.summon(app)
        self.assertEqual(app.ui.showing, "cc")
        self.assertEqual(app.summon_presses, 2)

    def test_a_swipe_that_starts_below_the_edge_does_nothing(self):
        app = self.make_app()
        self.swipe(app, 700, 300, 700, 800)
        self.assertEqual(app.ui.showing, "companion")

    def test_swipe_up_and_close_button_close_it(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.swipe(app, 900, 900, 900, 500)
        self.assertEqual(app.ui.showing, "companion")
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.close")
        self.assertEqual(app.ui.showing, "companion")

    def test_the_pull_tab_opens_it_even_with_swipe_and_button_disabled(self):
        app = self.make_app({"schema_version": 1, "command_center": {
            "swipe_down_enabled": False, "hardware_button": "none"}})
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "companion")
        self.summon(app)
        self.assertEqual(app.ui.showing, "companion")
        self.tap(app, "companion.tab")
        self.assertEqual(app.ui.showing, "cc")

    def test_auto_close_timeout(self):
        app = self.make_app({"schema_version": 1, "command_center": {"auto_close_timeout_s": 2}})
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "cc")
        app.pull._deadline = time.monotonic() - 0.01
        self.run_timers(app, 1.1)
        self.assertEqual(app.ui.showing, "companion")
        self.assertEqual(app.pull_log[-1][2], "timeout")

    def test_touch_restarts_the_auto_close_countdown(self):
        app = self.make_app({"schema_version": 1, "command_center": {"auto_close_timeout_s": 5}})
        self.swipe(app, 700, 20, 700, 400)
        app.pull._deadline = time.monotonic() - 0.01
        app._activity()
        self.run_timers(app, 1.1)
        self.assertEqual(app.ui.showing, "cc")

    def test_hidden_closes_it_and_bar_or_hidden_ignore_the_button_and_swipes(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        app.on_mode(HIDDEN, "foreign window")
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.COMPANION)
        self.assertEqual(app.layer.calls[-1], "hide")
        self.summon(app)
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.COMPANION)
        app.on_mode(BAR, "rp5deck-web")
        app.ui.set_size(W, screens.BAR_H)
        app._update_active()
        self.assertEqual(app.ui.showing, "bar")
        self.assertIn("bar.slider", app.ui.targets())
        self.swipe(app, 700, 5, 700, 130)
        self.summon(app)
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.COMPANION)
        self.assertFalse(app.companion.active)

    def test_bar_mode_battery_tap_does_not_open_a_hud(self):
        app = self.make_app()
        app.on_mode(BAR, "rp5deck-web")
        app.ui.set_size(W, screens.BAR_H)
        self.tap(app, "bar.battery")
        self.assertIsNone(app.ui.sheet)
        self.assertEqual(app.ui.showing, "bar")

    def test_video_stops_when_a_game_starts_through_the_app(self):
        app = self.make_app()
        self.es(app, game_ev(ROM_A, "Alpha"))
        self.run_timers(app, 0.7)
        self.assertEqual(app.video.names(), ["play"])
        self.es(app, game_ev(ROM_A, "Alpha", kind=esevents.GAME_START))
        self.assertEqual(app.video.names(), ["play", "stop"])
        self.run_timers(app, 5)
        self.assertEqual(app.video.names(), ["play", "stop"])
        app.hud_inflight = True
        app._hud_done({"running_game": None})
        self.assertEqual(app.hud_last["running_game"], "Alpha")

    def test_opening_the_command_center_stops_video(self):
        app = self.make_app()
        self.es(app, game_ev(ROM_A, "Alpha"))
        self.run_timers(app, 0.7)
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.video.names(), ["play", "stop"])


class TestAppServices(AppCase):
    def test_es_events_and_summon_reader_survive_absent_devices_and_stop(self):
        app = self.make_app()
        import es_api as _es
        orig = _es.running_game
        _es.running_game = lambda: None                 # no ES here: never reach the network
        self.addCleanup(setattr, _es, "running_game", orig)
        self.addCleanup(setattr, esevents, "probe_running_game", esevents.probe_running_game)
        esevents.probe_running_game = lambda: esevents.UNKNOWN   # (main may pass the probe)
        app.start_services()
        self.assertIsNotNone(app.es_watcher)
        self.assertTrue(app.summon_thread.is_alive())    # default binding btn_back_f1, no pad
        # a hook's event file, in the spool the app watches (derived from
        # RP5DECK_ES_EVENT_FILE's directory, as the hooks derive it)
        self.assertEqual(app.es_watcher.dir, os.path.join(self.tmp, "run", "es-events"))
        esevents.write_spool_event(app.es_watcher.dir, (1, 1, 1), esevents.GAME_SELECTED,
                                   ["gb", ROM_B, "Beta"])
        self.assertTrue(wait_for(lambda: (app.post.drain(), app.companion.selected)[1]
                                 is not None, 5.0))
        self.assertEqual(app.companion.selected.rom_path, ROM_B)
        t0 = time.monotonic()
        app.shutdown()
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertFalse(app.summon_thread.is_alive())
        self.assertFalse(any(t.is_alive() for t in app.es_watcher._threads))
        self.assertIn("shutdown", app.video.names())
        self.assertIn("destroy", app.layer.calls)
        with open(os.path.join(os.environ["RP5DECK_RUN_DIR"], "state.json")) as f:
            st = json.load(f)
        self.assertFalse(st["running"])
        self.assertEqual(st["version"], "I1")
        self.assertEqual(st["companion"]["target"]["rom"], ROM_B)

    def test_no_summon_reader_for_binding_none(self):
        app = self.make_app({"schema_version": 1, "command_center": {"hardware_button": "none"}})
        import es_api as _es
        orig = _es.running_game
        _es.running_game = lambda: None
        self.addCleanup(setattr, _es, "running_game", orig)
        self.addCleanup(setattr, esevents, "probe_running_game", esevents.probe_running_game)
        esevents.probe_running_game = lambda: esevents.UNKNOWN
        app.start_services()
        self.assertIsNone(app.summon_reader)
        app.shutdown()


class TestAppVolumeOverlay(AppCase):
    def master(self, v, muted=False):
        return {"state": "ok", "volume": v, "muted": muted, "sink_description": "Speaker"}

    def test_overlay_only_while_the_command_center_is_closed(self):
        app = self.make_app()
        osd = app.companion.view.osd
        app.apply_master(self.master(0.30))
        self.assertFalse(osd.visible)                   # the first read is not a change
        app.apply_master(self.master(0.35))
        self.assertTrue(osd.visible)
        self.assertEqual((osd.volume, osd.muted), (0.35, False))
        self.run_timers(app, main.OSD_SECONDS + 0.1)
        self.assertFalse(osd.visible)
        self.swipe(app, 700, 20, 700, 400)              # CC open: its own slider shows it
        app.apply_master(self.master(0.40))
        self.assertFalse(osd.visible)
        self.assertEqual(app.osd_shows, 1)
        self.summon(app)
        app.apply_master(self.master(0.40, muted=True))
        self.assertTrue(osd.visible)
        self.assertTrue(osd.muted)

    def test_no_overlay_when_the_setting_is_off_or_audio_is_not_ok(self):
        app = self.make_app({"schema_version": 1, "audio": {"show_volume_overlay": False}})
        app.apply_master(self.master(0.30))
        app.apply_master(self.master(0.50))
        self.assertFalse(app.companion.view.osd.visible)
        app = self.make_app()
        app.apply_master(self.master(0.30))
        app.apply_master({"state": "restoring", "volume": None, "muted": None})
        app.apply_master(self.master(0.30))
        self.assertFalse(app.companion.view.osd.visible)

    def test_opening_the_command_center_hides_a_showing_overlay(self):
        app = self.make_app()
        app.apply_master(self.master(0.30))
        app.apply_master(self.master(0.31))
        self.swipe(app, 700, 20, 700, 400)
        self.assertFalse(app.companion.view.osd.visible)


class TestAppSettings(AppCase):
    def test_gear_opens_full_panel_settings_back_returns_home(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.SETTINGS)
        self.assertEqual(app.ui.sheet, "settings")
        self.assertEqual(app.ui.settings.rect, (0, 0, W, H))
        self.assertNotIn("bar.slider", app.ui.targets())
        self.tap(app, "settings.back")
        self.assertEqual(app.pull.state, summon.PullDownStateMachine.COMMAND_CENTER)
        self.assertIsNone(app.ui.sheet)
        self.assertIn("bar.slider", app.ui.targets())

    def test_a_control_on_another_tab_is_tappable_after_switching(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.tap(app, "settings.tab.audio")
        key = "settings.row.audio.show_volume_overlay.control"
        t = app.ui.targets()
        self.assertIn(key, t)
        x, y, w, h = t[key]
        self.assertGreater(w * h, 0)
        self.assertGreater(y, 250)                      # laid out on the page, not at 0,0
        self.tap(app, key)
        self.assertFalse(config.get_value(app.cfg, ("audio", "show_volume_overlay")))

    def test_live_settings_apply_and_everything_is_saved(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        s = app.ui.settings
        s.controls[("command_center", "swipe_down_enabled")].clicked()
        self.assertFalse(app.pull.swipe_down_enabled)
        s.controls[("command_center", "swipe_sensitivity")].clicked()   # medium -> high
        self.assertEqual((app.gestures.edge, app.gestures.distance), (90, 80))
        s.controls[("command_center", "hardware_button")].clicked()     # restart-flagged
        self.assertEqual(app.pull.hardware_button, "btn_back_f1")        # not applied live
        self.run_timers(app, main.SAVE_DELAY + 0.1)
        with open(os.environ["RP5DECK_CONFIG"]) as f:
            saved = json.load(f)
        self.assertFalse(saved["command_center"]["swipe_down_enabled"])
        self.assertEqual(saved["command_center"]["swipe_sensitivity"], "high")
        self.assertEqual(saved["command_center"]["hardware_button"], "btn_c_paddle")
        self.assertEqual(app.config_saves, 1)
        self.tap(app, "settings.back")
        self.tap(app, "home.close")
        self.swipe(app, 700, 20, 700, 400)
        self.assertEqual(app.ui.showing, "companion")    # swipe-down is now off

    def test_companion_settings_redraw_the_companion(self):
        app = self.make_app()
        self.es(app, game_ev(ROM_A, "Alpha"))
        self.assertEqual(app.companion.view.image.path, "/i/alpha-title.png")
        config.set_value(app.cfg, ("companion", "media_priority"),
                         media_priority_with_first("image", "video"))
        app.on_setting(("companion", "media_priority"), app.cfg["companion"]["media_priority"])
        app.post.drain()
        self.assertEqual(app.companion.view.image.path, "/i/alpha.png")


class TestAppMixerPaging(AppCase):
    def test_more_than_five_streams_page(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        app.ui.open("mixer")
        streams = [{"id": 100 + i, "display_name": "app%d" % i, "volume": 0.5, "muted": False}
                   for i in range(7)]
        app.ui.mixer.set_streams(streams)
        self.assertEqual(sorted(app.ui.mixer.rows), [100, 101, 102, 103, 104])
        self.assertIn("1 / 2", app.ui.mixer.title.text)
        t = app.ui.targets()
        self.assertNotIn("mixer.prev", t)               # disabled on page 1
        self.tap(app, "mixer.next")
        self.assertEqual(sorted(app.ui.mixer.rows), [105, 106])
        self.assertIn("mixer.slider.106", app.ui.targets())
        self.assertNotIn("mixer.next", app.ui.targets())
        self.tap(app, "mixer.prev")
        self.assertEqual(sorted(app.ui.mixer.rows), [100, 101, 102, 103, 104])
        rows = [r.rect for r in app.ui.mixer.rows.values()]
        self.assertTrue(all(r[1] + r[3] <= H for r in rows))
        app.ui.mixer.set_streams(streams[:3])           # shrinking resets onto a valid page
        self.assertEqual(len(app.ui.mixer.rows), 3)
        self.assertFalse(app.ui.mixer.next.visible)


def _gfx():
    try:
        import gfx
        media._lib("cairo")
        return gfx
    except Exception:           # noqa: BLE001 - no cairo/pango here (the Windows PC)
        return None


@unittest.skipUnless(_gfx(), "needs libcairo + libpango (WSL, the device)")
class TestRealRendering(AppCase):
    """Paints the real widget tree into a real cairo Canvas - the drawing
    code runs for real, and pixels are checked. RP5DECK_RENDER_DIR=<dir>
    also writes each state as a PNG for a human to look at."""

    def setUp(self):
        AppCase.setUp(self)
        self.gfx = _gfx()
        import ctypes
        self.ct = ctypes
        # a real 640x480 PNG "screenshot" as game art, drawn with cairo
        art = self.gfx.Canvas(640, 480)
        art.fill_rect((0, 0, 640, 480), (0.1, 0.5, 0.9))
        art.fill_rect((0, 0, 320, 240), (0.9, 0.2, 0.2))
        art.text("ART", (0, 180, 640, 120), 100, (1, 1, 1), True, "center")
        self.art = os.path.join(self.tmp, "Alpha-titleshot.png")
        self._write_png(art, self.art)
        art.free()
        self.pages = []
        for i in (1, 2):
            pg = self.gfx.Canvas(700, 900)
            pg.fill_rect((0, 0, 700, 900), (0.95, 0.93, 0.85))
            pg.text("Page %d" % i, (0, 380, 700, 140), 90, (0.1, 0.1, 0.1), True, "center")
            p = os.path.join(self.tmp, "p%d.png" % i)
            self._write_png(pg, p)
            pg.free()
            self.pages.append(p)

    def _write_png(self, canvas, path):
        f = self.gfx._cairo.cairo_surface_write_to_png
        f.argtypes = [self.ct.c_void_p, self.ct.c_char_p]
        canvas.pixels()
        self.assertEqual(f(canvas.surf, os.fsencode(path)), 0)

    def render(self, app, name):
        canvas = self.gfx.Canvas(W, H)
        self.addCleanup(canvas.free)
        with canvas.clipped((0, 0, W, H)):
            app.ui.root.paint(canvas)
        out = os.environ.get("RP5DECK_RENDER_DIR")
        if out:
            os.makedirs(out, exist_ok=True)
            self._write_png(canvas, os.path.join(out, "i1-pc-render-%s.png" % name))
        data, stride = canvas.pixels()

        def px(x, y):
            return tuple(self.ct.string_at(data + int(y) * stride + int(x) * 4, 4))   # B,G,R,A
        return px

    def app_with_art(self, **info_extra):
        app = self.make_app()
        info = {"kind": "game", "title": "Alpha's Quest & Friends", "developer": "Dev Co",
                "releasedate": "19950101T000000", "players": "1-2",
                "desc": "A long description " * 20,
                "media": {"titleshot": self.art, "video": "/v/alpha.mp4"},
                "manual": "/m/alpha.pdf"}
        info.update(info_extra)
        app.companion.resolver = FakeResolver({ROM_A: info})
        app.companion.load_image = media.load_image
        return app

    def test_idle_clock(self):
        app = self.make_app()
        px = self.render(app, "companion-idle-clock")
        self.assertEqual(px(10, 10), (0, 0, 0, 255))            # black, opaque, whole panel
        self.assertEqual(px(W - 10, H - 10), (0, 0, 0, 255))

    def test_game_art_band_and_manual_button(self):
        app = self.app_with_art()
        config.set_value(app.cfg, ("companion", "show_metadata", "description"), True)
        self.es(app, game_ev(ROM_A, "Alpha"))
        self.assertTrue(app.companion.view.manual_btn.visible)
        px = self.render(app, "companion-game-art")
        b, g, r, a = px(W / 2, H / 3)
        self.assertEqual(a, 255)
        self.assertGreater(b, r)                                # the blue art, dimmed a little
        self.assertEqual(px(W / 2, 3)[3], 255)                  # no transparent pixel anywhere
        self.assertEqual(px(W - 10, H - 10)[3], 255)

    def test_video_hole_is_transparent_and_the_band_is_not(self):
        app = self.app_with_art()
        self.es(app, game_ev(ROM_A, "Alpha"))
        self.run_timers(app, 0.7)
        app.companion.video_frame_ready(640, 480)
        px = self.render(app, "companion-video-hole")
        dim = config.get_value(app.cfg, ("companion", "background_dim"))
        self.assertEqual(px(W / 2, H / 3)[3], int(round(dim * 255)))   # only the dim veil
        veil_and_band = 1 - (1 - dim) * (1 - companion.BAND[3])
        self.assertAlmostEqual(px(60, H - 40)[3], veil_and_band * 255, delta=2)  # text band
        mb = app.companion.view.manual_btn.rect
        self.assertEqual(px(mb[0] + mb[2] / 2, mb[1] + 10)[3], 255)    # the button is solid

    def test_system_view_text_only(self):
        app = self.make_app()
        app.companion.resolver = FakeResolver({("system", "snes"): {
            "kind": "system", "title": "Super Nintendo", "total_games": 831, "media": {},
            "logo": None}})
        self.es(app, esevents.interpret(esevents.SYSTEM_SELECTED, ["snes", "", ""]))
        self.render(app, "companion-system")

    def test_command_center_volume_strip_on_top(self):
        app = self.make_app()
        app.apply_master({"state": "ok", "volume": 0.42, "muted": False,
                          "sink_description": "Speaker"})
        self.swipe(app, 700, 20, 700, 400)
        px = self.render(app, "command-center-home")
        bar = tuple(int(round(c * 255)) for c in ui.THEME["bar"][:3])
        self.assertEqual(px(4, 70)[:3][::-1], bar)             # the strip is the first row
        self.assertEqual(px(10, 1070)[3], 255)

    def test_settings_mixer_hud_and_osd_draw(self):
        app = self.make_app()
        self.swipe(app, 700, 20, 700, 400)
        self.tap(app, "home.settings")
        self.render(app, "settings-companion")
        self.tap(app, "settings.tab.command_center")
        self.render(app, "settings-command-center")
        self.tap(app, "settings.back")
        app.ui.open("mixer")
        app.ui.mixer.set_streams([{"id": 100 + i, "display_name": "app %d" % i, "volume": 0.6,
                                   "muted": i == 2} for i in range(7)])
        self.render(app, "mixer-paged")
        app.ui.close()
        app.open_hud()
        app.ui.hud.set_sample({"battery_percent": 77})
        self.render(app, "hud")
        self.summon(app)
        app.apply_master({"state": "ok", "volume": 0.3, "muted": False})
        app.apply_master({"state": "ok", "volume": 0.55, "muted": False})
        px = self.render(app, "companion-volume-osd")
        osd = app.companion.view.osd.rect
        self.assertGreater(px(osd[0] + 20, osd[1] + osd[3] / 2)[3], 200)

    def test_manual_spread(self):
        app = self.app_with_art()
        self.es(app, game_ev(ROM_A, "Alpha"))
        fm = FakeManuals(pages=2)
        pages = self.pages
        fm.render_spread = lambda pdf, left, height=1080, total_pages=None: (pages[0], pages[1])
        fm.png_size = lambda p: (700, 900)
        app.companion.manuals = fm
        app.open_manual()
        app.post.drain()
        px = self.render(app, "manual-spread")
        (li, lx, ly), (ri, rx, ry) = app.companion.view.manual.pages
        b, g, r, a = px(lx + 20, ly + 20)
        self.assertGreater(r, 200)                              # the cream page, not black


if __name__ == "__main__":
    unittest.main()
