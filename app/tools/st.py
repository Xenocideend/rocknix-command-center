#!/usr/bin/env python3
"""Shell-side helper for the B1 device verification (b1verify.sh).

  st.py get PATH                 value at a dotted path in /run/rp5deck/state.json
  st.py target NAME              layout coords "X Y" of a widget's centre
  st.py track NAME V             layout coords "X Y" for slider value V
  st.py wait EXPR TIMEOUT        poll state until EXPR (python, `s` = state) is true;
                                 prints elapsed seconds, exit 1 on timeout
  st.py count FILE.ppm R,G,B [Y0 Y1]   pixels within +-3 of a colour (every 2nd px)
  st.py focus                    (stdin: get_tree) focused node "id type name"
  st.py esid                     (stdin: get_tree) EmulationStation con id
  st.py app APP_ID               (stdin: get_tree) "output x y w h" of that app, or "absent"
  st.py apps PREFIX              (stdin: get_tree) app_ids starting with PREFIX
  st.py mode                     (stdin: get_tree) compute_mode() using $APP/sway_ipc.py
"""
import json
import os
import subprocess
import sys
import time

STATE = "/run/rp5deck/state.json"
DSI_H = 1080

# Import sway_outputs for parsing (same directory as st.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sway_outputs import get_output_rect  # noqa: E402


def get_dsi_y():
    """Get DSI-1's y position from swaymsg -t get_outputs.

    Raises:
        RuntimeError: if swaymsg fails, returns invalid JSON, or DSI-1 is not found.

    Note: Does not cache; layout changes when the add-on is plugged/unplugged.
    """
    try:
        output = subprocess.check_output(["swaymsg", "-t", "get_outputs"],
                                         text=True, timeout=2)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"swaymsg failed: {e}") from e
    except subprocess.TimeoutExpired:
        raise RuntimeError("swaymsg timed out") from None
    except OSError as e:
        raise RuntimeError(f"swaymsg not found: {e}") from e

    try:
        outputs = json.loads(output)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"swaymsg returned invalid JSON: {e}") from e

    try:
        rect = get_output_rect(outputs, "DSI-1")
        return rect.get("y", 0)
    except ValueError as e:
        raise RuntimeError(f"DSI-1 layout error: {e}") from e


def state():
    with open(STATE) as f:
        return json.load(f)


def origin(s):
    # The surface is anchored to DSI-1's bottom edge in both FULL and BAR.
    return get_dsi_y() + DSI_H - s["surface"]["size"][1]


def walk(n, out=None):
    if n.get("type") == "output":
        out = n.get("name")
    yield n, out
    for k in ("nodes", "floating_nodes"):
        for c in n.get(k) or []:
            yield from walk(c, out)


def count(path, rgb, y0=None, y1=None):
    b = open(path, "rb").read()
    parts, i = [], 0
    while len(parts) < 4:
        while b[i:i + 1].isspace():
            i += 1
        j = i
        while not b[j:j + 1].isspace():
            j += 1
        parts.append(b[i:j])
        i = j
    i += 1
    assert parts[0] == b"P6", parts[0]
    w, h = int(parts[1]), int(parts[2])
    data = memoryview(b)[i:]
    y0 = 0 if y0 is None else y0
    y1 = h if y1 is None else y1
    hit = tot = 0
    for y in range(y0, y1, 2):
        row = y * w * 3
        for x in range(0, w, 2):
            o = row + x * 3
            tot += 1
            if abs(data[o] - rgb[0]) <= 3 and abs(data[o + 1] - rgb[1]) <= 3 \
                    and abs(data[o + 2] - rgb[2]) <= 3:
                hit += 1
    return "%dx%d rows %d-%d match=%d/%d (%.1f%%)" % (w, h, y0, y1, hit, tot, 100.0 * hit / max(1, tot))


def main(a):
    cmd = a[0]
    if cmd == "get":
        v = state()
        for k in a[1].split("."):
            v = v.get(k) if isinstance(v, dict) else None
        print(json.dumps(v))
    elif cmd == "target":
        s = state()
        x, y, w, h = s["targets"][a[1]]
        print("%d %d" % (x + w // 2, origin(s) + y + h // 2))
    elif cmd == "track":
        s = state()
        x0, x1, yc = s["targets"][a[1] + ".track"]
        v = float(a[2])
        print("%d %d" % (round(x0 + v * (x1 - x0)), origin(s) + yc))
    elif cmd == "wait":
        expr, timeout = a[1], float(a[2])
        t0 = time.monotonic()
        while True:
            try:
                s = state()
                if eval(expr, {"s": s}):
                    print("%.2f" % (time.monotonic() - t0))
                    return 0
            except Exception:
                pass
            if time.monotonic() - t0 > timeout:
                print("TIMEOUT after %.1fs" % timeout)
                return 1
            time.sleep(0.05)
    elif cmd == "count":
        rgb = [int(v) for v in a[2].split(",")]
        y0 = int(a[3]) if len(a) > 3 else None
        y1 = int(a[4]) if len(a) > 4 else None
        print(count(a[1], rgb, y0, y1))
    elif cmd == "mode":
        import os
        sys.path.insert(0, os.environ["APP"])
        import sway_ipc
        print("%s (%s)" % sway_ipc.compute_mode(json.load(sys.stdin)))
    elif cmd in ("focus", "esid", "app", "apps"):
        t = json.load(sys.stdin)
        for n, out in walk(t):
            if cmd == "focus" and n.get("focused"):
                print("%s %s %s" % (n.get("id"), n.get("type"), n.get("app_id") or n.get("name")))
                return 0
            if cmd == "esid" and n.get("app_id") == "emulationstation":
                print(n["id"])
                return 0
            if cmd == "app" and n.get("app_id") == a[1]:
                r = n["rect"]
                print("%s %d %d %d %d" % (out, r["x"], r["y"], r["width"], r["height"]))
                return 0
            if cmd == "apps" and str(n.get("app_id") or "").startswith(a[1]):
                print(n.get("app_id"))
        if cmd in ("app",):
            print("absent")
        if cmd in ("focus", "esid"):
            print("none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
