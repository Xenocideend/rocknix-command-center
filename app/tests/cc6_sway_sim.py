#!/usr/bin/env python3
"""cc6_sway_sim - a small, stateful stand-in for sway's window tree, for
window_switcher / app_tabs tests (CC6). SYNTHETIC: it models only what the
switcher's three command shapes touch, plus the one sway behaviour that
makes a parked window dangerous (focusing a window on a hidden workspace
makes that workspace visible).

Model:
  * outputs in order, each showing one current workspace; workspaces hold
    tiling and floating window ids; the __i3 / __i3_scratch pseudo output is
    always emitted (empty) like real sway;
  * `[con_id=N] move container to workspace NAME` - NAME is created when
    missing (on the focused output, or with park_on="source" on the moved
    window's own output), never made current; the window keeps its
    fullscreen / floating state;
  * `[con_id=N] move container to output X` - onto X's current workspace;
  * both moves: if N had focus, focus goes to its old workspace (sway's
    cmd_move_container "restore focus"); with the pessimistic knob
    focus_follows_source=True it goes there even when N did not have focus;
  * an empty workspace that is neither current nor focused is destroyed;
  * `[con_id=N] focus` - focus N; if N's workspace is not current on its
    output, that workspace BECOMES current (what makes a parked window
    reappear over the game);
  * any other command: success false (the switcher must never send one);
  * knobs: refuse=True answers every command with success false and
    changes nothing; lie=True answers success true and changes nothing.

Server: serve(sim) returns a real window_switcher.Ipc connected over a
socketpair to a thread speaking the i3-ipc wire protocol, so the REAL
client code (framing, enforce_allowed in run_command) is what runs.
"""
import json
import re
import socket
import struct
import threading

MAGIC = b"i3-ipc"
_HDR = struct.Struct("=6sII")

_MOVE_WS = re.compile(r"\[con_id=(\d+)\] move container to workspace (\S+)")
_MOVE_OUT = re.compile(r"\[con_id=(\d+)\] move container to output (\S+)")
_FOCUS = re.compile(r"\[con_id=(\d+)\] focus")
_WORKSPACE = re.compile(r"workspace (?:number )?(\S+)")   # shows the workspace, focuses what is on it


class Sim:
    def __init__(self, outputs=("DP-1", "DSI-1"), park_on="focused", focus_follows_source=False):
        self.order = list(outputs)
        self.current = {}               # output -> ws name
        self.prev = {}                  # output -> the workspace it showed before (where an emptied one goes back to)
        self.ws = {}                    # ws name -> {"output", "tiling": [], "floating": []}
        self.win = {}                   # id -> dict(app_id, title, fullscreen, pid)
        self.focus = None               # ("con", id) | ("ws", name) | None
        self.park_on = park_on
        self.focus_follows_source = focus_follows_source
        self.refuse = False
        self.lie = False
        self.received = []              # every RUN_COMMAND payload, verbatim
        self.next_id = 100
        for i, o in enumerate(self.order):
            self.add_ws(str(i + 1), o, current=True)

    # -- building --------------------------------------------------------------
    def add_ws(self, name, output, current=False):
        self.ws[name] = {"output": output, "tiling": [], "floating": []}
        if current or output not in self.current:
            self.current[output] = name
        return name

    def ws_of_output(self, output):
        return self.current[output]

    def map(self, output, app_id=None, title="", fullscreen=False, floating=False, focus=False,
            con_id=None):
        cid = con_id if con_id is not None else self.next_id
        self.next_id = max(self.next_id, cid) + 1
        self.win[cid] = {"app_id": app_id, "title": title, "fullscreen": fullscreen,
                         "pid": 1000 + cid}
        ws = self.current[output]
        self.ws[ws]["floating" if floating else "tiling"].append(cid)
        if focus:
            self.focus = ("con", cid)
        return cid

    def close(self, cid):
        ws = self.ws_name_of(cid)
        for key in ("tiling", "floating"):
            if cid in self.ws[ws][key]:
                self.ws[ws][key].remove(cid)
        del self.win[cid]
        if self.focus == ("con", cid):
            self.focus = ("ws", ws)
        out = self.ws[ws]["output"]
        if not (self.ws[ws]["tiling"] or self.ws[ws]["floating"]) and self.current.get(out) == ws:
            # sway goes back to the workspace the output showed before when the one on screen empties
            back = self.prev.get(out)
            if back in self.ws and back != ws:
                self._switch_to(back)
                rest = self.ws[back]["tiling"] + self.ws[back]["floating"]
                self.focus = ("con", rest[-1]) if rest else ("ws", back)
        self._reap()

    def _switch_to(self, name):
        out = self.ws[name]["output"]
        old = self.current.get(out)
        if old != name:
            self.prev[out] = old
            self.current[out] = name

    def ws_name_of(self, cid):
        for name, w in self.ws.items():
            if cid in w["tiling"] or cid in w["floating"]:
                return name
        return None

    def output_of(self, cid):
        return self.ws[self.ws_name_of(cid)]["output"]

    def focused_output(self):
        if self.focus is None:
            return self.order[0]
        if self.focus[0] == "con":
            return self.output_of(self.focus[1])
        return self.ws[self.focus[1]]["output"]

    def undock(self, output="DP-1"):
        """The output goes away; its workspaces move to the first other one."""
        self.order.remove(output)
        dest = self.order[0]
        for w in self.ws.values():
            if w["output"] == output:
                w["output"] = dest
        del self.current[output]

    # -- commands --------------------------------------------------------------
    def _reap(self):
        for name in list(self.ws):
            w = self.ws[name]
            if w["tiling"] or w["floating"]:
                continue
            if self.current.get(w["output"]) == name or self.focus == ("ws", name):
                continue
            del self.ws[name]

    def _detach(self, cid):
        src = self.ws_name_of(cid)
        key = "floating" if cid in self.ws[src]["floating"] else "tiling"
        self.ws[src][key].remove(cid)
        return src, key

    def _move(self, cid, dest_ws):
        had_focus = self.focus == ("con", cid)
        src, key = self._detach(cid)
        self.ws[dest_ws][key].append(cid)
        if had_focus or self.focus_follows_source:
            rest = self.ws[src]["tiling"] + self.ws[src]["floating"]
            self.focus = ("con", rest[-1]) if rest else ("ws", src)
        self._reap()

    def command(self, part):
        m = _MOVE_WS.fullmatch(part)
        if m:
            cid, name = int(m.group(1)), m.group(2)
            if cid not in self.win:
                return {"success": False, "error": "No matching node."}
            if name not in self.ws:
                out = self.focused_output() if self.park_on == "focused" else self.output_of(cid)
                self.add_ws(name, out)
            self._move(cid, name)
            return {"success": True}
        m = _MOVE_OUT.fullmatch(part)
        if m:
            cid, out = int(m.group(1)), m.group(2)
            if cid not in self.win:
                return {"success": False, "error": "No matching node."}
            if out not in self.current:
                return {"success": False, "error": "Can't find output with name '%s'" % out}
            self._move(cid, self.current[out])
            return {"success": True}
        m = _WORKSPACE.fullmatch(part)
        if m:
            name = m.group(1)
            if name not in self.ws:
                self.add_ws(name, self.focused_output())
            self._switch_to(name)
            rest = self.ws[name]["tiling"] + self.ws[name]["floating"]
            self.focus = ("con", rest[-1]) if rest else ("ws", name)
            self._reap()
            return {"success": True}
        m = _FOCUS.fullmatch(part)
        if m:
            cid = int(m.group(1))
            if cid not in self.win:
                return {"success": False, "error": "No matching node."}
            ws = self.ws_name_of(cid)
            self._switch_to(ws)                             # focusing shows its workspace
            self.focus = ("con", cid)
            self._reap()
            return {"success": True}
        return {"success": False, "error": "Unknown/invalid command (sim)"}

    def run(self, payload):
        self.received.append(payload)
        parts = payload.split(";")
        if self.refuse:
            return [{"success": False, "error": "refused (sim knob)"} for _ in parts]
        if self.lie:
            return [{"success": True} for _ in parts]
        return [self.command(p.strip()) for p in parts]

    # -- the tree ----------------------------------------------------------------
    def _con(self, cid, floating):
        w = self.win[cid]
        return {"type": "floating_con" if floating else "con", "id": cid, "name": w["title"],
                "app_id": w["app_id"], "pid": w["pid"], "shell": "xdg_shell",
                "fullscreen_mode": 1 if w["fullscreen"] else 0,
                "focused": self.focus == ("con", cid), "nodes": [], "floating_nodes": []}

    def tree(self):
        outs = [{"type": "output", "id": 2147483647, "name": "__i3", "nodes": [
            {"type": "workspace", "id": 2147483646, "name": "__i3_scratch", "nodes": [],
             "floating_nodes": []}]}]
        for i, o in enumerate(self.order):
            wss = []
            for name, w in self.ws.items():
                if w["output"] != o:
                    continue
                wss.append({"type": "workspace", "id": 5000 + len(wss) + 10 * i, "name": name,
                            "focused": self.focus == ("ws", name),
                            "nodes": [self._con(c, False) for c in w["tiling"]],
                            "floating_nodes": [self._con(c, True) for c in w["floating"]]})
            outs.append({"type": "output", "id": 3 + i, "name": o, "current_workspace":
                         self.current[o], "nodes": wss})
        return {"type": "root", "id": 1, "name": "root", "nodes": outs}

    # -- a direct connection (no socket) ---------------------------------------------
    def connection(self, check=True):
        """An object with get_tree()/run_command() that enforces the real
        allow-list (check=True) - for tests that do not need the wire."""
        sim = self

        class Conn:
            def get_tree(self):
                return sim.tree()

            def run_command(self, payload, tree, cc_output, es_output):
                if check:
                    import window_switcher
                    window_switcher.enforce_allowed(payload, tree, cc_output, es_output)
                return sim.run(payload)

            def close(self):
                pass
        return Conn()


# ---------------------------------------------------------------------------
# A real i3-ipc server over a socketpair
# ---------------------------------------------------------------------------
def _read(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _server(sock, sim):
    try:
        while True:
            hdr = _read(sock, _HDR.size)
            if hdr is None:
                return
            magic, length, mtype = _HDR.unpack(hdr)
            payload = _read(sock, length) if length else b""
            if mtype == 4:
                body = sim.tree()
            elif mtype == 0:
                body = sim.run(payload.decode())
            else:
                body = {"success": False, "error": "unsupported type %d" % mtype}
            data = json.dumps(body).encode()
            sock.sendall(_HDR.pack(MAGIC, len(data), mtype) + data)
    except OSError:
        return
    finally:
        sock.close()


def serve(sim):
    """A real window_switcher.Ipc whose socket talks to `sim`."""
    import window_switcher
    a, b = socket.socketpair()
    a.settimeout(3.0)
    threading.Thread(target=_server, args=(b, sim), daemon=True).start()
    ipc = window_switcher.Ipc.__new__(window_switcher.Ipc)
    ipc.sock = a
    return ipc
