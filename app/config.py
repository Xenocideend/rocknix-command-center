"""config - rp5deck settings, from ONE JSON file read at start.

    /storage/rp5deck/config.json   (override the path with RP5DECK_CONFIG)

Schema v1 (see research/R6-settings.md): settings are grouped under
companion / manuals / command_center / screens / audio - SCHEMA below is the
single source of truth for every key (type, allowed values/range, default,
whether it needs an rp5deck restart, a group and a label) and is what
settings_view.py walks to build one control per key.

Backward compatibility (mandatory - main.py is unchanged): main.py reads
cfg["output"] (the wl_output the Command Center's layer surface binds to)
and cfg["es_output"] (the screen EmulationStation / games live on) exactly
as before. Both keys are still always present in the dict `load()` returns,
derived from screens.command_center_screen / screens.es_screen
("builtin_bottom" -> "DSI-1", "addon_top" -> "DP-1"). They are read-only
conveniences: save() never persists them, they are recomputed every load.

Migration: a file in today's shape ({"output": ..., "es_output": ...}, no
"schema_version") is turned into screens.* the first time it is loaded.
Only the two known connector names round-trip through the new two-screen
enum; anything else (a hand-edited third output) cannot be represented and
is dropped back to the default, with a note - never a crash.

Validation, key by key: wrong type, out of range, or an unlisted enum value
falls back to that key's own default alone, with a note recorded - a typo
in a settings file must not take the panel away. Unknown keys (a field this
version of rp5deck does not know about yet, or one it used to but no longer
carries meaning for) are preserved as-is and written back unchanged.

screens.es_screen and screens.command_center_screen must name opposite
outputs (there are only two screens); if a file ever has them equal, the
Command Center screen is reset to the other one, both at load and at save.

SW1 (screen swap, = B13): screens.es_screen is THE swap setting, and it has
two readers that must agree byte-for-byte on what a file means - this
module and 092-dual-screen-persist, which moves ES. The shared contract is
es_screen_from_raw(): "builtin_bottom" only if the file is a JSON object
whose screens.es_screen is literally "builtin_bottom"; anything else (no
file, bad JSON, a legacy flat file, a typo) is "addon_top", the unswapped
default. load() applies exactly that to es_screen and derives
command_center_screen as its opposite, so a legacy file's migrated screens
(which 092 cannot see) never disagree with where 092 actually puts ES. The
old RV1-M4 guard (_lock_screens_to_default, "ignore any non-default
combination") is gone: rp5deck now binds its layer surface to whichever
output ES is really on the other side of (screen_swap.py), so no stored
value can hide the Command Center. WIRED (below) lists every schema key
that has a real consumer; settings_view.py only shows a control for a WIRED
key; command_center_screen stays out of it (it is derived, never chosen).

Save is atomic: a temp file is written in the same directory, flushed and
fsync'd, then renamed over the target - a crash mid-write leaves the old
file intact, never a half-written one.

Environment variables (RP5DECK_AUDIO_DRYRUN, RP5DECK_LOG_DIR, RP5DECK_RUN_DIR,
RP5DECK_DEBUG) are process plumbing set by the supervisor or a test harness,
not user settings, and stay in the environment.
"""
import json
import os
import tempfile

SCHEMA_VERSION = 1
DEFAULT_PATH = "/storage/rp5deck/config.json"

# The only two screens rp5deck knows about, and the wl_output each is wired
# to on this device (DESIGN.md "Screen terms").
SCREEN_VALUES = ("addon_top", "builtin_bottom")
SCREEN_TO_OUTPUT = {"builtin_bottom": "DSI-1", "addon_top": "DP-1"}
OUTPUT_TO_SCREEN = {v: k for k, v in SCREEN_TO_OUTPUT.items()}

MEDIA_VALUES = ("video", "titleshot", "mix", "image", "marquee", "fanart",
                "cartridge", "boxback")

# Tile customisation: screens.Home's tile names, in its construction order -
# the canonical set both screens.py and config.py agree on (add a tile in
# Home.__init__, add its name here in the same pass; tests/test_config.py
# and the cc*_merged suites both assert the two stay in sync). "home.settings"
# is deliberately excluded from HIDEABLE_TILE_KEYS: it is the one screen that
# can undo everything else here, so the owner must never be able to hide it
# and lock themselves out - enforced here (an attempt to store it in
# hidden_tiles fails _validate() outright, since it is not a legal member of
# the "tile_set" field's own "values"), not just in the UI.
HOME_TILE_KEYS = ("home.mixer", "home.hud", "home.browser",
                  "home.discord", "home.ytapp", "home.hotkeys", "home.clean", "home.settings",
                  "home.swap", "home.lights", "home.keyboard", "home.sleep")
HIDEABLE_TILE_KEYS = tuple(k for k in HOME_TILE_KEYS if k != "home.settings")

GROUPS = ("companion", "manuals", "command_center", "screens", "audio", "lights", "youtube",
          "battery", "appearance", "dualscreen")
GROUP_LABELS = {
    "companion": "Companion view",
    "manuals": "Manuals",
    "command_center": "Command Center",
    "screens": "Screens",
    "audio": "Audio",
    "lights": "Stick lights",
    "youtube": "YouTube",
    "battery": "Battery",
    "appearance": "Appearance",
    "dualscreen": "Dual-screen keys",
}

# Kept in sync with palettes.PRESET_ORDER + ("custom",) by
# tests/test_palettes.py (config.py itself stays import-free of
# palettes.py/ui.py - see the "appearance" schema block below).
THEME_PRESET_VALUES = (
    "default", "high_contrast",
    "rp5_black", "rp5_white", "rp5_16bit", "rp5_gc", "rp5_yellow", "rp5_turquoise",
    "gameboy", "nes", "genesis", "playstation", "n64", "dreamcast",
    "custom",
)

# ---------------------------------------------------------------------------
# Schema: one entry per leaf setting (research/R6-settings.md §3), with
# owner-approved changes over R6's draft: manuals.viewer gains a "native"
# option (R7 confirmed poppler/pdftoppm is on-device) and defaults to it, and
# command_center.hardware_button defaults to "btn_back_f1" - device-verified
# 23 Sep: the summon reader saw a debounced SUMMON from Back (KEY_F1 on
# "InputPlumber Keyboard"), while BOTH paddle codes produced 0 events in
# 120 s, and the owner confirmed the RP5 has no rear paddles at all (L2/R2
# are analog axes, not the digital paddle buttons this setting binds to).
# The paddle enum values stay in the schema (some other device might have
# them) - see WIRED's hardware_button comment and _note_paddle_button()
# below for how a stored paddle value is still honoured, just flagged.
#
# key_path is a tuple addressing the nested config dict. Add a key here and
# it must gain a control in settings_view.py too - tests/test_settings_view.py
# asserts one-to-one coverage.
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
    # CC2: what the bottom screen shows during a single-screen game (any
    # game that does NOT put its own window on DSI-1 - the DS/3DS/Wii U
    # second-screen cases go HIDDEN before companion.py is even consulted,
    # see DESIGN.md "Three display modes"). CC3 maps this directly onto the
    # ES-DE Companion app's "Game Playing Screen Behavior" (On/Dim/Off/
    # Manual/Guide) - see research/CC3-esde-companion.md.
    {"key_path": ("companion", "in_game_display"), "type": "enum",
     "values": ("art", "dim", "off", "manual", "hud", "clock", "slideshow"),
     "default": "art", "restart": False, "group": "companion",
     "label": "During a game, show"},
    {"key_path": ("companion", "brightness_pct"), "type": "int",
     "range": (5, 100), "step": 5, "default": 80, "restart": False,
     "group": "companion", "label": "Bottom-screen brightness"},
    # CC4: the system-carousel background colour (companion.py
    # CompanionView.draw() - the fill behind the system logo, hard-coded
    # BLACK until now). "theme" reads the installed ES theme's own colours
    # (free, instant, but only the theme's *declared* colour - real-
    # capture research on the device's es-theme-PiStation-X in
    # theme_colour.py found no per-system override, just one theme-wide
    # backgroundColor). "sample" takes a read-only `grim` screenshot of
    # DP-1 (the ES/top screen) instead - exact for whatever ES is really
    # drawing, including a per-system background IMAGE the theme parser
    # cannot see, but costs one screenshot per system change (debounced
    # ~400ms of no further scrolling, and never while a game is running -
    # theme_colour.SettleGate/should_sample). "off" keeps the old BLACK.
    # Default "sample" since test day (24 Sep): on the device PiStation-X's
    # default colorset (gray) has backgroundColor ffffff - a tint over a
    # dark background image - so "theme" painted the panel WHITE while ES
    # showed navy; the DP-1 sample measured (0, 2, 31), what ES shows.
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
    # SW1: the small pull tab in the corner of the game/ES screen that opens
    # the Command Center over the game (cc_overlay.py). Off = no tab; the
    # Back button still opens it there when the other screen cannot show it.
    {"key_path": ("command_center", "game_screen_tab"), "type": "bool",
     "default": True, "restart": False, "group": "command_center",
     "label": "Pull tab on game screen"},
    # CC5 (hidden_overlay.py): the Command Center over an emulator's second
    # screen. overlay_on_hidden: the hardware button opens it while a DS /
    # 3DS / Wii U window owns the panel's screen (off = cc_overlay.py's
    # game-screen Command Center takes that press, SW1's behaviour).
    # corner_handle: a small tap target in one corner of that screen while
    # such a window is there (costs its pixels of the game's touch area).
    {"key_path": ("command_center", "overlay_on_hidden"), "type": "bool",
     "default": True, "restart": False, "group": "command_center",
     "label": "Open over DS/3DS screen"},
    {"key_path": ("command_center", "corner_handle"), "type": "enum",
     "values": ("off", "top-left", "top-right", "bottom-left", "bottom-right"),
     "default": "off", "restart": False, "group": "command_center",
     "label": "Corner handle"},
    # Tile customisation (owner: "add a customisation mode with drag and
    # drop tiles"): screens.Home's own "Edit tiles" mode is the one real
    # control for both of these - like lights.*, neither is in WIRED
    # (settings_view.py's generic array_enum reorder page assumes
    # MEDIA_VALUES-style labels and would need its own "tile_set" renderer
    # for hidden_tiles; the dedicated in-place drag-and-drop UI is a better
    # fit for a spatial grid than a generic list page either way).
    # tile_order must be a full permutation of HOME_TILE_KEYS (array_enum's
    # existing "nothing dropped" rule) - a hidden tile still holds its slot
    # in the order for when it is shown again.
    {"key_path": ("command_center", "tile_order"), "type": "array_enum",
     "values": HOME_TILE_KEYS, "default": list(HOME_TILE_KEYS), "restart": False,
     "group": "command_center", "label": "Tile order"},
    {"key_path": ("command_center", "hidden_tiles"), "type": "tile_set",
     "values": HIDEABLE_TILE_KEYS, "default": [], "restart": False,
     "group": "command_center", "label": "Hidden tiles"},

    # -- Screens -----------------------------------------------------------
    # SW1: es_screen is shown as ONE toggle, "Swap screens" (on =
    # builtin_bottom: ES and games on the built-in panel, the Command Center
    # on the add-on). command_center_screen is always its opposite and has
    # no control of its own. Live, not restart: 092 re-reads the file every
    # poll and rp5deck follows ES (screen_swap.py).
    {"key_path": ("screens", "es_screen"), "type": "enum",
     "values": SCREEN_VALUES, "default": "addon_top", "restart": False,
     "group": "screens", "label": "Swap screens"},
    {"key_path": ("screens", "command_center_screen"), "type": "enum",
     "values": SCREEN_VALUES, "default": "builtin_bottom", "restart": True,
     "group": "screens", "label": "Command Center screen"},

    # -- Audio -------------------------------------------------------------
    {"key_path": ("audio", "volume_step_pct"), "type": "int",
     "range": (1, 20), "default": 5, "restart": False,
     "group": "audio", "label": "Volume step"},
    {"key_path": ("audio", "show_volume_overlay"), "type": "bool",
     "default": True, "restart": False, "group": "audio",
     "label": "Show volume overlay"},

    # -- Stick lights (RG) ---------------------------------------------
    # rgb_leds.py/rgb_view.py own these; config.py only stores + validates
    # them (never touches system.cfg - see rgb_leds.py's module docstring).
    # None of the four are in WIRED: their one real control is the
    # dedicated "Stick lights" sheet (rgb_view.RGBSheet, opened from its
    # own home tile), the same pattern hotkeys_view.py/cleanstate_view.py
    # use for their own dedicated sheets - see WIRED's comment below for
    # why a generic Settings row for these would be redundant.
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

    # -- YouTube (YT2) ---------------------------------------------------
    # yt_feeds.py owns everything cookie-gated (subscriptions/history/watch
    # later feeds, the Firefox-cookie sign-in path) and reads this exactly
    # one key as its kill switch: off means never pass
    # --cookies-from-browser to yt-dlp, i.e. a hard opt-out back to
    # search-only behaviour, independent of whether the owner is actually
    # signed in to Firefox. Default True: the feature never sees or stores
    # a password (it only reads Firefox's own already-trusted cookie
    # database, read-only, the same profile already used for Discord/
    # browsing), degrades to today's plain search on any failure, and is
    # exactly what the owner asked for ("full-featured... sign in") - see
    # patches/YT2-NOTES.md for the full justification.
    {"key_path": ("youtube", "sign_in_enabled"), "type": "bool",
     "default": True, "restart": False, "group": "youtube",
     "label": "Use Firefox sign-in for YouTube"},
    # AH: the YouTube TV app's control strip auto-hides after this many
    # seconds of no touch, so leanback's 16:9 UI fills the whole 1080 px
    # panel instead of the fixed 140 px every other BAR app reserves
    # (bar_autohide.py, main.py's geometry()). 0 disables it - same "0 = off"
    # convention as command_center.auto_close_timeout_s - and the strip
    # keeps the fixed reserved zone forever, like Browser/Discord/mpv.
    {"key_path": ("youtube", "tv_bar_hide_s"), "type": "int",
     "range": (0, 30), "default": 4, "restart": False,
     "group": "youtube", "label": "Auto-hide TV controls"},
    # YT4 (owner: "can we make youtube swipeable"): swipe direction ->
    # WEBDRIVER key. True ("natural"/content-follows-finger, phone-scroll
    # convention - a swipe left drags the NEXT item into view from the
    # right, i.e. sends "right"): the default, matching how the owner
    # already scrolls every other touchscreen. False sends the swipe's own
    # raw compass direction instead (a swipe left sends "left") - offered
    # since this is a judgement call, unverified with a real finger on the
    # actual leanback UI (patches/YT4-NOTES.md).
    {"key_path": ("youtube", "tv_swipe_natural"), "type": "bool",
     "default": True, "restart": False, "group": "youtube",
     "label": "Natural swipe direction"},

    # -- Battery (YT4: Safe charge) ---------------------------------------
    # rocknix-config/095-charge-limit (Main's file, not edited here) is what
    # actually enforces this at boot and applies a live change; charge_limit.py
    # mirrors its exact file/sysfs contract. Range/step/default are a short
    # literature + OEM review (patches/YT4-NOTES.md, cited sources): 85 keeps
    # 095's own already-shipped DEFAULT unchanged for an owner who never
    # touches this UI at all - only reachable by moving the slider or the
    # toggle. 50-95 in steps of 5 mirrors 095's own accepted minimum (50) and
    # leaves 100 (uncapped) exclusively to the OFF state of the toggle, never
    # a slider value, so "no limit" is one unambiguous state, not two.
    {"key_path": ("battery", "safe_charge_enabled"), "type": "bool",
     "default": True, "restart": False, "group": "battery",
     "label": "Safe charge"},
    # range/step/default must stay in step with charge_limit.py's own
    # MIN_END/MAX_END/STEP/DEFAULT_END constants (not imported here - config.py
    # stays dependency-free, same as lights.*/rgb_leds.py's own constants
    # just below); tests/test_config.py checks the two agree.
    {"key_path": ("battery", "safe_charge_end_pct"), "type": "int",
     "range": (50, 95), "step": 5, "default": 85, "restart": False,
     "group": "battery", "label": "Stop charging at"},

    # -- Appearance (colour themes) ---------------------------------------
    # palettes.py owns the actual colour data (PRESET_ORDER, PRESETS,
    # derive_custom()) and stays dependency-free of config.py, the same
    # direction every other module here already goes; THEME_PRESET_VALUES
    # below is the one place that DOES need to know palettes.py's preset
    # names, so it is spelled out by hand rather than imported (config.py
    # stays import-free of ui.py/palettes.py, same reasoning as lights.*
    # just above) - tests/test_palettes.py asserts this tuple, minus
    # "custom", equals palettes.PRESET_ORDER exactly, so the two cannot
    # silently drift apart.
    {"key_path": ("appearance", "theme_preset"), "type": "enum",
     "values": THEME_PRESET_VALUES, "default": "default",
     "restart": False, "group": "appearance", "label": "Colour theme"},
    # custom_accent/custom_bg/custom_text are deliberately NOT in WIRED,
    # same reasoning as lights.left/lights.right just above: their one
    # real control is the dedicated "Custom colours" sheet
    # (appearance_view.AppearanceSheet, opened from the Appearance page's
    # own "Edit custom colours..." button, not a generic settings row) -
    # a hex_color has no FieldRow renderer and should not get one.
    {"key_path": ("appearance", "custom_accent"), "type": "hex_color",
     "default": "#3d8bfd", "restart": False, "group": "appearance",
     "label": "Custom accent colour"},
    {"key_path": ("appearance", "custom_bg"), "type": "hex_color",
     "default": "#12141c", "restart": False, "group": "appearance",
     "label": "Custom background colour"},
    {"key_path": ("appearance", "custom_text"), "type": "hex_color",
     "default": "#f4f5f7", "restart": False, "group": "appearance",
     "label": "Custom text colour"},

    # -- Dual-screen per-game keys (dualscreen_keys.py) ---------------------
    # dualscreen_keys.py always checks/restores two generic system.cfg keys
    # (3ds.screen_layout, wiiu.gamepad_enabled - its own BASE_KEYS, not
    # schema-driven). A DS title can need its own per-game override (e.g.
    # nds["<rom filename>"].screen_layout=<value>), but which title and what
    # value is specific to one owner's own ROM collection, so it is never
    # hard-coded in the app - it lives here instead, with an EMPTY public
    # default: a fresh install checks/restores only the two generic keys.
    # Each entry: {"key": the exact system.cfg key name, "line": the exact
    # "key=value" text dualscreen_keys.restore() appends verbatim (never
    # built any other way), "rom": optional, a path relative to
    # dualscreen_keys.ROMS_DIR (/storage/roms) - when given, the key only
    # counts as "expected" while that file actually exists, so an owner
    # without the game never sees it listed as missing. Not in WIRED: like
    # lights.*/hidden_tiles just above, this is hand-edited config.json, not
    # a generic Settings row (a per-key form has no FieldRow renderer and
    # would need its own dedicated editor to be worth adding).
    {"key_path": ("dualscreen", "extra_keys"), "type": "key_line_list",
     "default": [], "restart": False, "group": "dualscreen",
     "label": "Extra per-game keys"},
]

FIELD_BY_PATH = {f["key_path"]: f for f in SCHEMA}


# ---------------------------------------------------------------------------
# WIRED: the subset of SCHEMA keys that actually change something at
# runtime, verified by grepping every non-test .py file outside config.py/
# settings_view.py for a reader (RV4-M2, RV5b check 12: 4 of 27 leaves had a
# working Settings-screen control and precisely zero effect).
# settings_view.py builds a control ONLY for a key in this set, so a schema
# entry can be added ahead of its consumer without silently presenting a
# fake working control - tests/test_settings_view.py asserts the two sets
# (WIRED, and "has a control") are identical in both directions.
#
# screens.es_screen is WIRED since SW1: its consumers are 092 (reads
# config.json itself and moves ES - rocknix-config/092-dual-screen-persist,
# rp5_es_screen()) and screen_swap.py (read_setting() -> the fallback when
# ES cannot be seen in the tree). screens.command_center_screen stays out:
# it is derived (always the opposite), so a control for it would be the old
# RV1-M4 "dangerous combo" all over again.
# ---------------------------------------------------------------------------
WIRED = frozenset([
    ("companion", "media_priority"),               # companion.py CompanionController.refresh(): prio = self._c("media_priority")
    ("companion", "play_video"),                    # companion.py CompanionController.video_allowed()/refresh(): self._c("play_video")
    ("companion", "video_audio"),                   # companion.py CompanionController._start_video(): video_muted(self._c("video_audio"), ...)
    ("companion", "video_start_delay_ms"),          # companion.py CompanionController._update_video(): delay = ... self._c("video_start_delay_ms")
    ("companion", "video_loop"),                    # companion.py CompanionController._start_video(): self.video.play(..., loop=bool(self._c("video_loop")))
    ("companion", "image_fit"),                     # companion.py CompanionController.video_frame_ready()/refresh()/_resolve_job(): self._c("image_fit")
    ("companion", "background_dim"),                # companion.py CompanionController._resolved(): self.view.show_info(..., float(self._c("background_dim")))
    ("companion", "show_metadata", "title"),         # companion.py metadata_lines(info, show): show.get("title", True)
    ("companion", "show_metadata", "year"),          # companion.py metadata_lines(): show.get("year", True)
    ("companion", "show_metadata", "developer"),     # companion.py metadata_lines(): show.get("developer", True)
    ("companion", "show_metadata", "players"),       # companion.py metadata_lines(): show.get("players", True)
    ("companion", "show_metadata", "rating"),        # companion.py metadata_lines(): show.get("rating", False)
    ("companion", "show_metadata", "description"),   # companion.py metadata_lines(): show.get("description", False)
    ("companion", "idle_mode"),                      # companion.py CompanionController._show_idle()/_arm_slides(): self._c("idle_mode")
    ("companion", "idle_slideshow_interval_s"),      # companion.py CompanionController._arm_slides()/_ingame_slide_tick(): self._c("idle_slideshow_interval_s")
    ("companion", "show_metadata", "playtime"),      # companion.py metadata_lines(): show.get("playtime", False)
    ("companion", "in_game_display"),                # companion.py CompanionController._in_game_mode()/_apply_in_game_mode() (CC2)
    ("manuals", "open_mode"),                        # companion.py CompanionController (auto-open check): config.get_value(..., ("manuals", "open_mode"))
    ("command_center", "swipe_down_enabled"),        # summon.py PullDownStateMachine.swipe_down_enabled; main.py App (wires on_change + reads cfg at startup)
    ("command_center", "swipe_sensitivity"),         # summon.py swipe_kwargs_for(); main.py App: config.get_value(..., ("command_center", "swipe_sensitivity"))
    ("command_center", "hardware_button"),           # summon.py SummonButtonReader binding; main.py App: config.get_value(..., ("command_center", "hardware_button"))
    ("command_center", "auto_close_timeout_s"),      # summon.py PullDownStateMachine._deadline; main.py App (ticks it down, wires on_change)
    ("command_center", "game_screen_tab"),           # cc_overlay.py OverlayApp._tab_enabled(): config.get_value(..., ("command_center", "game_screen_tab"))
    ("command_center", "overlay_on_hidden"),         # hidden_overlay.py OverlayController.enabled()/takes_summon(): setting_overlay_on_hidden(cfg) (CC5)
    ("command_center", "corner_handle"),             # hidden_overlay.py OverlayController.corner()/_apply_handle(); main.py App.on_setting -> cc5.config_changed() (CC5)
    ("audio", "show_volume_overlay"),                # main.py App: config.get_value(..., ("audio", "show_volume_overlay")) gates the volume HUD
    ("screens", "es_screen"),                        # 092-dual-screen-persist rp5_es_screen() moves ES; screen_swap.read_setting() (SW1)
    ("companion", "system_bg_source"),               # companion.py CompanionController (CC4): self._c("system_bg_source") - theme_colour.theme_background_color()/SampleCache
    ("youtube", "sign_in_enabled"),                   # yt_feeds.py sign_in_enabled()/_cookies_args(): config.get_value(cfg, ("youtube","sign_in_enabled")) - the Firefox-cookie kill switch (YT2)
    ("youtube", "tv_bar_hide_s"),                     # bar_autohide.BarAutoHide.timeout_s; main.py App (reads at startup, wires on_setting) (AH)
    ("youtube", "tv_swipe_natural"),                  # web_tiles.YtAppSession.swipe_natural; main.py App.on_setting (YT4)
    ("battery", "safe_charge_enabled"),               # main.py App.on_setting -> charge_limit.apply() (YT4)
    ("battery", "safe_charge_end_pct"),               # main.py App.on_setting -> charge_limit.apply() (YT4)
    ("appearance", "theme_preset"),                   # main.py App.on_setting -> palettes.apply_theme() (Appearance); cc_overlay.py re-reads it on its own poll
    # appearance.custom_accent/custom_bg/custom_text are deliberately NOT
    # here, same reasoning as lights.* just below: their one real control
    # is the dedicated "Custom colours" sheet (appearance_view.py), not a
    # generic hex_color settings row.
    #
    # lights.* (RG) are deliberately NOT here: settings_view.py has no
    # renderer for "hex_color" (nor should it - a generic Settings row for
    # a colour would duplicate the dedicated sheet), and mode/linked/
    # brightness are edited from that same sheet, not the generic one.
    # rgb_leds.py/rgb_view.py still read/write them for real through
    # config.get_value()/set_value()/save_changes() - "wired" to a real
    # consumer, just not to settings_view's generic UI. Compare
    # hotkeys_view.py/cleanstate_view.py, whose dedicated-sheet settings
    # never entered config.SCHEMA/WIRED at all.
    #
    # command_center.tile_order / hidden_tiles: same reasoning again - the
    # real control is screens.Home's own "Edit tiles" mode (long-press,
    # drag to reorder, tap a badge to hide/show), read/written directly
    # through get_value()/set_value()/save_changes() from main.py, not a
    # generic settings_view.py row.
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
    """Nested dict mirroring SCHEMA's key paths: True at every leaf, a
    dict at every branch. Used to tell "a key this schema knows about"
    from "an unknown key to preserve verbatim"."""
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
    """A fresh nested dict of every schema default, plus schema_version and
    the derived legacy output/es_output keys - what load() returns for a
    missing file."""
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
        # Like array_enum's membership check, but a SUBSET (order does not
        # matter, dropping members is the whole point) - hidden_tiles: any
        # key not in field["values"] is refused outright, which is how
        # "home.settings" (never in HIDEABLE_TILE_KEYS) can never be stored
        # here even by a hand-edited config.json.
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
        # dualscreen.extra_keys: a list of {"key", "line", "rom" (optional)}
        # - "key"/"line" must be non-empty strings, "key" must be unique in
        # the list (a duplicate would make restore()'s key->line map
        # ambiguous), and "rom" (when present) must be a non-empty relative
        # path - no leading "/" and no ".." component, so a hand-edited
        # config.json can never point dualscreen_keys.check() outside
        # ROMS_DIR.
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
    return (False, value)               # an unknown type in SCHEMA itself: refuse, don't guess


def _merge_unknown(data, cfg, tree):
    """Copy any key in `data` that SCHEMA does not know about into `cfg`
    verbatim (recursing into branches SCHEMA does partially cover), so a
    future or foreign key round-trips through load()/save() unharmed."""
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
    """THE shared contract for what a config file means for the swap (SW1).
    092-dual-screen-persist's rp5_es_screen() implements exactly this in its
    own python3 snippet (tests/test_sw1_092.py runs both over the same files
    and requires identical answers): "builtin_bottom" only when `raw` (the
    parsed JSON, before any migration) is an object whose "screens" is an
    object whose "es_screen" is literally "builtin_bottom". Everything else
    - None (no file / unreadable / bad JSON), a list, a legacy flat file, a
    typo, a wrong type - is "addon_top", the unswapped default."""
    if isinstance(raw, dict):
        screens = raw.get("screens")
        if isinstance(screens, dict) and screens.get("es_screen") == "builtin_bottom":
            return "builtin_bottom"
    return "addon_top"


def _screens_follow_contract(cfg, raw, notes):
    """es_screen := es_screen_from_raw(raw); command_center_screen := its
    opposite. Runs after migration/validation/_fixup_screens, so it has the
    last word: whatever those derived, what 092 will do with this file is
    what load() reports. The one case where they differ is a legacy flat
    file whose output/es_output migrate to the swapped combo - 092 reads
    only screens.es_screen, so it will keep ES on the add-on, and so must
    we (note recorded)."""
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
    """Device-verified 23 Sep: this RP5 has no rear paddles (0 evdev events
    in 120 s for either paddle code, while BTN_BACK/F1 fired reliably), so
    the default moved to "btn_back_f1". The paddle enum values stay valid
    (a stored file might be for a different device with real paddles) -
    load() honours a stored paddle value as-is, it just leaves a note."""
    v = _get(cfg, ("command_center", "hardware_button"))
    if v in PADDLE_HARDWARE_BUTTONS:
        notes.append("command_center.hardware_button=%r: this device (RP5) has no rear "
                     "paddles - keeping the stored value" % v)


def _add_legacy(cfg):
    cfg["output"] = SCREEN_TO_OUTPUT[_get(cfg, ("screens", "command_center_screen"))]
    cfg["es_output"] = SCREEN_TO_OUTPUT[_get(cfg, ("screens", "es_screen"))]


def _migrate(data, notes):
    """Turn today's flat {"output": ..., "es_output": ...} shape into
    screens.* the first time a pre-schema file is loaded. Returns a new
    dict; never mutates `data`. A no-op once "schema_version" is present."""
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
    """Set one schema value in place. Invalid values are refused (cfg is
    left unchanged, returns False). Setting one of the two screens.* keys
    flips the other to keep them opposite, and refreshes the legacy
    output/es_output keys to match - "picking one flips the other"."""
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
    """Return (config dict, note). note says where the values came from, or
    what was wrong with the file. The dict always has every schema key
    (defaults for anything missing/invalid) plus the legacy "output" /
    "es_output" keys main.py reads."""
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
    """Validate and atomically write cfg. Returns (written dict, notes).
    Never persists the derived output/es_output keys - they are recomputed
    by load() every time. Raises on a write failure (caller decides what to
    do; the OLD file on disk is untouched either way, because the write
    lands in a temp file that is only renamed over the target on success)."""
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
    """Read-modify-write of ONLY the given keys: {key_path: value}. Loads
    the file as it is on disk right now, applies each change through
    set_value() (an invalid value is skipped with a note, never written),
    and saves atomically. Returns (written dict, notes).

    Why (SW1): two rp5deck processes can now write this file - the main
    panel and the Command Center overlay on the game screen (cc_overlay.py,
    whose "Swap screens" tile writes screens.es_screen). save(cfg) writes a
    whole in-memory snapshot, so a process holding a stale copy would put
    the OTHER process's change back the next time it saved anything at all
    - including undoing a screen swap. Writing only what changed, on top of
    the current file, keeps both processes' changes."""
    path = path or config_path(env)
    cfg, _note = load(path)
    notes = []
    for key_path, value in dict(changes).items():
        if not set_value(cfg, key_path, value):
            notes.append("refused %s=%r: invalid" % (".".join(key_path), value))
    out, save_notes = save(cfg, path)
    return out, notes + save_notes
