"""sdl3: the part of SDL 3.4.10 rp5deck uses, through ctypes.

Struct layouts are from SDL 3.4.10's SDL_events.h, and the offset asserts at the bottom catch a
wrong layout at import instead of as garbage coordinates later.

Importing this loads libSDL3.so.0, so tests never import it.
"""
import ctypes
from ctypes import (POINTER, Structure, c_bool, c_char_p, c_float, c_int,
                    c_int32, c_int64, c_uint8, c_uint32, c_uint64, c_void_p)

lib = ctypes.CDLL("libSDL3.so.0")


def _fn(name, res, *args):
    f = getattr(lib, name)
    f.restype = res
    f.argtypes = list(args)
    return f


class SDL_Rect(Structure):
    _fields_ = [("x", c_int), ("y", c_int), ("w", c_int), ("h", c_int)]


class SDL_FRect(Structure):
    _fields_ = [("x", c_float), ("y", c_float), ("w", c_float), ("h", c_float)]


class SDL_Event(Structure):
    _fields_ = [("raw", c_uint8 * 128)]     # union SDL_Event is 128 bytes


Init = _fn("SDL_Init", c_bool, c_uint32)
Quit = _fn("SDL_Quit", None)
GetError = _fn("SDL_GetError", c_char_p)
SetHint = _fn("SDL_SetHint", c_bool, c_char_p, c_char_p)
GetCurrentVideoDriver = _fn("SDL_GetCurrentVideoDriver", c_char_p)
CreateProperties = _fn("SDL_CreateProperties", c_uint32)
DestroyProperties = _fn("SDL_DestroyProperties", None, c_uint32)
SetBooleanProperty = _fn("SDL_SetBooleanProperty", c_bool, c_uint32, c_char_p, c_bool)
SetNumberProperty = _fn("SDL_SetNumberProperty", c_bool, c_uint32, c_char_p, c_int64)
SetStringProperty = _fn("SDL_SetStringProperty", c_bool, c_uint32, c_char_p, c_char_p)
GetPointerProperty = _fn("SDL_GetPointerProperty", c_void_p, c_uint32, c_char_p, c_void_p)
CreateWindowWithProperties = _fn("SDL_CreateWindowWithProperties", c_void_p, c_uint32)
DestroyWindow = _fn("SDL_DestroyWindow", None, c_void_p)
GetWindowProperties = _fn("SDL_GetWindowProperties", c_uint32, c_void_p)
SetWindowSize = _fn("SDL_SetWindowSize", c_bool, c_void_p, c_int, c_int)
CreateRenderer = _fn("SDL_CreateRenderer", c_void_p, c_void_p, c_char_p)
DestroyRenderer = _fn("SDL_DestroyRenderer", None, c_void_p)
GetRendererName = _fn("SDL_GetRendererName", c_char_p, c_void_p)
SetRenderVSync = _fn("SDL_SetRenderVSync", c_bool, c_void_p, c_int)
CreateTexture = _fn("SDL_CreateTexture", c_void_p, c_void_p, c_uint32, c_int, c_int, c_int)
DestroyTexture = _fn("SDL_DestroyTexture", None, c_void_p)
UpdateTexture = _fn("SDL_UpdateTexture", c_bool, c_void_p, POINTER(SDL_Rect), c_void_p, c_int)
RenderTexture = _fn("SDL_RenderTexture", c_bool, c_void_p, c_void_p, c_void_p, c_void_p)
# the companion video gets its own XRGB8888 texture drawn under the cairo UI texture. The UI
# texture blends with premultiplied alpha (cairo ARGB32 is premultiplied), so a transparent hole
# in the UI shows the video.
SetTextureBlendMode = _fn("SDL_SetTextureBlendMode", c_bool, c_void_p, c_uint32)
SetTextureScaleMode = _fn("SDL_SetTextureScaleMode", c_bool, c_void_p, c_int)
SetRenderDrawColor = _fn("SDL_SetRenderDrawColor", c_bool, c_void_p, c_uint8, c_uint8,
                         c_uint8, c_uint8)
RenderClear = _fn("SDL_RenderClear", c_bool, c_void_p)
RenderPresent = _fn("SDL_RenderPresent", c_bool, c_void_p)
GetTouchDevices = _fn("SDL_GetTouchDevices", POINTER(c_uint64), POINTER(c_int))
GetTouchDeviceName = _fn("SDL_GetTouchDeviceName", c_char_p, c_uint64)
free = _fn("SDL_free", None, c_void_p)
RegisterEvents = _fn("SDL_RegisterEvents", c_uint32, c_int)
PushEvent = _fn("SDL_PushEvent", c_bool, POINTER(SDL_Event))
WaitEventTimeout = _fn("SDL_WaitEventTimeout", c_bool, POINTER(SDL_Event), c_int32)
PollEvent = _fn("SDL_PollEvent", c_bool, POINTER(SDL_Event))

INIT_VIDEO = 0x20
INIT_EVENTS = 0x4000
PIXELFORMAT_ARGB8888 = 0x16362004   # == cairo ARGB32 on little-endian
PIXELFORMAT_XRGB8888 = 0x16161804  # == mpv "bgr0" / cairo RGB24, 4th byte ignored
BLENDMODE_NONE = 0x00000000
BLENDMODE_BLEND_PREMULTIPLIED = 0x00000010
SCALEMODE_LINEAR = 1
TEXTUREACCESS_STREAMING = 1
TOUCH_MOUSEID = 0xFFFFFFFF          # mouse event synthesised from touch

# hint names (SDL_hints.h)
HINT_TOUCH_MOUSE_EVENTS = b"SDL_TOUCH_MOUSE_EVENTS"  # "0", no mouse from touch
HINT_MOUSE_TOUCH_EVENTS = b"SDL_MOUSE_TOUCH_EVENTS"  # "0", no touch from mouse
HINT_NO_SIGNAL_HANDLERS = b"SDL_NO_SIGNAL_HANDLERS"     # Python owns SIGTERM
HINT_VIDEO_ALLOW_SCREENSAVER = b"SDL_VIDEO_ALLOW_SCREENSAVER"  # "1", no idle inhibitor

EV_QUIT = 0x100
EV_WINDOW_FIRST, EV_WINDOW_LAST = 0x202, 0x21A
EV_WINDOW_EXPOSED = 0x204
EV_MOUSE_MOTION, EV_MOUSE_DOWN, EV_MOUSE_UP = 0x400, 0x401, 0x402
EV_FINGER_DOWN, EV_FINGER_UP, EV_FINGER_MOTION, EV_FINGER_CANCELED = 0x700, 0x701, 0x702, 0x703
BUTTON_LEFT = 1
BUTTON_LMASK = 1


class MouseButton(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64),
                ("windowID", c_uint32), ("which", c_uint32), ("button", c_uint8),
                ("down", c_bool), ("clicks", c_uint8), ("padding", c_uint8),
                ("x", c_float), ("y", c_float)]


class MouseMotion(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64),
                ("windowID", c_uint32), ("which", c_uint32), ("state", c_uint32),
                ("x", c_float), ("y", c_float), ("xrel", c_float), ("yrel", c_float)]


class TouchFinger(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64),
                ("touchID", c_uint64), ("fingerID", c_uint64), ("x", c_float),
                ("y", c_float), ("dx", c_float), ("dy", c_float),
                ("pressure", c_float), ("windowID", c_uint32)]


class UserEvent(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64),
                ("windowID", c_uint32), ("code", c_int32), ("data1", c_void_p),
                ("data2", c_void_p)]


assert MouseButton.x.offset == 28 and MouseButton.y.offset == 32
assert MouseMotion.which.offset == 20 and MouseMotion.state.offset == 24 and MouseMotion.x.offset == 28
assert TouchFinger.x.offset == 32 and TouchFinger.windowID.offset == 52
assert UserEvent.code.offset == 20 and UserEvent.data1.offset == 24


def event_type(ev):
    return c_uint32.from_buffer(ev).value


def error():
    e = GetError()
    return e.decode(errors="replace") if e else ""
