#!/usr/bin/env python3
"""sw1_sway_sim - a small, stateful stand-in for sway, driven through a fake
`swaymsg`, so the REAL 092-dual-screen-persist main loop can be run under dash
on a PC (tests/test_sw1_092.py). SYNTHETIC: it models only what 092 touches.

World state lives in a JSON file ($SIM_STATE); every swaymsg call loads it,
acts, saves it, and appends its argv to $SIM_LOG (one line per call) so two
versions of 092 can be compared command for command.

Modelled sway behaviour (and the one knob where real sway is uncertain):
  * outputs, workspaces (name, output), one visible workspace per output,
    the focused workspace and window; an output always shows a workspace
    (a new lowest-free-number one if it has none); an empty workspace that
    is neither visible nor focused is destroyed.
  * `workspace N output A B...` records the assignment. assign_moves=True
    ALSO moves an existing workspace to the first present output in the list;
    False (default) does not. 092's SW1 code must converge under BOTH.
  * `move container to output X` puts the container on X's visible
    workspace; keyboard focus stays where it was (the source workspace).
  * `move workspace to output X` moves the focused workspace, which becomes
    X's visible one.
  * for_window rules are applied in registration order when a window maps;
    the world starts with ROCKNIX's else-branch rule (what the live config
    has, R3/R4): for_window [app_id="emulationstation"] move output DP-1.
    `exec` commands in rules are really run (sh -c), like sway does.
  * a new window takes focus only if it ends up on the focused workspace
    (sway's should_focus), unless a rule focused it explicitly.

Timeline actions (state["timeline"][str(tick)] = [[action, arg], ...]),
applied by `sw1_sway_sim.py --tick`, which the fake `sleep 5` calls:
  config OBJ|null, raw TEXT, game_start, game_exit, undock, dock,
  map {app_id,title}, stop.
"""
import json
import os
import re
import subprocess
import sys

STATE = os.environ.get("SIM_STATE", "")
LOG = os.environ.get("SIM_LOG", "")


def load():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------------------
# world helpers
# ---------------------------------------------------------------------------
def ws_get(st, name):
    for w in st["workspaces"]:
        if w["name"] == name:
            return w
    return None


def ws_on(st, output):
    return [w for w in st["workspaces"] if w["output"] == output]


def lowest_free(st):
    n = 1
    names = set(w["name"] for w in st["workspaces"])
    while str(n) in names:
        n += 1
    return str(n)


def assigned_output(st, name):
    for o in st["assign"].get(name, []):
        if o in st["outputs"]:
            return o
    return None


def create_ws(st, name, output):
    o = assigned_output(st, name) or output
    w = {"name": name, "output": o, "windows": []}
    st["workspaces"].append(w)
    return w


def cleanup(st):
    keep = []
    for w in st["workspaces"]:
        visible = st["visible"].get(w["output"]) == w["name"]
        if w["windows"] or visible or w["name"] == st["focused_ws"]:
            keep.append(w)
    st["workspaces"] = keep
    for o in st["outputs"]:
        if not ws_get(st, st["visible"].get(o, "")) or ws_get(st, st["visible"][o])["output"] != o:
            mine = ws_on(st, o)
            if mine:
                st["visible"][o] = mine[0]["name"]
            else:
                w = {"name": lowest_free(st), "output": o, "windows": []}
                st["workspaces"].append(w)
                st["visible"][o] = w["name"]


def make_visible(st, w):
    st["visible"][w["output"]] = w["name"]


def focus_ws(st, w):
    make_visible(st, w)
    st["focused_ws"] = w["name"]
    st["focused_win"] = w["windows"][-1]["app_id"] if w["windows"] else None
    cleanup(st)


def find_windows(st, key, pattern):
    out = []
    for w in st["workspaces"]:
        for win in w["windows"]:
            if re.search(pattern, win.get(key) or ""):
                out.append((w, win))
    return out


def detach(st, w, win):
    w["windows"].remove(win)
    if st["focused_win"] == win["app_id"] and st["focused_ws"] == w["name"]:
        st["focused_win"] = w["windows"][-1]["app_id"] if w["windows"] else None


def move_to_ws(st, w, win, target):
    detach(st, w, win)
    target["windows"].append(win)


def es_where(st):
    for w in st["workspaces"]:
        for win in w["windows"]:
            if win["app_id"] == "emulationstation":
                return w
    return None


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------
def get_outputs(st):
    return [{"name": o, "active": True, "current_workspace": st["visible"].get(o)}
            for o in st["outputs"]]


def get_workspaces(st):
    return [{"name": w["name"], "output": w["output"],
             "visible": st["visible"].get(w["output"]) == w["name"],
             "focused": st["focused_ws"] == w["name"]} for w in st["workspaces"]]


def get_tree(st):
    outs = []
    for o in st["outputs"]:
        wss = []
        for w in ws_on(st, o):
            cons = [{"type": "con", "name": win.get("title"), "app_id": win["app_id"],
                     "pid": 1, "nodes": [], "floating_nodes": [],
                     "focused": st["focused_win"] == win["app_id"] and st["focused_ws"] == w["name"]}
                    for win in w["windows"]]
            wss.append({"type": "workspace", "name": w["name"], "nodes": cons,
                        "floating_nodes": [],
                        "focused": st["focused_ws"] == w["name"] and st["focused_win"] is None})
        outs.append({"type": "output", "name": o, "current_workspace": st["visible"].get(o),
                     "nodes": wss})
    return {"type": "root", "name": "root", "nodes": outs}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
CRIT = re.compile(r'^\[(app_id|title)="(.*)"\]\s*(.*)$')


def run_rule_cmds(st, w, win, cmds):
    """Commands of a for_window rule, in the context of window `win`."""
    for cmd in [c.strip() for c in cmds.split(",")]:
        m = re.match(r"^move (?:container )?(?:to )?output (\S+)$", cmd)
        if m:
            o = m.group(1)
            if o in st["outputs"]:
                w = es_or_self_ws(st, win)
                move_to_ws(st, w, win, ws_get(st, st["visible"][o]))
            continue
        m = re.match(r"^move (?:container )?(?:to )?workspace number (\S+)$", cmd)
        if m:
            w = es_or_self_ws(st, win)
            t = ws_get(st, m.group(1)) or create_ws(st, m.group(1),
                                                     ws_get(st, st["focused_ws"])["output"])
            move_to_ws(st, w, win, t)
            continue
        if cmd == "focus":
            w = es_or_self_ws(st, win)
            focus_ws(st, w)
            st["focused_win"] = win["app_id"]
            win["_focused_by_rule"] = True
            continue
        m = re.match(r"^exec (.*)$", cmd)
        if m:
            st.setdefault("exec_log", []).append(m.group(1))
            save(st)
            subprocess.run(["sh", "-c", m.group(1)], env=os.environ.copy())
            st.update(load())
            continue


def es_or_self_ws(st, win):
    for w in st["workspaces"]:
        if win in w["windows"]:
            return w
        for x in w["windows"]:
            if x["app_id"] == win["app_id"]:
                return w
    return None


def map_window(st, win):
    """A new window maps on the focused workspace, rules run, then sway's
    should_focus decides whether it gets focus."""
    fws = ws_get(st, st["focused_ws"])
    fws["windows"].append(win)
    for rule in list(st["rules"]):
        m = re.match(r'^for_window \[(app_id|title)="(.*?)"\] (.*)$', rule)
        if not m:
            continue
        key, pat, cmds = m.groups()
        if re.search(pat, win.get(key) or ""):
            live = None
            for w in st["workspaces"]:
                for x in w["windows"]:
                    if x["app_id"] == win["app_id"] and x.get("title") == win.get("title"):
                        live = x
            if live is not None:
                run_rule_cmds(st, None, live, cmds)
    w = es_or_self_ws(st, win)
    if w is not None and w["name"] == st["focused_ws"]:
        st["focused_win"] = win["app_id"]
    cleanup(st)


def command(st, text):
    text = text.strip()
    rc = 0
    m = CRIT.match(text)
    if m:
        key, pat, rest = m.groups()
        hits = find_windows(st, key, pat)
        if not hits:
            return 2
        mo = re.match(r"^move container to output (\S+)$", rest)
        mw = re.match(r"^move container to workspace number (\S+)$", rest)
        for w, win in hits:
            if mo and mo.group(1) in st["outputs"]:
                move_to_ws(st, w, win, ws_get(st, st["visible"][mo.group(1)]))
            elif mw:
                t = ws_get(st, mw.group(1)) or create_ws(st, mw.group(1),
                                                          ws_get(st, st["focused_ws"])["output"])
                move_to_ws(st, w, win, t)
        cleanup(st)
        return rc
    if text.startswith("for_window ") or text.startswith("no_focus "):
        st["rules"].append(text)
        return 0
    if text.startswith("output ") or text.startswith("input "):
        return 0
    m = re.match(r"^focus output (\S+)$", text)
    if m:
        o = m.group(1)
        if o in st["outputs"]:
            focus_ws(st, ws_get(st, st["visible"][o]))
        return 0
    m = re.match(r"^workspace (\S+) output (.+)$", text)
    if m:
        name, outs = m.group(1), m.group(2).split()
        st["assign"][name] = outs
        w = ws_get(st, name)
        if st.get("assign_moves") and w is not None:
            o = assigned_output(st, name)
            if o and o != w["output"]:
                w["output"] = o
                make_visible(st, w)
                cleanup(st)
        return 0
    m = re.match(r"^workspace (?:number )?(\S+)$", text)
    if m:
        name = m.group(1)
        w = ws_get(st, name) or create_ws(st, name, ws_get(st, st["focused_ws"])["output"])
        focus_ws(st, w)
        return 0
    m = re.match(r"^move workspace to output (\S+)$", text)
    if m:
        o = m.group(1)
        w = ws_get(st, st["focused_ws"])
        if o in st["outputs"] and w["output"] != o:
            w["output"] = o
            make_visible(st, w)
            cleanup(st)
        return 0
    st.setdefault("unknown", []).append(text)
    return 0


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------
def write_config(st, obj):
    p = st["config_path"]
    if obj is None:
        if os.path.exists(p):
            os.remove(p)
        return
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)       # a new inode, like config.save()


def snapshot(st, label):
    es = es_where(st)
    w1 = ws_get(st, "1")
    st["history"].append({"tick": st["tick"], "label": label,
                          "es": es["output"] if es else None,
                          "es_ws": es["name"] if es else None,
                          "ws1": w1["output"] if w1 else None,
                          "visible": dict(st["visible"]),
                          "focused_ws": st["focused_ws"], "focused_win": st["focused_win"],
                          "windows": {win["app_id"]: w["output"] for w in st["workspaces"]
                                      for win in w["windows"]}})


def act(st, action, arg):
    if action == "config":
        write_config(st, arg)
    elif action == "raw":
        write_config(st, arg)
    elif action == "game_start":
        es = es_where(st)
        if es is not None:
            detach(st, es, next(x for x in es["windows"] if x["app_id"] == "emulationstation"))
        map_window(st, {"app_id": "retroarch", "title": "RetroArch"})
    elif action == "game_exit":
        for w, win in find_windows(st, "app_id", "^retroarch$"):
            detach(st, w, win)
        cleanup(st)
        map_window(st, {"app_id": "emulationstation", "title": "EmulationStation"})
        snapshot(st, "after-es-map")
    elif action == "undock":
        if "DP-1" in st["outputs"]:
            st["outputs"].remove("DP-1")
            st["visible"].pop("DP-1", None)
            for w in st["workspaces"]:
                if w["output"] == "DP-1":
                    w["output"] = "DSI-1"
            f = ws_get(st, st["focused_ws"])
            if f is None or f["output"] != "DSI-1":
                st["focused_ws"] = st["visible"]["DSI-1"]
            cleanup(st)
    elif action == "dock":
        if "DP-1" not in st["outputs"]:
            st["outputs"].append("DP-1")
            for w in st["workspaces"]:
                if st["assign"].get(w["name"], [None])[0] == "DP-1":
                    w["output"] = "DP-1"
                    st["visible"]["DP-1"] = w["name"]
            cleanup(st)
    elif action == "map":
        map_window(st, dict(arg))
    elif action == "stop":
        open(st["disable_path"], "w").close()


def tick():
    st = load()
    st["tick"] += 1
    for action, arg in st["timeline"].get(str(st["tick"]), []):
        act(st, action, arg)
    snapshot(st, "tick")
    if st["tick"] >= st["stop_at"]:
        open(st["disable_path"], "w").close()
    save(st)


def main(argv):
    if argv[:1] == ["--tick"]:
        tick()
        return 0
    st = load()
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(" ".join(argv) + "\n")
    if argv[:1] == ["-t"]:
        kind = argv[1]
        reply = {"get_outputs": get_outputs, "get_workspaces": get_workspaces,
                 "get_tree": get_tree}[kind](st)
        print(json.dumps(reply, indent=2))
        return 0
    rc = command(st, " ".join(argv))
    save(st)
    if rc:
        print('[{"success": false, "error": "No matching node."}]')
    else:
        print('[{"success": true}]')
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
