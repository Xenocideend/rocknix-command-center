"""gfx - cairo + pango drawing onto an ARGB32 image, through ctypes.

The Canvas is what ui.py widgets draw with. Colours are (r, g, b) or
(r, g, b, a) tuples of floats in 0..1 (ui.rgb() builds them from hex).
Rectangles are (x, y, w, h) tuples in pixels.

The image's bytes are handed to SDL as an ARGB8888 streaming texture (cairo
ARGB32 on little-endian == SDL ARGB8888). Every drawing call honours the
current clip, and Canvas.clip_rect lets widgets skip drawing entirely when
they are outside the damaged area.

Importing this module loads libcairo/libpango, so tests never import it.
"""
import ctypes
from contextlib import contextmanager
from ctypes import POINTER, byref, c_char_p, c_double, c_int, c_void_p

_cairo = ctypes.CDLL("libcairo.so.2")
_pango = ctypes.CDLL("libpango-1.0.so.0")
_pc = ctypes.CDLL("libpangocairo-1.0.so.0")
_gobj = ctypes.CDLL("libgobject-2.0.so.0")


def _c(lib, name, res, *args):
    f = getattr(lib, name)
    f.restype = res
    f.argtypes = list(args)
    return f


_D = c_double
image_surface_create = _c(_cairo, "cairo_image_surface_create", c_void_p, c_int, c_int, c_int)
image_surface_get_data = _c(_cairo, "cairo_image_surface_get_data", c_void_p, c_void_p)
image_surface_get_stride = _c(_cairo, "cairo_image_surface_get_stride", c_int, c_void_p)
surface_flush = _c(_cairo, "cairo_surface_flush", None, c_void_p)
surface_destroy = _c(_cairo, "cairo_surface_destroy", None, c_void_p)
create = _c(_cairo, "cairo_create", c_void_p, c_void_p)
destroy = _c(_cairo, "cairo_destroy", None, c_void_p)
set_source_rgba = _c(_cairo, "cairo_set_source_rgba", None, c_void_p, _D, _D, _D, _D)
rectangle = _c(_cairo, "cairo_rectangle", None, c_void_p, _D, _D, _D, _D)
fill = _c(_cairo, "cairo_fill", None, c_void_p)
stroke = _c(_cairo, "cairo_stroke", None, c_void_p)
move_to = _c(_cairo, "cairo_move_to", None, c_void_p, _D, _D)
line_to = _c(_cairo, "cairo_line_to", None, c_void_p, _D, _D)
close_path = _c(_cairo, "cairo_close_path", None, c_void_p)
new_path = _c(_cairo, "cairo_new_path", None, c_void_p)
new_sub_path = _c(_cairo, "cairo_new_sub_path", None, c_void_p)
arc = _c(_cairo, "cairo_arc", None, c_void_p, _D, _D, _D, _D, _D)
set_line_width = _c(_cairo, "cairo_set_line_width", None, c_void_p, _D)
set_line_cap = _c(_cairo, "cairo_set_line_cap", None, c_void_p, c_int)
save = _c(_cairo, "cairo_save", None, c_void_p)
restore = _c(_cairo, "cairo_restore", None, c_void_p)
clip = _c(_cairo, "cairo_clip", None, c_void_p)
set_operator = _c(_cairo, "cairo_set_operator", None, c_void_p, c_int)

pc_create_layout = _c(_pc, "pango_cairo_create_layout", c_void_p, c_void_p)
pc_show_layout = _c(_pc, "pango_cairo_show_layout", None, c_void_p, c_void_p)
layout_set_text = _c(_pango, "pango_layout_set_text", None, c_void_p, c_char_p, c_int)
layout_set_font = _c(_pango, "pango_layout_set_font_description", None, c_void_p, c_void_p)
layout_get_pixel_size = _c(_pango, "pango_layout_get_pixel_size", None, c_void_p, POINTER(c_int), POINTER(c_int))
layout_set_width = _c(_pango, "pango_layout_set_width", None, c_void_p, c_int)
layout_set_ellipsize = _c(_pango, "pango_layout_set_ellipsize", None, c_void_p, c_int)
layout_set_height = _c(_pango, "pango_layout_set_height", None, c_void_p, c_int)
layout_set_wrap = _c(_pango, "pango_layout_set_wrap", None, c_void_p, c_int)
fd_from_string = _c(_pango, "pango_font_description_from_string", c_void_p, c_char_p)
fd_set_absolute_size = _c(_pango, "pango_font_description_set_absolute_size", None, c_void_p, _D)
fd_free = _c(_pango, "pango_font_description_free", None, c_void_p)
g_object_unref = _c(_gobj, "g_object_unref", None, c_void_p)

FORMAT_ARGB32 = 0
OPERATOR_CLEAR, OPERATOR_OVER = 0, 2
WRAP_WORD = 0
LINE_CAP_ROUND = 1
PANGO_SCALE = 1024
ELLIPSIZE_NONE, ELLIPSIZE_END = 0, 3
FONT_FAMILY = "Sans"          # fontconfig -> DejaVu Sans on ROCKNIX


def _rgba(c):
    return (c[0], c[1], c[2], c[3] if len(c) > 3 else 1.0)


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.surf = image_surface_create(FORMAT_ARGB32, w, h)
        self.cr = create(self.surf)
        self.layout = pc_create_layout(self.cr)
        self._fonts = {}
        self.clip_rect = (0, 0, w, h)

    # -- resources ---------------------------------------------------------
    def _font(self, size, bold):
        key = (int(size), bool(bold))
        fd = self._fonts.get(key)
        if fd is None:
            fd = fd_from_string(("%s%s" % (FONT_FAMILY, " Bold" if bold else "")).encode())
            fd_set_absolute_size(fd, float(size) * PANGO_SCALE)   # size in PIXELS
            self._fonts[key] = fd
        return fd

    def free(self):
        for fd in self._fonts.values():
            fd_free(fd)
        self._fonts.clear()
        g_object_unref(self.layout)
        destroy(self.cr)
        surface_destroy(self.surf)

    def pixels(self):
        surface_flush(self.surf)
        return image_surface_get_data(self.surf), image_surface_get_stride(self.surf)

    # -- clipping ------------------------------------------------------------
    @contextmanager
    def clipped(self, rect):
        x, y, w, h = rect
        save(self.cr)
        rectangle(self.cr, x, y, w, h)
        clip(self.cr)
        old, self.clip_rect = self.clip_rect, rect
        try:
            yield
        finally:
            self.clip_rect = old
            restore(self.cr)

    # -- primitives ----------------------------------------------------------
    def _src(self, color):
        set_source_rgba(self.cr, *_rgba(color))

    def fill_rect(self, rect, color):
        self._src(color)
        rectangle(self.cr, *rect)
        fill(self.cr)

    def _rr_path(self, rect, r):
        x, y, w, h = rect
        r = max(0.0, min(r, w / 2.0, h / 2.0))
        cr = self.cr
        new_sub_path(cr)
        arc(cr, x + w - r, y + r, r, -1.5708, 0)
        arc(cr, x + w - r, y + h - r, r, 0, 1.5708)
        arc(cr, x + r, y + h - r, r, 1.5708, 3.1416)
        arc(cr, x + r, y + r, r, 3.1416, 4.7124)
        close_path(cr)

    def round_rect(self, rect, r, color):
        self._src(color)
        self._rr_path(rect, r)
        fill(self.cr)

    def stroke_round_rect(self, rect, r, color, width=3):
        self._src(color)
        set_line_width(self.cr, width)
        self._rr_path(rect, r)
        stroke(self.cr)

    def circle(self, cx, cy, r, color):
        self._src(color)
        new_sub_path(self.cr)
        arc(self.cr, cx, cy, r, 0, 6.28319)
        fill(self.cr)

    def ring(self, cx, cy, r, color, width=4, a0=0.0, a1=6.28319):
        self._src(color)
        set_line_width(self.cr, width)
        set_line_cap(self.cr, LINE_CAP_ROUND)
        new_sub_path(self.cr)
        arc(self.cr, cx, cy, r, a0, a1)
        stroke(self.cr)

    def line(self, x0, y0, x1, y1, color, width=3):
        self._src(color)
        set_line_width(self.cr, width)
        set_line_cap(self.cr, LINE_CAP_ROUND)
        move_to(self.cr, x0, y0)
        line_to(self.cr, x1, y1)
        stroke(self.cr)

    def image(self, img, x, y, alpha=1.0):
        """Paint a media.Image (premultiplied ARGB32) at (x, y) - I1 art."""
        img.paint(self.cr, x, y, alpha)

    def clear_rect(self, rect):
        """Make rect fully transparent (alpha 0). I1: the companion punches
        this hole where the video texture, drawn under the UI, must show."""
        save(self.cr)
        set_operator(self.cr, OPERATOR_CLEAR)
        rectangle(self.cr, *rect)
        fill(self.cr)
        restore(self.cr)

    def polygon(self, pts, color):
        self._src(color)
        new_path(self.cr)
        move_to(self.cr, *pts[0])
        for p in pts[1:]:
            line_to(self.cr, *p)
        close_path(self.cr)
        fill(self.cr)

    # -- text ----------------------------------------------------------------
    def _prep(self, text, size, bold, width):
        layout_set_font(self.layout, self._font(size, bold))
        layout_set_text(self.layout, text.encode("utf-8"), -1)
        if width:
            layout_set_width(self.layout, int(width * PANGO_SCALE))
            layout_set_ellipsize(self.layout, ELLIPSIZE_END)
        else:
            layout_set_width(self.layout, -1)
            layout_set_ellipsize(self.layout, ELLIPSIZE_NONE)
        tw, th = c_int(), c_int()
        layout_get_pixel_size(self.layout, byref(tw), byref(th))
        return tw.value, th.value

    def text_block(self, text, rect, size, color, bold=False, max_lines=3):
        """Word-wrapped text inside rect, at most max_lines lines, the last
        one ellipsized (I1: the companion's game description)."""
        x, y, w, h = rect
        layout_set_font(self.layout, self._font(size, bold))
        layout_set_text(self.layout, text.encode("utf-8"), -1)
        layout_set_width(self.layout, int(max(1, w) * PANGO_SCALE))
        layout_set_wrap(self.layout, WRAP_WORD)
        layout_set_ellipsize(self.layout, ELLIPSIZE_END)
        layout_set_height(self.layout, -max(1, int(max_lines)))
        self._src(color)
        move_to(self.cr, x, y)
        pc_show_layout(self.cr, self.layout)
        layout_set_height(self.layout, -1)          # back to the one-line default

    def measure(self, text, size, bold=False):
        return self._prep(text, size, bold, None)

    def text(self, text, rect, size, color, bold=False, align="left",
             valign="middle"):
        """Draw one line of text inside rect, ellipsized to its width."""
        x, y, w, h = rect
        tw, th = self._prep(text, size, bold, w if w > 0 else None)
        if align == "center":
            x += (w - tw) / 2.0
        elif align == "right":
            x += w - tw
        if valign == "middle":
            y += (h - th) / 2.0
        elif valign == "bottom":
            y += h - th
        self._src(color)
        move_to(self.cr, x, y)
        pc_show_layout(self.cr, self.layout)
        return tw, th
