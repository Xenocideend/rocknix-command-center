"""button_colours: the colour of EmulationStation's A/B/X/Y button prompts.

Button colours that match each RP5 model, changeable without changing the screen theme, plus
schemes from classic and modern controllers with 4 face buttons. Devices with 2, 3 or 6 face
buttons get a coming soon message for now.

It changes the eight face button icons of the RP5 help icon set (glyphs/make_rp5_sfc_glyphs.py)
in ES's user resource folder: button_a/b/x/y and the buttons_east/north/south/west clusters.
Every other icon stays as installed. The templates in es_help_face/ are that set's SFC files,
and apply() swaps their face colour (and the letter colour, dark on a light button) and writes
them. ES reads the icons when it starts, so a change shows after ES restarts.

Colours go by physical position on the RP5 (A east, B south, X north, Y west), so a
controller's scheme lands where your thumb expects it: Xbox green is the bottom button,
PlayStation's red circle the right one.

"follow_theme" (the default) picks the scheme matching appearance.theme_preset: an RP5 edition
theme gets that edition's buttons, PlayStation and Dreamcast themes their pad's colours,
anything else the Super Famicom set the icons shipped with.

An ES theme can name its own button icons in its help bar (<iconA>... in a <helpsystem>), and
then ES ignores the resource folder. So a chosen scheme also goes over the active theme's own
icons, and the help bar's iconColor tint is set to plain white so the colours show. The theme's
originals are backed up the first time, and "use_theme" puts them back.
"""
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "es_help_face")
ES_HELP_DIR = "/storage/.config/emulationstation/resources/help"
DEVICES_PATH = "/proc/bus/input/devices"

FOLLOW = "follow_theme"
USE_THEME = "use_theme"

ES_SETTINGS = "/storage/.config/emulationstation/es_settings.cfg"
THEME_DIRS = ("/storage/roms/themes", "/storage/.config/emulationstation/themes")
THEME_BACKUP = "/storage/rp5deck/theme-buttons-backup"
# help bar letter -> our icon
THEME_ICON_FILE = {"A": "button_a.svg", "B": "button_b.svg", "X": "button_x.svg", "Y": "button_y.svg"}

# file -> the physical position its face colour belongs to
FILE_POS = {
    "button_a": "east", "button_b": "south", "button_x": "north", "button_y": "west",
    "buttons_east": "east", "buttons_south": "south",
    "buttons_north": "north", "buttons_west": "west",
}
# the colour each template carries now (the SFC set), by position
TEMPLATE_HEX = {"east": "#ED3736", "south": "#EED43C", "north": "#4460E6", "west": "#37CB5A"}
TEMPLATE_LETTER = "#FFFFFF"
DARK_LETTER = "#1A1A1A"

# name -> (label, {position: hex}, source note), in the Settings cycle order. "VERIFIED" means
# sampled or documented, "ESTIMATE" means from the colourway's name or photos, same as
# palettes.py's RP5 edition themes.
SCHEMES = {
    "rp5_16bit": ("RP5 16 Bit", {"east": "#ED3736", "south": "#EED43C", "north": "#4460E6",
                                 "west": "#37CB5A"},
                  "VERIFIED: sampled from Retroid's 16Bit product photo"),
    "rp5_black": ("RP5 Black", {p: "#2B2B2D" for p in TEMPLATE_HEX}, "ESTIMATE: black caps"),
    "rp5_white": ("RP5 White", {p: "#ECECEC" for p in TEMPLATE_HEX}, "ESTIMATE: white caps"),
    "rp5_gc": ("RP5 GC", {"east": "#2E9E4F", "south": "#D8403A", "north": "#B9B4C9",
                          "west": "#B9B4C9"},
               "ESTIMATE: GameCube-style green A, red B, grey X/Y"),
    "rp5_yellow": ("RP5 Yellow", {p: "#F2C230" for p in TEMPLATE_HEX}, "ESTIMATE: shell yellow"),
    "rp5_turquoise": ("RP5 Turquoise", {p: "#1FB5A6" for p in TEMPLATE_HEX},
                      "ESTIMATE: shell turquoise"),
    "super_famicom": ("Super Famicom", {"east": "#ED3736", "south": "#EED43C", "north": "#4460E6",
                                        "west": "#37CB5A"}, "same as RP5 16 Bit"),
    "snes_us": ("SNES (US)", {"east": "#4B3F9E", "south": "#4B3F9E", "north": "#A8A2D6",
                              "west": "#A8A2D6"}, "dark purple A/B, lavender X/Y"),
    "xbox": ("Xbox", {"south": "#5DBB46", "east": "#E23B2E", "west": "#2A74D4", "north": "#F2C12E"},
             "A green, B red, X blue, Y yellow"),
    "playstation": ("PlayStation", {"south": "#7FA6D9", "east": "#E0435B", "west": "#D07AB8",
                                    "north": "#3FB28F"},
                    "cross blue, circle red, square pink, triangle green"),
    "dreamcast": ("Dreamcast", {"south": "#D9322E", "east": "#2C6CC9", "west": "#F3C21B",
                                "north": "#3AA655"}, "A red, B blue, X yellow, Y green"),
    "gamecube": ("GameCube", {"south": "#2E9E4F", "west": "#D8403A", "east": "#A9A6AE",
                              "north": "#A9A6AE"}, "big green A, red B, grey X/Y"),
    "steam_deck": ("Steam Deck", {p: "#34373B" for p in TEMPLATE_HEX},
                   "ESTIMATE: the Deck's dark grey ABXY caps with light letters"),
    "modern_grey": ("Plain grey", {p: "#3A3A3C" for p in TEMPLATE_HEX},
                    "one colour, like most modern pads"),
}
ORDER = (FOLLOW, USE_THEME) + tuple(SCHEMES)
VALUES = ORDER                                # config.py's enum (kept equal by a test)

THEME_TO_SCHEME = {
    "rp5_16bit": "rp5_16bit", "rp5_black": "rp5_black", "rp5_white": "rp5_white",
    "rp5_gc": "rp5_gc", "rp5_yellow": "rp5_yellow", "rp5_turquoise": "rp5_turquoise",
    "playstation": "playstation", "dreamcast": "dreamcast",
}
DEFAULT_SCHEME = "super_famicom"              # what the icon set shipped with

COMING_SOON = "Coming soon for 2, 3 and 6-button devices"

# evdev face-button codes: SOUTH EAST C NORTH WEST Z
FACE_CODES = (0x130, 0x131, 0x132, 0x133, 0x134, 0x135)
# emulated or virtual pads report the layout they copy, not the device's own
_NOT_THE_DEVICE = re.compile(r"InputPlumber|DualSense|Xbox|virtual|uinput|Keyboard", re.I)


def label(name):
    if name == FOLLOW:
        return "Match screen theme"
    if name == USE_THEME:
        return "Use current theme"
    return SCHEMES[name][0] if name in SCHEMES else name


def resolve(choice, theme_preset):
    """The scheme actually drawn for a setting value and the current screen theme."""
    if choice in SCHEMES or choice == USE_THEME:
        return choice
    return THEME_TO_SCHEME.get(theme_preset, DEFAULT_SCHEME)


def face_button_count(text=None, devices_path=DEVICES_PATH):
    """Face buttons on the device's own gamepad (None if there's no gamepad).

    Reads /proc/bus/input/devices for the first gamepad that isnt emulated or virtual, and counts
    which of the six face button codes its KEY bitmap has.
    """
    if text is None:
        try:
            with open(devices_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None
    for block in text.split("\n\n"):
        name = re.search(r'^N: Name="(.*)"', block, re.M)
        keys = re.search(r"^B: KEY=([0-9a-f ]+)$", block, re.M)
        if not name or not keys or _NOT_THE_DEVICE.search(name.group(1)):
            continue
        words = keys.group(1).split()
        bits = 0
        for i, w in enumerate(reversed(words)):  # last word = lowest 64 bits
            bits |= int(w, 16) << (64 * i)
        if not bits >> 0x130 & 1:  # no BTN_SOUTH, not a gamepad
            continue
        return sum(1 for c in FACE_CODES if bits >> c & 1)
    return None


def supported(count=None):
    """The option works on devices with 4 face buttons, unknown counts are allowed."""
    if count is None:
        count = face_button_count()
    return count is None or count == 4


def _light(hexcol):
    r, g, b = (int(hexcol[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.85  # near white only, real yellow caps keep white letters


def render(scheme):
    """{filename: svg text} for one scheme, from the templates."""
    colours = SCHEMES[scheme][1]
    out = {}
    for stem, pos in FILE_POS.items():
        with open(os.path.join(TEMPLATES, stem + ".svg"), encoding="utf-8") as f:
            s = f.read()
        old = TEMPLATE_HEX[pos]
        if s.count(old) != 1:
            raise ValueError("%s.svg: expected one %s, found %d" % (stem, old, s.count(old)))
        new = colours[pos]
        s = s.replace(old, new)
        if stem.startswith("button_") and _light(new):
            # the letter, white on a dark cap and dark on a light one
            s = s.replace('stroke="%s"' % TEMPLATE_LETTER, 'stroke="%s"' % DARK_LETTER)
        out[stem + ".svg"] = s
    return out


def _write(path, text):
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def active_theme(settings_path=ES_SETTINGS, theme_dirs=THEME_DIRS):
    """(name, folder) of the ES theme in use, or (None, None). Reads only the ThemeSet key."""
    try:
        with open(settings_path, encoding="utf-8", errors="replace") as f:
            m = re.search(r'<string name="ThemeSet" value="([^"]+)"', f.read())
    except OSError:
        return None, None
    if not m:
        return None, None
    for d in theme_dirs:
        root = os.path.join(d, m.group(1))
        if os.path.isfile(os.path.join(root, "theme.xml")):
            return m.group(1), root
    return m.group(1), None


_HELPSYSTEM = re.compile(r"<helpsystem\b[^>]*>.*?</helpsystem>", re.S)


def theme_help_icons(root):
    """{letter: icon path} the theme's help bar uses, and the xml files holding a help bar.

    Paths resolve from the theme folder like ES does ("./_inc/icons/..."), falling back to the
    file's own folder. One with a ${variable} in it cant be followed and is skipped."""
    icons, xmls = {}, []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".xml"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            blocks = _HELPSYSTEM.findall(text)
            if not blocks:
                continue
            xmls.append(path)
            for block in blocks:
                for letter, rel in re.findall(r"<icon([ABXY])>\s*([^<]+?)\s*</icon\1>", block):
                    if "$" in rel:
                        continue
                    for base in (root, dirpath):
                        p = os.path.normpath(os.path.join(base, rel))
                        if os.path.isfile(p):
                            icons.setdefault(letter, p)
                            break
    return icons, xmls


def _backup_once(path, root, backup):
    dest = os.path.join(backup, os.path.relpath(path, root))
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)


def restore_theme(root, backup):
    """Puts every backed up theme file back, removes our tinted icons and clears the backup.
    Returns how many files came back."""
    if not root:
        return 0
    shutil.rmtree(os.path.join(root, TINTED_DIR), ignore_errors=True)
    if not os.path.isdir(backup):
        return 0
    n = 0
    for dirpath, _dirs, files in os.walk(backup):
        for fn in files:
            src = os.path.join(dirpath, fn)
            shutil.copy2(src, os.path.join(root, os.path.relpath(src, backup)))
            n += 1
    shutil.rmtree(backup)
    return n


# White turns the theme's tint off for every help icon, and the rest are white too, so on a
# light theme they vanish. These are the other icons a helpsystem can name, with ES's file.
OTHER_HELP_ICONS = (
    ("iconUpDown", "dpad_updown.svg"), ("iconLeftRight", "dpad_leftright.svg"),
    ("iconUpDownLeftRight", "dpad_all.svg"), ("iconL", "button_l.svg"), ("iconR", "button_r.svg"),
    ("iconStart", "button_start.svg"), ("iconSelect", "button_select.svg"), ("iconF1", "F1.svg"),
)
ES_BUILTIN_HELP = "/usr/config/emulationstation/resources/help"
TINTED_DIR = "_rp5deck-tinted-help"
_VARIABLES = re.compile(r"<variables\b([^>]*)>(.*?)</variables>", re.S)
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")


def _original(path, root, backup):
    """A theme file as the theme shipped it (the backup once we've written over it)."""
    b = os.path.join(backup, os.path.relpath(path, root))
    with open(b if os.path.exists(b) else path, encoding="utf-8", errors="replace") as f:
        return f.read()


def theme_tints(root, value):
    """[(ifSubset or None, RRGGBB)] an iconColor value resolves to: a plain hex as is, a
    ${variable} from every <variables> block that sets it (unconditional ones first)."""
    m = _HEX.match(value.strip())
    if m:
        return [(None, m.group(1).upper())]
    v = re.match(r"^\$\{(\w[\w.-]*)\}$", value.strip())
    if not v:
        return []
    found = []
    for dirpath, _dirs, files in sorted(os.walk(root)):
        for fn in sorted(files):
            if not fn.endswith(".xml"):
                continue
            with open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace") as f:
                text = f.read()
            for attrs, body in _VARIABLES.findall(text):
                hit = re.search(r"<%s>\s*([^<]+?)\s*</%s>" % (re.escape(v.group(1)), re.escape(v.group(1))), body)
                h = _HEX.match(hit.group(1)) if hit else None
                if h:
                    sub = re.search(r'ifSubset="([^"]*)"', attrs)
                    found.append((sub.group(1) if sub else None, h.group(1).upper()))
    return sorted(found, key=lambda t: t[0] is not None)


def tint_svg(text, rrggbb):
    """What ES draws when it tints an icon: every colour in it multiplied by the tint."""
    t = [int(rrggbb[i:i + 2], 16) for i in (0, 2, 4)]

    def mul(rgb):
        return "#%02X%02X%02X" % tuple(round(c * k / 255.0) for c, k in zip(rgb, t))

    def hexcol(m):
        s = m.group(1)
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        return mul([int(s[i:i + 2], 16) for i in (0, 2, 4)])

    text = re.sub(r"#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})(?![0-9A-Fa-f])", hexcol, text)
    text = re.sub(r'(fill|stroke|stop-color)(="|:\s*)white\b', lambda m: m.group(1) + m.group(2) + mul([255] * 3), text)
    return text


def _tinted_overrides(block, root, help_dir, tints):
    """Writes the tinted copies of the other help icons and returns the lines naming them."""
    lines = []
    for prop, fn in OTHER_HELP_ICONS:
        own = re.search(r"<%s>\s*([^<$]+?)\s*</%s>" % (prop, prop), block)
        cands = [os.path.normpath(os.path.join(root, own.group(1)))] if own else []
        cands += [os.path.join(d, fn) for d in (help_dir, ES_BUILTIN_HELP)]
        src = next((p for p in cands if os.path.isfile(p)), None)
        if not src:
            continue
        with open(src, encoding="utf-8", errors="replace") as f:
            svg = f.read()
        for sub, hexcol in tints:
            os.makedirs(os.path.join(root, TINTED_DIR, hexcol), exist_ok=True)
            _write(os.path.join(root, TINTED_DIR, hexcol, fn), tint_svg(svg, hexcol))
            attr = ' ifSubset="%s"' % sub if sub else ""
            lines.append("      <%s%s>./%s/%s/%s</%s>" % (prop, attr, TINTED_DIR, hexcol, fn, prop))
    return lines


def overwrite_theme(files, root, backup, help_dir=None):
    """Writes our A/B/X/Y icons over the theme's own and turns the help bar tint white, with the
    other help icons pointed at copies already in the theme's tint. Returns how many A/B/X/Y
    icons were written (0 if the theme uses the resource folder)."""
    icons, xmls = theme_help_icons(root)
    if not icons:
        return 0
    help_dir = help_dir or ES_HELP_DIR
    for letter, path in icons.items():
        _backup_once(path, root, backup)
        _write(path, files[THEME_ICON_FILE[letter]])
    shutil.rmtree(os.path.join(root, TINTED_DIR), ignore_errors=True)
    for path in xmls:
        text = _original(path, root, backup)

        def fix(m):
            block = m.group(0)
            col = re.search(r"<iconColor>([^<]*)</iconColor>", block)
            if not col:
                return block
            block = block.replace(col.group(0), "<iconColor>FFFFFFFF</iconColor>")
            if not re.search(r"<icon[ABXY]>", block):
                return block
            extra = _tinted_overrides(block, root, help_dir, theme_tints(root, col.group(1)))
            if not extra:
                return block
            end = block.rindex("</helpsystem>")
            return block[:end].rstrip(" ") + "\n".join(extra) + "\n    " + block[end:]

        new = _HELPSYSTEM.sub(fix, text)
        if new != text:
            _backup_once(path, root, backup)
            _write(path, new)
    return len(icons)


def apply(choice, theme_preset, help_dir=None, count=None, settings_path=ES_SETTINGS,
          theme_dirs=THEME_DIRS, backup_dir=THEME_BACKUP):
    """Writes the chosen scheme's icons to the resource folder and over the active theme's own,
    or with "use_theme" puts the theme's own back. Returns (scheme, message)."""
    help_dir = help_dir or ES_HELP_DIR
    if not supported(count):
        return None, COMING_SOON
    scheme = resolve(choice, theme_preset)
    name, root = active_theme(settings_path, theme_dirs)
    backup = os.path.join(backup_dir, name) if name else None
    files = render(DEFAULT_SCHEME if scheme == USE_THEME else scheme)
    os.makedirs(help_dir, exist_ok=True)
    for fn, text in files.items():
        _write(os.path.join(help_dir, fn), text)
    if scheme == USE_THEME:
        restore_theme(root, backup)
        return scheme, "%s's own buttons - shows after ES restarts" % (name or "the theme")
    wrote = overwrite_theme(files, root, backup, help_dir) if root and os.access(root, os.W_OK) else 0
    where = " (and over %s's own)" % name if wrote else ""
    return scheme, "%s buttons%s - shows after ES restarts" % (label(scheme), where)
