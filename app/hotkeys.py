"""hotkeys: the data behind the Hotkey cheat sheet.

Every combo here comes from one of two places, never memory:

  verified_on_device=True   seen on this device (TASKS.md's CC7 and DS3 rows,
                            research/R7-input-media.md, research/DS1-dual-screen-emulators.md)
  verified_on_device=False  read from ROCKNIX's GitHub source (ROCKNIX/distribution, branch
                            "next"), cited by file path, and for input_sense the commit it was
                            read at (dc5f51a). The RP5's nightly is newer but nothing on the
                            device disagrees with it.

A combo that couldnt be sourced with confidence stays out of CONTEXTS and gets named in
UNCONFIRMED at the bottom instead, never make up a binding. Two button facts in particular
arent confirmed on the device even though five separate config files agree (input_sense's
SM8250 default, and melonDS, Azahar and Flycast's InputPlumber profiles all use the same
modifier, and Dolphin's Hotkeys.ini spells it out). UNCONFIRMED says which button press on
the device would settle it.

Live overrides (read_overrides()/load()): retroarch.cfg and system.cfg's key.* lines are read
with a small allow list, never a general ini reader. system.cfg also holds the Wi-Fi password,
so nothing outside SYSTEM_CFG_KEYS is ever read or logged from it.
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
    # Face buttons are Nintendo layout, checked by pressing them on this device: bottom=B,
    # right=A, left=Y, top=X.
    "BTN_NORTH": "X", "BTN_SOUTH": "B",
    "BTN_EAST": "A", "BTN_WEST": "Y",
    "BTN_BACK": "Back", "BTN_TOUCH": "Touch screen",
}


def btn_glyph(name):
    return BTN_GLYPH.get(name, name)


# Role -> the BTN_* name input_sense and SM8250 fall back to when system.cfg has no override
# (input_sense lines 53-60 for hotkey.a/b/c, and SM8250's
# projects/ROCKNIX/packages/hardware/quirks/platforms/SM8250/070-modifiers for function.a/b.
# The Retroid Pocket 5 quirks folder has no modifiers file of its own so it gets the platform
# default).
ROLE_DEFAULT_BTN = {
    "hotkey_a": "BTN_TL", "hotkey_b": "BTN_SELECT", "hotkey_c": "BTN_START",
    "function_a": "BTN_MODE", "function_b": "BTN_START",
}
# Role -> the system.cfg key that overrides it (input_sense lines 44, 47, 53, 56, 59:
# key.function.a / key.function.b / key.hotkey.a/b/c).
ROLE_SYSTEM_CFG_KEY = {
    "hotkey_a": "key.hotkey.a", "hotkey_b": "key.hotkey.b", "hotkey_c": "key.hotkey.c",
    "function_a": "key.function.a", "function_b": "key.function.b",
}


def _roles(*names):
    return tuple(ROLE_DEFAULT_BTN[n] for n in names), tuple(names)


# ---------------------------------------------------------------------------
# GLOBAL: ROCKNIX's own input_sense (every ROCKNIX device), read at ROCKNIX/distribution
# commit dc5f51a, projects/ROCKNIX/packages/sysutils/system-utils/sources/scripts/input_sense
# ---------------------------------------------------------------------------
_KILL_BTN, _KILL_ROLES = _roles("hotkey_a", "hotkey_b", "hotkey_c")
_FNVOL_BTN, _FNVOL_ROLES = _roles("function_a")

# The notes shown for the two ROCKNIX-gated groups. apply_overrides() swaps them by identity,
# never by searching the text, so the text can read plainly.
DPAD_NOTE = "off on this device (ROCKNIX has D-pad hotkeys turned off)"
TOUCH_NOTE = "only when ROCKNIX's touch-screen hotkeys are on"
TOUCH_OFF_NOTE = "off on this device (ROCKNIX has touch-screen hotkeys turned off)"

GLOBAL_COMBOS = [
    combo(tuple(btn_glyph(b) for b in _KILL_BTN), "Close the running game or app",
         True, "TASKS.md CC7 row + DS3 row (device-verified: 'Exit = L1+SELECT+START "
               "(input_sense kill combo)'); input_sense:406-441 (dc5f51a) shows the same "
               "three-modifier AND, expressed as HOTKEY_A+HOTKEY_B+HOTKEY_C all held",
         role_buttons=_KILL_ROLES),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Up"), "Volume up",
         True, "TASKS.md CC7 row ('FN + D-pad up/down = volume'); "
               "input_sense:442-447 (dc5f51a): FN_A held + BTN_DPAD_UP, gated on key.dpad.events",
         note=DPAD_NOTE, role_buttons=(_FNVOL_ROLES[0], None)),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Down"), "Volume down",
         True, "TASKS.md CC7 row; input_sense:448-453 (dc5f51a)",
         note=DPAD_NOTE, role_buttons=(_FNVOL_ROLES[0], None)),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Right"), "Brightness up",
         True, "TASKS.md CC7 row ('FN + D-pad left/right = brightness'); "
               "input_sense:454-460 (dc5f51a)",
         note=DPAD_NOTE, role_buttons=(_FNVOL_ROLES[0], None)),
    combo((btn_glyph(_FNVOL_BTN[0]), "D-pad Left"), "Brightness down",
         True, "TASKS.md CC7 row; input_sense:462-468 (dc5f51a)",
         note=DPAD_NOTE, role_buttons=(_FNVOL_ROLES[0], None)),
    combo((btn_glyph(_FNVOL_BTN[0]), "Volume +"), "Brightness up",
         True, "TASKS.md CC7 row ('FN_A/FN_B/FN_AB actions = brightness/LED/Wi-Fi'); "
               "input_sense:125,133,187-220 (dc5f51a): FN_A_ACTION_UP default 'brightness up'"),
    combo((btn_glyph(_FNVOL_BTN[0]), "Volume -"), "Brightness down",
         True, "TASKS.md CC7 row; input_sense:126,134,187-220 (dc5f51a)"),
    combo((btn_glyph("BTN_START"), "Volume +"), "Light control",
         True, "TASKS.md CC7 row ('FN_B = LED'); input_sense:127,135,221-232 (dc5f51a): "
               "FN_B_ACTION_UP default 'ledcontrol'. FN_B's own modifier is a SEPARATE "
               "button (key.function.b, default BTN_START on SM8250) from FN_A - held "
               "together with the volume key, not with FN_A"),
    combo((btn_glyph("BTN_START"), "Volume -"), "Lights off",
         True, "TASKS.md CC7 row; input_sense:128,136 (dc5f51a)"),
    combo((btn_glyph(_FNVOL_BTN[0]), btn_glyph("BTN_START"), "Volume +"), "Wi-Fi on",
         True, "TASKS.md CC7 row ('FN_AB = Wi-Fi'); input_sense:129,137,189-200 (dc5f51a)"),
    combo((btn_glyph(_FNVOL_BTN[0]), btn_glyph("BTN_START"), "Volume -"), "Wi-Fi off",
         True, "TASKS.md CC7 row; input_sense:130,138 (dc5f51a)"),
    # Extra finds from reading all of input_sense. Source only, and they use HOTKEY_A (L1), not
    # FUNCTION_A (the "FN" that isnt named for sure yet, see UNCONFIRMED).
    combo((btn_glyph("BTN_TL"), btn_glyph("BTN_EAST")), "Screenshot",
         False, "input_sense:476-484 (dc5f51a): HOTKEY_A_PRESSED + BTN_EAST"),
    combo((btn_glyph("BTN_TL"), btn_glyph("BTN_WEST")), "Show or hide the performance overlay",
         False, "input_sense:485-490 (dc5f51a): HOTKEY_A_PRESSED + BTN_WEST"),
    combo((btn_glyph("BTN_TL"), btn_glyph("BTN_NORTH")), "Open game guides",
         False, "input_sense:491-496 (dc5f51a): HOTKEY_A_PRESSED + BTN_NORTH"),
    combo((btn_glyph(_FNVOL_BTN[0]), btn_glyph("BTN_TOUCH")), "Show or hide the on-screen keyboard",
         False, "input_sense:470-475 (dc5f51a); gated on key.touchscreen.events",
         note=TOUCH_NOTE,
         role_buttons=(_FNVOL_ROLES[0], None)),
]

# ---------------------------------------------------------------------------
# RETROARCH: set at boot by setsettings.sh's configure_hotkeys() from the joypad autoconfig
# that matches the connected pad. Here that's InputPlumber's virtual DualSense, so the values
# come from that file. system.autohotkeys defaults to "1"
# (rocknix/config/system/configs/system.cfg:178), so this is on out of the box.
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
# The exact default for each key, so an override can be spotted (see
# apply_retroarch_overrides) without making up a glyph for an unknown raw button index.
RETROARCH_DEFAULT_RAW = {
    "input_enable_hotkey_btn": "8", "input_exit_emulator_btn": "9",
    "input_fps_toggle_btn": "3", "input_menu_toggle_btn": "2",
    "input_save_state_btn": "5", "input_load_state_btn": "4",
    "input_toggle_fast_forward_axis": "+5", "input_rewind_axis": "+2",
}

# ---------------------------------------------------------------------------
# Standalone emulators. A combo's `buttons` says "Guide" wherever the source itself does
# (Dolphin, dsperate). melonDS, Azahar and Flycast use a numeric or named modifier that isnt
# confirmed to be the same button as Guide, see UNCONFIRMED. None of these have live
# overrides, only retroarch.cfg and system.cfg key.* do.
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

# default page order when no game is running
CONTEXTS_ORDER = ["global", "retroarch", "melonds", "azahar", "dsperate", "dolphin",
                  "aethersx2", "flycast", "cemu"]

_COMBOS_BY_ID = {
    "global": GLOBAL_COMBOS, "retroarch": RETROARCH_COMBOS, "melonds": MELONDS_COMBOS,
    "azahar": AZAHAR_COMBOS, "dsperate": DSPERATE_COMBOS, "dolphin": DOLPHIN_COMBOS,
    "aethersx2": AETHERSX2_COMBOS, "flycast": FLYCAST_COMBOS, "cemu": CEMU_COMBOS,
}

# ES system short name -> the page to show first while that system's game runs. Not checked
# against ES's real es_systems.cfg, it follows the short names in
# research/DS1-dual-screen-emulators.md's default core table plus the usual ROCKNIX names. A
# miss just opens the sheet on Global instead of the right emulator, never a wrong combo.
SYSTEM_TO_CONTEXT = {
    "nds": "melonds", "n3ds": "azahar", "3ds": "azahar",
    "gc": "dolphin", "gamecube": "dolphin", "wii": "dolphin", "wiiu": "cemu",
    "ps2": "aethersx2", "dreamcast": "flycast", "dc": "flycast",
}

# Systems ROCKNIX runs on a standalone emulator with no hotkey file found (PPSSPP,
# mupen64plus-sa, see UNCONFIRMED). They stay out of the RetroArch fallback below, since
# RetroArch's hotkeys dont apply to a standalone emulator and showing them would be wrong, not
# just incomplete.
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
# Public: pages and ordering (pure)
# ---------------------------------------------------------------------------
def default_contexts():
    """id -> Context, straight from the tables above (no I/O)."""
    return {cid: Context(cid, CONTEXT_TITLES[cid], list(_COMBOS_BY_ID[cid]))
            for cid in CONTEXTS_ORDER}


def ordered_context_ids(running_system=None):
    """CONTEXTS_ORDER with the running game's emulator first, then Global, then the rest in the
    default order.

    Nothing running (running_system falsy) gives the plain default order.

    A system with its own standalone page in SYSTEM_TO_CONTEXT (nds/3ds/gc/wii/wiiu/ps2/dreamcast)
    puts that page first.

    Any other running system puts "retroarch" first, since ROCKNIX runs most systems (Genesis,
    SNES, NES, PS1, GBA, arcade, ...) on a libretro core, unless it's in SYSTEM_NOT_RETROARCH (a
    standalone build with no known hotkeys), where RetroArch's page would be wrong.
    """
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
# Only these key.* names (plus system.autohotkeys, read but not shown yet) ever get pulled
# from system.cfg. It also holds the Wi-Fi password and other secrets, and nothing else in it
# is ever read or logged.
SYSTEM_CFG_KEYS = frozenset([
    "key.hotkey.a", "key.hotkey.b", "key.hotkey.c",
    "key.function.a", "key.function.b",
    "key.dpad.events", "key.touchscreen.events",
])
RETROARCH_CFG_KEYS = frozenset(RETROARCH_DEFAULT_RAW)

# RetroArch joypad index -> the RP5's printed label, only for indices with evidence, from
# pressing every button once on the device (tests/fixtures/device/buttons-real-capture-2026-09-24.txt):
# A=1, B=0, X=2, Y=3, L1=4, R1=5, Select=8, Start=9, Home=10, L3=11, R3=12. L2/R2 are analog
# only, and Back is KEY_F1 on the InputPlumber keyboard, not the pad. Home + Start closes
# RetroArch (input_enable_hotkey_btn = 10). Anything else stays not decoded.
RA_INDEX_LABEL = {"0": "B", "1": "A", "2": "X", "3": "Y", "4": "L1", "5": "R1",
                  "8": "Select", "9": "Start", "10": "Home", "11": "L3", "12": "R3"}
# RETROARCH_COMBOS row -> the retroarch.cfg key of its second button (None for an axis row,
# left as written)
RETROARCH_ROW_KEYS = ["input_exit_emulator_btn", "input_fps_toggle_btn",
                      "input_menu_toggle_btn", "input_save_state_btn",
                      "input_load_state_btn", None, None]

_LINE_RE = re.compile(r'^([A-Za-z0-9_.]+)\s*=\s*"?([^"#]*?)"?\s*(?:#.*)?$')


def _read_text(path):
    """None if the file cant be read (wrong OS, not this device, missing, permissions). Never
    raises.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def parse_allowlisted(text, allowed_keys):
    """A line by line 'key = value' / 'key=value' parser that only returns keys in `allowed_keys`,
    a key not on the list isnt looked at long enough to get logged. Comments (# or ;), blank lines
    and broken lines are skipped, a corrupt file cant crash the sheet.
    """
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
    """{'system_cfg': {...}, 'retroarch_cfg': {...}}, each holding only allow-listed keys and empty
    (never missing) when the file couldnt be read. read_fn (path -> text|None) is for tests and
    defaults to a real file read that never raises.
    """
    read_fn = read_fn or _read_text
    return {
        "system_cfg": parse_allowlisted(read_fn(SYSTEM_CFG_PATH), SYSTEM_CFG_KEYS),
        "retroarch_cfg": parse_allowlisted(read_fn(RETROARCH_CFG_PATH), RETROARCH_CFG_KEYS),
    }


def _dpad_events_state(system_cfg):
    v = system_cfg.get("key.dpad.events")
    if v is None:
        return None  # unread (no file), unknown, not off
    return v not in ("", "0")


def _touch_events_state(system_cfg):
    v = system_cfg.get("key.touchscreen.events")
    if v is None:
        return None
    return v not in ("", "0")


def apply_overrides(contexts, overrides):
    """Returns a new pages dict with GLOBAL's role buttons worked out from system.cfg's live key.*
    values (the ROCKNIX default when a role has no override), and RetroArch's raw *_btn/*_axis
    values flagged, not translated (see UNCONFIRMED), when they differ from the default. Every
    other page comes back unchanged.
    """
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
            if note == DPAD_NOTE and dpad_on is not None:
                note = "" if dpad_on else DPAD_NOTE
            if note == TOUCH_NOTE and touch_on is not None:
                note = "" if touch_on else TOUCH_OFF_NOTE
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
        # Only note it if a combo can be tied to a specific overridden key. RETROARCH_COMBOS dont
        # carry role_buttons, so this adds one device-wide note on the first combo instead of pinning
        # an override on the wrong row.
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
    """The one call main.py and hotkeys_view.py need: (contexts, order) with live overrides applied
    where there's a source, ordered toward `running_system`'s emulator. Never raises, a failed
    read just shows the ROCKNIX defaults.
    """
    contexts = apply_overrides(default_contexts(), read_overrides(read_fn))
    order = ordered_context_ids(running_system)
    return contexts, order
