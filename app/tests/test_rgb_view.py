"""tests for rgb_view.py (RG: the "Stick lights" Command Center sheet).

RGBSheet is built and laid out without gfx (ui.py widgets only); the PC
render test at the bottom is skipped unless cairo/pango are importable
(WSL / the device), same convention as tests/cc6_patched_cases.py."""
import os
import sys
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import rgb_leds     # noqa: E402
import rgb_view     # noqa: E402
import ui           # noqa: E402

PANEL = (0, 0, 1920, 1080)


class FakeHost:
    def __init__(self):
        self.opened = []
        self.closed = 0
        self.state_dirty = False

        class _UI:
            def __init__(self, outer):
                self.outer = outer

            def open(self, name):
                self.outer.opened.append(name)

        self.ui = _UI(self)

    def close_sheet(self):
        self.closed += 1


def make_controller(save_calls=None, initial=None):
    lights = rgb_leds.Controller(state=initial or rgb_leds.LightState(), run=lambda argv: True)
    host = FakeHost()
    save_calls = save_calls if save_calls is not None else []
    ctrl = rgb_view.RGBController(host, lights, save_fn=lambda changes: save_calls.append(changes))
    return host, ctrl, save_calls


class TestSheetShape(unittest.TestCase):
    def test_widgets_exist(self):
        sheet = rgb_view.RGBSheet(lambda name, payload: None)
        self.assertEqual(set(sheet.mode_buttons), {"rocknix", "off", "colour"})
        self.assertEqual(len(sheet.swatches), len(rgb_leds.PRESETS))
        self.assertEqual(set(sheet.stick_buttons), {"left", "right"})

    def test_big_touch_targets(self):
        """DESIGN: every interactive control >= 120 px both ways on the
        1920x1080 panel (mode buttons, linked toggle, stick selector,
        preset swatches, both sliders). Preview circles are not tappable
        and are exempt."""
        sheet = rgb_view.RGBSheet(lambda name, payload: None)
        sheet.linked.set_state(False)          # show the stick selector too
        sheet.layout(PANEL)
        targets = (list(sheet.mode_buttons.values()) + [sheet.linked]
                  + list(sheet.stick_buttons.values()) + [sw for _n, sw in sheet.swatches]
                  + [sheet.hue, sheet.brightness])
        for w in targets:
            _x, _y, w_, h_ = w.rect
            self.assertGreaterEqual(w_, 120, w.name)
            self.assertGreaterEqual(h_, 120, w.name)

    def test_layout_stays_inside_the_panel(self):
        sheet = rgb_view.RGBSheet(lambda name, payload: None)
        sheet.layout(PANEL)
        for w in (list(sheet.mode_buttons.values()) + [sheet.linked, sheet.hue,
                                                        sheet.brightness]):
            x, y, w_, h_ = w.rect
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w_, PANEL[2])
            self.assertLessEqual(y + h_, PANEL[3])


class TestController(unittest.TestCase):
    def test_mode_change_applies_and_persists(self):
        host, ctrl, saves = make_controller()
        ctrl.action("lights.mode", "off")
        self.assertEqual(ctrl.lights.state.mode, "off")
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[-1][("lights", "mode")], "off")
        self.assertTrue(host.state_dirty)

    def test_linked_toggle_persists(self):
        host, ctrl, saves = make_controller()
        ctrl.action("lights.linked", False)
        self.assertFalse(ctrl.lights.state.linked)
        self.assertEqual(len(saves), 1)

    def test_preset_tap_linked_sets_both_sticks(self):
        host, ctrl, saves = make_controller(initial=rgb_leds.LightState(mode="colour"))
        ctrl.action("lights.preset", "blue")
        want = rgb_leds.PRESET_BY_NAME["blue"]
        self.assertEqual(ctrl.lights.state.left, want)
        self.assertEqual(ctrl.lights.state.right, want)
        self.assertEqual(len(saves), 1)

    def test_preset_tap_unlinked_sets_only_the_active_stick(self):
        st = rgb_leds.LightState(mode="colour", linked=False, left=(1, 1, 1), right=(2, 2, 2))
        host, ctrl, saves = make_controller(initial=st)
        ctrl.action("lights.stick", "right")
        ctrl.action("lights.preset", "green")
        want = rgb_leds.PRESET_BY_NAME["green"]
        self.assertEqual(ctrl.lights.state.right, want)
        self.assertEqual(ctrl.lights.state.left, (1, 1, 1), "the other stick must not move")

    def test_hue_drag_does_not_persist_but_commit_does(self):
        host, ctrl, saves = make_controller(initial=rgb_leds.LightState(mode="colour"))
        ctrl.action("lights.hue", 120)
        self.assertEqual(len(saves), 0, "a mid-drag value must never be written to disk")
        ctrl.action("lights.hue.commit", 120)
        self.assertEqual(len(saves), 1)
        self.assertEqual(ctrl.lights.state.left, rgb_leds.hue_to_rgb(120))

    def test_brightness_drag_does_not_persist_but_commit_does(self):
        host, ctrl, saves = make_controller(initial=rgb_leds.LightState(mode="colour"))
        ctrl.action("lights.brightness", 33)
        self.assertEqual(len(saves), 0)
        ctrl.action("lights.brightness.commit", 33)
        self.assertEqual(len(saves), 1)
        self.assertEqual(ctrl.lights.state.brightness, 33)

    def test_close_calls_host(self):
        host, ctrl, _saves = make_controller()
        ctrl.action("lights.close", None)
        self.assertEqual(host.closed, 1)

    def test_open_refreshes_and_shows_the_sheet(self):
        host, ctrl, _saves = make_controller()
        ctrl.open()
        self.assertEqual(host.opened, ["lights"])

    def test_refresh_updates_mode_lit_state(self):
        host, ctrl, _saves = make_controller(initial=rgb_leds.LightState(mode="off"))
        ctrl.refresh()
        self.assertTrue(ctrl.sheet.mode_buttons["off"].lit)
        self.assertFalse(ctrl.sheet.mode_buttons["colour"].lit)

    def test_live_preview_matches_state(self):
        st = rgb_leds.LightState(mode="colour", linked=False, left=(10, 20, 30),
                                 right=(40, 50, 60))
        host, ctrl, _saves = make_controller(initial=st)
        ctrl.refresh()
        self.assertEqual(ctrl.sheet.preview_left.rgb_val, (10, 20, 30))
        self.assertEqual(ctrl.sheet.preview_right.rgb_val, (40, 50, 60))
        self.assertTrue(ctrl.sheet.preview_left.on)

    def test_live_preview_off_in_off_mode(self):
        st = rgb_leds.LightState(mode="off", left=(10, 20, 30))
        host, ctrl, _saves = make_controller(initial=st)
        ctrl.refresh()
        self.assertFalse(ctrl.sheet.preview_left.on)


# ---------------------------------------------------------------------------
# PC renders (cairo + pango: WSL / the device) - screenshots/rg-pc-render-*.png
# ---------------------------------------------------------------------------
def _gfx():
    try:
        import gfx
        return gfx
    except Exception:            # noqa: BLE001 - no cairo/pango here (the Windows PC)
        return None


@unittest.skipUnless(_gfx(), "needs libcairo + libpango (WSL, the device)")
class TestRealRendering(unittest.TestCase):
    def render(self, root, w, h, name):
        import ctypes
        gfx = _gfx()
        canvas = gfx.Canvas(w, h)
        self.addCleanup(canvas.free)
        with canvas.clipped((0, 0, w, h)):
            root.paint(canvas)
        out = os.environ.get("RP5DECK_RENDER_DIR")
        if out:
            os.makedirs(out, exist_ok=True)
            f = gfx._cairo.cairo_surface_write_to_png
            f.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            canvas.pixels()
            self.assertEqual(f(canvas.surf, os.fsencode(
                os.path.join(out, "rg-pc-render-%s.png" % name))), 0)

    def _rooted(self, ctrl):
        root = ui.Root(1920, 1080, bg=ui.THEME["bg"])
        root.add(ctrl.sheet)
        ctrl.sheet.set_visible(True)
        ctrl.sheet.layout(PANEL)
        return root

    def test_render_colour_mode_linked(self):
        st = rgb_leds.LightState(mode="colour", linked=True, left=(30, 90, 255), brightness=200)
        _host, ctrl, _saves = make_controller(initial=st)
        ctrl.refresh()
        self.render(self._rooted(ctrl), 1920, 1080, "colour-linked")

    def test_render_colour_mode_unlinked(self):
        st = rgb_leds.LightState(mode="colour", linked=False, left=(255, 0, 0),
                                 right=(0, 220, 220), brightness=180)
        _host, ctrl, _saves = make_controller(initial=st)
        ctrl.refresh()
        self.render(self._rooted(ctrl), 1920, 1080, "colour-unlinked")

    def test_render_off_mode(self):
        st = rgb_leds.LightState(mode="off")
        _host, ctrl, _saves = make_controller(initial=st)
        ctrl.refresh()
        self.render(self._rooted(ctrl), 1920, 1080, "off-mode")


if __name__ == "__main__":
    unittest.main()
