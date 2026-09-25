"""palettes - colour theme presets for rp5deck's Appearance setting.

ui.THEME (ui.py, ~line 33) is a flat dict of 16 keys, read by every widget
at DRAW time (`THEME["accent"]`, never a cached copy) - so the live-apply
story for a new theme is simply "mutate the dict in place", never rebind
the name: several modules do `from ui import THEME`, which binds their own
local name to the SAME dict object, so `ui.THEME = {...}` would leave every
one of those modules pointing at the old dict. apply_theme() below always
does THEME.clear() + THEME.update(...) for exactly this reason (see its
docstring).

THEME_KEYS is read directly off ui.THEME at import time - the single
source of truth for "every key the app uses" - so a future widget adding
THEME["something_new"] to ui.py fails PRESETS_COMPLETE loudly (a test)
rather than silently drawing a KeyError on whichever preset forgot it.

Preset data: PRESETS maps a name -> a dict of THEME_KEYS -> an 0xRRGGBB
int (not yet an rgba tuple - ui.rgb() does that conversion once, in
theme_rgba(), so every preset's numbers can be compared/tested as plain
ints). "default" is copied byte-for-byte from ui.THEME's own literals
(test_palettes.py asserts this with ui.rgb() equality) so existing users
see no change at all.

Custom mode (config appearance.theme_preset == "custom") is not a fixed
PRESETS entry: derive_custom() builds the same 16 keys from just the
three colours the owner actually picks (accent/bg/text), by blending
towards bg/text/accent in fixed proportions - see its docstring for the
exact formula. danger/ok/warn keep the shipped Default hues (owner did
not pick those) but are nudged towards readable via ensure_contrast() if
the owner's own background would otherwise wash them out.

Contrast: contrast_ratio() is the plain WCAG 2 formula (relative
luminance with the sRGB gamma-correction piecewise curve, then
(L1+0.05)/(L2+0.05)). meets_body()/meets_large() are the two thresholds
this feature is held to everywhere (settings_view.py's inline Custom
warning, tools/palette_break_tests.py, tests/test_palettes.py):
  - body text on its background: >= 4.5:1
  - "large" text/buttons - accent-text-on-accent, and (extra, home-grown
    check here) each of danger/ok/warn against its own preset's
    background - >= 3:1
"""
import ui

# ---------------------------------------------------------------------------
# THEME_KEYS: read off the running theme, not hand-copied, so this module
# cannot silently drift from ui.py.
# ---------------------------------------------------------------------------
THEME_KEYS = tuple(ui.THEME.keys())

BODY_MIN_RATIO = 4.5
LARGE_MIN_RATIO = 3.0

DEFAULT_PRESET = "default"
CUSTOM_PRESET = "custom"


# ---------------------------------------------------------------------------
# Colour maths (pure - no ui.rgb() here, everything stays plain 0xRRGGBB
# ints/0-255 byte tuples until theme_rgba() at the very end of the pipeline)
# ---------------------------------------------------------------------------
def _bytes(hexval):
    return ((hexval >> 16) & 255, (hexval >> 8) & 255, hexval & 255)


def _from_bytes(rgb_bytes):
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb_bytes)
    return (r << 16) | (g << 8) | b


# Public names for appearance_view.py (its Custom colour picker moves
# between rgb_leds' own (r, g, b) 0-255 byte tuples - the Swatch widget it
# reuses from rgb_view.py speaks that shape - and this module's 0xRRGGBB
# ints); _bytes()/_from_bytes() stay private, same "helpers private to
# their own module" convention rgb_leds.py's own _hex_to_rgb()/
# _rgb_to_hex() follow.
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
# HSL: appearance_view.py's Custom colour picker needs a real lightness
# axis that rgb_leds.hue_to_rgb()/rgb_to_hue() do not have (those are
# fixed at value=1.0 - exactly right for a full-brightness LED colour,
# useless for picking a near-black background or a near-white one, which
# is most of what "background"/"text" actually need). Kept here rather
# than in rgb_leds.py since these have nothing to do with the stick
# lights' own device protocol - appearance_view.py reuses rgb_view's
# Swatch/PreviewCircle widgets and rgb_leds' hue dial CONCEPT (see its
# module docstring), not this specific math.
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
    """Approximate inverse of hsl_to_rgb - only used to park a picker's
    sliders at roughly the right spot for a stored colour, same spirit as
    rgb_leds.rgb_to_hue(). Grey (s=0) returns hue 0."""
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
    """If fg already reads on bg, return it unchanged. Otherwise push fg
    towards whichever of black/white increases contrast, in 5% steps,
    until min_ratio is met (or fg has become that extreme, for a bg so
    mid-grey neither corner would ever ordinarily satisfy a sane
    min_ratio) - keeps fg's hue recognisable (a nudged red is still red)
    instead of swapping in an unrelated fixed colour."""
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
# Shared derivation: every non-"default" preset below is built by calling
# this with just 6 "anchor" colours (bg/text/accent + the 3 semantic
# colours) - it is also exactly what Custom mode uses (derive_custom()),
# so "sensible defaults for the keys the owner didn't pick" only has one
# implementation to trust. Fractions were chosen by matching the shipped
# Default theme's own bg->bar->panel->tile->tile_pressed ramp (see
# tools/palette_break_tests.py's "derivation drifts from Default" check
# for the tolerance this is allowed to drift by) - NOT required to
# reproduce Default exactly (Default is its own hand-written entry below,
# byte-identical to ui.THEME), just to land in the same neighbourhood for
# every other preset so tiles/panels/lines still read as "a bit lighter/
# darker than the background" regardless of which bg/text a preset or a
# custom pick uses.
# ---------------------------------------------------------------------------
def _derive_full(bg, text, accent, danger, ok, warn):
    tile = blend(bg, text, 0.18)
    return {
        "bg": bg,
        "bar": blend(bg, text, 0.06),
        "panel": blend(bg, text, 0.11),
        "tile": tile,
        # pressed/lit state: nudge the tile towards the accent's own hue
        # AND towards text (brighter on a dark theme, darker on a light
        # one, because blend() walks straight at whatever "text" is).
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
# "default": copied literally from ui.THEME's own hex constants (ui.py
# ~line 34-50) - test_palettes.py's test_default_matches_ui_theme() checks
# this dict, run through theme_rgba(), equals ui.THEME exactly, key for
# key, value for value. Never route this one through _derive_full(): the
# whole point is "byte-identical to what a user already has".
_DEFAULT = {
    "bg": 0x12141C, "bar": 0x1B1E27, "panel": 0x1F232D, "tile": 0x262B37,
    "tile_pressed": 0x3C4558, "line": 0x363C4A, "text": 0xF4F5F7,
    "dim": 0xA3A9B5, "faint": 0x6B7280, "accent": 0x3D8BFD,
    "accent_pressed": 0x6AA6FF, "track": 0x3A3F4B, "disabled": 0x555B66,
    "danger": 0xE5484D, "ok": 0x46A758, "warn": 0xF5A524,
}
_DEFAULT_DANGER, _DEFAULT_OK, _DEFAULT_WARN = (_DEFAULT["danger"], _DEFAULT["ok"],
                                               _DEFAULT["warn"])

# name -> (bg, text, accent, danger, ok, warn) "anchor" colours. Order here
# is cycling/display/CLI order (PRESET_ORDER, below) - default first (so a
# fresh CycleButton or --list starts on what the owner already has),
# high-contrast next, then the RP5 hardware editions in their goretroid.com
# listing order, then the classic-console set, alphabetically-ish within
# that group.
#
# Sourcing / estimation notes (owner asked this be flagged, not just
# picked): goretroid.com's own product photos could not be colour-sampled
# from this environment (WebFetch on the product page returned no usable
# colour detail - "product images shown are thumbnails without sufficient
# detail"); a web search confirmed the SIX names (GC=purple, 16Bit=grey,
# Black, White, Yellow, Turquoise - steamdeckhq.com/news/the-retroid-
# pocket-5-gets-two-new-colors/, retrododo.com's colourway coverage) but
# not exact shell hex values. So every RP5xxx shell-body hue below is an
# ESTIMATE from the colourway's plain-English name, EXCEPT rp5_16bit's
# accent/danger/ok/warn, which are the SFC face-button colours verified
# by direct pixel-sampling of Retroid's own product photo (see
# D:/Tools/RP5-DualScreen-Setup/glyphs/RP5-SFC-GLYPHS.md) - those four are
# real, sourced hex values, not estimates.
_ANCHORS = {
    "high_contrast": (0x000000, 0xFFFFFF, 0x3D8BFD, 0xFF3B3B, 0x2ECC71, 0xFFC400),
    # RP5 Black: ESTIMATE (shell is simply black; near-black UI, default's
    # own blue accent family reused for continuity).
    "rp5_black": (0x141414, 0xF5F5F5, 0x4A90E2, 0xE5484D, 0x46A758, 0xF5A524),
    # RP5 White: ESTIMATE (light shell -> light theme). The one preset the
    # owner specifically flagged to double check readability on - every
    # widget's fg/bg pair here was picked to clear >=4.5:1 (see
    # test_palettes.py's per-preset contrast test), not just the two
    # required pairs, because a light theme is the one direction none of
    # ui.py's widgets were ever drawn against before.
    "rp5_white": (0xF5F5F5, 0x1A1A1A, 0x3D8BFD, 0xC62828, 0x1E7B34, 0x9A6400),
    # RP5 16 Bit: body colour is an ESTIMATE (grey/silver shell); accent/
    # danger/ok/warn are the VERIFIED SFC face-button hexes (A/B/X/Y).
    "rp5_16bit": (0x201F22, 0xF3F1EF, 0x4460E6, 0xED3736, 0x37CB5A, 0xEED43C),
    # RP5 GC: ESTIMATE. Indigo/purple body per the GameCube reference the
    # colourway is named for; accent/danger/ok borrow the GameCube pad's
    # own green-A/red-B/yellow-C-stick scheme (a real, well-known
    # controller colour scheme, not sampled from this specific shell).
    "rp5_gc": (0x1B1730, 0xF3F1FA, 0x2E7D32, 0xE1483F, 0x43A047, 0xFFD400),
    # RP5 Yellow / RP5 Turquoise: ESTIMATE (2026 colourways reused from the
    # discontinued Pocket G2 - no sampled swatch available at all, only
    # the plain colour name from press coverage).
    "rp5_yellow": (0x1D1A10, 0xF6F2E4, 0x8A6819, 0xE5484D, 0x46A758, 0xF5A524),
    "rp5_turquoise": (0x0F1E1D, 0xF0F7F6, 0x159488, 0xE5484D, 0x46A758, 0xF5A524),
    # Game Boy (DMG): the four anchor colours ARE (three of) the real
    # 4-shade DMG LCD palette (bg=shade1 lightest, accent=shade2, ok=
    # shade3 - all textbook "Game Boy green" hex values); danger/warn
    # break the strict monochrome (a same-hue "error" colour would be
    # indistinguishable from body text on a 2-bit screen) - dark brick-red
    # /olive chosen to still read against the light-green background
    # rather than a bright red or amber that would have no contrast here.
    "gameboy": (0x9BBC0F, 0x0F380F, 0x8BAC0F, 0x7A2000, 0x306230, 0x6B3A00),
    # NES: grey/black body -> near-black bg; the single NES accent colour
    # (its red) doubles as this preset's whole "flavour"; danger separated
    # from accent (brighter red) so a real red alert is distinguishable
    # from the red UI chrome; ok/warn are Default's own (nothing NES-
    # specific to say for "success green").
    "nes": (0x1E1E1E, 0xECECEC, 0xE60012, 0xFF5252, 0x3CB043, 0xF2A900),
    # Sega Genesis / Mega Drive: black body, Genesis-red accent; ok/warn
    # are Default's own.
    "genesis": (0x0D0D0D, 0xF2F2F2, 0xE4002B, 0xE5484D, 0x46A758, 0xF5A524),
    # PlayStation: grey body; accent/danger/ok are 3 of the 4 face-symbol
    # colours (Cross blue / Circle red / Triangle green); Square's own
    # pink was deliberately left unused here - pink does not read as a
    # "warning" colour, so warn stays a conventional amber instead of
    # forcing the 4th symbol colour in where it would be actively
    # confusing.
    "playstation": (0x1B1C1E, 0xF1F1F2, 0x2E6DA4, 0xE0435B, 0x4FBF8B, 0xC9962A),
    # Nintendo 64: charcoal body; accent/danger/ok/warn follow the
    # multicolour N64 logo's own N=blue/4=red/6=green + gold keystone -
    # NOT the controller's own button colours (not confident enough in
    # those from memory to assert them as fact).
    "n64": (0x1C1C1E, 0xF2F2F2, 0x2A5DB0, 0xE53935, 0x2E7D32, 0xC98A00),
    # Dreamcast: white body, orange swirl accent (its most recognisable
    # single colour); danger/warn are the same darkened reds/ambers the
    # other light theme (RP5 White) uses, for the same contrast-on-light
    # reason; ok deliberately is NOT the Dreamcast blue (blue does not
    # read as "success"), a conventional green instead.
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
    "custom": "Custom",   # not in PRESET_ORDER/PRESETS (it has no fixed colours of
                          # its own) but IS a config.THEME_PRESET_VALUES enum value,
                          # so display_label() needs an entry for it too.
}

# The classic-console set's PRESET_LABELS are deliberately descriptive, not
# console names (item 2's brief: "keep them as plain descriptive labels...
# which is fine for a colour scheme label"). display_label() (below) adds
# the console back in brackets for the UI (settings_view.py's CycleButton -
# device field test 25 Sep found the button showing "Dreamcast" while
# `palettes.py --list` showed "White/orange swirl", i.e. the two only
# disagreed because settings_view.py's generic _display() was splitting
# the RAW PRESET NAME on underscores instead of using PRESET_LABELS at
# all - this dict is only the bracketed hint, not a second label system).
# The RP5 editions/Default/High contrast need no hint: their PRESET_LABELS
# already ARE the console/device name.
PRESET_CONSOLE_HINT = {
    "gameboy": "Game Boy", "nes": "NES", "genesis": "Genesis",
    "playstation": "PlayStation", "n64": "N64", "dreamcast": "Dreamcast",
}


def display_label(name):
    """The one label the CycleButton and --list should agree on: the
    descriptive PRESET_LABELS text, plus "(Console)" for the classic set."""
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
    """The three owner-picked colours (0xRRGGBB ints) -> a full 16-key
    THEME dict, via the same _derive_full() every shipped preset (other
    than Default) uses. danger/ok/warn are NOT owner-picked - they keep
    Default's own hues, nudged towards the owner's background with
    ensure_contrast() so a very light or very dark custom background
    cannot wash out an error/success/warning colour to invisible (item 2's
    "keep semantic colours meaningful in every preset" applies here too,
    even though this "preset" is computed rather than shipped)."""
    danger = ensure_contrast(_DEFAULT_DANGER, bg)
    ok = ensure_contrast(_DEFAULT_OK, bg)
    warn = ensure_contrast(_DEFAULT_WARN, bg)
    return _derive_full(bg, text, accent, danger, ok, warn)


def custom_contrast_warning(accent, bg, text):
    """None if the owner's own accent/bg/text pick is readable; otherwise
    a short string naming which pair is below the WCAG body-text ratio -
    settings_view.py shows this inline (item 3: "show a short inline
    warning... don't block it") rather than refusing the pick."""
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
    """preset: a config.py appearance.theme_preset value (already schema-
    validated, but defended again here - see module docstring). Returns a
    16-key dict of 0xRRGGBB ints, never raises: an unrecognised preset
    name (a build that shipped a preset since removed, or a caller that
    skipped config's own validation) falls back to Default, same as a
    genuinely corrupt config.json does further up the chain in
    config.py's own _validate_tree()."""
    if preset == CUSTOM_PRESET:
        accent = _hex_or(custom_accent, _DEFAULT["accent"])
        bg = _hex_or(custom_bg, _DEFAULT["bg"])
        text = _hex_or(custom_text, _DEFAULT["text"])
        return derive_custom(accent, bg, text)
    return dict(PRESETS.get(preset, PRESETS[DEFAULT_PRESET]))


def hex_str_to_int(value, fallback):
    """A config.py "#rrggbb" string (or anything else at all) -> a 0xRRGGBB
    int, or `fallback` if it isn't one - public counterpart to
    hex_to_bytes()/bytes_to_hex() above, for the same reason (appearance_
    view.py's picker needs to go from a stored hex_color string to bytes
    and back)."""
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
    """hexdict (THEME_KEYS -> 0xRRGGBB int) -> the same keys -> ui.rgb()
    rgba tuples, i.e. exactly ui.THEME's own value shape."""
    return {k: ui.rgb(v) for k, v in hexdict.items()}


def theme_for_config(cfg, config_mod=None):
    """cfg: a loaded config.py dict. Reads appearance.* through
    config.get_value() (never cfg[...] directly, so a config.py that
    predates the "appearance" group - or one where validation already
    fell back a field to its own default - still resolves to Default
    rather than raising)."""
    m = config_mod
    if m is None:
        import config as m
    preset = m.get_value(cfg, ("appearance", "theme_preset")) or DEFAULT_PRESET
    accent = m.get_value(cfg, ("appearance", "custom_accent"))
    bg = m.get_value(cfg, ("appearance", "custom_bg"))
    text = m.get_value(cfg, ("appearance", "custom_text"))
    return resolve_theme_hex(preset, accent, bg, text)


def apply_theme(cfg, config_mod=None, theme=None):
    """Recompute the effective theme from `cfg` and apply it LIVE: mutate
    ui.THEME in place (clear() + update(), never `ui.THEME = {...}` - see
    the module docstring for why a rebind would miss every module that
    already did `from ui import THEME`). `theme` is an injection point for
    tests (a throwaway dict standing in for ui.THEME); main.py and
    cc_overlay.py both call this with no `theme` arg, i.e. against the
    real ui.THEME both processes actually draw from.

    Caller's job, not this function's: mark whatever needs a redraw dirty
    afterwards (main.py's on_setting() already sets self.state_dirty for
    every settings change; cc_overlay.py does the same in its own apply
    path) - this function only ever touches the dict, never the screen."""
    hexdict = theme_for_config(cfg, config_mod=config_mod)
    target = theme if theme is not None else ui.THEME
    target.clear()
    target.update(theme_rgba(hexdict))
    return hexdict


# ---------------------------------------------------------------------------
# CLI: `python palettes.py --list` - preset names + the two required
# contrast ratios, for a quick by-eye sanity check (also handy input for
# whoever later screenshots each preset on the device - not this agent's
# job per the brief).
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
