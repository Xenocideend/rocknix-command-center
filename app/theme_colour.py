"""theme_colour: the companion's system carousel background colour, matched to the installed
ES theme instead of plain black.

Two sources, neither needing cairo or pango so this loads anywhere:

(a) The theme XML (parse_theme_variables()/theme_background_color()). Free since the files
are already on disk, but only as good as the theme's declared colours. It cant see a
per-system background image the theme might show instead of a flat colour.

(b) Sampling DP-1 (SampleCache/capture_dp1()/decode_png()). Exact for whatever ES is really
drawing, but it costs a `grim` screenshot per system change, so it waits for scrolling to
settle (SettleGate) and never runs while a game does (should_sample()). The PNG decoder is
pure Python so there are no native dependencies.

What the device's theme, es-theme-PiStation-X, actually does (from
https://github.com/JandersonJS/es-theme-PiStation-X @ 377efd8, copies in
tests/fixtures/cc4-pistation-x-*-real-capture-*.xml):

theme.xml's top level <variables> sets backgroundColor (default "ffffff"), baseColor,
systemInfoColor and so on, one colour for the whole theme, not per system.

The per-system includes (infos/gba.xml and 418 more, pulled in with
`<include>./infos/${system.theme}.xml</include>`) only carry description and name text.

main/colors/*.xml redefine the same variables with other values (colorsets you can pick in
ES), but this theme.xml has no `<subset name="colorset">` wiring them in. So
theme_background_color() reads the default variables unless a caller passes a colorset file
in. There's no known way to read your live colorset pick.

theme.xml isnt well-formed (`--->` closes one comment), so parse_theme_variables() uses a
lenient regex scan instead of xml.etree, which would raise on it.

Since PiStation-X has no per-system colour, theme_background_color() also handles themes
that set one with `<view system="...">` and prefers it when there (per_system_background()).
"""
import logging
import os
import re
import statistics
import struct
import subprocess
import zlib

import screen_map

log = logging.getLogger("rp5deck.theme_colour")

THEME_ENTRY = "theme.xml"


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
def hex_to_rgb(s):
    """"RRGGBB" or "RRGGBBAA" -> (r, g, b, a) floats 0..1. Raises ValueError on anything else,
    callers take that as no usable colour.
    """
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
    """A readable text colour for a background, near black on light and near white on dark (WCAG
    style luminance, good enough for a name and count label).
    """
    return (0.06, 0.06, 0.08, 1.0) if _luminance(rgb) > 0.55 else (0.96, 0.96, 0.98, 1.0)


# ---------------------------------------------------------------------------
# (a) Theme XML: variables and the <include> chain, read leniently since the real theme.xml
# isnt well-formed
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
    """Removes <subset>/<view>/<feature> blocks. Their <variables>/<include> children only apply
    when that option, system or feature is active, so scanning every <variables> anywhere would
    treat every alternative as always on.

    The tag match needs whitespace, '/' or '>' after the name on both tags, never '.'. The real
    theme.xml has a variable named "subset.colorset", and a plain `<subset` match (a \\b boundary
    also matches before '.') pairs it with a much later `</subset>` and quietly deletes
    everything between, real <variables> block included. The tests cover this.
    """
    for tag in tags:
        pattern = re.compile(r"<%s(?=[\s/>])[^>]*?(?<!/)>.*?</%s(?=[\s>])\s*>" % (tag, tag), re.S)
        prev = None
        while prev != text:
            prev = text
            text = pattern.sub("", text)
    return text


def _top_level_variables_and_includes(text):
    """(vars_dict, [include_path, ...]) from the parts of `text` directly under <theme>, not inside
    <subset>/<view>/<feature>.
    """
    text = _COMMENT_RE.sub("", text)
    stripped = _strip_container_blocks(text, ("subset", "view", "feature"))
    merged = {}
    for m in _VARIABLES_RE.finditer(stripped):
        attrs = m.group(1) or ""
        if "lang=" in attrs:
            continue  # default (unlocalised) block only
        for km in _VAR_ENTRY_RE.finditer(m.group(2)):
            merged[km.group(1)] = km.group(2).strip()
    includes = []
    for m in _INCLUDE_RE.finditer(stripped):
        attrs = m.group(1) or ""
        if re.search(r"\bif\s*=", attrs):
            continue  # conditional, no runtime flag to evaluate it with
        body = (m.group(2) or "").strip()
        if body:
            includes.append(body)
    return merged, includes


def _resolve_refs(variables):
    """${name} -> another variable's value, a few passes until nothing changes (real themes nest
    1-2 deep, a cycle just stops changing and we bail).
    """
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
    """Merges every top level <variables> block reachable from theme_root/entry, following
    unconditional <include> tags like ES does (paths relative to the including file,
    "${system.theme}" replaced with `system`). A file's own variables win over what it included,
    which fits this theme's layout (<include> lines first, <variables> last).
    """
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
            continue  # a placeholder that cant be resolved, skip it
        inc_rel = os.path.normpath(os.path.join(base_dir, inc))
        result.update(parse_theme_variables(theme_root, system, read, isfile, inc_rel, _visited))
    result.update(merged)
    return result


def per_system_background(theme_xml_text, system):
    """A per-system `<view system="...">` background image's <color>, if the theme has one.
    PiStation-X doesnt, this is for themes that do.
    """
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
    """Walks up from a file ES gave us (like system_logo()'s
    "/storage/roms/themes/es-theme-PiStation-X/main/logos/gba.png") to the folder holding
    theme.xml, whatever the theme's own folder layout is.
    """
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
    """(bg_rgba, fg_rgba) for `system`'s carousel background from the theme's XML, or None if it
    has nothing usable (the caller keeps black). fg is the theme's systemInfoColor when it has
    one, since that's meant for this background, else a computed contrast colour.

    colorset: an optional colour variant name (PiStation-X ships main/colors/{blue,brawn,cyan,
    gray,green,orange,red,silver,violet,yellow}.xml). When given and the file exists under
    theme_root/colorset_dir its variables go over the theme's own before the colours are
    picked. Theres no way yet to know which colorset you picked in ES, so pass it in (from a
    setting, say) instead of assuming one.
    """
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
            # Only trust the theme's text colour if it's readable on this background. PiStation-X's
            # defaults are backgroundColor "ffffff" and systemInfoColor "FFFFFF", the same colour, since a
            # colorset is meant to replace both together, and white on white would ship unreadable.
            if abs(_luminance(candidate) - _luminance(bg)) >= 0.3:
                fg = candidate
        except ValueError:
            fg = None
    return bg, (fg if fg is not None else contrast_text_color(bg))


# ---------------------------------------------------------------------------
# (b) Sampling DP-1: a PNG decoder with no cairo or PIL, plus the median border colour
# ---------------------------------------------------------------------------
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class PngError(Exception):
    pass


def decode_png(data):
    """(w, h, rgba bytes, len == w*h*4). Handles 8-bit non-interlaced truecolor (type 2) and
    truecolor+alpha (6), which is what `grim -t png` makes. Anything else raises PngError and
    the caller skips the sample.
    """
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
    """The centre band where the carousel and logo sit, in screenshot pixels, from the theme's
    main/systemview/reflected.xml: <carousel><pos>0 0.39</pos><size>1 0.3925</size></carousel>
    with the logo inside. There's a generous margin around it (0.28 to 0.82 of the height, full
    width) so the logo and the carousel's glow dont leak into the border sample.

    This comes from the theme's declared layout and hasnt been measured on the real DP-1, so
    check it still clears the logo before trusting sample mode.
    """
    return (0, int(h * 0.28), w, int(h * 0.54))


def median_border_colour(w, h, rgba, exclude_box=None, band_frac=0.06, stride=None):
    """Median R/G/B of the outer border ring (strips band_frac of min(w, h) thick), skipping pixels
    inside exclude_box=(x, y, box_w, box_h). Median so one odd bright or dark pixel cant skew
    it. None if nothing was sampled.
    """
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


# Quarter size with no compression. On the device that took 0.36 s and gave the same median
# as the full 1920x1080 capture, which took 3.45 s.
DEFAULT_GRIM_CMD = ("grim", "-t", "png", "-l", "0", "-s", "0.25", "-o", screen_map.CURRENT.top, "-")


def capture_dp1(run=subprocess.run, cmd=DEFAULT_GRIM_CMD, timeout=2.0):
    """One read-only `grim` screenshot of DP-1 (config.SCREEN_TO_OUTPUT["addon_top"]). Never
    raises, a missing grim, a timeout or a bad exit all return None.
    """
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
    """The one gate every caller needs: never sample while a game runs. The game needs the CPU and
    its window covers DP-1 anyway, so the capture would show the game.
    """
    return source == "sample" and not running


class SettleGate:
    """Debounce for sample mode. Fast carousel scrolling fires a system-selected event per tick,
    but only the last one (after `settle_s` of quiet) should trigger a capture. Pure logic with a
    fake clock in tests, the real timer lives in companion.py.
    """

    def __init__(self, settle_s=0.4):
        self.settle_s = settle_s
        self._pending = None
        self._due = None

    def note(self, system, now):
        """A system was selected (or selected again), restart the settle window and drop whatever was
        pending.
        """
        self._pending = system
        self._due = now + self.settle_s

    def ready(self, now):
        """The system whose settle window has passed, exactly once (another call with no note() in
        between returns None). Call it from the timer tick that would fire the capture.
        """
        if self._pending is not None and self._due is not None and now >= self._due - 1e-9:
            system = self._pending
            self._pending = self._due = None
            return system
        return None

    def cancel(self):
        self._pending = self._due = None


class SampleCache:
    """(b) put together: one grim capture per system, kept in memory. capture() runs on a worker
    thread, never raises and returns None on any failure.
    """

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
        """Worker thread entry: grim -> decode -> median colour, cached under `system`. Returns
        (bg, fg) or None.
        """
        png = self._capture()
        if png is None:
            return None
        self.captures += 1
        try:
            w, h, rgba = self._decode(png)
        except PngError as e:
            log.info("%s capture for %s: %s", screen_map.CURRENT.top, system, e)
            return None
        box = self._exclude_box(w, h) if callable(self._exclude_box) else self._exclude_box
        bg = self._median(w, h, rgba, box)
        if bg is None:
            return None
        result = (bg, contrast_text_color(bg))
        self._cache[system] = result
        return result
