#!/usr/bin/env python3
"""gamma_dim: software brightness for one sway output.

The Dual Screen add-on has no brightness control the handheld can reach: no backlight device,
and its DisplayPort AUX I2C channel (i2c-20, dpu_dp_aux) doesnt even answer an EDID read from
userspace, so DDC/CI is out. sway implements wlr-gamma-control-unstable-v1, so this dims the
output by scaling its gamma ramps.

    python3 gamma_dim.py DP-1          then write levels (0.05..1.0), one per line

It holds the gamma control for as long as it runs, and sway puts the normal gamma back the
moment it exits or crashes, so a dead helper can never leave the screen dim. brightness.TopDim
owns the process. Exit codes: 2 no such output or no gamma manager, 3 sway refused (another
client holds the gamma).
"""
import os
import sys
from ctypes import CFUNCTYPE, c_uint32, c_void_p

import wl_layer as W
from brightness import ramps   # pure, tested offline (tests/test_brightness.py)

_wl = W._wl
_wl.wl_display_connect.restype = c_void_p
_wl.wl_display_connect.argtypes = [c_void_p]
_wl.wl_display_disconnect.restype = None
_wl.wl_display_disconnect.argtypes = [c_void_p]

gamma_control_iface = W.wl_interface()
gamma_manager_iface = W.wl_interface()
W._fill(gamma_control_iface, "zwlr_gamma_control_v1", 1,
        methods=[("set_gamma", "h", [None]),                                 # 0
                 ("destroy", "", [])],                                       # 1
        events=[("gamma_size", "u", [None]),                                 # 0
                ("failed", "", [])])                                         # 1
W._fill(gamma_manager_iface, "zwlr_gamma_control_manager_v1", 1,
        methods=[("get_gamma_control", "no", [gamma_control_iface, W.wl_output_interface]),
                 ("destroy", "", [])],
        events=[])

_GAMMA_SIZE = CFUNCTYPE(None, c_void_p, c_void_p, c_uint32)
_FAILED = CFUNCTYPE(None, c_void_p, c_void_p)
MIN_LEVEL = 0.05


class GammaDim:
    def __init__(self, output_name):
        self.output_name = output_name
        self.display = None
        self.manager = None
        self.outputs = {}           # global name -> W._Output
        self.control = None
        self.size = 0
        self.failed = False
        self._listeners = []

    def _on_global(self, data, reg, name, iface, version):
        iface = iface.decode()
        if iface == "zwlr_gamma_control_manager_v1":
            self.manager = W.marshal(reg, W._WL_REGISTRY_BIND,
                                     [('u', name), ('s', b"zwlr_gamma_control_manager_v1"),
                                      ('u', 1), ('n', None)],
                                     new_iface=gamma_manager_iface, version=1)
        elif iface == "wl_output":
            v = min(version, 4)
            proxy = W.marshal(reg, W._WL_REGISTRY_BIND,
                              [('u', name), ('s', b"wl_output"), ('u', v), ('n', None)],
                              new_iface=W.wl_output_interface, version=v)
            out = W._Output(name, proxy, v)
            self._listen_output(out)
            self.outputs[name] = out

    def _listen_output(self, out):
        def name_ev(data, proxy, s):
            out.name = s.decode() if s else None
        funcs = [W._OUT_GEOMETRY(lambda *a: None), W._OUT_MODE(lambda *a: None),
                 W._OUT_DONE(lambda *a: None), W._OUT_SCALE(lambda *a: None),
                 W._OUT_STR(name_ev), W._OUT_STR(lambda *a: None)]
        self._listeners.append(W._Listener(out.proxy, funcs[:4 if out.version < 4 else 6]))

    def connect(self):
        self.display = _wl.wl_display_connect(None)
        if not self.display:
            raise SystemExit("gamma_dim: cannot connect to the Wayland display")
        reg = W.marshal(self.display, W._WL_DISPLAY_GET_REGISTRY, [('n', None)],
                        new_iface=W.wl_registry_interface, version=1)
        self._listeners.append(W._Listener(reg, [W._REG_GLOBAL(self._on_global),
                                                 W._REG_REMOVE(lambda *a: None)]))
        _wl.wl_display_roundtrip(self.display)          # globals
        _wl.wl_display_roundtrip(self.display)          # output names
        out = next((o for o in self.outputs.values() if o.name == self.output_name), None)
        if self.manager is None or out is None:
            print("gamma_dim: %s" % ("no gamma manager" if self.manager is None else
                                     "no output %s" % self.output_name), file=sys.stderr)
            sys.exit(2)
        self.control = W.marshal(self.manager, 0, [('n', None), ('o', out.proxy)],
                                 new_iface=gamma_control_iface, version=1)

        def size_ev(data, proxy, n):
            self.size = int(n)

        def failed_ev(data, proxy):
            self.failed = True
        self._listeners.append(W._Listener(self.control, [_GAMMA_SIZE(size_ev),
                                                          _FAILED(failed_ev)]))
        _wl.wl_display_roundtrip(self.display)
        if self.failed or self.size <= 0:
            print("gamma_dim: sway refused the gamma control (size %d)" % self.size,
                  file=sys.stderr)
            sys.exit(3)

    def set_level(self, level):
        fd = os.memfd_create("rp5deck-gamma", os.MFD_CLOEXEC)
        try:
            os.write(fd, ramps(self.size, level))
            os.lseek(fd, 0, os.SEEK_SET)
            W.marshal(self.control, 0, [('i', fd)])        # set_gamma(fd): 'h' shares 'i'
            _wl.wl_display_flush(self.display)
            _wl.wl_display_roundtrip(self.display)
        finally:
            os.close(fd)
        if self.failed:
            sys.exit(3)


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    g = GammaDim(argv[1])
    g.connect()
    print("ready size=%d" % g.size, flush=True)
    for line in sys.stdin:
        try:
            level = float(line.strip())
        except ValueError:
            continue
        g.set_level(level)
        print("level %.3f" % max(MIN_LEVEL, min(1.0, level)), flush=True)
    return 0  # EOF, exiting puts the output's gamma back


if __name__ == "__main__":
    sys.exit(main(sys.argv))
