"""wl_layer - give an existing wl_surface the zwlr_layer_surface_v1 role, from Python.

Pure ctypes over libwayland-client.so.0; nothing compiled. Written for rp5deck on
ROCKNIX (sway 1.11, libwayland-client 0.26.0), where the surface comes from SDL3
(SDL_PROP_WINDOW_CREATE_WAYLAND_SURFACE_ROLE_CUSTOM_BOOLEAN) but nothing here is
SDL-specific: any wl_display* + roleless wl_surface* will do.

Why a layer surface: on this compositor a tap on an ordinary window, or on bare
output area, moves keyboard focus off the game on the other screen. A tap on a
layer surface with keyboard_interactivity NONE does not (sway's node_at_coords
returns NULL for layer surfaces, so seat_set_focus is never reached).

Usage:
    ls = LayerSurface(display_ptr, surface_ptr, output_name="DSI-1",
                      namespace="rp5deck")
    ls.create()            # roundtrips; returns after the first configure
    w, h = ls.size
    ...
    ls.pending_size()      # (w, h) if a later configure arrived, else None
    ls.closed              # True once the compositor sent `closed`
    ls.set_geometry(anchor, (w, h), exclusive_zone)   # runtime reconfigure
    ls.hide()              # unmap: NULL buffer + commit (nothing drawn, no input)
    ls.show(anchor, (w, h), exclusive_zone)           # remap after hide()
    ls.can_present         # True only when a buffer may legally be attached
    ls.rebind("DP-1")      # SW1: recreate the role on another output (screen swap)
    ls.set_layer(LAYER_OVERLAY)   # CC5: next commit moves it above fullscreen windows
    ls.destroy()

Promoted from proto/wl_layer.py (kept there untouched as the reference). The
additions are the runtime reconfiguration methods above; the ABI tables and
the create/destroy path are unchanged.

Unmap/remap follows the protocol text: unmapping returns the layer surface to
the state it had right after get_layer_surface, and the client re-maps it by
committing without a buffer, waiting for a configure and handling it as usual.
hide() attaches NULL behind SDL's back; that is safe for EGL (the compositor
simply releases the old buffer). What is NOT safe is presenting while unmapped
and unconfigured - that attaches a buffer to an unconfigured layer surface, a
fatal protocol error - so the caller must check `can_present` before every
SDL_RenderPresent.

Threading/dispatch: every proxy made here lives on the display's DEFAULT queue,
the one SDL3 dispatches inside SDL_PollEvent/SDL_PumpEvents (same as SDL's own
tests/testwaylandcustom.c). So our callbacks run on the main thread, from inside
SDL's event pump. They therefore only record state and ack; they never call back
into SDL (SDL_SetWindowSize can roundtrip, and re-entering dispatch from inside a
dispatch is asking for trouble). The caller applies pending_size() after polling.
"""
import ctypes
from ctypes import (CFUNCTYPE, POINTER, Structure, Union, byref, c_char_p,
                    c_int, c_int32, c_uint32, c_void_p, cast, pointer)

# ---------------------------------------------------------------------------
# libwayland-client ABI
# ---------------------------------------------------------------------------
_wl = ctypes.CDLL("libwayland-client.so.0")


class wl_interface(Structure):
    pass


class wl_message(Structure):
    # struct wl_message { const char *name; const char *signature;
    #                     const struct wl_interface **types; };
    _fields_ = [("name", c_char_p),
                ("signature", c_char_p),
                ("types", POINTER(POINTER(wl_interface)))]


# struct wl_interface { const char *name; int version;
#                       int method_count; const struct wl_message *methods;
#                       int event_count;  const struct wl_message *events; };
wl_interface._fields_ = [("name", c_char_p),
                         ("version", c_int),
                         ("method_count", c_int),
                         ("methods", POINTER(wl_message)),
                         ("event_count", c_int),
                         ("events", POINTER(wl_message))]


class wl_argument(Union):
    # union wl_argument: 8 bytes on aarch64. We only need these members.
    _fields_ = [("i", c_int32), ("u", c_uint32), ("s", c_char_p),
                ("o", c_void_p)]


# Core interfaces exported by libwayland-client itself (data symbols).
wl_registry_interface = wl_interface.in_dll(_wl, "wl_registry_interface")
wl_surface_interface = wl_interface.in_dll(_wl, "wl_surface_interface")
wl_output_interface = wl_interface.in_dll(_wl, "wl_output_interface")

# The NON-variadic marshal entry point (libwayland >= 1.20). ctypes has no way
# to mark which arguments of wl_proxy_marshal_flags(...) are variadic, and
# whether that happens to work depends on the platform ABI; the array form
# takes a plain union array and removes the question.
#   struct wl_proxy *wl_proxy_marshal_array_flags(struct wl_proxy *proxy,
#       uint32_t opcode, const struct wl_interface *interface, uint32_t version,
#       uint32_t flags, union wl_argument *args);
_marshal = _wl.wl_proxy_marshal_array_flags
_marshal.restype = c_void_p
_marshal.argtypes = [c_void_p, c_uint32, POINTER(wl_interface), c_uint32,
                     c_uint32, POINTER(wl_argument)]
WL_MARSHAL_FLAG_DESTROY = 1

_wl.wl_proxy_add_listener.restype = c_int
_wl.wl_proxy_add_listener.argtypes = [c_void_p, c_void_p, c_void_p]
_wl.wl_proxy_get_version.restype = c_uint32
_wl.wl_proxy_get_version.argtypes = [c_void_p]
_wl.wl_proxy_destroy.restype = None
_wl.wl_proxy_destroy.argtypes = [c_void_p]
_wl.wl_display_roundtrip.restype = c_int
_wl.wl_display_roundtrip.argtypes = [c_void_p]
_wl.wl_display_flush.restype = c_int
_wl.wl_display_flush.argtypes = [c_void_p]
_wl.wl_display_get_error.restype = c_int
_wl.wl_display_get_error.argtypes = [c_void_p]


def marshal(proxy, opcode, args=(), new_iface=None, version=None, destroy=False):
    """Send request `opcode` on `proxy`. `args` are (kind, value) with kind in
    'i','u','s','o','n'. For a new_id request pass new_iface; the returned value
    is the new proxy pointer (the 'n' slot is filled in by libwayland)."""
    arr = (wl_argument * max(1, len(args)))()
    keep = []                       # bytes objects must outlive the call
    for k, (kind, val) in enumerate(args):
        if kind == 'i':
            arr[k].i = val
        elif kind == 'u':
            arr[k].u = val
        elif kind == 's':
            b = val if isinstance(val, bytes) else val.encode()
            keep.append(b)
            arr[k].s = b
        elif kind in ('o', 'n'):
            arr[k].o = val
        else:
            raise ValueError(kind)
    if version is None:
        version = _wl.wl_proxy_get_version(proxy)
    iface = pointer(new_iface) if new_iface is not None else None
    ret = _marshal(proxy, opcode, iface, version,
                   WL_MARSHAL_FLAG_DESTROY if destroy else 0, arr)
    if new_iface is not None and not ret:
        raise RuntimeError("wl_proxy_marshal_array_flags returned NULL "
                           "(opcode %d)" % opcode)
    return ret


# ---------------------------------------------------------------------------
# Building interface tables at runtime
# ---------------------------------------------------------------------------
_KEEPALIVE = []   # interface tables are referenced by libwayland forever


def _messages(specs):
    """specs: [(name, signature, [wl_interface or None per arg]), ...]"""
    arr = (wl_message * max(1, len(specs)))()
    for k, (name, sig, types) in enumerate(specs):
        targ = (POINTER(wl_interface) * max(1, len(types)))()
        for j, t in enumerate(types):
            targ[j] = pointer(t) if t is not None else None
        _KEEPALIVE.append(targ)
        arr[k].name = name.encode()
        arr[k].signature = sig.encode()
        arr[k].types = cast(targ, POINTER(POINTER(wl_interface)))
    _KEEPALIVE.append(arr)
    return arr


def _fill(iface, name, version, methods, events):
    iface.name = name.encode()
    iface.version = version
    iface.method_count = len(methods)
    iface.methods = cast(_messages(methods), POINTER(wl_message))
    iface.event_count = len(events)
    iface.events = cast(_messages(events), POINTER(wl_message))
    _KEEPALIVE.append(iface)


# wlr-layer-shell-unstable-v1, version 5 (wlr-protocols master). Signatures
# follow wayland-scanner: a leading digit is the `since` version, '?' marks a
# nullable object/string. Opcodes are the order of <request>/<event> in the XML.
zwlr_layer_shell_v1_interface = wl_interface()
zwlr_layer_surface_v1_interface = wl_interface()

_fill(zwlr_layer_surface_v1_interface, "zwlr_layer_surface_v1", 5,
      methods=[
          ("set_size", "uu", [None, None]),                          # 0
          ("set_anchor", "u", [None]),                               # 1
          ("set_exclusive_zone", "i", [None]),                       # 2
          ("set_margin", "iiii", [None] * 4),                        # 3
          ("set_keyboard_interactivity", "u", [None]),               # 4
          # xdg_popup's interface lives in xdg-shell, not libwayland; the
          # type slot is only consulted when demarshalling, so NULL is safe.
          ("get_popup", "o", [None]),                                # 5
          ("ack_configure", "u", [None]),                            # 6
          ("destroy", "", []),                                       # 7
          ("set_layer", "2u", [None]),                               # 8
          ("set_exclusive_edge", "5u", [None]),                      # 9
      ],
      events=[
          ("configure", "uuu", [None, None, None]),                  # 0
          ("closed", "", []),                                        # 1
      ])
_fill(zwlr_layer_shell_v1_interface, "zwlr_layer_shell_v1", 5,
      methods=[
          ("get_layer_surface", "no?ous",
           [zwlr_layer_surface_v1_interface, wl_surface_interface,
            wl_output_interface, None, None]),                       # 0
          ("destroy", "3", []),                                      # 1
      ],
      events=[])

LAYER_BACKGROUND, LAYER_BOTTOM, LAYER_TOP, LAYER_OVERLAY = 0, 1, 2, 3
ANCHOR_TOP, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT = 1, 2, 4, 8
ANCHOR_ALL = 15
KEYBOARD_NONE, KEYBOARD_EXCLUSIVE, KEYBOARD_ON_DEMAND = 0, 1, 2

# Opcodes of core requests we send.
_WL_DISPLAY_GET_REGISTRY = 1
_WL_REGISTRY_BIND = 0
_WL_SURFACE_ATTACH = 1          # attach(?o buffer, i x, i y)
_WL_SURFACE_COMMIT = 6
_WL_OUTPUT_RELEASE = 0          # since v3

# Listener prototypes. First two args are always (void *data, proxy *self).
_REG_GLOBAL = CFUNCTYPE(None, c_void_p, c_void_p, c_uint32, c_char_p, c_uint32)
_REG_REMOVE = CFUNCTYPE(None, c_void_p, c_void_p, c_uint32)
_OUT_GEOMETRY = CFUNCTYPE(None, c_void_p, c_void_p, c_int32, c_int32, c_int32,
                          c_int32, c_int32, c_char_p, c_char_p, c_int32)
_OUT_MODE = CFUNCTYPE(None, c_void_p, c_void_p, c_uint32, c_int32, c_int32,
                      c_int32)
_OUT_DONE = CFUNCTYPE(None, c_void_p, c_void_p)
_OUT_SCALE = CFUNCTYPE(None, c_void_p, c_void_p, c_int32)
_OUT_STR = CFUNCTYPE(None, c_void_p, c_void_p, c_char_p)
_LS_CONFIGURE = CFUNCTYPE(None, c_void_p, c_void_p, c_uint32, c_uint32,
                          c_uint32)
_LS_CLOSED = CFUNCTYPE(None, c_void_p, c_void_p)


class _Listener:
    """A C array of function pointers plus the Python callables behind it.

    libwayland stores only the raw array address. If the CFUNCTYPE objects or
    the array are garbage-collected, the next event jumps into freed memory
    (a segfault, typically long after the cause). Holding this object on the
    owner keeps both alive for exactly as long as the proxy."""

    def __init__(self, proxy, funcs):
        self.funcs = funcs
        self.table = (c_void_p * len(funcs))(
            *[cast(f, c_void_p).value for f in funcs])
        if _wl.wl_proxy_add_listener(proxy, self.table, None) != 0:
            raise RuntimeError("wl_proxy_add_listener failed (listener already set?)")


class _Output:
    def __init__(self, global_name, proxy, version):
        self.global_name = global_name
        self.proxy = proxy
        self.version = version
        self.name = None
        self.description = None
        self.mode = None
        self.scale = 1
        self.done = False
        self.listener = None


class LayerSurface:
    def __init__(self, display, surface, output_name="DSI-1", layer=LAYER_TOP,
                 namespace="rp5deck", anchor=ANCHOR_ALL, size=(0, 0),
                 exclusive_zone=0, keyboard=KEYBOARD_NONE, log=print):
        self.display = display
        self.surface = surface
        self.output_name = output_name
        self.layer = layer
        self.namespace = namespace
        self.anchor = anchor
        self.req_size = size
        self.exclusive_zone = exclusive_zone
        self.keyboard = keyboard
        self.log = log

        self.registry = None
        self.shell = None
        self.shell_version = 0
        self.shell_advertised = 0
        self.outputs = {}           # global name -> _Output
        self.output = None          # the _Output we bound to
        self.proxy = None           # zwlr_layer_surface_v1
        self.size = None            # (w, h) from the last configure
        self._pending = None
        self.configure_count = 0
        self.closed = False
        self._listeners = []        # see _Listener: must outlive the proxies
        self._gone_outputs = []
        # Runtime state for set_geometry/hide/show.
        self.configured = False     # a configure was acked since the last (re)map request
        self.hidden = False         # hide() was called and show() has not been
        self.has_buffer = False     # a frame was presented since the last map
        self.geometry = (anchor, tuple(size), exclusive_zone)

    # -- registry ---------------------------------------------------------
    def _on_global(self, data, reg, name, iface, version):
        iface = iface.decode()
        if iface == "zwlr_layer_shell_v1":
            self.shell_advertised = version
            self.shell_version = min(version, zwlr_layer_shell_v1_interface.version)
            self.shell = marshal(reg, _WL_REGISTRY_BIND,
                                 [('u', name), ('s', b"zwlr_layer_shell_v1"),
                                  ('u', self.shell_version), ('n', None)],
                                 new_iface=zwlr_layer_shell_v1_interface,
                                 version=self.shell_version)
        elif iface == "wl_output":
            # v4 is the first version with the `name` event ("DSI-1").
            v = min(version, 4)
            proxy = marshal(reg, _WL_REGISTRY_BIND,
                            [('u', name), ('s', b"wl_output"), ('u', v),
                             ('n', None)],
                            new_iface=wl_output_interface, version=v)
            out = _Output(name, proxy, v)
            self._add_output_listener(out)
            self.outputs[name] = out

    def _on_global_remove(self, data, reg, name):
        out = self.outputs.pop(name, None)
        if out is not None:
            # The proxy (and its listener) stays alive until destroy(); keep
            # the Python side too, or a late event calls freed callbacks.
            self._gone_outputs.append(out)
            self.log("wl_layer: output global removed: %s" % out.name)

    def _add_output_listener(self, out):
        def geometry(d, p, x, y, pw, ph, sub, make, model, tr):
            pass

        def mode(d, p, flags, w, h, refresh):
            if flags & 1:           # WL_OUTPUT_MODE_CURRENT
                out.mode = (w, h, refresh)

        def done(d, p):
            out.done = True

        def scale(d, p, f):
            out.scale = f

        def name(d, p, s):
            out.name = s.decode()

        def description(d, p, s):
            out.description = s.decode()

        # One slot per event of the BOUND version; a NULL slot for an event the
        # server sends is a crash, so v4 needs all six.
        funcs = [_OUT_GEOMETRY(geometry), _OUT_MODE(mode), _OUT_DONE(done),
                 _OUT_SCALE(scale), _OUT_STR(name), _OUT_STR(description)]
        out.listener = _Listener(out.proxy, funcs)

    # -- layer surface ----------------------------------------------------
    def _on_configure(self, data, ls, serial, w, h):
        # Ack immediately (the protocol wants the ack before the next commit of
        # a buffer sized for it). The resize itself happens in the caller.
        marshal(ls, 6, [('u', serial)])                 # ack_configure
        self.configure_count += 1
        self.configured = True
        self._pending = (w, h)
        self.size = (w, h)
        self.log("wl_layer: configure #%d serial=%d size=%dx%d (acked)"
                 % (self.configure_count, serial, w, h))

    def _on_closed(self, data, ls):
        self.closed = True
        self.log("wl_layer: compositor sent closed")

    def pending_size(self):
        p, self._pending = self._pending, None
        return p

    def _roundtrip(self, what):
        if _wl.wl_display_roundtrip(self.display) < 0:
            err = _wl.wl_display_get_error(self.display)
            raise RuntimeError("wl_display_roundtrip failed during %s "
                               "(display error %d - protocol error?)" % (what, err))

    def create(self):
        self.registry = marshal(self.display, _WL_DISPLAY_GET_REGISTRY,
                                [('n', None)], new_iface=wl_registry_interface)
        self._listeners.append(_Listener(self.registry, [
            _REG_GLOBAL(self._on_global), _REG_REMOVE(self._on_global_remove)]))
        self._roundtrip("registry enumeration")     # globals -> binds
        self._roundtrip("wl_output events")         # binds -> name/mode/done

        if not self.shell:
            raise RuntimeError("compositor does not advertise zwlr_layer_shell_v1")
        names = [o.name for o in self.outputs.values()]
        self.log("wl_layer: zwlr_layer_shell_v1 advertised v%d, bound v%d; "
                 "outputs %s" % (self.shell_advertised, self.shell_version, names))
        match = [o for o in self.outputs.values() if o.name == self.output_name]
        if not match:
            # Refuse rather than pass NULL: a NULL output means "the focused
            # output", which on this device is the top screen (DP-1).
            raise RuntimeError("no wl_output named %r (have %s)"
                               % (self.output_name, names))
        self.output = match[0]

        self.proxy = marshal(self.shell, 0,             # get_layer_surface
                             [('n', None), ('o', self.surface),
                              ('o', self.output.proxy), ('u', self.layer),
                              ('s', self.namespace)],
                             new_iface=zwlr_layer_surface_v1_interface,
                             version=self.shell_version)
        self._listeners.append(_Listener(self.proxy, [
            _LS_CONFIGURE(self._on_configure), _LS_CLOSED(self._on_closed)]))
        marshal(self.proxy, 0, [('u', self.req_size[0]), ('u', self.req_size[1])])
        marshal(self.proxy, 1, [('u', self.anchor)])
        marshal(self.proxy, 2, [('i', self.exclusive_zone)])
        marshal(self.proxy, 4, [('u', self.keyboard)])
        # Initial commit WITHOUT a buffer: that is what asks for the first
        # configure. Attaching a buffer before acking it is a protocol error,
        # so the caller must not present anything until create() returns.
        marshal(self.surface, _WL_SURFACE_COMMIT, [],
                version=_wl.wl_proxy_get_version(self.surface))
        self._roundtrip("first configure")
        if self.size is None:
            raise RuntimeError("no configure received after initial commit")
        if self.closed:
            raise RuntimeError("layer surface closed immediately")
        self.geometry = (self.anchor, tuple(self.req_size), self.exclusive_zone)
        return self.size

    # -- runtime reconfiguration -----------------------------------------
    @property
    def can_present(self):
        """True when attaching a buffer (SDL_RenderPresent) is legal: the
        surface is not hidden, has acked a configure, and is not closed."""
        return bool(self.proxy) and not self.hidden and self.configured \
            and not self.closed

    def _send_geometry(self, anchor, size, exclusive_zone):
        marshal(self.proxy, 0, [('u', int(size[0])), ('u', int(size[1]))])
        marshal(self.proxy, 1, [('u', int(anchor))])
        marshal(self.proxy, 2, [('i', int(exclusive_zone))])
        marshal(self.proxy, 4, [('u', self.keyboard)])  # re-assert NONE
        self.geometry = (anchor, (int(size[0]), int(size[1])), int(exclusive_zone))

    def _commit(self):
        marshal(self.surface, _WL_SURFACE_COMMIT, [],
                version=_wl.wl_proxy_get_version(self.surface))

    def set_geometry(self, anchor, size, exclusive_zone):
        """Change anchor/size/exclusive zone of a MAPPED surface in place.

        The commit carries the currently attached buffer along, so the panel
        never disappears (a bare DSI-1 pixel steals focus when tapped). The
        compositor answers with a configure for the new size; the caller
        picks it up with pending_size(), resizes, and presents a new frame.
        If the surface is hidden this only records the geometry for show()."""
        if self.hidden:
            self.geometry = (anchor, (int(size[0]), int(size[1])), int(exclusive_zone))
            return
        self._send_geometry(anchor, size, exclusive_zone)
        self._commit()
        self._roundtrip("set_geometry")
        self.log("wl_layer: geometry anchor=%d size=%dx%d zone=%d (configure #%d)"
                 % (anchor, size[0], size[1], exclusive_zone, self.configure_count))

    def set_layer(self, layer):
        """CC5: move the surface to another layer (zwlr_layer_surface_v1.
        set_layer, since v2). Double-buffered: it takes effect with the next
        commit - the caller follows it with show() or set_geometry(), which
        commit (and re-assert keyboard interactivity NONE). sway 1.11
        advertises v4 and reparents the scene node on that commit
        (desktop/layer_shell.c:273-277). Why it matters: sway stacks TOP
        below a fullscreen window and OVERLAY above it (tree/root.c:43-56),
        so the Command Center over an emulator's fullscreen second window
        must be on OVERLAY. Recorded in self.layer either way, so rebind()
        recreates the role on the same layer. Returns True if a request was
        sent (False: unchanged, or the bound version is < 2)."""
        layer = int(layer)
        if layer == self.layer:
            return False
        if self.proxy and self.shell_version < 2:
            self.log("wl_layer: set_layer needs layer-shell v2, bound v%d; layer stays %d"
                     % (self.shell_version, self.layer))
            return False
        self.layer = layer
        if not self.proxy:
            return False                # create() / rebind() will use it
        marshal(self.proxy, 8, [('u', layer)])          # set_layer
        self.log("wl_layer: layer -> %d (applies at the next commit)" % layer)
        return True

    def presented(self):
        """The caller must call this after every present: it records that a
        buffer is attached, i.e. that the surface is actually mapped."""
        self.has_buffer = True

    def hide(self):
        """Unmap: attach a NULL buffer and commit. Nothing is drawn and the
        surface receives no input until show().

        If no buffer was ever presented since the last map, there is nothing
        to unmap: sending NULL would not reset the role state (wlroots only
        resets on an unmap commit), so no fresh configure would follow the
        remap and can_present would never come back. Only the flag is set."""
        if self.hidden or not self.proxy:
            return
        self.hidden = True
        if not self.has_buffer:
            self.log("wl_layer: hidden before any frame was shown (nothing to unmap)")
            return
        marshal(self.surface, _WL_SURFACE_ATTACH, [('o', None), ('i', 0), ('i', 0)],
                version=_wl.wl_proxy_get_version(self.surface))
        self._commit()
        self.has_buffer = False
        self.configured = False     # the protocol resets the role state on unmap
        _wl.wl_display_flush(self.display)
        self.log("wl_layer: hidden (NULL buffer committed)")

    def show(self, anchor, size, exclusive_zone):
        """Remap after hide(): re-send the whole state, commit WITHOUT a
        buffer, and roundtrip for the configure. The caller presents a frame
        once can_present is True (normally already true on return)."""
        if not self.proxy:
            raise RuntimeError("show() before create()")
        if not self.hidden:
            self.set_geometry(anchor, size, exclusive_zone)
            return
        self._send_geometry(anchor, size, exclusive_zone)
        self.hidden = False
        self._commit()
        self._roundtrip("remap")
        self.log("wl_layer: shown anchor=%d size=%dx%d zone=%d configured=%s"
                 % (anchor, size[0], size[1], exclusive_zone, self.configured))

    def rebind(self, output_name):
        """SW1: move this surface to another output (the screen swap). A
        layer surface's output is fixed at get_layer_surface, so the role
        object is recreated on the SAME wl_surface - which the protocol
        allows once the old role object is destroyed (a wl_surface may take
        the same role again), provided no buffer is attached at that moment
        (wlroots rejects get_layer_surface on a surface with a buffer). So:

          1. unmap: NULL buffer + commit (only if a frame was ever presented);
          2. destroy the old zwlr_layer_surface_v1;
          3. get_layer_surface on the new wl_output, re-send the recorded
             geometry and keyboard interactivity NONE, commit WITHOUT a
             buffer, roundtrip for the configure.

        The caller presents a new frame when can_present is true (damage
        everything: nothing is on screen after step 1). A hidden surface
        stays hidden: its new role object is created and configured but not
        mapped, and show() maps it as usual. Raises RuntimeError on failure
        (unknown output, protocol error); main.py then exits with code 4 and
        the supervisor starts a fresh surface - which binds to the right
        output by itself, so a failed rebind costs a restart, never the
        panel."""
        if not self.proxy:
            raise RuntimeError("rebind() before create()")
        match = [o for o in self.outputs.values() if o.name == output_name]
        if not match:
            raise RuntimeError("no wl_output named %r (have %s)"
                               % (output_name, [o.name for o in self.outputs.values()]))
        old = self.output.name if self.output else None
        if self.has_buffer:
            marshal(self.surface, _WL_SURFACE_ATTACH, [('o', None), ('i', 0), ('i', 0)],
                    version=_wl.wl_proxy_get_version(self.surface))
            self._commit()
            self.has_buffer = False
        marshal(self.proxy, 7, [], destroy=True)        # layer_surface.destroy
        self.proxy = None
        self.configured = False
        self.closed = False         # a `closed` was about the old role object (e.g. its output left)
        self.size = None
        self._pending = None
        self.output = match[0]
        anchor, size, zone = self.geometry
        self.proxy = marshal(self.shell, 0,             # get_layer_surface
                             [('n', None), ('o', self.surface),
                              ('o', self.output.proxy), ('u', self.layer),
                              ('s', self.namespace)],
                             new_iface=zwlr_layer_surface_v1_interface,
                             version=self.shell_version)
        # The old listener stays in self._listeners: libwayland may still
        # hold its table for events queued before the destroy.
        self._listeners.append(_Listener(self.proxy, [
            _LS_CONFIGURE(self._on_configure), _LS_CLOSED(self._on_closed)]))
        self._send_geometry(anchor, size, zone)
        self._commit()
        self._roundtrip("rebind to %s" % output_name)
        if self.size is None:
            raise RuntimeError("no configure after rebind to %s" % output_name)
        if self.closed:
            raise RuntimeError("layer surface closed after rebind to %s" % output_name)
        self.log("wl_layer: rebound %s -> %s (configure #%d, hidden=%s)"
                 % (old, output_name, self.configure_count, self.hidden))
        return self.size

    def destroy(self):
        """Tear down in reverse order. Does NOT touch the wl_surface or the
        wl_display - those belong to SDL."""
        if self.proxy:
            marshal(self.proxy, 7, [], destroy=True)    # layer_surface.destroy
            self.proxy = None
        if self.shell:
            if self.shell_version >= 3:
                marshal(self.shell, 1, [], destroy=True)
            else:
                _wl.wl_proxy_destroy(self.shell)
            self.shell = None
        for out in list(self.outputs.values()) + self._gone_outputs:
            if out.version >= 3:
                marshal(out.proxy, _WL_OUTPUT_RELEASE, [], destroy=True)
            else:
                _wl.wl_proxy_destroy(out.proxy)
        self.outputs.clear()
        self._gone_outputs.clear()
        if self.registry:
            _wl.wl_proxy_destroy(self.registry)         # wl_registry has no destructor request
            self.registry = None
        _wl.wl_display_flush(self.display)
        # Listener tables are dropped only now that no proxy can call them.
        self._listeners.clear()
