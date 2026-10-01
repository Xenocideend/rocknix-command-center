#!/usr/bin/env python3
"""perf_profile: caps the GPU for light games and uncaps CPU and GPU for heavy ones.

ROCKNIX's SM8250 400-set_gpu_overclock checks GPU_OC_STATE (never set) instead of
GPU_OC_SPEED, so with gpu-overclock-speed empty no cap is applied and the Adreno runs up to
925 MHz for everything. This applies the cap ROCKNIX meant to, and lifts it (plus the CPU)
while a heavy game runs.

    light  GPU max = the ES "GPU overclock" value if one is set, else 587 MHz (ROCKNIX's
           "0"/off value). CPU left alone (runemu's governor).
    heavy  GPU max = 925 MHz and every cpufreq policy on `performance` (on Wind Waker HD
           that gave +42 % fps for +19 % power).

Heavy is decided every PERIOD_S from /proc. ALWAYS_HEAVY emulators (Wii U, PS3, PS2, Vita,
Xbox, Switch, GC/Wii) are heavy whenever they run. LOAD_GATED processes (box64/FEX/Wine, how
Steam and Windows games run, and the Steam client) are heavy only while they burn at least
LOAD_GATE_CORES of CPU over LOAD_WINDOW_S, so an idle Steam client doesnt hold the uncap.
Leaving heavy waits HOLD_S after the last trigger since loading screens dip.

/storage/perf-mode overrides it: auto (default) | max (always heavy) | saver (never heavy),
reread every poll. State for the Command Center goes to STATE_PATH, and every side effect can
be swapped so tests stay offline.

    python3 perf_profile.py                 run forever (gpu-cap-heavy-game-boost)
    python3 perf_profile.py status          print the live state
    python3 perf_profile.py auto|max|saver  save the override
"""
import json
import logging
import os
import sys
import time

PERIOD_S = 2.0
LOAD_WINDOW_S = 10.0
LOAD_GATE_CORES = 1.0
HOLD_S = 30.0
MODE_PATH = "/storage/perf-mode"
STATE_PATH = "/run/rp5deck-perf.json"
SYSTEM_CFG = "/storage/.config/system/configs/system.cfg"
GPU_DEV = "/sys/devices/platform/soc@0/3d00000.gpu/devfreq/3d00000.gpu"
CPUFREQ = "/sys/devices/system/cpu/cpufreq"
GPU_LIGHT_DEFAULT_HZ = 587000000
GPU_HEAVY_HZ = 925000000
# ROCKNIX's gpu_overclock case list (quirks/platforms/SM8250/bin/gpu_overclock)
GPU_OC_VALUES = {"0": 587000000, "650": 650000000, "700": 700000000, "725": 725000000,
                 "750": 750000000, "800": 800000000, "855": 855000000, "905": 905000000,
                 "925": 925000000}
MODES = ("auto", "max", "saver")

# matched against /proc/PID/comm (15 chars) and argv[0]'s basename
ALWAYS_HEAVY = frozenset((
    "cemu",                                             # Wii U
    "rpcs3", "rpcs3-sa",                                # PS3
    "aethersx2", "pcsx2", "pcsx2-qt", "aethersx2-sa",   # PS2
    "vita3k", "vita3k-sa",                              # Vita
    "xemu",                                             # Xbox
    "eden", "citron", "yuzu", "suyu", "sudachi", "torzu", "ryujinx",   # Switch
    "shadps4",                                          # PS4
    "dolphin-emu", "dolphin-emu-nog",                   # GameCube / Wii (comm is 15 chars)
))
LOAD_GATED = frozenset((
    "box64", "box86", "fexinterpreter", "fexloader",
    "wine", "wine64", "wine-preloader", "wine64-preload", "wineserver",
    "steam", "steamwebhelper",
))
log = logging.getLogger("rp5deck.perf_profile")


def _name_keys(comm, argv0):
    keys = set()
    if comm:
        keys.add(comm.strip().lower())
    if argv0:
        keys.add(os.path.basename(argv0).lower())
    # Windows games under Wine show up as "Game.exe", count them as Wine
    if any(k.endswith(".exe") for k in keys):
        keys.add("wine")
    return keys


def classify(comm, argv0):
    """'always', 'gated' or None for one process."""
    keys = _name_keys(comm, argv0)
    if keys & ALWAYS_HEAVY:
        return "always"
    if keys & LOAD_GATED:
        return "gated"
    return None


def read_procs(proc="/proc"):
    """[(pid, comm, argv0, cpu_ticks)] for every readable process."""
    out = []
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        base = os.path.join(proc, d)
        try:
            with open(os.path.join(base, "stat"), "rb") as f:
                st = f.read().decode("utf-8", "replace")
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
        except OSError:
            continue
        # comm is in parentheses and can have spaces or ')' in it
        l, r = st.find("("), st.rfind(")")
        if l < 0 or r < 0:
            continue
        comm = st[l + 1:r]
        fields = st[r + 2:].split()
        try:
            ticks = int(fields[11]) + int(fields[12])      # utime + stime
        except (IndexError, ValueError):
            continue
        out.append((int(d), comm, argv0, ticks))
    return out


class Detector:
    """Pure decision logic: feed it samples, get heavy True/False."""

    def __init__(self, hz=100, window_s=LOAD_WINDOW_S, gate_cores=LOAD_GATE_CORES,
                 hold_s=HOLD_S):
        self.hz = hz
        self.window_s = window_s
        self.gate_cores = gate_cores
        self.hold_s = hold_s
        self.hist = []            # [(t, {pid: ticks})] for gated processes
        self.last_trigger = None
        self.reason = ""

    def trigger(self, now, procs):
        """The raw trigger for this sample, without the hold: (bool, reason)."""
        gated = {}
        for pid, comm, argv0, ticks in procs:
            c = classify(comm, argv0)
            if c == "always":
                return True, "running: %s" % (comm or os.path.basename(argv0))
            if c == "gated":
                gated[pid] = (ticks, comm)
        self.hist.append((now, {p: t for p, (t, _) in gated.items()}))
        # keep samples back to the window start (one older sample for the delta)
        while len(self.hist) > 2 and self.hist[1][0] <= now - self.window_s:
            self.hist.pop(0)
        if not gated or len(self.hist) < 2:
            return False, ""
        t0, old = self.hist[0]
        dt = now - t0
        if dt < self.window_s * 0.5:
            return False, ""
        used = 0
        for pid, (ticks, _) in gated.items():
            if pid in old:
                used += max(0, ticks - old[pid])
        cores = used / float(self.hz) / dt
        if cores >= self.gate_cores:
            names = sorted({c for _, c in gated.values()})
            return True, "load %.1f cores: %s" % (cores, ",".join(names)[:60])
        return False, ""

    def step(self, now, procs, mode="auto"):
        hit, why = self.trigger(now, procs)
        if mode == "max":
            self.reason = "perf-mode max"
            return True
        if mode == "saver":
            self.reason = "perf-mode saver"
            return False
        if hit:
            self.last_trigger = now
            self.reason = why
            return True
        if self.last_trigger is not None and now - self.last_trigger < self.hold_s:
            self.reason = "holding %.0fs after last trigger" % (self.hold_s - (now - self.last_trigger))
            return True
        self.reason = "no heavy game"
        return False


def read_mode(path=MODE_PATH):
    try:
        with open(path) as f:
            m = f.read().strip().lower()
    except OSError:
        return "auto"
    return m if m in MODES else "auto"


def light_gpu_hz(cfg_path=SYSTEM_CFG):
    """The ES GPU overclock choice if it's one of ROCKNIX's values, else 587 MHz. Empty or "0" both
    mean off.
    """
    try:
        with open(cfg_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("gpu-overclock-speed="):
                    v = line.split("=", 1)[1].strip()
                    return GPU_OC_VALUES.get(v, GPU_LIGHT_DEFAULT_HZ)
    except OSError:
        pass
    return GPU_LIGHT_DEFAULT_HZ


class Hardware:
    """sysfs writes, paths can be swapped for tests"""

    def __init__(self, gpu_dev=GPU_DEV, cpufreq=CPUFREQ):
        self.gpu_dev = gpu_dev
        self.cpufreq = cpufreq
        self.saved_govs = None

    @staticmethod
    def _w(path, value):
        with open(path, "w") as f:
            f.write(str(value))

    @staticmethod
    def _r(path):
        with open(path) as f:
            return f.read().strip()

    def policies(self):
        try:
            return sorted(p for p in os.listdir(self.cpufreq) if p.startswith("policy"))
        except OSError:
            return []

    def gpu_max(self):
        try:
            return int(self._r(os.path.join(self.gpu_dev, "max_freq")))
        except (OSError, ValueError):
            return None

    def set_gpu_max(self, hz):
        if self.gpu_max() != hz:
            self._w(os.path.join(self.gpu_dev, "max_freq"), hz)

    def governors(self):
        g = {}
        for p in self.policies():
            try:
                g[p] = self._r(os.path.join(self.cpufreq, p, "scaling_governor"))
            except OSError:
                pass
        return g

    def cpu_heavy(self):
        """All policies to performance, remembering what they were (once)."""
        if self.saved_govs is None:
            self.saved_govs = self.governors()
        for p in self.policies():
            gp = os.path.join(self.cpufreq, p, "scaling_governor")
            if self._r(gp) != "performance":
                self._w(gp, "performance")

    def cpu_restore(self):
        """Puts back the governors seen on entering heavy, but only on policies still on `performance`.
        If runemu or you changed one since, that wins.
        """
        if self.saved_govs is None:
            return
        for p, gov in self.saved_govs.items():
            gp = os.path.join(self.cpufreq, p, "scaling_governor")
            try:
                if self._r(gp) == "performance" and gov != "performance":
                    self._w(gp, gov)
            except OSError:
                pass
        self.saved_govs = None


class Profile:
    """Detector + hardware, one apply per step."""

    def __init__(self, hw=None, det=None, mode_path=MODE_PATH, cfg_path=SYSTEM_CFG,
                 state_path=STATE_PATH, clock=time.monotonic, procs=read_procs):
        self.hw = hw or Hardware()
        self.det = det or Detector(hz=os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100)
        self.mode_path = mode_path
        self.cfg_path = cfg_path
        self.state_path = state_path
        self.clock = clock
        self.procs = procs
        self.heavy = None

    def step(self):
        mode = read_mode(self.mode_path)
        heavy = self.det.step(self.clock(), self.procs(), mode)
        light_hz = light_gpu_hz(self.cfg_path)
        # GPU and CPU apply separately, so a missing devfreq node cant leave the CPU unboosted (or
        # unrestored) and the other way round
        for what, fn in (("gpu", lambda: self.hw.set_gpu_max(GPU_HEAVY_HZ if heavy else light_hz)),
                         ("cpu", self.hw.cpu_heavy if heavy else self.hw.cpu_restore)):
            try:
                fn()
            except OSError as e:
                log.warning("apply %s failed: %s", what, e)
        if heavy != self.heavy:
            log.info("%s (%s) gpu_max=%s govs=%s", "HEAVY" if heavy else "light",
                     self.det.reason, self.hw.gpu_max(), self.hw.governors())
            self.heavy = heavy
        self.write_state(mode, heavy, light_hz)
        return heavy

    def write_state(self, mode, heavy, light_hz):
        st = {"mode": mode, "heavy": heavy, "reason": self.det.reason,
              "gpu_max": self.hw.gpu_max(), "gpu_light": light_hz, "gpu_heavy": GPU_HEAVY_HZ,
              "governors": self.hw.governors(), "t": time.time()}
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def shutdown(self):
        """On exit leaves the device light (capped GPU, governors put back)."""
        try:
            self.hw.set_gpu_max(light_gpu_hz(self.cfg_path))
            self.hw.cpu_restore()
        except OSError:
            pass


def main(argv):
    if len(argv) > 1:
        a = argv[1].lower()
        if a in MODES:
            with open(MODE_PATH, "w") as f:
                f.write(a + "\n")
            print("perf-mode = %s (applied within %.0f s)" % (a, PERIOD_S))
            return 0
        if a == "status":
            try:
                with open(STATE_PATH) as f:
                    print(json.dumps(json.load(f), indent=1))
            except OSError:
                print("not running (no %s)" % STATE_PATH)
            return 0
        print(__doc__)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import signal
    prof = Profile()

    def _stop(*_):
        prof.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("start: light GPU %d MHz, heavy %d MHz", light_gpu_hz() // 1000000,
             GPU_HEAVY_HZ // 1000000)
    while True:
        prof.step()
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
