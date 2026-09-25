#!/usr/bin/env python3
"""wl_layer.LayerSurface.rebind() (SW1): the request sequence that moves a
layer surface to another output, checked against a recording fake of the
wire (marshal / roundtrip / listener). No compositor involved.

The protocol rules it must follow (wlr-layer-shell + wlroots):
  * no buffer may be attached to the wl_surface when get_layer_surface is
    sent (wlroots: "surface has a buffer attached") -> NULL attach + commit
    first, but only if a frame was ever presented;
  * the old role object is destroyed BEFORE the new one is created (a
    surface may take the same role again only once the old object is gone);
  * the new role object gets the recorded geometry and keyboard
    interactivity NONE (the focus rule), then a commit WITHOUT a buffer.

Needs libwayland-client.so.0 to import wl_layer (Linux / WSL); skipped
elsewhere.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import wl_layer  # noqa: E402
except OSError as e:          # no libwayland-client (Windows)
    wl_layer = None
    WHY = str(e)
else:
    WHY = ""


class Rec:
    def __init__(self):
        self.calls = []
        self.next_id = 1000

    def marshal(self, proxy, opcode, args=(), new_iface=None, version=None, destroy=False):
        ret = None
        if new_iface is not None:
            self.next_id += 1
            ret = self.next_id
        self.calls.append((proxy, opcode, list(args), new_iface is not None, destroy, ret))
        return ret


@unittest.skipIf(wl_layer is None, "libwayland-client not loadable here: %s" % WHY)
class TestRebind(unittest.TestCase):
    SURFACE, SHELL, OLD = 11, 22, 33

    def setUp(self):
        self.rec = Rec()
        self.patches = []
        self._patch(wl_layer, "marshal", self.rec.marshal)
        self._patch(wl_layer, "_Listener", lambda proxy, funcs: ("listener", proxy))
        self.configure_on_roundtrip = True
        test = self

        class FakeWl:
            def wl_display_roundtrip(self, display):
                if test.configure_on_roundtrip and test.ls.proxy:
                    test.ls._on_configure(None, test.ls.proxy, 7, 1920, 1080)
                return 0

            def wl_proxy_get_version(self, p):
                return 4

            def wl_display_flush(self, d):
                return 0

            def wl_display_get_error(self, d):
                return 0
        self._patch(wl_layer, "_wl", FakeWl())
        ls = wl_layer.LayerSurface(display=1, surface=self.SURFACE, output_name="DSI-1",
                                   log=lambda m: None)
        ls.shell, ls.shell_version, ls.proxy = self.SHELL, 4, self.OLD
        o1 = wl_layer._Output(1, 101, 4)
        o1.name = "DSI-1"
        o2 = wl_layer._Output(2, 202, 4)
        o2.name = "DP-1"
        ls.outputs = {1: o1, 2: o2}
        ls.output = o1
        ls.configured = True
        ls.has_buffer = True
        ls.size = (1920, 1080)
        ls.geometry = (wl_layer.ANCHOR_ALL, (0, 0), 0)
        self.ls = ls

    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def ops(self):
        """(target, opcode, destroy) per request, in order."""
        return [(c[0], c[1], c[4]) for c in self.rec.calls]

    def test_sequence_when_a_frame_was_shown(self):
        size = self.ls.rebind("DP-1")
        self.assertEqual(size, (1920, 1080))
        calls = self.rec.calls
        new = calls[3][5]
        self.assertEqual(self.ops(), [
            (self.SURFACE, wl_layer._WL_SURFACE_ATTACH, False),   # NULL buffer ...
            (self.SURFACE, wl_layer._WL_SURFACE_COMMIT, False),   # ... committed: unmapped
            (self.OLD, 7, True),                                  # old role object destroyed
            (self.SHELL, 0, False),                               # get_layer_surface
            (new, 0, False), (new, 1, False), (new, 2, False), (new, 4, False),
            (self.SURFACE, wl_layer._WL_SURFACE_COMMIT, False),   # initial commit, no buffer
            (new, 6, False),                                      # ack_configure (roundtrip)
        ])
        self.assertEqual(calls[0][2][0], ('o', None))              # the attach IS a NULL
        get = calls[3][2]
        self.assertEqual(get[1], ('o', self.SURFACE))
        self.assertEqual(get[2], ('o', 202))                        # DP-1's wl_output
        self.assertEqual(get[3], ('u', wl_layer.LAYER_TOP))
        self.assertEqual(get[4], ('s', "rp5deck"))
        self.assertEqual(calls[7][2], [('u', wl_layer.KEYBOARD_NONE)])
        self.assertEqual(calls[5][2], [('u', wl_layer.ANCHOR_ALL)])
        self.assertEqual(self.ls.output.name, "DP-1")
        self.assertEqual(self.ls.proxy, new)
        self.assertTrue(self.ls.can_present)
        self.assertFalse(self.ls.has_buffer)

    def test_no_buffer_attached_when_get_layer_surface_goes_out(self):
        """Replays the requests against a model of the surface's buffer
        state: attaching NULL + commit clears it; get_layer_surface must
        find it clear. (The rule wlroots enforces with a protocol error.)"""
        self.ls.rebind("DP-1")
        has_buffer = True                  # a frame was presented before
        attached, pending = False, None
        for proxy, opcode, args, _new, _destroy, _ret in self.rec.calls:
            if proxy == self.SURFACE and opcode == wl_layer._WL_SURFACE_ATTACH:
                attached, pending = True, args[0][1]
            elif proxy == self.SURFACE and opcode == wl_layer._WL_SURFACE_COMMIT:
                if attached:               # a commit applies the pending attach
                    has_buffer = pending is not None
                attached, pending = False, None
            elif proxy == self.SHELL and opcode == 0:
                self.assertFalse(has_buffer, "get_layer_surface sent with a buffer attached")
                return
        self.fail("no get_layer_surface sent")

    def test_old_role_destroyed_before_the_new_one_exists(self):
        self.ls.rebind("DP-1")
        order = [(c[0], c[1]) for c in self.rec.calls]
        self.assertLess(order.index((self.OLD, 7)), order.index((self.SHELL, 0)))

    def test_nothing_to_unmap_if_no_frame_was_ever_shown(self):
        self.ls.has_buffer = False
        self.ls.rebind("DP-1")
        self.assertNotIn(wl_layer._WL_SURFACE_ATTACH, [c[1] for c in self.rec.calls
                                                       if c[0] == self.SURFACE])

    def test_hidden_surface_stays_hidden(self):
        self.ls.hidden = True
        self.ls.has_buffer = False
        self.ls.rebind("DP-1")
        self.assertTrue(self.ls.hidden)
        self.assertFalse(self.ls.can_present)       # show() maps it later

    def test_geometry_and_layer_carry_over(self):
        self.ls.geometry = (wl_layer.ANCHOR_TOP | wl_layer.ANCHOR_RIGHT, (260, 72), 0)
        self.ls.layer = wl_layer.LAYER_OVERLAY
        self.ls.rebind("DP-1")
        new = self.rec.calls[3][5]
        by_op = {c[1]: c[2] for c in self.rec.calls if c[0] == new}
        self.assertEqual(by_op[0], [('u', 260), ('u', 72)])
        self.assertEqual(by_op[1], [('u', wl_layer.ANCHOR_TOP | wl_layer.ANCHOR_RIGHT)])
        self.assertEqual(self.rec.calls[3][2][3], ('u', wl_layer.LAYER_OVERLAY))

    def test_a_closed_old_surface_can_be_rebound(self):
        # the compositor closes a layer surface whose output went away
        self.ls.closed = True
        self.ls.rebind("DP-1")
        self.assertFalse(self.ls.closed)

    def test_unknown_output_refused_before_touching_anything(self):
        with self.assertRaises(RuntimeError):
            self.ls.rebind("HDMI-A-1")
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.ls.proxy, self.OLD)

    def test_no_configure_is_an_error(self):
        self.configure_on_roundtrip = False
        with self.assertRaises(RuntimeError):
            self.ls.rebind("DP-1")


if __name__ == "__main__":
    unittest.main()
