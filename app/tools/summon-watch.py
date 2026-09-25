#!/usr/bin/env python3
"""summon-watch.py - device-side verification tool for rp5deck's summon
button reader (B11a). Watches every configured hardware-button binding
(btn_c_paddle, btn_z_paddle, btn_back_f1) concurrently, by NAME, and prints:

  - a SUMMON line for each debounced press edge (what summon.py's reader
    itself would act on), and
  - a raw line for EVERY EV_KEY event it sees on a watched device, matched
    or not (autorepeat and release included) - so a paddle or the Back
    button can be diagnosed even if it turns out to send a code other than
    the one summon.py currently watches for (see summon.py's module
    docstring for why that is a real, currently-open possibility for the
    paddles specifically).

It never grabs anything (no EVIOCGRAB, ever) and never writes to a device.

Usage:
    python3 summon-watch.py [--seconds N] [--bindings b1,b2,...] [--quiet-raw]

Exits cleanly after N seconds (default 30) or on Ctrl-C, printing a summary
line per binding. Zero presses during the run is a PASS for "the reader
starts, finds its devices, and idles cleanly" - it is not itself proof that
a press works; that needs a human at the device (see the run's own summary).
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import summon  # noqa: E402

ALL_BINDINGS = ("btn_c_paddle", "btn_z_paddle", "btn_back_f1")


def _fmt_code(code):
    names = {
        summon.KEY_F1_CODE: "KEY_F1",
        summon.BTN_C_PADDLE_CODE: "BTN_TRIGGER_HAPPY4",
        summon.BTN_Z_PADDLE_CODE: "BTN_TRIGGER_HAPPY3",
    }
    return "%s (0x%03x/%d)" % (names.get(code, "?"), code, code)


class Watch:
    def __init__(self, binding, start_t, quiet_raw):
        self.binding = binding
        self.start_t = start_t
        self.quiet_raw = quiet_raw
        self.summon_count = 0
        self.raw_count = 0
        target = summon.BINDING_TARGETS.get(binding)
        self.device_name, self.code = target if target else (None, None)
        self.path = summon.find_event_path(self.device_name) if self.device_name else None
        self.reader = summon.SummonButtonReader(
            binding, on_summon=self._on_summon, on_any_key=self._on_any_key,
            log=self._log)
        self.thread = threading.Thread(target=self.reader.run, daemon=True,
                                        name="summon-watch-%s" % binding)

    def _elapsed(self):
        return time.monotonic() - self.start_t

    def _log(self, msg):
        print("%7.3fs  [%s] %s" % (self._elapsed(), self.binding, msg))

    def _on_summon(self, ev):
        self.summon_count += 1
        print('%7.3fs  SUMMON    binding=%-13s device="%s" path=%s code=%s value=%d'
              % (self._elapsed(), ev.binding, ev.device_name, ev.device_path,
                 _fmt_code(ev.code), ev.value))

    def _on_any_key(self, ev, path, device_name):
        self.raw_count += 1
        if self.quiet_raw:
            return
        tag = "raw(match)" if ev.code == self.code else "raw"
        print('%7.3fs  %-9s binding=%-13s device="%s" path=%s code=0x%03x/%d value=%d'
              % (self._elapsed(), tag, self.binding, device_name, path, ev.code, ev.code, ev.value))

    def report_resolution(self):
        if self.device_name is None:
            print("  %-13s binding not recognised" % self.binding)
            return
        found = self.path or "NOT FOUND"
        print('  %-13s device="%s" code=%s -> %s'
              % (self.binding, self.device_name, _fmt_code(self.code), found))

    def start(self):
        self.thread.start()

    def stop_and_join(self, timeout=3.0):
        self.reader.stop()
        self.thread.join(timeout)
        self.reader.close()
        return not self.thread.is_alive()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=30.0,
                     help="how long to watch before exiting (default 30)")
    ap.add_argument("--bindings", default=",".join(ALL_BINDINGS),
                     help="comma-separated binding names to watch (default: all three)")
    ap.add_argument("--quiet-raw", action="store_true",
                     help="only print SUMMON lines, not every raw key event")
    args = ap.parse_args(argv)

    bindings = [b.strip() for b in args.bindings.split(",") if b.strip()]
    unknown = [b for b in bindings if b not in ALL_BINDINGS]
    if unknown:
        print("unknown binding(s): %s (valid: %s)" % (", ".join(unknown), ", ".join(ALL_BINDINGS)))
        return 2

    start_t = time.monotonic()
    watches = [Watch(b, start_t, args.quiet_raw) for b in bindings]

    print("summon-watch: resolving %d binding(s) by NAME (never by a fixed event number)"
          % len(watches))
    for w in watches:
        w.report_resolution()
    print("watching for %.0fs - press the paddles / Back now. Ctrl-C also ends the run cleanly.\n"
          % args.seconds)

    for w in watches:
        w.start()

    interrupted = False
    try:
        deadline = start_t + args.seconds
        while time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted - stopping")

    print("\nstopping readers...")
    clean = True
    for w in watches:
        ok = w.stop_and_join()
        clean = clean and ok
        if not ok:
            print("  WARNING: %s reader thread did not stop within the timeout" % w.binding)

    elapsed = time.monotonic() - start_t
    print("\n--- summary after %.1fs%s ---" % (elapsed, " (interrupted)" if interrupted else ""))
    total_summon = 0
    for w in watches:
        print("  %-13s summon events=%d  raw key events=%d  resolved=%s"
              % (w.binding, w.summon_count, w.raw_count, "yes" if w.path else "NO"))
        total_summon += w.summon_count
    print("total summon events: %d" % total_summon)
    print("clean shutdown: %s" % ("yes" if clean else "NO - see WARNING above"))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
