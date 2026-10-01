"""Colour theme presets for the Appearance setting.

ui.THEME is one flat dict that every widget reads when it draws, so a new theme is applied by
changing the dict in place, never by rebinding the name. Several modules do
`from ui import THEME`, and `ui.THEME = {...}` would leave them on the old dict, which is why
apply_theme() does THEME.clear() + THEME.update().

THEME_KEYS is read off ui.THEME at import, so a widget that adds a new key to ui.py makes the
preset tests fail instead of drawing a KeyError on some preset.

PRESETS maps a name to THEME_KEYS -> 0xRRGGBB ints (theme_rgba() turns them into rgba once,
so the numbers stay easy to test). "default" is copied exactly from ui.THEME so nothing
changes for existing setups.

Custom (theme_preset == "custom") isnt a fixed entry: derive_custom() builds all 16 keys from
the three colours you pick (accent, background, text). danger/ok/warn keep Default's hues but
get nudged by ensure_contrast() if your background would wash them out.

Contrast is plain WCAG 2 (relative luminance with the sRGB curve, then (L1+0.05)/(L2+0.05)).
Body text on its background needs 4.5:1, large text and buttons (and here also each of
danger/ok/warn on its background) need 3:1.
"""
import ui

# ---------------------------------------------------------------------------
# THEME_KEYS comes from the running theme, not a copy, so this cant drift from ui.py.
# ---------------------------------------------------------------------------
THEME_KEYS = tuple(ui.THEME.keys())

BODY_MIN_RATIO = 4.5
LARGE_MIN_RATIO = 3.0

DEFAULT_PRESET = "default"
CUSTOM_PRESET = "custom"


# ---------------------------------------------------------------------------
# Colour maths, pure: plain 0xRRGGBB ints or 0-255 byte tuples until theme_rgba() at the end.
# ---------------------------------------------------------------------------
def _bytes(hexval):
    return ((hexval >> 16) & 255, (hexval >> 8) & 255, hexval & 255)


def _from_bytes(rgb_bytes):
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb_bytes)
    return (r << 16) | (g << 8) | b


# Public names for the Custom colour picker, which moves between rgb_leds' (r, g, b) byte tuples
# (its Swatch widget speaks those) and this module's 0xRRGGBB ints.
def hex_to_bytes(hexval):
    return _bytes(hexval)


def bytes_to_hex(rgb_bytes):
    return _from_bytes(rgb_bytes)


def blend(a, b, t):
    """a, b: 0xRRGGBB ints. t=0 -> a, t=1 -> b, linear per channel."""
    ar, ag, ab = _bytes(a)
    br, bg, bb = _bytes(b)
    return _from_bytes((ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t))


def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hexval):
    r, g, b = _bytes(hexval)
    return (0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b))


def contrast_ratio(a, b):
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def meets_body(fg, bg):
    return contrast_ratio(fg, bg) >= BODY_MIN_RATIO


def meets_large(fg, bg):
    return contrast_ratio(fg, bg) >= LARGE_MIN_RATIO


# ---------------------------------------------------------------------------
# HSL, since the Custom picker needs a real lightness axis. rgb_leds' hue helpers are fixed at
# full brightness, which is right for an LED and useless for picking a near-black background or
# near-white text.
# ---------------------------------------------------------------------------
def hsl_to_rgb(h, s, l):
    """h: degrees (wraps mod 360), s/l: 0-1. -> 0xRRGGBB int."""
    h = float(h) % 360.0
    s = 0.0 if s < 0.0 else 1.0 if s > 1.0 else float(s)
    l = 0.0 if l < 0.0 else 1.0 if l > 1.0 else float(l)
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = l - c / 2.0
    if h < 60:
        r1, g1, b1 = c, x, 0.0
    elif h < 120:
        r1, g1, b1 = x, c, 0.0
    elif h < 180:
        r1, g1, b1 = 0.0, c, x
    elif h < 240:
        r1, g1, b1 = 0.0, x, c
    elif h < 300:
        r1, g1, b1 = x, 0.0, c
    else:
        r1, g1, b1 = c, 0.0, x
    return _from_bytes(((r1 + m) * 255, (g1 + m) * 255, (b1 + m) * 255))


def rgb_to_hsl(hexval):
    """Rough inverse of hsl_to_rgb, only used to put a picker's sliders near a stored colour. Grey
    (s=0) gives hue 0.
    """
    r, g, b = (c / 255.0 for c in _bytes(hexval))
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    l = (mx + mn) / 2.0
    if d == 0:
        return 0.0, 0.0, l
    s = d / (1 - abs(2 * l - 1)) if l not in (0.0, 1.0) else 0.0
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return (h * 60.0) % 360.0, s, l


def ensure_contrast(fg, bg, min_ratio=LARGE_MIN_RATIO):
    """If fg already reads on bg, returns it as is. Otherwise pushes fg towards black or white
    (whichever helps) in 5% steps until min_ratio is met or fg hits that end, so the hue stays
    recognisable (a nudged red is still red).
    """
    if contrast_ratio(fg, bg) >= min_ratio:
        return fg
    target = 0xFFFFFF if relative_luminance(bg) < 0.5 else 0x000000
    best = fg
    for i in range(1, 21):
        candidate = blend(fg, target, i / 20.0)
        best = candidate
        if contrast_ratio(candidate, bg) >= min_ratio:
            return candidate
    return best


# ---------------------------------------------------------------------------
# Every preset except Default is built from 6 anchor colours (bg, text, accent and the 3
# semantic ones), and Custom uses the same code, so the keys you dont pick have one
# implementation. The blend fractions follow Default's own bg -> bar -> panel -> tile ramp
# closely (tools/palette_break_tests.py checks the drift), so tiles and panels always read as a
# bit lighter or darker than the background whatever the preset.
# ---------------------------------------------------------------------------
def _derive_full(bg, text, accent, danger, ok, warn):
    tile = blend(bg, text, 0.18)
    return {
        "bg": bg,
        "bar": blend(bg, text, 0.06),
        "panel": blend(bg, text, 0.11),
        "tile": tile,
        # pressed/lit: pull the tile towards the accent and towards text (brighter on a dark theme,
        # darker on a light one)
        "tile_pressed": blend(blend(tile, accent, 0.4), text, 0.15),
        "line": blend(bg, text, 0.24),
        "text": text,
        "dim": blend(text, bg, 0.30),
        "faint": blend(text, bg, 0.50),
        "accent": accent,
        "accent_pressed": blend(accent, text, 0.30),
        "track": blend(bg, text, 0.22),
        "disabled": blend(bg, text, 0.40),
        "danger": danger,
        "ok": ok,
        "warn": warn,
    }


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
# "default" is copied straight from ui.THEME's own values, and a test checks it matches exactly.
# Never build it with _derive_full(), the point is its identical to what you already have.
_DEFAULT = {
    "bg": 0x12141C, "bar": 0x1B1E27, "panel": 0x1F232D, "tile": 0x262B37,
    "tile_pressed": 0x3C4558, "line": 0x363C4A, "text": 0xF4F5F7,
    "dim": 0xA3A9B5, "faint": 0x6B7280, "accent": 0x3D8BFD,
    "accent_pressed": 0x6AA6FF, "track": 0x3A3F4B, "disabled": 0x555B66,
    "danger": 0xE5484D, "ok": 0x46A758, "warn": 0xF5A524,
}
_DEFAULT_DANGER, _DEFAULT_OK, _DEFAULT_WARN = (_DEFAULT["danger"], _DEFAULT["ok"],
                                               _DEFAULT["warn"])

# name -> (bg, text, accent, danger, ok, warn) anchor colours. The order here is the cycling
# and --list order: Default first, High contrast, the RP5 editions in Retroid's listing order,
# then the classic consoles.
#
# Where the colours come from: the six RP5 edition names are confirmed (GC, 16 Bit, Black,
# White, Yellow, Turquoise), but Retroid's product photos couldnt be colour-sampled, so every
# shell colour is an estimate from the edition's name. The one exception is rp5_16bit's
# accent/danger/ok/warn, which are the SFC face buttons sampled from Retroid's own photo
# (glyphs/RP5-SFC-GLYPHS.md).
_ANCHORS = {
    "high_contrast": (0x000000, 0xFFFFFF, 0x3D8BFD, 0xFF3B3B, 0x2ECC71, 0xFFC400),
    # RP5 Black: estimate (black shell, near-black UI, Default's blue accent kept)
    "rp5_black": (0x141414, 0xF5F5F5, 0x4A90E2, 0xE5484D, 0x46A758, 0xF5A524),
    # RP5 White: estimate (light shell, light theme). Every fg/bg pair here clears 4.5:1, not just
    # the two required ones, since none of the widgets were drawn on a light theme before.
    "rp5_white": (0xF5F5F5, 0x1A1A1A, 0x3D8BFD, 0xC62828, 0x1E7B34, 0x9A6400),
    # RP5 16 Bit: the grey body is an estimate, accent/danger/ok/warn are the sampled SFC buttons
    "rp5_16bit": (0x201F22, 0xF3F1EF, 0x4460E6, 0xED3736, 0x37CB5A, 0xEED43C),
    # RP5 GC: estimate. Indigo body after the GameCube, with the GameCube pad's green A, red B and
    # yellow C-stick for accent/danger/ok.
    "rp5_gc": (0x1B1730, 0xF3F1FA, 0x2E7D32, 0xE1483F, 0x43A047, 0xFFD400),
    # RP5 Yellow / RP5 Turquoise: estimate, only the colour names are known
    "rp5_yellow": (0x1D1A10, 0xF6F2E4, 0x8A6819, 0xE5484D, 0x46A758, 0xF5A524),
    "rp5_turquoise": (0x0F1E1D, 0xF0F7F6, 0x159488, 0xE5484D, 0x46A758, 0xF5A524),
    # Game Boy (DMG): bg, accent and ok are real shades of the 4-shade DMG LCD palette. danger and
    # warn break the monochrome (a same-hue error colour would disappear on a 2-bit screen), dark
    # brick red and olive so they still read on the light green.
    "gameboy": (0x9BBC0F, 0x0F380F, 0x8BAC0F, 0x7A2000, 0x306230, 0x6B3A00),
    # NES: near-black bg with the NES red as the accent. danger is a brighter red so a real alert
    # stands out from the red chrome, ok/warn are Default's.
    "nes": (0x1E1E1E, 0xECECEC, 0xE60012, 0xFF5252, 0x3CB043, 0xF2A900),
    # Sega Genesis / Mega Drive: black body, Genesis red accent, ok/warn are Default's
    "genesis": (0x0D0D0D, 0xF2F2F2, 0xE4002B, 0xE5484D, 0x46A758, 0xF5A524),
    # PlayStation: grey body, accent/danger/ok are Cross blue, Circle red and Triangle green. Square's
    # pink isnt used, pink doesnt read as a warning, so warn stays amber.
    "playstation": (0x1B1C1E, 0xF1F1F2, 0x2E6DA4, 0xE0435B, 0x4FBF8B, 0xC9962A),
    # Nintendo 64: charcoal body, accent/danger/ok/warn follow the N64 logo (blue N, red 4, green 6
    # and the gold), not the controller buttons
    "n64": (0x1C1C1E, 0xF2F2F2, 0x2A5DB0, 0xE53935, 0x2E7D32, 0xC98A00),
    # Dreamcast: white body, orange swirl accent. danger/warn are the same darker red and amber RP5
    # White uses for contrast on light, and ok is a normal green since blue doesnt read as success.
    "dreamcast": (0xF7F5F0, 0x1B1B1B, 0xE8720B, 0xC62828, 0x1E7B34, 0x9A6400),
}

PRESET_ORDER = (
    "default", "high_contrast",
    "rp5_black", "rp5_white", "rp5_16bit", "rp5_gc", "rp5_yellow", "rp5_turquoise",
    "gameboy", "nes", "genesis", "playstation", "n64", "dreamcast",
)

PRESET_LABELS = {
    "default": "Default",
    "high_contrast": "High contrast",
    "rp5_black": "RP5 Black",
    "rp5_white": "RP5 White",
    "rp5_16bit": "RP5 16 Bit",
    "rp5_gc": "RP5 GC",
    "rp5_yellow": "RP5 Yellow",
    "rp5_turquoise": "RP5 Turquoise",
    "gameboy": "Classic handheld green",
    "nes": "8-bit grey",
    "genesis": "16-bit black/red",
    "playstation": "90s console grey",
    "n64": "Charcoal primaries",
    "dreamcast": "White/orange swirl",
    "custom": "Custom",  # not in PRESET_ORDER/PRESETS (it has no fixed colours
                          # of its own) but its a config.THEME_PRESET_VALUES value, so display_label() needs it too
}

# The classic-console presets have descriptive labels, and display_label() adds the console in
# brackets for the UI. The RP5 editions, Default and High contrast need no hint since their
# labels already are the device name.
PRESET_CONSOLE_HINT = {
    "gameboy": "Game Boy", "nes": "NES", "genesis": "Genesis",
    "playstation": "PlayStation", "n64": "N64", "dreamcast": "Dreamcast",
}


def display_label(name):
    """The one label the Settings button and --list agree on: the PRESET_LABELS text, plus
    "(Console)" for the classic set.
    """
    label = PRESET_LABELS.get(name, name)
    hint = PRESET_CONSOLE_HINT.get(name)
    return "%s (%s)" % (label, hint) if hint else label


def _build_presets():
    presets = {"default": dict(_DEFAULT)}
    for name in PRESET_ORDER:
        if name == "default":
            continue
        bg, text, accent, danger, ok, warn = _ANCHORS[name]
        presets[name] = _derive_full(bg, text, accent, danger, ok, warn)
    return presets


PRESETS = _build_presets()


# ---------------------------------------------------------------------------
# Custom mode
# ---------------------------------------------------------------------------
def derive_custom(accent, bg, text):
    """The three colours you pick (0xRRGGBB ints) -> a full 16-key THEME, through the same
    _derive_full() the shipped presets use. danger/ok/warn keep Default's hues, nudged with
    ensure_contrast() so a very light or very dark background cant wash them out.
    """
    danger = ensure_contrast(_DEFAULT_DANGER, bg)
    ok = ensure_contrast(_DEFAULT_OK, bg)
    warn = ensure_contrast(_DEFAULT_WARN, bg)
    return _derive_full(bg, text, accent, danger, ok, warn)


def custom_contrast_warning(accent, bg, text):
    """None if your accent/bg/text pick is readable, otherwise a short note naming the pair below the
    body-text ratio. Settings shows it inline instead of refusing the pick.
    """
    problems = []
    if not meets_body(text, bg):
        problems.append("text on background")
    if not meets_large(text, accent):
        problems.append("text on accent")
    if not problems:
        return None
    return "Low contrast: " + " and ".join(problems) + " may be hard to read"


# ---------------------------------------------------------------------------
# Resolving + applying a config to ui.THEME
# ---------------------------------------------------------------------------
def resolve_theme_hex(preset, custom_accent=None, custom_bg=None, custom_text=None):
    """preset: an appearance.theme_preset value (config already checked it, checked again here).
    Returns a 16-key dict of 0xRRGGBB ints and never raises, an unknown name falls back to
    Default.
    """
    if preset == CUSTOM_PRESET:
        accent = _hex_or(custom_accent, _DEFAULT["accent"])
        bg = _hex_or(custom_bg, _DEFAULT["bg"])
        text = _hex_or(custom_text, _DEFAULT["text"])
        return derive_custom(accent, bg, text)
    return dict(PRESETS.get(preset, PRESETS[DEFAULT_PRESET]))


def hex_str_to_int(value, fallback):
    """A config "#rrggbb" string (or anything else) -> a 0xRRGGBB int, or `fallback` if it isnt one.
    The picker uses it to go from a stored colour string to bytes and back.
    """
    return _hex_or(value, fallback)


def _hex_or(value, fallback):
    if not isinstance(value, str):
        return fallback
    v = value.strip().lstrip("#")
    if len(v) != 6:
        return fallback
    try:
        return int(v, 16)
    except ValueError:
        return fallback


def theme_rgba(hexdict):
    """hexdict (THEME_KEYS -> 0xRRGGBB int) -> the same keys as ui.rgb() rgba tuples, ui.THEME's own
    shape.
    """
    return {k: ui.rgb(v) for k, v in hexdict.items()}


def theme_for_config(cfg, config_mod=None):
    """cfg: a loaded config dict. Reads appearance.* through config.get_value(), so an older config
    without the group, or a field that fell back to its default, still gives Default instead of
    raising.
    """
    m = config_mod
    if m is None:
        import config as m
    preset = m.get_value(cfg, ("appearance", "theme_preset")) or DEFAULT_PRESET
    accent = m.get_value(cfg, ("appearance", "custom_accent"))
    bg = m.get_value(cfg, ("appearance", "custom_bg"))
    text = m.get_value(cfg, ("appearance", "custom_text"))
    return resolve_theme_hex(preset, accent, bg, text)


def apply_theme(cfg, config_mod=None, theme=None):
    """Works out the theme from `cfg` and applies it live by changing ui.THEME in place (clear() +
    update(), never rebinding, see the module docstring). `theme` lets tests pass a stand-in dict.
    main.py and cc_overlay.py call it with no `theme`, against the real ui.THEME.

    This only touches the dict. Marking things for a redraw afterwards is the caller's job.
    """
    hexdict = theme_for_config(cfg, config_mod=config_mod)
    target = theme if theme is not None else ui.THEME
    target.clear()
    target.update(theme_rgba(hexdict))
    return hexdict


# ---------------------------------------------------------------------------
# CLI: `python palettes.py --list` prints the preset names and their two contrast ratios, for a
# quick check by eye.
# ---------------------------------------------------------------------------
def _cli_list():
    for name in PRESET_ORDER:
        p = PRESETS[name]
        tb = contrast_ratio(p["text"], p["bg"])
        ta = contrast_ratio(p["text"], p["accent"])
        db = contrast_ratio(p["danger"], p["bg"])
        ob = contrast_ratio(p["ok"], p["bg"])
        wb = contrast_ratio(p["warn"], p["bg"])
        print("%-16s %-22s text/bg=%5.2f:1  text/accent=%5.2f:1  "
              "danger/bg=%5.2f:1  ok/bg=%5.2f:1  warn/bg=%5.2f:1"
              % (name, PRESET_LABELS[name], tb, ta, db, ob, wb))


if __name__ == "__main__":
    import sys
    if "--list" in sys.argv or len(sys.argv) == 1:
        _cli_list()
    else:
        print("usage: python palettes.py --list")
