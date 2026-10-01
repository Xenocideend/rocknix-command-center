"""rp5deck's settings, one JSON file read at start.

    /storage/rp5deck/config.json   (RP5DECK_CONFIG overrides the path)

SCHEMA below is the one source of truth for every key (type, allowed values, default,
whether it needs a restart, group and label), and settings_view.py builds one control per
key from it.

main.py still reads cfg["output"] and cfg["es_output"]. Theyre always in what load()
returns, worked out from screens.command_center_screen / screens.es_screen
("builtin_bottom" -> DSI-1, "addon_top" -> DP-1), and save() never writes them.

An old flat file ({"output": ..., "es_output": ...}, no schema_version) gets turned into
screens.* the first time its loaded. Anything that isnt one of the two known outputs falls
back to the default with a note, never a crash.

Every key is checked on its own. A wrong type, out of range or unknown enum value falls
back to that key's default with a note, so a typo cant take the panel away. Keys we dont
know are kept and written back as they were.

The two screens.* keys always name opposite outputs. es_screen is the swap setting and
dual-screen-layout-and-power reads it too, so both have to agree on what a file means:
es_screen_from_raw() is that shared rule.

Saving writes a temp file, fsyncs it and renames it over the old one, so a crash mid-write
leaves the old file whole.
"""
import json
import os
import tempfile

import screen_map

SCHEMA_VERSION = 1
DEFAULT_PATH = "/storage/rp5deck/config.json"

# The only two screens rp5deck knows, and the output each one is on this device.
SCREEN_VALUES = ("addon_top", "builtin_bottom")
SCREEN_TO_OUTPUT = {"builtin_bottom": screen_map.CURRENT.bottom, "addon_top": screen_map.CURRENT.top}
OUTPUT_TO_SCREEN = {v: k for k, v in SCREEN_TO_OUTPUT.items()}

MEDIA_VALUES = ("video", "titleshot", "mix", "image", "marquee", "fanart",
                "cartridge", "boxback")

# Home's tile names in the order Home builds them (add a tile in Home.__init__ and here
# together, the tests check they match). "home.settings" can never be hidden since its the
# one screen that can undo everything else, and _validate() refuses it, not just the UI.
# HUD, Browser, Discord and YouTube App are tabs above Home now, so they arent grid tiles
# (HUD still shows in the over-a-game overlay, which has no tabs).
HOME_TILE_KEYS = ("home.mixer", "home.hotkeys", "home.clean", "home.settings",
                  "home.swap", "home.lights", "home.keyboard", "home.power",
                  "home.topscreen", "home.perf")  # top screen off, performance
HIDEABLE_TILE_KEYS = tuple(k for k in HOME_TILE_KEYS if k != "home.settings")

GROUPS = ("companion", "manuals", "command_center", "screens", "audio", "lights", "youtube",
          "steam", "battery", "appearance", "dualscreen")
GROUP_LABELS = {
    "companion": "Companion view",
    "manuals": "Manuals",
    "command_center": "Command Center",
    "screens": "Screens",
    "audio": "Audio",
    "lights": "Stick lights",
    "youtube": "YouTube",
    "steam": "Steam",
    "battery": "Battery",
    "appearance": "Appearance",
    "dualscreen": "Dual-screen keys",
}

# kept equal to palettes.PRESET_ORDER + ("custom",) by the tests, config.py doesnt import
# palettes.py
THEME_PRESET_VALUES = (
    "default", "high_contrast",
    "rp5_black", "rp5_white", "rp5_16bit", "rp5_gc", "rp5_yellow", "rp5_turquoise",
    "gameboy", "nes", "genesis", "playstation", "n64", "dreamcast",
    "custom",
)

# the panel size the Command Center lays out for, kept equal to screen_presets.VALUES by the
# tests
UI_RESOLUTION_VALUES = ("auto", "1920x1080", "1280x720", "1024x768", "720x720", "640x480")

# ES's A/B/X/Y prompt colours, separate from the screen theme, kept equal to
# button_colours.VALUES by the tests
BUTTON_COLOUR_VALUES = (
    "follow_theme", "use_theme", "rp5_16bit", "rp5_black", "rp5_white", "rp5_gc", "rp5_yellow",
    "rp5_turquoise", "super_famicom", "snes_us", "xbox", "playstation", "dreamcast",
    "gamecube", "steam_deck", "modern_grey",
)

# ---------------------------------------------------------------------------
# Schema, one entry per setting. manuals.viewer defaults to "native" (poppler is on the
# device). command_center.hardware_button defaults to "btn_back_f1" because the RP5 has no
# rear paddles (L2/R2 are analog). The paddle values stay for other devices, see
# _note_paddle_button().
#
# key_path addresses the nested dict. A new key needs a control in settings_view.py too, the
# tests check every key has one.
# ---------------------------------------------------------------------------
SCHEMA = [
    # -- Companion view ------------------------------------------------
    {"key_path": ("companion", "media_priority"), "type": "array_enum",
     "values": MEDIA_VALUES, "default": list(MEDIA_VALUES), "restart": False,
     "group": "companion", "label": "Preferred media order"},
    {"key_path": ("companion", "play_video"), "type": "bool", "default": True,
     "restart": False, "group": "companion", "label": "Play video"},
    {"key_path": ("companion", "video_audio"), "type": "enum",
     "values": ("muted", "unmuted", "follow_es"), "default": "follow_es",
     "restart": False, "group": "companion", "label": "Video audio"},
    {"key_path": ("companion", "video_start_delay_ms"), "type": "int",
     "range": (0, 3000), "default": 500, "restart": False,
     "group": "companion", "label": "Video start delay"},
    {"key_path": ("companion", "video_loop"), "type": "bool", "default": True,
     "restart": False, "group": "companion", "label": "Loop video"},
    {"key_path": ("companion", "image_fit"), "type": "enum",
     "values": ("fill", "fit", "crop"), "default": "fit", "restart": False,
     "group": "companion", "label": "Image fit"},
    {"key_path": ("companion", "background_dim"), "type": "float",
     "range": (0.0, 1.0), "default": 0.15, "restart": False,
     "group": "companion", "label": "Background dim"},
    {"key_path": ("companion", "background_blur"), "type": "bool",
     "default": False, "restart": False, "group": "companion",
     "label": "Background blur"},
    {"key_path": ("companion", "show_metadata", "title"), "type": "bool",
     "default": True, "restart": False, "group": "companion",
     "label": "Show title"},
    {"key_path": ("companion", "show_metadata", "year"), "type": "bool",
     "default": True, "restart": False, "group": "companion",
     "label": "Show year"},
    {"key_path": ("companion", "show_metadata", "developer"), "type": "bool",
     "default": True, "restart": False, "group": "companion",
     "label": "Show developer"},
    {"key_path": ("companion", "show_metadata", "players"), "type": "bool",
     "default": True, "restart": False, "group": "companion",
     "label": "Show players"},
    {"key_path": ("companion", "show_metadata", "rating"), "type": "bool",
     "default": False, "restart": False, "group": "companion",
     "label": "Show rating"},
    {"key_path": ("companion", "show_metadata", "description"), "type": "bool",
     "default": False, "restart": False, "group": "companion",
     "label": "Show description"},
    {"key_path": ("companion", "show_metadata", "playtime"), "type": "bool",
     "default": False, "restart": False, "group": "companion",
     "label": "Show playtime"},
    {"key_path": ("companion", "idle_mode"), "type": "enum",
     "values": ("clock", "slideshow", "blank"), "default": "clock",
     "restart": False, "group": "companion", "label": "Idle behaviour"},
    {"key_path": ("companion", "idle_slideshow_interval_s"), "type": "int",
     "range": (5, 60), "default": 10, "restart": False,
     "group": "companion", "label": "Idle slideshow interval"},
    # what the bottom screen shows during a single-screen game (DS/3DS/Wii U games put their own
    # window here and go HIDDEN before this is asked). It matches the ES-DE Companion app's
    # "Game Playing Screen Behavior" (On/Dim/Off/Manual/Guide).
    {"key_path": ("companion", "in_game_display"), "type": "enum",
     "values": ("art", "dim", "off", "manual", "hud", "clock", "slideshow"),
     "default": "art", "restart": False, "group": "companion",
     "label": "During a game, show"},
    {"key_path": ("companion", "brightness_pct"), "type": "int",
     "range": (5, 100), "step": 5, "default": 80, "restart": False,
     "group": "companion", "label": "Bottom-screen brightness"},
    # The colour behind the system logo on the carousel. "theme" uses the ES theme's declared
    # colour (free but can be wrong), "sample" takes a small read-only grim shot of the top
    # screen (exact, one shot per system change, never during a game), "off" is black.
    # "sample" is the default because PiStation-X declares white while it actually draws navy.
    {"key_path": ("companion", "system_bg_source"), "type": "enum",
     "values": ("theme", "sample", "off"), "default": "sample",
     "restart": False, "group": "companion", "label": "System background colour"},

    # -- Manuals ---------------------------------------------------------
    {"key_path": ("manuals", "open_mode"), "type": "enum",
     "values": ("on_tap", "auto_on_game_start"), "default": "on_tap",
     "restart": False, "group": "manuals", "label": "Open manual"},
    {"key_path": ("manuals", "viewer"), "type": "enum",
     "values": ("native", "firefox_pdfjs"), "default": "native",
     "restart": True, "group": "manuals", "label": "Viewer"},

    # -- Command Center ----------------------------------------------
    {"key_path": ("command_center", "swipe_down_enabled"), "type": "bool",
     "default": True, "restart": False, "group": "command_center",
     "label": "Swipe down to open"},
    {"key_path": ("command_center", "swipe_sensitivity"), "type": "enum",
     "values": ("low", "medium", "high"), "default": "medium",
     "restart": False, "group": "command_center", "label": "Swipe sensitivity"},
    {"key_path": ("command_center", "hardware_button"), "type": "enum",
     "values": ("none", "btn_back_f1", "btn_c_paddle", "btn_z_paddle"),
     "default": "btn_back_f1", "restart": True, "group": "command_center",
     "label": "Hardware button"},
    {"key_path": ("command_center", "auto_close_timeout_s"), "type": "int",
     "range": (0, 120), "default": 0, "restart": False,
     "group": "command_center", "label": "Auto-close timeout"},
    # the small pull tab in the corner of the game screen that opens the Command Center over the
    # game. Off means no tab, the Back button still opens it there.
    {"key_path": ("command_center", "game_screen_tab"), "type": "bool",
     "default": True, "restart": False, "group": "command_center",
     "label": "Pull tab on game screen"},
    # The Command Center over an emulator's second screen. overlay_on_hidden lets the hardware
    # button open it while a DS/3DS/Wii U window owns this screen. corner_handle is a small tap
    # spot in one corner while that window is up (it takes those pixels from the game's touch area).
    {"key_path": ("command_center", "overlay_on_hidden"), "type": "bool",
     "default": True, "restart": False, "group": "command_center",
     "label": "Open over DS/3DS screen"},
    {"key_path": ("command_center", "corner_handle"), "type": "enum",
     "values": ("off", "top-left", "top-right", "bottom-left", "bottom-right"),
     "default": "off", "restart": False, "group": "command_center",
     "label": "Corner handle"},
    # Home's own Edit tiles mode is the real control for these two, so neither is in WIRED.
    # tile_order has to hold every tile, a hidden one keeps its slot for when its shown again.
    {"key_path": ("command_center", "tile_order"), "type": "array_enum",
     "values": HOME_TILE_KEYS, "default": list(HOME_TILE_KEYS), "restart": False,
     "group": "command_center", "label": "Tile order"},
    {"key_path": ("command_center", "hidden_tiles"), "type": "tile_set",
     "values": HIDEABLE_TILE_KEYS, "default": [], "restart": False,
     "group": "command_center", "label": "Hidden tiles"},

    # -- Screens -----------------------------------------------------------
    # es_screen shows as one toggle, "Swap screens" (on = ES and games on the built-in panel,
    # the Command Center on the add-on). command_center_screen is always the opposite and has no
    # control. Its live: dual-screen-layout-and-power re-reads the file every poll and rp5deck
    # follows ES.
    {"key_path": ("screens", "es_screen"), "type": "enum",
     "values": SCREEN_VALUES, "default": "addon_top", "restart": False,
     "group": "screens", "label": "Swap screens"},
    {"key_path": ("screens", "command_center_screen"), "type": "enum",
     "values": SCREEN_VALUES, "default": "builtin_bottom", "restart": True,
     "group": "screens", "label": "Command Center screen"},
    # bottom is the live backlight (read every time Settings opens, never applied at start so
    # ROCKNIX's own brightness stays in charge until you move it). top dims the add-on in
    # software, applied at start. match keeps top = bottom x match_ratio.
    {"key_path": ("screens", "bottom_brightness"), "type": "int",
     "range": (5, 100), "default": 50, "restart": False,
     "group": "screens", "label": "Bottom screen brightness"},
    {"key_path": ("screens", "top_brightness"), "type": "int",
     "range": (20, 100), "default": 100, "restart": False,
     "group": "screens", "label": "Top screen brightness"},
    {"key_path": ("screens", "match_brightness"), "type": "bool",
     "default": False, "restart": False,
     "group": "screens", "label": "Match brightness"},
    {"key_path": ("screens", "match_ratio"), "type": "float",
     "range": (0.2, 20.0), "default": 1.0, "restart": False,
     "group": "screens", "label": "Match ratio"},
    {"key_path": ("screens", "ui_resolution"), "type": "enum",
     "values": UI_RESOLUTION_VALUES, "default": "auto", "restart": False,
     "group": "screens", "label": "Screen size preset"},

    # -- Audio -------------------------------------------------------------
    {"key_path": ("audio", "volume_step_pct"), "type": "int",
     "range": (1, 20), "default": 5, "restart": False,
     "group": "audio", "label": "Volume step"},
    {"key_path": ("audio", "show_volume_overlay"), "type": "bool",
     "default": True, "restart": False, "group": "audio",
     "label": "Show volume overlay"},

    # -- Stick lights -------------------------------------------------------
    # rgb_leds.py and rgb_view.py own these, config.py only stores and checks them (never
    # touches system.cfg). None are in WIRED, their control is the Stick lights sheet.
    {"key_path": ("lights", "mode"), "type": "enum",
     "values": ("rocknix", "off", "colour"), "default": "rocknix",
     "restart": False, "group": "lights", "label": "Stick lights"},
    {"key_path": ("lights", "linked"), "type": "bool", "default": True,
     "restart": False, "group": "lights",
     "label": "Same colour on both sticks"},
    {"key_path": ("lights", "left"), "type": "hex_color", "default": "#ff0000",
     "restart": False, "group": "lights", "label": "Left stick colour"},
    {"key_path": ("lights", "right"), "type": "hex_color", "default": "#ff0000",
     "restart": False, "group": "lights", "label": "Right stick colour"},
    {"key_path": ("lights", "brightness"), "type": "int", "range": (0, 255),
     "default": 200, "restart": False, "group": "lights",
     "label": "Brightness"},

    # -- YouTube ----------------------------------------------------------------
    # the YouTube TV strip hides after this many seconds without a touch so leanback fills the
    # whole panel. 0 turns it off and the strip keeps its fixed spot like the other BAR apps.
    {"key_path": ("youtube", "tv_bar_hide_s"), "type": "int",
     "range": (0, 30), "default": 4, "restart": False,
     "group": "youtube", "label": "Auto-hide TV controls"},
    # Swipe direction for YouTube. True is phone-style (content follows the finger, a swipe left
    # sends "right"), False sends the swipe's own direction. Not tried with a real finger on
    # leanback yet.
    {"key_path": ("youtube", "tv_swipe_natural"), "type": "bool",
     "default": True, "restart": False, "group": "youtube",
     "label": "Natural swipe direction"},

    # -- Steam ------------------------------------------------------------------
    # What the bottom screen shows while a Steam game runs. "same" follows companion.in_game_display like
    # every other game, the rest are that setting's own choices (no "manual", Steam games have none).
    {"key_path": ("steam", "in_game_display"), "type": "enum",
     "values": ("same", "art", "dim", "off", "hud", "clock", "slideshow"),
     "default": "same", "restart": False, "group": "steam",
     "label": "During a Steam game, show"},

    # -- Battery (Safe charge) --------------------------------------------------
    # rocknix-config/battery-charge-limit is what actually applies this at boot and live,
    # charge_limit.py follows its file and sysfs rules. 85 is its default. 50-95 in steps of 5,
    # and 100 (no limit) is only ever the toggle's off state so "no limit" means one thing.
    {"key_path": ("battery", "safe_charge_enabled"), "type": "bool",
     "default": True, "restart": False, "group": "battery",
     "label": "Safe charge"},
    # has to match charge_limit.py's MIN_END/MAX_END/STEP/DEFAULT_END (not imported, config.py
    # stays dependency free), the tests check it
    {"key_path": ("battery", "safe_charge_end_pct"), "type": "int",
     "range": (50, 95), "step": 5, "default": 85, "restart": False,
     "group": "battery", "label": "Stop charging at"},

    # -- Appearance (colour themes) -----------------------------------------------
    # palettes.py owns the colours and doesnt import config.py. The preset names are spelled out
    # here by hand and the tests check they match palettes.PRESET_ORDER.
    {"key_path": ("appearance", "theme_preset"), "type": "enum",
     "values": THEME_PRESET_VALUES, "default": "default",
     "restart": False, "group": "appearance", "label": "Colour theme"},
    {"key_path": ("appearance", "button_colours"), "type": "enum",
     "values": BUTTON_COLOUR_VALUES, "default": "follow_theme",
     "restart": False, "group": "appearance", "label": "Button colours"},
    # the custom colours arent in WIRED, their control is the Custom colours sheet (a hex colour
    # has no Settings row and shouldnt)
    {"key_path": ("appearance", "custom_accent"), "type": "hex_color",
     "default": "#3d8bfd", "restart": False, "group": "appearance",
     "label": "Custom accent colour"},
    {"key_path": ("appearance", "custom_bg"), "type": "hex_color",
     "default": "#12141c", "restart": False, "group": "appearance",
     "label": "Custom background colour"},
    {"key_path": ("appearance", "custom_text"), "type": "hex_color",
     "default": "#f4f5f7", "restart": False, "group": "appearance",
     "label": "Custom text colour"},

    # -- Dual-screen per-game keys ---------------------------------------------------
    # dualscreen_keys.py always checks two generic system.cfg keys (3ds.screen_layout,
    # wiiu.gamepad_enabled). A DS game can need its own override, but which game and what value
    # depends on someone's own collection, so it lives here, empty by default.
    # Each entry: {"key": the exact system.cfg key, "line": the exact "key=value" text restore()
    # appends, "rom": optional path under /storage/roms, the key only counts while that file
    # exists}. Hand-edited, not in WIRED.
    {"key_path": ("dualscreen", "extra_keys"), "type": "key_line_list",
     "default": [], "restart": False, "group": "dualscreen",
     "label": "Extra per-game keys"},
]

FIELD_BY_PATH = {f["key_path"]: f for f in SCHEMA}


# ---------------------------------------------------------------------------
# WIRED: the SCHEMA keys that actually change something, each with a real reader somewhere
# outside config.py and settings_view.py. settings_view.py only builds a control for these,
# so a key can be added before its reader without showing a fake control. The tests check
# WIRED and "has a control" match both ways.
#
# command_center_screen stays out since its derived, a control for it could put both
# screens on the same output.
# ---------------------------------------------------------------------------
WIRED = frozenset([
    ("companion", "media_priority"),  # companion CompanionController.refresh()
    ("companion", "play_video"),  # companion video_allowed() / refresh()
    ("companion", "video_audio"),  # companion _start_video()
    ("companion", "video_start_delay_ms"),  # companion _update_video()
    ("companion", "video_loop"),  # companion _start_video()
    ("companion", "image_fit"),  # companion video_frame_ready() / refresh()
    ("companion", "background_dim"),  # companion _resolved()
    ("companion", "show_metadata", "title"),  # companion metadata_lines()
    ("companion", "show_metadata", "year"),  # companion metadata_lines()
    ("companion", "show_metadata", "developer"),  # companion metadata_lines()
    ("companion", "show_metadata", "players"),  # companion metadata_lines()
    ("companion", "show_metadata", "rating"),  # companion metadata_lines()
    ("companion", "show_metadata", "description"),  # companion metadata_lines()
    ("companion", "idle_mode"),  # companion _show_idle() / _arm_slides()
    ("companion", "idle_slideshow_interval_s"),  # companion _arm_slides() / _ingame_slide_tick()
    ("companion", "show_metadata", "playtime"),  # companion metadata_lines()
    ("companion", "in_game_display"),  # companion _in_game_mode()
    ("manuals", "open_mode"),  # companion's auto-open check
    ("command_center", "swipe_down_enabled"),  # summon PullDownStateMachine.swipe_down_enabled
    ("command_center", "swipe_sensitivity"),  # summon swipe_kwargs_for()
    ("command_center", "hardware_button"),  # summon SummonButtonReader
    ("command_center", "auto_close_timeout_s"),  # summon PullDownStateMachine timeout
    ("command_center", "game_screen_tab"),  # cc_overlay OverlayApp._tab_enabled()
    ("command_center", "overlay_on_hidden"),  # hidden_overlay enabled() / takes_summon()
    ("command_center", "corner_handle"),  # hidden_overlay corner(), live via main.App.on_setting
    ("audio", "show_volume_overlay"),  # main.App's volume overlay
    ("screens", "bottom_brightness"),  # brightness.BottomBacklight
    ("screens", "top_brightness"),  # brightness.TopDim
    ("screens", "match_brightness"),  # main.App match_ratio
    ("screens", "ui_resolution"),  # screen_presets.layout_size via main.App._resize
    ("screens", "es_screen"),  # dual-screen-layout-and-power moves ES, screen_swap.read_setting()
    ("companion", "system_bg_source"),  # companion's system background (theme_colour)
    ("youtube", "tv_bar_hide_s"),  # bar_autohide.BarAutoHide.timeout_s
    ("youtube", "tv_swipe_natural"),  # web_tiles.YtAppSession.swipe_natural
    ("steam", "in_game_display"),  # companion _in_game_raw() for a Steam game
    ("battery", "safe_charge_enabled"),  # charge_limit.apply()
    ("battery", "safe_charge_end_pct"),  # charge_limit.apply()
    ("appearance", "theme_preset"),  # palettes.apply_theme(), the game-screen overlay re-reads it on its own poll
    ("appearance", "button_colours"),  # button_colours.apply()
    # Not in WIRED on purpose, each has its own dedicated sheet instead of a generic row:
    # the custom colours (Custom colours sheet), lights.* (Stick lights sheet) and
    # tile_order / hidden_tiles (Home's Edit tiles). They still read and write through
    # get_value()/set_value()/save_changes().
])


def fields_in_group(group):
    return [f for f in SCHEMA if f["group"] == group]


def field_for(key_path):
    return FIELD_BY_PATH.get(tuple(key_path))


def other_screen(value):
    return "addon_top" if value == "builtin_bottom" else "builtin_bottom"


# ---------------------------------------------------------------------------
# Nested-dict helpers
# ---------------------------------------------------------------------------
def _copy_default(v):
    return list(v) if isinstance(v, list) else v


def _has(d, path):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


def _get(d, path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _set(d, path, value):
    cur = d
    for k in path[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[path[-1]] = value


def _schema_tree():
    """Nested dict mirroring SCHEMA's key paths (True at every leaf), to tell a key we know from
    an unknown one to keep as is.
    """
    tree = {}
    for f in SCHEMA:
        node = tree
        path = f["key_path"]
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = True
    return tree


_TREE = _schema_tree()


def defaults():
    """A fresh dict of every default plus schema_version and the derived output/es_output, what
    load() returns with no file.
    """
    cfg = {"schema_version": SCHEMA_VERSION}
    for f in SCHEMA:
        _set(cfg, f["key_path"], _copy_default(f["default"]))
    _add_legacy(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate(field, value):
    """Return (ok, value). ok False means: use the field's default instead."""
    t = field["type"]
    if t == "bool":
        return (isinstance(value, bool), value)
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return (False, value)
        lo, hi = field.get("range", (None, None))
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            return (False, value)
        return (True, value)
    if t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (False, value)
        value = float(value)
        lo, hi = field.get("range", (None, None))
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            return (False, value)
        return (True, value)
    if t == "enum":
        return (isinstance(value, str) and value in field["values"], value)
    if t == "hex_color":
        if not isinstance(value, str):
            return (False, value)
        v = value.strip()
        if len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
            return (True, v.lower())
        return (False, value)
    if t == "array_enum":
        if not isinstance(value, list):
            return (False, value)
        vals = set(field["values"])
        seen = set()
        out = []
        for v in value:
            if not isinstance(v, str) or v not in vals or v in seen:
                return (False, value)
            seen.add(v)
            out.append(v)
        if seen != vals:                # must be a full permutation, nothing dropped
            return (False, value)
        return (True, out)
    if t == "tile_set":
        # Like array_enum's check but a subset (dropping members is the point). Anything not in
        # field["values"] is refused, which is how "home.settings" can never end up hidden even from
        # a hand-edited file.
        if not isinstance(value, list):
            return (False, value)
        vals = set(field["values"])
        seen = set()
        out = []
        for v in value:
            if not isinstance(v, str) or v not in vals or v in seen:
                return (False, value)
            seen.add(v)
            out.append(v)
        return (True, out)
    if t == "key_line_list":
        # dualscreen.extra_keys: a list of {"key", "line", "rom" (optional)}. key and line are
        # non-empty strings, key is unique (restore() maps key to line), and rom is a relative path
        # with no leading "/" or "..", so a hand-edited file cant point the check outside ROMS_DIR.
        if not isinstance(value, list):
            return (False, value)
        out = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                return (False, value)
            key, line = item.get("key"), item.get("line")
            if not isinstance(key, str) or not key or not isinstance(line, str) or not line:
                return (False, value)
            if key in seen:
                return (False, value)
            seen.add(key)
            entry = {"key": key, "line": line}
            rom = item.get("rom")
            if rom is not None:
                if (not isinstance(rom, str) or not rom or rom.startswith("/")
                        or rom.startswith("\\") or ".." in rom.replace("\\", "/").split("/")):
                    return (False, value)
                entry["rom"] = rom
            out.append(entry)
        return (True, out)
    return (False, value)  # an unknown type in SCHEMA itself, refuse it dont guess


def _merge_unknown(data, cfg, tree):
    """Copies any key SCHEMA doesnt know into cfg as is (recursing into partly known branches),
    so a future or foreign key survives load()/save().
    """
    if not isinstance(data, dict):
        return
    for k, v in data.items():
        if k not in tree:
            cfg[k] = v
        elif tree[k] is not True:
            if not isinstance(cfg.get(k), dict):
                cfg[k] = {}
            if isinstance(v, dict):
                _merge_unknown(v, cfg[k], tree[k])


def _validate_tree(data, notes):
    cfg = defaults()
    del cfg["output"], cfg["es_output"]     # recomputed at the end, never merged as "unknown"
    for f in SCHEMA:
        if _has(data, f["key_path"]):
            raw = _get(data, f["key_path"])
            ok, val = _validate(f, raw)
            if ok:
                _set(cfg, f["key_path"], val)
            else:
                notes.append("invalid %s: using default" % ".".join(f["key_path"]))
    if isinstance(data, dict):
        _merge_unknown(data, cfg, _TREE)
    cfg["schema_version"] = SCHEMA_VERSION
    return cfg


def _fixup_screens(cfg, notes):
    es = _get(cfg, ("screens", "es_screen"))
    cc = _get(cfg, ("screens", "command_center_screen"))
    if es == cc:
        _set(cfg, ("screens", "command_center_screen"), other_screen(es))
        notes.append("screens.es_screen and screens.command_center_screen were "
                      "equal: reset command_center_screen to the other output")


def es_screen_from_raw(raw):
    """The shared rule for what a file means for the swap. dual-screen-layout-and-power's
    rp5_es_screen() does exactly this too (test_sw1_092.py runs both on the same files).
    "builtin_bottom" only when raw is an object whose "screens" is an object whose
    "es_screen" is literally "builtin_bottom". Anything else (no file, bad JSON, a legacy flat
    file, a typo) is "addon_top", the unswapped default.
    """
    if isinstance(raw, dict):
        screens = raw.get("screens")
        if isinstance(screens, dict) and screens.get("es_screen") == "builtin_bottom":
            return "builtin_bottom"
    return "addon_top"


def _screens_follow_contract(cfg, raw, notes):
    """es_screen comes from es_screen_from_raw(raw) and command_center_screen is its opposite.
    Runs last so load() always reports what dual-screen-layout-and-power will actually do with
    the file. The one case that differs is an old flat file that migrates to the swapped combo,
    the daemon only reads screens.es_screen so ES stays on the add-on and so do we (with a note).
    """
    want_es = es_screen_from_raw(raw)
    es = _get(cfg, ("screens", "es_screen"))
    cc = _get(cfg, ("screens", "command_center_screen"))
    if es != want_es:
        notes.append("screens.es_screen %r not in the file as 092 reads it: using %r"
                     % (es, want_es))
    _set(cfg, ("screens", "es_screen"), want_es)
    _set(cfg, ("screens", "command_center_screen"), other_screen(want_es))


PADDLE_HARDWARE_BUTTONS = ("btn_c_paddle", "btn_z_paddle")


def _note_paddle_button(cfg, notes):
    """The RP5 has no rear paddles, so the default is "btn_back_f1". A stored paddle value is
    still honoured as is (another device might have them), it just leaves a note.
    """
    v = _get(cfg, ("command_center", "hardware_button"))
    if v in PADDLE_HARDWARE_BUTTONS:
        notes.append("command_center.hardware_button=%r: this device (RP5) has no rear "
                     "paddles - keeping the stored value" % v)


def _add_legacy(cfg):
    cfg["output"] = SCREEN_TO_OUTPUT[_get(cfg, ("screens", "command_center_screen"))]
    cfg["es_output"] = SCREEN_TO_OUTPUT[_get(cfg, ("screens", "es_screen"))]


def _migrate(data, notes):
    """Turns the old flat {"output": ..., "es_output": ...} shape into screens.* the first time an
    old file is loaded. Returns a new dict and never changes `data`. Does nothing once
    schema_version is there.
    """
    if not isinstance(data, dict) or "schema_version" in data:
        return data
    out = dict(data)
    old_output = out.pop("output", None)
    old_es = out.pop("es_output", None)
    if old_output is None and old_es is None:
        return out
    screens = dict(out.get("screens") or {})
    migrated = False
    for key, old in (("command_center_screen", old_output), ("es_screen", old_es)):
        if key in screens or old is None:
            continue
        if isinstance(old, str) and old in OUTPUT_TO_SCREEN:
            screens[key] = OUTPUT_TO_SCREEN[old]
            migrated = True
        else:
            notes.append("could not migrate legacy %s=%r: using default"
                         % ("output" if key == "command_center_screen" else "es_output", old))
    if screens:
        out["screens"] = screens
    if migrated:
        notes.append("migrated legacy output/es_output to screens.*")
    return out


# renamed tiles keep their place and hidden state in a saved layout
RENAMED_TILES = {"home.sleep": "home.power"}  # Sleep moved into the Power sheet


def _rename_tiles(names):
    return [RENAMED_TILES.get(n, n) for n in names] if isinstance(names, list) else names


def _prune_hidden_tiles(data, notes):
    """A saved hidden_tiles naming a tile that no longer exists keeps the rest of the hidden set
    instead of failing as a whole. Anything malformed is left for _validate. Never mutates.
    """
    try:
        hidden = _rename_tiles(data["command_center"]["hidden_tiles"])
    except (KeyError, TypeError):
        return data
    if not isinstance(hidden, list) or not all(isinstance(v, str) for v in hidden):
        return data
    kept = [v for v in hidden if v in HIDEABLE_TILE_KEYS]
    if len(kept) == len(hidden) and hidden == data["command_center"]["hidden_tiles"]:
        return data
    out = dict(data)
    cc = dict(out["command_center"])
    cc["hidden_tiles"] = kept
    out["command_center"] = cc
    notes.append("hidden_tiles: dropped tiles that no longer exist: %s"
                 % ", ".join(v for v in hidden if v not in HIDEABLE_TILE_KEYS))
    return out


def _extend_tile_order(data, notes):
    """A saved tile_order from before a new tile existed gets the new tiles added at the end
    instead of failing the every-tile check (which would reset your layout). Unknown names are
    dropped, anything malformed is left for _validate. Never changes `data`.
    """
    try:
        order = _rename_tiles(data["command_center"]["tile_order"])
    except (KeyError, TypeError):
        return data
    if not isinstance(order, list) or not all(isinstance(v, str) for v in order):
        return data
    known = [v for v in order if v in HOME_TILE_KEYS]
    if len(set(known)) != len(known):
        return data
    missing = [k for k in HOME_TILE_KEYS if k not in known]
    if not missing and len(known) == len(order) and \
            order == data["command_center"]["tile_order"]:
        return data
    out = dict(data)
    cc = dict(out["command_center"])
    cc["tile_order"] = known + missing
    out["command_center"] = cc
    if missing:
        notes.append("tile_order: new tiles added at the end: %s" % ", ".join(missing))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def config_path(env=None):
    env = os.environ if env is None else env
    return env.get("RP5DECK_CONFIG") or DEFAULT_PATH


def get_value(cfg, key_path):
    f = field_for(key_path)
    default = _copy_default(f["default"]) if f else None
    return _get(cfg, tuple(key_path), default)


def set_value(cfg, key_path, value):
    """Sets one schema value in place. Invalid values are refused (cfg unchanged, returns False).
    Setting one screens.* key flips the other and refreshes output/es_output to match.
    """
    key_path = tuple(key_path)
    f = field_for(key_path)
    if f is None:
        return False
    ok, val = _validate(f, value)
    if not ok:
        return False
    _set(cfg, key_path, val)
    if key_path == ("screens", "es_screen"):
        _set(cfg, ("screens", "command_center_screen"), other_screen(val))
    elif key_path == ("screens", "command_center_screen"):
        _set(cfg, ("screens", "es_screen"), other_screen(val))
    if key_path[0] == "screens":
        _add_legacy(cfg)
    return True


def load(path=None, env=None):
    """Returns (config dict, note). The note says where the values came from or what was wrong
    with the file. The dict always has every key (defaults for anything missing or bad) plus
    output / es_output for main.py.
    """
    path = path or config_path(env)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return defaults(), "no %s: defaults" % path
    except (OSError, ValueError) as e:
        return defaults(), "IGNORED %s (%s): defaults" % (path, e)
    if not isinstance(raw, dict):
        return defaults(), "IGNORED %s (top level is not an object): defaults" % path
    notes = []
    data = _migrate(raw, notes)
    data = _extend_tile_order(data, notes)
    data = _prune_hidden_tiles(data, notes)
    cfg = _validate_tree(data, notes)
    _fixup_screens(cfg, notes)
    _screens_follow_contract(cfg, raw, notes)
    _note_paddle_button(cfg, notes)
    _add_legacy(cfg)
    note = "loaded %s" % path
    if notes:
        note += " (%s)" % "; ".join(notes)
    return cfg, note


def _atomic_write(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic on POSIX and on Windows (3.3+)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(cfg, path=None, env=None):
    """Checks cfg and writes it atomically, returns (written dict, notes). Never writes the
    derived output/es_output. Raises if the write fails, the old file is untouched either way.
    """
    path = path or config_path(env)
    notes = []
    data = dict(cfg)
    data.pop("output", None)
    data.pop("es_output", None)
    out = _validate_tree(data, notes)
    _fixup_screens(out, notes)
    text = json.dumps(out, indent=2, sort_keys=True) + "\n"
    _atomic_write(path, text)
    _add_legacy(out)
    return out, notes


def save_changes(changes, path=None, env=None):
    """Read-modify-write of only the given keys, {key_path: value}. Loads the file as it is right
    now, applies each change through set_value() (a bad value is skipped with a note) and saves
    atomically. Returns (written dict, notes).

    Two processes write this file (the panel and the game-screen overlay, whose "Swap screens"
    writes es_screen), so saving a whole older copy would put the other one's change back.
    Writing only what changed keeps both.
    """
    path = path or config_path(env)
    cfg, _note = load(path)
    notes = []
    for key_path, value in dict(changes).items():
        if not set_value(cfg, key_path, value):
            notes.append("refused %s=%r: invalid" % (".".join(key_path), value))
    out, save_notes = save(cfg, path)
    return out, notes + save_notes
