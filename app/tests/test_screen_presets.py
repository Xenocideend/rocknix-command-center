"""Batch 2: screen size presets - the Command Center laid out for the panel, then scaled."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import config  # noqa: E402
import screen_presets as sp  # noqa: E402
from test_companion import AppCase  # noqa: E402


class LayoutSize(unittest.TestCase):
    def test_config_enum_matches_the_module(self):
        self.assertEqual(config.UI_RESOLUTION_VALUES, sp.VALUES)

    def test_rp5_is_unchanged(self):
        self.assertEqual(sp.layout_size(1920, 1080), (1920, 1080))
        self.assertEqual(sp.layout_size(1920, 1080, "1920x1080"), (1920, 1080))

    def test_small_panel_gets_the_whole_ui_shrunk(self):
        self.assertEqual(sp.layout_size(640, 480), (1280, 960))
        self.assertEqual(sp.layout_size(1024, 768), (1280, 960))
        self.assertEqual(sp.layout_size(720, 720), (1280, 1280))

    def test_never_smaller_than_the_minimum(self):
        for w, h in ((640, 480), (1280, 720), (1024, 768), (720, 720), (800, 480)):
            lw, lh = sp.layout_size(w, h)
            self.assertGreaterEqual(min(lw, lh), 720, (w, h))
            self.assertAlmostEqual(lw / float(lh), w / float(h), places=2)    # same shape

    def test_preset_sets_the_scale(self):
        # the RP5 told "720p panel": a bigger UI, laid out at 1280x720
        self.assertEqual(sp.layout_size(1920, 1080, "1280x720"), (1280, 720))

    def test_preset_of_another_shape_keeps_the_surface_shape(self):
        # a 4:3 preset on a 16:9 surface sets the scale, never squashes the UI
        self.assertEqual(sp.layout_size(1920, 1080, "1024x768"), (1707, 960))

    def test_preset_follows_a_rotated_panel(self):
        self.assertEqual(sp.layout_size(480, 640, "640x480"), (960, 1280))

    def test_unknown_preset_is_auto(self):
        self.assertEqual(sp.layout_size(640, 480, "nonsense"), sp.layout_size(640, 480))

    def test_a_full_width_strip_is_laid_out_one_to_one(self):
        # the BAR's 140 px row and its 24 px handle used to be stretched to 57600 px wide
        self.assertEqual(sp.layout_size(1920, 140), (1920, 140))
        self.assertEqual(sp.layout_size(1920, 24), (1920, 24))
        self.assertEqual(sp.layout_size(1920, 140, "1024x768"), (1920, 140))

    def test_a_narrow_strip_is_still_shrunk_to_fit(self):
        lw, lh = sp.layout_size(640, 100)
        self.assertEqual(lh, 720)
        self.assertAlmostEqual(lw / float(lh), 6.4, places=1)

    def test_labels(self):
        self.assertEqual(sp.label("auto"), "Auto (this screen)")
        self.assertEqual(sp.label("640x480"), "640x480")


class FakeSdl:
    PIXELFORMAT_ARGB8888 = TEXTUREACCESS_STREAMING = BLENDMODE_BLEND_PREMULTIPLIED = 0

    def __init__(self):
        self.window = None
        self.textures = []

    def SetWindowSize(self, win, w, h):
        self.window = (w, h)

    def CreateTexture(self, ren, fmt, access, w, h):
        self.textures.append((w, h))
        return object()

    def SetTextureBlendMode(self, tex, mode):
        return True

    def DestroyTexture(self, tex):
        pass

    class SDL_FRect:
        def __init__(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h


class FakeCanvas:
    def __init__(self, w, h):
        self.size = (w, h)

    def free(self):
        pass


class FakeGfx:
    Canvas = FakeCanvas


class InTheApp(AppCase):
    def resize(self, app, w, h):
        app.sdl, app.win, app.gfx = FakeSdl(), object(), FakeGfx()
        app.tex = app.canvas = None
        app._resize(w, h)

    def test_small_surface_lays_out_big_and_the_window_stays_real(self):
        app = self.make_app()
        self.resize(app, 640, 480)
        self.assertEqual(app.surface, (640, 480))
        self.assertEqual((app.w, app.h), (1280, 960))
        self.assertEqual(app.sdl.window, (640, 480))
        self.assertEqual(app.sdl.textures[-1], (1280, 960))
        self.assertEqual(app.canvas.size, (1280, 960))

    def test_rp5_surface_is_one_to_one(self):
        app = self.make_app()
        self.resize(app, 1920, 1080)
        self.assertEqual((app.w, app.h), (1920, 1080))
        self.assertEqual(app._to_layout(100, 200), (100, 200))

    def test_pointer_and_video_map_between_surface_and_layout(self):
        app = self.make_app()
        self.resize(app, 640, 480)
        self.assertEqual(app._to_layout(320, 240), (640, 480))
        r = app._to_surface(FakeSdl.SDL_FRect(640, 480, 1280, 960))
        self.assertEqual((r.x, r.y, r.w, r.h), (320, 240, 640, 480))

    def test_changing_the_preset_relays_out(self):
        app = self.make_app()
        self.resize(app, 1920, 1080)
        config.set_value(app.cfg, ("screens", "ui_resolution"), "1280x720")
        app.on_setting(("screens", "ui_resolution"), "1280x720")
        self.assertEqual((app.w, app.h), (1280, 720))
        self.assertEqual(app.sdl.window, (1920, 1080))

    def test_the_setting_is_in_settings(self):
        app = self.make_app()
        app.open_settings()
        found = [w for w in (getattr(app.ui, n, None) for n in dir(app.ui))
                 if isinstance(getattr(w, "controls", None), dict)
                 and ("screens", "ui_resolution") in w.controls]
        self.assertTrue(found, "no Screen size preset row")
        self.assertEqual(found[0].controls[("screens", "ui_resolution")].text, "Auto (this screen)")


if __name__ == "__main__":
    unittest.main()
