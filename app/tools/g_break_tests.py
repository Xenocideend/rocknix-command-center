"""Break proofs for G's changes (24 Sep 2026): each fix/guard is undone in
place, its test must go red, then the file is restored byte-identical.
Pattern: tools/td_fix_break_tests.py. Scoped to G's own files only
(config.py, screens.py, main.py, rocknix_keyboard.py) - rgb_leds.py/
rgb_view.py already have their own tools/rg_break_tests.py.

  1. The RG root cause (settings_view.py:297 has no "hex_color" renderer,
     main.py's build_ui() builds the settings sheet unconditionally):
     lights.left put back into config.WIRED - test_settings_view goes red
     the same way it did for every merged suite before the fix (see
     patches/APPLIED.md's "RG (stick lights) + W-screens" section).
  2. screens.py Home._toggle_hidden()'s "home.settings can never be
     hidden" guard removed.
  3. main.py App.on_tile_layout_changed()'s persistence (save_changes)
     removed - the drag/hide/reset UI would still work, but nothing
     survives a restart.
  4. rocknix_keyboard.py's "--hidden" allow-list check removed - it would
     then also signal rp5deck's own osk.py wvkbd-mobintl (Firefox's
     keyboard), not just ROCKNIX's touchkeyboard.service one.
  5. config.py's "tile_set" validator's membership/duplicate check
     removed - hidden_tiles would then accept "home.settings" directly
     (screens.py's own guard is not the only thing stopping that).

Run: python -B tools/g_break_tests.py (Windows or WSL)."""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("config.py",
     '    ("companion", "system_bg_source"),               # companion.py CompanionController (CC4): self._c("system_bg_source") - theme_colour.theme_background_color()/SampleCache\n',
     '    ("companion", "system_bg_source"),               # companion.py CompanionController (CC4): self._c("system_bg_source") - theme_colour.theme_background_color()/SampleCache\n'
     '    ("lights", "left"),                              # BREAK: RG root cause re-introduced\n',
     "tests.test_settings_view"),
    ("screens.py",
     '    def _toggle_hidden(self, name):\n'
     '        if name == "home.settings":            # can never be hidden - see __init__\n'
     '            return\n'
     '        if name in self.hidden:\n',
     '    def _toggle_hidden(self, name):\n'
     '        if name in self.hidden:\n',
     "tests.test_screens.TestEditMode.test_settings_cannot_be_hidden_even_by_direct_call"),
    ("main.py",
     '        if changes:\n'
     '            config.save_changes(changes)\n'
     '        self.state_dirty = True\n',
     '        if changes:\n'
     '            pass                                     # BREAK: persistence removed\n'
     '        self.state_dirty = True\n',
     "tests.test_tile_customisation.TestDragPersists.test_a_drag_drop_writes_tile_order_to_config_json"),
    ("rocknix_keyboard.py",
     '    if "--hidden" not in tokens:\n'
     '        return False\n',
     '',
     "tests.test_rocknix_keyboard.TestFindPid.test_requires_hidden_and_simple_together"),
    ("config.py",
     '    if t == "tile_set":\n'
     '        # Like array_enum\'s membership check, but a SUBSET (order does not\n'
     '        # matter, dropping members is the whole point) - hidden_tiles: any\n'
     '        # key not in field["values"] is refused outright, which is how\n'
     '        # "home.settings" (never in HIDEABLE_TILE_KEYS) can never be stored\n'
     '        # here even by a hand-edited config.json.\n'
     '        if not isinstance(value, list):\n'
     '            return (False, value)\n'
     '        vals = set(field["values"])\n'
     '        seen = set()\n'
     '        out = []\n'
     '        for v in value:\n'
     '            if not isinstance(v, str) or v not in vals or v in seen:\n'
     '                return (False, value)\n'
     '            seen.add(v)\n'
     '            out.append(v)\n'
     '        return (True, out)\n',
     '    if t == "tile_set":\n'
     '        if not isinstance(value, list):\n'
     '            return (False, value)\n'
     '        return (True, list(value))              # BREAK: no membership/dup check\n',
     "tests.test_config.TestTileCustomisationSchema.test_hidden_tiles_refuses_home_settings_even_directly"),
]


def clear_caches():
    for root, dirs, _ in os.walk(APP):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def run(mod):
    clear_caches()
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", mod], cwd=APP,
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr.strip().splitlines() or ["?"])[-1]


ok_all = True
for i, (fn, good, bad, mod) in enumerate(BREAKS, 1):
    path = os.path.join(APP, fn)
    orig = open(path, "rb").read()
    text = orig.decode("utf-8")
    if text.count(good) != 1:
        print("%d ANCHOR MISSING (count=%d) in %s" % (i, text.count(good), fn))
        ok_all = False
        continue
    try:
        open(path, "wb").write(text.replace(good, bad).encode("utf-8"))
        passed_broken, last_b = run(mod)
    finally:
        open(path, "wb").write(orig)
    passed_restored, last_r = run(mod)
    same = open(path, "rb").read() == orig
    proven = (not passed_broken) and passed_restored and same
    ok_all &= proven
    print("%d %s  %-18s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
