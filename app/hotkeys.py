"""hotkeys - CC7: the Hotkey cheat sheet's data model.

PC-only research task (device offline, read-only rule anyway): every combo
below is sourced from one of two places, never from memory -

  verified_on_device=True   confirmed by an EARLIER agent's actual read of
                             this device (cited by the doc/row that recorded
                             it: TASKS.md's CC7 row, research/R7-input-media.md,
                             research/DS1-dual-screen-emulators.md, TASKS.md's
                             DS3 device-test row). This module does not touch
                             the device itself (PC-only, no device access).
  verified_on_device=False  read from ROCKNIX's public GitHub source
                             (ROCKNIX/distribution, branch "next") - cited by
                             exact file path (+ line numbers where the file
                             is short and stable enough for them to mean
                             anything) and, for input_sense, the commit this
                             was read at (dc5f51a, 2026-09-11 - the RP5's
                             20260923 nightly is 12 days newer but nothing in
                             TASKS.md's CC7 row conflicts with what that
                             commit shows, and the CC7 row's own line numbers
                             land inside the same function this read finds a
                             few lines earlier at - a nightly-build shift, not
                             a different mechanism).

A combo this module could not source with confidence is left OUT of
CONTEXTS entirely and named instead in UNCONFIRMED at the bottom - "never
invent a binding" (owner's rule). Two physical-button facts in particular
are NOT verified_on_device (TASKS.md says so explicitly) even though the
GitHub source points at an answer with unusual unanimity across five
independently-shipped config files (input_sense's own SM8250 platform
default, melonDS, Azahar and Flycast's shipped InputPlumber profiles all use
the same numeric/named modifier, and Dolphin's Hotkeys.ini spells it out in
English) - see UNCONFIRMED for exactly what would need a real button press
on the device to close the loop.

Runtime override (see read_overrides()/load()): retroarch.cfg and
system.cfg's key.* lines are parsed with a small ALLOW-LIST, never a
general-purpose ini reader - system.cfg also holds Wi-Fi credentials, and
this module must never read or log anything from it outside SYSTEM_CFG_KEYS.
"""
import collections
import os
import re

RETROARCH_CFG_PATH = "/storage/.config/retroarch/retroarch.cfg"
SYSTEM_CFG_PATH = "/storage/.config/system/configs/system.cfg"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
Combo = collections.namedtuple(
    "Combo", "buttons action verified_on_device source note role_buttons")
Combo.__new__.__defaults__ = ("", None)
Combo.__doc__ = """One hotkey combo.
buttons: tuple of display glyphs, held together (display order).
action: what it does, one short phrase.
verified_on_device: True (an earlier agent pressed it / read it live) or
    False (ROCKNIX source only).
source: citation string (file path [+lines], or the doc/row that verified it).
note: "" or a short caveat shown alongside the combo (e.g. "off by default").
role_buttons: None, or a tuple of role ids (see ROLE_DEFAULT_BTN) the SAME
    length as `buttons` - only GLOBAL's combos carry this, so a system.cfg
    override can recompute `buttons` from the live key.* value. Every other
    context's `buttons` is fixed (this module has no live-override source
    for melonDS/Azahar/Dolphin/etc.'s OWN config files - out of scope, see
    module docstring)."""


def combo(buttons, action, verified_on_device, source, note="", role_buttons=None):
    return Combo(tuple(buttons), action, bool(verified_on_device), source, note,
                tuple(role_buttons) if role_buttons else None)


Context = collections.namedtuple("Context", "id title combos")


# ---------------------------------------------------------------------------
# Button glyphs (evdev BTN_* name -> what the sheet shows)
# ---------------------------------------------------------------------------
BTN_GLYPH = {
    "BTN_TL": "L1", "BTN_TR": "R1", "BTN_TL2": "L2", "BTN_TR2": "R2",
    "BTN_SELECT": "Select", "BTN_START": "Start", "BTN_MODE": "Home",
    "BTN_THUMBL": "L3", "BTN_THUMBR": "R3",
    "BTN_DPAD_UP": "D-pad Up", "BTN_DPAD_DOWN": "D-pad Down",
    "BTN_DPAD_LEFT": "D-pad Left", "BTN_DPAD_RIGHT": "D-pad Right",
    # face buttons: Nintendo layout, CONFIRMED by the owner directly on this
    # device (test day, 2026-09-24): bottom=B, right=A, left=Y, top=X. This
    # supersedes R7-input-media.md's own read of retroid_mcu.yaml, which only
    # called the physical-to-Sony-glyph mapping "Circle/X depending on
    # layout" (its words, i.e. NOT confidently one glyph at the time) - the
    # owner's on-device press-and-look closes that gap for THIS device.
    "BTN_NORTH": "X", "BTN_SOUTH": "B",
    "BTN_EAST": "A", "BTN_WEST": "Y",
    "BTN_BACK": "Back", "BTN_TOUCH": "Touch screen",
}


def btn_glyph(name):
    return BTN_GLYPH.get(name, name)


# Role -> the BTN_* name input_sense/SM8250 fall back to when system.cfg has
# no override (input_sense lines 53-60 for hotkey.a/b/c; SM8250's
# projects/ROCKNIX/packages/hardware/quirks/platforms/SM8250/070-modifiers
# for function.a/b - the "Retroid Pocket 5" device quirks dir has no
# 050-modifiers/070-modifiers of its own, so it inherits the platform
# default unchanged).
ROLE_DEFAULT_BTN = {
    "hotkey_a": "BTN_TL", "hotkey_b": "BTN_SELECT", "hotkey_c": "BTN_START",
    "function_a": "BTN_MODE", "function_b": "BTN_START",
}
# Role -> the system.cfg key that overrides it (input_sense lines 44, 47, 53,
# 56, 59: get_setting key.function.a / key.function.b / key.hotkey.a/b/c).
ROLE_SYSTEM_CFG_KEY = {
    "hotkey_a": "key.hotkey.a", "hotkey_b": "key.hotkey.b", "hotkey_c": "key.hotkey.c",
    "function_a": "key.function.a", "function_b": "key.function.b",
}


def _roles(*names):
    return tuple(ROLE_DEFAULT_BTN[n] for n in names), tuple(names)


# ---------------------------------------------------------------------------
# GLOBAL - ROCKNIX's own input_sense (every ROCKNIX device, this build read
# at ROCKNIX/distribution commit dc5f51a,
# projects/ROCKNIX/packages/sysutils/system-utils/sources/scripts/input_sense)
# ---------------------------------------------------------------------------
_KILL_BTN, _KILL_ROLES = _roles("hotkey_a", "hotkey_b", "hotkey_c")
_FNVOL_BTN, _FNVOL_ROLES = _roles("function_a")

GLOBAL_COMBOS = [
    combo(tuple(btn_glyph(b) for b in _KILL_BTN), "Kill the running app (input_sense execute_kill)",
         True, "TASKS.md CC7 row + DS3 row (device-verified: 'Exit = L1+SELECT+START "
               "(input_sense kill combo)'); input_sense:406-441 (dc5f51a) shows the same "
               "three-modifier AND, expressed as HOTKEY_A+HOTKEY_B+HOTKEY_C all held",
         role_buttons=_KILL_ROLES),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Up"), "Volume up",
         True, "TASKS.md CC7 row ('FN + D-pad up/down = volume'); "
               "input_sense:442-447 (dc5f51a): FN_A held + BTN_DPAD_UP, gated on key.dpad.events",
         note="OFF on this device (the ROCKNIX setting key.dpad.events is not enabled)", role_buttons=(_FNVOL_ROLES[0], None)),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Down"), "Volume down",
         True, "TASKS.md CC7 row; input_sense:448-453 (dc5f51a)",
         note="OFF on this device (the ROCKNIX setting key.dpad.events is not enabled)", role_buttons=(_FNVOL_ROLES[0], None)),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Right"), "Brightness up",
         True, "TASKS.md CC7 row ('FN + D-pad left/right = brightness'); "
               "input_sense:454-460 (dc5f51a)",
         note="OFF on this device (the ROCKNIX setting key.dpad.events is not enabled)", role_buttons=(_FNVOL_ROLES[0], None)),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Left"), "Brightness down",
         True, "TASKS.md CC7 row; input_sense:462-468 (dc5f51a)",
         note="OFF on this device (the ROCKNIX setting key.dpad.events is not enabled)", role_buttons=(_FNVOL_ROLES[0], None)),
    combo(("VOL+ (hold)",), "Brightness up (FN_A held while pressing the hardware volume-up key)",
         True, "TASKS.md CC7 row ('FN_A/FN_B/FN_AB actions = brightness/LED/Wi-Fi'); "
               "input_sense:125,133,187-220 (dc5f51a): FN_A_ACTION_UP default 'brightness up'"),
    combo(("VOL- (hold)",), "Brightness down (FN_A held while pressing the hardware volume-down key)",
         True, "TASKS.md CC7 row; input_sense:126,134,187-220 (dc5f51a)"),
    combo((btn_glyph(_FNVOL_BTN[0]) + " (FN_B)", "VOL+ (hold)"), "LED control",
         True, "TASKS.md CC7 row ('FN_B = LED'); input_sense:127,135,221-232 (dc5f51a): "
               "FN_B_ACTION_UP default 'ledcontrol'. FN_B's own modifier is a SEPARATE "
               "button (key.function.b, default BTN_START on SM8250) from FN_A - held "
               "together with the volume key, not with FN_A"),
    combo(("FN_B", "VOL- (hold)"), "LED off / power off (ledcontrol poweroff)",
         True, "TASKS.md CC7 row; input_sense:128,136 (dc5f51a)"),
    combo(("FN_A", "FN_B", "VOL+ (hold)"), "Wi-Fi enable",
         True, "TASKS.md CC7 row ('FN_AB = Wi-Fi'); input_sense:129,137,189-200 (dc5f51a)"),
    combo(("FN_A", "FN_B", "VOL- (hold)"), "Wi-Fi disable",
         True, "TASKS.md CC7 row; input_sense:130,138 (dc5f51a)"),
    # Bonus finds while reading input_sense in full - NOT in the TASKS row,
    # so from_source only, and they use HOTKEY_A (L1), not FUNCTION_A (the
    # "FN" this module cannot yet name for certain - see UNCONFIRMED).
    combo((btn_glyph("BTN_TL"), btn_glyph("BTN_EAST")), "Screenshot (rocknix-screenshot)",
         False, "input_sense:476-484 (dc5f51a): HOTKEY_A_PRESSED + BTN_EAST"),
    combo((btn_glyph("BTN_TL"), btn_glyph("BTN_WEST")), "Toggle MangoHud overlay",
         False, "input_sense:485-490 (dc5f51a): HOTKEY_A_PRESSED + BTN_WEST"),
    combo((btn_glyph("BTN_TL"), btn_glyph("BTN_NORTH")), "Open the game-guides tool",
         False, "input_sense:491-496 (dc5f51a): HOTKEY_A_PRESSED + BTN_NORTH"),
    combo((btn_glyph(_FNVOL_BTN[0]), btn_glyph("BTN_TOUCH")), "Toggle the on-screen keyboard (wvkbd)",
         False, "input_sense:470-475 (dc5f51a); gated on key.touchscreen.events",
         note="only if the ROCKNIX setting key.touchscreen.events is on (not yet confirmed here)",
         role_buttons=(_FNVOL_ROLES[0], None)),
]

# ---------------------------------------------------------------------------
# RETROARCH - derived at boot by setsettings.sh's configure_hotkeys() from
# whichever joypad autoconfig matches the connected pad; on this device that
# is InputPlumber's virtual DualSense (R7-input-media.md), so the values
# below come from that autoconfig file. system.autohotkeys defaults to "1"
# (rocknix/config/system/configs/system.cfg:178) - i.e. this whole scheme is
# ON out of the box.
# ---------------------------------------------------------------------------
_RA_SOURCE = ("ROCKNIX/distribution: projects/ROCKNIX/packages/rocknix/sources/scripts/"
             "setsettings.sh:347-417 (configure_hotkeys) reading "
             "retroarch-joypads/gamepads/'Sony Interactive Entertainment DualSense Wireless "
             "Controller.cfg' (input_select_btn=8 'Create', input_start_btn=9 'Options', "
             "input_y_btn=3 'Square', input_x_btn=2 'Triangle', input_r_btn=5 'R1', "
             "input_l_btn=4 'L1', input_r2_axis/input_l2_axis present -> the axis branch, "
             "setsettings.sh:404-412); default enabled by system.cfg:178 system.autohotkeys=1")

RETROARCH_COMBOS = [
    combo(("Select (hold)", "Start"), "Exit RetroArch / close the running core",
         False, _RA_SOURCE, note="RetroArch's own 'quit' hotkey, not the ROCKNIX kill combo"),
    combo(("Select (hold)", "Y"), "Toggle the on-screen FPS counter", False, _RA_SOURCE),
    combo(("Select (hold)", "X"), "Open RetroArch's Quick Menu", False, _RA_SOURCE),
    combo(("Select (hold)", "R1"), "Save state", False, _RA_SOURCE),
    combo(("Select (hold)", "L1"), "Load state", False, _RA_SOURCE),
    combo(("Select (hold)", "R2 (hold)"), "Fast-forward toggle", False, _RA_SOURCE),
    combo(("Select (hold)", "L2 (hold)"), "Rewind (hold)", False, _RA_SOURCE),
]
# The exact numeric default for each key, so an override can be recognised
# (see apply_retroarch_overrides) without this module inventing a glyph for
# an unfamiliar raw joypad-button index.
RETROARCH_DEFAULT_RAW = {
    "input_enable_hotkey_btn": "8", "input_exit_emulator_btn": "9",
    "input_fps_toggle_btn": "3", "input_menu_toggle_btn": "2",
    "input_save_state_btn": "5", "input_load_state_btn": "4",
    "input_toggle_fast_forward_axis": "+5", "input_rewind_axis": "+2",
}

# ---------------------------------------------------------------------------
# Standalone emulators - each Combo's `buttons` already says "Guide" in
# English wherever the SOURCE itself says so (Dolphin, dsperate); melonDS,
# Azahar and Flycast's own files use a numeric/named modifier this module
# has NOT independently confirmed maps to the same physical button as
# "Guide" - see UNCONFIRMED. None of these six have a live-override path
# (task scope: only retroarch.cfg + system.cfg key.*, see module docstring).
# ---------------------------------------------------------------------------
_MELONDS_SRC = ("ROCKNIX/distribution: projects/ROCKNIX/packages/emulators/standalone/"
               "melonds-sa/config/InputPlumber/melonDS.ini (HKJoy_HotkeyEnable=10, "
               "HKJoy_SaveState=5, HKJoy_LoadState=4 - 5/4 match this same file's own "
               "Joy_R=5/Joy_L=4, i.e. R1/L1)")
MELONDS_COMBOS = [
    combo(("Guide (button 10)", "R1"), "Save state", False, _MELONDS_SRC,
         note="'Guide' is the Home button (not yet confirmed on this device)"),
    combo(("Guide (button 10)", "L1"), "Load state", False, _MELONDS_SRC,
         note="'Guide' is the Home button (not yet confirmed on this device)"),
]

_AZAHAR_SRC = ("ROCKNIX/distribution: projects/ROCKNIX/packages/emulators/standalone/"
              "azahar-sa/config/InputPlumber/qt-config.ini:56-57 "
              "(profiles\\1\\button_home=\"button:10,...\"); every "
              "Shortcuts\\*\\controller_keyseq in the same file is empty - no other "
              "gamepad combo is bound")
AZAHAR_COMBOS = [
    combo(("Guide (button 10)",), "Open Azahar's Home Menu (Resume / Save State / Load State / Exit)",
         False, _AZAHAR_SRC, note="'Guide' is the Home button (not yet confirmed on this device)"),
]

_DSPERATE_SRC = ("ROCKNIX/distribution: projects/ROCKNIX/packages/emulators/standalone/"
                "dsperate-sa/config/InputPlumber/dsperate.ini:44-58 ([padhotkeys], "
                "modifier=guide - this file names the modifier in plain text)")
DSPERATE_COMBOS = [
    combo(("Guide", "Start"), "Quit", False, _DSPERATE_SRC),
    combo(("Guide", "X"), "Pause", False, _DSPERATE_SRC),
    combo(("Guide", "B (hold)"), "Fast-forward (hold)", False, _DSPERATE_SRC),
    combo(("Guide (hold)", "R2"), "Fast-forward toggle", False, _DSPERATE_SRC),
    combo(("Guide", "R1"), "Save state", False, _DSPERATE_SRC),
    combo(("Guide", "L1"), "Load state", False, _DSPERATE_SRC),
    combo(("Guide", "D-pad Right"), "Next save-state slot", False, _DSPERATE_SRC),
    combo(("Guide", "D-pad Left"), "Previous save-state slot", False, _DSPERATE_SRC),
    combo(("R2",), "Next screen layout", False, _DSPERATE_SRC),
    combo(("L2",), "Previous screen layout", False, _DSPERATE_SRC),
    combo(("Guide", "L2"), "Swap top/bottom screen", False, _DSPERATE_SRC),
    combo(("Guide", "Y"), "Toggle the FPS counter", False, _DSPERATE_SRC),
    combo(("Left stick click",), "Microphone", False, _DSPERATE_SRC),
]

_DOLPHIN_SRC = ("ROCKNIX/distribution: projects/ROCKNIX/packages/emulators/standalone/"
               "dolphin-sa/config/controllers/Hotkeys.ini (this file spells the modifier "
               "'Guide' in plain text, and every action button by its generic SDL name)")
DOLPHIN_COMBOS = [
    combo(("Guide", "Start"), "Exit Dolphin", False, _DOLPHIN_SRC),
    combo(("Guide", "South button"), "Toggle aspect ratio", False, _DOLPHIN_SRC),
    combo(("Guide", "East button"), "Take a screenshot", False, _DOLPHIN_SRC),
    combo(("Guide", "R1"), "Save state (to the selected slot)", False, _DOLPHIN_SRC),
    combo(("Guide", "L1"), "Load state (slot 1)", False, _DOLPHIN_SRC),
    combo(("Guide", "D-pad Up"), "Next save-state slot", False, _DOLPHIN_SRC),
    combo(("Guide", "D-pad Down"), "Previous save-state slot", False, _DOLPHIN_SRC),
    combo(("Guide", "R2"), "Speed up emulation", False, _DOLPHIN_SRC),
    combo(("Guide", "L2"), "Slow down emulation", False, _DOLPHIN_SRC),
]

_AETHERSX2_SRC = ("ROCKNIX/distribution: projects/ROCKNIX/packages/emulators/standalone/"
                 "aethersx2-sa/config/InputPlumber/aethersx2/inis/PCSX2.ini:322-326 "
                 "([Hotkeys], SDL-0/Guide named directly)")
AETHERSX2_COMBOS = [
    combo(("Guide", "L1"), "Load state (from the selected slot)", False, _AETHERSX2_SRC),
    combo(("Guide", "R1"), "Save state (to the selected slot)", False, _AETHERSX2_SRC),
    combo(("Guide", "Y"), "Open the pause menu", False, _AETHERSX2_SRC),
    combo(("Guide", "B"), "Screenshot", False, _AETHERSX2_SRC),
]

_FLYCAST_SRC = ("ROCKNIX/distribution: projects/ROCKNIX/packages/emulators/standalone/"
               "flycast-sa/config/InputPlumber/'SDL_Sony Interactive Entertainment "
               "DualSense Wireless Controller.cfg' ([combo] section; the held button is "
               "digital code 10 - not spelled out by this file, unlike melonDS's/Azahar's "
               "use of the same code 10 as their Guide/Home hotkey button)")
FLYCAST_COMBOS = [
    combo(("code 10 (likely Guide)", "X"), "Open menu", False, _FLYCAST_SRC,
         note="'Guide' is the Home button (not yet confirmed on this device)"),
    combo(("code 10 (likely Guide)", "Z"), "Jump to save state", False, _FLYCAST_SRC,
         note="'Guide' is the Home button (not yet confirmed on this device)"),
    combo(("code 10 (likely Guide)", "C"), "Quick save", False, _FLYCAST_SRC,
         note="'Guide' is the Home button (not yet confirmed on this device)"),
    combo(("code 10 (likely Guide)", "Start"), "Escape / exit", False, _FLYCAST_SRC,
         note="'Guide' is the Home button (not yet confirmed on this device)"),
    combo(("code 10 (likely Guide)", "R1 (hold)"), "Fast-forward", False, _FLYCAST_SRC,
         note="'Guide' is the Home button (not yet confirmed on this device)"),
]

_CEMU_SRC = ("No ROCKNIX-shipped controller hotkey/exit binding was found for Cemu: "
            "projects/ROCKNIX/packages/emulators/standalone/cemu-sa/config/InputPlumber/"
            "wii_u_gamepad.xml only maps face buttons/sticks to the Wii U GamePad, with no "
            "separate hotkey file anywhere under cemu-sa/. TASKS.md's DS3 device-test row "
            "already confirmed the GLOBAL kill combo closes a running Wii U/Cemu session.")
CEMU_COMBOS = [
    combo(tuple(btn_glyph(b) for b in _KILL_BTN),
         "Close Cemu (no Cemu-specific hotkey - this is the GLOBAL kill combo)",
         True, "TASKS.md DS3 row (device-verified for a running Wii U session) + " + _CEMU_SRC),
]

CONTEXT_TITLES = {
    "global": "Global (ROCKNIX)",
    "retroarch": "RetroArch (libretro cores)",
    "melonds": "melonDS (NDS)",
    "azahar": "Azahar (3DS)",
    "dsperate": "DraStic (NDS, dsperate)",
    "dolphin": "Dolphin (GameCube / Wii)",
    "aethersx2": "AetherSX2 (PS2)",
    "flycast": "Flycast (Dreamcast)",
    "cemu": "Cemu (Wii U)",
}

# Default paging / context order when no game is running.
CONTEXTS_ORDER = ["global", "retroarch", "melonds", "azahar", "dsperate", "dolphin",
                  "aethersx2", "flycast", "cemu"]

_COMBOS_BY_ID = {
    "global": GLOBAL_COMBOS, "retroarch": RETROARCH_COMBOS, "melonds": MELONDS_COMBOS,
    "azahar": AZAHAR_COMBOS, "dsperate": DSPERATE_COMBOS, "dolphin": DOLPHIN_COMBOS,
    "aethersx2": AETHERSX2_COMBOS, "flycast": FLYCAST_COMBOS, "cemu": CEMU_COMBOS,
}

# Best-effort ES system short name -> the context to show FIRST while that
# system's game is running. This mapping itself is NOT device-verified (this
# module never saw ES's real es_systems.cfg on this device) - it follows the
# short names research/DS1-dual-screen-emulators.md's own default-core table
# uses (melonds-sa default for nds, azahar-sa default for 3ds) plus the
# conventional ES/ROCKNIX short names for the rest. A miss here only means
# the sheet opens on Global first instead of the right emulator page - never
# a wrong or invented combo.
SYSTEM_TO_CONTEXT = {
    "nds": "melonds", "n3ds": "azahar", "3ds": "azahar",
    "gc": "dolphin", "gamecube": "dolphin", "wii": "dolphin", "wiiu": "cemu",
    "ps2": "aethersx2", "dreamcast": "flycast", "dc": "flycast",
}

# Systems ROCKNIX runs on a STANDALONE emulator this module could not source
# any hotkey file for (see UNCONFIRMED: PPSSPP, mupen64plus-sa - both "-sa"
# builds, same family as melonds-sa/azahar-sa/etc., just without a controls
# file this module found). These must be EXCLUDED from the "default to
# RetroArch" fallback below: RetroArch's hotkeys do not apply to a standalone
# emulator, and showing them would be actively wrong, not just incomplete -
# never invent a binding by implying the wrong emulator owns it.
SYSTEM_NOT_RETROARCH = frozenset(["psp", "n64"])

UNCONFIRMED = [
    "Which physical button is ROCKNIX's 'FN'/function-key-A modifier on this "
    "exact RP5: five independently-authored ROCKNIX config files agree with "
    "unusual unanimity (input_sense's SM8250-platform default "
    "DEVICE_FUNC_KEYA_MODIFIER=BTN_MODE, with no Retroid-Pocket-5-specific "
    "override in quirks/devices/'Retroid Pocket 5'; melonDS, Azahar and "
    "Flycast's shipped InputPlumber configs all use digital button code 10 "
    "as their 'hotkey enable'/Home button; Dolphin's Hotkeys.ini spells the "
    "same modifier 'Guide' in plain text) - but no CC7 device read has "
    "pressed it and watched something happen. TASKS.md's CC7 row lists this "
    "as unconfirmed and this module leaves it that way: check by pressing "
    "the Guide/Home button (BTN_MODE) plus a D-pad direction and watching "
    "for a volume or brightness change.",
    "Whether key.dpad.events is actually turned on for this device: ROCKNIX "
    "ships it OFF by default on the RP5 - no "
    "packages/hardware/quirks/devices/'Retroid Pocket 5'/*dpad* quirk file "
    "exists, unlike e.g. Powkiddy RGB10 / Anbernic RG351V / RG351M / Game "
    "Console R33S, which each carry a 075-dpad-volbright quirk that turns it "
    "on. Unless the owner enabled key.dpad.events by hand, FN+D-pad volume/"
    "brightness does nothing yet - check system.cfg or Settings first.",
    "key.touchscreen.events (FN + a touchscreen tap toggles the on-screen "
    "keyboard, input_sense:470-475): not in the TASKS.md CC7 row, default "
    "off per the same input_sense logic as key.dpad.events, unconfirmed.",
    "melonDS's HKJoy_FastForwardToggle (86114303) and HKJoy_SwapScreenEmphasis "
    "(35782655): melonDS packs a joystick hotkey binding into one opaque "
    "integer (button id + axis/hat + held-modifier bits); decoding it right "
    "needs melonDS's own C++ encoder, so these two combos are omitted rather "
    "than guessed at.",
    "Flycast's [combo] section holds digital-code-10 combos "
    "(menu/save/load/exit/fast-forward) but its OWN file never names that "
    "code - unlike Dolphin's literal 'Guide' - so those five entries carry a "
    "'likely Guide' qualifier rather than a bare assertion.",
    "PPSSPP (PSP): ROCKNIX ships no InputPlumber/RP5-specific override and "
    "no controls.ini under emulators/standalone/ppsspp-sa/config/ - it runs "
    "on PPSSPP's own built-in defaults, which live in its UI, not a file "
    "this module could source. No entries published.",
    "mupen64plus-sa (N64): the only shipped InputPlumber default.ini "
    "(mupen64plus-sa-input-sdl/config/InputPlumber/default.ini) is keyed to "
    "an '[Xbox Series X Controller]' profile with face-button/analog "
    "mapping only - no savestate/fast-forward/exit hotkey section was found "
    "anywhere under mupen64plus-sa/. No entries published.",
    "RetroArch overrides: this module recognises an input_*_btn value in "
    "the device's real retroarch.cfg as 'the default, unchanged' or "
    "'overridden' by comparing against RETROARCH_DEFAULT_RAW, but an "
    "overridden value is shown as a raw joypad-button index, never "
    "translated to a glyph - decoding an arbitrary index back to a physical "
    "button needs the joypad autoconfig file this module does not read live "
    "(out of the task's two named override sources).",
]


# ---------------------------------------------------------------------------
# Public: contexts + ordering (pure)
# ---------------------------------------------------------------------------
def default_contexts():
    """id -> Context, straight from the static tables above (no I/O)."""
    return {cid: Context(cid, CONTEXT_TITLES[cid], list(_COMBOS_BY_ID[cid]))
            for cid in CONTEXTS_ORDER}


def ordered_context_ids(running_system=None):
    """CONTEXTS_ORDER, reordered so the running game's emulator page comes
    first, then Global, then everything else in the default order (the
    owner's rule: 'while a game runs, that emulator's hotkeys first').

    Nothing running (running_system falsy) -> the plain default order,
    unchanged (Global first, everything else reachable by paging).

    A system with its own standalone-emulator context in SYSTEM_TO_CONTEXT
    (nds/3ds/gc/wii/wiiu/ps2/dreamcast) -> that context first.

    Any OTHER running system -> "retroarch" first. ROCKNIX puts the large
    majority of systems (Genesis/Mega Drive, SNES, NES, PS1, GBA, arcade,
    ...) on a libretro core via RetroArch (setsettings.sh's
    configure_hotkeys - see the RETROARCH_COMBOS module comment); a game IS
    running, and RetroArch is what ROCKNIX puts it under unless this module
    already knows a more specific standalone emulator owns it (the mapping
    above) or the system is explicitly excluded because it's a standalone
    build this module could not source hotkeys for (SYSTEM_NOT_RETROARCH) -
    showing RetroArch's hotkeys there would be wrong, not just incomplete."""
    ids = list(CONTEXTS_ORDER)
    system = (running_system or "").strip().lower()
    if not system:
        return ids
    primary = SYSTEM_TO_CONTEXT.get(system)
    if primary is None and system not in SYSTEM_NOT_RETROARCH:
        primary = "retroarch"
    if primary in ids and primary != "global":
        rest = [i for i in ids if i not in (primary, "global")]
        return [primary, "global"] + rest
    return ids


def format_combo(c):
    return " + ".join(c.buttons)


# ---------------------------------------------------------------------------
# Live override loader
# ---------------------------------------------------------------------------
# ONLY these key.* names (plus system.autohotkeys, read for completeness but
# not currently surfaced) are ever extracted from system.cfg - it also holds
# Wi-Fi passwords and other credentials, and this module must never read or
# log anything else out of it.
SYSTEM_CFG_KEYS = frozenset([
    "key.hotkey.a", "key.hotkey.b", "key.hotkey.c",
    "key.function.a", "key.function.b",
    "key.dpad.events", "key.touchscreen.events",
])
RETROARCH_CFG_KEYS = frozenset(RETROARCH_DEFAULT_RAW)

# RetroArch joypad index -> the RP5's printed label, ONLY for indices with
# evidence: 2/3/4/5/8/9 from the DualSense autoconfig _RA_SOURCE cites
# (x=2 Triangle = top = X, y=3 Square = left = Y, l=4, r=5, select=8,
# start=9), and 10 = Home from the device on test day (retroarch.cfg has
# input_enable_hotkey_btn = 10 and the owner found Home + Start closes
# RetroArch). Anything else stays "not decoded".
# Test day button log (tests/fixtures/device/buttons-real-capture-2026-09-24.txt):
# every button pressed once on the device, with the index RetroArch's udev
# driver assigns - A=1, B=0, X=2, Y=3, L1=4, R1=5, Select=8, Start=9,
# Home=10, L3=11, R3=12 (L2/R2 are analog-only; Back is KEY_F1 on the
# InputPlumber keyboard, not the pad).
RA_INDEX_LABEL = {"0": "B", "1": "A", "2": "X", "3": "Y", "4": "L1", "5": "R1",
                  "8": "Select", "9": "Start", "10": "Home", "11": "L3", "12": "R3"}
# RETROARCH_COMBOS row -> the retroarch.cfg key of its second button (None:
# an axis row, left as written).
RETROARCH_ROW_KEYS = ["input_exit_emulator_btn", "input_fps_toggle_btn",
                      "input_menu_toggle_btn", "input_save_state_btn",
                      "input_load_state_btn", None, None]

_LINE_RE = re.compile(r'^([A-Za-z0-9_.]+)\s*=\s*"?([^"#]*?)"?\s*(?:#.*)?$')


def _read_text(path):
    """None if the file cannot be read (wrong OS, not this device, missing,
    permission) - never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def parse_allowlisted(text, allowed_keys):
    """Line-oriented 'key = value' / 'key=value' parser that returns ONLY
    keys in `allowed_keys` - a key not on the list is never even looked at
    long enough to be logged. Comments (# or ;) and blank lines are skipped;
    a malformed line is skipped, never raised on (a corrupt file must not
    crash the sheet)."""
    out = {}
    if not text:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
        else:
            continue
        if k not in allowed_keys:
            continue
        v = v.strip()
        m = re.match(r'^"?([^"#]*?)"?\s*(?:#.*)?$', v)
        out[k] = (m.group(1).strip() if m else v)
    return out


def read_overrides(read_fn=None):
    """{'system_cfg': {...}, 'retroarch_cfg': {...}} - each dict holds only
    the keys this module allow-lists, and is empty (never missing) when its
    file could not be read. `read_fn` (path -> text|None) is injected for
    tests; defaults to a real, exception-safe file read."""
    read_fn = read_fn or _read_text
    return {
        "system_cfg": parse_allowlisted(read_fn(SYSTEM_CFG_PATH), SYSTEM_CFG_KEYS),
        "retroarch_cfg": parse_allowlisted(read_fn(RETROARCH_CFG_PATH), RETROARCH_CFG_KEYS),
    }


def _dpad_events_state(system_cfg):
    v = system_cfg.get("key.dpad.events")
    if v is None:
        return None                 # unread (no file) - unknown, not "off"
    return v not in ("", "0")


def _touch_events_state(system_cfg):
    v = system_cfg.get("key.touchscreen.events")
    if v is None:
        return None
    return v not in ("", "0")


def apply_overrides(contexts, overrides):
    """Returns a NEW contexts dict with GLOBAL's role-based buttons
    recomputed from system.cfg's live key.* values (falling back to the
    ROCKNIX-source default role button when a role has no override), and
    RetroArch's raw *_btn/*_axis values flagged (not translated - see
    UNCONFIRMED) when they differ from the shipped default. Every other
    context is returned unchanged (no live-override source for those)."""
    system_cfg = (overrides or {}).get("system_cfg") or {}
    retroarch_cfg = (overrides or {}).get("retroarch_cfg") or {}
    out = dict(contexts)

    g = out.get("global")
    if g is not None:
        dpad_on = _dpad_events_state(system_cfg)
        touch_on = _touch_events_state(system_cfg)
        new_combos = []
        for c in g.combos:
            buttons = c.buttons
            note = c.note
            if c.role_buttons:
                resolved = []
                for role, default_glyph in zip(c.role_buttons, buttons):
                    if role is None:
                        resolved.append(default_glyph)
                        continue
                    key = ROLE_SYSTEM_CFG_KEY.get(role)
                    raw = system_cfg.get(key) if key else None
                    resolved.append(btn_glyph(raw) if raw else default_glyph)
                buttons = tuple(resolved)
            if "key.dpad.events" in (note or "") and dpad_on is not None:
                note = "" if dpad_on else "OFF on this device (key.dpad.events=0)"
            if "key.touchscreen.events" in (note or "") and touch_on is not None:
                note = "" if touch_on else "OFF on this device (key.touchscreen.events=0)"
            new_combos.append(c._replace(buttons=buttons, note=note))
        out["global"] = g._replace(combos=new_combos)

    ra = out.get("retroarch")
    if ra is not None and retroarch_cfg:
        def live(key):
            return retroarch_cfg.get(key) or RETROARCH_DEFAULT_RAW.get(key)
        hk = RA_INDEX_LABEL.get(live("input_enable_hotkey_btn"))
        new_combos = []
        undecoded = set()
        for i, c in enumerate(ra.combos):
            key = RETROARCH_ROW_KEYS[i] if i < len(RETROARCH_ROW_KEYS) else None
            second = c.buttons[1] if len(c.buttons) > 1 else None
            if key is not None:
                second = RA_INDEX_LABEL.get(live(key))
                if second is None:
                    undecoded.add(key)
            if hk is not None and second is not None:
                c = c._replace(buttons=(hk + " (hold)", second))
            new_combos.append(c)
        if hk is None:
            undecoded.add("input_enable_hotkey_btn")
        # Only annotate if we can tie a combo to a specific overridden key;
        # RETROARCH_COMBOS don't carry role_buttons (see module docstring),
        # so this loop only adds a device-wide note once, on the first combo,
        # rather than mis-attribute an override to the wrong row.
        changed = [k for k, v in retroarch_cfg.items()
                  if v and v != RETROARCH_DEFAULT_RAW.get(k) and k in undecoded]
        if changed and new_combos:
            extra = ("device overrides %s away from the shipped default (raw value(s) "
                    "not decoded to a glyph, so not shown here)" % ", ".join(sorted(changed)))
            first = new_combos[0]
            new_combos[0] = first._replace(
                note=(first.note + "; " + extra) if first.note else extra)
        out["retroarch"] = ra._replace(combos=new_combos)
    return out


def load(running_system=None, read_fn=None):
    """The one call main.py/hotkeys_view.py need: (contexts, order) with
    live overrides applied where this module has a source for them, and
    ordering biased toward `running_system`'s emulator (see
    ordered_context_ids). Never raises - a device read failure just means
    the ROCKNIX-source defaults are shown, per the task's fallback rule."""
    contexts = apply_overrides(default_contexts(), read_overrides(read_fn))
    order = ordered_context_ids(running_system)
    return contexts, order
