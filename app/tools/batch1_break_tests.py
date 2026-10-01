#!/usr/bin/env python3
"""Batch 1 mutation check: every mutation must turn its suite red. Works on a temp copy
of rp5deck + rocknix-config (the real files are never edited), runs with -B."""
import os
import shutil
import subprocess
import sys
import tempfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(APP))
D092 = "dual-screen-layout-and-power"

# (file relative to the temp repo, old, new, test module)
MUTATIONS = [
    ("bottom-screen-app/rp5deck/screen_idle.py", "DIM_FRACTION = 0.15", "DIM_FRACTION = 1.0",
     "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/screen_idle.py", 'POWER_OFF_STYLES = ("black", "random video", "slideshow")',
     'POWER_OFF_STYLES = ("black",)', "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/screen_idle.py",
     "        if self.active is not None or not self.is_docked():",
     "        if self.active is not None:", "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/screen_idle.py",
     "        self._save(st)  # record first so a crash can be undone\n",
     "", "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/screen_idle.py", '    return m.group(1) if m else "dim"',
     '    return m.group(1) if m else "black"', "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/screen_idle.py",
     "        if kind in (esevents.SCREENSAVER_STOP, esevents.WAKE):",
     "        if kind == esevents.SCREENSAVER_STOP:", "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/screen_idle.py", "        if cur <= 0:\n            return False",
     "        if cur < 0:\n            return False", "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/main.py",
     "        if self.screen_idle is not None and self.screen_idle.on_touch():",
     "        if self.screen_idle is not None and self.screen_idle.on_touch() and False:",
     "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/main.py",
     "                self.screen_idle.on_es_event(ev.kind)", "                pass",
     "tests.test_screen_idle"),
    ("bottom-screen-app/rp5deck/main.py", '        if self.output != screen_map.CURRENT.bottom:\n            return False\n',
     "", "tests.test_screen_idle"),
    # notepad (Notes tab)
    ("bottom-screen-app/rp5deck/notepad.py", "        if math.hypot(x - lx, y - ly) < MIN_STEP:\n            return\n",
     "", "tests.test_notepad"),
    ("bottom-screen-app/rp5deck/notepad.py", "        if not self.clear_armed:", "        if False:",
     "tests.test_notepad"),
    ("bottom-screen-app/rp5deck/notepad.py", "                os.replace(self.path, self.path + \".bad\")",
     "                pass", "tests.test_notepad"),
    ("bottom-screen-app/rp5deck/notepad.py", "        if len(t) + len(s) > MAX_TEXT:", "        if False:",
     "tests.test_notepad"),
    ("bottom-screen-app/rp5deck/notepad.py", "        if self.sheet.mode != \"draw\" or self.pid is not None:",
     "        if self.pid is not None:", "tests.test_notepad"),
    ("bottom-screen-app/rp5deck/ui.py", "        if self.gestures and not getattr(owner, \"owns_drag\", False):",
     "        if self.gestures:", "tests.test_notepad"),
    ("bottom-screen-app/rp5deck/notepad.py", "        return self.sheet.mode == \"draw\"", "        return True",
     "tests.test_notepad"),
    # manual PDF tools
    ("bottom-screen-app/rp5deck/companion.py", "        step = 1 if st.get(\"single\") else 2\n        left = st[\"left\"]",
     "        step = 2\n        left = st[\"left\"]", "tests.test_manual_tools"),
    ("bottom-screen-app/rp5deck/companion.py", "        rh = int(ph * zoom)", "        rh = ph", "tests.test_manual_tools"),
    ("bottom-screen-app/rp5deck/companion.py", "            return max(a1 - hi_edge, min(a0 - lo_edge, off))",
     "            return off", "tests.test_manual_tools"),
    ("bottom-screen-app/rp5deck/manuals.py",
     "    k = min(1.0, (screen_w - g) / float(pages_w) if pages_w else 1.0,\n            screen_h / float(tall) if tall else 1.0)",
     "    k = 1.0", "tests.test_manuals"),
    # brightness
    ("bottom-screen-app/rp5deck/brightness.py", "    pct = max(BOTTOM_MIN_PCT, min(100, int(pct)))",
     "    pct = min(100, int(pct))", "tests.test_brightness"),
    ("bottom-screen-app/rp5deck/brightness.py", "        if pct >= 100:\n            self.stop()\n            return True\n", "",
     "tests.test_brightness"),
    ("bottom-screen-app/rp5deck/main.py", "            if g(\"match_brightness\"):\n                top = brightness.matched_top",
     "            if False:\n                top = brightness.matched_top", "tests.test_brightness"),
    # tile migrations, YouTube focus, overlay HUD, three-row grid
    ("bottom-screen-app/rp5deck/config.py", "    data = _extend_tile_order(data, notes)\n", "", "tests.test_config"),
    ("bottom-screen-app/rp5deck/config.py", "    data = _prune_hidden_tiles(data, notes)\n", "", "tests.test_config"),
    ("bottom-screen-app/rp5deck/focus_guard.py", "YOUTUBE_APP_IDS = (YT_APP_ID, YTAPP_APP_ID)",
     "YOUTUBE_APP_IDS = (YT_APP_ID,)", "tests.test_focus_guard"),
    ("bottom-screen-app/rp5deck/cc_overlay.py", "        self.cc.home.order = [t.name for t in keep]\n", "",
     "tests.test_sw1_merged"),
    ("bottom-screen-app/rp5deck/screens.py",
     "        return HOME_ROWS if len(self.visible_tiles()) <= HOME_ROWS_THREE_AFTER else 3",
     "        return HOME_ROWS", "tests.test_screens"),
    ("rocknix-config/" + D092, "        boot_reattach=waiting\n", "", "tests.test_092_topoff"),
    ("rocknix-config/" + D092, "            if [ \"$reattach_polls\" -ge \"$REATTACH_POLLS\" ]; then\n                boot_reattach=done\n",
     "            if [ \"$reattach_polls\" -ge \"$REATTACH_POLLS\" ]; then\n", "tests.test_092_topoff"),
    ("rocknix-config/" + D092, "        if [ -d \"${PORT}-partner\" ]; then\n            boot_reattach=done\n",
     "        if false; then\n            boot_reattach=done\n", "tests.test_092_topoff"),
    ("rocknix-config/" + D092, "                echo dual > \"$PORT/port_type\" 2>/dev/null\n                log \"top-screen-off: after the nudge",
     "                log \"top-screen-off: after the nudge", "tests.test_092_topoff"),
]


def run(tmp, module):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               SW1_092_PATH=os.path.join(tmp, "rocknix-config", D092),
               TOPOFF_CONTROL_REPO=REPO)       # the control reads an old 092 from git
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", module],
                       cwd=os.path.join(tmp, "bottom-screen-app", "rp5deck"), env=env,
                       capture_output=True, text=True, timeout=1500)
    return r.returncode


def main():
    tmp = tempfile.mkdtemp(prefix="batch1-")
    try:
        shutil.copytree(APP, os.path.join(tmp, "bottom-screen-app", "rp5deck"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "firefox", "screenshots",
                                                      "steam", "proto"))
        os.makedirs(os.path.join(tmp, "rocknix-config"))
        shutil.copy(os.path.join(REPO, "rocknix-config", D092), os.path.join(tmp, "rocknix-config"))
        for m in sorted({m for *_, m in MUTATIONS}):
            if run(tmp, m) != 0:
                print("BASELINE RED:", m)
                return 1
        print("baselines green")
        caught = 0
        for i, (rel, old, new, mod) in enumerate(MUTATIONS, 1):
            p = os.path.join(tmp, rel)
            orig = open(p, encoding="utf-8", newline="").read()
            if orig.count(old) != 1:
                print("  M%-2d ANCHOR count %d: %r" % (i, orig.count(old), old[:60]))
                return 1
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(orig.replace(old, new))
            rc = run(tmp, mod)
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(orig)
            caught += rc != 0
            print("  M%-2d %s  %s: %s" % (i, "caught" if rc else "MISSED", os.path.basename(rel),
                                         old.strip().splitlines()[0][:55]))
        print("%d/%d mutations caught" % (caught, len(MUTATIONS)))
        return 0 if caught == len(MUTATIONS) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
