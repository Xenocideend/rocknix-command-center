#!/usr/bin/env python3
"""perfmon - read-only performance sampler for per-emulator tuning (test day).

Every second: CPU governor/cur/max per cpufreq policy, GPU governor/freq
(devfreq), the hottest thermal zones, battery power, fan (if exposed), and
an FPS figure parsed from the focused window title when the emulator shows
one (Cemu "FPS: 30.03", Azahar/melonDS "[60/60]"). Writes a CSV and prints a
summary (averages, maxima, time at max clock, a throttling hint).

    python3 perfmon.py <seconds> <out.csv> [label]
Changes nothing. Safe to run while a game plays.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time


def rd(p, default=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return default


def cpu():
    out = []
    for pol in sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*")):
        out.append((os.path.basename(pol), rd(pol + "/scaling_governor"),
                    int(rd(pol + "/scaling_cur_freq", "0") or 0) // 1000,
                    int(rd(pol + "/scaling_max_freq", "0") or 0) // 1000,
                    int(rd(pol + "/cpuinfo_max_freq", "0") or 0) // 1000))
    return out


def gpu():
    for d in glob.glob("/sys/class/devfreq/*"):
        name = os.path.basename(d)
        if "gpu" in name or "kgsl" in name or "3d00000" in name:
            return (name, rd(d + "/governor"), int(rd(d + "/cur_freq", "0") or 0) // 1000000,
                    int(rd(d + "/max_freq", "0") or 0) // 1000000)
    return ("?", "?", 0, 0)


def temps():
    t = []
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        v = rd(z + "/temp")
        if v.lstrip("-").isdigit():
            t.append((int(v) / 1000.0, rd(z + "/type")))
    t.sort(reverse=True)
    return t[:3]


def power_w():
    """Battery power in W: positive = the device DRAWING from the battery,
    negative = charging (so a plugged-in run is never mistaken for a load;
    test day: an idle run on the charger read 7.7 W 'draw')."""
    c = rd("/sys/class/power_supply/battery/current_now")
    v = rd("/sys/class/power_supply/battery/voltage_now")
    st = rd("/sys/class/power_supply/battery/status")
    try:
        w = abs(int(c)) * int(v) / 1e12
    except ValueError:
        return None
    return -w if st == "Charging" else w


def fan():
    for p in glob.glob("/sys/class/hwmon/hwmon*/pwm1"):
        return rd(p)
    return ""


FPS_RES = [re.compile(r"FPS:\s*([0-9.]+)"), re.compile(r"\[(\d+)/\d+\]")]


def title_fps():
    env = dict(os.environ)
    socks = glob.glob("/run/0-runtime-dir/sway-ipc.*.sock")
    if not socks:
        return None, ""
    env["SWAYSOCK"] = socks[0]
    try:
        tree = json.loads(subprocess.run(["swaymsg", "-t", "get_tree", "-r"], env=env,
                                         capture_output=True, text=True, timeout=2).stdout)
    except Exception:
        return None, ""
    stack = [tree]
    while stack:
        n = stack.pop()
        if n.get("focused") and n.get("name"):
            name = n["name"]
            for rx in FPS_RES:
                m = rx.search(name)
                if m:
                    return float(m.group(1)), name[:60]
            return None, name[:60]
        stack.extend(n.get("nodes", []) + n.get("floating_nodes", []))
    return None, ""


def main():
    secs, out = int(sys.argv[1]), sys.argv[2]
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    rows = []
    with open(out, "w") as f:
        f.write("t,label,cpu,gpu_gov,gpu_mhz,gpu_max,temp_max,temp_zone,power_w,fan,fps,window\n")
        for i in range(secs):
            c, g, t = cpu(), gpu(), temps()
            p, fp = power_w(), title_fps()
            row = {"c": c, "g": g, "t": t, "p": p, "fps": fp[0], "win": fp[1]}
            rows.append(row)
            f.write("%d,%s,%s,%s,%d,%d,%.1f,%s,%s,%s,%s,%s\n" % (
                i, label, "|".join("%s:%s:%d/%d/%d" % x for x in c), g[1], g[2], g[3],
                t[0][0] if t else 0, t[0][1] if t else "", "%.2f" % p if p is not None else "",
                fan(), fp[0] if fp[0] is not None else "", fp[1].replace(",", " ")))
            f.flush()
            time.sleep(1)
    # summary
    print("== perfmon %s: %d samples" % (label, len(rows)))
    if not rows:
        return
    govs = sorted({x[1] for r in rows for x in r["c"]})
    print("cpu governors seen:", govs)
    for idx, pol in enumerate(rows[0]["c"]):
        cur = [r["c"][idx][2] for r in rows if idx < len(r["c"])]
        mx = pol[4]
        at_max = sum(1 for v in cur if v >= mx * 0.97) * 100 // len(cur)
        caps = sorted({r["c"][idx][3] for r in rows if idx < len(r["c"])})
        print("  %s: avg %d MHz, max seen %d / hw %d MHz, %d%% of samples at max; scaling_max seen %s" % (
            pol[0], sum(cur) // len(cur), max(cur), mx, at_max, caps))
    gm = [r["g"][2] for r in rows]
    print("gpu %s: governor %s, avg %d MHz, max seen %d / %d MHz" % (
        rows[0]["g"][0], rows[0]["g"][1], sum(gm) // len(gm), max(gm), rows[0]["g"][3]))
    tm = [r["t"][0][0] for r in rows if r["t"]]
    if tm:
        print("temp: avg %.1f C, max %.1f C (%s)" % (sum(tm) / len(tm), max(tm), rows[-1]["t"][0][1]))
    pw = [r["p"] for r in rows if r["p"] is not None]
    if pw:
        if min(pw) < 0:
            print("battery: CHARGING during the run (%.2f W in) - power draw not measurable; unplug for a load figure" % -min(pw))
        else:
            print("battery draw: avg %.2f W, max %.2f W" % (sum(pw) / len(pw), max(pw)))
    fps = [r["fps"] for r in rows if r["fps"] is not None]
    if fps:
        print("fps (window title): avg %.1f, min %.1f, max %.1f over %d samples" % (
            sum(fps) / len(fps), min(fps), max(fps), len(fps)))
    wins = sorted({r["win"] for r in rows if r["win"]})
    print("focused windows:", wins[:4])


main()
