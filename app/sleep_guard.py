#!/usr/bin/env python3
"""sleep_guard: no suspend while the charger is stuck.

A power key suspend with a healthy charger slept and resumed fine. The same press a couple of
minutes later, while the pm8150b was stuck (APSD "not ready" loop, ICL_STATUS 0, battery
draining), reset the handheld at "PM: suspend entry (s2idle)", and it cold booted with the
charger still in, which keeps it stuck. So while stuck, sleep cant happen at all.

Detection is charge_stuck's, the same evidence the Command Center's warning uses (its Sampler
only reads files). While stuck there are two separate locks:
  1. a logind block inhibitor (`systemd-inhibit --what=sleep --mode=block`), so logind refuses
     its own power key and lid suspend (logind.conf here has HandlePowerKey=suspend and no
     PowerKeyIgnoreInhibited)
  2. FLAG_PATH holding this process's pid, which charge-sleep-veto.service
     (RequiredBy=sleep.target) checks. It fails while the flag names a live pid, so
     sleep.target and any suspend with it is aborted, even a root `systemctl suspend` that would
     override an inhibitor.
The veto checks the pid, so a guard that died never blocks sleep forever, and the inhibitor
child gets PDEATHSIG and its own process group for the same reason.

    python3 sleep_guard.py                  run forever (no-sleep-while-charger-stuck)
    python3 sleep_guard.py --simulate-stuck hold both locks as if stuck (device tests)
"""
import collections
import ctypes
import logging
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import charge_stuck  # noqa: E402

PERIOD_S = 10.0
FLAG_PATH = "/run/rp5deck-charge-stuck"
INHIBIT_CMD = ["systemd-inhibit", "--what=sleep", "--mode=block", "--who=rp5deck sleep_guard",
               "--why=Charger stuck: suspending now resets the RP5", "sleep", "2147483647"]
log = logging.getLogger("rp5deck.sleep_guard")


def _pdeathsig():
    """preexec_fn, the inhibitor dies with the guard (PR_SET_PDEATHSIG = 1)."""
    try:
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    except Exception:
        pass
    os.setpgid(0, 0)


class Locks:
    """The two locks. Every side effect can be swapped so tests stay offline."""

    def __init__(self, flag_path=FLAG_PATH, spawn=None, pid=None):
        self.flag_path = flag_path
        self.spawn = spawn or (lambda: subprocess.Popen(
            INHIBIT_CMD, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, preexec_fn=_pdeathsig))
        self.pid = pid if pid is not None else os.getpid()
        self.proc = None

    @property
    def held(self):
        return self.proc is not None

    def take(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        self.proc = self.spawn()
        tmp = self.flag_path + ".tmp"
        with open(tmp, "w") as f:
            f.write("%d\n" % self.pid)
        os.replace(tmp, self.flag_path)

    def release(self):
        try:
            os.unlink(self.flag_path)
        except FileNotFoundError:
            pass
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)       # the inhibitor and its sleep child
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass


class Guard:
    def __init__(self, sampler, locks, window_s=charge_stuck.WINDOW_S):
        self.sampler = sampler
        self.locks = locks
        self.window_s = window_s
        self.samples = collections.deque()

    def step(self, forced=None):
        """One poll -> (stuck, reason). forced True/False overrides detection."""
        if forced is None:
            s = self.sampler.sample()
            self.samples.append(s)
            while self.samples and s["t"] - self.samples[0]["t"] > self.window_s * 2:
                self.samples.popleft()
            stuck, reason = charge_stuck.evaluate(list(self.samples), window_s=self.window_s)
        else:
            stuck, reason = forced, "forced"
        if stuck and not self.locks.held:
            self.locks.take()
            log.warning("charger stuck (%s) - sleep blocked", reason)
        elif stuck and self.locks.proc is not None and self.locks.proc.poll() is not None:
            log.warning("inhibitor exited - taking it again")
            self.locks.proc = None
            self.locks.take()
        elif not stuck and self.locks.held:
            self.locks.release()
            log.warning("charger fine again (%s) - sleep allowed", reason)
        return stuck, reason


def _exit_on_signal(signum, _frame):
    raise SystemExit(128 + signum)


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, _exit_on_signal)
    simulate = "--simulate-stuck" in argv
    guard = Guard(charge_stuck.Sampler(), Locks())
    log.warning("sleep_guard starting (pid %d)%s", os.getpid(), " SIMULATING STUCK" if simulate else "")
    try:
        while True:
            guard.step(forced=True if simulate else None)
            time.sleep(PERIOD_S)
    finally:
        guard.locks.release()
        log.warning("sleep_guard exiting - locks released")


if __name__ == "__main__":
    main(sys.argv[1:])
