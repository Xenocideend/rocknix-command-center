#!/usr/bin/env python3
"""ab_tune - same-session A/B of GPU cap, CPU governor and big-core pinning
while one game runs.

    python3 ab_tune.py <outdir> [settle_s] [sample_s] [--phases LIST] [--pin PATTERN]
    python3 ab_tune.py --check          one perfmon second: is the fps readable?

--phases is a comma list of phase kinds (default: the 25 Sep GPU/governor run,
base,gpu750,gpu587,base,cpuperf,base). Kinds:
    base     GPU cap 925 MHz, CPU governor as found, affinity as found
    gpu750   GPU cap 750            gpu587   GPU cap 587 (ROCKNIX's "no overclock")
    cpuperf  CPU governor performance on every policy
    big      every thread of the --pin process pinned to cores 4-7 (the big cores)
    bigperf  big + cpuperf
Repeated kinds are numbered (base1, base2, ...): repeat base between the others
so drift in the scene or temperature shows up. --pin is a `pgrep -f` pattern
(e.g. "^cemu -g"), needed by big/bigperf.

Each phase: apply, settle, sample with perfmon.py; the summary line has fps,
battery W (meaningless on external power - a warning says so), GPU MHz,
per-cluster average CPU MHz and temperatures. Every change - GPU cap,
governors, and each thread's original CPU affinity - is restored in a
finally block that SIGTERM/SIGHUP also reach.
"""
import argparse
import csv
import glob
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PERFMON = os.path.join(HERE, "perfmon.py")
GPU = "/sys/class/devfreq/3d00000.gpu/max_freq"
POLICIES = sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*"))
BIG = {4, 5, 6, 7}
KINDS = {                # kind: (gpu Hz, governor or None = as found, pin big cores)
    "base": (925000000, None, False),
    "gpu750": (750000000, None, False),
    "gpu587": (587000000, None, False),
    "cpuperf": (925000000, "performance", False),
    "big": (925000000, None, True),
    "bigperf": (925000000, "performance", True),
}
DEFAULT_PHASES = "base,gpu750,gpu587,base,cpuperf,base"


def rd(p):
    with open(p) as f:
        return f.read().strip()


def wr(p, v):
    with open(p, "w") as f:
        f.write(str(v))
    got = rd(p)
    if got != str(v):
        raise RuntimeError("%s: wrote %s, reads back %s" % (p, v, got))


def set_gov(g):
    for pol in POLICIES:
        wr(pol + "/scaling_governor", g)


def external_power():
    """Names of power supplies reporting online (charger, USB-C partner), else ''.
    The battery's own status is not enough: at the charge limit it reads
    'Not charging' or 'Full' with ~0 A, which looks like a tiny load."""
    on = []
    for d in glob.glob("/sys/class/power_supply/*"):
        name = os.path.basename(d)
        if name == "battery" or "controller" in name:
            continue
        try:
            if int(rd(d + "/online") or 0) != 0:
                on.append(name)
        except (OSError, ValueError):
            pass
    return ", ".join(on)


def find_pid(pattern):
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    pids = [int(x) for x in r.stdout.split() if int(x) != os.getpid()]
    return pids[0] if pids else None


def threads(pid):
    """Thread ids of pid; [] once it has exited (the game quit mid-run)."""
    try:
        return [int(t) for t in os.listdir("/proc/%d/task" % pid)]
    except FileNotFoundError:
        return []


class Affinity:
    """Saves each thread's CPU mask once; pins to BIG or puts them back."""

    def __init__(self, pid):
        self.pid = pid
        self.proc_mask = os.sched_getaffinity(pid)
        self.saved = {}
        for t in threads(pid):
            try:
                self.saved[t] = os.sched_getaffinity(t)
            except OSError:
                pass

    def pin_big(self):
        n = 0
        for t in threads(self.pid):
            try:
                self.saved.setdefault(t, os.sched_getaffinity(t))
                os.sched_setaffinity(t, BIG)
                n += 1
            except OSError:
                pass            # thread exited between listdir and the call
        return n

    def restore(self):
        n = 0
        for t in threads(self.pid) if os.path.isdir("/proc/%d" % self.pid) else []:
            try:
                os.sched_setaffinity(t, self.saved.get(t, self.proc_mask))
                n += 1
            except OSError:
                pass
        return n

    def pinned_now(self):
        """(threads on exactly BIG, all threads) right now."""
        on, total = 0, 0
        for t in threads(self.pid):
            try:
                total += 1
                on += os.sched_getaffinity(t) == BIG
            except OSError:
                pass
        return on, total


def summarise(path):
    rows = list(csv.DictReader(open(path)))
    fps = [float(r["fps"]) for r in rows if r["fps"]]
    pw = [float(r["power_w"]) for r in rows if r["power_w"]]
    gm = [int(r["gpu_mhz"]) for r in rows]
    tm = [float(r["temp_max"]) for r in rows]
    clus = {}
    for r in rows:
        for part in r["cpu"].split("|"):
            pol, _gov, f = part.split(":")
            clus.setdefault(pol, []).append(int(f.split("/")[0]))
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    return {"n": len(rows), "fps_n": len(fps), "fps": avg(fps), "fps_min": min(fps) if fps else float("nan"),
            "w": avg(pw), "charging": any(p < 0 for p in pw), "gpu": avg(gm), "tmax": max(tm) if tm else 0,
            "tavg": avg(tm), "clusters": {p: avg(v) for p, v in sorted(clus.items())}}


def _exit_on_signal(signum, _frame):
    # SIGTERM/SIGHUP skip `finally` by default; make them unwind so the
    # original GPU cap, governors and affinities are always restored.
    raise SystemExit(128 + signum)


def parse_phases(spec):
    kinds = [k.strip() for k in spec.split(",") if k.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise SystemExit("unknown phase kind(s): %s (known: %s)" % (bad, ", ".join(KINDS)))
    seen, out = {}, []
    for k in kinds:
        seen[k] = seen.get(k, 0) + 1
        out.append(("%s%d" % (k, seen[k]) if kinds.count(k) > 1 else k, k))
    return out


def main():
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, _exit_on_signal)
    if sys.argv[1:] == ["--check"]:
        subprocess.run([sys.executable, PERFMON, "2", "/tmp/ab-check.csv", "check"], check=True)
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("settle", nargs="?", type=int, default=10)
    ap.add_argument("sample", nargs="?", type=int, default=60)
    ap.add_argument("--phases", default=DEFAULT_PHASES)
    ap.add_argument("--pin")
    a = ap.parse_args()
    phases = parse_phases(a.phases)
    aff = None
    if any(KINDS[k][2] for _n, k in phases):
        if not a.pin:
            raise SystemExit("big/bigperf need --pin PATTERN")
        pid = find_pid(a.pin)
        if not pid:
            raise SystemExit("no process matches %r" % a.pin)
        aff = Affinity(pid)
    os.makedirs(a.outdir, exist_ok=True)
    orig_gpu = rd(GPU)
    orig_gov = {pol: rd(pol + "/scaling_governor") for pol in POLICIES}
    print("start %s: gpu max %s, governors %s%s" % (
        time.strftime("%H:%M:%S"), orig_gpu, orig_gov,
        "" if not aff else ", pin pid %d (%d threads, mask %s)" % (
            aff.pid, len(aff.saved), sorted(aff.proc_mask))), flush=True)
    ext = external_power()
    if ext:
        print("WARNING: external power present (%s) - watts are NOT the game's draw" % ext, flush=True)
    try:
        for name, kind in phases:
            gpu, gov, pin = KINDS[kind]
            wr(GPU, gpu)
            if gov:
                set_gov(gov)
            else:
                for pol, g in orig_gov.items():
                    wr(pol + "/scaling_governor", g)
            if aff:
                aff.pin_big() if pin else aff.restore()
            time.sleep(a.settle)
            if aff and pin:
                aff.pin_big()           # catch threads started during the settle
            path = os.path.join(a.outdir, name + ".csv")
            subprocess.run([sys.executable, PERFMON, str(a.sample), path, name],
                           check=True, stdout=subprocess.DEVNULL)
            s = summarise(path)
            pinned = ""
            if aff:
                on, total = aff.pinned_now()
                pinned = "  big-pinned %d/%d threads" % (on, total)
            print("%s %-9s gpu<=%d %-11s fps %.1f (min %.1f, %d/%d)  %.2f W%s  gpu %d MHz  cpu %s  temp %.1f/%.1f%s" % (
                time.strftime("%H:%M:%S"), name, gpu // 1000000, gov or "as-found", s["fps"], s["fps_min"],
                s["fps_n"], s["n"], s["w"], " CHARGING" if s["charging"] else "", s["gpu"],
                "/".join("%d" % v for v in s["clusters"].values()), s["tavg"], s["tmax"], pinned), flush=True)
    finally:
        wr(GPU, orig_gpu)
        for pol, g in orig_gov.items():
            wr(pol + "/scaling_governor", g)
        restored = aff.restore() if aff else 0
        print("restored %s: gpu max %s, governors %s%s" % (
            time.strftime("%H:%M:%S"), rd(GPU), {p: rd(p + "/scaling_governor") for p in POLICIES},
            "" if not aff else ", affinity back on %d threads (big-pinned now %d/%d)" % (
                (restored,) + aff.pinned_now())), flush=True)


if __name__ == "__main__":
    main()
