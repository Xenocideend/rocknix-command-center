#!/usr/bin/env python3
"""A stand-in for swaymsg for the built-in-panels layout daemon. State is a JSON file ($SIM_STATE):

    {"outputs": [{"name": "DSI-1", "power": false}, ...],
     "inputs":  [{"identifier": "0:0:bottom_touchscreen", "type": "touch", "send_events": "disabled"}, ...],
     "mapped":  {}, "silent": false}

Queries print what real sway prints (pretty JSON, two-space indent, nested objects below the fields the daemon reads) and
every call is appended to $SIM_LOG. Commands change the state like sway would.
"""
import json
import os
import sys

state_path = os.environ["SIM_STATE"]
with open(state_path, encoding="utf-8") as f:
    st = json.load(f)
args = sys.argv[1:]
with open(os.environ["SIM_LOG"], "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\n")

if args[:3] == ["-r", "-t", "get_outputs"] or args[:2] == ["-t", "get_outputs"]:
    if st.get("silent"):
        sys.exit(1)
    outs = []
    for o in st["outputs"]:
        outs.append({"name": o["name"], "make": "Unknown", "model": "Unknown", "serial": "Unknown",
                     "active": True, "dpms": o["power"], "power": o["power"], "scale": 1.0, "transform": "270",
                     "current_workspace": "1",
                     "modes": [{"width": 1080, "height": 1920, "refresh": 60000, "picture_aspect_ratio": "none"}],
                     "current_mode": {"width": 1080, "height": 1920, "refresh": 60000},
                     "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                     "focused": False})
    print(json.dumps(outs, indent=2))
elif args[:3] == ["-r", "-t", "get_inputs"] or args[:2] == ["-t", "get_inputs"]:
    if st.get("silent"):
        sys.exit(1)
    ins = []
    for i in st["inputs"]:
        d = {"identifier": i["identifier"], "name": i["identifier"].split(":")[-1], "vendor": 0, "product": 0,
             "type": i.get("type", "touch")}
        if i.get("send_events") is not None:
            d["libinput"] = {"send_events": i["send_events"], "calibration_matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]}
        ins.append(d)
    print(json.dumps(ins, indent=2))
elif len(args) == 4 and args[0] == "output" and args[2] == "power":
    for o in st["outputs"]:
        if o["name"] == args[1]:
            o["power"] = args[3] == "on"
elif len(args) == 4 and args[0] == "input" and args[2] == "events":
    for i in st["inputs"]:
        if i["identifier"] == args[1]:
            i["send_events"] = args[3]
elif len(args) == 4 and args[0] == "input" and args[2] == "map_to_output":
    st.setdefault("mapped", {})[args[1]] = args[3]

with open(state_path, "w", encoding="utf-8") as f:
    json.dump(st, f)
