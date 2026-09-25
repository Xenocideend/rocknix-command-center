"""Break proofs for Appearance (palettes.py / ui.py's live-theme keys):
each safety property is undone IN PLACE, its test must go red, then the
file is restored byte-identical. Pattern: tools/rg_break_tests.py /
tools/az_rule_break_tests.py. Only the file named in each tuple is ever
touched - never another agent's break-test script, never another agent's
module.

  1. A preset drops a THEME key (_derive_full() stops returning "warn") -
     every non-Default preset routes through it, so this breaks all 13 at
     once. Caught by: every preset defines every THEME key.
  2. A shipped preset's own contrast goes bad (RP5 White's text darkened
     just enough to fail body-text contrast against its own background).
     Caught by: the per-preset WCAG contrast test.
  3. Live apply stops mutating THEME (apply_theme() no longer calls
     target.update(...), so ui.THEME never changes after a setting change).
     Caught by: the in-place-mutation test AND the "redraw uses it" test.
  4. The corrupt/unknown-preset fallback is removed (resolve_theme_hex()
     indexes PRESETS[preset] directly instead of PRESETS.get(preset,
     default) - an unrecognised preset name RAISES instead of falling
     back to Default). Caught by: the unknown-preset-falls-back-to-Default
     test.
  5. Container.bg_color() stops resolving a THEME key string live (returns
     self.bg raw) - every already-built screen's background would freeze
     at whatever ui.THEME held at construction, silently undoing the
     entire "applies live, no restart" promise for backgrounds. Caught by:
     ui.py's own live-theming test.

Device field test 25 Sep (build i10) found three more real bugs none of
the above caught - #6-8 reproduce them:

  6. main.App.on_setting()'s "appearance" branch stops calling
     self.ui.root.damage_all() after palettes.apply_theme(). THEME is
     still correctly mutated, but nothing besides the tapped CycleButton
     (which invalidates itself) is ever marked damaged, so the real render
     loop (main.App._render(): clipped to take_damage()'s rect) never
     redraws anything else - observed on the device as the whole Settings
     page freezing at one preset's colours after a Home round-trip while
     later cycle-button taps kept "changing" the theme underneath. Caught
     by: the exact repro sequence test (change, go Home, come back, change
     again) that also checks a RecordingCanvas actually draws the Back
     button in the new colour.
  7. screens.py's Bar.set_master() goes back to passing an already-
     RESOLVED THEME["text"]/THEME["warn"] tuple to Label.set_text()
     instead of the bare key - freezes the volume % readout's colour at
     whatever it was the last time the volume itself changed, independent
     of any later theme change (observed on the device: white text stuck
     on RP5 White's light bar). Caught by: the readout-follows-a-theme-
     change-with-no-new-volume test.
  8. settings_view.py stops overriding the "Colour theme" CycleButton's
     display() with palettes.display_label(), so it falls back to the
     generic _display() (splits the raw preset NAME on underscores) and
     disagrees with `palettes.py --list`'s own descriptive labels for
     every classic-console preset (device: button said "Dreamcast",
     --list said "White/orange swirl"). Caught by: the cycle-button-
     matches-the-cli-label test.

Run: python -B tools/palette_break_tests.py   (Windows or WSL/python3 -
none of these eight need a device or cairo)."""
import os
import shutil
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BREAKS = [
    ("palettes.py",
     '        "warn": warn,\n',
     '',
     "tests.test_palettes.TestThemeKeys.test_every_preset_defines_every_key"),
    ("palettes.py",
     '    "rp5_white": (0xF5F5F5, 0x1A1A1A, 0x3D8BFD, 0xC62828, 0x1E7B34, 0x9A6400),\n',
     '    "rp5_white": (0xF5F5F5, 0xAAAAAA, 0x3D8BFD, 0xC62828, 0x1E7B34, 0x9A6400),\n',
     "tests.test_palettes.TestShippedPresetContrast.test_text_on_background_meets_body_threshold"),
    ("palettes.py",
     "    target.update(theme_rgba(hexdict))\n",
     "    pass\n",
     "tests.test_palettes.TestResolveAndApply.test_apply_theme_mutates_the_given_dict_in_place_not_rebinding"),
    ("palettes.py",
     "    return dict(PRESETS.get(preset, PRESETS[DEFAULT_PRESET]))\n",
     "    return dict(PRESETS[preset])\n",
     "tests.test_palettes.TestResolveAndApply.test_resolve_unknown_preset_falls_back_to_default"),
    ("ui.py",
     "        return THEME[self.bg] if isinstance(self.bg, str) else self.bg\n",
     "        return self.bg\n",
     "tests.test_ui.TestLiveTheming.test_container_bg_string_key_resolves_live"),
    ("main.py",
     "            if self.ui is not None:\n                self.ui.root.damage_all()\n",
     "",
     "tests.test_appearance_main.TestFullRepaintAfterHomeRoundTrip."
     "test_a_second_change_after_a_home_round_trip_repaints_everything"),
    ("screens.py",
     '        self.readout.set_text(volume_readout(master), "text" if ok else "warn")\n',
     '        self.readout.set_text(volume_readout(master),\n'
     '                              THEME["text"] if ok else THEME["warn"])\n',
     "tests.test_ui.TestLiveTheming.test_volume_readout_follows_a_theme_change_without_a_new_volume"),
    ("settings_view.py",
     '_ENUM_DISPLAY_OVERRIDES[("appearance", "theme_preset")] = palettes.display_label\n',
     '',
     "tests.test_settings_view.TestAppearanceGroup."
     "test_cycle_button_shows_the_same_descriptive_label_as_the_cli"),
]


def clear_caches():
    for root, dirs, _ in os.walk(APP):
        for d in list(dirs):
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
        print("%d ANCHOR MISSING/AMBIGUOUS in %s (count=%d)" % (i, fn, text.count(good)))
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
    print("%d %s  %-10s broken: %s | restored: %s | identical: %s"
          % (i, "PROVEN" if proven else "NOT PROVEN", fn, last_b, last_r, same))
print("ALL PROVEN" if ok_all else "SOME NOT PROVEN")
sys.exit(0 if ok_all else 1)
