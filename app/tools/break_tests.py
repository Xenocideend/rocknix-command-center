#!/usr/bin/env python3
"""Prove the offline tests can fail: apply one deliberate bug at a time, run
the relevant test module, record which tests went red, restore the file and
check it is byte-identical to the original. Run from rp5deck/."""
import hashlib
import re
import subprocess
import sys

BREAKS = [
    ("hit-testing: right/bottom edge inclusive (off by one)", "ui.py",
     "return rx - pad <= x < rx + rw + pad and ry - pad <= y < ry + rh + pad",
     "return rx - pad <= x <= rx + rw + pad and ry - pad <= y <= ry + rh + pad",
     "tests.test_ui"),
    ("hit-testing: bottom-most child wins on overlap", "ui.py",
     "for c in reversed(self.children):          # topmost first",
     "for c in self.children:          # BROKEN",
     "tests.test_ui"),
    ("slider: no clamping in value_at", "ui.py",
     "f = clamp01((px - x0) / float(x1 - x0))",
     "f = (px - x0) / float(x1 - x0)",
     "tests.test_ui"),
    # I2 (24 Sep): FX-A rebuilt the slider's release path (a drag commits on
    # release, a cancel / vertical stroke restores), so the old anchor is
    # gone; the same property - a finished drag is reported - on today's code:
    ("slider: a finished drag is never reported on release", "ui.py",
     "            if self.on_release_cb:\n                self.on_release_cb(self.value)",
     "            pass",
     "tests.test_ui"),
    ("modes: floating windows ignored", "sway_ipc.py",
     '    for key in ("nodes", "floating_nodes"):\n        for c in node.get(key) or []:\n            yield from _views(c)',
     '    for key in ("nodes",):\n        for c in node.get(key) or []:\n            yield from _views(c)',
     "tests.test_modes"),
    ("modes: every DSI-1 workspace counted, not just the visible one", "sway_ipc.py",
     "        if cur is not None and ws.get(\"name\") != cur:\n            continue\n",
     "", "tests.test_modes"),
    ("modes: undocked check removed", "sway_ipc.py",
     '    if external not in outs:\n        return HIDDEN, "undocked: %s absent" % external\n',
     "", "tests.test_modes"),
    # I2: F1 replaced the prefix rule with an explicit allow-list
    # (OWN_APP_IDS); the break is now "back to a pure prefix rule".
    ("modes: own-window allow-list becomes a pure rp5deck- prefix rule", "sway_ipc.py",
     "    return aid in OWN_APP_IDS",
     "    return aid.startswith(\"rp5deck-\")",
     "tests.test_modes"),
    ("hud: fdinfo summed per fd instead of per drm-client-id", "hud.py",
     "totals[client] = max(totals.get(client, 0), ns)",
     "totals[client] = totals.get(client, 0) + ns",
     "tests.test_hud"),
    ("device: trailing NUL not stripped", "device.py",
     r'text = raw.split(b"\0", 1)[0].decode("utf-8", "replace")',
     'text = raw.decode("utf-8", "replace")',
     "tests.test_device"),
    ("throttle: pending value flushed on release (would add a set after the commit)", "audioctl.py",
     "    def finish(self):\n        self.pending = None\n",
     "    def finish(self):\n        if self.pending is not None:\n            self._do(self.pending, self.last_sent)\n        self.pending = None\n",
     "tests.test_ui"),
]


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def run(module):
    p = subprocess.run([sys.executable, "-m", "unittest", module], capture_output=True, text=True)
    out = p.stdout + p.stderr
    red = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\w+) \(", out, re.M)))
    summary = [l for l in out.splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
    return p.returncode, red, summary


def main():
    allgood = True
    for title, path, old, new, module in BREAKS:
        orig = open(path, "rb").read()
        before = md5(path)
        text = orig.decode("utf-8")
        n = text.count(old)
        if n != 1:
            print("SETUP ERROR: %r found %d times in %s" % (old[:50], n, path))
            allgood = False
            continue
        open(path, "wb").write(text.replace(old, new).encode("utf-8"))
        try:
            rc, red, summary = run(module)
        finally:
            open(path, "wb").write(orig)
        restored = md5(path) == before
        went_red = rc != 0 and red
        allgood &= bool(went_red) and restored
        print("BREAK  %s  [%s]" % (title, path))
        print("  %s -> %s; red tests: %s" % (module, " / ".join(summary), ", ".join(red) or "NONE"))
        print("  restored byte-identical: %s" % restored)
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       capture_output=True, text=True)
    last = [l for l in (p.stdout + p.stderr).splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
    print("AFTER RESTORE, full suite: %s" % " / ".join(last))
    print("EVERY BREAK WENT RED AND WAS RESTORED: %s" % (allgood and p.returncode == 0))
    return 0 if allgood and p.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
