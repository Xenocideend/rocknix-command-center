"""media: game art and video drawn inside rp5deck's own surface.

A separate mpv window on DSI-1 takes keyboard focus from the game on the other screen, so
video is decoded by libmpv with its software render API into a buffer we own, and art is
decoded straight into a cairo ready buffer. Both end up as pixels the app paints with cairo
or uploads to its SDL texture. No window, no Wayland surface.

Images
    load_image(path, w, h, mode="fit") -> Image
        PNG  -> cairo's own loader (cairo_image_surface_create_from_png)
        JPEG -> libturbojpeg, decoded as BGRA, with DCT downscaling (n/8) when the box is
                much smaller
        WebP -> libwebp's advanced API in MODE_bgrA (premultiplied BGRA)
        The format comes from the file's magic bytes, not the extension. Scaling to the box
        is a cairo paint with FILTER_GOOD.
    Image.data is cairo FORMAT_ARGB32, premultiplied, native endian, so bytes B,G,R,A on
    little endian == SDL_PIXELFORMAT_ARGB8888.

    Modes (the CSS object-fit names work too):
        fit  (contain) the whole image inside the box with its aspect kept, the Image is
                       the scaled size (<= box), centre it with place()
        fill (stretch) exactly the box size, aspect ignored
        crop (cover)   exactly the box size, aspect kept, the overflow cut evenly

Video
    VideoPlayer(path, w, h, loop=True, mute=True, hwdec="no", ao=None,
                mode="fit", wake=None)
        .poll_frame() -> bool   drain mpv events, True when a new frame is due
        .render()               render into the player's own 64-byte aligned buffer
                                (.address, .stride, format bgr0)
        .render_into(buf, stride)  render into caller memory instead
        .start() .pause() .resume() .stop() .seek(t) .set_loop(b) .set_mute(b)
        .load(path) .resize(w, h) .close()   (also a context manager)
    Pixel format "bgr0" is bytes B,G,R,X and X is garbage (mpv render.h), so treat it as
    cairo FORMAT_RGB24 / SDL_PIXELFORMAT_XRGB8888, never ARGB, or X becomes alpha.

    Every VideoPlayer method has to be called from one thread (the app's main thread).
    `wake` is called from mpv's threads when poll_frame() should run soon, and it may only
    signal (main.Poster.wake, SDL_PushEvent), never call back into the player. ctypes.CDLL
    releases the GIL for every foreign call, so mpv never waits on Python.

Anything needing a native library loads lazily, so importing this on the PC (no cairo, no
libmpv) works and the pure helpers (geometry, sniffing, strides, state machine, options)
get tested there.

CLI (headless, on the device):
    python3 media.py libs
    python3 media.py decode IMG OUT.png W H [fit|fill|crop]
    python3 media.py webp-selftest OUTDIR
    python3 media.py control-test VIDEO
    python3 media.py bench VIDEO --size 960x540|native --hwdec no --seconds 10
                     [--scaler bilinear|fast-bilinear|default]
                     [--frames-at 1,3,6 --frames-dir DIR] [--opt k=v] [--no-render]
"""
import collections
import ctypes
import ctypes.util
import math
import os
import sys
import threading
from ctypes import (POINTER, Structure, Union, byref, c_char_p, c_double, c_int,
                    c_int64, c_size_t, c_ubyte, c_uint32, c_uint64, c_ulong,
                    c_void_p)


class MediaError(Exception):
    """decode or playback failure with a readable reason"""


# ===========================================================================
# Pure logic, no native libraries, tested on the PC
# ===========================================================================

FIT, FILL, CROP = "fit", "fill", "crop"
_MODE_ALIASES = {"fit": FIT, "contain": FIT,
                 "fill": FILL, "stretch": FILL,
                 "crop": CROP, "cover": CROP}


def norm_mode(mode):
    m = _MODE_ALIASES.get(str(mode).lower())
    if m is None:
        raise ValueError("unknown scale mode %r (fit/fill/crop)" % (mode,))
    return m


class Geometry(tuple):
    """(out_w, out_h, sx, sy, ox, oy): the output is out_w x out_h and the source is drawn scaled by
    (sx, sy) with its top left at (ox, oy) in output pixels (negative = cropped).
    """
    __slots__ = ()

    def __new__(cls, out_w, out_h, sx, sy, ox, oy):
        return tuple.__new__(cls, (out_w, out_h, sx, sy, ox, oy))

    out_w = property(lambda s: s[0])
    out_h = property(lambda s: s[1])
    sx = property(lambda s: s[2])
    sy = property(lambda s: s[3])
    ox = property(lambda s: s[4])
    oy = property(lambda s: s[5])


def fit_geometry(src_w, src_h, box_w, box_h, mode=FIT, upscale=True):
    """Where a src_w x src_h image lands in a box_w x box_h box."""
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source size must be positive, got %dx%d" % (src_w, src_h))
    if box_w <= 0 or box_h <= 0:
        raise ValueError("box size must be positive, got %dx%d" % (box_w, box_h))
    mode = norm_mode(mode)
    if mode == FILL:
        return Geometry(box_w, box_h, box_w / src_w, box_h / src_h, 0.0, 0.0)
    if mode == FIT:
        s = min(box_w / src_w, box_h / src_h)
        if not upscale:
            s = min(s, 1.0)
        ow = max(1, min(box_w, int(round(src_w * s))))
        oh = max(1, min(box_h, int(round(src_h * s))))
        # exact per-axis scale so the scaled image covers its output exactly
        return Geometry(ow, oh, ow / src_w, oh / src_h, 0.0, 0.0)
    # CROP (cover)
    s = max(box_w / src_w, box_h / src_h)
    if not upscale:
        s = min(s, 1.0)
    ow = min(box_w, max(1, int(round(src_w * s))))
    oh = min(box_h, max(1, int(round(src_h * s))))
    return Geometry(ow, oh, s, s, (ow - src_w * s) / 2.0, (oh - src_h * s) / 2.0)


def place(img_w, img_h, box_w, box_h, halign="center", valign="center"):
    """Top-left (x, y) that aligns an img_w x img_h image inside a box."""
    def one(i, b, a):
        if a in ("left", "top", "start"):
            return 0
        if a in ("right", "bottom", "end"):
            return b - i
        return (b - i) // 2
    return one(img_w, box_w, halign), one(img_h, box_h, valign)


def needed_source_size(src_w, src_h, geom):
    """Smallest source resolution that still gives full detail at geom's
    scale (never larger than the source)."""
    return (min(src_w, max(1, math.ceil(src_w * geom.sx))),
            min(src_h, max(1, math.ceil(src_h * geom.sy))))


JPEG_FACTORS = tuple((n, 8) for n in range(1, 9))     # 1/8 .. 8/8


def scaled_dim(d, factor):
    """libjpeg-turbo's TJSCALED(): ceil(d * num / denom)."""
    num, den = factor
    return (d * num + den - 1) // den


def pick_jpeg_factor(src_w, src_h, need_w, need_h, factors=JPEG_FACTORS):
    """Smallest DCT scaling factor whose output is still >= the needed size, so the decoder does
    most of a big downscale and cairo only refines. Falls back to the largest factor <= 1.
    """
    best = None
    for f in factors:
        if f[0] > f[1]:
            continue                                     # never upscale here
        w, h = scaled_dim(src_w, f), scaled_dim(src_h, f)
        if w >= need_w and h >= need_h:
            if best is None or f[0] * best[1] < best[0] * f[1]:
                best = f
    if best is None:
        best = max((f for f in factors if f[0] <= f[1]),
                   key=lambda f: f[0] / f[1])
    return best


def stride_for(width, bpp=4, align=4):
    """bytes per row, rounded up to `align` (cairo 4, mpv sw 64)"""
    if width <= 0 or bpp <= 0 or align <= 0:
        raise ValueError("width, bpp and align must be positive")
    return ((width * bpp + align - 1) // align) * align


def sniff(head):
    """Image format from the first bytes of a file, since scrapers sometimes save JPEG data as
    .png.
    """
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


class AlignedBuffer:
    """`size` bytes of ctypes memory whose address is a multiple of align. mpv recommends 64-byte
    aligned SW render targets.
    """

    def __init__(self, size, align=64):
        if size <= 0:
            raise ValueError("buffer size must be positive")
        self._raw = (c_ubyte * (size + align))()
        base = ctypes.addressof(self._raw)
        self.offset = (-base) % align
        self.address = base + self.offset
        self.size = size
        self.align = align

    def view(self):
        return (c_ubyte * self.size).from_buffer(self._raw, self.offset)

    def tobytes(self):
        return ctypes.string_at(self.address, self.size)

    def clear(self, value=0):
        ctypes.memset(self.address, value, self.size)


def buffer_address(buf, need):
    """Address of caller memory, refusing anything too small. Takes AlignedBuffer, a ctypes
    array, a writable bytearray/memoryview, or a raw int address (unchecked, the caller vouches
    for its size).
    """
    if isinstance(buf, int):
        return buf
    if isinstance(buf, AlignedBuffer):
        size, addr = buf.size, buf.address
    elif isinstance(buf, ctypes.Array):
        size, addr = ctypes.sizeof(buf), ctypes.addressof(buf)
    else:
        mv = memoryview(buf)
        if mv.readonly:
            raise TypeError("render target must be writable")
        size = mv.nbytes
        addr = ctypes.addressof((c_ubyte * size).from_buffer(buf))
    if size < need:
        raise ValueError("render target holds %d bytes, needs %d" % (size, need))
    return addr


# -- player state machine --------------------------------------------------
IDLE, LOADING, PLAYING, PAUSED, ENDED, STOPPED, ERROR, CLOSED = (
    "idle", "loading", "playing", "paused", "ended", "stopped", "error", "closed")

END_EOF, END_STOP, END_QUIT, END_ERROR, END_REDIRECT = 0, 2, 3, 4, 5


def next_state(state, event, paused=False, end_reason=None):
    """Player state after an mpv event (pure). Events: "start-file", "file-loaded", "end-file",
    "shutdown", "pause", "resume".
    """
    if state == CLOSED:
        return CLOSED
    if event == "start-file":
        return LOADING
    if event == "file-loaded":
        return PAUSED if paused else PLAYING
    if event == "end-file":
        if end_reason == END_EOF:
            return ENDED
        if end_reason == END_ERROR:
            return ERROR
        if state == LOADING and end_reason == END_REDIRECT:
            return LOADING
        return STOPPED
    if event == "shutdown":
        return CLOSED
    if event == "pause" and state == PLAYING:
        return PAUSED
    if event == "resume" and state == PAUSED:
        return PLAYING
    return state


# mpv's built-in Lua scripts (stats, console, select, ...) still start with load-scripts=no,
# 6 lua threads per player when measured. Each has its own switch, and an unknown name on a
# future mpv shouldnt break playback, so these are applied soft (a failure is logged).
SOFT_OPTIONS = (
    ("load-stats-overlay", "no"),
    ("load-console", "no"),
    ("load-commands", "no"),
    ("load-select", "no"),
    ("load-positioning", "no"),
    ("load-context-menu", "no"),
    ("load-auto-profiles", "no"),
)

# Measured on the RP5 in one run (MEDIA-NOTES.md), 640x480 source to 1920x1080: mpv's default
# bicubic swscale 21.9 ms/frame (45.5 fps of 59.94, 143 drops), bilinear 12.2 ms (60.2 fps,
# 0 drops), fast-bilinear 12.3 ms. Bilinear costs the same and looks better, so it's the
# default.
DEFAULT_SCALER = "bilinear"


def build_options(hwdec="no", loop=True, mute=True, ao=None, mode=FIT,
                  extra=None, scaler=DEFAULT_SCALER):
    """mpv options in order for a headless in-surface player (the required ones, SOFT_OPTIONS are
    applied separately and can fail).
    """
    mode = norm_mode(mode)
    opts = [
        ("config", "no"),                  # ignore any mpv.conf on the device
        ("load-scripts", "no"),
        ("ytdl", "no"),
        ("terminal", "no"),
        ("input-default-bindings", "no"),
        ("input-vo-keyboard", "no"),
        ("osc", "no"),
        ("sub-auto", "no"),
        ("audio-display", "no"),           # never show cover art as video
        ("idle", "yes"),                   # keep the core alive between files
        ("keep-open", "no"),  # EOF -> end-file, our buffer keeps the last frame
        ("vo", "libmpv"),  # render API only, never a window
        ("hwdec", str(hwdec)),
        ("loop-file", "inf" if loop else "no"),
        ("mute", "yes" if mute else "no"),
        ("keepaspect", "no" if mode == FILL else "yes"),
        ("panscan", "1.0" if mode == CROP else "0.0"),
    ]
    if scaler:
        opts.append(("sws-scaler", str(scaler)))
    if ao is not None:
        opts.append(("ao", str(ao)))
    for k, v in (extra or {}).items():
        opts.append((str(k), str(v)))
    return opts


class FrameSignal:
    """Thread-safe "something new" flag that folds bursts. set() can be called from any thread
    (mpv's), take() on the owner thread returns True once per burst.
    """

    def __init__(self, wake=None):
        self._ev = threading.Event()
        self._wake = wake
        self.count = 0              # how many times set() fired (diagnostics)

    def set(self):
        self.count += 1
        self._ev.set()
        w = self._wake
        if w is not None:
            try:
                w()
            except Exception:       # noqa: BLE001 - never raise into mpv
                pass

    def take(self):
        if self._ev.is_set():
            self._ev.clear()
            return True
        return False

    def wait(self, timeout=None):
        return self._ev.wait(timeout)


# ===========================================================================
# Native libraries (lazy)
# ===========================================================================

def _cdll(*names):
    last = None
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError as e:
            last = e
        found = ctypes.util.find_library(n.split(".so")[0].replace("lib", "", 1))
        if found:
            try:
                return ctypes.CDLL(found)
            except OSError as e:
                last = e
    raise OSError("none of %s could be loaded: %s" % (", ".join(names), last))


def _fn(lib, name, res, *args):
    f = getattr(lib, name)
    f.restype = res
    f.argtypes = list(args)
    return f


CAIRO_FORMAT_ARGB32, CAIRO_FORMAT_RGB24 = 0, 1
CAIRO_FILTER_GOOD = 1
CAIRO_EXTEND_PAD = 3


class _Cairo:
    def __init__(self):
        L = _cdll("libcairo.so.2")
        V, I, D = c_void_p, c_int, c_double
        self.version = _fn(L, "cairo_version_string", c_char_p)().decode()
        self.create_from_png = _fn(L, "cairo_image_surface_create_from_png", V, c_char_p)
        self.surface_create = _fn(L, "cairo_image_surface_create", V, I, I, I)
        self.create_for_data = _fn(L, "cairo_image_surface_create_for_data", V, V, I, I, I, I)
        self.stride_for_width = _fn(L, "cairo_format_stride_for_width", I, I, I)
        self.get_data = _fn(L, "cairo_image_surface_get_data", V, V)
        self.get_width = _fn(L, "cairo_image_surface_get_width", I, V)
        self.get_height = _fn(L, "cairo_image_surface_get_height", I, V)
        self.get_stride = _fn(L, "cairo_image_surface_get_stride", I, V)
        self.get_format = _fn(L, "cairo_image_surface_get_format", I, V)
        self.surface_status = _fn(L, "cairo_surface_status", I, V)
        self.status_to_string = _fn(L, "cairo_status_to_string", c_char_p, I)
        self.surface_flush = _fn(L, "cairo_surface_flush", None, V)
        self.surface_mark_dirty = _fn(L, "cairo_surface_mark_dirty", None, V)
        self.surface_destroy = _fn(L, "cairo_surface_destroy", None, V)
        self.write_to_png = _fn(L, "cairo_surface_write_to_png", I, V, c_char_p)
        self.create = _fn(L, "cairo_create", V, V)
        self.destroy = _fn(L, "cairo_destroy", None, V)
        self.status = _fn(L, "cairo_status", I, V)
        self.translate = _fn(L, "cairo_translate", None, V, D, D)
        self.scale = _fn(L, "cairo_scale", None, V, D, D)
        self.set_source_surface = _fn(L, "cairo_set_source_surface", None, V, V, D, D)
        self.get_source = _fn(L, "cairo_get_source", V, V)
        self.pattern_set_filter = _fn(L, "cairo_pattern_set_filter", None, V, I)
        self.pattern_set_extend = _fn(L, "cairo_pattern_set_extend", None, V, I)
        self.paint = _fn(L, "cairo_paint", None, V)
        self.paint_with_alpha = _fn(L, "cairo_paint_with_alpha", None, V, D)
        self.save = _fn(L, "cairo_save", None, V)
        self.restore = _fn(L, "cairo_restore", None, V)
        self.rectangle = _fn(L, "cairo_rectangle", None, V, D, D, D, D)
        self.clip = _fn(L, "cairo_clip", None, V)

    def check_surface(self, s, what):
        st = self.surface_status(s)
        if st != 0:
            msg = self.status_to_string(st).decode()
            self.surface_destroy(s)
            raise MediaError("%s: cairo: %s" % (what, msg))


TJPF_BGRA = 8          # alpha/X filled with 0xFF on decompression
TJERR_WARNING = 0


class _TurboJpeg:
    def __init__(self):
        L = _cdll("libturbojpeg.so.0")
        V, I, P = c_void_p, c_int, POINTER(c_int)
        self.init = _fn(L, "tjInitDecompress", V)
        self.header = _fn(L, "tjDecompressHeader3", I, V, c_char_p, c_ulong, P, P, P, P)
        self.decompress = _fn(L, "tjDecompress2", I, V, c_char_p, c_ulong, V, I, I, I, I, I)
        self.errstr = _fn(L, "tjGetErrorStr2", c_char_p, V)
        self.errcode = _fn(L, "tjGetErrorCode", I, V)
        self.destroy = _fn(L, "tjDestroy", I, V)


# libwebp decode.h (ABI 0x0209, unchanged through libwebp 1.x)
WEBP_DECODER_ABI_VERSION = 0x0209
MODE_bgrA = 8          # premultiplied B,G,R,A == cairo ARGB32 on little-endian


class _WebPFeatures(Structure):
    _fields_ = [("width", c_int), ("height", c_int), ("has_alpha", c_int),
                ("has_animation", c_int), ("format", c_int), ("pad", c_uint32 * 5)]


class _WebPRGBA(Structure):
    _fields_ = [("rgba", c_void_p), ("stride", c_int), ("size", c_size_t)]


class _WebPYUVA(Structure):
    _fields_ = [("y", c_void_p), ("u", c_void_p), ("v", c_void_p), ("a", c_void_p),
                ("y_stride", c_int), ("u_stride", c_int), ("v_stride", c_int),
                ("a_stride", c_int), ("y_size", c_size_t), ("u_size", c_size_t),
                ("v_size", c_size_t), ("a_size", c_size_t)]


class _WebPU(Union):
    _fields_ = [("RGBA", _WebPRGBA), ("YUVA", _WebPYUVA)]


class _WebPDecBuffer(Structure):
    _fields_ = [("colorspace", c_int), ("width", c_int), ("height", c_int),
                ("is_external_memory", c_int), ("u", _WebPU),
                ("pad", c_uint32 * 4), ("private_memory", c_void_p)]


class _WebPOptions(Structure):
    _fields_ = [(n, c_int) for n in (
        "bypass_filtering", "no_fancy_upsampling", "use_cropping", "crop_left",
        "crop_top", "crop_width", "crop_height", "use_scaling", "scaled_width",
        "scaled_height", "use_threads", "dithering_strength", "flip",
        "alpha_dithering_strength")] + [("pad", c_uint32 * 5)]


class _WebPConfig(Structure):
    _fields_ = [("input", _WebPFeatures), ("output", _WebPDecBuffer),
                ("options", _WebPOptions)]


class _WebPConfigGuarded(Structure):
    # trailing guard, if a future libwebp's struct were bigger its memset in
    # WebPInitDecoderConfig lands here instead of on the Python heap
    _fields_ = [("cfg", _WebPConfig), ("guard", c_ubyte * 256)]


class _WebP:
    def __init__(self):
        L = _cdll("libwebp.so.7")
        V, I = c_void_p, c_int
        self.version = _fn(L, "WebPGetDecoderVersion", I)()
        self.init_config = _fn(L, "WebPInitDecoderConfigInternal", I, V, I)
        self.get_features = _fn(L, "WebPGetFeaturesInternal", I, c_char_p, c_size_t, V, I)
        self.decode = _fn(L, "WebPDecode", I, c_char_p, c_size_t, V)
        self.free_dec_buffer = _fn(L, "WebPFreeDecBuffer", None, V)
        # encoder, only used by the self-test to make a WebP with alpha
        self.encode_lossless_bgra = _fn(L, "WebPEncodeLosslessBGRA", c_size_t,
                                        V, I, I, I, POINTER(c_void_p))
        self.free = _fn(L, "WebPFree", None, V)


_LIBS = {}
_LIBS_LOCK = threading.Lock()


def _lib(name):
    """Loads a native binding once, raises MediaError if it isnt there."""
    with _LIBS_LOCK:
        if name not in _LIBS:
            cls = {"cairo": _Cairo, "turbojpeg": _TurboJpeg, "webp": _WebP,
                   "mpv": _MpvLib}[name]
            try:
                _LIBS[name] = cls()
            except (OSError, AttributeError) as e:
                _LIBS[name] = MediaError("%s unavailable: %s" % (name, e))
        v = _LIBS[name]
    if isinstance(v, MediaError):
        raise v
    return v


def available():
    """{name: True | "reason"} for every native library media.py can use."""
    out = {}
    for n in ("cairo", "turbojpeg", "webp", "mpv"):
        try:
            _lib(n)
            out[n] = True
        except MediaError as e:
            out[n] = str(e)
    return out


# ===========================================================================
# Images
# ===========================================================================

class Image:
    """Decoded, scaled art: w x h, cairo FORMAT_ARGB32 premultiplied (B,G,R,A bytes on little
    endian), rows `stride` bytes apart.
    """

    FORMAT = "argb32"

    def __init__(self, w, h, stride, buf):
        self.w, self.h, self.stride, self.buf = w, h, stride, buf

    @property
    def address(self):
        return self.buf.address

    @property
    def data(self):
        return self.buf.view()

    def tobytes(self):
        return self.buf.tobytes()

    def pixel(self, x, y):
        """(b, g, r, a) bytes at (x, y)."""
        if not (0 <= x < self.w and 0 <= y < self.h):
            raise IndexError((x, y))
        return tuple(ctypes.string_at(self.address + y * self.stride + x * 4, 4))

    def cairo_surface(self):
        """A cairo surface viewing this image's memory. The caller destroys it and the Image has to
        outlive it.
        """
        c = _lib("cairo")
        s = c.create_for_data(self.address, CAIRO_FORMAT_ARGB32, self.w, self.h, self.stride)
        c.check_surface(s, "wrap image")
        return s

    def paint(self, cr, x, y, alpha=1.0):
        """Draws at (x, y) on a cairo context (like gfx.Canvas.cr)."""
        c = _lib("cairo")
        s = self.cairo_surface()
        try:
            c.save(cr)
            c.set_source_surface(cr, s, float(x), float(y))
            if alpha >= 1.0:
                c.paint(cr)
            else:
                c.paint_with_alpha(cr, float(alpha))
            c.restore(cr)
        finally:
            c.surface_destroy(s)

    def to_png(self, path):
        c = _lib("cairo")
        s = self.cairo_surface()
        try:
            st = c.write_to_png(s, os.fsencode(path))
        finally:
            c.surface_destroy(s)
        if st != 0:
            raise MediaError("write %s: %s" % (path, c.status_to_string(st).decode()))


def image_size(path):
    """(format, width, height) without a full decode where cheap."""
    fmt, w, h, _ = _probe(path)
    return fmt, w, h


def _read_head(path, n=32):
    with open(path, "rb") as f:
        return f.read(n)


def _probe(path):
    head = _read_head(path, 64)
    fmt = sniff(head)
    if fmt == "png":
        import struct
        w, h = struct.unpack(">II", head[16:24])
        return fmt, w, h, None
    if fmt == "jpeg":
        data = open(path, "rb").read()
        tj = _lib("turbojpeg")
        hnd = tj.init()
        if not hnd:
            raise MediaError("tjInitDecompress failed")
        try:
            w, h, ss, cs = c_int(), c_int(), c_int(), c_int()
            if tj.header(hnd, data, len(data), byref(w), byref(h), byref(ss), byref(cs)) != 0:
                raise MediaError("%s: jpeg header: %s" % (path, tj.errstr(hnd).decode()))
            return fmt, w.value, h.value, data
        finally:
            tj.destroy(hnd)
    if fmt == "webp":
        data = open(path, "rb").read()
        wp = _lib("webp")
        g = _WebPConfigGuarded()
        if not wp.init_config(byref(g), WEBP_DECODER_ABI_VERSION):
            raise MediaError("WebPInitDecoderConfig: ABI mismatch")
        st = wp.get_features(data, len(data), byref(g.cfg.input), WEBP_DECODER_ABI_VERSION)
        if st != 0:
            raise MediaError("%s: webp features: status %d" % (path, st))
        return fmt, g.cfg.input.width, g.cfg.input.height, data
    if fmt is None:
        raise MediaError("%s: not a PNG, JPEG or WebP file" % path)
    raise MediaError("%s: %s is not supported" % (path, fmt.upper()))


def _decode_png(path):
    c = _lib("cairo")
    s = c.create_from_png(os.fsencode(path))
    c.check_surface(s, path)
    return s, None                                 # surface owns its pixels


def _decode_jpeg(path, data, need_w, need_h):
    c = _lib("cairo")
    tj = _lib("turbojpeg")
    hnd = tj.init()
    if not hnd:
        raise MediaError("tjInitDecompress failed")
    try:
        w, h, ss, cs = c_int(), c_int(), c_int(), c_int()
        if tj.header(hnd, data, len(data), byref(w), byref(h), byref(ss), byref(cs)) != 0:
            raise MediaError("%s: jpeg header: %s" % (path, tj.errstr(hnd).decode()))
        f = pick_jpeg_factor(w.value, h.value, need_w, need_h)
        dw, dh = scaled_dim(w.value, f), scaled_dim(h.value, f)
        stride = c.stride_for_width(CAIRO_FORMAT_RGB24, dw)
        buf = AlignedBuffer(stride * dh, 16)
        rc = tj.decompress(hnd, data, len(data), buf.address, dw, stride, dh, TJPF_BGRA, 0)
        if rc != 0 and tj.errcode(hnd) != TJERR_WARNING:
            raise MediaError("%s: jpeg decode: %s" % (path, tj.errstr(hnd).decode()))
    finally:
        tj.destroy(hnd)
    s = c.create_for_data(buf.address, CAIRO_FORMAT_RGB24, dw, dh, stride)
    c.check_surface(s, path)
    return s, buf                                  # buf must outlive s


def _decode_webp(path, data):
    c = _lib("cairo")
    wp = _lib("webp")
    g = _WebPConfigGuarded()
    cfg = g.cfg
    if not wp.init_config(byref(g), WEBP_DECODER_ABI_VERSION):
        raise MediaError("WebPInitDecoderConfig: ABI mismatch")
    st = wp.get_features(data, len(data), byref(cfg.input), WEBP_DECODER_ABI_VERSION)
    if st != 0:
        raise MediaError("%s: webp features: status %d" % (path, st))
    if cfg.input.has_animation:
        raise MediaError("%s: animated WebP is not supported" % path)
    dw, dh = cfg.input.width, cfg.input.height
    stride = c.stride_for_width(CAIRO_FORMAT_ARGB32, dw)
    buf = AlignedBuffer(stride * dh, 16)
    cfg.output.colorspace = MODE_bgrA
    cfg.output.is_external_memory = 1
    cfg.output.u.RGBA.rgba = buf.address
    cfg.output.u.RGBA.stride = stride
    cfg.output.u.RGBA.size = buf.size
    st = wp.decode(data, len(data), byref(g))
    wp.free_dec_buffer(byref(cfg.output))
    if st != 0:
        raise MediaError("%s: webp decode: status %d" % (path, st))
    s = c.create_for_data(buf.address, CAIRO_FORMAT_ARGB32, dw, dh, stride)
    c.check_surface(s, path)
    return s, buf


def _scale_surface(src, src_w, src_h, geom, orig_w, orig_h):
    """Paints src (a decode of an orig_w x orig_h image, maybe pre-shrunk to src_w x src_h) into a
    new Image per geom.
    """
    c = _lib("cairo")
    ow, oh = geom.out_w, geom.out_h
    stride = c.stride_for_width(CAIRO_FORMAT_ARGB32, ow)
    buf = AlignedBuffer(stride * oh, 16)
    buf.clear()
    dst = c.create_for_data(buf.address, CAIRO_FORMAT_ARGB32, ow, oh, stride)
    c.check_surface(dst, "scale target")
    cr = c.create(dst)
    try:
        c.translate(cr, geom.ox, geom.oy)
        c.scale(cr, geom.sx * orig_w / src_w, geom.sy * orig_h / src_h)
        c.set_source_surface(cr, src, 0.0, 0.0)
        pat = c.get_source(cr)
        c.pattern_set_filter(pat, CAIRO_FILTER_GOOD)
        c.pattern_set_extend(pat, CAIRO_EXTEND_PAD)    # no faded edges
        c.paint(cr)
        st = c.status(cr)
        if st != 0:
            raise MediaError("scale: cairo: %s" % c.status_to_string(st).decode())
    finally:
        c.destroy(cr)
        c.surface_flush(dst)
        c.surface_destroy(dst)
    return Image(ow, oh, stride, buf)


def load_image(path, w, h, mode=FIT, upscale=True):
    """Decodes PNG/JPEG/WebP art and scales it into a w x h box (modes in the module docstring).
    Raises MediaError on any failure.
    """
    c = _lib("cairo")
    fmt, sw, sh, data = _probe(path)
    geom = fit_geometry(sw, sh, w, h, mode, upscale)
    need_w, need_h = needed_source_size(sw, sh, geom)
    keep = None
    if fmt == "png":
        src, keep = _decode_png(path)
    elif fmt == "jpeg":
        src, keep = _decode_jpeg(path, data, need_w, need_h)
    else:
        src, keep = _decode_webp(path, data)
    try:
        dw, dh = c.get_width(src), c.get_height(src)
        return _scale_surface(src, dw, dh, geom, sw, sh)
    finally:
        c.surface_destroy(src)
        del keep


def save_frame_png(address, w, h, stride, path):
    """Writes a bgr0 / RGB24 frame (like VideoPlayer's) to a PNG."""
    c = _lib("cairo")
    s = c.create_for_data(address, CAIRO_FORMAT_RGB24, w, h, stride)
    c.check_surface(s, "wrap frame")
    try:
        st = c.write_to_png(s, os.fsencode(path))
    finally:
        c.surface_destroy(s)
    if st != 0:
        raise MediaError("write %s: %s" % (path, c.status_to_string(st).decode()))


# ===========================================================================
# Video: libmpv software render API
# ===========================================================================

# render.h
MPV_RENDER_PARAM_INVALID = 0
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME = 12
MPV_RENDER_PARAM_SW_SIZE = 17
MPV_RENDER_PARAM_SW_FORMAT = 18
MPV_RENDER_PARAM_SW_STRIDE = 19
MPV_RENDER_PARAM_SW_POINTER = 20
MPV_RENDER_UPDATE_FRAME = 1

# client.h event ids
MPV_EVENT_NONE = 0
MPV_EVENT_SHUTDOWN = 1
MPV_EVENT_LOG_MESSAGE = 2
MPV_EVENT_START_FILE = 6
MPV_EVENT_END_FILE = 7
MPV_EVENT_FILE_LOADED = 8
_EVENT_NAMES = {MPV_EVENT_SHUTDOWN: "shutdown", MPV_EVENT_START_FILE: "start-file",
                MPV_EVENT_END_FILE: "end-file", MPV_EVENT_FILE_LOADED: "file-loaded"}


class _RenderParam(Structure):
    _fields_ = [("type", c_int), ("data", c_void_p)]


class _Event(Structure):
    _fields_ = [("event_id", c_int), ("error", c_int),
                ("reply_userdata", c_uint64), ("data", c_void_p)]


class _EventEndFile(Structure):
    _fields_ = [("reason", c_int), ("error", c_int), ("playlist_entry_id", c_int64),
                ("playlist_insert_id", c_int64), ("playlist_insert_num_entries", c_int)]


class _EventLog(Structure):
    _fields_ = [("prefix", c_char_p), ("level", c_char_p), ("text", c_char_p),
                ("log_level", c_int)]


_CB = ctypes.CFUNCTYPE(None, c_void_p)


class _MpvLib:
    """Thin wrapper over libmpv. VideoPlayer only talks to this, so tests can swap in a fake."""

    def __init__(self):
        L = _cdll("libmpv.so.2")
        V, I, S = c_void_p, c_int, c_char_p
        self.api_version = _fn(L, "mpv_client_api_version", c_ulong)()
        self._create = _fn(L, "mpv_create", V)
        self._initialize = _fn(L, "mpv_initialize", I, V)
        self._terminate_destroy = _fn(L, "mpv_terminate_destroy", None, V)
        self._set_option_string = _fn(L, "mpv_set_option_string", I, V, S, S)
        self._set_property_string = _fn(L, "mpv_set_property_string", I, V, S, S)
        self._get_property_string = _fn(L, "mpv_get_property_string", V, V, S)
        self._free = _fn(L, "mpv_free", None, V)
        self._command = _fn(L, "mpv_command", I, V, POINTER(c_char_p))
        self._wait_event = _fn(L, "mpv_wait_event", POINTER(_Event), V, c_double)
        self._error_string = _fn(L, "mpv_error_string", S, I)
        self._request_log = _fn(L, "mpv_request_log_messages", I, V, S)
        self._set_wakeup = _fn(L, "mpv_set_wakeup_callback", None, V, _CB, V)
        self._rc_create = _fn(L, "mpv_render_context_create", I, POINTER(V), V,
                              POINTER(_RenderParam))
        self._rc_set_update = _fn(L, "mpv_render_context_set_update_callback",
                                  None, V, _CB, V)
        self._rc_update = _fn(L, "mpv_render_context_update", c_uint64, V)
        self._rc_render = _fn(L, "mpv_render_context_render", I, V, POINTER(_RenderParam))
        self._rc_free = _fn(L, "mpv_render_context_free", None, V)
        self._cbs = {}          # keep CFUNCTYPE objects alive while registered

    def _check(self, rc, what):
        if rc < 0:
            raise MediaError("%s: %s" % (what, self._error_string(rc).decode()))
        return rc

    def create(self):
        h = self._create()
        if not h:
            raise MediaError("mpv_create failed (LC_NUMERIC must be \"C\")")
        return h

    def set_option(self, h, name, value):
        self._check(self._set_option_string(h, name.encode(), str(value).encode()),
                    "option %s=%s" % (name, value))

    def initialize(self, h):
        self._check(self._initialize(h), "mpv_initialize")

    def request_log(self, h, level):
        self._check(self._request_log(h, level.encode()), "log level")

    def set_property(self, h, name, value):
        self._check(self._set_property_string(h, name.encode(), str(value).encode()),
                    "set %s=%s" % (name, value))

    def get_property(self, h, name):
        p = self._get_property_string(h, name.encode())
        if not p:
            return None
        try:
            return ctypes.string_at(p).decode("utf-8", "replace")
        finally:
            self._free(p)

    def command(self, h, args):
        arr = (c_char_p * (len(args) + 1))(*[os.fsencode(a) if isinstance(a, str) else a
                                            for a in args], None)
        self._check(self._command(h, arr), "command %s" % args[0])

    def wait_event(self, h, timeout=0.0):
        """None when the queue is empty, else (name_or_id, error, info)."""
        ev = self._wait_event(h, timeout).contents
        eid = ev.event_id
        if eid == MPV_EVENT_NONE:
            return None
        info = None
        if eid == MPV_EVENT_END_FILE and ev.data:
            ef = ctypes.cast(ev.data, POINTER(_EventEndFile)).contents
            err = self._error_string(ef.error).decode() if ef.error < 0 else ""
            info = (ef.reason, err)
        elif eid == MPV_EVENT_LOG_MESSAGE and ev.data:
            lm = ctypes.cast(ev.data, POINTER(_EventLog)).contents
            info = ((lm.level or b"").decode(), (lm.prefix or b"").decode(),
                    (lm.text or b"").decode("utf-8", "replace").rstrip("\n"))
            return ("log", ev.error, info)
        return (_EVENT_NAMES.get(eid, eid), ev.error, info)

    def set_wakeup(self, h, fn):
        key = ("wakeup", h)
        if fn is None:
            self._set_wakeup(h, _CB(), None)            # NULL function pointer
            self._cbs.pop(key, None)
        else:
            cb = _CB(lambda _d: fn())
            self._cbs[key] = cb
            self._set_wakeup(h, cb, None)

    def render_create(self, h):
        ctx = c_void_p()
        api = c_char_p(b"sw")
        params = (_RenderParam * 2)(
            _RenderParam(MPV_RENDER_PARAM_API_TYPE, ctypes.cast(api, c_void_p)),
            _RenderParam(MPV_RENDER_PARAM_INVALID, None))
        self._check(self._rc_create(byref(ctx), h, params), "render context (sw)")
        return ctx.value

    def set_update_callback(self, ctx, fn):
        key = ("update", ctx)
        if fn is None:
            self._rc_set_update(ctx, _CB(), None)
            self._cbs.pop(key, None)
        else:
            cb = _CB(lambda _d: fn())
            self._cbs[key] = cb
            self._rc_set_update(ctx, cb, None)

    def update(self, ctx):
        return self._rc_update(ctx)

    def render_sw(self, ctx, w, h, fmt, stride, address):
        size = (c_int * 2)(w, h)
        fmt_p = c_char_p(fmt.encode())
        stride_v = c_size_t(stride)
        block = c_int(0)                # never block the app's main loop
        params = (_RenderParam * 6)(
            _RenderParam(MPV_RENDER_PARAM_SW_SIZE, ctypes.cast(size, c_void_p)),
            _RenderParam(MPV_RENDER_PARAM_SW_FORMAT, ctypes.cast(fmt_p, c_void_p)),
            _RenderParam(MPV_RENDER_PARAM_SW_STRIDE, ctypes.cast(byref(stride_v), c_void_p)),
            _RenderParam(MPV_RENDER_PARAM_SW_POINTER, c_void_p(address)),
            _RenderParam(MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME,
                         ctypes.cast(byref(block), c_void_p)),
            _RenderParam(MPV_RENDER_PARAM_INVALID, None))
        self._check(self._rc_render(ctx, params), "render")

    def render_free(self, ctx):
        self._rc_free(ctx)

    def terminate_destroy(self, h):
        self._terminate_destroy(h)
        self._cbs = {k: v for k, v in self._cbs.items() if k[1] != h}


SW_FORMAT = "bgr0"      # == cairo RGB24 / SDL XRGB8888 on little-endian
SW_ALIGN = 64


class VideoPlayer:
    """Plays one video at a time into a w x h bgr0 buffer (see module doc)."""

    def __init__(self, path, w, h, loop=True, mute=True, hwdec="no", ao=None,
                 mode=FIT, wake=None, options=None, log_level=None,
                 scaler=DEFAULT_SCALER, _lib_obj=None):
        self._mpv = _lib_obj if _lib_obj is not None else _lib("mpv")
        self.loop, self.mute, self.hwdec, self.mode = bool(loop), bool(mute), hwdec, mode
        self.path = None
        self.state = IDLE
        self.error = None
        self.frames_rendered = 0
        self.log = collections.deque(maxlen=64)
        self._paused = False
        self._handle = None
        self._ctx = None
        self.signal = FrameSignal(wake)
        self.w = self.h = 0
        self.buffer = None
        self.stride = 0
        self._set_size(w, h)
        if _lib_obj is None:
            import locale
            locale.setlocale(locale.LC_NUMERIC, "C")    # libmpv refuses anything else
        m = self._mpv
        self._handle = m.create()
        try:
            for k, v in build_options(hwdec, loop, mute, ao, mode, options, scaler):
                m.set_option(self._handle, k, v)
            self.soft_failures = []
            for k, v in SOFT_OPTIONS:
                try:
                    m.set_option(self._handle, k, v)
                except MediaError as e:
                    self.soft_failures.append(k)
                    self.log.append(("warn", "media", str(e)))
            m.initialize(self._handle)
            if log_level:
                m.request_log(self._handle, log_level)
            self._ctx = m.render_create(self._handle)
            m.set_update_callback(self._ctx, self.signal.set)
            m.set_wakeup(self._handle, self.signal.set)
        except Exception:
            self.close()
            raise
        if path:
            self.load(path)

    # -- geometry ----------------------------------------------------------
    def _set_size(self, w, h):
        if w <= 0 or h <= 0:
            raise ValueError("video size must be positive, got %dx%d" % (w, h))
        self.w, self.h = int(w), int(h)
        self.stride = stride_for(self.w, 4, SW_ALIGN)
        self.buffer = AlignedBuffer(self.stride * self.h, SW_ALIGN)
        self.buffer.clear()

    def resize(self, w, h):
        """New render size, the next render() fills the new buffer."""
        self._alive()
        self._set_size(w, h)
        self.signal.set()           # make sure the next poll renders

    @property
    def address(self):
        return self.buffer.address

    # -- control -------------------------------------------------------------
    def _alive(self):
        if self._handle is None:
            raise MediaError("player is closed")

    def load(self, path, start=True):
        self._alive()
        self.path = path
        self.error = None
        self._paused = not start
        self._mpv.set_property(self._handle, "pause", "no" if start else "yes")
        self._mpv.command(self._handle, ["loadfile", path, "replace"])
        self.state = LOADING

    def start(self):
        """Play: resume if paused, restart the file if it ended or was stopped."""
        self._alive()
        if self.state in (ENDED, STOPPED, ERROR, IDLE):
            if not self.path:
                raise MediaError("nothing loaded")
            self.load(self.path, start=True)
        else:
            self.resume()

    def pause(self):
        self._alive()
        self._mpv.set_property(self._handle, "pause", "yes")
        self._paused = True
        self.state = next_state(self.state, "pause")

    def resume(self):
        self._alive()
        self._mpv.set_property(self._handle, "pause", "no")
        self._paused = False
        self.state = next_state(self.state, "resume")

    def stop(self):
        """Stops playback, the player stays usable (start() or load() again). The buffer keeps the
        last frame. close() tears down.
        """
        self._alive()
        self._mpv.command(self._handle, ["stop"])
        self.state = STOPPED

    def seek(self, seconds, absolute=True):
        self._alive()
        self._mpv.command(self._handle, ["seek", "%.3f" % seconds,
                                         "absolute" if absolute else "relative"])

    def set_loop(self, on):
        self._alive()
        self.loop = bool(on)
        self._mpv.set_property(self._handle, "loop-file", "inf" if on else "no")

    def set_mute(self, on):
        self._alive()
        self.mute = bool(on)
        self._mpv.set_property(self._handle, "mute", "yes" if on else "no")

    def get(self, prop):
        """Any mpv property as a string (None if unavailable)."""
        self._alive()
        return self._mpv.get_property(self._handle, prop)

    def position(self):
        v = self.get("time-pos")
        return float(v) if v not in (None, "") else None

    def duration(self):
        v = self.get("duration")
        return float(v) if v not in (None, "") else None

    def native_size(self):
        """(w, h) of the video after aspect correction ("dwidth"/"dheight"), or None before the file
        loads. Rendering at this size and letting the GPU scale (SDL_RenderTexture dst rect) is
        cheapest.
        """
        try:
            w, h = self.get("dwidth"), self.get("dheight")
            return (int(w), int(h)) if w and h else None
        except ValueError:
            return None

    # -- frames --------------------------------------------------------------
    def _drain_events(self):
        m, h = self._mpv, self._handle
        while True:
            ev = m.wait_event(h, 0.0)
            if ev is None:
                return
            name, err, info = ev
            if name == "log":
                self.log.append(info)
                continue
            if name == "end-file":
                reason, emsg = info if info else (None, "")
                self.state = next_state(self.state, name, self._paused, reason)
                if reason == END_ERROR:
                    self.error = emsg or "playback error"
            elif name in ("start-file", "file-loaded", "shutdown"):
                self.state = next_state(self.state, name, self._paused)

    def poll_frame(self):
        """Owner thread. Drains mpv events, then returns True if mpv has a new frame to render. Cheap
        when nothing happened.
        """
        if self._handle is None:
            return False
        if not self.signal.take():
            return False
        self._drain_events()
        if self._ctx is None:
            return False
        return bool(self._mpv.update(self._ctx) & MPV_RENDER_UPDATE_FRAME)

    def render_into(self, buf, stride, w=None, h=None):
        """Renders the current frame into caller memory (bgr0 rows of `stride` bytes). w/h default to
        the player's size.
        """
        self._alive()
        w = self.w if w is None else int(w)
        h = self.h if h is None else int(h)
        if stride < w * 4 or stride % 4:
            raise ValueError("stride %d too small / unaligned for width %d" % (stride, w))
        addr = buffer_address(buf, stride * h)
        self._mpv.render_sw(self._ctx, w, h, SW_FORMAT, stride, addr)
        self.frames_rendered += 1

    def render(self):
        """Renders into the player's own buffer, returns (address, stride)."""
        self.render_into(self.buffer, self.stride)
        return self.buffer.address, self.stride

    def paint(self, cr, x, y):
        """Draws the last rendered frame on a cairo context."""
        c = _lib("cairo")
        s = c.create_for_data(self.buffer.address, CAIRO_FORMAT_RGB24, self.w, self.h,
                              self.stride)
        c.check_surface(s, "wrap frame")
        try:
            c.surface_mark_dirty(s)
            c.set_source_surface(cr, s, float(x), float(y))
            c.paint(cr)
        finally:
            c.surface_destroy(s)

    # -- teardown ------------------------------------------------------------
    def close(self):
        """Safe to call twice. Order matters: no callback can run into a freed context, and the render
        context has to be freed before the core is destroyed (mpv_terminate_destroy joins every mpv
        thread).
        """
        m = self._mpv
        if self._ctx is not None:
            try:
                m.set_update_callback(self._ctx, None)
            finally:
                m.render_free(self._ctx)
                self._ctx = None
        if self._handle is not None:
            h, self._handle = self._handle, None
            try:
                m.set_wakeup(h, None)
            finally:
                m.terminate_destroy(h)
        self.state = CLOSED

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            if self._handle is not None or self._ctx is not None:
                self.close()
        except Exception:           # noqa: BLE001
            pass


# ===========================================================================
# Headless CLI for on-device verification
# ===========================================================================

def _threads():
    """[(tid, comm)] of this process (Linux)."""
    out = []
    for t in sorted(os.listdir("/proc/self/task"), key=int):
        try:
            with open("/proc/self/task/%s/comm" % t) as f:
                out.append((int(t), f.read().strip()))
        except OSError:
            pass
    return out


def _cpu_ticks():
    with open("/proc/self/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])      # utime + stime (fields 14, 15)


def _mean_abs_diff(a, b, step=97):
    n = min(len(a), len(b))
    idx = range(0, n, step)
    return sum(abs(a[i] - b[i]) for i in idx) / max(1, len(idx))


def _cmd_bench(argv):
    import argparse
    import hashlib
    import time
    ap = argparse.ArgumentParser(prog="media.py bench")
    ap.add_argument("video")
    ap.add_argument("--size", default="960x540")
    ap.add_argument("--hwdec", default="no")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--mode", default=FIT)
    ap.add_argument("--frames-at", default="")
    ap.add_argument("--frames-dir", default="")
    ap.add_argument("--opt", action="append", default=[], help="extra mpv option k=v")
    ap.add_argument("--no-render", action="store_true",
                    help="poll but never render (measures decode-only cost)")
    ap.add_argument("--scaler", default=DEFAULT_SCALER,
                    help="sws-scaler; 'default' = mpv's own (bicubic)")
    a = ap.parse_args(argv)
    native = a.size == "native"
    w, h = (640, 480) if native else (int(x) for x in a.size.lower().split("x"))
    extra = dict(o.split("=", 1) for o in a.opt)
    grab = sorted(float(x) for x in a.frames_at.split(",") if x)
    clk = os.sysconf("SC_CLK_TCK")
    scaler = None if a.scaler == "default" else a.scaler

    t_before = _threads()
    wake_count = [0]
    p = VideoPlayer(a.video, w, h, loop=True, mute=True, hwdec=a.hwdec, ao="null",
                    mode=a.mode, options=extra, log_level="warn", scaler=scaler,
                    wake=lambda: wake_count.__setitem__(0, wake_count[0] + 1))
    t_open = _threads()
    # warm up, wait for the first frame so startup cost isnt in the numbers
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        p.signal.wait(0.05)
        if p.poll_frame():
            p.render()
            break
    if p.state == ERROR or p.frames_rendered == 0:
        print("FAIL no first frame; state=%s error=%s log=%s" % (p.state, p.error, list(p.log)))
        p.close()
        return 1
    if native:
        ns = p.native_size()
        if ns and ns != (w, h):
            w, h = ns
            p.resize(w, h)
        a.size = "native-%dx%d" % (w, h)
    hwcur = p.get("hwdec-current")
    fps_nominal = p.get("container-fps")
    vw, vh = p.get("width"), p.get("height")
    drops0 = int(p.get("frame-drop-count") or 0)
    ddrops0 = int(p.get("decoder-frame-drop-count") or 0)
    p.seek(0)
    frames = 0
    render_s = 0.0
    shots = []
    c0, w0 = _cpu_ticks(), time.monotonic()
    while True:
        now = time.monotonic()
        if now - w0 >= a.seconds:
            break
        p.signal.wait(0.02)
        if p.poll_frame():
            if not a.no_render:
                r0 = time.perf_counter()
                p.render()
                render_s += time.perf_counter() - r0
            frames += 1
            if grab and not a.no_render:
                pos = p.position() or 0.0
                if pos >= grab[0]:
                    grab.pop(0)
                    data = p.buffer.tobytes()
                    shots.append((pos, data))
                    if a.frames_dir:
                        fn = os.path.join(a.frames_dir, "frame-%s-%.1fs.png" % (a.size, pos))
                        save_frame_png(p.address, w, h, p.stride, fn)
    elapsed = time.monotonic() - w0
    cpu = (_cpu_ticks() - c0) / clk / elapsed * 100.0
    drops = int(p.get("frame-drop-count") or 0) - drops0
    ddrops = int(p.get("decoder-frame-drop-count") or 0) - ddrops0
    t_play = _threads()
    p.close()
    time.sleep(0.2)
    t_after = _threads()
    print("RESULT size=%s hwdec=%s hwdec-current=%s video=%sx%s nominal_fps=%s scaler=%s "
          "opts=%s no_render=%s soft_failures=%s" % (
              a.size, a.hwdec, hwcur, vw, vh, fps_nominal, scaler or "mpv-default",
              extra, a.no_render, p.soft_failures))
    print("RESULT frames=%d elapsed=%.2fs fps=%.2f cpu=%.1f%% (100%%=1 core) "
          "render_ms_avg=%.2f vo_drops=%d dec_drops=%d wakeups=%d"
          % (frames, elapsed, frames / elapsed, cpu,
             (render_s / frames * 1000) if frames and not a.no_render else 0.0,
             drops, ddrops, wake_count[0]))
    for i, (pos, data) in enumerate(shots):
        print("SHOT t=%.2fs md5=%s" % (pos, hashlib.md5(data).hexdigest()))
        if i:
            print("SHOT diff(prev)=%.2f" % _mean_abs_diff(shots[i - 1][1], data))
    print("THREADS before=%d open=%d playing=%d after_close=%d" %
          (len(t_before), len(t_open), len(t_play), len(t_after)))
    print("THREADS playing names: %s" % sorted({n for _, n in t_play} - {n for _, n in t_before}))
    leaked = [t for t in t_after if t not in t_before]
    print("THREADS leaked after close: %s" % (leaked or "none"))
    if p.log:
        print("LOG %s" % list(p.log)[-8:])
    return 0 if not leaked else 2


def _cmd_control_test(argv):
    """Runs every control on a real video, headless with ao=null. Each check prints PASS/FAIL with
    what it saw.
    """
    import time
    video = argv[0]
    results = []
    t_before = _threads()
    wakes = [0]
    p = VideoPlayer(video, 320, 180, loop=False, mute=True, ao="null", log_level="warn",
                    wake=lambda: wakes.__setitem__(0, wakes[0] + 1))

    def pump(sec):
        n, end = 0, time.monotonic() + sec
        while time.monotonic() < end:
            p.signal.wait(0.02)
            if p.poll_frame():
                p.render()
                n += 1
        return n

    def check(name, ok, detail):
        results.append(ok)
        print("%s %-28s %s" % ("PASS" if ok else "FAIL", name, detail))

    n = pump(1.5)
    check("plays", n > 30 and p.state == PLAYING, "frames=%d state=%s wakes=%d" % (n, p.state, wakes[0]))
    m0 = p.get("mute")
    p.set_mute(False)
    m1 = p.get("mute")
    p.set_mute(True)
    m2 = p.get("mute")
    check("mute default on + toggles", (m0, m1, m2) == ("yes", "no", "yes"), "%s -> %s -> %s" % (m0, m1, m2))
    p.pause()
    pump(0.3)
    a0, n = p.position(), pump(1.0)
    a1 = p.position()
    check("pause holds position", p.state == PAUSED and abs(a1 - a0) < 0.05,
          "state=%s pos %.3f -> %.3f frames=%d" % (p.state, a0, a1, n))
    p.resume()
    n = pump(1.0)
    check("resume", p.state == PLAYING and n > 30, "state=%s frames=%d" % (p.state, n))
    p.seek(10)
    pump(0.4)
    pos = p.position()
    check("seek absolute 10s", 10.0 <= pos < 11.0, "pos=%.2f" % pos)
    dur = p.duration()
    p.seek(dur - 1.0)
    pump(2.5)
    check("loop off -> ended at EOF", p.state == ENDED, "state=%s dur=%.2f" % (p.state, dur))
    p.start()
    pump(1.0)
    pos = p.position()
    check("start() after end replays", p.state == PLAYING and pos is not None and pos < 2.0,
          "state=%s pos=%s" % (p.state, pos))
    p.set_loop(True)
    p.seek(dur - 0.5)
    pump(2.0)
    pos = p.position()
    check("loop on wraps to start", p.state == PLAYING and pos is not None and pos < 2.0,
          "state=%s pos=%s" % (p.state, pos))
    p.stop()
    pump(0.5)
    check("stop", p.state == STOPPED, "state=%s" % p.state)
    p.load("/storage/rp5deck-b10b/does-not-exist.mp4")
    pump(1.5)
    check("missing file -> error", p.state == ERROR and bool(p.error), "state=%s error=%r" % (p.state, p.error))
    p.load(video)
    n = pump(1.0)
    check("recovers with load()", p.state == PLAYING and n > 30, "state=%s frames=%d" % (p.state, n))
    t_play = _threads()
    p.close()
    p.close()  # safe twice
    time.sleep(0.2)
    t_after = _threads()
    leaked = [t for t in t_after if t not in t_before]
    check("close joins all threads", not leaked,
          "threads before=%d playing=%d after=%d leaked=%s" % (
              len(t_before), len(t_play), len(t_after), leaked or "none"))
    try:
        p.pause()
        check("closed player refuses calls", False, "no error")
    except MediaError as e:
        check("closed player refuses calls", True, str(e))
    print("%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


def _cmd_webp_selftest(argv):
    """Encodes a made-up BGRA image with alpha as lossless WebP, decodes it through load_image and
    checks premultiplication.
    """
    outdir = argv[0]
    wp = _lib("webp")
    W, H = 64, 32
    src = bytearray(W * H * 4)
    for y in range(H):
        for x in range(W):
            i = (y * W + x) * 4
            a = 255 if x < W // 2 else 128              # right half 50% alpha
            src[i:i + 4] = bytes((20, 100, 200, a))     # straight B,G,R,A
    arr = (c_ubyte * len(src)).from_buffer(src)
    out = c_void_p()
    n = wp.encode_lossless_bgra(ctypes.addressof(arr), W, H, W * 4, byref(out))
    if not n:
        print("FAIL encode")
        return 1
    path = os.path.join(outdir, "selftest.webp")
    with open(path, "wb") as f:
        f.write(ctypes.string_at(out, n))
    wp.free(out)
    img = load_image(path, W, H, FILL)
    opaque, half = img.pixel(5, 5), img.pixel(W - 5, 5)
    os.remove(path)
    print("webp decoder version 0x%06x" % wp.version)
    print("opaque pixel (b,g,r,a) =", opaque, "expected (20,100,200,255)")
    print("alpha-128 pixel        =", half, "expected premultiplied ~(10,50,100,128)")
    ok = (opaque == (20, 100, 200, 255) and half[3] == 128 and
          abs(half[2] - 100) <= 1 and abs(half[1] - 50) <= 1 and abs(half[0] - 10) <= 1)
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "libs":
        for k, v in available().items():
            print("%-10s %s" % (k, "ok" if v is True else v))
        if available().get("mpv") is True:
            v = _lib("mpv").api_version
            print("mpv client api %d.%d" % (v >> 16, v & 0xFFFF))
        if available().get("cairo") is True:
            print("cairo", _lib("cairo").version)
        return 0
    if cmd == "decode":
        import time
        src, out, w, h = rest[0], rest[1], int(rest[2]), int(rest[3])
        mode = rest[4] if len(rest) > 4 else FIT
        fmt, sw, sh = image_size(src)
        t0 = time.perf_counter()
        img = load_image(src, w, h, mode)
        dt = time.perf_counter() - t0
        img.to_png(out)
        print("decoded %s %dx%d -> %s %dx%d (%s) in %.1f ms; centre px %s" % (
            fmt, sw, sh, mode, img.w, img.h, out, dt * 1000,
            img.pixel(img.w // 2, img.h // 2)))
        return 0
    if cmd == "webp-selftest":
        return _cmd_webp_selftest(rest)
    if cmd == "bench":
        return _cmd_bench(rest)
    if cmd == "control-test":
        return _cmd_control_test(rest)
    print("unknown command %r" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
