#!/usr/bin/env python3
"""media.py: pure logic (geometry, JPEG DCT factor choice, sniffing, strides,
buffers, state machine, mpv options, frame signal) and the VideoPlayer
lifecycle against a fake libmpv. Native-library tests (cairo, libmpv) run
only where those libraries load - i.e. on the device - and skip on the PC."""
import collections
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import media  # noqa: E402
from media import (CROP, FILL, FIT, MediaError, VideoPlayer, fit_geometry,  # noqa: E402
                   next_state)


# ---------------------------------------------------------------------------
class TestGeometry(unittest.TestCase):
    def test_fit_landscape_into_wider_box_is_height_bound(self):
        g = fit_geometry(640, 480, 1920, 1080, FIT)
        self.assertEqual((g.out_w, g.out_h), (1440, 1080))
        self.assertAlmostEqual(g.sx, 2.25)
        self.assertAlmostEqual(g.sy, 2.25)
        self.assertEqual((g.ox, g.oy), (0.0, 0.0))

    def test_fit_portrait_box_art(self):
        g = fit_geometry(256, 384, 270, 270, FIT)       # real Just Sing! cover
        self.assertEqual((g.out_w, g.out_h), (180, 270))

    def test_fit_never_exceeds_box(self):
        for sw, sh in ((1, 1000), (1000, 1), (333, 777), (1921, 1079)):
            g = fit_geometry(sw, sh, 480, 270, FIT)
            self.assertLessEqual(g.out_w, 480)
            self.assertLessEqual(g.out_h, 270)
            self.assertGreaterEqual(min(g.out_w, g.out_h), 1)

    def test_fit_without_upscale_keeps_small_art_native(self):
        g = fit_geometry(256, 224, 1920, 1080, FIT, upscale=False)
        self.assertEqual((g.out_w, g.out_h), (256, 224))
        self.assertEqual((g.sx, g.sy), (1.0, 1.0))

    def test_fill_stretches_exactly(self):
        g = fit_geometry(600, 200, 480, 270, FILL)
        self.assertEqual((g.out_w, g.out_h), (480, 270))
        self.assertAlmostEqual(g.sx, 0.8)
        self.assertAlmostEqual(g.sy, 1.35)

    def test_crop_covers_box_and_centres_overflow(self):
        g = fit_geometry(600, 200, 480, 270, CROP)
        self.assertEqual((g.out_w, g.out_h), (480, 270))
        self.assertAlmostEqual(g.sx, 1.35)
        self.assertAlmostEqual(g.sx, g.sy)
        # scaled width 810 overflows 480 by 330 -> 165 cut on each side
        self.assertAlmostEqual(g.ox, -165.0)
        self.assertAlmostEqual(g.oy, 0.0)
        self.assertGreaterEqual(600 * g.sx + g.ox, g.out_w - 1e-9)   # right edge covered

    def test_crop_same_aspect_has_no_offset(self):
        g = fit_geometry(1920, 1080, 480, 270, CROP)
        self.assertEqual((g.out_w, g.out_h, g.ox, g.oy), (480, 270, 0.0, 0.0))
        self.assertAlmostEqual(g.sx, 0.25)

    def test_aliases_and_bad_input(self):
        self.assertEqual(fit_geometry(10, 10, 5, 5, "contain"), fit_geometry(10, 10, 5, 5, FIT))
        self.assertEqual(fit_geometry(10, 20, 5, 5, "cover"), fit_geometry(10, 20, 5, 5, CROP))
        self.assertEqual(fit_geometry(10, 20, 5, 5, "Stretch"), fit_geometry(10, 20, 5, 5, FILL))
        with self.assertRaises(ValueError):
            fit_geometry(10, 10, 5, 5, "zoom")
        with self.assertRaises(ValueError):
            fit_geometry(0, 10, 5, 5)
        with self.assertRaises(ValueError):
            fit_geometry(10, 10, 5, -1)

    def test_place(self):
        self.assertEqual(media.place(1440, 1080, 1920, 1080), (240, 0))
        self.assertEqual(media.place(100, 50, 200, 100, "left", "bottom"), (0, 50))
        self.assertEqual(media.place(100, 50, 200, 100, "right", "top"), (100, 0))


class TestJpegFactor(unittest.TestCase):
    def test_scaled_dim_rounds_up_like_tjscaled(self):
        self.assertEqual(media.scaled_dim(1920, (1, 8)), 240)
        self.assertEqual(media.scaled_dim(1921, (1, 8)), 241)
        self.assertEqual(media.scaled_dim(7, (3, 8)), 3)

    def test_quarter_is_enough_for_quarter_box(self):
        g = fit_geometry(1920, 1080, 480, 270, FIT)
        need = media.needed_source_size(1920, 1080, g)
        self.assertEqual(need, (480, 270))
        self.assertEqual(media.pick_jpeg_factor(1920, 1080, *need), (2, 8))

    def test_picks_smallest_factor_that_still_covers(self):
        f = media.pick_jpeg_factor(1920, 1080, 500, 280)
        self.assertEqual(f, (3, 8))          # 2/8 = 480 would be too small
        w = media.scaled_dim(1920, f)
        self.assertGreaterEqual(w, 500)

    def test_full_size_when_box_is_bigger_than_source(self):
        self.assertEqual(media.pick_jpeg_factor(256, 384, 256, 384), (8, 8))
        self.assertEqual(media.pick_jpeg_factor(256, 384, 9999, 9999), (8, 8))

    def test_needed_size_capped_at_source(self):
        g = fit_geometry(256, 224, 1920, 1080, FIT)   # upscaling
        self.assertEqual(media.needed_source_size(256, 224, g), (256, 224))


class TestBytes(unittest.TestCase):
    def test_sniff_by_magic_not_extension(self):
        self.assertEqual(media.sniff(b"\x89PNG\r\n\x1a\n" + b"\0" * 8), "png")
        self.assertEqual(media.sniff(b"\xff\xd8\xff\xe0\0\x10JFIF"), "jpeg")
        self.assertEqual(media.sniff(b"RIFF\x10\0\0\0WEBPVP8 "), "webp")
        self.assertEqual(media.sniff(b"GIF89a\x01\0"), "gif")
        self.assertIsNone(media.sniff(b"<html><body>"))
        self.assertIsNone(media.sniff(b""))
        self.assertIsNone(media.sniff(b"RIFF\x10\0\0\0WAVEfmt "))

    def test_stride(self):
        self.assertEqual(media.stride_for(1920, 4, 64), 7680)
        self.assertEqual(media.stride_for(961, 4, 64), 3904)
        self.assertEqual(media.stride_for(3, 4, 4), 12)
        self.assertEqual(media.stride_for(3, 3, 4), 12)
        with self.assertRaises(ValueError):
            media.stride_for(0)

    def test_aligned_buffer(self):
        for align in (16, 64):
            b = media.AlignedBuffer(1000, align)
            self.assertEqual(b.address % align, 0)
            self.assertEqual(b.size, 1000)
            v = b.view()
            v[0], v[999] = 7, 9
            self.assertEqual(b.tobytes()[0], 7)
            self.assertEqual(b.tobytes()[999], 9)
            b.clear(3)
            self.assertEqual(set(b.tobytes()), {3})

    def test_buffer_address_refuses_small_or_readonly(self):
        b = media.AlignedBuffer(100)
        self.assertEqual(media.buffer_address(b, 100), b.address)
        with self.assertRaises(ValueError):
            media.buffer_address(b, 101)
        ba = bytearray(64)
        self.assertIsInstance(media.buffer_address(ba, 64), int)
        with self.assertRaises(ValueError):
            media.buffer_address(ba, 65)
        with self.assertRaises(TypeError):
            media.buffer_address(b"x" * 64, 10)
        self.assertEqual(media.buffer_address(12345, 10**9), 12345)   # raw pointer: caller vouches


class TestStateMachine(unittest.TestCase):
    def test_normal_lifecycle(self):
        s = media.IDLE
        s = next_state(s, "start-file")
        self.assertEqual(s, media.LOADING)
        s = next_state(s, "file-loaded")
        self.assertEqual(s, media.PLAYING)
        s = next_state(s, "pause")
        self.assertEqual(s, media.PAUSED)
        s = next_state(s, "resume")
        self.assertEqual(s, media.PLAYING)
        s = next_state(s, "end-file", end_reason=media.END_EOF)
        self.assertEqual(s, media.ENDED)

    def test_loaded_while_paused(self):
        self.assertEqual(next_state(media.LOADING, "file-loaded", paused=True), media.PAUSED)

    def test_end_reasons(self):
        self.assertEqual(next_state(media.PLAYING, "end-file", end_reason=media.END_ERROR), media.ERROR)
        self.assertEqual(next_state(media.PLAYING, "end-file", end_reason=media.END_STOP), media.STOPPED)
        self.assertEqual(next_state(media.PLAYING, "end-file", end_reason=media.END_QUIT), media.STOPPED)

    def test_pause_only_from_playing_and_closed_is_final(self):
        self.assertEqual(next_state(media.ENDED, "pause"), media.ENDED)
        self.assertEqual(next_state(media.STOPPED, "resume"), media.STOPPED)
        self.assertEqual(next_state(media.CLOSED, "start-file"), media.CLOSED)
        self.assertEqual(next_state(media.PLAYING, "shutdown"), media.CLOSED)


class TestOptions(unittest.TestCase):
    def d(self, **kw):
        return dict(media.build_options(**kw))

    def test_headless_render_api_only(self):
        o = self.d()
        self.assertEqual(o["vo"], "libmpv")
        self.assertEqual(o["config"], "no")
        self.assertEqual(o["input-default-bindings"], "no")
        self.assertEqual(o["terminal"], "no")
        self.assertNotIn("ao", o)                       # only when asked
        self.assertEqual(self.d(ao="null")["ao"], "null")

    def test_defaults_muted_looping_bilinear(self):
        o = self.d()
        self.assertEqual((o["mute"], o["loop-file"]), ("yes", "inf"))
        self.assertEqual(o["sws-scaler"], "bilinear")
        self.assertEqual(o["hwdec"], "no")
        o = self.d(mute=False, loop=False, hwdec="auto-copy", scaler=None)
        self.assertEqual((o["mute"], o["loop-file"], o["hwdec"]), ("no", "no", "auto-copy"))
        self.assertNotIn("sws-scaler", o)

    def test_modes_map_to_mpv_scaling(self):
        self.assertEqual((self.d(mode=FIT)["keepaspect"], self.d(mode=FIT)["panscan"]), ("yes", "0.0"))
        self.assertEqual(self.d(mode="cover")["panscan"], "1.0")
        self.assertEqual(self.d(mode=FILL)["keepaspect"], "no")

    def test_extra_last_and_soft_list(self):
        opts = media.build_options(extra={"vd-lavc-threads": 4})
        self.assertEqual(opts[-1], ("vd-lavc-threads", "4"))
        soft = dict(media.SOFT_OPTIONS)
        for k in ("load-stats-overlay", "load-console", "load-select", "load-commands"):
            self.assertEqual(soft[k], "no")


class TestFrameSignal(unittest.TestCase):
    def test_coalesces_bursts_across_threads(self):
        woke = []
        s = media.FrameSignal(lambda: woke.append(1))
        ts = [threading.Thread(target=s.set) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertTrue(s.take())
        self.assertFalse(s.take())
        self.assertEqual(len(woke), 8)
        self.assertEqual(s.count, 8)

    def test_wake_exception_never_escapes(self):
        def boom():
            raise RuntimeError("x")
        s = media.FrameSignal(boom)
        s.set()                              # must not raise into mpv's thread
        self.assertTrue(s.wait(0))


# ---------------------------------------------------------------------------
class FakeMpv:
    """Stands in for media._MpvLib; records every call in order."""

    def __init__(self, fail_options=(), fail_init=False):
        self.calls = []
        self.events = collections.deque()
        self.flags = 0
        self.update_cb = None
        self.wakeup_cb = None
        self.props = {}
        self.fail_options = set(fail_options)
        self.fail_init = fail_init

    def create(self):
        self.calls.append(("create",))
        return 0x1000

    def set_option(self, h, k, v):
        self.calls.append(("opt", k, v))
        if k in self.fail_options:
            raise MediaError("option %s=%s: option not found" % (k, v))

    def initialize(self, h):
        self.calls.append(("init",))
        if self.fail_init:
            raise MediaError("mpv_initialize: boom")

    def request_log(self, h, level):
        self.calls.append(("log", level))

    def render_create(self, h):
        self.calls.append(("render_create",))
        return 0x2000

    def set_update_callback(self, ctx, fn):
        self.calls.append(("update_cb", fn is not None))
        self.update_cb = fn

    def set_wakeup(self, h, fn):
        self.calls.append(("wakeup_cb", fn is not None))
        self.wakeup_cb = fn

    def update(self, ctx):
        self.calls.append(("update",))
        return self.flags

    def render_sw(self, ctx, w, h, fmt, stride, addr):
        self.calls.append(("render", w, h, fmt, stride, addr))

    def render_free(self, ctx):
        self.calls.append(("render_free",))

    def terminate_destroy(self, h):
        self.calls.append(("destroy",))

    def set_property(self, h, k, v):
        self.calls.append(("prop", k, v))
        self.props[k] = v

    def get_property(self, h, k):
        return self.props.get(k)

    def command(self, h, args):
        self.calls.append(("cmd",) + tuple(args))

    def wait_event(self, h, timeout=0.0):
        return self.events.popleft() if self.events else None

    def names(self):
        return [c[0] for c in self.calls]


def player(fake=None, **kw):
    fake = fake or FakeMpv()
    kw.setdefault("path", "/v.mp4")
    return VideoPlayer(kw.pop("path"), kw.pop("w", 320), kw.pop("h", 180),
                       _lib_obj=fake, **kw), fake


class TestVideoPlayer(unittest.TestCase):
    def test_setup_order(self):
        p, f = player(ao="null")
        n = f.names()
        self.assertLess(n.index("opt"), n.index("init"))
        self.assertLess(n.index("init"), n.index("render_create"))
        self.assertLess(n.index("render_create"), n.index("update_cb"))
        opts = {c[1]: c[2] for c in f.calls if c[0] == "opt"}
        self.assertEqual((opts["vo"], opts["ao"], opts["mute"]), ("libmpv", "null", "yes"))
        self.assertIn(("cmd", "loadfile", "/v.mp4", "replace"), f.calls)
        self.assertEqual(p.state, media.LOADING)
        p.close()

    def test_unknown_soft_option_is_tolerated(self):
        p, f = player(FakeMpv(fail_options={"load-select"}))
        self.assertEqual(p.soft_failures, ["load-select"])
        self.assertIn("init", f.names())
        p.close()

    def test_failed_init_tears_down(self):
        f = FakeMpv(fail_init=True)
        with self.assertRaises(MediaError):
            player(f)
        self.assertEqual(f.names()[-1], "destroy")
        self.assertNotIn("render_create", f.names())

    def test_poll_only_calls_update_after_a_signal(self):
        p, f = player()
        f.flags = media.MPV_RENDER_UPDATE_FRAME
        self.assertFalse(p.poll_frame())
        self.assertNotIn("update", f.names())
        t = threading.Thread(target=f.update_cb)       # mpv's thread
        t.start()
        t.join()
        self.assertTrue(p.poll_frame())
        self.assertFalse(p.poll_frame())               # coalesced, consumed
        f.wakeup_cb()
        f.flags = 0
        self.assertFalse(p.poll_frame())               # event wakeup, no frame
        self.assertEqual(f.names().count("update"), 2)
        p.close()

    def test_wake_is_forwarded(self):
        woke = []
        p, f = player(wake=lambda: woke.append(1))
        f.update_cb()
        f.wakeup_cb()
        self.assertEqual(len(woke), 2)
        p.close()

    def test_events_drive_state_and_error(self):
        p, f = player()
        f.events.extend([("start-file", 0, None), ("file-loaded", 0, None),
                         ("log", 0, ("warn", "vd", "x"))])
        f.update_cb()
        p.poll_frame()
        self.assertEqual(p.state, media.PLAYING)
        self.assertEqual(list(p.log)[-1], ("warn", "vd", "x"))
        f.events.append(("end-file", 0, (media.END_ERROR, "loading failed")))
        f.update_cb()
        p.poll_frame()
        self.assertEqual((p.state, p.error), (media.ERROR, "loading failed"))
        p.close()

    def test_start_after_end_reloads(self):
        p, f = player(loop=False)
        f.events.extend([("start-file", 0, None), ("file-loaded", 0, None),
                         ("end-file", 0, (media.END_EOF, ""))])
        f.update_cb()
        p.poll_frame()
        self.assertEqual(p.state, media.ENDED)
        f.calls.clear()
        p.start()
        self.assertIn(("cmd", "loadfile", "/v.mp4", "replace"), f.calls)
        self.assertIn(("prop", "pause", "no"), f.calls)

    def test_controls(self):
        p, f = player()
        f.events.extend([("start-file", 0, None), ("file-loaded", 0, None)])
        f.update_cb()
        p.poll_frame()
        p.pause()
        self.assertEqual((p.state, f.props["pause"]), (media.PAUSED, "yes"))
        p.resume()
        self.assertEqual((p.state, f.props["pause"]), (media.PLAYING, "no"))
        p.set_mute(False)
        p.set_loop(False)
        self.assertEqual((f.props["mute"], f.props["loop-file"]), ("no", "no"))
        p.seek(12.5)
        self.assertIn(("cmd", "seek", "12.500", "absolute"), f.calls)
        p.seek(-2, absolute=False)
        self.assertIn(("cmd", "seek", "-2.000", "relative"), f.calls)
        p.stop()
        self.assertEqual(p.state, media.STOPPED)
        self.assertIn(("cmd", "stop"), f.calls)
        p.close()

    def test_render_targets(self):
        p, f = player(w=320, h=180)
        addr, stride = p.render()
        self.assertEqual(stride, 1280)
        self.assertEqual(addr % 64, 0)
        self.assertEqual(f.calls[-1], ("render", 320, 180, "bgr0", 1280, addr))
        buf = bytearray(1280 * 180)
        p.render_into(buf, 1280)
        with self.assertRaises(ValueError):
            p.render_into(bytearray(1280 * 179), 1280)     # too small: refused
        with self.assertRaises(ValueError):
            p.render_into(buf, 1276)                       # stride < w*4
        self.assertEqual(p.frames_rendered, 2)
        p.resize(640, 360)
        self.assertEqual((p.w, p.h, p.stride, p.buffer.size), (640, 360, 2560, 2560 * 360))
        self.assertTrue(p.signal.take())                   # resize asks for a redraw
        p.close()

    def test_close_order_idempotent_and_final(self):
        p, f = player()
        f.calls.clear()
        p.close()
        self.assertEqual(f.calls, [("update_cb", False), ("render_free",),
                                   ("wakeup_cb", False), ("destroy",)])
        p.close()
        self.assertEqual(len(f.calls), 4)                  # second close: no calls
        self.assertEqual(p.state, media.CLOSED)
        self.assertFalse(p.poll_frame())
        with self.assertRaises(MediaError):
            p.pause()
        with self.assertRaises(MediaError):
            p.render()

    def test_context_manager_closes(self):
        f = FakeMpv()
        with VideoPlayer("/v.mp4", 64, 64, _lib_obj=f):
            pass
        self.assertEqual(f.names()[-1], "destroy")


# ---------------------------------------------------------------------------
def _have(name):
    return media.available().get(name) is True


@unittest.skipUnless(_have("cairo"), "libcairo not available (PC)")
class TestCairoImages(unittest.TestCase):
    """Device only: build a PNG with cairo, then decode and scale it."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.png = os.path.join(self.dir, "t.png")
        c = media._lib("cairo")
        w, h = 200, 100                        # left half red, right half blue
        s = c.surface_create(media.CAIRO_FORMAT_ARGB32, w, h)
        data, stride = c.get_data(s), c.get_stride(s)
        import ctypes
        for y in range(h):
            row = bytes((0, 0, 255, 255)) * (w // 2) + bytes((255, 0, 0, 255)) * (w // 2)
            ctypes.memmove(data + y * stride, row, len(row))
        c.surface_mark_dirty(s)
        c.write_to_png(s, self.png.encode())
        c.surface_destroy(s)

    def tearDown(self):
        os.remove(self.png)
        os.rmdir(self.dir)

    def test_fit_keeps_colours_and_size(self):
        img = media.load_image(self.png, 100, 100, FIT)
        self.assertEqual((img.w, img.h), (100, 50))
        self.assertEqual(img.pixel(5, 25), (0, 0, 255, 255))      # red = B,G,R,A
        self.assertEqual(img.pixel(95, 25), (255, 0, 0, 255))     # blue

    def test_crop_cuts_evenly(self):
        img = media.load_image(self.png, 50, 50, CROP)
        self.assertEqual((img.w, img.h), (50, 50))
        self.assertEqual(img.pixel(2, 25), (0, 0, 255, 255))
        self.assertEqual(img.pixel(47, 25), (255, 0, 0, 255))

    def test_non_image_is_refused(self):
        bad = os.path.join(self.dir, "x.png")
        with open(bad, "wb") as f:
            f.write(b"<html>not an image</html>")
        try:
            with self.assertRaises(MediaError):
                media.load_image(bad, 10, 10)
        finally:
            os.remove(bad)


VIDEO = os.environ.get("RP5DECK_TEST_VIDEO")


@unittest.skipUnless(_have("mpv") and VIDEO, "libmpv or RP5DECK_TEST_VIDEO not available")
class TestRealMpv(unittest.TestCase):
    def test_first_frame_and_clean_close(self):
        import time
        before = set(os.listdir("/proc/self/task"))
        p = VideoPlayer(VIDEO, 160, 90, ao="null")
        t0 = time.monotonic()
        got = False
        while time.monotonic() - t0 < 10 and not got:
            p.signal.wait(0.05)
            if p.poll_frame():
                p.render()
                got = True
        p.close()
        time.sleep(0.2)
        self.assertTrue(got, "no frame in 10 s: %s" % list(p.log))
        self.assertEqual(set(os.listdir("/proc/self/task")), before)


if __name__ == "__main__":
    unittest.main()
