"""theme_colour - CC4: the companion's system-carousel background colour
should match the installed ES theme instead of hard-coded BLACK
(companion.py CompanionView.draw(): `g.fill_rect(self.rect, BLACK)`).

Two independent sources, both implemented and unit-tested here, neither
importing companion.py/gfx.py/media.py (no cairo/pango - this module loads
cleanly on Windows, Linux, with or without the native libs):

  (a) THEME XML, via parse_theme_variables()/theme_background_color().
      Free (files already on disk, no device I/O beyond what system_logo()
      already does), but only as good as the theme's *declared* colours -
      it does not see a per-system BACKGROUND IMAGE the theme's own
      "systembackground" subset might be showing instead of a flat colour
      (see the module docstring section below - confirmed against the
      device's real theme, es-theme-PiStation-X).

  (b) DP-1 SCREEN SAMPLING, via SampleCache/capture_dp1()/decode_png().
      Exact for whatever ES is really drawing (gradients, per-system
      images, anything) but costs one `grim` screenshot per system change,
      so it is debounced (SettleGate) and gated off entirely while a game
      runs (should_sample()). A pure-Python PNG decoder is used instead of
      cairo/PIL specifically so this module has zero native dependencies.

Real-capture research on es-theme-PiStation-X (the device's installed
theme - companion.py's system_logo() already fetches paths under
"/storage/roms/themes/es-theme-PiStation-X/main/logos/..."), fetched from
https://github.com/JandersonJS/es-theme-PiStation-X @ 377efd8
(2026-09-24) - see tests/fixtures/cc4-pistation-x-*-real-capture-*.xml:

  - theme.xml's top-level <variables> block defines backgroundColor
    (default "ffffff"), baseColor, systemInfoColor, etc - one colour PER
    THEME, not per system.
  - infos/gba.xml (one of 419 per-system include files theme.xml pulls in
    via `<include>./infos/${system.theme}.xml</include>`) carries only
    system_description/system_name text, no colour or background override
    - checked for gba specifically, matching DESIGN.md's worked example.
  - main/colors/{blue,brawn,cyan,...}.xml each redefine the SAME variable
    names with different hex values (alternate "colorset" choices a user
    can pick in ES's theme options) - but this copy of theme.xml has no
    `<subset name="colorset">` wiring them in (main/colors/ looks
    orphaned/legacy in this theme revision). theme_background_color()
    therefore reads the theme's *default* (unselected-colorset) variables
    unless a caller passes one of these files in explicitly - there is no
    reliable, confirmed way to learn the user's live colorset pick without
    the device (see the CC4 report's device checklist).
  - theme.xml itself is not strictly well-formed (`--->` instead of `-->`
    closes one comment) - same class of bug as the "XML comment cannot
    hold --" memory note. parse_theme_variables() therefore does NOT use
    xml.etree (it would raise ParseError on this real file); it uses a
    lenient regex scan instead, same approach already used elsewhere in
    this repo for malformed ES XML (gamelists).

Because PiStation-X has no per-system colour, theme_background_color()
also supports the MORE GENERAL case some other theme might use - a
per-system `<view system="...">` background colour override - and prefers
that when present (per_system_background()).
"""
import logging
import os
import re
import statistics
import struct
import subprocess
import zlib

log = logging.getLogger("rp5deck.theme_colour")

THEME_ENTRY = "theme.xml"


# ---------------------------------------------------------------------------
# Colour helpers (pure)
# ---------------------------------------------------------------------------
def hex_to_rgb(s):
    """"RRGGBB" or "RRGGBBAA" -> (r, g, b, a) floats 0..1. Raises ValueError
    on anything else (missing/short/non-hex) - callers treat that as "no
    usable colour", never a crash."""
    s = (s or "").strip().lstrip("#")
    if len(s) not in (6, 8) or not re.match(r"^[0-9A-Fa-f]+$", s):
        raise ValueError("not a hex colour: %r" % s)
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    a = int(s[6:8], 16) / 255.0 if len(s) == 8 else 1.0
    return (r, g, b, a)


def _luminance(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def contrast_text_color(rgb):
    """A readable text colour for a background - near-black on a light
    background, near-white on a dark one (WCAG-style relative luminance,
    not exact contrast ratio - good enough for a system name/count label)."""
    return (0.06, 0.06, 0.08, 1.0) if _luminance(rgb) > 0.55 else (0.96, 0.96, 0.98, 1.0)


# ---------------------------------------------------------------------------
# (a) Theme XML: variables + <include> chain, lenient (not xml.etree - see
# module docstring: real theme.xml is not well-formed XML)
# ---------------------------------------------------------------------------
_COMMENT_RE = re.compile(r"<!--.*?--+>", re.S)
_VARIABLES_RE = re.compile(r"<variables(\s[^>]*)?>(.*?)</variables(?=[\s>])\s*>", re.S)
_VAR_ENTRY_RE = re.compile(r"<(\w[\w.]*)>([^<]*)</\1>")
_INCLUDE_RE = re.compile(r"<include\b([^>]*?)/?>(?:(.*?)</include(?=[\s>])\s*>)?", re.S)
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z0-9_.]+)\}")
_SYSTEM_VIEW_RE = re.compile(r'<view\b[^>]*\bsystem\s*=\s*"([^"]*)"[^>]*>(.*?)</view(?=[\s>])\s*>', re.S)
_BG_IMAGE_RE = re.compile(r'<image\s+name\s*=\s*"background"[^>]*>(.*?)</image(?=[\s>])\s*>', re.S)
_COLOR_TAG_RE = re.compile(r"<color>\s*([0-9A-Fa-f]{6,8})\s*</color>")


def _strip_container_blocks(text, tags):
    """Remove <subset>/<view>/<feature> blocks - their <variables>/
    <include> children are CONDITIONAL (only apply if that theme option/
    system/feature is the active one), so a plain "any <variables>
    anywhere" scan would wrongly treat every alternative as always-applied.

    A tag-name match must require the next character to be whitespace, '/'
    or '>' - never '.' - on BOTH the open and close tag, because real
    theme.xml defines a *variable* literally named "subset.colorset"
    (`<subset.colorset>Colorset</subset.colorset>`) which a naive `<subset`
    match (with a `\\b` boundary, which also matches before '.') pairs with
    the wrong, much later `</subset>` and silently deletes everything in
    between, including the real <variables> block. Proven against the real
    downloaded theme.xml during development; regression-tested here."""
    for tag in tags:
        pattern = re.compile(r"<%s(?=[\s/>])[^>]*?(?<!/)>.*?</%s(?=[\s>])\s*>" % (tag, tag), re.S)
        prev = None
        while prev != text:
            prev = text
            text = pattern.sub("", text)
    return text


def _top_level_variables_and_includes(text):
    """(vars_dict, [include_path, ...]) from the parts of `text` that are
    direct children of <theme> - not inside <subset>/<view>/<feature>."""
    text = _COMMENT_RE.sub("", text)
    stripped = _strip_container_blocks(text, ("subset", "view", "feature"))
    merged = {}
    for m in _VARIABLES_RE.finditer(stripped):
        attrs = m.group(1) or ""
        if "lang=" in attrs:
            continue                        # default (unlocalised) block only
        for km in _VAR_ENTRY_RE.finditer(m.group(2)):
            merged[km.group(1)] = km.group(2).strip()
    includes = []
    for m in _INCLUDE_RE.finditer(stripped):
        attrs = m.group(1) or ""
        if re.search(r"\bif\s*=", attrs):
            continue                        # conditional - no runtime flag to evaluate it with
        body = (m.group(2) or "").strip()
        if body:
            includes.append(body)
    return merged, includes


def _resolve_refs(variables):
    """${name} -> another collected variable's value, a few passes to a
    fixed point (real themes nest at most 1-2 deep; a cycle just stops
    changing and we bail, never hangs)."""
    out = dict(variables)
    for _ in range(4):
        changed = False
        for k, v in list(out.items()):
            nv = _VAR_REF_RE.sub(lambda m: out.get(m.group(1), m.group(0)), v)
            if nv != v:
                out[k] = nv
                changed = True
        if not changed:
            break
    return out


def _read_text(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def parse_theme_variables(theme_root, system=None, read=_read_text, isfile=os.path.isfile,
                           entry=THEME_ENTRY, _visited=None):
    """Merge every top-level <variables> block reachable from
    theme_root/entry, following unconditional <include> tags the way ES
    does (path relative to the including file; "${system.theme}"
    substituted with `system`). A file's OWN variables win over whatever
    it included (matches this theme's real layout: <include> lines are
    textually first, <variables> last) - see the module docstring's
    real-capture summary."""
    _visited = _visited if _visited is not None else set()
    path = os.path.normpath(os.path.join(theme_root, entry))
    if path in _visited or not isfile(path):
        return {}
    _visited.add(path)
    try:
        text = read(path)
    except OSError:
        return {}
    merged, includes = _top_level_variables_and_includes(text)
    result = {}
    base_dir = os.path.dirname(entry)
    for inc in includes:
        inc = inc.replace("${system.theme}", system or "")
        if "${" in inc:
            continue                        # an unresolvable placeholder - skip, don't crash
        inc_rel = os.path.normpath(os.path.join(base_dir, inc))
        result.update(parse_theme_variables(theme_root, system, read, isfile, inc_rel, _visited))
    result.update(merged)
    return result


def per_system_background(theme_xml_text, system):
    """A per-system `<view system="...">` background image's <color>, if
    the theme defines one (PiStation-X does not - see module docstring;
    this exists for themes that DO, so the parser genuinely answers
    "the colour for a given system", not just "the theme's one colour")."""
    text = _COMMENT_RE.sub("", theme_xml_text)
    for m in _SYSTEM_VIEW_RE.finditer(text):
        names = [n.strip() for n in re.split(r"[,\s]+", m.group(1)) if n.strip()]
        if system not in names:
            continue
        bg = _BG_IMAGE_RE.search(m.group(2))
        if not bg:
            continue
        cm = _COLOR_TAG_RE.search(bg.group(1))
        if cm:
            return cm.group(1)
    return None


def find_theme_root(any_theme_file_path, isfile=os.path.isfile):
    """Walk up from a file ES handed us (system_logo()'s
    "/storage/roms/themes/es-theme-PiStation-X/main/logos/gba.png") to the
    directory holding theme.xml - independent of a theme's own internal
    folder layout, which varies theme to theme."""
    if not any_theme_file_path:
        return None
    d = os.path.dirname(os.path.normpath(any_theme_file_path))
    seen = set()
    while d and d not in seen:
        seen.add(d)
        if isfile(os.path.join(d, THEME_ENTRY)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def theme_background_color(theme_root, system, read=_read_text, isfile=os.path.isfile,
                            colorset=None, colorset_dir="main/colors"):
    """(bg_rgba, fg_rgba) for `system`'s carousel background from the
    theme's own XML, or None if it has nothing usable (caller keeps the
    existing BLACK). fg is the theme's own systemInfoColor when it defines
    one (it is meant to sit on this exact background), else a computed
    contrast colour.

    colorset: an optional theme colour-variant name (PiStation-X ships
    main/colors/{blue,brawn,cyan,gray,green,orange,red,silver,violet,
    yellow}.xml - each a full <variables> block overriding backgroundColor/
    systemInfoColor/etc). When given and the file exists under
    theme_root/colorset_dir, its variables are overlaid on top of the
    theme's own (last write wins) before backgroundColor/systemInfoColor
    are picked. There is no confirmed, device-verified way to learn which
    colorset (if any) the user actually has selected in ES - see the
    module docstring - so callers that want this should pass it in
    explicitly (e.g. from a config setting), not assume one."""
    entry_path = os.path.join(theme_root, THEME_ENTRY)
    if not isfile(entry_path):
        return None
    try:
        root_text = read(entry_path)
    except OSError:
        return None
    per_system_hex = per_system_background(root_text, system)
    if per_system_hex:
        try:
            bg = hex_to_rgb(per_system_hex)
            return bg, contrast_text_color(bg)
        except ValueError:
            pass
    variables = parse_theme_variables(theme_root, system, read, isfile)
    if colorset:
        cs_path = os.path.join(theme_root, colorset_dir, "%s.xml" % colorset)
        if isfile(cs_path):
            try:
                cs_vars, _incs = _top_level_variables_and_includes(read(cs_path))
                variables = dict(variables)
                variables.update(cs_vars)
            except OSError:
                pass
    variables = _resolve_refs(variables)
    hexval = variables.get("backgroundColor")
    if not hexval:
        return None
    try:
        bg = hex_to_rgb(hexval)
    except ValueError:
        return None
    fg_hex = variables.get("systemInfoColor")
    fg = None
    if fg_hex:
        try:
            candidate = hex_to_rgb(fg_hex)
            # Only trust the theme's own text colour if it is actually
            # readable on this background - real-capture proof: PiStation-
            # X's UN-colorset-overridden defaults are backgroundColor
            # "ffffff" and systemInfoColor "FFFFFF" (the same colour),
            # because both are meant to be replaced together by a chosen
            # colorset (main/colors/*.xml) that this parser cannot
            # currently resolve (module docstring) - white-on-white would
            # otherwise ship silently unreadable.
            if abs(_luminance(candidate) - _luminance(bg)) >= 0.3:
                fg = candidate
        except ValueError:
            fg = None
    return bg, (fg if fg is not None else contrast_text_color(bg))


# ---------------------------------------------------------------------------
# (b) DP-1 screen sampling: a dependency-free PNG decoder (no cairo/PIL -
# this module must import cleanly on Windows) + median border colour.
# ---------------------------------------------------------------------------
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class PngError(Exception):
    pass


def decode_png(data):
    """(w, h, rgba bytes, len == w*h*4). Supports 8-bit, non-interlaced
    truecolor (colour type 2) and truecolor+alpha (6) - what `grim -t png`
    produces; anything else raises PngError (caller treats as "no sample
    this time", never a crash)."""
    if data[:8] != _PNG_SIG:
        raise PngError("not a PNG (bad signature)")
    pos = 8
    w = h = bitdepth = colortype = interlace = None
    idat = bytearray()
    n = len(data)
    while pos + 8 <= n:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, bitdepth, colortype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    if w is None:
        raise PngError("no IHDR chunk")
    if bitdepth != 8 or interlace != 0 or colortype not in (2, 6):
        raise PngError("unsupported PNG variant (bitdepth=%r colortype=%r interlace=%r)"
                        % (bitdepth, colortype, interlace))
    channels = 3 if colortype == 2 else 4
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise PngError("zlib: %s" % e)
    stride = w * channels
    out = bytearray(w * h * 4)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        if pos >= len(raw):
            raise PngError("truncated scanline data")
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if len(line) != stride:
            raise PngError("truncated scanline %d" % y)
        _unfilter(ftype, line, prev, channels)
        row_off = y * w * 4
        if channels == 4:
            out[row_off:row_off + stride] = line
        else:
            for x in range(w):
                o = row_off + x * 4
                i = x * 3
                out[o] = line[i]
                out[o + 1] = line[i + 1]
                out[o + 2] = line[i + 2]
                out[o + 3] = 255
        prev = line
    return w, h, bytes(out)


def _unfilter(ftype, line, prev, bpp):
    n = len(line)
    if ftype == 0:
        return
    if ftype == 1:                          # Sub
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:                        # Up
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:                        # Average
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:                        # Paeth
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            c = prev[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
    else:
        raise PngError("unsupported filter type %d" % ftype)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def default_exclude_box(w, h):
    """The centre band where the carousel/logo sits, in screenshot pixel
    space - from the real theme's main/systemview/reflected.xml
    (real-capture fixture): <carousel><pos>0 0.39</pos>
    <size>1 0.3925</size></carousel>, with <image name="logo"> inside it.
    A generous margin around that band (0.28..0.82 of the height, full
    width) so neither the logo nor the carousel's own glow/fade leaks into
    the border sample.

    DEVICE CHECKLIST: this fraction is derived from the theme's declared
    layout, not measured on the device's real DP-1 resolution - confirm it
    still clears the actual on-screen logo before trusting "sample" mode,
    and re-tune the fractions here if not (see the CC4 report)."""
    return (0, int(h * 0.28), w, int(h * 0.54))


def median_border_colour(w, h, rgba, exclude_box=None, band_frac=0.06, stride=None):
    """Median R/G/B of the outer border ring (top/bottom/left/right strips
    `band_frac` of min(w, h) thick), skipping any pixel inside
    exclude_box=(x, y, box_w, box_h). Median (not mean) resists a stray
    bright/dark artifact skewing the result. None if nothing was sampled
    (never happens unless exclude_box covers the whole image)."""
    band = max(2, int(min(w, h) * band_frac))
    step = stride or max(1, w // 64)
    vstep = stride or max(1, h // 64)

    def excluded(x, y):
        if exclude_box is None:
            return False
        ex, ey, ew, eh = exclude_box
        return ex <= x < ex + ew and ey <= y < ey + eh

    rs, gs, bs = [], [], []

    def take(x, y):
        if excluded(x, y):
            return
        o = (y * w + x) * 4
        rs.append(rgba[o])
        gs.append(rgba[o + 1])
        bs.append(rgba[o + 2])

    for y in list(range(0, band)) + list(range(max(0, h - band), h)):
        for x in range(0, w, step):
            take(x, y)
    for x in list(range(0, band)) + list(range(max(0, w - band), w)):
        for y in range(0, h, vstep):
            take(x, y)
    if not rs:
        return None
    r = statistics.median(rs) / 255.0
    g = statistics.median(gs) / 255.0
    b = statistics.median(bs) / 255.0
    return (r, g, b, 1.0)


# Quarter size, no compression: the device measured 0.36 s per capture and
# the same median colour as the full 1920x1080 capture (3.45 s), 24 Sep.
DEFAULT_GRIM_CMD = ("grim", "-t", "png", "-l", "0", "-s", "0.25", "-o", "DP-1", "-")


def capture_dp1(run=subprocess.run, cmd=DEFAULT_GRIM_CMD, timeout=2.0):
    """One read-only `grim` screenshot of DP-1 (the ES/top-screen output -
    config.SCREEN_TO_OUTPUT["addon_top"]). Never raises: a missing grim
    binary, a timeout, or a non-zero exit all just return None, so a
    worker-thread caller can treat "no colour this time" uniformly."""
    try:
        r = run(cmd, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.info("grim capture failed: %s", e)
        return None
    if getattr(r, "returncode", 1) != 0 or not r.stdout:
        log.info("grim exit %s", getattr(r, "returncode", "?"))
        return None
    return r.stdout


def should_sample(source, running):
    """The one gate every caller must apply: never sample while a game is
    running (RV1-M5-style rule - the game needs the CPU, and its window
    covers DP-1 anyway so the capture would show the game, not ES)."""
    return source == "sample" and not running


class SettleGate:
    """Debounce for the "sample" source: fast carousel scrolling fires a
    system-selected event per tick, but only the LAST one (after
    `settle_s` with no further change) should trigger a `grim` capture.
    Pure logic (fake clock in tests) - the real timer callback lives in
    companion.py's own Timers (call_later/cancel), wired by
    patches/CC4-companion.patch."""

    def __init__(self, settle_s=0.4):
        self.settle_s = settle_s
        self._pending = None
        self._due = None

    def note(self, system, now):
        """A new system was selected (or re-selected): (re)start the
        settle window - cancels whatever was pending before."""
        self._pending = system
        self._due = now + self.settle_s

    def ready(self, now):
        """The system whose settle window has elapsed, exactly once (a
        later call with no intervening note() returns None) - call this
        from the same timer tick that would fire the capture."""
        if self._pending is not None and self._due is not None and now >= self._due - 1e-9:
            system = self._pending
            self._pending = self._due = None
            return system
        return None

    def cancel(self):
        self._pending = self._due = None


class SampleCache:
    """(b), glued together: one grim capture per system, remembered in
    memory. capture() is meant to run on a worker thread (submit()); it
    never raises and returns None on any failure so a caller can just
    check the result."""

    def __init__(self, run=subprocess.run, cmd=DEFAULT_GRIM_CMD, timeout=2.0,
                 exclude_box=default_exclude_box, decode=decode_png,
                 median=median_border_colour, capture=None):
        self._run = run
        self._cmd = cmd
        self._timeout = timeout
        self._exclude_box = exclude_box
        self._decode = decode
        self._median = median
        self._capture = capture or (lambda: capture_dp1(self._run, self._cmd, self._timeout))
        self._cache = {}
        self.captures = 0

    def cached(self, system):
        return self._cache.get(system)

    def forget(self, system=None):
        if system is None:
            self._cache.clear()
        else:
            self._cache.pop(system, None)

    def capture(self, system):
        """Worker-thread entry point: grim -> decode -> median colour,
        cached under `system`. Returns (bg, fg) or None."""
        png = self._capture()
        if png is None:
            return None
        self.captures += 1
        try:
            w, h, rgba = self._decode(png)
        except PngError as e:
            log.info("DP-1 capture for %s: %s", system, e)
            return None
        box = self._exclude_box(w, h) if callable(self._exclude_box) else self._exclude_box
        bg = self._median(w, h, rgba, box)
        if bg is None:
            return None
        result = (bg, contrast_text_color(bg))
        self._cache[system] = result
        return result
